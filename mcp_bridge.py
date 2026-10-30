"""Stdio MCP adapter for one session-owned CPU demo or a prevalidated fixed workflow."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import uuid

from lab_client import ROOT, Client, ClientError, atomic_json, now, private_profile, read_json,bound_workflow

FINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}


class BridgeError(ValueError):
    pass


class Bridge:
    def __init__(self, session_path):
        self.path = Path(session_path).resolve()
        self.session = read_json(self.path)
        if not self.session or not self.session.get('run_id'):
            raise BridgeError('Missing local session manifest')
        self.directory = self.path.parent
        self.state_path = self.directory/'mcp-state.json'
        if not self.state_path.exists():
            atomic_json(self.state_path, {'jobs': []})

    def api(self, path, data=None, raw=False):
        try:
            client = Client.from_profile(self.session.get('client_profile', ROOT/'runtime/config.json'))
            if self.session.get('identity') and client.request('/api/me').get('id')!=self.session['identity']:
                raise BridgeError('Client credential identity changed during this conversation')
            return client.request(path, data, raw=raw, max_bytes=1024*1024)
        except ClientError as error:
            raise BridgeError(str(error)) from None

    def owned(self, job_id):
        if job_id not in (read_json(self.state_path) or {}).get('jobs', []):
            raise BridgeError('This job is not owned by the current Agent session')
        return job_id

    def state(self, job_id):
        job = self.api('/api/jobs/'+self.owned(job_id))['job']
        return {k: job.get(k) for k in ['id','name','owner','project','node_id','attempt_id','state','reason','environment','resources','allocation','started_at','finished_at','result','archive_dir']}

    def call(self, name, args):
        if not isinstance(args, dict):
            raise BridgeError('Arguments must be an object')
        schema = next((t['inputSchema'] for t in tools(self.session) if t['name'] == name), None)
        if schema is None:
            raise BridgeError('Unknown tool')
        if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
            raise BridgeError('Unexpected or missing tool arguments')
        if name == 'queue_status':
            summary = self.api('/api/jobs')
            policy={'template': 'cpu_demo', 'npu': 0, 'max_jobs': 1,
                    'allowed_environment': self.session['environment'], 'max_seconds': 10}
            if self.session.get('workflow_request'):
                request=self.session['workflow_request'];policy={'template':request['template'],'input_id':request['parameters']['input_id'],
                    'resources':request['parameters']['resources'],'timeout_seconds':request['parameters']['timeout_seconds'],
                    'max_jobs':1,'allowed_environment':self.session['environment'],'command_overrides':False}
                policy['persistent_cache']=request['parameters'].get('cache')
                policy['depends_on']=request.get('depends_on',[]);policy['artifact_inputs']=request.get('artifact_inputs',[])
            return {'policy': summary['policy'], 'reserved': summary['reserved'],
                    'environments': summary['environments'],
                    'session_policy': policy,
                    'session_jobs': (read_json(self.state_path) or {}).get('jobs', [])}
        if name == 'submit_job':
            workflow=self.session.get('workflow_request')
            allowed = {'template','environment','request_key','name'}|({'seconds'} if not workflow else set())
            if set(args)-allowed or args.get('template') != (workflow['template'] if workflow else 'cpu_demo'):
                raise BridgeError('Template or arguments are outside the fixed session policy')
            environment = args.get('environment', self.session['environment'])
            if environment != self.session['environment']:
                raise BridgeError('Environment is outside this session policy')
            seconds = args.get('seconds', self.session['seconds'])
            if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 10:
                raise BridgeError('seconds must be an integer from 1 to 10')
            key = args.get('request_key')
            if not isinstance(key, str) or not 1 <= len(key) <= 40 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in key):
                raise BridgeError('Provide a short stable request_key; reuse it after connection failures')
            state = read_json(self.state_path)
            prior = self.directory/'submitted-request.json'
            previous = read_json(prior)
            body = {'name': args.get('name') or 'Agent CPU 示例 · '+self.session['run_id'][-6:],
                    'environment': environment, 'kind':'command', 'cwd': str(ROOT),
                    'argv':['python','-u','examples/cpu_task.py','--seconds',str(seconds)],
                    'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0,'timeout_seconds':30,
                    'idempotency_key':self.session['run_id']+':'+key,
                    'origin': {'client':self.session.get('client','opencode-mcp'),'model':self.session['model'], 'agent_run_id':self.session['run_id']}}
            if workflow:
                body={**workflow,'name':args.get('name') or workflow.get('name') or body['name'],
                      'idempotency_key':body['idempotency_key'],'origin':body['origin']}
            elif self.session.get('execution_mode') == 'templates':
                body = {k:v for k,v in body.items() if k in {'name','environment','idempotency_key','origin'}}
                body.update(template='cpu_demo',parameters={'seconds':seconds})
            if previous and body != previous:
                raise BridgeError('One task is allowed in this session; inspect the submitted task instead')
            atomic_json(prior, body)
            result = self.api('/api/jobs', body)
            job_id = result['job']['id']
            if job_id not in state['jobs']:
                state['jobs'].append(job_id)
                atomic_json(self.state_path, state)
            return {'job': self.state(job_id), 'reused': result['reused']}
        if name in {'get_job','wait_job','get_logs','get_result','cancel_job'}:
            job_id = self.owned(args.get('job_id'))
            if name == 'get_job':
                return self.state(job_id)
            if name == 'wait_job':
                seconds = args.get('timeout_seconds', 15)
                if isinstance(seconds,bool) or not isinstance(seconds,int) or not 1<=seconds<=15:
                    raise BridgeError('Wait duration must be between 1 and 15 seconds')
                deadline = time.monotonic()+seconds
                while True:
                    job = self.state(job_id)
                    if job['state'] in FINAL or time.monotonic()>=deadline:
                        return {'terminal':job['state'] in FINAL,'needs_attention':job['state']=='needs_attention',
                                'note':'Execution state is unconfirmed; resources remain reserved. Do not submit a replacement task.' if job['state']=='needs_attention' else '',
                                'job':job}
                    time.sleep(.5)
            if name == 'get_logs':
                stream = args.get('stream','stdout')
                if stream not in {'stdout','stderr'}:
                    raise BridgeError('Invalid stream')
                result = self.api('/api/jobs/'+job_id+'/log?stream='+stream)
                return {'job_id':job_id,'stream':stream,'text':result['text'][-8192:],
                        'bytes':result['bytes'],'truncated':result['truncated'] or len(result['text'])>8192}
            if name == 'get_result':
                job = self.state(job_id)
                if self.session.get('workflow_request'):
                    if job['state'] not in FINAL:return {'job':job,'artifact':None,'note':'Workflow remains managed; result is not terminal'}
                    try:record=self.api('/api/jobs/'+job_id+'/artifact?name=validated-result.json',raw=True)
                    except ClientError as error:
                        if str(error)!='Controller HTTP 404' or job['state']=='succeeded':raise
                        record=None
                    value={'job_id':job_id,'status':job['state'],'artifact_name':'validated-result.json' if record is not None else None,
                           'artifact':json.loads(record) if record is not None else None}
                    result_file=self.session['workflow_request']['parameters']['result_file']
                    if job['state']=='succeeded':
                        import urllib.parse
                        raw=self.api('/api/jobs/'+job_id+'/artifact?name='+urllib.parse.quote(result_file,safe=''),raw=True)
                        value['workload_result']=json.loads(raw) if len(raw)<=16384 else {'note':'Raw result exceeds tool preview; download the archived artifact','artifact':result_file}
                    return self.archive_result(value,job_id)
                if job['state'] != 'succeeded':
                    value={'job':job,'artifact':None,'note':'Do not claim success without a successful job and its result artifact'}
                    return self.archive_result(value,job_id) if job['state'] in FINAL else value
                body = self.api('/api/jobs/'+job_id+'/artifact?name=results%2Fdemo-result.json',raw=True)
                value={'job_id':job_id,'status':job['state'],'exit_code':job['result']['exit_code'],
                       'artifact_name':'results/demo-result.json','artifact':json.loads(body)}
                return self.archive_result(value,job_id)
            return {'job': self.api('/api/jobs/'+job_id+'/cancel',{})['job']}
        raise BridgeError('Unknown tool')

    def archive_result(self,value,job_id):
        if self.session.get('personal_archive'):
            from client_archive import download
            value['local_archive']=download(Client.from_profile(self.session['client_profile']),job_id,
                                           profile=self.session['client_profile'],root=self.session['personal_archive'])
        return value

    def audit(self, name, args, result, error=False):
        # Contains only this session's bounded tool arguments/results, never provider credentials.
        record={'at':now(),'tool':name,'arguments':args,'is_error':error,'result':result}
        with (self.directory/'mcp-audit.jsonl').open('a') as file:
            file.write(json.dumps(record,ensure_ascii=False)+'\n')


def tools(session=None):
    def tool(name,description,properties,required=(),readonly=True):
        return {'name':name,'description':description,
                'inputSchema':{'type':'object','properties':properties,'required':list(required),'additionalProperties':False},
                'annotations':{'readOnlyHint':readonly,'destructiveHint':name=='cancel_job','idempotentHint':name!='submit_job','openWorldHint':False}}
    jid={'type':'string','pattern':'^job-[0-9a-f]{12}$'}
    registered=[
        tool('queue_status','Read scheduler resource budgets and this session policy; does not expose unrelated task details.',{}),
        tool('submit_job','Submit one real CPU demo to the central queue. This never runs a command directly. Reuse the same request_key and arguments on retries.',
             {'template':{'type':'string','enum':['cpu_demo']},'environment':{'type':'string','enum':['host-python','zdl-python','zdl-mxh-dev','worker-python']},
              'seconds':{'type':'integer','minimum':1,'maximum':10},'request_key':{'type':'string','minLength':1,'maxLength':40},
              'name':{'type':'string','maxLength':100}},('template','request_key'),False),
        tool('get_job','Read status of a job owned by this Agent session.',{'job_id':jid},('job_id',)),
        tool('wait_job','Wait at most 15 seconds for this session job. Queue continues after Agent disconnects.',
             {'job_id':jid,'timeout_seconds':{'type':'integer','minimum':1,'maximum':15}},('job_id',)),
        tool('get_logs','Read bounded stdout/stderr of this session job.',{'job_id':jid,'stream':{'type':'string','enum':['stdout','stderr']}},('job_id',)),
        tool('get_result','Read the actual demo-result.json of a successful session job. Distinguish observed facts from model claims.',{'job_id':jid},('job_id',)),
        tool('cancel_job','Request cancellation only of a job created by this session. Never cancel other or unmanaged jobs.',{'job_id':jid},('job_id',),False),
    ]
    request=(session or {}).get('workflow_request')
    if request:
        submit=next(tool for tool in registered if tool['name']=='submit_job')
        submit['description']='Submit this session\'s prevalidated frozen workflow to the central queue; command, input, lock and resources cannot be overridden. Reuse the request_key on retries.'
        submit['inputSchema']['properties']['template']['enum']=[request['template']]
        submit['inputSchema']['properties'].pop('seconds')
        next(tool for tool in registered if tool['name']=='get_result')['description']='Read controller result contract and protected environment evidence for this session workflow; workload assertions are not independent numerical certification.'
    if (session or {}).get('personal_archive'):
        result=next(tool for tool in registered if tool['name']=='get_result')
        result['annotations']['readOnlyHint']=False
        result['description']+=' Download and verify the terminal archive into the personal directory fixed at session creation; existing different files are never overwritten.'
    return registered


def serve(bridge, instream=sys.stdin, outstream=sys.stdout):
    def send(data):
        outstream.write(json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n')
        outstream.flush()
    for line in instream:
        request=None
        try:
            if len(line.encode())>32768:
                raise BridgeError('MCP message too large')
            request=json.loads(line)
            if not isinstance(request,dict) or request.get('jsonrpc')!='2.0':
                raise BridgeError('Invalid JSON-RPC request')
            if 'id' not in request:
                continue
            method=request.get('method');params=request.get('params') or {}
            if method=='initialize':
                version=params.get('protocolVersion','2025-06-18')
                if version not in {'2024-11-05','2025-03-26','2025-06-18','2025-11-25'}:version='2025-06-18'
                result={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},
                        'serverInfo':{'name':'ascend-lab','version':'0.11.0'},
                        'instructions':'Only use the central queue; this session permits one fixed task and only its own job IDs.'}
            elif method=='ping':result={}
            elif method=='tools/list':result={'tools':tools(bridge.session)}
            elif method=='tools/call':
                name=params.get('name');arguments=params.get('arguments') or {}
                try:
                    value=bridge.call(name,arguments)
                    bridge.audit(name,arguments,value)
                    result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],'isError':False}
                except (BridgeError,ValueError,KeyError,TypeError) as error:
                    value={'error':str(error)[:500]}
                    bridge.audit(name,arguments,value,True)
                    result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],'isError':True}
            else:
                send({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Method not found'}})
                continue
            send({'jsonrpc':'2.0','id':request['id'],'result':result})
        except (BridgeError,ValueError,KeyError,TypeError):
            send({'jsonrpc':'2.0','id':request.get('id') if isinstance(request,dict) else None,
                  'error':{'code':-32700,'message':'Invalid JSON-RPC message'}})


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--session', help='Existing private session made by the authorized demo launcher')
    parser.add_argument('--profile', help='Personal profile; no access to controller private state required')
    parser.add_argument('--session-dir', help='New or resumable private session directory; use a different directory per conversation')
    parser.add_argument('--environment', default='worker-python')
    parser.add_argument('--seconds', type=int, default=2)
    parser.add_argument('--workflow', help='Bind a prevalidated immutable-input workflow; no model command/resource overrides')
    parser.add_argument('--archive', action='store_true', help='Archive terminal results when get_result is called; fixed local path, not a model argument')
    parser.add_argument('--archive-root');parser.add_argument('--local-source')
    args=parser.parse_args()
    if args.session:
        if args.profile or args.session_dir or args.workflow or args.archive or args.archive_root or args.local_source:parser.error('Choose --session or --profile/--session-dir')
        serve(Bridge(args.session)); return
    if not args.profile or not args.session_dir:parser.error('--profile requires --session-dir')
    if not 1 <= args.seconds <= 10:parser.error('seconds must be 1..10')
    client=Client.from_profile(args.profile)
    if (args.archive_root or args.local_source) and not args.archive:parser.error('Archive paths require --archive')
    identity=client.request('/api/me')
    if not {'jobs:read','jobs:submit'} <= set(identity.get('scopes',[])):
        raise ClientError('Personal credential lacks queue scopes')
    available=client.request('/api/jobs')['environments']
    if args.environment not in {env['id'] for env in available}:
        raise ClientError('Execution environment is not authorized')
    directory=Path(args.session_dir).expanduser().absolute()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info=directory.lstat()
    if directory.is_symlink() or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ClientError('MCP session directory must be private to its owner (chmod 700)')
    # One bridge process per conversation; prevents concurrent state/one-job bypass.
    fd=os.open(directory/'bridge.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ClientError('MCP session is already in use') from None
        path=directory/'session.json'
        bound={'client_profile':str(Path(args.profile).expanduser().absolute()),'identity':identity['id'],
               'environment':args.environment,'seconds':args.seconds,'execution_mode':'templates','client':'user-harness-mcp'}
        if args.workflow:bound['workflow_request']=bound_workflow(client,args.workflow,args.environment)
        if args.archive:
            from client_archive import archive_root
            bound['personal_archive']=str(archive_root(client,'job-000000000000',root=args.archive_root,source=args.local_source))
        old=read_json(path)
        if old:
            if old.get('personal_archive')!=bound.get('personal_archive') or old.get('workflow_request')!=bound.get('workflow_request') or any(old.get(k)!=v for k,v in bound.items() if k!='client' or k in old):
                raise ClientError('Existing MCP session has a different identity or policy; choose a new session directory')
        else:
            atomic_json(path,{**bound,'run_id':'agent-'+uuid.uuid4().hex[:12],'created_at':now(),
                              'model':'user-harness','scope':'one bounded CPU template job; no local execution'})
        serve(Bridge(path))


if __name__=='__main__':
    try:main()
    except (ClientError,BridgeError) as error:
        print(str(error),file=sys.stderr);raise SystemExit(1)

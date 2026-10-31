"""Actual installed CLI, scoped tokens and separate stdio MCP conversations."""
import io
import json
from pathlib import Path
import subprocess
import tarfile

from lab_client import Client,ClientHTTPError,atomic_json


def run(ctx):
    base=ctx['base'];root=ctx['ROOT'];docker=ctx['docker'];state=ctx['controller_state'];controller=ctx['controller'];check=ctx['check']
    who=str(ctx['installed']['uid'])+':'+str(ctx['installed']['gid'])
    personal=state/'personas/proof_user/client.json';cli=['exec','--user',who,controller,'/usr/bin/python3','-B',ctx['client_target']+'/labctl','--profile',str(personal),'experiment-budget']
    spec={'name':'Installed finite IR investigation','idempotency_key':'installed-budget-original',
          'limits':{'jobs':2,'cpu_seconds':120,'npu_seconds':0},'templates':['ir_v1'],'lifetime_seconds':900}
    target=state/'experiment-budget-spec.json'
    writer='import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)'
    docker(['exec','-i','--user',who,controller,'/usr/bin/python3','-I','-c',writer,str(target)],input=json.dumps(spec))
    created=json.loads(docker(cli+['create','--spec',str(target)]).stdout);budget_id=created['budget']['id'];assert not created['reused']
    assert json.loads(docker(cli+['create','--spec',str(target)]).stdout)['reused']
    agent_path=state/'experiment-agent.json';issued=docker(cli+['token',budget_id,'--output',str(agent_path)])
    private=ctx['secret_json'](controller,agent_path);assert set(private)=={'controller','token'} and private['token'] not in issued.stdout
    assert docker(cli+['token',budget_id,'--output',str(agent_path)],check=False).returncode!=0
    profile=base/'experiment-client.json';atomic_json(profile,private);client=Client.from_profile(profile)
    assert client.request('/api/me')['experiment_id']==budget_id
    installed=base/'experiment-personal-client'
    subprocess.run([str(root/'install-client'),'--prefix',str(installed)],check=True,capture_output=True,text=True,timeout=30)
    fixed={**ctx['request'],'name':'Budget-scoped actual IR'};fixed.pop('idempotency_key',None)
    source=base/'experiment-workflow.json';atomic_json(source,fixed)
    sessions=[];tasks=[]
    def conversation(number):
        session=base/('experiment-session-'+str(number))
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',str(profile),'--session-dir',str(session),'--environment','worker-python','--workflow',str(source)]
        calls=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
               {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'queue_status','arguments':{}}},
               {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':'ir_v1','request_key':'stable'}}}]
        reply=subprocess.run(command,input=''.join(json.dumps(c)+'\n' for c in calls),capture_output=True,text=True,timeout=45)
        (base/('experiment-mcp-'+str(number)+'.log')).write_text(reply.stderr)
        assert reply.returncode==0,'Inspect original private MCP stderr; do not replace an uncertain task'
        values={x['id']:x['result'] for x in map(json.loads,reply.stdout.splitlines())}
        status=json.loads(values[2]['content'][0]['text']);assert status['experiment_budget']['bound_id']==budget_id
        result=values[3]
        if result.get('isError'):return None,result
        return json.loads(result['content'][0]['text']),result
    for number in [1,2]:
        result,_=conversation(number);assert result is not None
        job=result['job'];tasks.append({'job_id':job['id']});sessions.append(number)
        done=ctx['wait'](job['id']);assert done['state']=='succeeded'
        detail=client.request('/api/jobs/'+job['id']);assert len(detail['attempts'])==1
        assert detail['job']['experiment_id']==budget_id and detail['job']['experiment_charge']['cpu_seconds']==60
        tasks[-1].update(attempt_id=done['attempt_id'],attempts=1)
        replay,_=conversation(number);assert replay['reused'] and replay['job']['id']==job['id']
    assert client.request('/api/experiment-budgets/'+budget_id)['budget']['charged']=={'jobs':2,'cpu_seconds':120,'npu_seconds':0}
    refused,error=conversation(3);assert refused is None and error['isError']
    assert {j['id'] for j in client.request('/api/jobs')['jobs']}=={j['job_id'] for j in tasks}
    for path,data in [('/api/experiment-budgets',spec),('/api/experiment-budgets/'+budget_id+'/token',{}),('/api/experiment-budgets/'+budget_id+'/close',{})]:
        try:client.request(path,data)
        except ClientHTTPError as e:assert e.status==403
        else:raise AssertionError('Scoped Agent credential received approval authority')
    try:client.request('/api/jobs/'+ctx['first']['id'])
    except ClientHTTPError as e:assert e.status in {403,404}
    else:raise AssertionError('Scoped Agent credential read an unrelated personal task')
    closed=json.loads(docker(cli+['close',budget_id]).stdout)['budget'];assert closed['state']=='closed'
    replay,_=conversation(1);assert replay['reused'] and replay['job']['id']==tasks[0]['job_id']
    check('Installed personal CLI approves one finite budget; two stdio MCP sessions execute real single-attempt IR, reconnect free, and cannot overspend or broaden authority')
    # Preserve a genuine historical release for the offline downgrade gate.
    old=base/'experiment-old-source';old.mkdir(mode=0o700)
    packed=subprocess.check_output(['git','archive','0c27e7b532a3bfdb284bf848d9398279e65b97cd'],cwd=root)
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:tar.extractall(old,filter='data')
    package=base/'experiment-old-server.tar.gz'
    code='import sys;sys.path.insert(0,sys.argv[1]);from deployment import bundle;bundle(sys.argv[2],sys.argv[1])'
    subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(old),str(package)],check=True,capture_output=True,text=True,timeout=45)
    docker(['cp',str(package),controller+':/provision/experiment-old-server.tar.gz'])
    return {'passed':True,'id':budget_id,'tasks':tasks,'charged':closed['charged'],'original_expiry':created['budget']['expires_at'],
            'private_client':str(profile),'separate_mcp_sessions':2,'third_session_rejected':True,'agent_approval_mutations_denied':True,
            'unrelated_job_hidden':True,'profile_contains_only_scoped_token':True,'npu_kernel_launches':0,'model_invocations':0}


def after_restore(ctx,proof):
    client=Client.from_profile(proof['private_client'])
    try:client.request('/api/me')
    except ClientHTTPError as error:assert error.status==401
    else:raise AssertionError('Restored checkpoint revived the previous spending credential')
    value=ctx['api']('/api/experiment-budgets/'+proof['id'])[1]['budget']
    assert value['state']=='restored_frozen' and value['charged']==proof['charged'] and value['expires_at']==proof['original_expiry']
    for task in proof['tasks']:
        detail=ctx['api']('/api/jobs/'+task['job_id'])[1];assert detail['job']['state']=='succeeded' and detail['job']['attempt_id']==task['attempt_id'] and len(detail['attempts'])==1
    ctx['ctl'](ctx['node'],'node','stop');ctx['ctl'](ctx['controller'],'controller','stop')
    rejected=ctx['ctl'](ctx['controller'],'controller','activate','--archive','/provision/experiment-old-server.tar.gz',check=False)
    assert rejected.returncode!=0 and 'Cumulative experiment' in rejected.stderr
    assert ctx['ctl'](ctx['controller'],'controller','start')['ready'] and ctx['ctl'](ctx['node'],'node','start')['ready']
    assert ctx['api']('/api/experiment-budgets/'+proof['id'])[1]['budget']['charged']==proof['charged']
    proof['restored_history_preserved_and_delegation_fenced']=True;proof['actual_old_code_downgrade_refused']=True
    proof['old_budget_credential_revoked']=True
    state=ctx['controller_state'];who=str(ctx['installed']['uid'])+':'+str(ctx['installed']['gid'])
    path=state/'experiment-readonly.json'
    command=['exec','--user',who,ctx['controller'],'/usr/bin/python3','-B',ctx['client_target']+'/labctl',
             '--profile',str(state/'personas/proof_user/client.json'),'experiment-budget','token',proof['id'],'--read-only','--output',str(path)]
    issued=ctx['docker'](command);credential=ctx['secret_json'](ctx['controller'],path);assert credential['token'] not in issued.stdout
    private=ctx['base']/'experiment-readonly-client.json';atomic_json(private,credential);reader=Client.from_profile(private)
    assert reader.request('/api/me')['scopes']==['jobs:read']
    assert reader.request('/api/jobs/'+proof['tasks'][0]['job_id'])['job']['state']=='succeeded'
    try:reader.request('/api/jobs',{**ctx['request'],'idempotency_key':'readonly-cannot-submit'})
    except ClientHTTPError as error:assert error.status==403
    else:raise AssertionError('Historical budget credential received execution authority')
    proof['read_only_history_credential_verified']=True

    ctx['check']('Controller/Node restore preserves checkpoint history and original IR attempts, freezes budgets and revokes spending credentials; an actual old release cannot discard the boundary')

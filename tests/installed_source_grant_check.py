"""Installed personal approval, revoked partial copy and restored fresh round."""
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

from accuracy_data import DRIVER,INPUT,sha
from accuracy_diagnostic import read_case
from environment_contract import lock_for
from installed_operator_check import upload
from lab_client import Client,ClientHTTPError,atomic_json

HOME='/home/source-grant-client'
PUBLIC_CLIENT='/opt/ascend-lab-source-grant-client'


def run(ctx):
    base=ctx['base'];root=ctx['ROOT'];reference=json.loads(Path(ctx['options'].source_grants).read_text())
    source=Path(next(x['path'] for x in reference['datasets'] if x['id']=='legacy-pre'));assert source.is_relative_to(root/'runtime')
    setup='import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642);f=p/"owner.json";f.write_text(sys.stdin.read());f.chmod(0o600);os.chown(f,23642,23642)'
    ctx['docker'](['exec','-i',ctx['controller'],'/usr/bin/python3','-I','-c',setup,HOME],
        input=json.dumps({'controller':ctx['controller_url'],'token':ctx['token']}))
    installed=base/'source-grant-personal-client';subprocess.run([str(root/'install-client'),'--prefix',str(installed)],capture_output=True,text=True,check=True,timeout=30)
    ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-B','/opt/ascend-lab-controller/current/install-client','--prefix',PUBLIC_CLIENT])
    tasks=[];grants=[];budget_ids=[];profiles=[];lock=lock_for(ctx['configuration']['environment_manifest'])
    def cli(*args):
        result=ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',
            PUBLIC_CLIENT+'/labctl','--profile',HOME+'/owner.json',*args])
        return json.loads(result.stdout)
    def write(name,value):
        path=HOME+'/'+name
        code='import sys;from pathlib import Path;p=Path(sys.argv[1]);assert not p.exists();p.write_text(sys.stdin.read());p.chmod(0o600)'
        ctx['docker'](['exec','-i','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-I','-c',code,path],input=json.dumps(value))
        return path
    def budget(label,count):
        spec={'name':'Approved source '+label,'idempotency_key':'source-grant-'+label,
            'limits':{'jobs':count,'cpu_seconds':60*count,'npu_seconds':0},'templates':['build_v1'],'lifetime_seconds':1800}
        key=cli('experiment-budget','create','--spec',write(label+'-budget.json',spec))['budget']['id'];budget_ids.append(key)
        output=HOME+'/'+label+'-budget-client.json';cli('experiment-budget','token',key,'--output',output)
        config=ctx['secret_json'](ctx['controller'],output);assert set(config)=={'controller','token'}
        profile=base/(label+'-source-grant-client.json');atomic_json(profile,config);profiles.append(str(profile))
        return key,Client(config),profile
    def approval(label,parent,destination,files):
        spec={'source_job_id':parent,'destination_experiment_id':destination,
            'files':[{'name':name} for name in files],'lifetime_seconds':1200,'idempotency_key':'source-'+label}
        plan_path=HOME+'/'+label+'-plan.json'
        preview=cli('source-grant','plan','--spec',write(label+'-source.json',spec),'--output',plan_path);assert not preview['approved']
        approved=cli('source-grant','approve','--plan',plan_path);assert not approved['reused']
        again=cli('source-grant','approve','--plan',plan_path);assert again['reused'] and again['grant']['expires_at']==approved['grant']['expires_at']
        grant=approved['grant'];grants.append({'id':grant['id'],'plan':plan_path,'source':grant['source'],'files':grant['files']})
        return grant
    def submit(label,client,profile,input_id,imports=None,parent=None):
        request={'name':'Source grant '+label,'template':'build_v1','environment':'worker-python','parameters':{
            'input_id':input_id,'argv':['python','-I','-B',DRIVER],'lock':copy.deepcopy(lock),
            'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60,
            'input_mode':'readonly','workspace':{'min_mib':8,'min_inodes':128,'durable':True},'artifacts':{'file_mib':8,'total_mib':32}}}
        if parent:request.update(depends_on=[parent],artifact_inputs=imports)
        path=base/(label+'-source-grant-request.json');atomic_json(path,request)
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',str(profile),
            '--session-dir',str(base/('source-grant-session-'+label)),'--environment','worker-python','--workflow',str(path)]
        calls=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'queue_status','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':'build_v1','request_key':'original'}}}]
        def conversation():
            reply=subprocess.run(command,input=''.join(json.dumps(x)+'\n' for x in calls),capture_output=True,text=True,timeout=60)
            assert reply.returncode==0,'Inspect the same private MCP session; never replace an uncertain submission'
            values={x['id']:x['result'] for x in map(json.loads,reply.stdout.splitlines())}
            assert not values[3].get('isError'),values[3]
            policy=json.loads(values[2]['content'][0]['text'])['session_policy'];assert policy['artifact_inputs']==request.get('artifact_inputs',[])
            return json.loads(values[3]['content'][0]['text'])
        value=conversation();identifier=value['job']['id'];task={'job_id':identifier,'label':label,'input_id':input_id};tasks.append(task)
        atomic_json(base/'source-grant-in-progress.json',{'tasks':tasks,'grants':grants,'budget_ids':budget_ids})
        task['_conversation']=conversation
        return identifier
    def finished(identifier,client,expected):
        job=ctx['wait'](identifier,seconds=100);assert job['state']==expected,(identifier,job['state'],job.get('reason'),job.get('result'))
        detail=client.request('/api/jobs/'+identifier);task=next(t for t in tasks if t['job_id']==identifier)
        task.update(state=expected,attempt_id=job.get('attempt_id'),attempts=len(detail['attempts']))
        replay=task.pop('_conversation')();assert replay['reused'] and replay['job']['id']==identifier
        if expected=='failed':
            assert len(detail['attempts'])==1 and read_case(client,identifier)['comparison']['mismatches']==11
            assert all(job['result'][k] for k in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed'])
            env=ctx['api']('/api/jobs/'+identifier+'/artifact?name=environment.json')[1]
            assert env['task_uid']==23003 and env['device_nodes']==[] and env['network_mode']=='none' and env['workspace']['user_quota']
        else:assert not detail['attempts']
        return job
    producer=base/'source-grant-producer';shutil.copytree(source,producer);(producer/DRIVER).write_bytes((root/DRIVER).read_bytes())
    producer_input=upload(ctx,producer)
    consumer=base/'source-grant-consumer';consumer.mkdir(mode=0o700);(consumer/DRIVER).write_bytes((root/DRIVER).read_bytes())
    transport=b'g'*(6*1024**2);(consumer/'zz-transfer-check.bin').write_bytes(transport);consumer_input=upload(ctx,consumer)
    old_id,old_client,old_profile=budget('previous',1)
    parent=submit('original-failure',old_client,old_profile,producer_input);original=finished(parent,old_client,'failed')
    old_case=read_case(old_client,parent);names=[INPUT,'actual.bin','reference.bin','source_context.bin']
    files=['results/accuracy-input/'+name for name in names]
    destination,agent,profile=budget('next',2);grant=approval('revoked-copy',parent,destination,files)
    for client,path in [(agent,'/api/jobs/'+parent),(agent,'/api/jobs/'+parent+'/artifact?name=results%2Faccuracy-input%2Factual.bin'),
        (old_client,'/api/source-grants/'+grant['id'])]:
        try:client.request(path)
        except ClientHTTPError as error:assert error.status==404
        else:raise AssertionError('Source grant expanded unrelated job or budget visibility')
    assert ctx['api']('/api/source-grants/'+grant['id'],extra_headers=ctx['peer_headers'])[0]==404
    reader='import sys;open(sys.argv[1],"rb").read(1)'
    for path in [HOME+'/owner.json',HOME+'/revoked-copy-plan.json']:
        denied=ctx['docker'](['exec','--user','23643:23643',ctx['controller'],'/usr/bin/python3','-I','-c',reader,path],check=False)
        assert denied.returncode!=0
    imports=[{'job_id':parent,'name':file,'target':name,'when':'terminal','source_grant':grant['id']} for file,name in zip(files,names)]
    ctx['ctl'](ctx['node'],'node','stop')
    blocked=submit('revoked-partial',agent,profile,consumer_input,imports,parent)
    deadline=time.monotonic()+90;partial=None
    while time.monotonic()<deadline:
        detail=agent.request('/api/jobs/'+blocked);stage=detail['job'].get('dependency_state',{})
        if stage.get('status')=='copying' and 0<stage.get('copied_bytes',0)<stage['total_bytes']:
            partial=stage;break
        assert detail['job']['state']=='queued';time.sleep(.05)
    assert partial,'Inspect the original partial transfer; do not replace it'
    assert cli('source-grant','revoke',grant['id'])['grant']['state']=='revoked'
    ctx['ctl'](ctx['controller'],'controller','stop')
    inspect='import json,sqlite3,sys;db=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True);j=json.loads(db.execute("SELECT data FROM jobs WHERE id=?",(sys.argv[2],)).fetchone()[0]);key=j["dependency_state"]["input_id"];uploaded=dict(db.execute("SELECT name,size FROM input_uploads WHERE bundle=?",(key,)));print(json.dumps({"state":j["state"],"attempt_id":j.get("attempt_id"),"input_id":key,"uploaded":uploaded,"dependency_state":j["dependency_state"]}))'
    stopped=json.loads(ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-I','-c',inspect,str(ctx['controller_state']/'jobs.sqlite3'),blocked]).stdout)
    assert stopped['state']=='queued' and stopped['attempt_id'] is None
    assert ctx['ctl'](ctx['controller'],'controller','start')['ready']
    time.sleep(2)
    detail=agent.request('/api/jobs/'+blocked);assert detail['job']['state']=='queued' and not detail['attempts']
    assert detail['job']['dependency_state']['input_id']==stopped['input_id']
    status,retained=ctx['api']('/api/inputs/'+stopped['input_id']);assert status==200 and retained['uploaded']==stopped['uploaded']
    agent.request('/api/jobs/'+blocked+'/cancel',{});finished(blocked,agent,'cancelled')
    replay=cli('source-grant','approve','--plan',HOME+'/revoked-copy-plan.json');assert replay['reused'] and replay['grant']['state']=='revoked'
    second=approval('fresh-copy',parent,destination,files)
    imports=[{**item,'source_grant':second['id']} for item in imports]
    ctx['ctl'](ctx['node'],'node','start')
    child=submit('approved-diagnostic',agent,profile,consumer_input,imports,parent);diagnostic=finished(child,agent,'failed')
    case=read_case(agent,child);assert case['tensor_sources']['actual']['sha256']==old_case['tensor_sources']['actual']['sha256']
    proof=ctx['api']('/api/jobs/'+child+'/artifact?name=dependency-inputs.json')[1]
    assert {v['source_grant'] for v in proof['artifacts']}=={second['id']}
    assert proof['terminal_sources'][parent]['attempt_id']==original['attempt_id'] and proof['terminal_sources'][parent]['state']=='failed'
    assert ctx['api']('/api/storage/plan',{'kind':'archive','id':parent,'action':'trash'},extra_headers=ctx['owner_headers'])[0]==409
    assert agent.request('/api/experiment-budgets/'+destination)['budget']['charged']=={'jobs':2,'cpu_seconds':120,'npu_seconds':0}
    old=base/'source-grant-old-source';old.mkdir(mode=0o700)
    packed=subprocess.check_output(['git','archive','4e2d4df6f5995ba88155d7fb0209552853aff7df'],cwd=root)
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:archive.extractall(old,filter='data')
    package=base/'source-grant-old-server.tar.gz';code='import sys;sys.path.insert(0,sys.argv[1]);from deployment import bundle;bundle(sys.argv[2],sys.argv[1])'
    subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(old),str(package)],capture_output=True,text=True,check=True,timeout=45)
    ctx['docker'](['cp',str(package),ctx['controller']+':/provision/source-grant-old-server.tar.gz'])
    atomic_json(base/'source-grant-partial.json',stopped)
    # Functions are deliberately retained only in this process, never JSON evidence.
    ctx['_source_grant_helpers']={'budget':budget,'approval':approval,'submit':submit,'finished':finished,
        'consumer_input':consumer_input,'names':names,'files':files,'tasks':tasks,'grants':grants}
    return {'passed':True,'tasks':tasks,'grants':grants,'old_budget_id':old_id,'destination_budget_id':destination,
        'parent':parent,'diagnostic':child,'cancelled_partial':blocked,'original_attempt_id':original['attempt_id'],
        'actual_sha256':case['tensor_sources']['actual']['sha256'],'original_mismatches':11,'elements':5376,
        'old_budget_charged':{'jobs':1,'cpu_seconds':60,'npu_seconds':0},'destination_charged':{'jobs':2,'cpu_seconds':120,'npu_seconds':0},
        'private_budget_profiles':profiles,'partial_input_id':stopped['input_id'],'uploaded_offsets_retained':stopped['uploaded'],
        'revoked_copy_cannot_resume_after_restart':True,'source_job_and_archive_hidden':True,'peer_linux_uid_cannot_read_personal_files':True,
        'synthetic_transfer_bytes':len(transport),'synthetic_transfer_sha256':sha(transport),'task_uid':23003,'personal_uid':23642,
        'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Actual installed UID-separated CPU handoff of historical 5376-element precision evidence. New budget cannot read old jobs; explicit personal file grant permits central import. Not new NPU/device/PID1/physical multi-host acceptance.'}


def after_restore(ctx,proof):
    owner=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    for profile in proof['private_budget_profiles']:
        try:Client.from_profile(profile).request('/api/me')
        except ClientHTTPError as error:assert error.status==401
        else:raise AssertionError('Restore revived an old spending credential')
    for grant in proof['grants']:
        value=owner.request('/api/source-grants/'+grant['id'])['grant'];assert value['state']=='restored_frozen' and not value['usable']
    for task in proof['tasks']:
        detail=owner.request('/api/jobs/'+task['job_id'])
        assert detail['job']['state']==task['state'] and detail['job'].get('attempt_id')==task['attempt_id'] and len(detail['attempts'])==task['attempts']
    for key,charged in [(proof['old_budget_id'],proof['old_budget_charged']),(proof['destination_budget_id'],proof['destination_charged'])]:
        value=owner.request('/api/experiment-budgets/'+key)['budget'];assert value['state']=='restored_frozen' and value['charged']==charged
    ctx['ctl'](ctx['node'],'node','stop');ctx['ctl'](ctx['controller'],'controller','stop')
    rejected=ctx['ctl'](ctx['controller'],'controller','activate','--archive','/provision/source-grant-old-server.tar.gz',check=False)
    assert rejected.returncode!=0 and 'source grants' in rejected.stderr,rejected.stderr
    assert ctx['ctl'](ctx['controller'],'controller','start')['ready'] and ctx['ctl'](ctx['node'],'node','start')['ready']
    helpers=ctx['_source_grant_helpers'];key,client,profile=helpers['budget']('restored',1)
    grant=helpers['approval']('restored',proof['parent'],key,helpers['files'])
    imports=[{'job_id':proof['parent'],'name':file,'target':name,'when':'terminal','source_grant':grant['id']}
        for file,name in zip(helpers['files'],helpers['names'])]
    identifier=helpers['submit']('restored-diagnostic',client,profile,helpers['consumer_input'],imports,proof['parent'])
    job=helpers['finished'](identifier,client,'failed');case=read_case(client,identifier)
    assert case['comparison']['mismatches']==11 and case['tensor_sources']['actual']['sha256']==proof['actual_sha256']
    parent=owner.request('/api/jobs/'+proof['parent']);assert parent['job']['attempt_id']==proof['original_attempt_id'] and len(parent['attempts'])==1
    assert client.request('/api/experiment-budgets/'+key)['budget']['charged']=={'jobs':1,'cpu_seconds':60,'npu_seconds':0}
    return {'passed':True,'old_grants_frozen':True,'old_tokens_revoked':True,'old_charges_and_attempts_preserved':True,
        'actual_previous_release_downgrade_denied':True,'fresh_budget_id':key,'fresh_grant_id':grant['id'],
        'fresh_diagnostic_job':identifier,'fresh_attempt_id':job['attempt_id'],'fresh_mismatches':11,
        'same_original_actual_bytes':True,'original_producer_not_reexecuted':True}

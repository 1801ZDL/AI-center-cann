"""Owned installed process drain proof. Real CPU IR, no hardware/model probe."""
import copy
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def prepare(session,node_prefix,config):
    import deployment as deploy
    from worker import Worker
    deploy.start(session.prefix)
    # A real heartbeat allows submissions while the installed Node process is
    # still stopped, so the first old-owner task stays queued deterministically.
    worker=Worker(config)
    try:worker.heartbeat()
    finally:worker.lock.close();worker.state_lock.close()
    value=session.service_code('''import json,sys
from pathlib import Path
from lab_client import Client,read_json,atomic_json
s=Path(sys.argv[1]);source=s/'old-owner-drain-input';source.mkdir(mode=0o700)
(source/'input.mlir').write_text('module { func.func @add() -> i32 { %a = arith.constant 19 : i32\\n %b = arith.constant 23 : i32\\n %r = arith.addi %a, %b : i32\\n return %r : i32 } }\\n')
(source/'run.py').write_text("import os,json,subprocess,shutil,time,sys\\nfrom pathlib import Path\\nassert os.getuid()==23801\\ntime.sleep(int(sys.argv[1]) if len(sys.argv)>1 else 5)\\nr=Path(os.environ['LAB_JOB_DIR']);out=r/'ir/canonical.mlir'\\nsubprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)\\nassert 'arith.constant 42' in out.read_text()\\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],'uid':os.getuid()}))\\n")
c=Client.from_profile(s/'personas/old_owner/client.json');bundle=c.upload_input(source)
atomic_json(s/'old-owner-drain-bundle.json',bundle)
print(json.dumps({'input_id':bundle['id']}))
''')
    return value


def submit(session,key,delay=None):
    argv=['python','run.py']+([str(delay)] if delay is not None else [])
    timeout=max(30,delay+15) if delay is not None else 30
    return session.service_code('''import json,sys
from pathlib import Path
from lab_client import Client,read_json
from environment_contract import lock_for
s=Path(sys.argv[1]);c=Client.from_profile(s/'personas/old_owner/client.json');config=read_json(s/'node-profile.json');bundle=read_json(s/'old-owner-drain-bundle.json')
r={'environment':'worker-python','template':'ir_v1','idempotency_key':''' +repr(key)+''',
'parameters':{'input_id':bundle['id'],'argv':''' +repr(argv)+''','resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},
'lock':lock_for(config['environment_manifest']),'timeout_seconds':''' +repr(timeout)+''','artifacts':{'file_mib':1,'total_mib':2},'workspace':{'min_mib':1,'min_inodes':16,'durable':True}}}
v=c.request('/api/jobs',r);print(json.dumps({'job_id':v['job']['id'],'state':v['job']['state']}))
''')


def no_new_submission(session):
    return session.service_code('''import json,sys
from pathlib import Path
from lab_client import Client,ClientError
c=Client.from_profile(Path(sys.argv[1])/'personas/old_owner/client.json')
try:c.request('/api/jobs',{'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}})
except ClientError as error:assert '503' in str(error)
else:raise AssertionError('Draining allowed a new task')
print(json.dumps({'draining_rejects_new_submission':True}))
''')


def wait_job(session,identifier,expect_active=False):
    code='''import json,sys,time
from pathlib import Path
from lab_client import Client
c=Client.from_profile(Path(sys.argv[1])/'personas/old_owner/client.json')
j=c.request('/api/jobs/'''+identifier+'''')
print(json.dumps({'state':j['job']['state'],'attempts':len(j['attempts'])}))
'''
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        value=session.service_code(code)
        if expect_active and value['state'] in {'reserved','running'}:return value
        if value['state']=='succeeded':
            assert value['attempts']==1
            return value
        assert value['state'] not in {'failed','cancelled','timed_out','needs_attention'},value
        time.sleep(.15)
    raise AssertionError('Original old-owner drain job did not reach its expected state')


def idle_node(config):
    from maintenance import assert_quiescent
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        try:assert_quiescent(Path(config['state_dir']),'node');return
        except (ValueError,OSError):time.sleep(.15)
    raise AssertionError('Owned Node did not finish cleanup/acknowledgement')


def busy_abort(session,node_prefix,config,spec,source,cli,local,kill):
    import deployment as deploy
    from lab_client import atomic_json,read_json
    import owner_provision as master
    task=submit(session,'old-owner-aborted-drain');assert task['state']=='queued'
    abandoned=copy.deepcopy(spec);abandoned['operation_id']=uuid.uuid4().hex;abandoned['owner']='drain_abort_owner'
    abandoned['delivery'].update(owner='drain_abort_owner',name='unused-drain')
    atomic_json(source,abandoned);kill('held',local_nodes=True)
    assert read_json(Path(session.prefix)/master.GATE)['phase']=='draining';no_new_submission(session)
    deploy.start(node_prefix);wait_job(session,task['job_id'],expect_active=True)
    before=deploy.process_status(node_prefix)['pid']
    result=json.loads(subprocess.run([*cli,'--abort'],capture_output=True,text=True,check=True,timeout=30).stdout)
    assert result['state']=='aborted' and deploy.process_status(node_prefix)['pid']==before
    wait_job(session,task['job_id']);idle_node(config)
    assert not (Path(session.prefix)/master.JOURNAL).exists()
    # Stop-side interruption is still reversible: both old processes return,
    # with no new identity or resource allocation.
    stopped=copy.deepcopy(abandoned);stopped['operation_id']=uuid.uuid4().hex;stopped['owner']='drained_abort_owner'
    stopped['delivery'].update(owner='drained_abort_owner',name='unused-stopped')
    atomic_json(source,stopped);kill('stopped',local_nodes=True)
    record=read_json(Path(session.prefix)/master.JOURNAL);assert record['phase']=='drained' and record['stage_id'] is None
    assert not deploy.process_status(session.prefix)['running'] and not deploy.process_status(node_prefix)['running']
    restored=json.loads(subprocess.run([*cli,'--abort'],capture_output=True,text=True,check=True,timeout=40).stdout)
    assert restored['state']=='aborted' and deploy.process_status(session.prefix)['ready'] and deploy.process_status(node_prefix)['ready']
    for owner in ['drain_abort_owner','drained_abort_owner']:
        try:session.command('accessctl','user-status',owner)
        except RuntimeError:pass
        else:raise AssertionError('A drain-only abort created an owner')
    return {'busy_abort_job':task['job_id'],'busy_abort_preserved_running_job_single_attempt':True,'abort_after_local_and_controller_stop_restored_processes':True}

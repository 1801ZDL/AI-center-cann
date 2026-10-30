"""Actual installed CPU/IR admission while another UID holds the Node slot."""
import json
import os
from pathlib import Path
import time


def snapshot(session,job,owner='old_owner'):
    return session.service_code('''import json,sys
from pathlib import Path
from lab_client import Client
c=Client.from_profile(Path(sys.argv[1])/'personas/'''+owner+'''/client.json')
v=c.request('/api/jobs/'''+job+'''');j=v['job']
print(json.dumps({'job_id':j['id'],'state':j['state'],'attempt_id':j.get('attempt_id'),'attempts':len(v['attempts']),
 'allocation':j['allocation'],'started_at':j['started_at'],'finished_at':j['finished_at']}))
''')


def start(session):
    from owner_drain_fixture import submit
    task=submit(session,'old-owner-during-final-activation',delay=30)
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        value=snapshot(session,task['job_id'])
        if value['state']=='running':
            assert value['attempts']==1
            return value
        assert value['state'] in {'queued','reserved'},value
        time.sleep(.15)
    raise AssertionError('Original old-owner CPU IR did not start')


def still_running(session,before):
    value=snapshot(session,before['job_id'])
    assert value['state']=='running' and value['attempts']==1,value
    assert value['attempt_id']==before['attempt_id'] and value['allocation']==before['allocation']
    return value


def complete(session,before,config):
    from lab_client import atomic_json
    from owner_drain_fixture import wait_job,idle_node
    still_running(session,before)
    personal=Path(session.personal_path)
    atomic_json(personal/'environment.json',config['environment_manifest']);os.chown(personal/'environment.json',23802,23802)
    task=session.personal_code('''import json,sys,os
from pathlib import Path
from lab_client import Client,read_json
from environment_contract import lock_for
assert os.getuid()==23802
p=Path(sys.argv[1]);c=Client.from_profile(p/'client.json');assert c.request('/api/me')['id']=='new_owner'
source=p/'busy-activation-input';source.mkdir(mode=0o700)
(source/'input.mlir').write_text('module { func.func @add() -> i32 { %a = arith.constant 19 : i32\\n %b = arith.constant 23 : i32\\n %r = arith.addi %a, %b : i32\\n return %r : i32 } }\\n')
(source/'run.py').write_text("import os,json,subprocess,shutil\\nfrom pathlib import Path\\nassert os.getuid()==23802\\nr=Path(os.environ['LAB_JOB_DIR']);out=r/'ir/canonical.mlir'\\nsubprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)\\nassert 'arith.constant 42' in out.read_text()\\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],'uid':os.getuid()}))\\n")
bundle=c.upload_input(source)
request={'environment':'worker-python','template':'ir_v1','idempotency_key':'new-owner-while-node-full',
 'parameters':{'input_id':bundle['id'],'argv':['python','run.py'],'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},
 'lock':lock_for(read_json(p/'environment.json')),'timeout_seconds':30,'artifacts':{'file_mib':1,'total_mib':2},
 'workspace':{'min_mib':1,'min_inodes':16,'durable':True}}}
v=c.request('/api/jobs',request);j=c.request('/api/jobs/'+v['job']['id'])
assert j['job']['state']=='queued' and len(j['attempts'])==0,j['job']['state']
print(json.dumps({'job_id':j['job']['id'],'state':'queued','attempts':0,'personal_uid':os.getuid()}))
''')
    still_running(session,before)
    wait_job(session,before['job_id']);old=snapshot(session,before['job_id'])
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        new=snapshot(session,task['job_id'],'new_owner')
        if new['state']=='succeeded':break
        assert new['state'] in {'queued','reserved','running'},new
        time.sleep(.15)
    else:raise AssertionError('The original queued personal IR did not complete')
    assert old['attempts']==new['attempts']==1
    assert old['allocation']['cpu_ids']==new['allocation']['cpu_ids']
    from datetime import datetime
    assert datetime.fromisoformat(old['finished_at'])<=datetime.fromisoformat(new['started_at'])
    idle_node(config)
    return {'old_owner_job':old,'new_owner_job':new,'personal_uid':23802,'task_uids':[23801,23802],
        'activated_and_delivered_while_old_owner_running':True,'new_personal_ir_queued_while_node_full':True,
        'old_allocation_and_attempt_preserved':True,'single_slot_reused_after_terminal_cleanup':True,
        'both_actual_ir_tasks_succeeded_once':True,'npu_kernel_launches':0}

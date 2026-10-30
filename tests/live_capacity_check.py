"""Twenty logical users submit concurrently; two bounded CPU slots drain the queue."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import ACTIVE,TERMINAL,JobStore,atomic_json,now,read_json
from lab_client import Client

config=read_json(ROOT/'runtime/config.json');admin=Client.from_profile(ROOT/'runtime/config.json')
assert not [job for job in admin.request('/api/jobs')['jobs'] if job['state'] in ACTIVE|{'queued'}]
jobs=JobStore(ROOT/'runtime',config);physical=jobs.backend.physical_id;before=jobs.resources.get(physical)
cpus=before['policy']['cpu_ids'];assert len(cpus)==2
prefix='capacity_'+uuid.uuid4().hex[:8];users=[];created=[];node=None;path=None;samples=[]

def ctl(action):
    subprocess.run([str(ROOT/'workerctl'),action,'--config',str(path)],check=True,capture_output=True,timeout=20)

def submit(index):
    started=time.monotonic();user=users[index]
    client=Client.from_profile(user['profile'])
    request={'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},
             'name':'20用户排队验收 '+str(index+1),'idempotency_key':prefix+'-'+str(index)}
    first=client.request('/api/jobs',request);again=client.request('/api/jobs',request)
    assert first['job']['id']==again['job']['id']
    return {'id':first['job']['id'],'owner':user['id'],'submission_seconds':time.monotonic()-started}

try:
    jobs.resources.configure(physical,{'cpu_ids':cpus,'memory_mib':768,'shm_mib':0,'max_running':2},before['revision'])
    for index in range(20):
        user=prefix+'_'+str(index);directory=ROOT/'runtime/personas'/user;workspace=directory/'workspace';workspace.mkdir(mode=0o700,parents=True)
        password=jobs.access.create_user(user,workspace,environments=['worker-python']);token=jobs.access.issue_token(user,{'jobs:read','jobs:submit','jobs:cancel'})
        profile=directory/'client.json';atomic_json(profile,{'username':user,'password':password,'token':token,'host':'127.0.0.1','port':config['port']})
        users.append({'id':user,'profile':profile,'uid':23201+index})
    profile={'name':prefix+'-node','physical_id':physical,'templates':['cpu_demo'],
      'users':{user['id']:{'uid':user['uid'],'gid':user['uid']} for user in users},
      'policy':{'cpu_ids':cpus,'memory_mib':768,'shm_mib':0,'max_running':2}}
    node,key=jobs.nodes.enroll(profile);state=ROOT/'runtime/workers'/node;state.mkdir(mode=0o700)
    path=state/'config.json';atomic_json(path,{**profile,'node_id':node,'node_key':key,'state_dir':str(state),
       'controller':'http://127.0.0.1:'+str(config['port']),'python':'/usr/bin/python3',
       'image':'sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'})
    # Submit the whole burst before starting this node, making the queue peak measurable.
    with ThreadPoolExecutor(max_workers=20) as pool:created=list(pool.map(submit,range(20)))
    queued=admin.request('/api/jobs');assert sum(row['owner'].startswith(prefix) and row['state']=='queued' for row in queued['jobs'])==20
    ctl('start');deadline=time.monotonic()+180;started_at=now()
    while time.monotonic()<deadline:
        summary=admin.request('/api/jobs');domain=next(item for item in summary['resource_domains'] if item['physical_id']==physical)
        own=[row for row in summary['jobs'] if row['owner'].startswith(prefix)]
        assert len(own)==20
        assert not [row for row in own if row['state'] in {'failed','timed_out','needs_attention'}],[(r['id'],r['state'],r['reason']) for r in own]
        reserved=domain['reserved'];assert reserved['slots']<=2 and reserved['cpu']<=2 and reserved['memory_mib']<=768
        assert len(reserved['cpu_ids'])==reserved['cpu']
        samples.append({'at':now(),**reserved,'queued':sum(row['state']=='queued' for row in own),'completed':sum(row['state']=='succeeded' for row in own)})
        if all(row['state']=='succeeded' for row in own):break
        time.sleep(.2)
    else:raise AssertionError('Twenty-user queue did not drain before bounded deadline')
    checked=[]
    for item in created:
        detail=admin.request('/api/jobs/'+item['id']);assert len(detail['attempts'])==1
        environment=admin.request('/api/jobs/'+item['id']+'/artifact?name=environment.json')
        result=admin.request('/api/jobs/'+item['id']+'/artifact?name=results%2Fdemo-result.json')
        user=next(user for user in users if user['id']==item['owner'])
        assert environment['task_uid']==user['uid'] and environment['termination']['Pid']==0
        assert result['passed'] and result['seconds']==1
        checked.append({**item,'task_uid':environment['task_uid'],'attempt_id':detail['job']['attempt_id']})
    report={'passed':True,'verified_at':now(),'started_at':started_at,'logical_users':20,'submission_concurrency':20,
      'queue_peak':20,'execution_slots':2,'maximum_reserved_memory_mib':max(item['memory_mib'] for item in samples),
      'maximum_observed_slots':max(item['slots'] for item in samples),'jobs':checked,'samples':samples,
      'idempotent_retries':20,'node_id':node,'physical_id':physical,'actual_servers':1,'host_accounts_created':0,
      'npu_kernel_launches':0,'scope':'Real HTTP burst and bounded containers with 20 task UIDs; logical accounts in one host user, CPU-only, not full NPU/workflow production load'}
    atomic_json(ROOT/'evidence/capacity-live-validation.json',report)
    print(json.dumps({key:report[key] for key in ['passed','logical_users','submission_concurrency','queue_peak','execution_slots','maximum_reserved_memory_mib','maximum_observed_slots','idempotent_retries','actual_servers','npu_kernel_launches']},indent=2))
finally:
    # Search by this test's owner prefix as well, so a lost submission response cannot orphan a task.
    own=[row for row in admin.request('/api/jobs')['jobs'] if row['owner'].startswith(prefix)]
    for row in own:
        if row['state'] in {'queued','reserved','running','cancelling'}:admin.request('/api/jobs/'+row['id']+'/cancel',{})
    deadline=time.monotonic()+70
    while time.monotonic()<deadline:
        own=[row for row in admin.request('/api/jobs')['jobs'] if row['owner'].startswith(prefix)]
        if all(row['state'] in TERMINAL for row in own):break
        time.sleep(.3)
    safe=all(row['state'] in TERMINAL for row in own)
    if safe:
        if path:ctl('stop');jobs.nodes.disable(node)
        for user in users:jobs.access.disable_user(user['id'],True)
        latest=jobs.resources.get(physical);jobs.resources.configure(physical,before['policy'],latest['revision'],before['draining'])
    jobs.access.close()

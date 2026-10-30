"""Bounded CPU proof: concurrent managed containers, shared-domain budgets and recovery."""
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
# Reload only this demo. Existing idle workers acquire the new concurrent runner.
for action in ['stop','start']:subprocess.run([str(ROOT/'labctl'),action],check=True,capture_output=True,timeout=15)
jobs=JobStore(ROOT/'runtime',config);physical=jobs.backend.physical_id;before=jobs.resources.get(physical)
cpus=before['policy']['cpu_ids'];assert len(cpus)==2
tag='budget_'+uuid.uuid4().hex[:8];users=[];nodes=[];created=[];checks=[];samples=[];pairs=[]
image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'

def ctl(action,path):
    return subprocess.run([str(ROOT/'workerctl'),action,'--config',str(path)],check=True,capture_output=True,text=True,timeout=20)

def observe():
    data=admin.request('/api/jobs');domain=next(item for item in data['resource_domains'] if item['physical_id']==physical)
    reserved=domain['reserved'];samples.append({'at':now(),**reserved,'unknown':domain['unknown_attempts']})
    assert reserved['slots']<=2 and reserved['cpu']<=2 and reserved['memory_mib']<=768
    assert len(reserved['cpu_ids'])==reserved['cpu']
    return data

def wait_for(job_ids,states,timeout=50):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        observe();rows=[admin.request('/api/jobs/'+job_id)['job'] for job_id in job_ids]
        if all(row['state'] in states for row in rows):return rows
        assert not any(row['state'] in {'failed','timed_out','needs_attention'}-states for row in rows),[(r['id'],r['state'],r['reason']) for r in rows]
        time.sleep(.15)
    raise AssertionError('Resource workflow did not reach expected states before deadline')

def submit(index):
    user=users[index]
    reply=Client.from_profile(user['profile']).request('/api/jobs',{'template':'cpu_demo','environment':'worker-python',
      'parameters':{'seconds':10},'name':'整机预算并发验收 '+str(index+1),'idempotency_key':tag+'-'+str(index)})
    created.append(reply['job']['id']);return created[-1]

def inspect(job):
    name='ascend-lab-'+job['attempt_id'][8:]
    result=subprocess.run(['docker','inspect',name],check=True,capture_output=True,text=True,timeout=12)
    return json.loads(result.stdout)[0]

try:
    jobs.resources.configure(physical,{'cpu_ids':cpus,'memory_mib':768,'shm_mib':0,'max_running':2},before['revision'])
    for index in range(4):
        user=tag+'_'+str(index);directory=ROOT/'runtime/personas'/user;workspace=directory/'workspace';workspace.mkdir(parents=True,mode=0o700)
        password=jobs.access.create_user(user,workspace,environments=['worker-python'])
        token=jobs.access.issue_token(user,{'jobs:read','jobs:submit','jobs:cancel'})
        profile=directory/'client.json';atomic_json(profile,{'username':user,'password':password,'token':token,'host':'127.0.0.1','port':config['port']})
        users.append({'id':user,'profile':profile})
    for index in range(2):
        selected=users[:3] if index==0 else users[3:]
        profile={'name':tag+'-node'+str(index),'physical_id':physical,'templates':['cpu_demo'],
          'users':{user['id']:{'uid':23101+users.index(user),'gid':23101+users.index(user)} for user in selected},
          'policy':{'cpu_ids':cpus if index==0 else cpus[-1:],'memory_mib':768 if index==0 else 512,'shm_mib':0,'max_running':2 if index==0 else 1}}
        node,key=jobs.nodes.enroll(profile);state=ROOT/'runtime/workers'/node;state.mkdir(mode=0o700)
        path=state/'config.json';atomic_json(path,{**profile,'node_id':node,'node_key':key,'state_dir':str(state),
          'controller':'http://127.0.0.1:'+str(config['port']),'image':image,'python':'/usr/bin/python3'})
        nodes.append({'id':node,'path':path});ctl('start',path)
    first=[submit(0),submit(1)];running=wait_for(first,{'running'})
    assert len({row['node_id'] for row in running})==1 and running[0]['node_id']==nodes[0]['id']
    identities=[inspect(row) for row in running]
    assert all(info['State']['Running'] for info in identities)
    assert len({info['Id'] for info in identities})==2
    assert {info['Config']['User'] for info in identities}=={'0:0'} # trusted bootstrap; task UID checked from protected runtime output below
    assert all(info['HostConfig']['Memory']==256*1024**2 and info['HostConfig']['ReadonlyRootfs'] for info in identities)
    checks.append('One node runs two distinct bounded containers simultaneously on separate approved CPUs')
    third=submit(2);fourth=submit(3);time.sleep(.8);observe()
    assert all(admin.request('/api/jobs/'+job_id)['job']['state']=='queued' for job_id in [third,fourth])
    checks.append('Other users and a competing entry remain queued while the shared domain has two 384 MiB reservations')
    ctl('stop',nodes[0]['path'])
    for row,old in zip(running,identities):
        current=inspect(row);assert current['Id']==old['Id'] and current['State']['Running']
    for action in ['stop','start']:subprocess.run([str(ROOT/'labctl'),action],check=True,capture_output=True,timeout=15)
    observe();ctl('start',nodes[0]['path'])
    completed=wait_for(first,{'succeeded'})
    for row,old in zip(completed,identities):
        detail=admin.request('/api/jobs/'+row['id']);assert len(detail['attempts'])==1
        env=admin.request('/api/jobs/'+row['id']+'/artifact?name=environment.json')
        assert env['container_id']==old['Id'] and env['termination']['Pid']==0
        assert env['task_uid'] in {23101,23102} and env['task_gid']==env['task_uid']
        result=admin.request('/api/jobs/'+row['id']+'/artifact?name=results%2Fdemo-result.json')
        assert result['passed'] and result['seconds']==10
        logs=admin.request('/api/jobs/'+row['id']+'/log')['text'];assert logs.count('CPU-only demonstration')==1
        pairs.append({'job_id':row['id'],'attempt_id':row['attempt_id'],'container_id':old['Id'],'cpu_ids':row['allocation']['cpu_ids']})
    checks.append('Controller/node restart recovers both original containers and attempts; each workload runs once')
    wait_for([third],{'running'})
    admin.request('/api/jobs/'+third+'/cancel',{})
    wait_for([third],{'cancelled'});wait_for([fourth],{'succeeded'})
    checks.append('Queued users progress after release; cancelling one job does not cancel the other user')
    final=observe();domain=next(item for item in final['resource_domains'] if item['physical_id']==physical)
    assert domain['reserved']['slots']==0
    report={'passed':True,'verified_at':now(),'checks':checks,'job_ids':created,'same_attempt_recovery':pairs,
      'samples':samples,'maximum':{key:max(sample[key] for sample in samples) for key in ['slots','cpu','memory_mib','execution_overhead_mib']},
      'physical_id':physical,'node_ids':[node['id'] for node in nodes],
      'actual_servers':1,'npu_kernel_launches':0,'host_accounts_created':0,'resources_released':True}
    atomic_json(ROOT/'evidence/resources-live-validation.json',report)
    print(json.dumps({key:report[key] for key in ['passed','checks','job_ids','maximum','actual_servers','npu_kernel_launches']},ensure_ascii=False,indent=2))
finally:
    # Cancel and observe only jobs this test created; unknown state is never force-released.
    for job_id in created:
        row=admin.request('/api/jobs/'+job_id)['job']
        if row['state'] in {'queued','reserved','running','cancelling'}:admin.request('/api/jobs/'+job_id+'/cancel',{})
    deadline=time.monotonic()+70
    while any(admin.request('/api/jobs/'+job_id)['job']['state'] not in TERMINAL for job_id in created) and time.monotonic()<deadline:time.sleep(.3)
    safe=all(admin.request('/api/jobs/'+job_id)['job']['state'] in TERMINAL for job_id in created)
    if safe:
        for node in nodes:ctl('stop',node['path']);jobs.nodes.disable(node['id'])
        for user in users:jobs.access.disable_user(user['id'],True)
        latest=jobs.resources.get(physical);jobs.resources.configure(physical,before['policy'],latest['revision'],before['draining'])
    jobs.access.close()

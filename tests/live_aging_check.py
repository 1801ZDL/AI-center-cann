"""Real two-entry CPU fragmentation, age protection, controller restart and cleanup.

All user commands run through the queue in this demo's labeled containers. Only
the demo's policy is temporarily changed, and restored after confirmed cleanup.
No host account, existing user container, driver or NPU is modified.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import ACTIVE,TERMINAL,JobStore,atomic_json,read_json,now
from lab_client import Client
from queue_policy import WAIT_REASON,age

config=read_json(ROOT/'runtime/config.json');admin=Client.from_profile(ROOT/'runtime/config.json')
assert not any(job['state'] in ACTIVE|{'queued'} for job in admin.request('/api/jobs')['jobs'])
jobs=JobStore(ROOT/'runtime',config);physical=jobs.backend.physical_id;before=jobs.resources.get(physical)
cpus=before['policy']['cpu_ids'];assert len(cpus)==2
tag='aging_'+uuid.uuid4().hex[:8];base=ROOT/'runtime'/tag;base.mkdir(mode=0o700)
atomic_json(base/'before-config.json',config)
image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
users=[];nodes=[];created=[];samples=[];checks=[];policy_changed=False;threshold_changed=False

def lifecycle(action):
    subprocess.run([str(ROOT/'labctl'),action],check=True,capture_output=True,text=True,timeout=20)

def worker(action,node):
    subprocess.run([str(ROOT/'workerctl'),action,'--config',str(node['config'])],check=True,capture_output=True,text=True,timeout=20)

def observe():
    snapshot=admin.request('/api/jobs');domain=next(d for d in snapshot['resource_domains'] if d['physical_id']==physical)
    r=domain['reserved'];samples.append({'at':now(),**r})
    assert r['slots']<=2 and r['cpu']<=2 and r['memory_mib']<=768
    return domain

def wait(job_id,states,timeout=100):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        observe();job=admin.request('/api/jobs/'+job_id)['job']
        if job['state'] in states:return job
        assert job['state'] not in TERMINAL|{'needs_attention'},(job_id,job['state'],job['reason'])
        time.sleep(.2)
    raise AssertionError('Owned fairness job did not reach expected state: '+job_id)

def submit(index,hold=1,cpu=1,memory=256):
    user=users[index]
    response=user['client'].request('/api/jobs',{'name':'等待老化验收 '+str(index)+' / '+str(memory),
      'environment':'worker-python','template':'build_v1','idempotency_key':tag+'-'+str(len(created)),
      'parameters':{'input_id':user['input']['id'],'argv':['python','build.py',str(hold)],
                    'lock':{'python':'3.10.12'},'resources':{'cpu':cpu,'memory_mib':memory},'timeout_seconds':90}})
    created.append(response['job']['id']);return created[-1]

def inspect(job):
    result=subprocess.run(['docker','inspect','ascend-lab-'+job['attempt_id'][8:]],check=True,capture_output=True,text=True,timeout=12)
    return json.loads(result.stdout)[0]

try:
    revised={**config,'scheduler':{**config.get('scheduler',{}),'aging_seconds':3}}
    atomic_json(ROOT/'runtime/config.json',revised);threshold_changed=True
    lifecycle('stop');lifecycle('start')
    assert admin.request('/api/jobs')['policy']['aging_seconds']==3
    jobs.resources.configure(physical,{'cpu_ids':cpus,'memory_mib':768,'shm_mib':0,'max_running':2},before['revision']);policy_changed=True
    source=base/'source';source.mkdir(mode=0o700)
    (source/'kernel.c').write_text('int lab_add(int a,int b){return a+b;}\n')
    (source/'build.py').write_text('''import ctypes,json,os,subprocess,sys,time
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);target=root/'results/libaging.so'
subprocess.run(['cc','-O2','-shared','-fPIC','kernel.c','-o',str(target)],check=True)
library=ctypes.CDLL(str(target));assert library.lab_add(20,22)==42
print('Compiled actual C library; holding only assigned resources.',flush=True)
time.sleep(int(sys.argv[1]))
caps=next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,'artifacts':['results/libaging.so'],'value':42,'uid':os.getuid(),'cpus':sorted(os.sched_getaffinity(0)),'cap_eff':caps,'max_jobs':os.environ['MAX_JOBS']}))
''')
    for index in range(4):
        name=tag+'_'+str(index);home=ROOT/'runtime/personas'/name/'workspace';home.mkdir(parents=True,mode=0o700)
        jobs.access.create_user(name,home,environments=['worker-python'])
        jobs.access.set_quota(name,{'cpu':2,'memory_mib':1024,'max_running':1})
        token=jobs.access.issue_token(name,{'jobs:read','jobs:submit','jobs:cancel'})
        profile=home.parent/'client.json';atomic_json(profile,{'controller':'http://127.0.0.1:'+str(config['port']),'token':token})
        client=Client.from_profile(profile);users.append({'id':name,'client':client,'profile':profile,'input':client.upload_input(source)})
    for index in range(2):
        selected=users[:3] if index==0 else users[3:]
        profile={'name':tag+'-entry'+str(index),'physical_id':physical,'templates':['build_v1'],
          'users':{user['id']:{'uid':23551+users.index(user),'gid':23551+users.index(user)} for user in selected},
          'policy':{'cpu_ids':cpus if index==0 else cpus[-1:],'memory_mib':768 if index==0 else 512,'shm_mib':0,'max_running':2 if index==0 else 1}}
        node,key=jobs.nodes.enroll(profile);state=ROOT/'runtime/workers'/node;state.mkdir(mode=0o700)
        path=state/'config.json';atomic_json(path,{**profile,'node_id':node,'node_key':key,'state_dir':str(state),
          'controller':'http://127.0.0.1:'+str(config['port']),'image':image,'python':'/usr/bin/python3'})
        nodes.append({'id':node,'config':path});worker('start',nodes[-1])
    first=submit(0,hold=35);short=submit(2,hold=8)
    first_job=wait(first,{'running'});wait(short,{'running'})
    original=inspect(first_job);assert original['State']['Running']
    impossible=submit(3,memory=640)
    older=submit(1,hold=5,cpu=2)
    wait(short,{'succeeded'})
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        row=jobs.get(older)
        if row.get('_aging_target') and time.time()-row['_aging_target']['checked_epoch']<5:break
        time.sleep(.2)
    assert row['state']=='queued' and row.get('_aging_target'),row['reason']
    assert row['_aging_target']['physical_id']==physical and age(row)>=3
    assert not jobs.get(impossible).get('_aging_target')
    assert admin.request('/api/jobs/'+impossible)['job']['state']=='queued'
    domain=observe();assert domain['reserved']['cpu']==1 and domain['reserved']['slots']==1
    checks.append('An aged two-CPU build establishes protection while one CPU is free; an oversized older job does not')
    younger=submit(3,hold=1)
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        row=admin.request('/api/jobs/'+younger)['job']
        if row['reason']==WAIT_REASON:break
        time.sleep(.2)
    assert row['state']=='queued' and row['reason']==WAIT_REASON
    checks.append('A second entry with a matching free CPU cannot refill the fragmented domain ahead of the aged build')
    lifecycle('stop');lifecycle('start')
    assert inspect(first_job)['Id']==original['Id'] and inspect(first_job)['State']['Running']
    assert admin.request('/api/jobs/'+older)['job']['state']=='queued'
    assert admin.request('/api/jobs/'+younger)['job']['state']=='queued'
    assert len(admin.request('/api/jobs/'+first)['attempts'])==1
    checks.append('Controller restart retains the original running container, attempt and queued age')
    # End only this test's blocker; no preemption is performed by the policy.
    users[0]['client'].request('/api/jobs/'+first+'/cancel',{});wait(first,{'cancelled'})
    old_running=wait(older,{'running'});current=inspect(old_running)
    assert current['State']['Running'] and current['HostConfig']['CpusetCpus']==','.join(map(str,cpus))
    assert admin.request('/api/jobs/'+younger)['job']['state']=='queued'
    completed=wait(older,{'succeeded'});wait(younger,{'succeeded'})
    result=admin.request('/api/jobs/'+older+'/artifact?name=results%2Fworkload-result.json')
    assert result['uid']==23552 and result['cpus']==cpus and result['cap_eff']=='0000000000000000'
    assert result['max_jobs']=='2' and result['value']==42
    assert len(admin.request('/api/jobs/'+older)['attempts'])==1
    checks.append('After blocker cleanup the aged build gets both CPUs first, compiles real C as UID23552, then the younger user runs')
    users[3]['client'].request('/api/jobs/'+impossible+'/cancel',{});wait(impossible,{'cancelled'})
    assert not any(admin.request('/api/jobs/'+j)['job']['state'] not in TERMINAL for j in created)
    assert observe()['reserved']['slots']==0
    report={'passed':True,'verified_at':now(),'checks':checks,'job_ids':created,'older_job':older,'younger_job':younger,
      'impossible_job':impossible,'blocker_job':first,'completed_short_job':short,'actual_build_result':result,
      'physical_id':physical,'nodes':[node['id'] for node in nodes],
      'maximum':{key:max(s[key] for s in samples) for key in ['cpu','slots','memory_mib']},
      'aging_seconds_used':3,'actual_servers':1,'logical_users':4,'host_accounts_created':0,
      'model_calls':0,'npu_kernel_launches':0,'policy_preempted_tasks':0,
      'scope':'Actual same-host two-entry CPU/memory fragmentation and restart proof; not predictive backfill, DAG, real multi-host or mixed NPU peak acceptance'}
    atomic_json(ROOT/'evidence/aging-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
finally:
    for job_id in created:
        row=admin.request('/api/jobs/'+job_id)['job']
        if row['state'] in {'queued','reserved','running','cancelling'}:admin.request('/api/jobs/'+job_id+'/cancel',{})
    deadline=time.monotonic()+120
    while any(admin.request('/api/jobs/'+j)['job']['state'] not in TERMINAL for j in created) and time.monotonic()<deadline:time.sleep(.3)
    safe=all(admin.request('/api/jobs/'+j)['job']['state'] in TERMINAL for j in created)
    if safe:
        for node in nodes:worker('stop',node);jobs.nodes.disable(node['id'])
        for user in users:jobs.access.disable_user(user['id'],True)
        if policy_changed:
            current=jobs.resources.get(physical);jobs.resources.configure(physical,before['policy'],current['revision'],before['draining'])
    if threshold_changed:
        current=read_json(ROOT/'runtime/config.json');assert current['scheduler']['aging_seconds']==3
        if 'aging_seconds' in config.get('scheduler',{}):current['scheduler']['aging_seconds']=config['scheduler']['aging_seconds']
        else:current['scheduler'].pop('aging_seconds',None)
        atomic_json(ROOT/'runtime/config.json',current);lifecycle('stop');lifecycle('start')
    jobs.access.close()
    assert safe,'Unknown owned task remains; preserve its reservation and reconcile before restoring the domain'

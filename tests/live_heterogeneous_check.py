"""Two real image/compiler environments, targeted admission and controller restart.

Only newly created demo identities/nodes are controlled. Existing nodes, physical
budgets and configurations are unchanged. User code runs only in queued tasks.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import JobStore,read_json,atomic_json,now,ACTIVE,TERMINAL
from lab_client import Client
from environment_contract import fingerprint
p=argparse.ArgumentParser();p.add_argument('--image-from',required=True);args=p.parse_args()
jobs=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'));admin=Client.from_profile(ROOT/'runtime/config.json')
assert not any(j['state'] in ACTIVE|{'queued'} for j in jobs.all())
prepared=jobs.get(args.image_from);assert prepared['state']=='succeeded'
images=read_json(jobs.directory(prepared['id'])/'results/heterogeneous-image.json')
assert images['passed'] and images['base_image']!=images['image']
physical=jobs.backend.physical_id;before=jobs.resources.get(physical)
tag='hetero_'+uuid.uuid4().hex[:8];base=ROOT/'runtime'/tag;base.mkdir(mode=0o700)
node_records=[];created=[];checks=[];executions=[];denials=[];certifications=[];samples=[];cleaned=False
user=tag;home=base/'personal/workspace';home.mkdir(parents=True,mode=0o700)
config=read_json(ROOT/'runtime/config.json')
jobs.access.create_user(user,home,environments=['worker-python'])
profile=base/'personal/client.json';atomic_json(profile,{'controller':'http://127.0.0.1:'+str(config['port']),
 'token':jobs.access.issue_token(user,{'jobs:read','jobs:submit','jobs:cancel'})})
client=Client.from_profile(profile)
source=base/'source';source.mkdir(mode=0o700)
(source/'kernel.c').write_text('int lab_add(int a,int b){return a+b;}\n')
(source/'build.py').write_text('''import ctypes,json,os,subprocess
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);target=root/'results/libheterogeneous.so'
version=subprocess.run(['cc','--version'],capture_output=True,text=True,check=True).stdout
subprocess.run(['cc','-shared','-fPIC','-O2','kernel.c','-o',str(target)],check=True)
library=ctypes.CDLL(str(target));library.lab_add.argtypes=[ctypes.c_int,ctypes.c_int];library.lab_add.restype=ctypes.c_int
assert library.lab_add(20,22)==42
caps=next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
assert os.getuid()==23621 and int(caps,16)==0
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,
 'artifacts':['results/libheterogeneous.so'],'value':42,'uid':os.getuid(),'cap_eff':caps,'compiler_version':version[:1000]}))
print('Actual compiled value 42; compiler: '+version.splitlines()[0],flush=True)
''')


def worker(action,index):
    subprocess.run([str(ROOT/'workerctl'),action,'--config',str(node_records[index]['config'])],
                   check=True,capture_output=True,text=True,timeout=22)


def job_state(job_id):return client.request('/api/jobs/'+job_id)['job']


def observe():
    domain=next(x for x in admin.request('/api/jobs')['resource_domains'] if x['physical_id']==physical)
    reserved=domain['reserved'];samples.append({'at':now(),**reserved})
    assert reserved['slots']<=before['policy']['max_running'] and reserved['cpu']<=len(before['policy']['cpu_ids'])
    assert reserved['memory_mib']<=before['policy']['memory_mib']


def wait(job_id,timeout=150):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        observe();j=job_state(job_id)
        if j['state'] in TERMINAL|{'needs_attention'}:return j
        time.sleep(.25)
    raise AssertionError('Observe the exact existing task; never resubmit on observation timeout: '+job_id)


def heartbeat(index):
    end=time.monotonic()+18
    while time.monotonic()<end:
        n=jobs.nodes.node(node_records[index]['id'])
        if time.time()-n['last_seen']<4 and n['report'].get('environment_sha256')==fingerprint(n['profile']['environment_manifest']):return n
        time.sleep(.2)
    raise AssertionError('No fresh confirmation of certified environment')


def certify(index):
    node=node_records[index];worker('start',index)
    out=subprocess.run([str(ROOT/'workerctl'),'node-probe',node['id'],'--user',user],check=True,capture_output=True,text=True)
    job_id=json.loads(out.stdout)['job_id'];created.append(job_id);j=wait(job_id);assert j['state']=='succeeded',j
    worker('stop',index)
    out=subprocess.run([str(ROOT/'workerctl'),'node-environment',node['id'],'--from-job',job_id,'--expected-revision','1'],
                       check=True,capture_output=True,text=True)
    worker('start',index);n=heartbeat(index);node['manifest']=n['profile']['environment_manifest']
    receipt=client.request('/api/jobs/'+job_id+'/artifact?name=results%2F_lab%2Fenvironment.json')
    tool=receipt['observed']['tools']['cc'];assert tool
    certifications.append({'node':node['id'],'probe_job':job_id,'image':n['profile']['image'],'environment_sha256':fingerprint(node['manifest']),
        'cc_sha256':tool['sha256'],'cc_realpath':tool['realpath'],'cc_version':tool['version_output'][:1000]})
    print('PASS: certified actual image/compiler '+str(index)+' '+tool['version_output'].splitlines()[0],flush=True)


def submit(lock,label):
    reply=client.request('/api/jobs',{'name':'异构环境 · '+label,'template':'build_v1','environment':'worker-python',
      'idempotency_key':tag+'-'+str(len(created)),
      'parameters':{'input_id':bundle['id'],'argv':['python','build.py'],'lock':{'python':'3.10.12',**lock},
       'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},'timeout_seconds':90}})
    created.append(reply['job']['id']);return created[-1]


def unassigned(job_id,wrong_index):
    # At least two ordinary worker polling intervals, with the mismatched entry confirmed live.
    end=time.monotonic()+6
    while time.monotonic()<end:
        heartbeat(wrong_index);observe();j=job_state(job_id)
        assert j['state']=='queued' and not j['allocation'] and not jobs.attempts(job_id),j
        time.sleep(.4)
    denials.append({'job':job_id,'wrong_node':node_records[wrong_index]['id'],'attempts':0,'allocation':{}})


def completed(job_id,index):
    j=wait(job_id);assert j['state']=='succeeded',{'job':job_id,'state':j['state'],'reason':j['reason']}
    assert j['node_id']==node_records[index]['id'] and len(jobs.attempts(job_id))==1
    result=client.request('/api/jobs/'+job_id+'/artifact?name=results%2Fworkload-result.json')
    protected=client.request('/api/jobs/'+job_id+'/artifact?name=results%2F_lab%2Fenvironment.json')
    meta=client.request('/api/jobs/'+job_id+'/artifact?name=environment.json')
    facts=node_records[index]['manifest']['observed']
    assert result['value']==42 and result['uid']==23621 and int(result['cap_eff'],16)==0
    assert protected['locked'] and protected['workload_started'] and protected['observed']['tools']['cc']['sha256']==facts['tools']['cc']['sha256']
    assert meta['image']==facts['image_sha256'] and meta['read_only_root'] and not meta['privileged'] and meta['network_mode']=='none'
    executions.append({'job':job_id,'node':j['node_id'],'attempt':j['attempt_id'],'image':meta['image'],'cc_sha256':facts['tools']['cc']['sha256'],
                       'compiler_version':result['compiler_version'],'value':42,'uid':23621,'cap_eff':result['cap_eff']})
    print('PASS: routed actual compile '+job_id+' to entry '+str(index),flush=True)

try:
    for index,image in enumerate([images['base_image'],images['image']]):
        node_profile={'name':tag+'-compiler'+str(index),'physical_id':physical,'image':image,
          'environment_setup':'/usr/local/Ascend/ascend-toolkit/set_env.sh',
          'users':{user:{'uid':23621,'gid':23621}},'templates':['environment_probe','build_v1'],
          'policy':{'cpu_ids':[before['policy']['cpu_ids'][index]],'memory_mib':512,'shm_mib':0,'max_running':1}}
        node,key=jobs.nodes.enroll(node_profile);state=ROOT/'runtime/workers'/node;state.mkdir(mode=0o700)
        target=state/'config.json';atomic_json(target,{**node_profile,'node_id':node,'node_key':key,'policy_revision':1,
            'controller':'http://127.0.0.1:'+str(config['port']),'state_dir':str(state),'python':'/usr/bin/python3'})
        node_records.append({'id':node,'config':str(target),'image':image})
        atomic_json(base/'fixture-state.json',{'node_ids':[n['id'] for n in node_records],'owner':user,'prepared_by':prepared['id']})
    # Strict inventory startup waits for every enabled entry, including newly enrolled ones.
    for index in range(2):worker('start',index)
    for index in range(2):certify(index)
    a,b=[x['manifest']['observed'] for x in node_records]
    assert a['image_sha256']!=b['image_sha256'] and a['tools']['cc']['sha256']!=b['tools']['cc']['sha256']
    assert 'gcc-11' in a['tools']['cc']['realpath'] and 'bisheng' in b['tools']['cc']['realpath']
    checks.append('Two different immutable image IDs and two different actual compiler binaries certified by queued fixed probes')
    bundle=client.upload_input(source)
    worker('stop',1)
    first=submit({'image_sha256':b['image_sha256'],'tools':{'cc':b['tools']['cc']['sha256']}},'镜像B等待B节点')
    unassigned(first,0);worker('start',1);heartbeat(1);completed(first,1)
    checks.append('Image-B request remains unassigned while only image-A entry polls; starts once B is online')
    worker('stop',0)
    second=submit({'tools':{'cc':a['tools']['cc']['sha256']}},'仅GCC指纹等待A节点')
    unassigned(second,1);worker('start',0);heartbeat(0);completed(second,0)
    checks.append('Compiler-only requirement without image constraint rejects actual Clang image and runs on GCC entry')
    impossible=submit({'image_sha256':a['image_sha256'],'tools':{'cc':b['tools']['cc']['sha256']}},'不存在的镜像与编译器组合')
    unassigned(impossible,0);heartbeat(1)
    subprocess.run([str(ROOT/'labctl'),'stop'],check=True,capture_output=True,timeout=20)
    subprocess.run([str(ROOT/'labctl'),'start'],check=True,capture_output=True,timeout=20)
    assert job_state(impossible)['state']=='queued' and not jobs.attempts(impossible)
    third=submit({'tools':{'cc':b['tools']['cc']['sha256']}},'重启后Clang任务')
    completed(third,1);assert job_state(impossible)['state']=='queued' and not jobs.attempts(impossible)
    client.request('/api/jobs/'+impossible+'/cancel',{});assert wait(impossible)['state']=='cancelled'
    checks.append('Impossible mixed contract survives controller restart without attempt and does not block a later compatible actual build')
    report={'passed':True,'recorded_at':now(),'checks':checks,'prepared_by':prepared['id'],'certifications':certifications,
      'executions':executions,'denials':denials,'job_ids':created,'domain_before':before['policy'],'samples':samples,
      'model_calls':0,'npu_kernel_launches':0,'existing_nodes_reconfigured':False,'host_accounts_modified':False,
      'scope':'Two actual compiler-selection images and independent pull entries on one physical host; not two servers or a rebuilt Triton/NPU-IR toolchain'}
    atomic_json(ROOT/'evidence/heterogeneous-live-validation.json',report)
    print(json.dumps({'passed':True,'checks':checks,'executed_jobs':[x['job'] for x in executions]},ensure_ascii=False,indent=2),flush=True)
finally:
    # Reconcile only this fixture's own submitted work. Unknown resources are never released by cleanup.
    for job_id in created:
        j=jobs.get(job_id)
        if j['state'] in ACTIVE|{'queued'}:
            client.request('/api/jobs/'+job_id+'/cancel',{});j=wait(job_id)
        if j['state'] not in TERMINAL:raise RuntimeError('Retain fixture node/image for exact-attempt reconciliation: '+job_id)
    for index,node in enumerate(node_records):
        worker('stop',index);jobs.nodes.disable(node['id'])
    jobs.access.disable_user(user)
    assert jobs.resources.get(physical)['policy']==before['policy']
    atomic_json(base/'cleanup.json',{'nodes_disabled':[n['id'] for n in node_records],'user_disabled':user,
      'physical_policy_unchanged':True,'image_cleanup_pending':images['image']})
    print('Fixture nodes stopped/revoked and personal credential disabled; physical policy unchanged.',flush=True)

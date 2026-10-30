"""Certify only the two idle resident demo nodes; real CPU build and negative admission."""
import json
import shutil
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import ACTIVE,TERMINAL,JobStore,read_json,atomic_json,now
from lab_client import Client
from environment_contract import fingerprint,lock_for
source=ROOT/'runtime'/('environment-source-'+uuid.uuid4().hex[:8]);source.mkdir(mode=0o700)
for name in ['build.py','kernel.c']:shutil.copyfile(ROOT/'examples/workflows'/name,source/name)

admin=Client.from_profile(ROOT/'runtime/config.json')
assert not any(j['state'] in ACTIVE|{'queued'} for j in admin.request('/api/jobs')['jobs'])
ids=['worker-59f0255aaeda','worker-829166f2505f'];checks=[];created=[];certifications=[]

def ctl(*args):
    done=subprocess.run([str(ROOT/'workerctl'),*args],check=True,capture_output=True,text=True,timeout=22)
    return done.stdout

def wait(client,job_id,timeout=150):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        j=client.request('/api/jobs/'+job_id)['job']
        if j['state'] in TERMINAL|{'needs_attention'}:return j
        time.sleep(.3)
    raise AssertionError('Observe this existing job; do not resubmit: '+job_id)

def beat(node_id,revision):
    end=time.monotonic()+18
    while time.monotonic()<end:
        jobs=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'));n=jobs.nodes.node(node_id)
        if time.time()-n['last_seen']<4 and n['profile']['policy_revision']==revision and n['report'].get('environment_sha256'):return n
        time.sleep(.2)
    raise AssertionError('No acknowledgement for revised certified node')

for node_id in ids:ctl('stop','--config',str(ROOT/'runtime/workers'/node_id/'config.json'))
subprocess.run([str(ROOT/'labctl'),'stop'],check=True,capture_output=True)
jobs=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'))
for node_id in ids:
    n=jobs.nodes.node(node_id);cfg=ROOT/'runtime/workers'/node_id/'config.json'
    templates=sorted(set(n['profile']['templates'])|{'environment_probe','build_v1'})
    jobs.nodes.set_templates(node_id,templates);value=read_json(cfg);value['templates']=templates;atomic_json(cfg,value)
subprocess.run([str(ROOT/'labctl'),'start'],check=True,capture_output=True)
for node_id in ids:ctl('start','--config',str(ROOT/'runtime/workers'/node_id/'config.json'))
for node_id in ids:
    jobs=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'));n=jobs.nodes.node(node_id)
    owner=next(iter(n['profile']['users']));client=Client.from_profile(ROOT/'runtime/personas'/owner/'client.json')
    submitted=json.loads(ctl('node-probe',node_id,'--user',owner));created.append(submitted['job_id'])
    j=wait(client,submitted['job_id']);assert j['state']=='succeeded',{'id':j['id'],'state':j['state'],'reason':j['reason']}
    ctl('stop','--config',str(ROOT/'runtime/workers'/node_id/'config.json'))
    revision=n['profile'].get('policy_revision',1)
    ctl('node-environment',node_id,'--from-job',j['id'],'--expected-revision',str(revision))
    ctl('start','--config',str(ROOT/'runtime/workers'/node_id/'config.json'))
    n=beat(node_id,revision+1);manifest=n['profile']['environment_manifest']
    certifications.append({'node_id':node_id,'probe_job':j['id'],'revision':revision+1,'sha256':fingerprint(manifest),
                           'image':manifest['image_sha256'],'python':manifest['observed']['python'],'machine':manifest['observed']['machine']})
    print('PASS: certified queued non-root CPU probe and revised heartbeat',node_id,flush=True)
checks.append('Both resident nodes certified from protected successful fixed probes; new revisions acknowledged')
# Enable the deployment default only after both resident nodes acknowledge certification.
subprocess.run([str(ROOT/'labctl'),'stop'],check=True,capture_output=True)
config=read_json(ROOT/'runtime/config.json');config.setdefault('scheduler',{})['require_environment_contract']=True
atomic_json(ROOT/'runtime/config.json',config)
subprocess.run([str(ROOT/'labctl'),'start'],check=True,capture_output=True)
for node_id in ids:
    jobs=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'));n=jobs.nodes.node(node_id)
    owner=next(iter(n['profile']['users']));client=Client.from_profile(ROOT/'runtime/personas'/owner/'client.json')
    bundle=client.upload_input(source)
    lock=lock_for(n['profile']['environment_manifest'])
    request={'template':'build_v1','environment':'worker-python','name':'认证环境实际C编译',
      'parameters':{'input_id':bundle['id'],'argv':['python','build.py'],'lock':lock,
        'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},'timeout_seconds':90}}
    good=client.request('/api/jobs',request)['job'];created.append(good['id']);good=wait(client,good['id'])
    assert good['state']=='succeeded',{'id':good['id'],'state':good['state'],'reason':good['reason']}
    assert good['node_id']==node_id
    checks.append('Actual non-root C compile and ctypes result 42 on '+node_id)
    for constraint in [{'python':'0.0.0'},{'machine':'different_arch'},{'image_sha256':'sha256:'+'f'*64},
                       {'tools':{'cc':'f'*64}},{'packages':{'unqueried-package-v017':'__absent__'}}]:
        bad=client.request('/api/jobs',{**request,'parameters':{**request['parameters'],'lock':{**lock,**constraint}}})['job'];created.append(bad['id'])
        time.sleep(2.2);current=client.request('/api/jobs/'+bad['id'])['job']
        assert current['state']=='queued' and not current['allocation']
        assert not jobs.attempts(bad['id'])
        client.request('/api/jobs/'+bad['id']+'/cancel',{})
    print('PASS: actual compiled result and five incompatible/unknown requests have no attempts',node_id,flush=True)
checks.append('Wrong Python, machine, image, compiler SHA and unqueried absent-package constraints never reserve or start')
report={'passed':True,'recorded_at':now(),'checks':checks,'certifications':certifications,'job_ids':created,
        'require_environment_contract':True,'npu_kernel_launches':0,'model_calls':0,
        'scope':'Two independent resident entries on one host, same immutable image; heterogeneous two-image and two-server execution not tested'}
atomic_json(ROOT/'evidence/environment-live-validation.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

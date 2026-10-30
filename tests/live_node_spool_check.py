"""Own idle demo Bob worker: lost ACK, crash after rename, and pressure admission.

Each fault targets a newly created fixture job. Restart reconnects that same
attempt; no existing node artifact or unrelated container is removed.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,read_json,now,private_profile
from node_storage import manifest_sha256

os.umask(0o077)
base=ROOT/'runtime'/('node-spool-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
mode=base/'fault.json';marker=base/'marker.json';wrapper=base/'worker-wrapper.py'
node_config=ROOT/'runtime/workers/worker-829166f2505f/config.json';node=read_json(node_config)
owner=Client.from_profile(ROOT/'runtime/personas/demo_bob/client.json');admin=Client.from_profile(ROOT/'runtime/config.json')
summary=admin.request('/api/jobs');assert not any(summary['counts'][key] for key in ['queued','reserved','running','cancelling','needs_attention'])
assert not any(d['admission']['blocked'] for d in summary['resource_domains'])
wrapper.write_text('''import json,sys
from pathlib import Path
sys.argv=sys.argv[1:];sys.path.insert(0,str(Path(sys.argv[0]).parent))
import worker,node_storage
from jobs import atomic_json,read_json
mode=Path('''+repr(str(mode))+''');marker=Path('''+repr(str(marker))+''')
original=worker.NodeClient.call
def call(self,action,body):
    fault=read_json(mode) or {}
    if action=='heartbeat':
        body=dict(body)
        if fault.get('kind')=='bytes':body['disk_free_bytes']=576*1024**2
        if fault.get('kind')=='inodes':body['disk_free_inodes']=0
    if action=='update' and fault.get('kind')=='lost-ack' and body.get('job_id')==fault.get('job_id') and marker.exists():
        raise RuntimeError('Injected lost completion response; original attempt retained')
    result=original(self,action,body)
    if action=='complete' and body.get('job_id')==fault.get('job_id') and fault.get('kind')=='lost-ack':
        atomic_json(marker,{'fault':'lost-ack','job_id':body['job_id'],'attempt_id':body['attempt_id']})
        raise RuntimeError('Injected lost completion response after actual central commit')
    return result
worker.NodeClient.call=call
original_prune=node_storage.prune
def prune(root,attempt):
    fault=read_json(mode) or {}
    if fault.get('kind')=='after-rename' and marker.exists():raise RuntimeError('Hold only injected node spool retirement')
    return original_prune(root,attempt)
node_storage.prune=prune
original_move=node_storage.fs.move
def move(source,target):
    original_move(source,target)
    fault=read_json(mode) or {}
    if fault.get('kind')=='after-rename' and target.name=='artifacts-retired':
        atomic_json(marker,{'fault':'after-rename','attempt_id':source.parent.parent.name})
        raise RuntimeError('Injected interruption following node artifact rename')
node_storage.fs.move=move
worker.main()
''')
created=[];faults=[]

def worker(action):
    subprocess.run([str(ROOT/'workerctl'),action,'--config',str(node_config)],capture_output=True,check=True,timeout=20)

def start_fault(kind,job_id=None):
    worker('stop');marker.unlink(missing_ok=True);atomic_json(mode,{'kind':kind,**({'job_id':job_id} if job_id else {})})
    with (base/'worker.log').open('ab') as output:
        subprocess.Popen(['/usr/bin/python3',str(wrapper),str(ROOT/'worker.py'),'--config',str(node_config)],cwd=ROOT,
                         stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    wait_for(lambda:read_json(Path(node['state_dir'])/'worker-process.json'),12)

def wait_for(predicate,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.2)
    raise RuntimeError('Observe the existing spool fixture; do not resubmit on timeout')

def submit(label,input_id=None):
    request=({'template':'ir_v1','parameters':{'input_id':input_id,'argv':['python','run.py'],'lock':{'python':'3.10.12'},'timeout_seconds':60}}
             if input_id else {'template':'cpu_demo','parameters':{'seconds':1}})
    request.update(name=base.name+' '+label,environment='worker-python',idempotency_key=base.name+' '+label)
    job=owner.request('/api/jobs',request)['job'];created.append(job['id']);return job['id']

def detail(value):return owner.request('/api/jobs/'+value)
def control(attempt):return Path(node['state_dir'])/'attempts'/attempt/'control'
def collected(value):
    d=detail(value)
    if d['job']['state']!='succeeded':return None
    c=control(d['job']['attempt_id']);s=read_json(c/'state.json') or {}
    return (d,c,s) if s.get('artifact_gc',{}).get('state')=='purged' and not c.joinpath('artifacts').exists() else None

source=base/'source';source.mkdir(mode=0o700)
(source/'run.py').write_text('''import json,os,hashlib
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);out=root/'ir/payload.bin'
payload=b'node-spool-confirmed-copy'+bytes(4096)
with out.open('wb') as f:
 for i in range(2048):f.write(payload)
digest=hashlib.sha256(out.read_bytes()).hexdigest()
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/payload.bin'],'sha256':digest}))
''')
try:
    bundle=owner.upload_input(source)
    # Start with no compute: stop worker first, submit exactly once, then inject its ACK path.
    worker('stop');first=submit('lost-ack',bundle['id']);start_fault('lost-ack',first)
    seen=wait_for(lambda:read_json(marker));d=detail(first);assert d['job']['state']=='succeeded' and len(d['attempts'])==1
    attempt=d['job']['attempt_id'];c=control(attempt);state=read_json(c/'state.json')
    assert state['stage']=='cleaned' and not state.get('controller_acknowledged') and (c/'artifacts/ir/payload.bin').is_file()
    local_sha=hashlib.sha256((c/'artifacts/ir/payload.bin').read_bytes()).hexdigest()
    transfer=read_json(c/'artifact-transfer.json');assert transfer['manifest']
    worker('stop');atomic_json(mode,{});worker('start');d,c,state=wait_for(lambda:collected(first))
    assert len(d['attempts'])==1 and d['job']['attempt_id']==attempt and state['artifact_receipt']['manifest_sha256']==manifest_sha256(transfer['manifest'])
    data=owner.request('/api/jobs/'+first+'/artifact?name=ir%2Fpayload.bin',raw=True,max_bytes=16*1024**2)
    assert hashlib.sha256(data).hexdigest()==local_sha
    faults.append({'kind':'lost-ack','job_id':first,'attempt_id':attempt,'single_attempt':True,'central_sha256':local_sha,'node_payload_removed':True})

    worker('stop');second=submit('after-rename',bundle['id']);start_fault('after-rename',second)
    seen=wait_for(lambda:read_json(marker));d=detail(second);attempt=d['job']['attempt_id'];c=control(attempt);state=read_json(c/'state.json')
    assert d['job']['state']=='succeeded' and state['controller_acknowledged'] and state['artifact_gc']['state']=='pending'
    assert (c/'artifacts-retired/ir/payload.bin').is_file() and not (c/'artifacts').exists()
    wait_for(lambda:any(n['id']==node['node_id'] and n['observed'].get('artifact_gc_ready') is False for n in owner.request('/api/nodes')['nodes']))
    waiting=submit('wait-for-prune');time.sleep(1.2);assert detail(waiting)['job']['state']=='queued' and not detail(waiting)['attempts']
    worker('stop');atomic_json(mode,{});worker('start');d,c,state=wait_for(lambda:collected(second));wait_for(lambda:collected(waiting))
    assert len(d['attempts'])==1 and state['artifact_gc']['state']=='purged' and not (c/'artifacts-retired').exists()
    faults.append({'kind':'after-rename','job_id':second,'attempt_id':attempt,'waiting_job':waiting,'single_attempt':True,'resumed_original_prune':True})

    start_fault('bytes');wait_for(lambda:any(n['id']==node['node_id'] and n['observed'].get('disk_free_bytes')==576*1024**2 for n in owner.request('/api/nodes')['nodes']))
    pending=submit('space-pressure');time.sleep(1.2);d=detail(pending)
    assert d['job']['state']=='queued' and not d['attempts'] and '暂存空间' in d['job']['reason'],d['job']['reason']
    atomic_json(mode,{'kind':'inodes'});wait_for(lambda:any(n['id']==node['node_id'] and n['observed'].get('disk_free_inodes')==0 for n in owner.request('/api/nodes')['nodes']))
    time.sleep(1.2);d=detail(pending);assert d['job']['state']=='queued' and not d['attempts'] and 'inode' in d['job']['reason']
    atomic_json(mode,{});wait_for(lambda:collected(pending));worker('stop');worker('start')
    report={'passed':True,'recorded_at':now(),'node_id':node['node_id'],'faults':faults,'jobs':created,
            'declared_byte_pressure_zero_attempts':True,'inode_pressure_zero_attempts':True,
            'pressure_scope':'Injected observations from own node process; no host disk filled or quota claimed',
            'normal_worker_restored':True,'model_calls':0,'npu_kernel_launches':0,'legacy_node_artifacts_deleted':False}
    atomic_json(ROOT/'evidence/node-spool-live-validation.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    worker('stop');atomic_json(mode,{});worker('start')

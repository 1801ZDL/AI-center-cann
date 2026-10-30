"""Owned-node lease mismatch quarantines worker and legacy admission, across restart.

Only an already-cleaned private demo receipt is fault-injected. No live task,
central job row, Docker daemon, other project or NPU is changed.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import ROOT,JobStore,read_json,atomic_json,ACTIVE,TERMINAL,now
from lab_client import Client
from attempt_inventory import HOLD_REASON

admin=Client.from_profile(ROOT/'runtime/config.json');jobs=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'))
assert not any(j['state'] in ACTIVE|{'queued'} for j in jobs.all())
alice=Client.from_profile(ROOT/'runtime/personas/demo_alice/client.json');bob=Client.from_profile(ROOT/'runtime/personas/demo_bob/client.json')
ids=['worker-59f0255aaeda','worker-829166f2505f'];base=ROOT/'runtime'/('inventory-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
created=[];checks=[];original_grant=None;original_state=None;grant_path=None;state_path=None;poisoned=False

def worker(action,node):
    subprocess.run([str(ROOT/'workerctl'),action,'--config',str(ROOT/'runtime/workers'/node/'config.json')],check=True,capture_output=True,text=True,timeout=22)

def server(action):subprocess.run([str(ROOT/'labctl'),action],check=True,capture_output=True,text=True,timeout=22)

def fresh(node,reconciled=True):
    end=time.monotonic()+20
    while time.monotonic()<end:
        current=jobs.nodes.node(node);receipt=current['report'].get('attempt_inventory',{});ready=read_json(ROOT/'runtime/server-ready.json')
        if (time.time()-current['last_seen']<3 and receipt.get('reconciled') is reconciled
            and receipt.get('controller_instance')==ready.get('controller_instance')):return receipt
        time.sleep(.2)
    raise AssertionError('Await the exact node receipt; no alternate task was submitted')

def wait(job_id,timeout=90):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        job=admin.request('/api/jobs/'+job_id)['job']
        if job['state'] in TERMINAL|{'needs_attention'}:return job
        time.sleep(.2)
    raise AssertionError('Existing task still requires observation: '+job_id)

def submit(client,name,environment='worker-python'):
    job=client.request('/api/jobs',{'name':name,'environment':environment,'template':'cpu_demo','parameters':{'seconds':1},
                                  'idempotency_key':base.name+'-'+str(len(created))})['job'];created.append(job['id']);return job['id']

def unassigned(job_id,seconds=4):
    end=time.monotonic()+seconds;observed_reason=False
    while time.monotonic()<end:
        job=admin.request('/api/jobs/'+job_id)['job']
        assert job['state']=='queued' and not job['allocation'],job
        observed_reason=observed_reason or job['reason']==HOLD_REASON
        assert not jobs.attempts(job_id);time.sleep(.3)
    assert observed_reason,'The scheduler has not yet published this hold reason'

try:
    for node in ids:worker('stop',node)
    server('stop');config=read_json(ROOT/'runtime/config.json');config.setdefault('scheduler',{})['require_attempt_inventory']=True
    atomic_json(ROOT/'runtime/config.json',config);server('start')
    for node in ids:worker('start',node)
    for node in ids:fresh(node)
    first=submit(bob,'执行清单演练 · 已确认结束的自有任务');job=wait(first);assert job['state']=='succeeded',job
    directory=ROOT/'runtime/workers'/ids[1]/'attempts'/job['attempt_id']/'control'
    grant_path=directory/'grant.json';state_path=directory/'state.json'
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        state=read_json(state_path)
        if state.get('stage')=='cleaned' and state.get('controller_acknowledged'):break
        time.sleep(.1)
    assert state['stage']=='cleaned' and state['controller_acknowledged']
    worker('stop',ids[1]);original_grant=read_json(grant_path);original_state=read_json(state_path)
    atomic_json(base/'original-grant.json',original_grant);atomic_json(base/'original-state.json',original_state)
    atomic_json(grant_path,{**original_grant,'lease_token':'mismatch-'+uuid.uuid4().hex})
    atomic_json(state_path,{**original_state,'controller_acknowledged':False});poisoned=True
    worker('start',ids[1]);fresh(ids[1],False)
    hold=next(h for h in jobs.inventory.holds() if h['node_id']==ids[1])
    release=subprocess.run([str(ROOT/'workerctl'),'inventory-release',ids[1],'--expected-revision',str(hold['revision'])],capture_output=True,text=True)
    assert release.returncode!=0 and jobs.inventory.holds()
    second=submit(alice,'执行清单演练 · 另一个节点等待')
    third=submit(admin,'执行清单演练 · 旧宿主执行入口等待','host-python')
    unassigned(second);unassigned(third)
    summary=admin.request('/api/jobs');domain=next(x for x in summary['resource_domains'] if x['physical_id']==jobs.backend.physical_id)
    assert domain['admission']['blocked'] and domain['admission']['inventory_holds']==1 and domain['reserved']['cpu']==0
    checks.append('A mismatched private local lease quarantines the entire physical domain; both another worker and the host executor stay queued with zero attempts/reservations')
    print('PASS: cross-entry quarantine, no fabricated resource allocation, unresolved release refused',flush=True)
    worker('stop',ids[1]);before=read_json(ROOT/'runtime/server-ready.json')['controller_instance'];server('stop');server('start')
    assert read_json(ROOT/'runtime/server-ready.json')['controller_instance']!=before
    fresh(ids[0]);unassigned(second,2);unassigned(third,2)
    checks.append('Durable quarantine survives a controller restart with a new instance; healthy-node heartbeat does not erase the mismatched node record')
    atomic_json(grant_path,original_grant);atomic_json(state_path,{**original_state,'controller_acknowledged':False});poisoned=False
    worker('start',ids[1]);fresh(ids[1]);unassigned(second,2)
    release=subprocess.run([str(ROOT/'workerctl'),'inventory-release',ids[1],'--expected-revision',str(hold['revision'])],check=True,capture_output=True,text=True)
    result=json.loads(release.stdout);assert result['released'] and not result['execution_leases_released']
    assert wait(second)['state']=='succeeded' and wait(third)['state']=='succeeded'
    assert len(jobs.attempts(second))==1 and len(jobs.attempts(third))==1
    assert not jobs.inventory.holds()
    checks.append('Restoring exact original local evidence reconciles the node but requires explicit revision-checked administrator release; queued tasks then execute once')
    print('PASS: exact evidence restored, explicit release, same queued tasks complete once',flush=True)
    report={'passed':True,'recorded_at':now(),'checks':checks,'job_ids':created,'fault_node':ids[1],
      'fault':'Only a completed private local lease token and acknowledgement marker were changed; no active container or central job row changed',
      'hold_revision':hold['revision'],'release':result,'require_attempt_inventory':True,'model_calls':0,'npu_kernel_launches':0,
      'scope':'One physical host; unresolved-inventory quarantine and current-controller heartbeat barrier, not multi-controller consensus or power/daemon fencing'}
    atomic_json(ROOT/'evidence/inventory-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
finally:
    if poisoned:
        worker('stop',ids[1]);atomic_json(grant_path,original_grant);atomic_json(state_path,original_state);worker('start',ids[1]);fresh(ids[1])
    # Never silently release a hold in cleanup. Unfinished tasks and holds require exact-state observation.
    outstanding=[(job_id,jobs.get(job_id)['state']) for job_id in created if jobs.get(job_id)['state'] not in TERMINAL]
    if outstanding:print('Observe original tasks; no cleanup requeue:',json.dumps(outstanding),flush=True)

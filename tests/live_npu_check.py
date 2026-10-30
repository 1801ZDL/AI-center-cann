"""Bounded NPU acceptance: queue first; cancel if busy instead of borrowing a device."""
import argparse
import base64
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jobs import atomic_json,now,read_json

parser=argparse.ArgumentParser()
parser.add_argument('--queue-wait',type=int,default=5)
args=parser.parse_args()
assert 1<=args.queue_wait<=300
config=read_json(ROOT/'runtime/config.json')
auth='Basic '+base64.b64encode((config['username']+':'+config['password']).encode()).decode()


def api(path,body=None):
    request=urllib.request.Request(f'http://127.0.0.1:{config["port"]}'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':auth,'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=8) as response:
        return json.load(response)


summary=api('/api/jobs')
assert not any(summary['counts'][k] for k in ['queued','reserved','running','cancelling','needs_attention']),'Other Demo jobs are active'
snapshot=api('/api/snapshot')['snapshot'] or {}
npu=snapshot.get('npu') or {'status':'pending','devices':[]}
observed={'sampled_at':npu.get('sampled_at'),'status':npu['status'],
          'devices':[{k:d.get(k) for k in ['id','health','utilization_pct','scoped_process_count','external_process_count']} for d in npu['devices']]}
job=api('/api/jobs',{'name':'NPU 精度烟测 · 单次 Triton add','kind':'accuracy','environment':'zdl-mxh-dev',
    'cwd':str(ROOT),'argv':['python','-B','-u','examples/npu_accuracy_smoke.py'],
    'cpu':1,'memory_mib':1024,'shm_mib':0,'npu':1,'timeout_seconds':120,
    'idempotency_key':'npu-smoke-'+uuid.uuid4().hex})['job']
queue_deadline=time.monotonic()+args.queue_wait
deadline=time.monotonic()+args.queue_wait+180
busy_cancelled=False
last_queue_reason=None
try:
    while time.monotonic()<deadline:
        job=api('/api/jobs/'+job['id'])['job']
        if job['state']=='queued':last_queue_reason=job['reason']
        if job['state']=='queued' and time.monotonic()>=queue_deadline:
            busy_cancelled=True
            api('/api/jobs/'+job['id']+'/cancel',{})
            continue
        if job['state'] in {'succeeded','failed','cancelled','timed_out','needs_attention'}:break
        time.sleep(.5)
    else:
        api('/api/jobs/'+job['id']+'/cancel',{})
        raise RuntimeError('Acceptance deadline exceeded; cancellation requested, verify worker state')
    result=api('/api/jobs/'+job['id']+'/artifact?name=results%2Fnpu-accuracy.json') if job['state']=='succeeded' else None
    report={'verified_at':now(),'job_id':job['id'],'state':job['state'],'reason':job['reason'],
            'completed_npu_accuracy_test':bool(result and result['passed']),
            'busy_queue_cancelled':busy_cancelled,'observed_before':observed,
            'admission_wait_reason':last_queue_reason,
            'result':result,'node_id':job.get('node_id'),'attempt_id':job.get('attempt_id'),
            'allocation':job['allocation'],'reserved_after':api('/api/jobs')['reserved']}
    atomic_json(ROOT/'evidence/npu-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    current=api('/api/jobs/'+job['id'])['job']
    if current['state'] in {'queued','reserved','running','cancelling'}:
        api('/api/jobs/'+job['id']+'/cancel',{})

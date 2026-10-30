"""Twenty logical identities reading during the actual long compiler job.

Only bounded HTTP GET traffic plus creation/revocation of this demo's fixtures.
No workload submission, host account, node, device or container changes.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from auth import AccessControl
from lab_client import Client,ClientError,atomic_json,now,read_json

os.umask(0o077)
parser=argparse.ArgumentParser();parser.add_argument('--stage',choices=['baseline','after'],default='baseline');args=parser.parse_args()
suffix='' if args.stage=='baseline' else '-after'
base=ROOT/('runtime/read-load-20261030'+suffix);base.mkdir(mode=0o700)
config=read_json(ROOT/'runtime/config.json');admin=Client(config);access=AccessControl(ROOT/'runtime',config)
state=read_json(ROOT/'runtime/full-build-20261030/control.json');identifier=state['jobs'][-1]['job_id']
before=admin.request('/api/jobs/'+identifier);attempt=before['attempts'][0]['id']
assert before['job']['state']=='running'
name='ascend-lab-'+attempt.removeprefix('attempt-')
def container_state():
    return json.loads(subprocess.run(['docker','inspect','--format','{{json .State}}',name],check=True,capture_output=True,text=True).stdout)
initial=container_state();assert initial['Running']
actors=[];created=[];samples=[];errors=[];barrier=threading.Barrier(20)
try:
    for index in range(20):
        identity='demo_read_20261030'+('' if args.stage=='baseline' else '_after')+'_'+str(index).zfill(2)
        subprocess.run([str(ROOT/'accessctl'),'user-add',identity],check=True,capture_output=True,timeout=15)
        created.append(identity);atomic_json(base/'created.json',{'users':created})
        token=access.issue_token(identity,{'jobs:read'},600)
        profile=base/(identity+'.json');atomic_json(profile,{'controller':admin.url,'token':token})
        actors.append((identity,profile))
    atomic_json(base/'actors.json',{'users':[identity for identity,_ in actors],'job_id':identifier,'attempt_id':attempt,'started_at':now()})
    def read_actor(actor):
        identity,profile=actor;client=Client.from_profile(profile);out=[]
        barrier.wait(timeout=15)
        for path in ['/api/me','/api/jobs','/api/results?limit=20']:
            start=time.monotonic();body=client.request(path);elapsed=time.monotonic()-start
            out.append({'endpoint':path,'seconds':elapsed,'response_bytes':len(json.dumps(body).encode())})
            if path=='/api/me':
                assert body['id']==identity and set(body['scopes'])=={'jobs:read'}
            elif path=='/api/jobs':
                assert identifier not in {job['id'] for job in body['jobs']}
                assert not body['jobs'] and body['reserved']['cpu']==2
            else:
                assert not body['rows']
        try:client.request('/api/jobs/'+identifier)
        except ClientError as error:assert str(error)=='Controller HTTP 404'
        else:raise AssertionError('Foreign private compiler job was visible')
        return out
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures={pool.submit(read_actor,actor):actor[0] for actor in actors}
        for future in as_completed(futures):
            try:samples.extend(future.result())
            except Exception as error:errors.append({'actor':futures[future],'error':type(error).__name__+': '+str(error)[:150]})
    after=admin.request('/api/jobs/'+identifier);current=container_state()
    assert after['attempts'][0]['id']==attempt and len(after['attempts'])==1
    assert current['Running'] and current['StartedAt']==initial['StartedAt']
finally:
    # Only the accounts successfully created by this exact run; retain private evidence.
    for identity,profile in actors:
        access.revoke_token(read_json(profile)['token'])
    for identity in created:access.disable_user(identity)

latencies=sorted(sample['seconds'] for sample in samples)
report={'passed':len(samples)==60 and not errors,'observed_at':now(),'logical_users':len(actors),'concurrency':20,
        'successful_get_requests':len(samples),'foreign_job_404_checks':20 if len(samples)==60 and not errors else None,
        'median_seconds':statistics.median(latencies) if latencies else None,
        'p95_seconds':latencies[math.ceil(.95*len(latencies))-1] if latencies else None,
        'maximum_seconds':max(latencies) if latencies else None,'errors':errors,
        'compiler_job_id':identifier,'attempt_id':attempt,'compiler_container_started_at':initial['StartedAt'],
        'original_compiler_still_running_single_attempt':True,'reader_tokens_read_only_and_revoked':True,
        'temporary_accounts_disabled':True,'host_accounts_modified':False,'workloads_submitted':0,
        'npu_kernel_launches':0,'model_calls':0,
        'scope':'Twenty logical identities, same host UID, bounded loopback HTTP reads during a real compiler task; not twenty OS users, mixed NPU workloads, TLS load or cross-server acceptance'}
atomic_json(base/'samples.json',samples);atomic_json(ROOT/('evidence/read-load'+suffix+'-live-validation.json'),report)
print(json.dumps(report,ensure_ascii=False,indent=2))
if not report['passed']:raise SystemExit(1)

"""Only newly created Bob fixtures: actual queued IR, owner CLI retention, default grace.

No pre-existing archive/input is changed; permanent deletion is not attempted past
the main controller's default grace. The isolated root installation covers purge.
"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,ClientError,atomic_json,now,private_profile

os.umask(0o077)
parser=argparse.ArgumentParser();parser.add_argument('--fixture');args=parser.parse_args()
base=Path(args.fixture).absolute() if args.fixture else ROOT/'runtime'/('retention-'+uuid.uuid4().hex[:8])
assert base.parent==ROOT/'runtime' and base.name.startswith('retention-') and not base.is_symlink()
base.mkdir(mode=0o700,exist_ok=bool(args.fixture))
source=base/'source'
if not args.fixture:
    source.mkdir(mode=0o700)
    for name in ['ir.py','input.mlir']:(source/name).write_bytes((ROOT/'examples/workflows'/name).read_bytes())
    with (source/'input.mlir').open('a') as stream:stream.write('\n// retention fixture '+base.name+'\n')
personal=private_profile(ROOT/'runtime/personas/demo_bob/client.json')
url='http://127.0.0.1:'+str(personal['port'])
profile=base/'owner.json';atomic_json(profile,{'controller':url,'username':personal['username'],'password':personal['password']})
owner=Client.from_profile(profile);ordinary=Client({'controller':url,'token':personal['token']})
client_dir=base/'installed-client'
if client_dir.exists():
    assert args.fixture
    client_dir=base/('installed-client-'+uuid.uuid4().hex[:6])
subprocess.run([str(ROOT/'install-client'),'--prefix',str(client_dir)],check=True,capture_output=True)
cli=[str(client_dir/'labctl'),'--profile',str(profile),'storage']
created=[]

def command(*arguments):
    return json.loads(subprocess.run([*cli,*arguments],capture_output=True,text=True,check=True,timeout=35).stdout)

def operate(kind,value,action):
    preview=command('plan',kind,value,action);plan_file=base/('plan-'+action+'.json');atomic_json(plan_file,preview)
    return command('apply','--plan',str(plan_file))

def denied(client,path,body,code):
    try:client.request(path,body)
    except ClientError as error:assert ('HTTP '+str(code)) in str(error),str(error)
    else:raise AssertionError('Expected access/lifecycle refusal')

bundle=owner.upload_input(source)
request={'name':'中央留存真实 IR · '+base.name,'template':'ir_v1','environment':'worker-python',
         'parameters':{'input_id':bundle['id'],'argv':['python','ir.py'],'lock':{'python':'3.10.12'},'timeout_seconds':60},
         'idempotency_key':base.name}
response=owner.request('/api/jobs',request);job_id=response['job']['id'];created.append(job_id)
deadline=time.monotonic()+120
while time.monotonic()<deadline:
    detail=owner.request('/api/jobs/'+job_id)
    if detail['job']['state'] in {'succeeded','failed','timed_out','cancelled','needs_attention'}:break
    time.sleep(.3)
assert detail['job']['state']=='succeeded',{'job_id':job_id,'state':detail['job']['state']}
artifact='/api/jobs/'+job_id+'/artifact?name='+urllib.parse.quote('ir/canonical.mlir',safe='')
original=owner.request(artifact,raw=True);assert b'arith.constant 42' in original
plan=ordinary.request('/api/storage/plan',{'kind':'archive','id':job_id,'action':'trash'})
assert not plan['can_apply'];denied(ordinary,'/api/storage/apply',plan['plan'],403)
alice=private_profile(ROOT/'runtime/personas/demo_alice/client.json')
denied(Client({'controller':url,'token':alice['token']}),'/api/storage/plan',{'kind':'archive','id':job_id,'action':'trash'},404)
before=owner.request('/api/me')['storage']['personal_bytes']['archive']
result=operate('archive',job_id,'trash');assert result['object']['state']=='trash' and not result['quota_released']
assert owner.request('/api/me')['storage']['personal_bytes']['archive']>=before
try:owner.request(artifact,raw=True)
except ClientError as error:assert 'HTTP 410' in str(error)
else:raise AssertionError('Trashed archive was downloadable')
retired=owner.request('/api/jobs/'+job_id)
assert retired['job']['state']=='succeeded' and retired['retention_metadata']['environment']['locked']
purge=owner.request('/api/storage/plan',{'kind':'archive','id':job_id,'action':'purge'})
assert not purge['eligible'] and purge['purge_after']>time.time()+6*86400
denied(owner,'/api/storage/apply',purge['plan'],409)
result=operate('archive',job_id,'restore');assert result['object']['state']=='available'
assert owner.request(artifact,raw=True)==original
input_result=operate('input',bundle['id'],'trash');assert input_result['object']['state']=='trash'
denied(owner,'/api/jobs',{**request,'idempotency_key':base.name+'-retired'},410)
assert owner.request('/api/jobs',request)['job']['id']==job_id
assert operate('input',bundle['id'],'restore')['object']['state']=='available'
rows=owner.request('/api/results?limit=200')['rows'];row=next(x for x in rows if x['job_id']==job_id)
assert row['archive_state']=='available' and row['passed']
listing=command('list','--state','available');assert listing['automatic_gc'] is False
runtime_environment=owner.request('/api/jobs/'+job_id+'/artifact?name=environment.json')
assert runtime_environment['task_uid']==23002
report={'passed':True,'recorded_at':now(),'job_id':job_id,'input_id':bundle['id'],'actual_ir_sha256':hashlib.sha256(original).hexdigest(),
        'task_uid':runtime_environment['task_uid'],
        'installed_owner_cli':True,'task_token_delete_denied':True,'cross_user_plan_denied':True,
        'default_seven_day_grace_preserved':True,'trashed_bytes_still_charged':True,'archive_hash_preserved':True,
        'input_retirement_blocks_new_job':True,'idempotent_old_job_preserved':True,'fixtures_restored_available':True,
        'model_calls':0,'npu_kernel_launches':0,'preexisting_payloads_modified':False}
atomic_json(ROOT/'evidence/retention-live-validation.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))

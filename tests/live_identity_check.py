"""Real HTTP/worker verification using two logical personas and tiny CPU tasks."""
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
from jobs import atomic_json, now, read_json

config=read_json(ROOT/'runtime/config.json')
clients={name:read_json(ROOT/'runtime/personas'/name/'client.json') for name in ['demo_alice','demo_bob']}
assert all(clients.values()),'Provision demo_alice and demo_bob with accessctl first'
clients['admin']=config
url=f'http://127.0.0.1:{config["port"]}'
prefix='identity-'+uuid.uuid4().hex[:8]
checks=[]


def call(who,path,body=None):
    client=clients[who]
    auth='Bearer '+client['token'] if client.get('token') else 'Basic '+base64.b64encode((client['username']+':'+client['password']).encode()).decode()
    request=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,
                                   headers={'Authorization':auth,'Content-Type':'application/json'})
    try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=8)
    except urllib.error.HTTPError as error:response=error
    with response:return response.status,json.load(response)


def wait(who,job_id,states,timeout=60):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        status,body=call(who,'/api/jobs/'+job_id)
        assert status==200,body
        if body['job']['state'] in states:return body['job']
        assert body['job']['state'] not in {'failed','needs_attention','timed_out'},body['job']
        time.sleep(.2)
    raise AssertionError('Task did not reach expected state within timeout: '+job_id)


status,start=call('admin','/api/jobs')
assert status==200 and all(start['counts'][k]==0 for k in ['queued','reserved','running','cancelling','needs_attention'])
requests={
 'a':{'template':'cpu_demo','parameters':{'seconds':5},'idempotency_key':prefix+'a','name':'PRIVATE_ALICE_'+prefix},
 'a2':{'template':'cpu_demo','parameters':{'seconds':1},'idempotency_key':prefix+'a2','name':'团队共享 CPU 验收','project':'demo_shared'},
 'b':{'template':'cpu_demo','parameters':{'seconds':2},'idempotency_key':prefix+'b','name':'PRIVATE_BOB_'+prefix}}
status,body=call('demo_alice','/api/jobs',requests['a']);assert status==201,body
a=body['job'];wait('demo_alice',a['id'],{'running'})
status,body=call('demo_alice','/api/jobs',requests['a2']);assert status==201,body
a2=body['job']
status,body=call('demo_bob','/api/jobs',requests['b']);assert status==201,body
b=body['job']
assert call('demo_bob','/api/jobs/'+a['id'])[0]==404
assert call('demo_bob','/api/jobs/'+a['id']+'/log')[0]==404
assert call('demo_bob','/api/jobs/'+a['id']+'/cancel',{})[0]==404
checks.append('Real private job/detail/log/cancel requests enforce authenticated ownership')
status,body=call('demo_bob','/api/jobs/'+a2['id']);assert status==200 and not body['permissions']['cancel']
assert call('demo_bob','/api/jobs/'+a2['id']+'/cancel',{})[0]==403
checks.append('Project viewer can read shared task but cannot cancel it')
for path in ['/api/jobs','/api/export','/api/activity']:
    status,body=call('demo_bob',path);assert status==200
    assert 'PRIVATE_ALICE_' not in json.dumps(body)
    if path=='/api/activity':
        assert all('cwd' not in row and 'argv' not in row for row in body['jobs'])
    else:
        visible=body['jobs'] if path=='/api/jobs' else body['scheduler']['jobs']
        assert all(row['id']!=a['id'] for row in visible)
assert call('demo_bob','/api/snapshot')[0]==403
checks.append('Lists/exports/activity do not leak private names, commands or paths; monitor data denied')
assert call('demo_bob','/api/jobs',{**requests['b'],'argv':['sh']})[0]==400
assert call('demo_bob','/api/jobs',{**requests['b'],'owner':'demo_alice'})[0]==400
status,body=call('demo_bob','/api/jobs',requests['b']);assert status==200 and body['reused'] and body['job']['id']==b['id']
checks.append('Template command overrides/identity forgery rejected; retry reuses one task')
finished=[wait(who,job['id'],{'succeeded'}) for who,job in [('demo_alice',a),('demo_bob',b),('demo_alice',a2)]]
assert finished[1]['started_at']<finished[2]['started_at']
assert finished[0]['finished_at']<=finished[1]['started_at']
checks.append('Different owner admitted before earlier queued task from same owner; no concurrent allocation')
for who,job in [('demo_alice',a),('demo_bob',b),('demo_bob',a2)]:
    status,result=call(who,'/api/jobs/'+job['id']+'/artifact?name=results%2Fdemo-result.json')
    assert status==200 and result['passed'] and result['npu_used'] is False
checks.append('All three tasks executed real CPU workers and returned authorized result artifacts')
legacy=read_json(ROOT/'runtime/agents/runs/agent-e0c9406726b4/submitted-request.json')
if legacy:
    status,result=call('admin','/api/jobs',legacy)
    assert status==200 and result['reused'] and result['job']['id']=='job-ecf8e917a1a3'
    checks.append('Existing v0.3 Agent submission remains idempotent after schema changes')
status,final=call('admin','/api/jobs');assert final['reserved']['cpu']==0
report={'passed':True,'verified_at':now(),'checks':checks,'jobs':[{k:j[k] for k in ['id','owner','project','state','started_at','finished_at']} for j in finished],
        'reserved_after':final['reserved'],'npu_kernel_launches':0,'isolation':'Same OS UID, logical identities only'}
atomic_json(ROOT/'evidence/identity-live-validation.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

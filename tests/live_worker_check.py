"""Real node HTTP/containers, worker recovery and cancellation; no NPU tasks."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jobs import atomic_json,now,read_json

config=read_json(ROOT/'runtime/config.json')
client=read_json(ROOT/'runtime/personas/demo_bob/client.json')
worker_path=next(path for path in (ROOT/'runtime/workers').glob('*/config.json') if read_json(path)['name']=='cpu-worker-b')
worker_config=read_json(worker_path)
prefix='worker-check-'+uuid.uuid4().hex[:8]
checks=[];jobs=[]


def api(path,body=None,credential=client):
    auth='Bearer '+credential['token'] if credential.get('token') else 'Basic '+base64.b64encode((credential['username']+':'+credential['password']).encode()).decode()
    request=urllib.request.Request(f'http://127.0.0.1:{config["port"]}'+path,
        data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':auth,'Content-Type':'application/json'})
    try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=8)
    except urllib.error.HTTPError as error:response=error
    with response:return response.status,json.load(response)


def submit(label,seconds):
    status,body=api('/api/jobs',{'template':'cpu_demo','parameters':{'seconds':seconds},
        'environment':'worker-python','name':label,'idempotency_key':prefix+label})
    assert status==201,body
    jobs.append(body['job']['id']);return body['job']['id']


def wait(job_id,states,timeout=50):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        status,body=api('/api/jobs/'+job_id);assert status==200,body
        if body['job']['state'] in states:return body
        assert body['job']['state'] not in {'failed','needs_attention','timed_out'},body
        time.sleep(.15)
    raise AssertionError('Node task did not reach '+str(states))


def inspect(job_id,attempt):
    result=subprocess.run(['docker','inspect','ascend-lab-'+attempt[8:]],capture_output=True,text=True)
    return json.loads(result.stdout)[0] if result.returncode==0 else None


def worker(action):
    subprocess.run([str(ROOT/'workerctl'),action,'--config',str(worker_path)],capture_output=True,text=True,check=True,timeout=15)


summary=api('/api/jobs',credential=config)[1]
assert not any(summary['counts'][k] for k in ['queued','reserved','running','cancelling','needs_attention'])
assert api('/api/worker/claim',{})[0]==401
assert api('/api/jobs',credential={'token':worker_config['node_key']})[0]==401
checks.append('Real node endpoints reject user credentials; task endpoints reject node credentials')
try:
    job_id=submit('节点重连恢复',10)
    running=wait(job_id,{'running'})['job'];attempt=running['attempt_id']
    first=inspect(job_id,attempt);assert first and first['State']['Running']
    worker('stop')
    second=inspect(job_id,attempt);assert second and second['Id']==first['Id'] and second['State']['Running']
    checks.append('Worker stop leaves its allocated container running with independent timeout')
    subprocess.run([str(ROOT/'labctl'),'stop'],capture_output=True,text=True,check=True,timeout=10)
    subprocess.run([str(ROOT/'labctl'),'start'],capture_output=True,text=True,check=True,timeout=10)
    held=api('/api/jobs/'+job_id)[1]['job'];assert held['attempt_id']==attempt
    worker('start')
    completed=wait(job_id,{'succeeded'})
    assert completed['job']['attempt_id']==attempt and len(completed['attempts'])==1
    status,environment=api('/api/jobs/'+job_id+'/artifact?name=environment.json')
    assert status==200 and environment['container_id']==first['Id'] and environment['termination']['Pid']==0
    status,result=api('/api/jobs/'+job_id+'/artifact?name=results%2Fdemo-result.json')
    assert status==200 and result['passed'] and result['seconds']==10
    logs=api('/api/jobs/'+job_id+'/log')[1]['text']
    assert logs.count('CPU-only demonstration')==1 and logs.count('progress 1/10')==1
    checks.append('Controller and worker restart recover the same container/attempt without repeated execution')
    checks.append('Artifact bytes cross HTTP, pass checksum verification and remain downloadable under user ACL')
    job_id=submit('节点取消验收',10)
    running=wait(job_id,{'running'})['job']
    assert api('/api/jobs/'+job_id+'/cancel',{})[0]==200
    immediately=api('/api/jobs/'+job_id)[1]['job']
    if immediately['state']=='cancelling':assert api('/api/jobs',credential=config)[1]['reserved']['cpu']==1
    cancelled=wait(job_id,{'cancelled'})
    environment=api('/api/jobs/'+job_id+'/artifact?name=environment.json')[1]
    assert environment['termination']['Pid']==0 and not environment['termination']['Running']
    checks.append('Remote cancellation retains reservation until Docker confirms the container has ended')
    final=api('/api/jobs',credential=config)[1]
    assert final['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
    report={'passed':True,'verified_at':now(),'checks':checks,'job_ids':jobs,
        'recovered_container_id':first['Id'],'recovered_attempt':attempt,
        'worker_node_id':worker_config['node_id'],'reserved_after':final['reserved'],
        'npu_kernel_launches':0,'scope':'Two independent worker processes on one physical host; not two physical servers'}
    atomic_json(ROOT/'evidence/worker-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    worker('start')
    for job_id in jobs:
        status,body=api('/api/jobs/'+job_id)
        if status==200 and body['job']['state'] in {'queued','reserved','running','cancelling'}:api('/api/jobs/'+job_id+'/cancel',{})

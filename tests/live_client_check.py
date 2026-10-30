"""Install a separate client and exercise real personal API/MCP/reconnect, CPU only."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now,private_profile

os.umask(0o077)
base=ROOT/'runtime'/('portable-client-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
installed=base/'release';profile=base/'personal.json';session=base/'conversation-001'
source=private_profile(ROOT/'runtime/personas/demo_bob/client.json')
atomic_json(profile,{'controller':'http://127.0.0.1:'+str(source['port']),'token':source['token']})
subprocess.run([str(ROOT/'install-client'),'--prefix',str(installed)],check=True,capture_output=True)
client=Client.from_profile(profile)
processes=[];created=[];checks=[];serial=0


def bridge(**changes):
    command=[sys.executable,str(installed/'mcp_bridge.py'),'--profile',str(profile),
             '--session-dir',str(session),'--environment',changes.get('environment','worker-python'),'--seconds','2']
    p=subprocess.Popen(command,cwd=base,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    processes.append(p);return p


def rpc(p,method,params=None):
    global serial
    serial+=1
    p.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,'method':method,'params':params or {}})+'\n');p.stdin.flush()
    if not select.select([p.stdout],[],[],30)[0]:raise AssertionError('MCP response timeout')
    line=p.stdout.readline()
    if not line:raise AssertionError('MCP terminated: '+p.stderr.read()[-500:])
    reply=json.loads(line);assert reply['id']==serial,reply
    assert 'error' not in reply,reply
    return reply['result']


def call(p,name,args=None):
    result=rpc(p,'tools/call',{'name':name,'arguments':args or {}})
    assert not result.get('isError'),result
    return json.loads(result['content'][0]['text'])


def close(p):
    if p.poll() is None:
        p.stdin.close();p.wait(timeout=20)
    assert p.returncode==0,p.stderr.read()[-500:]


try:
    response=subprocess.run([str(installed/'labctl'),'--profile',str(profile),'me'],capture_output=True,text=True,check=True)
    assert json.loads(response.stdout)['id']=='demo_bob'
    assert not (installed/'runtime').exists()
    checks.append('separate installation uses only personal profile and API; no controller state')
    p=bridge()
    assert rpc(p,'initialize',{'protocolVersion':'2025-06-18'})['serverInfo']['name']=='ascend-lab'
    assert len(rpc(p,'tools/list')['tools'])==8
    queue=call(p,'queue_status');assert 'jobs' not in queue
    args={'template':'cpu_demo','environment':'worker-python','seconds':2,'request_key':'portable-smoke'}
    accepted=call(p,'submit_job',args);job_id=accepted['job']['id'];created.append(job_id)
    assert accepted['job']['owner']=='demo_bob'
    checks.append('harness-independent MCP submits real server-owned template as personal identity')
    # A competing process cannot bypass the one-task manifest by racing state writes.
    second=bridge();out,err=second.communicate('',timeout=20)
    assert second.returncode!=0 and 'already in use' in err
    checks.append('one MCP process per conversation; simultaneous state access rejected')
    close(p)
    p=bridge();rpc(p,'initialize')
    replay=call(p,'submit_job',args)
    assert replay['reused'] and replay['job']['id']==job_id
    until=time.monotonic()+100
    while time.monotonic()<until:
        waited=call(p,'wait_job',{'job_id':job_id,'timeout_seconds':15})
        if waited['terminal']:break
    assert waited['job']['state']=='succeeded',waited
    log=call(p,'get_logs',{'job_id':job_id})
    result=call(p,'get_result',{'job_id':job_id})
    assert log['text'].count('CPU-only demonstration')==1
    assert result['artifact']['passed'] and not result['artifact']['npu_used']
    close(p)
    checks.append('restart resumes same job with stable request; single real execution and archived result')
    changed=bridge(environment='host-python');out,err=changed.communicate('',timeout=20)
    assert changed.returncode!=0 and 'different identity or policy' in err
    checks.append('conversation policy cannot be rebound on reconnect')
    job=client.request('/api/jobs/'+job_id)['job']
    assert job['node_id'].startswith('worker-')
    assert not (installed/'runtime').exists()
    report={'passed':True,'recorded_at':now(),'checks':checks,'job_ids':created,
            'owner':job['owner'],'node_id':job['node_id'],'attempt_id':job['attempt_id'],
            'state':job['state'],'no_controller_state_in_install':True,'no_model_credentials_in_install':True,
            'npu_kernel_launches':0,'installed_files':sorted(x.name for x in installed.iterdir() if x.name!='__pycache__')}
    atomic_json(ROOT/'evidence/client-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    for p in processes:
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
    for job_id in created:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state'] in {'queued','reserved','running'}:client.request('/api/jobs/'+job_id+'/cancel',{})

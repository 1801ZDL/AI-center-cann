"""Harness-independent installed stdio MCP executes and resumes one bound real IR task."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now,private_profile

os.umask(0o077)
base=ROOT/'runtime'/('workflow-mcp-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
install=base/'client';subprocess.run([str(ROOT/'install-client'),'--prefix',str(install)],check=True,capture_output=True)
profile=base/'personal.json';p=private_profile(ROOT/'runtime/personas/demo_bob/client.json')
atomic_json(profile,{'controller':'http://127.0.0.1:'+str(p['port']),'token':p['token']})
client=Client.from_profile(profile)
workflow=ROOT/'runtime/workflow-ir-request.json';processes=[];job_id=None;serial=0


def start():
    p=subprocess.Popen([sys.executable,str(install/'mcp_bridge.py'),'--profile',str(profile),'--session-dir',str(base/'conversation'),
                        '--environment','worker-python','--workflow',str(workflow)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=base)
    processes.append(p);return p


def rpc(p,method,params=None):
    global serial
    serial+=1;p.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,'method':method,'params':params or {}})+'\n');p.stdin.flush()
    assert select.select([p.stdout],[],[],30)[0],'MCP response deadline exceeded'
    line=p.stdout.readline();assert line,'MCP ended: '+p.stderr.read()[-500:]
    result=json.loads(line);assert 'error' not in result,result
    return result['result']


def tool(p,name,args=None):
    result=rpc(p,'tools/call',{'name':name,'arguments':args or {}})
    assert not result.get('isError'),result
    return json.loads(result['content'][0]['text'])


def close(p):p.stdin.close();p.wait(timeout=20);assert p.returncode==0,p.stderr.read()[-500:]


try:
    p=start();rpc(p,'initialize')
    schema=next(x for x in rpc(p,'tools/list')['tools'] if x['name']=='submit_job')['inputSchema']
    assert schema['properties']['template']['enum']==['ir_v1'] and 'seconds' not in schema['properties']
    policy=tool(p,'queue_status')['session_policy'];assert policy['resources']['npu']==0 and not policy['command_overrides']
    args={'template':'ir_v1','request_key':'portable-ir'}
    job_id=tool(p,'submit_job',args)['job']['id'];close(p)
    p=start();rpc(p,'initialize')
    replay=tool(p,'submit_job',args);assert replay['reused'] and replay['job']['id']==job_id
    for _ in range(8):
        state=tool(p,'wait_job',{'job_id':job_id,'timeout_seconds':15})
        if state['terminal']:break
    assert state['job']['state']=='succeeded',state
    assert tool(p,'get_logs',{'job_id':job_id})['text'].count('Actual bishengir-opt folded')==1
    result=tool(p,'get_result',{'job_id':job_id})
    assert result['artifact']['passed'] and result['artifact']['environment_locked']
    assert result['workload_result']['constant_folded_value']==42
    denied=rpc(p,'tools/call',{'name':'submit_job','arguments':{**args,'seconds':1}});assert denied['isError']
    close(p)
    job=client.request('/api/jobs/'+job_id)['job'];assert job['origin']['client']=='user-harness-mcp'
    report={'passed':True,'recorded_at':now(),'job_id':job_id,'checks':[
        'Independent client package validates bound workflow through API only',
        'MCP tool schema cannot override input, argv, lock, resources or template',
        'Conversation restart reuses one real job and one BishengIR execution',
        'Actual result and protected environment contract are returned',
        'Origin identifies a user harness without OpenCode or model dependencies'],
        'npu_kernel_launches':0,'model_calls':0}
    atomic_json(ROOT/'evidence/workflow-mcp-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    for p in processes:
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
    if job_id:
        j=client.request('/api/jobs/'+job_id)['job']
        if j['state'] in {'queued','reserved','running'}:client.request('/api/jobs/'+job_id+'/cancel',{})

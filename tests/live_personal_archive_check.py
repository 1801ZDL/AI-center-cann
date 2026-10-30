"""Installed CLI/MCP personal archive and interrupted real 60MiB HTTP download."""
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client, ClientError, atomic_json, now, private_profile
from client_archive import download

os.umask(0o077)
base=ROOT/'runtime'/('personal-archive-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
install=base/'client';subprocess.run([str(ROOT/'install-client'),'--prefix',str(install)],check=True,capture_output=True)
profile=base/'personal.json';personal=private_profile(ROOT/'runtime/personas/demo_bob/client.json')
atomic_json(profile,{'controller':'http://127.0.0.1:'+str(personal['port']),'token':personal['token']})
client=Client.from_profile(profile);command=[str(install/'labctl'),'--profile',str(profile)]
source=base/'local-source';source.mkdir(mode=0o700)
for name in ['ir.py','input.mlir']:shutil.copyfile(ROOT/'examples/workflows'/name,source/name)
bundle=json.loads(subprocess.run([*command,'input-add',str(source)],check=True,capture_output=True,text=True).stdout)['input']
workflow=json.loads((ROOT/'runtime/workflow-ir-request.json').read_text())
workflow['parameters']['input_id']=bundle['id'];workflow['name']='个人同级自动归档 · 实际IR'
workflow.pop('idempotency_key',None);request_file=base/'workflow.json';atomic_json(request_file,workflow)
print('Starting one real CPU IR job through the installed CLI',flush=True)
run=subprocess.run([*command,'submit','--workflow',str(request_file),'--archive','--wait-seconds','180',
                    '--key','personal-archive-'+base.name],capture_output=True,text=True,timeout=210)
(base/'cli.log').write_text(run.stdout+run.stderr)
assert run.returncode==0,'CLI did not complete; inspect the recorded job before retrying: '+str(base/'cli.log')
submitted=json.JSONDecoder().raw_decode(run.stdout)[0]['job'];job_id=submitted['id']
target=source/'.ascend-lab-results'/job_id
assert json.loads((target/'.lab-complete.json').read_text())['job_state']=='succeeded'
assert 'arith.constant 42' in (target/'ir/canonical.mlir').read_text()
assert json.loads((target/'validated-result.json').read_text())['passed']
assert client.request('/api/jobs/'+job_id)['job']['state']=='succeeded'
assert str(source) not in json.dumps(client.request('/api/jobs/'+job_id)['job'])

preferred=base/'preferred'
subprocess.run([*command,'archive-config','--root',str(preferred)],check=True,capture_output=True)
archived=json.loads(subprocess.run([*command,'archive',job_id],check=True,capture_output=True,text=True).stdout)
assert archived['complete'] and archived['directory']==str(preferred/job_id)
repeated=json.loads(subprocess.run([*command,'archive',job_id],check=True,capture_output=True,text=True).stdout)
assert repeated['bytes_downloaded']==0

large=json.loads((ROOT/'evidence/archive-pulse-validation.json').read_text());assert large['passed']
large_id=large['job_id'];original=client.request;chunks=0
def interrupted(path,*args,**kwargs):
    global chunks
    if 'artifact-chunk?' in path and 'payload.bin' in path:
        chunks+=1
        if chunks==8:raise ClientError('Injected personal download disconnect')
    return original(path,*args,**kwargs)
client.request=interrupted
print('Interrupting download of the existing 60MiB archive; no new compute job',flush=True)
try:download(client,large_id,root=base/'large-results')
except ClientError as error:assert str(error)=='Injected personal download disconnect',str(error)
else:raise AssertionError('Disconnect was not injected')
assert not (base/'large-results'/large_id/'.lab-complete.json').exists()
client.request=original
print('Resuming the same archive from retained verified-range bytes',flush=True)
resumed=download(client,large_id,root=base/'large-results')
assert resumed['bytes_resumed']>0 and resumed['complete']
payload=base/'large-results'/large_id/'results/payload.bin'
digest=hashlib.sha256()
with payload.open('rb') as file:
    for block in iter(lambda:file.read(1024*1024),b''):digest.update(block)
assert digest.hexdigest()==large['artifact_sha256'] and payload.stat().st_size==large['artifact_bytes']

processes=[];serial=0;mcp_job=None
def start():
    process=subprocess.Popen([sys.executable,str(install/'mcp_bridge.py'),'--profile',str(profile),
            '--session-dir',str(base/'conversation'),'--environment','worker-python','--workflow',str(request_file),
            '--archive','--archive-root',str(base/'mcp-results')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=base)
    processes.append(process);return process
def rpc(process,method,params=None):
    global serial
    serial+=1;process.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,'method':method,'params':params or {}})+'\n');process.stdin.flush()
    assert select.select([process.stdout],[],[],45)[0],'MCP observer deadline; inspect original conversation/job, do not resubmit'
    line=process.stdout.readline();assert line,'MCP stopped; inspect '+str(base/'conversation')
    response=json.loads(line);assert 'error' not in response,response;return response['result']
def tool(process,name,args=None):
    result=rpc(process,'tools/call',{'name':name,'arguments':args or {}});assert not result.get('isError'),result
    return json.loads(result['content'][0]['text'])
def close(process):process.stdin.close();process.wait(timeout=20);assert process.returncode==0
try:
    print('Checking session-bound automatic archive through installed MCP, without a model call',flush=True)
    process=start();rpc(process,'initialize')
    mcp_job=tool(process,'submit_job',{'template':'ir_v1','request_key':'personal-mcp-ir'})['job']['id'];close(process)
    process=start();rpc(process,'initialize')
    for _ in range(12):
        state=tool(process,'wait_job',{'job_id':mcp_job,'timeout_seconds':15})
        if state['terminal']:break
    assert state['job']['state']=='succeeded',state
    result=tool(process,'get_result',{'job_id':mcp_job})
    assert result['local_archive']['complete'] and result['workload_result']['constant_folded_value']==42
    assert result['local_archive']['directory']==str(base/'mcp-results'/mcp_job)
    rejected=rpc(process,'tools/call',{'name':'get_result','arguments':{'job_id':mcp_job,'archive_root':str(base/'unapproved')}})
    assert rejected['isError'] and not (base/'unapproved').exists()
    assert tool(process,'get_result',{'job_id':mcp_job})['local_archive']['bytes_downloaded']==0
    close(process)
finally:
    for process in processes:
        if process.poll() is None:process.terminate();process.wait(timeout=10)

report={'passed':True,'verified_at':now(),'cli_ir_job':job_id,'mcp_ir_job':mcp_job,
        'large_existing_job':large_id,'large_artifact_bytes':payload.stat().st_size,
        'large_artifact_sha256':digest.hexdigest(),'resumed_bytes':resumed['bytes_resumed'],
        'checks':['Installed CLI uploads source and archives real IR beside the original local input without server home mounts',
                  'Local source path is absent from the controller job record',
                  'Personal preference selects a different root; verified repeat downloads transfer zero bytes',
                  'Interrupted existing 60MiB download resumes the same immutable manifest and matches SHA256',
                  'Installed MCP reconnects one bound IR attempt and archives through get_result without a model',
                  'Model arguments cannot change the fixed personal archive directory'],
        'new_ir_jobs':2,'npu_kernel_launches':0,'model_calls':0,'host_accounts_modified':False,
        'scope':'Actual HTTP and isolated CPU IR tasks on one physical server; not disk hard-quota or NPU acceptance'}
atomic_json(ROOT/'evidence/personal-archive-live-validation.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)

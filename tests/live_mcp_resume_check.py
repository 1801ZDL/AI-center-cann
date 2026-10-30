"""Resume a completed existing conversation using a newly installed portable MCP."""
import argparse
import json
from pathlib import Path
import select
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now

parser=argparse.ArgumentParser();parser.add_argument('--session-dir',required=True);args=parser.parse_args()
directory=Path(args.session_dir).resolve();manifest=json.loads((directory/'session.json').read_text())
state=json.loads((directory/'mcp-state.json').read_text());assert len(state['jobs'])==1
job_id=state['jobs'][0];client=Client.from_profile(manifest['client_profile'])
before=client.request('/api/jobs/'+job_id);assert before['job']['state']=='succeeded'
count=len(before['attempts']);base=ROOT/'runtime'/('mcp-resume-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
installed=base/'client';subprocess.run([str(ROOT/'install-client'),'--prefix',str(installed)],capture_output=True,check=True)
process=subprocess.Popen([sys.executable,str(installed/'mcp_bridge.py'),'--session',str(directory/'session.json')],
 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=base)
serial=0
def rpc(method,params=None):
 global serial
 serial+=1;process.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,'method':method,'params':params or {}})+'\n');process.stdin.flush()
 assert select.select([process.stdout],[],[],30)[0],'Observe this same MCP process; do not submit a replacement'
 response=json.loads(process.stdout.readline());assert 'error' not in response,response
 return response['result']
def tool(name,arguments):
 result=rpc('tools/call',{'name':name,'arguments':arguments});assert not result.get('isError'),result
 return json.loads(result['content'][0]['text'])
try:
 rpc('initialize');job=tool('get_job',{'job_id':job_id});assert job['state']=='succeeded'
 waited=tool('wait_job',{'job_id':job_id,'timeout_seconds':1});assert waited['terminal'] and not waited['needs_attention']
 result=tool('get_result',{'job_id':job_id});assert result['artifact']['passed'] and result['workload_result']['constant_folded_value']==42
 assert tool('get_logs',{'job_id':job_id})['text'].count('Actual bishengir-opt folded')==1
 after=client.request('/api/jobs/'+job_id);assert len(after['attempts'])==count==1
 report={'passed':True,'verified_at':now(),'job_id':job_id,'attempt_id':after['job']['attempt_id'],
    'new_jobs_submitted':0,'model_calls':0,'checks':['New standalone client reconnects original conversation',
    'Original successful attempt and actual result remain readable','No submission or repeated execution'],
    'observation':'Original acceptance observer stopped at temporary needs_attention; original task subsequently completed'}
 atomic_json(ROOT/'evidence/mcp-resume-validation.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
 process.stdin.close();process.wait(timeout=15)
 assert process.returncode==0,process.stderr.read()[-1000:]

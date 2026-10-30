"""Executed as a separate non-root client UID inside the owned HTTPS fixture."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys

client,home,private_state=sys.argv[1:];home=Path(home);processes=[];serial=0
assert os.getuid()==23642
try:Path(private_state,'config.json').read_bytes()
except PermissionError:private_denied=True
else:raise AssertionError('Personal client UID read controller private configuration')
assert not os.access('/var/run/docker.sock',os.R_OK)

def start():
    p=subprocess.Popen(['/usr/bin/python3','-B',client+'/mcp_bridge.py','--profile',str(home/'client.json'),
      '--session-dir',str(home/'conversation'),'--environment','worker-python','--workflow',str(home/'workflow.json')],
      stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=home)
    processes.append(p);return p

def rpc(p,method,params=None):
    global serial
    serial+=1;p.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,'method':method,'params':params or {}})+'\n');p.stdin.flush()
    assert select.select([p.stdout],[],[],35)[0],'Observe the existing MCP process; no re-submission on timeout'
    line=p.stdout.readline();assert line,'MCP process ended without response'
    value=json.loads(line);assert 'error' not in value,value
    return value['result']

def tool(p,name,args=None):
    value=rpc(p,'tools/call',{'name':name,'arguments':args or {}});assert not value.get('isError'),value
    return json.loads(value['content'][0]['text'])

def close(p):p.stdin.close();p.wait(timeout=15);assert p.returncode==0

try:
    p=start();rpc(p,'initialize');schema=next(x for x in rpc(p,'tools/list')['tools'] if x['name']=='submit_job')['inputSchema']
    assert schema['properties']['template']['enum']==['ir_v1']
    arguments={'template':'ir_v1','request_key':'installed-https-mcp-ir'}
    first=tool(p,'submit_job',arguments)['job'];close(p)
    p=start();rpc(p,'initialize');again=tool(p,'submit_job',arguments)
    assert again['reused'] and again['job']['id']==first['id']
    for _ in range(6):
        observed=tool(p,'wait_job',{'job_id':first['id'],'timeout_seconds':15})
        if observed['terminal']:break
    assert observed['job']['state']=='succeeded',observed['job']['state']
    result=tool(p,'get_result',{'job_id':first['id']})
    assert result['artifact']['passed'] and result['artifact']['environment_locked'] and result['workload_result']['constant_folded_value']==42
    close(p)
    p=subprocess.Popen(['/usr/bin/python3','-B',client+'/diagnostic_case.py','--profile',str(home/'client.json'),
      '--job-id',first['id']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=home)
    processes.append(p);rpc(p,'initialize')
    listed=rpc(p,'tools/list')['tools'];assert [item['name'] for item in listed]==['get_diagnostic_case']
    assert listed[0]['annotations']['readOnlyHint']
    case=tool(p,'get_diagnostic_case');assert case['job']['id']==first['id'] and case['terminal']
    assert {'environment_receipt','runtime_environment','validated_result'}<={item['id'] for item in case['evidence']}
    assert rpc(p,'tools/call',{'name':'submit_job','arguments':{}})['isError']
    assert rpc(p,'tools/call',{'name':'get_diagnostic_case','arguments':{'job_id':'job-000000000000'}})['isError']
    close(p)
    print(json.dumps({'passed':True,'job_id':first['id'],'client_uid':os.getuid(),'controller_private_state_unreadable':private_denied,
      'docker_socket_accessible':False,'mcp_reconnect_reused_job':True,'actual_ir_constant':42,
      'diagnostic_case_readonly':True,'diagnostic_case_sha256':case['case_sha256'],'model_calls':0}),flush=True)
finally:
    for p in processes:
        if p.poll() is None:
            p.stdin.close()
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:raise RuntimeError('MCP handle is still live; preserve its bound job and session for observation')

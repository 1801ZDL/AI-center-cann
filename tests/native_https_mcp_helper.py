"""Independent personal UID uses only the published native-workflow MCP client."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys

client,home,private=sys.argv[1:];home=Path(home);processes=[];serial=0
assert os.getuid()==23642 and not os.access('/var/run/docker.sock',os.R_OK)
try:Path(private,'config.json').read_bytes();raise AssertionError('Private controller state was readable')
except PermissionError:pass
def start():
    p=subprocess.Popen(['/usr/bin/python3','-B',client+'/mcp_bridge.py','--profile',str(home/'client.json'),
        '--session-dir',str(home/'conversation'),'--environment','worker-python','--workflow',str(home/'workflow.json')],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=home);processes.append(p);return p
def rpc(p,name,params=None):
    global serial
    serial+=1;p.stdin.write(json.dumps({'jsonrpc':'2.0','id':serial,'method':name,'params':params or {}})+'\n');p.stdin.flush()
    assert select.select([p.stdout],[],[],35)[0],'Observe the same MCP/job after timeout; do not resubmit'
    value=json.loads(p.stdout.readline());assert 'error' not in value,value;return value['result']
def tool(p,name,args=None):
    r=rpc(p,'tools/call',{'name':name,'arguments':args or {}});assert not r.get('isError'),r;return json.loads(r['content'][0]['text'])
def close(p):p.stdin.close();assert p.wait(timeout=15)==0
try:
    p=start();rpc(p,'initialize');listed=rpc(p,'tools/list')['tools']
    assert next(t for t in listed if t['name']=='submit_job')['inputSchema']['properties']['template']['enum']==['build_v1']
    assert rpc(p,'tools/call',{'name':'submit_job','arguments':{'template':'build_v1','request_key':'bad-native-override','argv':['other']}})['isError']
    args={'template':'build_v1','request_key':'installed-native-mcp'};first=tool(p,'submit_job',args)['job'];close(p)
    p=start();rpc(p,'initialize');again=tool(p,'submit_job',args);assert again['reused'] and again['job']['id']==first['id']
    for _ in range(8):
        observed=tool(p,'wait_job',{'job_id':first['id'],'timeout_seconds':15})
        if observed['terminal']:break
    assert observed['job']['state']=='succeeded',observed
    result=tool(p,'get_result',{'job_id':first['id']});r=result['workload_result']
    assert result['artifact']['passed'] and result['artifact']['environment_locked'] and r['triton_cache_hit']
    assert not r['triton_stages_called'] and r['cmake']['cache_hits']==2 and r['cmake']['cache_misses']==0
    assert r['kernel_launches']==0;close(p)
    print(json.dumps({'passed':True,'job_id':first['id'],'client_uid':os.getuid(),'private_state_unreadable':True,
        'reconnect_reused_job':True,'fixed_workflow_override_rejected':True,'warm_triton_stages':[],
        'warm_ccache_hits':2,'warm_ccache_misses':0,'binary_sha256':r['binary_sha256'],'model_calls':0}),flush=True)
finally:
    for p in processes:
        if p.poll() is None:p.stdin.close();p.wait(timeout=15)

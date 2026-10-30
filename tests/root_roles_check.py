"""Actual controller UID / privileged node / task UID split, entirely in owned containers."""
import base64
import hashlib
import json
import http.client
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jobs import JobStore,atomic_json,now,read_json
from backends import physical_identity

assert os.environ.get('LAB_JOB_DIR'),'Run this test as a queued trusted CPU acceptance task'
assert len(os.sched_getaffinity(0))>=2,'Reserve two CPUs for this acceptance'
base=Path(os.environ['LAB_JOB_DIR'])/'tmp/root-roles'
base.mkdir(mode=0o700)
bundle=base/'release';bundle.mkdir(mode=0o755)
for source in ROOT.glob('*.py'):
    shutil.copyfile(source,bundle/source.name);(bundle/source.name).chmod(0o444)
for folder in ['examples','web']:
    shutil.copytree(ROOT/folder,bundle/folder,ignore=shutil.ignore_patterns('__pycache__'))
    for path in (bundle/folder).rglob('*'):path.chmod(0o555 if path.is_dir() else 0o444)
    (bundle/folder).chmod(0o555)
bundle.chmod(0o555)
controller_state=base/'controller-state';controller_state.mkdir(mode=0o700)
users=controller_state/'users';users.mkdir(mode=0o700)
workspace=users/'proof_user';workspace.mkdir(mode=0o700)
node_state=base/'node-state';node_state.mkdir(mode=0o700)
cpus=sorted(os.sched_getaffinity(0));image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
with socket.socket() as listener:
    listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
config={'username':'proof_admin','password':secrets.token_urlsafe(24),'host':'127.0.0.1','port':port,
        'container':'not-used','interval':15,'monitor_local':False,
        'scheduler':{'workspace_root':str(users),'cpu_ids':[cpus[-1]],'memory_mib':512,'shm_mib':16,'max_running':1,'local_enabled':False}}
atomic_json(controller_state/'config.json',config)
store=JobStore(controller_state,config)
store.access.create_user('proof_user',workspace,environments=['worker-python'])
store.access.set_quota('proof_user',{'shm_mib':16})
token=store.access.issue_token('proof_user',{'jobs:read','jobs:submit','jobs:cancel'})
profile={'name':'root-proof-node','physical_id':physical_identity(),
         'policy':{'cpu_ids':[cpus[-1]],'memory_mib':512,'shm_mib':16,'max_running':1},
         'users':{'proof_user':{'uid':23003,'gid':23003}},'templates':['cpu_demo','isolation_probe','ir_v1']}
node_id,node_key=store.nodes.enroll(profile)
node_config={**profile,'node_id':node_id,'node_key':node_key,'state_dir':str(node_state),
             'controller':f'http://127.0.0.1:{port}','image':image,'python':'/usr/bin/python3',
             'environment_setup':'/usr/local/Ascend/ascend-toolkit/set_env.sh',
             'probe_forbidden_paths':[str(controller_state/'config.json'),str(node_state/'config.json')]}
atomic_json(node_state/'config.json',node_config)
prefix='ascend-lab-rootproof-'+uuid.uuid4().hex[:8]
controller=prefix+'-controller';node=prefix+'-node'
containers=[];primary_stopped=False


def docker(args,timeout=30):
    result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Root role container operation failed: '+result.stderr[-300:])
    return result.stdout


def api(path,body=None):
    request=urllib.request.Request(f'http://127.0.0.1:{port}'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5)
    except urllib.error.HTTPError as error:response=error
    with response:return response.status,json.load(response)


def restore_owner(path,uid,gid):
    code='import os\np="/data"\nfor root,dirs,files in os.walk(p,followlinks=False):\n for n in dirs+files:\n  q=os.path.join(root,n);os.lchown(q,'+str(uid)+','+str(gid)+')\n  if n in dirs and not os.path.islink(q):os.chmod(q,0o700)\nos.chown(p,'+str(uid)+','+str(gid)+')\nos.chmod(p,0o700)\n'
    docker(['run','--rm','--label','io.ascend-lab.rootproof='+prefix,'--network','none','--read-only',
            '--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER',
            '--security-opt','no-new-privileges:true','--memory','128m','--memory-swap','128m',
            '--cpuset-cpus',str(cpus[0]),'--pids-limit','32',
            '--mount','type=bind,src='+str(path)+',dst=/data','--entrypoint','/usr/bin/python3',image,'-I','-c',code])


try:
    restore_owner(controller_state,24001,24001)
    common=['--detach','--label','io.ascend-lab.rootproof='+prefix,'--network','host','--read-only',
            '--security-opt','no-new-privileges:true','--memory','256m','--memory-swap','256m',
            '--cpuset-cpus',','.join(map(str,cpus)),'--cpus','0.25','--pids-limit','128',
            '--log-opt','max-size=1m','--log-opt','max-file=1',
            '--mount','type=bind,src='+str(bundle)+',dst=/opt/lab,readonly']
    subprocess.run([str(ROOT/'labctl'),'stop'],capture_output=True,text=True,check=True,timeout=10);primary_stopped=True
    docker(['run','--name',controller,*common,'--user','24001:24001','--cap-drop','ALL',
        '--mount','type=bind,src='+str(controller_state)+',dst='+str(controller_state),
        '--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
        '--entrypoint','/usr/bin/timeout',image,'--kill-after=5s','150s','/usr/bin/python3','-B','/opt/lab/server.py',
        '--state-dir',str(controller_state),'--config',str(controller_state/'config.json')])
    containers.append(controller)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            if api('/api/me')[0]==200:break
        except (OSError,http.client.HTTPException):pass
        time.sleep(.2)
    else:raise RuntimeError('Isolated controller did not become ready')
    docker(['run','--name',node,*common,'--user','0:0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--cap-add','CHOWN','--cap-add','FOWNER',
        '--mount','type=bind,src='+str(node_state)+',dst='+str(node_state),
        '--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
        '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
        '--mount','type=bind,src='+str(Path(shutil.which('docker')))+',dst=/usr/bin/docker,readonly',
        '--entrypoint','/usr/bin/timeout',image,'--kill-after=5s','150s','/usr/bin/python3','-B','/opt/lab/worker.py',
        '--config',str(node_state/'config.json')])
    containers.append(node)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if any(n['id']==node_id and n['status']=='online' for n in api('/api/nodes')[1]['nodes']):break
        time.sleep(.2)
    else:raise RuntimeError('Root node did not register a healthy heartbeat')
    status,body=api('/api/jobs',{'template':'isolation_probe','parameters':{},'environment':'worker-python','idempotency_key':'root-role-probe'})
    assert status==201,body
    job_id=body['job']['id'];deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        status,body=api('/api/jobs/'+job_id)
        if body['job']['state'] in {'succeeded','failed','needs_attention','timed_out'}:break
        time.sleep(.2)
    assert body['job']['state']=='succeeded',body['job']
    environment=api('/api/jobs/'+job_id+'/artifact?name=environment.json')[1]
    result=api('/api/jobs/'+job_id+'/artifact?name=results%2Fisolation.json')[1]
    assert result['passed'] and result['uid']==23003 and not result['protected_paths_readable']
    assert environment['node_daemon_uid']==0 and environment['task_uid']==23003
    assert body['job']['result']['execution_cleanup_confirmed'] is True
    input_files={name:(ROOT/'examples/workflows'/name).read_bytes() for name in ['ir.py','input.mlir']}
    manifest=[{'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in input_files.items()]
    status,input_response=api('/api/inputs',{'files':manifest});assert status in {200,201},input_response
    input_id=input_response['input']['id']
    for name,data in input_files.items():
        status,response=api('/api/inputs/'+input_id+'/blob',{'name':name,'offset':0,'data':base64.b64encode(data).decode()})
        assert status==200,response
    assert api('/api/inputs/'+input_id+'/seal',{})[1]['input']['sealed']
    status,response=api('/api/jobs',{'template':'ir_v1','environment':'worker-python','parameters':{
        'input_id':input_id,'argv':['python','ir.py'],'lock':{'python':'3.10.12','image_sha256':image},'timeout_seconds':60},
        'idempotency_key':'root-real-bishengir-ir'})
    assert status==201,response
    workflow_id=response['job']['id'];deadline=time.monotonic()+70
    while time.monotonic()<deadline:
        _,response=api('/api/jobs/'+workflow_id)
        if response['job']['state'] in {'succeeded','failed','needs_attention','timed_out'}:break
        time.sleep(.2)
    assert response['job']['state']=='succeeded',response['job']
    workflow_environment=api('/api/jobs/'+workflow_id+'/artifact?name=environment.json')[1]
    protected=api('/api/jobs/'+workflow_id+'/artifact?name=results%2F_lab%2Fenvironment.json')[1]
    assertion=api('/api/jobs/'+workflow_id+'/artifact?name=results%2Fworkload-result.json')[1]
    assert workflow_environment['node_daemon_uid']==0 and workflow_environment['task_uid']==23003
    assert protected['locked'] and protected['workload_started'] and protected['workload_exit_code']==0
    assert assertion['constant_folded_value']==42 and response['job']['workflow_result']['passed']
    assert response['job']['result']['execution_cleanup_confirmed'] is True
    proof_code='import os,json;from pathlib import Path;print(json.dumps({"uid":os.getuid(),"docker_socket":Path("/var/run/docker.sock").exists()}))'
    controller_role=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',proof_code]))
    node_role=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',proof_code]))
    assert controller_role=={'uid':24001,'docker_socket':False}
    assert node_role=={'uid':0,'docker_socket':True}
    report={'passed':True,'verified_at':now(),'controller_role':controller_role,'node_role':node_role,
            'task_probe':result,'environment':environment,'job_id':job_id,'node_id':node_id,
            'root_workflow':{'job_id':workflow_id,'input_id':input_id,'environment':workflow_environment,
                             'protected_environment':protected,'result':assertion},
            'checks':['Controller runs as UID 24001 without Docker socket or local execution',
                      'Independent node daemon runs as root and owns its private control state',
                      'Task runs as UID 23003 with no capabilities or node/controller credentials',
                      'Controller/node data directories are not shared; artifacts cross HTTP',
                      'Runtime and archive helpers finish before the controller releases the lease',
                      'Root node downloads immutable input, enforces protected environment lock and executes real BishengIR IR transform as UID23003'],
            'npu_used':False,'scope':'Real container UID split on one physical server; host accounts and global Docker configuration unchanged'}
    atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/root-roles-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    for name in reversed(containers):
        result=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
        if result.returncode==0:
            info=json.loads(result.stdout)[0]
            if info['Config'].get('Labels',{}).get('io.ascend-lab.rootproof')==prefix:
                logs=subprocess.run(['docker','logs',name],capture_output=True)
                (base/(name+'.log')).write_bytes(logs.stdout[-512*1024:]+logs.stderr[-512*1024:])
                subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
    try:restore_owner(base,os.getuid(),os.getgid())
    finally:
        if primary_stopped:subprocess.run([str(ROOT/'labctl'),'start'],capture_output=True,text=True,timeout=10)

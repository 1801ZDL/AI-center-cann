"""Real pinned-node workflows under poisoned Docker routing and a simulated ID mismatch.

Only the idle demo Bob worker and its private binding are controlled. No daemon,
context, existing user container or host account is changed. A loopback trap is
not a Docker server and should receive no connections.
"""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now
from jobs import ACTIVE,TERMINAL,read_json

admin=Client.from_profile(ROOT/'runtime/config.json')
assert not any(job['state'] in ACTIVE|{'queued'} for job in admin.request('/api/jobs')['jobs'])
bob=Client.from_profile(ROOT/'runtime/personas/demo_bob/client.json')
alice=Client.from_profile(ROOT/'runtime/personas/demo_alice/client.json')
config=ROOT/'runtime/workers/worker-829166f2505f/config.json';state=config.parent
base=ROOT/'runtime'/('docker-binding-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
created=[];processes=[];checks=[];connections=[];original_binding=None;injected=False

class Trap(socketserver.BaseRequestHandler):
    def handle(self):connections.append(now())

trap=socketserver.ThreadingTCPServer(('127.0.0.1',0),Trap);trap.daemon_threads=True
thread=threading.Thread(target=trap.serve_forever,daemon=True);thread.start()

def stop():
    subprocess.run([str(ROOT/'workerctl'),'stop','--config',str(config)],check=True,capture_output=True,text=True,timeout=20)
    for process in processes:
        if process.poll() is None:
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:raise AssertionError('Owned worker did not stop; inspect this exact handle')

def launch(env=None,ready=True):
    started=time.time()
    with (state/'worker.log').open('ab') as output:
        process=subprocess.Popen(['/usr/bin/python3',str(ROOT/'worker.py'),'--config',str(config)],
            stdin=subprocess.DEVNULL,stdout=output,stderr=output,env=env,start_new_session=True,cwd=ROOT)
    processes.append(process)
    if not ready:
        assert process.wait(timeout=12)!=0
        return process
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        assert process.poll() is None,'Owned worker exited during initialization'
        saved=read_json(state/'worker-process.json') or {};receipt=read_json(state/'heartbeat-receipt.json') or {}
        if saved.get('pid')==process.pid and receipt.get('acknowledged_at',0)>=started:return process
        time.sleep(.1)
    raise AssertionError('Owned worker did not publish a new authenticated heartbeat')

def wait(job_id,states,timeout=110):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        job=admin.request('/api/jobs/'+job_id)['job']
        if job['state'] in states:return job
        assert job['state'] not in TERMINAL,{'job':job_id,'state':job['state'],'reason':job['reason']}
        time.sleep(.25)
    raise AssertionError('Owned workflow observation deadline exceeded; no new attempt was submitted')

def inspect(job):
    done=subprocess.run(['docker','inspect','ascend-lab-'+job['attempt_id'][8:]],check=True,capture_output=True,text=True,timeout=12)
    return json.loads(done.stdout)[0]

def submit(label,hold=0):
    reply=bob.request('/api/jobs',{'template':'ir_v1','environment':'worker-python','name':label,
      'idempotency_key':base.name+'-'+str(len(created)),
      'parameters':{'input_id':bundle['id'],'argv':['python','ir.py',str(hold)],'lock':{'python':'3.10.12'},'timeout_seconds':90}})
    created.append(reply['job']['id']);return created[-1]

try:
    stop();launch();original_binding=read_json(state/'docker-runtime.json')
    assert original_binding and original_binding['endpoint'].startswith('unix:///')
    atomic_json(base/'original-binding.json',original_binding)
    marker='fixture-proxy-'+uuid.uuid4().hex
    cli_config=base/'personal-docker';cli_config.mkdir(mode=0o700)
    atomic_json(cli_config/'config.json',{'proxies':{'default':{'httpProxy':'http://'+marker+'@127.0.0.1:1'}},
                 'HttpHeaders':{'X-Ascend-Lab-Test':marker},'currentContext':'missing-context-'+uuid.uuid4().hex})
    source=base/'source';source.mkdir(mode=0o700)
    (source/'input.mlir').write_text('module { func.func @fold() -> i32 { %a = arith.constant 20 : i32\n %b = arith.constant 22 : i32\n %c = arith.addi %a, %b : i32\n return %c : i32 } }\n')
    (source/'ir.py').write_text('''import json,os,subprocess,sys,time
from pathlib import Path
assert MARKER not in json.dumps(dict(os.environ)), 'Synthetic personal Docker configuration reached the task'
root=Path(os.environ['LAB_JOB_DIR']);target=root/'ir/folded.mlir'
subprocess.run(['bishengir-opt','--canonicalize','input.mlir','-o',str(target)],check=True)
assert 'arith.constant 42' in target.read_text()
print('Actual offline IR completed; optional bounded hold follows.',flush=True)
time.sleep(int(sys.argv[1]))
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/folded.mlir'],'value':42,'uid':os.getuid(),'personal_docker_configuration_absent':True}))
'''.replace('MARKER',repr(marker)))
    bundle=bob.upload_input(source)
    poisoned=dict(os.environ);poisoned['DOCKER_HOST']='tcp://127.0.0.1:'+str(trap.server_address[1])
    poisoned['DOCKER_CONFIG']=str(cli_config);poisoned.pop('DOCKER_CONTEXT',None)
    for context in [None,'ascend-lab-context-that-does-not-exist']:
        stop()
        env=dict(poisoned)
        if context:env['DOCKER_CONTEXT']=context
        launch(env)
        job_id=submit('固定Docker绑定 · '+('context' if context else 'host'))
        job=wait(job_id,{'succeeded'})
        result=admin.request('/api/jobs/'+job_id+'/artifact?name=results/workload-result.json')
        assert result['value']==42 and result['uid']==23002 and result['personal_docker_configuration_absent']
        assert read_json(state/'docker-runtime.json')==original_binding and not connections
    checks.append('Actual IR creation/start/log/export/cleanup stays on the pinned daemon under poisoned DOCKER_HOST/CONTEXT/CONFIG')
    checks.append('Synthetic personal Docker proxy/header settings do not enter the task environment; loopback trap receives zero connections')
    print('PASS: poisoned routing and personal configuration isolation',flush=True)
    stop();launch()
    long_id=submit('固定Docker绑定 · 身份故障恢复',hold=55)
    running=wait(long_id,{'running'});original=inspect(running);assert original['State']['Running']
    stop()
    atomic_json(state/'docker-runtime.json',{**original_binding,'daemon_id':'simulated-daemon-change-'+uuid.uuid4().hex});injected=True
    rejected=launch(ready=False);assert rejected.returncode!=0
    assert inspect(running)['Id']==original['Id'] and inspect(running)['State']['Running']
    queued=alice.request('/api/jobs',{'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},
      'name':'固定Docker绑定 · 等待未知任务','idempotency_key':base.name+'-waiting'})['job']['id'];created.append(queued)
    unknown=wait(long_id,{'needs_attention'},timeout=50)
    summary=admin.request('/api/jobs');assert summary['reserved']['cpu']==1
    assert admin.request('/api/jobs/'+queued)['job']['state']=='queued'
    assert len(admin.request('/api/jobs/'+long_id)['attempts'])==1
    checks.append('A simulated saved-daemon mismatch refuses node startup while the original container remains; stale execution holds its resources')
    print('PASS: daemon mismatch refusal and unknown-resource retention',flush=True)
    atomic_json(state/'docker-runtime.json',original_binding);injected=False;launch()
    completed=wait(long_id,{'succeeded'});wait(queued,{'succeeded'})
    evidence=admin.request('/api/jobs/'+long_id+'/artifact?name=results/_lab/environment.json')
    assert len(admin.request('/api/jobs/'+long_id)['attempts'])==1
    assert completed['attempt_id']==running['attempt_id']
    metadata=admin.request('/api/jobs/'+long_id+'/artifact?name=environment.json')
    assert metadata['container_id']==original['Id'] and metadata['termination']['Pid']==0
    assert evidence['workload_started'] and evidence['workload_exit_code']==0
    checks.append('Restoring the exact saved binding recovers the original container/attempt, archives once, then lets the other user run')
    assert not connections and not (ROOT/'_docker_cli_empty').exists()
    report={'passed':True,'verified_at':now(),'checks':checks,'job_ids':created,'recovered_job':long_id,
      'attempt_id':completed['attempt_id'],'container_id':original['Id'],'trap_connections':len(connections),
      'mismatch_injection':'saved private daemon ID only; actual Docker daemon/context unchanged',
      'npu_kernel_launches':0,'model_calls':0,'actual_servers':1,'original_binding_restored':True}
    atomic_json(ROOT/'evidence/docker-binding-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
finally:
    if original_binding is not None:
        if injected:atomic_json(state/'docker-runtime.json',original_binding)
        # Restore this one worker with the normal operator environment before cleanup.
        stop();launch()
    for job_id in created:
        row=admin.request('/api/jobs/'+job_id)['job']
        if row['state'] in {'queued','reserved','running','cancelling'}:admin.request('/api/jobs/'+job_id+'/cancel',{})
    deadline=time.monotonic()+110
    while any(admin.request('/api/jobs/'+j)['job']['state'] not in TERMINAL for j in created) and time.monotonic()<deadline:time.sleep(.3)
    safe=all(admin.request('/api/jobs/'+j)['job']['state'] in TERMINAL for j in created)
    if original_binding is not None and safe:
        stop()
        subprocess.run([str(ROOT/'workerctl'),'start','--config',str(config)],check=True,capture_output=True,text=True,timeout=20)
    trap.shutdown();trap.server_close();thread.join(timeout=2)
    assert safe,'Owned task is still unknown; its reservation must remain held'

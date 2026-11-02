"""Own HTTP/process/Docker fixture, synthetic NPU observation, CPU payload only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import JobStore,Scheduler,atomic_json,read_json,now
from cooperative_start import DEFERRED,contract
from lab_client import Client,ClientUnavailable


def fake_probe(base):
    import npu_admission
    original_access=os.access
    os.access=lambda name,*args,**kw: True if str(name)=='/dev/davinci1' else original_access(name,*args,**kw)
    def probe():
        idle=(base/'phase').read_text()=='idle'
        return ([{'id':1,'health':'OK','utilization_pct':0 if idle else 1}],[])
    npu_admission.query=probe


def serve(path):
    from server import handler_class,ThreadingHTTPServer
    config=read_json(path);base=Path(config['fixture_base']);jobs=JobStore(base/'state',config)
    fake_probe(base)
    def launch(directory,job):
        with (directory/'launcher.log').open('ab') as log:
            return subprocess.Popen(['/usr/bin/python3','-B',str(Path(__file__).resolve()),
                '--supervise',str(directory),'--fixture-config',str(path)],stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,start_new_session=True,close_fds=True)
    jobs.backend.launch=launch
    class Observations:
        def latest(self):
            stamp=now();sample=stamp if (base/'phase').read_text()=='idle' else config['initial_sample']
            return {'sampled_at':stamp,'host':{'memory_available_bytes':8*1024**3,'shm':{'free_bytes':1024**3},
                'workspace_disk':{'free_bytes':1024**3}},'npu':{'status':'ok','sampled_at':sample,
                'devices':[{'id':1,'health':'OK','utilization_pct':0,'scoped_process_count':0,'external_process_count':0}]}}
    scheduler=Scheduler(jobs,Observations(),threading.Event());scheduler.acquire()
    # Test-only fault boundaries; production never consumes these switches.
    real_reconcile=scheduler.reconcile;real_write=jobs.write
    def before_reconcile():
        for job in jobs.all():
            result=read_json(jobs.directory(job['id'])/'completion.json') or {}
            if result.get('result',{}).get('status')==DEFERRED and not (base/'crash-after-ack').exists():
                atomic_json(base/'crash-after-ack',{'job':job['id'],'attempt':job['attempt_id']})
                os.kill(os.getpid(),signal.SIGKILL)
        return real_reconcile()
    def write(db,job):
        real_write(db,job)
        if job.get('_cooperative_deferral_count') and not (base/'crash-before-commit').exists():
            atomic_json(base/'crash-before-commit',{'job':job['id']})
            os.kill(os.getpid(),signal.SIGKILL)
    scheduler.reconcile=before_reconcile;jobs.write=write
    server=ThreadingHTTPServer(('127.0.0.1',config['port']),handler_class(Observations(),config,jobs))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    atomic_json(base/'ready.json',{'pid':os.getpid(),'recorded_at':now()})
    try:
        while True:scheduler.tick();time.sleep(.15)
    finally:server.shutdown();server.server_close()


def parent():
    assert os.environ.get('LAB_JOB_DIR'),'Submit this own CPU-only acceptance through Main'
    from docker_runtime import discover,command
    from deployment import CORE
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/cooperative-start';base.mkdir(mode=0o700)
    assert len(os.sched_getaffinity(0))>=2
    binding,_=discover();image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    name='ascend-lab-coop-'+secrets.token_hex(6);label='cooperative-start-'+Path(os.environ['LAB_JOB_DIR']).name
    def docker(args,check=True):
        argv,env=command(binding,['docker',*args]);r=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
        if check and r.returncode:raise RuntimeError('Own cooperative fixture operation failed: '+r.stderr[-500:])
        return r
    source=base/'source';source.mkdir(mode=0o700)
    for relative in CORE:
        target=source/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/relative,target)
    # Inside the fixed CPU-only fixture only: the normal executor remains byte
    # identical, while device access probes use this explicitly synthetic hook.
    instrumentation=base/'instrumentation';instrumentation.mkdir(mode=0o755)
    (instrumentation/'sitecustomize.py').write_text('import os\n_original_access=os.access\nos.access=lambda name,*a,**kw: True if str(name)=="/dev/davinci1" else _original_access(name,*a,**kw)\n')
    (base/'phase').write_text('busy');(base/'env.sh').write_text('true\n')
    payload=base/'payload.py';payload.write_text('import os\nfrom pathlib import Path\np=Path(os.environ["LAB_JOB_DIR"])/"results"/"once.txt"\nwith p.open("x") as f:f.write(os.environ["LAB_JOB_ID"])\nprint("PAYLOAD_EXACTLY_ONCE",flush=True)\n')
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    config={'username':pwd.getpwuid(os.getuid()).pw_name,'password':secrets.token_urlsafe(24),'container':name,
        'host':'127.0.0.1','port':port,'fixture_base':str(base),'initial_sample':now(),
        'scheduler':{'workspace_root':str(base),'cpu_ids':sorted(os.sched_getaffinity(0)),
            'max_running':1,'memory_mib':1024,'shm_mib':0},
        'environments':{'host-python':{'cann_env':str(base/'env.sh')},
            'zdl-python':{'uid':os.getuid(),'gid':os.getgid(),'cann_env':str(base/'env.sh')}}}
    path=base/'service-config.json';atomic_json(path,config);client=Client(config)
    clean={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(base),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1',
           'DOCKER_HOST':binding['endpoint']}
    processes=[];current=None;checks=[];job_ids=[];crashes=[]
    def start(expected_exit_marker=None):
        log=(base/'service.log').open('ab')
        p=subprocess.Popen(['/usr/bin/python3','-B',str(Path(__file__).resolve()),'--serve',str(path)],
            env=clean,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        log.close();processes.append(p)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if p.poll() is not None:
                if p.returncode==-signal.SIGKILL and expected_exit_marker and (base/expected_exit_marker).is_file():return p
                raise RuntimeError('Original fixture central exited during startup; inspect service.log')
            try:
                if client.request('/api/me'):return p
            except ClientUnavailable:pass
            time.sleep(.1)
        raise RuntimeError('Original fixture startup remains unknown; do not replace it')
    def wait(job_id,states,seconds=25):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:value=client.request('/api/jobs/'+job_id)
            except ClientUnavailable:
                time.sleep(.1);continue
            if value['job']['state'] in states:return value
            time.sleep(.1)
        raise RuntimeError('Original fixture job did not reach the expected state; preserve original IDs')
    def submit(key,environment='host-python',npu=1):
        value=client.request('/api/jobs',{'environment':environment,'argv':['/usr/bin/python3','-B',str(payload)],
            'cwd':str(base),'cpu':1,'memory_mib':128,'shm_mib':0,'npu':npu,'timeout_seconds':30,'idempotency_key':key})
        job_ids.append(value['job']['id']);return value['job']['id']
    try:
        # Bind only the copied public source and this new fixture, no Main state,
        # personal model config, Docker socket or NPU device nodes.
        nested=source/base.relative_to(ROOT);nested.mkdir(parents=True,exist_ok=True)
        docker(['create','--pull','never','--name',name,'--label','io.ascend-lab.cooperative='+label,
            '--user',str(os.getuid())+':'+str(os.getgid()),'--network','none','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--memory','256m','--memory-swap','256m','--cpus','1',
            '--cpuset-cpus',str(min(os.sched_getaffinity(0))),'--pids-limit','48','--ipc','none',
            '--env','PYTHONPATH='+str(instrumentation)+':'+str(ROOT),
            '--mount','type=bind,src='+str(source)+',dst='+str(ROOT)+',readonly',
            '--mount','type=bind,src='+str(base)+',dst='+str(base),
            '--entrypoint','/bin/sleep',image,'600']);docker(['start',name])
        current=start();host=submit('host-prestart')
        # Observe each specific SIGKILL, then recover that original database.
        for index,marker in enumerate(['crash-after-ack','crash-before-commit']):
            deadline=time.monotonic()+30
            while current.poll() is None and time.monotonic()<deadline:time.sleep(.1)
            if current.poll() is None:raise RuntimeError('Original central still alive; no replacement is allowed')
            assert current.returncode==-signal.SIGKILL and (base/marker).is_file()
            crashes.append({'pid':current.pid,'returncode':current.returncode,'boundary':marker})
            current=start('crash-before-commit' if index==0 else None)
        queued=wait(host,{'queued'});assert queued['job']['prestart_deferrals']['count']==1
        assert len(queued['attempts'])==1 and queued['attempts'][0]['state']==DEFERRED
        assert not (base/'state/jobs'/host/'results/once.txt').exists()
        cpu=submit('cpu-during-backoff',npu=0);assert wait(cpu,{'succeeded'})['job']['state']=='succeeded'
        assert client.request('/api/jobs/'+host)['job']['state']=='queued'
        checks.append('Two actual SIGKILL recoveries preserve the original prestart attempt; an independent CPU task completes while NPU work waits')
        (base/'phase').write_text('idle');finished=wait(host,{'succeeded'})
        assert len(finished['attempts'])==2 and [v['state'] for v in finished['attempts']]==[DEFERRED,'succeeded']
        checks.append('Original HTTP job runs one actual CPU marker after synthetic availability; idempotency and exactly-once marker retained')
        # A successful Docker exec must close its old broker before requeue.
        (base/'phase').write_text('busy');container_job=submit('docker-prestart','zdl-python')
        queued=wait(container_job,{'queued'})
        deadline=time.monotonic()+20
        while not queued['job'].get('prestart_deferrals') and time.monotonic()<deadline:
            time.sleep(.1);queued=client.request('/api/jobs/'+container_job)
        assert queued['job']['state']=='queued' and queued['job']['prestart_deferrals']['count']==1
        directory=base/'state/jobs'/container_job;assert not os.path.lexists(directory/'npu-admission.sock')
        assert not (directory/'results/once.txt').exists()
        (base/'phase').write_text('idle');finished=wait(container_job,{'succeeded'})
        assert len(finished['attempts'])==2 and [v['state'] for v in finished['attempts']]==[DEFERRED,'succeeded']
        checks.append('Actual Docker exec peer-bound refusal, broker teardown, source-identical inside proof and original task resume execute one CPU marker')
        for ident in [host,cpu,container_job]:
            directory=base/'state/jobs'/ident;assert (directory/'results/once.txt').read_text()==ident
            assert (directory/'stdout.log').read_text().count('PAYLOAD_EXACTLY_ONCE')==1
        assert client.request('/api/jobs')['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
        db=sqlite3.connect(base/'state/jobs.sqlite3');assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';db.close()
        report={'passed':True,'recorded_at':now(),'checks':checks,'job_ids':job_ids,'actual_sigkills':crashes,
            'contract':contract(),'npu_observation':'explicit simulator, no real NPU management or device calls',
            'npu_kernel_launches':0,'real_host_users_created':False,'docker_fixture_privileged':False,
            'scope':'Real HTTP/SQLite/host process/Docker-exec recovery with synthetic NPU observation and fixed CPU payloads; not NPU or Root isolation qualification'}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/cooperative-start-live.json',report);print(json.dumps(report))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        from jobs import identity_alive
        from executor import stop_group
        for directory in (base/'state/jobs').glob('job-*'):
            for filename in ['command-process.json','supervisor.json']:
                saved=read_json(directory/filename)
                if saved and identity_alive(saved):assert stop_group(saved['pid'],saved['start_ticks'])
        response=docker(['inspect','--format','{"label":{{json .Config.Labels}},"image":{{json .Image}},"state":{{json .State}}}',name],False)
        if response.returncode==0:
            info=json.loads(response.stdout);assert info['label']['io.ascend-lab.cooperative']==label and info['image']==image
            if info['state']['Running']:docker(['stop','--time','2',name])
            info=json.loads(docker(['inspect','--format','{{json .State}}',name]).stdout)
            assert not info['Running'] and info['Pid']==0;docker(['rm',name])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve');parser.add_argument('--supervise');parser.add_argument('--fixture-config');args=parser.parse_args()
    if args.serve:serve(args.serve)
    elif args.supervise:
        assert os.getuid()!=0
        from executor import supervisor
        fake_probe(Path(read_json(args.fixture_config)['fixture_base']))
        supervisor(Path(args.supervise),read_json(Path(args.supervise)/'dispatch.json'))
    else:parent()

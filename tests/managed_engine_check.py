"""Actual isolated-container engine acceptance, launched as a trusted queued CPU task."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from container_engine import DockerAttempt,SCRIPTS
from jobs import atomic_json,read_json

assert os.environ.get('LAB_JOB_DIR'),'Submit this acceptance through the central queue'
directory=Path(os.environ['LAB_JOB_DIR'])
config={'image':'sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273',
        'python':'/usr/bin/python3','policy':{'cpu_ids':sorted(os.sched_getaffinity(0)),'memory_mib':256},
        'users':{'alice':{'uid':23001,'gid':23001},'bob':{'uid':23002,'gid':23002}},
        'probe_forbidden_paths':[str(ROOT/'runtime/config.json'),str(directory/'tmp/node-secret.json')]}
secret=uuid.uuid4().hex
atomic_json(directory/'tmp/node-secret.json',{'node_key':secret})
reports=[]
for owner in ['alice','bob']:
    grant={'job_id':os.environ['LAB_JOB_ID'],'attempt_id':'attempt-'+uuid.uuid4().hex,
           'owner':owner,'template':'isolation_probe','parameters':{},
           'source_sha256':hashlib.sha256(SCRIPTS['isolation_probe'].read_bytes()).hexdigest(),
           'resources':{'cpu':1,'memory_mib':256,'shm_mib':16,'npu':0},
           'allocation':{'cpu_ids':[sorted(os.sched_getaffinity(0))[0]],'npu_ids':[]},
           'timeout_seconds':10,'lease_token':secret}
    engine=DockerAttempt(directory/'tmp/node-state',grant,config)
    try:
        engine.start()
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            result=engine.tick()
            if result:break
            time.sleep(.2)
        else:raise AssertionError('Container acceptance deadline exceeded')
        assert result['status']=='succeeded',result
        probe=read_json(engine.artifacts/'results/isolation.json')
        assert probe and probe['passed'],probe
        assert probe['uid']==config['users'][owner]['uid']
        assert probe['memory_limit_bytes']==str(256*1024**2),probe
        assert probe['shm_bytes']==16*1024**2
        assert secret.encode() not in (engine.payload/'run.json').read_bytes()
        metadata=read_json(engine.artifacts/'environment.json')
        assert metadata['privileged'] is False and metadata['read_only_root'] is True
        assert metadata['network_mode']=='none' and metadata['termination']['Pid']==0
        assert {m['destination'] for m in metadata['mounts']}=={'/work','/payload'}
        reports.append({'owner':owner,'probe':probe,'environment':metadata,'result':result})
    finally:
        info=engine.inspect()
        if info and info['State']['Running']:
            while engine.tick(cancel=True) is None:time.sleep(.2)
        engine.stop_log_client()
        engine.cleanup()
attack=directory/'tmp/archive-link-attack.py'
attack.write_text('from pathlib import Path\np=Path("/work/results/leak");p.symlink_to("/etc/shadow")\nprint("Created a test link without reading its target.")\n')
original=SCRIPTS['isolation_probe'];SCRIPTS['isolation_probe']=attack
grant={'job_id':os.environ['LAB_JOB_ID'],'attempt_id':'attempt-'+uuid.uuid4().hex,
       'owner':'alice','template':'isolation_probe','parameters':{},
       'source_sha256':hashlib.sha256(attack.read_bytes()).hexdigest(),
       'resources':{'cpu':1,'memory_mib':256,'shm_mib':16,'npu':0},
       'allocation':{'cpu_ids':[sorted(os.sched_getaffinity(0))[0]],'npu_ids':[]},'timeout_seconds':10}
engine=DockerAttempt(directory/'tmp/node-state',grant,config)
try:
    engine.start()
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        attack_result=engine.tick()
        if attack_result:break
        time.sleep(.2)
    else:raise AssertionError('Archive link rejection deadline exceeded')
    assert attack_result['status']=='failed' and attack_result['execution_status']=='succeeded',attack_result
    assert attack_result['failure_stage']=='artifact_collection'
    assert not (engine.artifacts/'results/leak').exists()
finally:
    engine.stop_log_client();engine.cleanup();SCRIPTS['isolation_probe']=original
report={'passed':True,'checks':['Actual task UIDs 23001 and 23002','No capabilities or inherited credentials',
        'Read-only image, no network, no Docker socket or control mounts','CPU/memory/SHM limits observed inside container',
        'Protected host files and root supervisor environment unreadable','Daemon confirms exited PID namespace','Only own labeled containers removed',
        'Read-only artifact helper rejects a task-created link without exporting its target'],
        'reports':reports,'archive_link_rejection':attack_result,'npu_used':False,'scope':'CPU Docker engine; not itself a two-server deployment proof'}
atomic_json(directory/'results/managed-engine-validation.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

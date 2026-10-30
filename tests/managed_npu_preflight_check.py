"""Queue-owned real DockerAttempt: access failure must stop a CPU-only sentinel.

Reserves one physical NPU, but never imports ACL/torch or launches a kernel.
Uses the same immutable bootstrap, environment probe, export and cleanup as a node.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import atomic_json,read_json,now
from container_engine import DockerAttempt,SCRIPTS
from docker_runtime import discover
from npu_admission import query,selected_reading
from npu_device import fingerprint,validate
from workflow_spec import runtime_manifest,validate as workflow


def main():
    directory=Path(os.environ['LAB_JOB_DIR']);parent=read_json(directory/'dispatch.json')
    assert parent['resources']['npu']==1 and len(parent['allocation']['npu_ids'])==1
    physical=parent['allocation']['npu_ids'][0];cpus=parent['allocation']['cpu_ids']
    def idle():
        devices,processes=query();selected_reading([physical],(devices,processes))
        return [{k:row.get(k) for k in ['id','name','health','utilization_pct']} for row in devices if row['id']==physical]
    before=idle();binding,_=discover()
    policy={'device_ids':[physical],'visible_ids':{str(physical):0},'device_gid':1001,
       'management_devices':['/dev/davinci_manager',*([ '/dev/hisi_hdc'] if Path('/dev/hisi_hdc').exists() else [])],
       'driver_paths':['/usr/local/Ascend/driver'],'query_tool':shutil.which('npu-smi'),
       'management_env':'/data/pri/cann/set_env.sh'}
    validate(policy,host=True)
    resources={'cpu':1,'memory_mib':256,'shm_mib':0,'npu':1}
    parameters=workflow({'input_id':'input-'+uuid.uuid4().hex[:24],
       'argv':['/usr/bin/python3','-I','-B','/inputs/sentinel.py'],
       'lock':{'python':'3.10.12'},'resources':resources,'timeout_seconds':45,'input_mode':'readonly'},'accuracy')
    owner=parent['owner'];image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    source=b'from pathlib import Path\nPath("/work/results/workload-started").write_text("CPU sentinel only; no numerical test\\n")\n'
    grant={'job_id':parent['id'],'attempt_id':'attempt-'+uuid.uuid4().hex,'owner':owner,
       'template':'accuracy_v1','parameters':parameters,'resources':resources,'timeout_seconds':45,
       'allocation':{'cpu_ids':cpus[:1],'npu_ids':[physical]},'runtime_files':runtime_manifest(),
       'source_sha256':hashlib.sha256(SCRIPTS['accuracy_v1'].read_bytes()).hexdigest(),
       'npu_policy_sha256':fingerprint(policy),
       'input':{'id':parameters['input_id'],'owner':owner,'files':[{'name':'sentinel.py','size':len(source),'sha256':hashlib.sha256(source).hexdigest()}]}}
    config={'image':image,'python':'/usr/bin/python3','users':{owner:{'uid':23901,'gid':23901}},
       'templates':['accuracy_v1'],'max_timeout_seconds':60,'docker_runtime':binding,'npu':policy,
       'policy':{'cpu_ids':cpus[:1],'memory_mib':256,'shm_mib':0,'max_running':1}}
    engine=DockerAttempt(directory/'tmp/preflight-engine',grant,config)
    engine.inputs.mkdir(mode=0o755);(engine.inputs/'sentinel.py').write_bytes(source);(engine.inputs/'sentinel.py').chmod(0o444)
    atomic_json(directory/'tmp/preflight-engine.json',{'attempt_id':grant['attempt_id'],'container_name':engine.name})
    result=None;report=None
    try:
        engine.start();deadline=time.monotonic()+65
        while time.monotonic()<deadline:
            result=engine.tick()
            if result is not None:break
            time.sleep(.2)
        if result is None:raise RuntimeError('Original preflight attempt is still unconfirmed; do not repeat it')
        receipt=read_json(engine.artifacts/'results/_lab/environment.json');assert isinstance(receipt,dict)
        access=receipt['device_access'];assert access['requested_device_ids']==[physical]
        assert access['uid']==access['euid']==23901 and access['cap_effective']==access['cap_permitted']=='0000000000000000'
        started=(engine.artifacts/'results/workload-started').exists()
        if access['passed'] is False:
            assert result['status']=='failed' and result['exit_code']==78 and result['failure_stage']=='npu_device_access'
            assert receipt['locked'] is False and receipt['workload_started'] is False and not started
        else:
            assert access['passed'] is True and receipt['locked'] is True and receipt['workload_started'] is True and started
            assert result['status']=='succeeded' and receipt['workload_exit_code']==0
        metadata=read_json(engine.artifacts/'environment.json')
        assert metadata['privileged'] is False and metadata['read_only_root'] and metadata['network_mode']=='none'
        assert metadata['termination']['Pid']==0 and metadata['task_uid']==23901
        exposed={item['PathOnHost'] for item in metadata['device_nodes']}
        assert exposed=={'/dev/davinci'+str(physical),*policy['management_devices']}
        assert not any(item['destination']=='/var/run/docker.sock' for item in metadata['mounts'])
        for name in ['results/_lab/environment.json','environment.json']:
            target=directory/'results'/('protected-preflight.json' if name.startswith('results/') else 'preflight-execution.json')
            shutil.copyfile(engine.artifacts/name,target);target.chmod(0o600)
        after=idle()
        report={'passed':True,'verified_at':now(),'parent_job':parent['id'],'managed_attempt':grant['attempt_id'],
            'access_allowed':access['passed'],'negative_gate_verified':access['passed'] is False,
            'protected_receipt':receipt,'execution':result,'workload_sentinel_started':started,
            'before':before,'after':after,'npu_kernel_launches':0,'acl_initialized':False,
            'scope':'Real managed bootstrap/access/export/cleanup with CPU sentinel; not numerical or full runtime acceptance',
            'existing_containers_modified':False,'runtime_files':runtime_manifest()}
    finally:
        info=engine.inspect()
        if info and info['State']['Running']:
            # Only this exact labeled attempt; no retry or unrelated process action.
            from container_engine import run
            run(['docker','stop','--time','2',engine.name],binding=engine.binding())
            info=engine.inspect()
        if info and (info['State']['Running'] or info['State']['Pid']):raise RuntimeError('Owned preflight container termination is unconfirmed')
        engine.cleanup()
    if report is None:raise RuntimeError('Preflight proof did not complete')
    report['cleanup_confirmed']=engine.state['stage']=='cleaned' and engine.inspect() is None
    assert report['cleanup_confirmed'];atomic_json(directory/'results/managed-preflight.json',report)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()

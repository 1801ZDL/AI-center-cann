"""Exercise live CPU fixture teardown before repairing protected file ownership.

One synthetic local grant and an owned file-backed ext4 volume; no scheduler
completion, retry, physical NPU device, host account or global cleanup.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from disk_workspace_fixture import OwnedDisk
from docker_runtime import discover,command
from lab_client import atomic_json,read_json
from node_owner_binding_check import IMAGE,cleanup_owned_attempts


def main():
    jobdir=Path(os.environ['LAB_JOB_DIR']);dispatch=read_json(jobdir/'dispatch.json')
    assert dispatch['resources']['npu']==0
    cpus=dispatch['allocation']['cpu_ids'];base=jobdir/'tmp/live-owned-cleanup';base.mkdir(mode=0o700)
    state=base/'state';state.mkdir(mode=0o700)
    attempt='attempt-'+uuid.uuid4().hex;name='ascend-lab-'+attempt.removeprefix('attempt-')
    folder=state/'node/attempts'/attempt;control=folder/'control';control.mkdir(mode=0o700,parents=True)
    receipt=folder/'receipt';receipt.mkdir(mode=0o777);receipt.chmod(0o777)
    atomic_json(state/'node/config.json',{'image':IMAGE,'node_id':'cleanup-fixture','physical_id':'cleanup-fixture'})
    grant={'attempt_id':attempt,'node_id':'cleanup-fixture','physical_id':'cleanup-fixture','resources':{'npu':0}}
    atomic_json(control/'grant.json',grant);grant_sha=hashlib.sha256((control/'grant.json').read_bytes()).hexdigest()
    binding,_=discover()
    def docker(args,check=True,timeout=45):
        argv,env=command(binding,['docker',*map(str,args)])
        result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned cleanup fixture failed: '+result.stderr[-1600:])
        return result
    prefix='ascend-lab-cleanup-'+uuid.uuid4().hex[:8];volume=prefix+'-disk';volume_created=False
    disk=OwnedDisk(base,prefix,IMAGE,cpus,docker);task_created=False;terminated=False;report=None
    try:
        device=disk.create()
        docker(['volume','create','--label','io.ascend-lab.cleanup='+prefix,'--driver','local','--opt','type=ext4',
            '--opt','device='+device,'--opt','o=nosuid,nodev',volume]);volume_created=True
        script="""import os,time
from pathlib import Path
os.chown('/receipt',0,0);os.chmod('/receipt',0o700)
p=Path('/receipt/protected.json');p.write_text('{"fixture":true}');p.chmod(0o600)
f=open('/work/held','w');f.write('owned fixture');f.flush()
print('READY',flush=True);time.sleep(120)
"""
        docker(['create','--name',name,'--label','io.ascend-lab.attempt='+attempt,'--network','none','--read-only',
            '--cap-drop','ALL','--cap-add','CHOWN','--security-opt','no-new-privileges:true','--memory','64m',
            '--memory-swap','64m','--pids-limit','16','--cpuset-cpus',str(cpus[0]),'--cpus','0.25',
            '--mount','type=volume,src='+volume+',dst=/work',
            '--mount','type=bind,src='+str(receipt)+',dst=/receipt',
            '--entrypoint','/usr/bin/python3',IMAGE,'-I','-c',script]);task_created=True
        docker(['start',name]);deadline=time.monotonic()+15
        while True:
            info=json.loads(docker(['inspect',name]).stdout)[0]
            assert info['State']['Running'],'Owned fixture ended before teardown'
            if 'READY' in docker(['logs',name]).stdout:break
            if time.monotonic()>deadline:raise RuntimeError('Owned fixture readiness timeout')
            time.sleep(.1)
        cleanup_owned_attempts(state,base,cpus,docker);terminated=True
        record=read_json(base/'owned-attempt-cleanup.json');assert len(record['containers'])==1
        item=record['containers'][0];assert item['before']['Running'] and not item['after']['Running'] and item['after']['Pid']==0
        assert docker(['inspect',name],check=False).returncode!=0
        assert hashlib.sha256((control/'grant.json').read_bytes()).hexdigest()==grant_sha
        repair="import os;from pathlib import Path;p=Path('/receipt/protected.json');assert p.stat().st_uid==0 and p.stat().st_mode&0o777==0o600;os.chown(p,"+str(os.getuid())+","+str(os.getgid())+");os.chown('/receipt',"+str(os.getuid())+","+str(os.getgid())+")"
        docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE',
            '--memory','64m','--cpuset-cpus',str(cpus[0]),'--mount','type=bind,src='+str(receipt)+',dst=/receipt',
            '--entrypoint','/usr/bin/python3',IMAGE,'-I','-c',repair])
        assert read_json(receipt/'protected.json')=={'fixture':True}
        report={'passed':True,'synthetic_local_grant':True,'actual_running_cpu_container_stopped':True,
            'termination_confirmed_before_ownership_repair':True,'protected_receipt_preserved':True,
            'grant_sha256_unchanged':True,'scheduler_completion_claimed':False,'task_retry':False,
            'npu_kernel_launches':0,'host_account_changes':False}
    finally:
        if task_created and not terminated:
            cleanup_owned_attempts(state,base,cpus,docker)
        if volume_created:docker(['volume','rm',volume])
        disk.cleanup()
    report['owned_disk_released']=disk.report.get('loop_released',False)
    # Native helper's persisted release record is the authority, not this flag.
    loop=read_json(disk.folder/'loop-state.json');assert loop['released']
    report['owned_disk_released']=True;report['backing_file_retained']=True
    atomic_json(jobdir/'results/live-owned-cleanup.json',report);print(json.dumps(report))


if __name__=='__main__':main()

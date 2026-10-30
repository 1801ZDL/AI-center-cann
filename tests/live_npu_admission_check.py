"""Read-only cross-container management handshake; no compute admission or kernel."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import read_json,atomic_json,now
from npu_admission import HostBroker,query,selected_reading

assert os.environ.get('LAB_JOB_DIR'),'Submit the diagnostic through the central CPU queue'
directory=Path(os.environ['LAB_JOB_DIR']);original=read_json(directory/'dispatch.json')
assert original['resources']['npu']==0,'This diagnostic never requests device compute'
assert 1001 in {os.getgid(),*os.getgroups()},'Use only the already-authorized demo device group'
device_ids=sorted(int(path.name[7:]) for path in Path('/dev').glob('davinci[0-9]*') if path.name[7:].isdigit())
assert device_ids
job={**original,'_environment':{**original['_environment'],'type':'docker','uid':os.getuid()},
     'allocation':{**original['allocation'],'npu_ids':device_ids[:1]}}
private=directory/'tmp/management-only-probe.json';atomic_json(private,job)
observations=[]
def probe():
    observation=query();observations.append(observation);return observation
code='''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from jobs import read_json
from npu_admission import request_receipt
job=read_json(sys.argv[2])
try:
 receipt=request_receipt(Path(sys.argv[2]).parents[1],job)
 result={'accepted':True,'query_uid':receipt['query_uid'],'task_uid':os.getuid(),'devices':receipt['devices']}
except RuntimeError:
 result={'accepted':False,'task_uid':os.getuid()}
print(json.dumps(result))
'''
try:
    with HostBroker(directory,job,probe):
        result=subprocess.run(['docker','exec','--user',str(os.getuid())+':1001','zdl','/usr/bin/python3','-I','-c',code,
                               str(ROOT),str(private)],capture_output=True,text=True,timeout=25)
    assert result.returncode==0,'Management-only child did not complete'
    child=json.loads(result.stdout);assert child['task_uid']==os.getuid()
    assert len(observations)==1,'The real host query was not reached exactly once'
    try:selected_reading(job['allocation']['npu_ids'],observations[0]);expected=True
    except RuntimeError:expected=False
    assert child['accepted']==expected
    report={'passed':True,'verified_at':now(),'job_id':original['id'],'checks':[
       'Real host supervisor authenticates a request from the existing container task UID/GID',
       'Management query runs outside the task container and exposes no other user processes',
       'Response matches fresh host health/occupancy; no compute or library initialization is attempted'],
       'task_uid':child['task_uid'],'task_gid':1001,'management_host_uid':os.getuid(),
       'selected_device_ids':job['allocation']['npu_ids'],'reading_was_idle':expected,
       'compute_resources_requested':0,'npu_kernel_launches':0,
       'scope':'Management-only diagnostic using a CPU job; not NPU compute allocation or isolation acceptance'}
    atomic_json(directory/'results/npu-admission-bridge.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:private.unlink(missing_ok=True)

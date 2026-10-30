"""One allocated non-root device-node probe; no runtime initialization or kernels."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import ACTIVE,TERMINAL,JobStore,read_json,atomic_json,now
from lab_client import Client
from npu_device import validate

parser=argparse.ArgumentParser();parser.add_argument('--queue-wait',type=int,default=60);args=parser.parse_args()
assert 1<=args.queue_wait<=180
config=read_json(ROOT/'runtime/config.json');admin=Client.from_profile(ROOT/'runtime/config.json')
assert not [job for job in admin.request('/api/jobs')['jobs'] if job['state'] in ACTIVE|{'queued'}]
for action in ['stop','start']:subprocess.run([str(ROOT/'labctl'),action],capture_output=True,check=True,timeout=15)
jobs=JobStore(ROOT/'runtime',config);tag='npu_'+uuid.uuid4().hex[:8];directory=ROOT/'runtime/personas'/tag
workspace=directory/'workspace';workspace.mkdir(mode=0o700,parents=True)
password=jobs.access.create_user(tag,workspace,environments=['worker-python'],quota={'npu':1})
token=jobs.access.issue_token(tag,{'jobs:read','jobs:submit','jobs:cancel'})
personal=directory/'client.json';atomic_json(personal,{'username':tag,'password':password,'token':token,'host':'127.0.0.1','port':config['port']})
policy={'device_ids':[0,1],'visible_ids':{'0':0,'1':0},'device_gid':1001,
        'management_devices':['/dev/davinci_manager','/dev/hisi_hdc'],'driver_paths':['/usr/local/Ascend/driver'],
        'query_tool':shutil.which('npu-smi'),'management_env':'/data/pri/cann/set_env.sh'}
validate(policy,host=True)
profile={'name':tag+'-device-node','physical_id':jobs.backend.physical_id,'templates':['npu_probe'],
         'users':{tag:{'uid':23901,'gid':23901}},'npu':policy,
         'policy':{'cpu_ids':jobs.policy['cpu_ids'][-1:],'memory_mib':512,'shm_mib':0,'max_running':1}}
node,key=jobs.nodes.enroll(profile);state=ROOT/'runtime/workers'/node;state.mkdir(mode=0o700)
path=state/'config.json';atomic_json(path,{**profile,'node_id':node,'node_key':key,'policy_revision':1,'state_dir':str(state),
     'controller':'http://127.0.0.1:'+str(config['port']),'image':'sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273','python':'/usr/bin/python3'})
def ctl(action):subprocess.run([str(ROOT/'workerctl'),action,'--config',str(path)],check=True,capture_output=True,timeout=20)
job_id=None
try:
    ctl('start');client=Client.from_profile(personal)
    job=client.request('/api/jobs',{'template':'npu_probe','environment':'worker-python','name':'受控NPU设备白名单验收','idempotency_key':tag})['job'];job_id=job['id']
    atomic_json(ROOT/'runtime/managed-npu-submission.json',{'job_id':job_id,'node_id':node})
    print(json.dumps({'job_id':job_id,'node_id':node}),flush=True)
    queue_deadline=time.monotonic()+args.queue_wait;deadline=queue_deadline+80
    while time.monotonic()<deadline:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state']=='queued' and time.monotonic()>=queue_deadline:client.request('/api/jobs/'+job_id+'/cancel',{})
        if job['state'] in TERMINAL|{'needs_attention'}:break
        time.sleep(.3)
    else:raise RuntimeError('Managed device probe did not settle within deadline')
    report={'passed':job['state']=='succeeded','verified_at':now(),'job_id':job_id,'node_id':node,
      'state':job['state'],'reason':job['reason'],'allocation':job['allocation'],'task_uid':23901,
      'npu_kernel_launches':0,'scope':'Device-node whitelist and non-root file access only; no ACL identity mapping or numerical kernel acceptance'}
    if job['state']=='succeeded':
        result=client.request('/api/jobs/'+job_id+'/artifact?name=results%2Fnpu-device-probe.json')
        env=client.request('/api/jobs/'+job_id+'/artifact?name=environment.json')
        assert result['passed'] and result['uid']==23901 and 1001 in result['groups']
        assert result['physical_device_files']==job['allocation']['npu_ids']==result['opened_devices']
        assert result['cap_effective']=='0000000000000000' and result['no_new_privileges']=='1'
        actual={item['PathOnHost'] for item in env['device_nodes']}
        expected={'/dev/davinci'+str(value) for value in job['allocation']['npu_ids']}|set(policy['management_devices'])
        assert actual==expected
        assert any(item['destination']=='/usr/local/Ascend/driver' and not item['writable'] for item in env['mounts'])
        assert env['termination']['Pid']==0 and job['result']['npu_release_confirmed']
        report.update(result=result,device_nodes=env['device_nodes'],driver_read_only=True,
                      release_basis=job['result']['npu_release_basis'])
    atomic_json(ROOT/'evidence/managed-npu-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    if job_id:
        job=admin.request('/api/jobs/'+job_id)['job']
        if job['state'] in {'queued','reserved','running','cancelling'}:admin.request('/api/jobs/'+job_id+'/cancel',{})
        deadline=time.monotonic()+50
        while job['state'] not in TERMINAL and time.monotonic()<deadline:
            time.sleep(.3);job=admin.request('/api/jobs/'+job_id)['job']
        if job['state'] not in TERMINAL:raise RuntimeError('Unconfirmed managed probe remains held; node left available for reconciliation')
    ctl('stop');jobs.nodes.disable(node);jobs.access.disable_user(tag,True);jobs.access.close()

"""Queue-only host/container Unix handshake; synthetic inventory, no NPU calls."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import atomic_json,now
from npu_admission import HostBroker,SOCKET_NAME


def main():
    base=Path(os.environ['LAB_JOB_DIR']);assert base.is_relative_to(ROOT/'runtime/jobs')
    assert len(os.sched_getaffinity(0))==1
    source=base/'tmp/socket-code';source.mkdir(mode=0o700)
    for p in ROOT.glob('*.py'):shutil.copy2(p,source/p.name)
    directory=base/'tmp'/('nested-'+'x'*80)/('nested-'+'y'*80);directory.mkdir(parents=True,mode=0o700)
    assert len(os.fsencode(directory/SOCKET_NAME))>107
    job={'id':base.name,'attempt_id':'attempt-'+secrets.token_hex(16),'node_id':'socket-test',
         '_token':secrets.token_hex(24),'_environment':{'uid':os.getuid(),'type':'docker'},'allocation':{'npu_ids':[0]}}
    atomic_json(directory/'request.json',job)
    name='ascend-lab-socket-'+base.name[4:];label='io.ascend-lab.socketproof='+base.name
    calls=[]
    def probe():calls.append(True);return ([{'id':0,'health':'OK','utilization_pct':0}],[])
    script='''import json,os,sys
from pathlib import Path
sys.path.insert(0,"/socket-code")
from npu_admission import request_receipt,validate_receipt
job=json.loads(Path("/run/lab-job/request.json").read_text())
try:request_receipt("/run/lab-job",{**job,"_token":"wrong-token"})
except RuntimeError:pass
else:raise AssertionError("Wrong token accepted")
receipt=request_receipt("/run/lab-job",job);validate_receipt(receipt,job)
assert receipt["query_uid"]==os.getuid() and receipt["task_uid"]==os.getuid()
s=dict(line.split(":",1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
assert s["CapEff"].strip()=="0000000000000000" and s["NoNewPrivs"].strip()=="1"
assert not Path("/var/run/docker.sock").exists()
print(json.dumps({"passed":True,"uid":os.getuid(),"cap_effective":s["CapEff"].strip(),"no_new_privileges":True}))
'''
    image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    argv=['docker','run','--rm','--pull','never','--name',name,'--label',label,'--network','none','--read-only',
          '--user',str(os.getuid())+':'+str(os.getgid()),'--cap-drop','ALL','--security-opt','no-new-privileges:true',
          '--memory','128m','--memory-swap','128m','--pids-limit','32','--cpus','0.25',
          '--cpuset-cpus',str(min(os.sched_getaffinity(0))),
          '--mount','type=bind,src='+str(source)+',dst=/socket-code,readonly',
          '--mount','type=bind,src='+str(directory)+',dst=/run/lab-job,readonly',
          '--entrypoint','/usr/bin/timeout',image,'--kill-after=3s','40s','/usr/bin/python3','-I','-c',script]
    try:
        with HostBroker(directory,job,probe):
            result=subprocess.run(argv,check=True,capture_output=True,text=True,timeout=50)
            peer=json.loads(result.stdout);assert peer['passed'] and len(calls)==1
        assert not (directory/SOCKET_NAME).exists()
        report={'passed':True,'recorded_at':now(),'host_socket_path_bytes':len(os.fsencode(directory/SOCKET_NAME)),
                'container_uid':peer['uid'],'container_cap_effective':peer['cap_effective'],
                'container_no_new_privileges':peer['no_new_privileges'],'synthetic_inventory_calls':len(calls),
                'wrong_token_rejected':True,'endpoint_cleaned':True,'npu_queries':0,'npu_kernel_launches':0,
                'scope':'Real cross-container Unix socket, peer UID and one-use authenticated receipt using synthetic inventory; not hardware admission or NPU compute acceptance'}
        atomic_json(base/'results/socket-path-report.json',report);print(json.dumps(report))
    finally:
        # Inspect only our exact name/label. Container timeout bounds ambiguous
        # client failures; never remove a still-running/unknown container here.
        r=subprocess.run(['docker','inspect',name],capture_output=True,text=True,timeout=15)
        if r.returncode==0:
            info=json.loads(r.stdout)[0];assert info['Config']['Labels'].get('io.ascend-lab.socketproof')==base.name
            assert not info['State']['Running'],'Preserve the original running container until its deadline'
            subprocess.run(['docker','rm',name],check=True,capture_output=True,timeout=15)


if __name__=='__main__':main()

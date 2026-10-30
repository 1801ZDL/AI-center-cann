"""Queue-only prepare a new owned Root node and finite tmpfs for full builds."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,read_json,now
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR'),'Provision owned Docker objects through queue'
base=ROOT/'runtime/full-build-20261030';state=read_json(base/'control.json');assert state['setup_complete']
assert not state.get('root_node_container')
binding,_=discover();cpus=','.join(map(str,os.sched_getaffinity(0)))

def docker(args,input=None):
    argv,env=command(binding,['docker',*args]);env['DOCKER_BUILDKIT']='0'
    result=subprocess.run(argv,input=input,env=env,capture_output=True,text=True,check=True,timeout=240)
    return result.stdout

context=base/'environment-layer';context.mkdir(mode=0o700)
setup='export PATH=/opt/lab-build-venv/bin:/opt/lab-llvm/bin:/usr/local/python3.11.15/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
(context/'build-env.sh').write_text(setup);(context/'build-env.sh').chmod(0o644)
(context/'Dockerfile').write_text('FROM '+state['image']+'\nLABEL io.ascend-lab.fullbuild="20261030-env"\nCOPY build-env.sh /opt/lab-build-env.sh\n')
docker(['build','--network','none','--pull=false','--cpuset-cpus',cpus,'--memory','256m','--memory-swap','256m',
        '--iidfile',str(base/'root-image-id'),str(context)])
image=(base/'root-image-id').read_text().strip();assert image.startswith('sha256:')
prefix='ascend-lab-fullbuild-'+uuid.uuid4().hex[:8];volume=prefix+'-work';container=prefix+'-node'
docker(['volume','create','--label','io.ascend-lab.fullbuild-workspace=20261030',
        '--driver','local','--opt','type=tmpfs','--opt','device=tmpfs',
        '--opt','o=size=32g,nr_inodes=262144,mode=0700,uid=0,gid=0,nosuid,nodev',volume])
path=json.loads(docker(['volume','inspect',volume]))[0]['Mountpoint']
code=base/'root-node-code';code.mkdir(mode=0o755)
with tarfile.open(ROOT/'evidence/ascend-lab-server-v021.tar.gz') as tar:
    for member in tar:
        assert member.isfile() and '..' not in Path(member.name).parts and not member.name.startswith('/')
        target=code/member.name;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
        with tar.extractfile(member) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
        target.chmod(0o555 if member.name in {'servicectl','workerctl','labctl','accessctl','install-client'} else 0o444)
node_state=base/'root-node-state';node_state.mkdir(mode=0o700)
state.update(root_node_container=container,root_workspace_volume=volume,root_workspace_path=path,
             root_node_state=str(node_state),root_image=image,environment_setup='/opt/lab-build-env.sh')
atomic_json(base/'control.json',state)
# This container remains idle until the final private configuration is supplied.
docker(['run','--detach','--name',container,'--label','io.ascend-lab.fullbuild-node=20261030',
        '--network','host','--security-opt','no-new-privileges:true','--cap-drop','ALL',
        '--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER','--cap-add','SETUID','--cap-add','SETGID',
        '--memory','384m','--memory-swap','384m','--cpus','0.25','--cpuset-cpus',cpus,'--pids-limit','128',
        '--mount','type=bind,src='+str(code)+',dst=/opt/ascend-lab-node-code,readonly',
        '--mount','type=bind,src='+str(node_state)+',dst='+str(node_state),
        '--mount','type=volume,src='+volume+',dst='+path+',volume-nocopy',
        '--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
        '--mount','type=bind,src='+shutil.which('docker')+',dst=/usr/bin/docker,readonly',
        '--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
        '--entrypoint','/opt/lab-build-venv/bin/python',image,'-I','-c','import time;time.sleep(7200)'])
probe='import json,os,sys;from pathlib import Path;p=Path(sys.argv[1]);s=p.stat();v=os.statvfs(p);print(json.dumps({"device":s.st_dev,"root_inode":s.st_ino,"bytes":v.f_blocks*v.f_frsize,"inodes":v.f_files}))'
slot=json.loads(docker(['exec',container,'/opt/lab-build-venv/bin/python','-I','-c',probe,path]))
slot.update(id='fullbuild0',storage_id=uuid.uuid4().hex,path=path,filesystem='tmpfs')
atomic_json(base/'root-workspace.json',{'slots':[slot]})
report={'passed':True,'recorded_at':now(),'container':container,'volume':volume,'image':image,
        'slot':slot,'environment_setup':'/opt/lab-build-env.sh','task_memory_mib':49152,
        'scope':'Owned Root node/32GiB finite tmpfs preparation, not disk-backed ext4 or build acceptance',
        'npu_kernel_launches':0,'host_accounts_modified':False}
atomic_json(base/'root-node-prepared.json',report)
atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/full-build-root-node.json',report)
print(json.dumps(report,indent=2))

"""Queue-only bounded prerequisites for disk-quota full compiler acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import read_json,atomic_json
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR')
base=ROOT/'runtime/quota-build-20261030';base.mkdir(mode=0o700,exist_ok=True)
binding,_=discover();cpus=','.join(map(str,os.sched_getaffinity(0)))
images=[read_json(ROOT/'runtime/full-build-20261030/runtime-image.json')['image'],
        (ROOT/'runtime/toolchain-cache-20261030/offline-image-id').read_text().strip()]
script=r'''import hashlib,importlib.metadata,json,os,platform,shutil,subprocess
from pathlib import Path
v={'uid':os.getuid(),'libc':platform.libc_ver(),'tools':{},'files':{},'packages':{}}
for n in ['ccache','c++','cmake','ninja','ld.lld','git']:
 p=shutil.which(n)
 if not p:v['tools'][n]=None;continue
 r=subprocess.run([p,'--version'],capture_output=True,text=True,timeout=8)
 v['tools'][n]={'path':p,'exit_code':r.returncode,'version':(r.stdout or r.stderr).splitlines()[:2]}
 if n=='ccache':
  r=subprocess.run(['ldd',p],capture_output=True,text=True,timeout=8);v['ccache_ldd']=r.stdout+r.stderr
for n in ['/opt/lab-build-env.sh','/opt/lab-pybind-import-audit.json','/opt/lab-toolchain-link-audit.json','/opt/lab-llvm-manifest.json','/opt/lab-bishengir-manifest.json']:
 p=Path(n);v['files'][n]=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
for n in ['pybind11','setuptools','triton-ascend']:
 try:v['packages'][n]=importlib.metadata.version(n)
 except importlib.metadata.PackageNotFoundError:v['packages'][n]=None
print(json.dumps(v))
'''
rows=[]
for index,image in enumerate(images):
    executable='/opt/lab-build-venv/bin/python' if index==0 else '/usr/local/python3.11.15/bin/python3.11'
    argv,env=command(binding,['docker','run','--rm','--pull','never','--label','io.ascend-lab.quota-buildproof='+Path(os.environ['LAB_JOB_DIR']).name,
        '--network','none','--read-only','--user','23900:23900','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--memory','384m','--memory-swap','384m','--cpus','0.5','--cpuset-cpus',cpus,'--pids-limit','32',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--entrypoint','/usr/bin/timeout',image,'--kill-after=2s','35s',executable,'-I','-c',script])
    result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr[-1600:]
    rows.append({'image':image,**json.loads(result.stdout)})
report={'passed':True,'images':rows,'scope':'Read-only queued image/toolchain prerequisites; no build, model or NPU execution'}
atomic_json(base/'prerequisites.json',report);atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/quota-build-prerequisites.json',report)
print(json.dumps(report,indent=2))

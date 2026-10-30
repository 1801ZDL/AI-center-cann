"""Queue-only, read-only image/toolchain prerequisite probe for an isolated build."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,now
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR'),'Run preparation through the queue'
binding,_=discover();cpu=str(min(os.sched_getaffinity(0)));label=uuid.uuid4().hex
images=['sha256:118386a81427e5a77d2dbb8a7cabe81a2159d0cc3dca8c4bcb2bed72d3fb99f7',
        'sha256:820dc486ebd3931addfaa9cf867a965534b15b78351d945a7119cb8bf3cfcf57',
        'sha256:1fe13f7f9afa32d7ec13ec9ba42b38f7ed05cfd284e19e705ee54ca992162704']
script=r'''import json,os,platform,shutil,subprocess
from pathlib import Path
v={'uid':os.getuid(),'libc':platform.libc_ver(),'os_release':Path('/etc/os-release').read_text(),'executables':{}}
for name in ['python3','python3.11','python3.12','cc','c++','clang','clang++','ld.lld','cmake','ninja','git']:
 path=shutil.which(name)
 if not path:continue
 result=subprocess.run([path,'--version'],capture_output=True,text=True,timeout=8)
 v['executables'][name]={'path':path,'status':result.returncode,'version':(result.stdout or result.stderr).splitlines()[:2]}
v['cann_set_env']=[str(p) for p in [Path('/usr/local/Ascend/ascend-toolkit/set_env.sh'),Path('/usr/local/Ascend/ascend-toolkit/latest/set_env.sh')] if p.is_file()]
print(json.dumps(v))
'''
reports=[]
for image in images:
    argv,env=command(binding,['docker','run','--pull','never','--rm','--label','io.ascend-lab.buildprobe='+label,
        '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--user','23701:23701','--memory','384m','--memory-swap','384m','--cpuset-cpus',cpu,'--cpus','0.5',
        '--pids-limit','64','--entrypoint','/bin/sh',image,'-c',
        'p=$(command -v python3.11 || command -v python3 || command -v python); exec "$p" -I -B -c "$1"','image-probe',script])
    result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=45)
    if result.returncode:
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'tmp'/('image-probe-'+image[7:19]+'.json'),
                    {'image':image,'status':result.returncode,'stdout':result.stdout[-4000:],'stderr':result.stderr[-4000:]})
        reports.append({'image':image,'probe_passed':False,'status':result.returncode})
    else:reports.append({'image':image,'probe_passed':True,**json.loads(result.stdout)})

llvm=Path('/home/h00951476/llvm-install')
files=['include/llvm/Config/llvm-config.h','lib/cmake/llvm/LLVMConfig.cmake','lib/cmake/mlir/MLIRConfig.cmake']
configuration={}
for name in files:
    p=llvm/name;configuration[name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size}
report={'passed':any(item['probe_passed'] for item in reports),'inspection_completed':True,'recorded_at':now(),'images':reports,'llvm_source':str(llvm),'llvm_configuration':configuration,
        'scope':'Read-only prerequisites; not a full compiler build, NPU initialization or acceptance',
        'npu_kernel_launches':0,'source_worktrees_modified':False,'existing_containers_modified':False}
atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/full-build-prerequisites.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

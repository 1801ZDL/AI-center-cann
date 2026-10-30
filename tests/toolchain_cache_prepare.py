"""Queue-only immutable toolchain inspection and private cache-test image setup."""
import argparse
import json
import os
import hashlib
from pathlib import Path
import subprocess
import sys
import uuid
import shutil

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import read_json,atomic_json,now
from docker_runtime import discover,command


def build_image(job,binding):
    base=ROOT/'runtime/toolchain-cache-20261030';context=base/'native-image';context.mkdir(mode=0o700)
    image='sha256:820dc486ebd3931addfaa9cf867a965534b15b78351d945a7119cb8bf3cfcf57'
    (context/'native-env.sh').write_text('source /usr/local/Ascend/ascend-toolkit/set_env.sh\nexport PATH=/usr/local/python3.11.15/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n')
    (context/'Dockerfile').write_text('FROM '+image+'\nUSER 0\nLABEL io.ascend-lab.toolchain-cache="20261030-native"\n'
        'RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ccache ninja-build python3 passwd && rm -rf /var/lib/apt/lists/*\n'
        'COPY native-env.sh /opt/ascend-lab-native-env.sh\n'
        'RUN chmod 644 /opt/ascend-lab-native-env.sh && ccache --version && /usr/bin/python3 -c "import sqlite3,ssl;print(sqlite3.sqlite_version)"\n')
    target=base/'native-image-id';assert not target.exists()
    argv,env=command(binding,['docker','build','--pull=false','--network','default','--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),
        '--memory','1024m','--memory-swap','1024m','--iidfile',str(target),str(context)])
    env['DOCKER_BUILDKIT']='0'
    with (job/'results/native-image-build.log').open('wb') as output:
        result=subprocess.run(argv,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=300)
    if result.returncode:raise RuntimeError('Private native toolchain image build failed; original image unchanged, inspect the archived build log')
    report={'passed':True,'recorded_at':now(),'image':target.read_text().strip(),'base_image':image,
        'dockerfile_sha256':hashlib.sha256((context/'Dockerfile').read_bytes()).hexdigest(),
        'setup_sha256':hashlib.sha256((context/'native-env.sh').read_bytes()).hexdigest(),
        'scope':'New private immutable image adds ccache/Ninja and system Python for service roles; not compiler-cache, NPU or managed workflow acceptance',
        'existing_images_modified':False,'existing_containers_modified':False}
    atomic_json(base/'native-image.json',report);atomic_json(job/'results/native-image.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


def compiler_image(job,binding):
    base=ROOT/'runtime/toolchain-cache-20261030';context=base/'compiler-image';context.mkdir(mode=0o700)
    source=next((ROOT/'runtime/jobs/job-7504b588b164/results').glob('*.whl'))
    def sha(path):
        h=hashlib.sha256()
        with path.open('rb') as f:
            for b in iter(lambda:f.read(1024**2),b''):h.update(b)
        return h.hexdigest()
    old=read_json(ROOT/'evidence/full-build-live-validation.json');assert sha(source)==old['wheel_sha256']
    wheel=context/source.name;shutil.copyfile(source,wheel);assert sha(wheel)==old['wheel_sha256']
    setup='source /usr/local/Ascend/ascend-toolkit/set_env.sh\nexport PATH=/usr/local/python3.11.15/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
    (context/'native-env.sh').write_text(setup)
    cann='sha256:820dc486ebd3931addfaa9cf867a965534b15b78351d945a7119cb8bf3cfcf57'
    host='sha256:1fe13f7f9afa32d7ec13ec9ba42b38f7ed05cfd284e19e705ee54ca992162704'
    (context/'Dockerfile').write_text('FROM '+cann+' AS cann_source\nFROM '+host+'\nUSER 0\nLABEL io.ascend-lab.toolchain-cache="20261030-compiled"\n'
        'COPY --from=cann_source /usr/local/Ascend /usr/local/Ascend\n'
        'RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ccache ninja-build cmake python3 passwd && rm -rf /var/lib/apt/lists/*\n'
        'COPY '+wheel.name+' /opt/lab-wheel/'+wheel.name+'\n'
        'RUN /usr/local/python3.11.15/bin/python3.11 -m pip --isolated uninstall -y triton && /usr/local/python3.11.15/bin/python3.11 -m pip --isolated install --no-index --no-deps /opt/lab-wheel/'+wheel.name+'\n'
        'COPY native-env.sh /opt/ascend-lab-native-env.sh\n'
        'RUN chmod 644 /opt/ascend-lab-native-env.sh && /usr/local/python3.11.15/bin/python3.11 -I -c "import triton;from triton.backends.ascend.compiler import AscendBackend;print(triton.__version__,triton.__file__)"\n')
    target=base/'compiler-image-id';assert not target.exists()
    argv,env=command(binding,['docker','build','--pull=false','--network','default','--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),
        '--memory','1024m','--memory-swap','1024m','--iidfile',str(target),str(context)])
    env['DOCKER_BUILDKIT']='0'
    with (job/'results/compiler-image-build.log').open('wb') as output:
        r=subprocess.run(argv,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=480)
    if r.returncode:raise RuntimeError('Private accepted-wheel image failed; inspect archived build log, original images/wheel unchanged')
    report={'passed':True,'recorded_at':now(),'image':target.read_text().strip(),'host_base':host,'cann_source':cann,
        'dockerfile_sha256':sha(context/'Dockerfile'),'setup_sha256':sha(context/'native-env.sh'),
        'accepted_wheel_job':old['job_id'],'accepted_wheel_sha256':sha(wheel),'wheel_rebuilt':False,
        'scope':'Private CPU toolchain image combines glibc-compatible runtime, CANN and previously accepted Ascend wheel; not new source build or cache/device acceptance'}
    atomic_json(base/'compiler-image.json',report);atomic_json(job/'results/compiler-image.json',report);print(json.dumps(report,indent=2))


def runtime_image(job,binding):
    base=ROOT/'runtime/toolchain-cache-20261030';previous=read_json(base/'compiler-image.json');assert previous['passed']
    context=base/'runtime-image';context.mkdir(mode=0o700)
    # Prepend the task interpreter while retaining the tool paths established
    # by CANN. Do not replace its PATH with just the system directories.
    setup='source /usr/local/Ascend/ascend-toolkit/set_env.sh\nexport PATH=/usr/local/python3.11.15/bin:$PATH\n'
    (context/'native-env.sh').write_text(setup)
    (context/'Dockerfile').write_text('FROM '+previous['image']+'\nUSER 0\nLABEL io.ascend-lab.toolchain-cache="20261030-runtime"\n'
        'COPY native-env.sh /opt/ascend-lab-native-env.sh\n'
        'RUN chmod 644 /opt/ascend-lab-native-env.sh && /bin/bash -c "source /opt/ascend-lab-native-env.sh && command -v bishengir-compile && command -v ccache && python3.11 -I -c \'from triton.backends.ascend.compiler import AscendBackend;print(AscendBackend.__module__)\'"\n')
    target=base/'runtime-image-id';assert not target.exists()
    argv,env=command(binding,['docker','build','--pull=false','--network','none','--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),
        '--memory','512m','--memory-swap','512m','--iidfile',str(target),str(context)])
    env['DOCKER_BUILDKIT']='0'
    with (job/'results/runtime-image-build.log').open('wb') as output:r=subprocess.run(argv,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=90)
    if r.returncode:raise RuntimeError('Private task environment finalization failed; original images unaffected')
    report={'passed':True,'recorded_at':now(),'image':target.read_text().strip(),'previous_image':previous['image'],
        'setup_sha256':hashlib.sha256(setup.encode()).hexdigest(),'dockerfile_sha256':hashlib.sha256((context/'Dockerfile').read_bytes()).hexdigest(),
        'accepted_wheel_sha256':previous['accepted_wheel_sha256'],'wheel_rebuilt':False,'tool_path_preserved':True,
        'scope':'Fixed CPU task environment verified to resolve CANN tools, ccache and Ascend backend; not compiler-cache or device acceptance'}
    atomic_json(base/'runtime-image.json',report);atomic_json(job/'results/runtime-image.json',report);print(json.dumps(report,indent=2))


def offline_image(job,binding):
    base=ROOT/'runtime/toolchain-cache-20261030';previous=read_json(base/'runtime-image.json');assert previous['passed']
    context=base/'offline-image';context.mkdir(mode=0o700)
    patch='''import hashlib,json,os,subprocess
from pathlib import Path
import triton.backends.ascend.driver as driver
import triton.backends.ascend.compiler as compiler
from triton.backends.ascend.utils import _get_npucompiler_path,is_cann_version_at_least
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
d=Path(driver.__file__);c=Path(compiler.__file__);before={'driver':sha(d),'compiler':sha(c)}
source=d.read_text();needle='    def has_device_limit(self):\\n        num_aic, num_aiv = self.get_device_core()'
assert source.count(needle)==1
replacement='''+repr('    def has_device_limit(self):\n        if os.getenv("TRITON_COMPILE_ONLY", "").lower() in {"1", "true"}:\n            if os.getenv("NPU_DEVICE_LIMIT") is not None:\n                raise ValueError("Offline compile cannot validate NPU_DEVICE_LIMIT; use a verified device stage")\n            return False\n        num_aic, num_aiv = self.get_device_core()')+'''
d.write_text(source.replace(needle,replacement))
tool,env=_get_npucompiler_path();helped=subprocess.run([tool,'--help-hidden'],env=env,capture_output=True,text=True,timeout=30)
assert helped.returncode==0
supports='enable-optimized-metaop' in helped.stdout
removed=False
if not supports:
 assert not is_cann_version_at_least(9,2,0),'Never omit a requested true optimization flag'
 source=c.read_text();dynamic='        _compile_option_list += [f"--enable-optimized-metaop={is_cann_version_at_least(9, 2, 0)}"]';fixed='        _compile_option_list += [f"--enable-optimized-metaop=false"]'
 assert source.count(dynamic)==1 and source.count(fixed)==1
 c.write_text(source.replace(dynamic,'        # Private CANN9.1 compatibility: unsupported disabled metaop flag omitted.').replace(fixed,'        # Private CANN9.1 compatibility: unsupported disabled metaop flag omitted.'));removed=True
report={'format':'ascend-lab-offline-compat/v1','before':before,'after':{'driver':sha(d),'compiler':sha(c)},'tool':str(Path(tool).resolve()),'tool_sha256':sha(Path(tool)),'metaop_option_supported':supports,'omitted_only_false_metaop_option':removed,'offline_no_device_limit_query':True,'offline_explicit_limit_rejected':True,'normal_online_branch_retained':True,'original_wheel_modified':False}
Path('/opt/ascend-lab-offline-compat.json').write_text(json.dumps(report,indent=2)+'\\n')
'''
    (context/'patch.py').write_text(patch)
    check='''import os
from triton.backends.ascend.driver import NPUUtils
def queried(self):raise RuntimeError('normal-online-query')
NPUUtils.get_device_core=queried;obj=object.__new__(NPUUtils)
os.environ['TRITON_COMPILE_ONLY']='1';os.environ.pop('NPU_DEVICE_LIMIT',None)
assert obj.has_device_limit() is False
os.environ['NPU_DEVICE_LIMIT']='1,2'
try:obj.has_device_limit();raise AssertionError('offline limit silently accepted')
except ValueError:pass
os.environ.pop('NPU_DEVICE_LIMIT');os.environ['TRITON_COMPILE_ONLY']='0'
try:obj.has_device_limit();raise AssertionError('normal branch skipped')
except RuntimeError as error:assert str(error)=='normal-online-query'
print('offline absent limit, explicit limit refusal and original online query branch verified')
'''
    (context/'check.py').write_text(check)
    (context/'Dockerfile').write_text('FROM '+previous['image']+'\nUSER 0\nLABEL io.ascend-lab.toolchain-cache="20261030-offline"\nCOPY patch.py check.py /opt/lab-offline-compat/\n'
        'RUN /bin/bash -c "source /opt/ascend-lab-native-env.sh && python3.11 -I /opt/lab-offline-compat/patch.py && python3.11 -I /opt/lab-offline-compat/check.py"\n')
    target=base/'offline-image-id';assert not target.exists()
    argv,env=command(binding,['docker','build','--pull=false','--network','none','--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),
        '--memory','1024m','--memory-swap','1024m','--iidfile',str(target),str(context)])
    env['DOCKER_BUILDKIT']='0'
    with (job/'results/offline-image-build.log').open('wb') as output:r=subprocess.run(argv,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=120)
    if r.returncode:raise RuntimeError('Private offline compiler compatibility failed; original wheel/images unchanged')
    report={'passed':True,'recorded_at':now(),'image':target.read_text().strip(),'previous_image':previous['image'],
        'patch_script_sha256':hashlib.sha256(patch.encode()).hexdigest(),'check_script_sha256':hashlib.sha256(check.encode()).hexdigest(),
        'accepted_wheel_sha256':previous['accepted_wheel_sha256'],'original_wheel_modified':False,'wheel_rebuilt':False,
        'scope':'Explicit private CPU-only device-query guard and measured CANN9.1 disabled-option compatibility; actual DSL/ELF/cache acceptance still required'}
    atomic_json(base/'offline-image.json',report);atomic_json(job/'results/offline-image.json',report);print(json.dumps(report,indent=2))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['inspect','image','compiler-image','runtime-image','offline-image']);parser.add_argument('--image',choices=['builder','cann','stable','hostcann'],default='builder');args=parser.parse_args()
    job=Path(os.environ['LAB_JOB_DIR']);dispatch=read_json(job/'dispatch.json');assert dispatch['resources']['npu']==0
    binding,_=discover()
    if args.phase=='image':return build_image(job,binding)
    if args.phase=='compiler-image':return compiler_image(job,binding)
    if args.phase=='runtime-image':return runtime_image(job,binding)
    if args.phase=='offline-image':return offline_image(job,binding)
    images={'builder':(ROOT/'runtime/full-build-20261030/pybind-image-id').read_text().strip(),
        'cann':'sha256:118386a81427e5a77d2dbb8a7cabe81a2159d0cc3dca8c4bcb2bed72d3fb99f7',
        'stable':'sha256:820dc486ebd3931addfaa9cf867a965534b15b78351d945a7119cb8bf3cfcf57',
        'hostcann':'sha256:1fe13f7f9afa32d7ec13ec9ba42b38f7ed05cfd284e19e705ee54ca992162704'}
    image=images[args.image]
    label='toolchain-cache-'+uuid.uuid4().hex[:12]
    code='''import importlib.metadata as md,json,os,platform,shutil,subprocess,sys
from pathlib import Path
packages={}
for name in ['triton-ascend','triton','torch','torch-npu','numpy','ccache','cmake','ninja','pybind11']:
 try:packages[name]=md.version(name)
 except md.PackageNotFoundError:packages[name]=None
tools={}
for name in ['ccache','cmake','ninja','c++','bishengir-compile','bishengir-opt','ccec']:
 p=shutil.which(name)
 if p:tools[name]={'path':p,'realpath':str(Path(p).resolve())}
print(json.dumps({'uid':os.getuid(),'python':platform.python_version(),'executable':sys.executable,'packages':packages,'tools':tools,'npu_devices':list(map(str,Path('/dev').glob('davinci*'))),'setup_files':{p:Path(p).is_file() for p in ['/opt/lab-build-env.sh','/usr/local/Ascend/ascend-toolkit/set_env.sh','/opt/lab-bishengir/bin/bishengir-compile']}}))
'''
    argv,env=command(binding,['docker','run','--rm','--pull','never','--label','io.ascend-lab.toolchain-cache='+label,
        '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--user','23811:23811','--memory','512m','--memory-swap','512m','--cpus','0.5',
        '--cpuset-cpus',str(min(os.sched_getaffinity(0))),'--pids-limit','32',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777',
        '--entrypoint','/bin/bash',image,'--noprofile','--norc','-c',
        'for setup in /opt/lab-build-env.sh /usr/local/Ascend/ascend-toolkit/set_env.sh; do if [ -f "$setup" ]; then source "$setup" >/dev/null 2>&1 || exit 126; fi; done; if [ -x /opt/lab-build-venv/bin/python ]; then task_python=/opt/lab-build-venv/bin/python; else task_python=$(command -v python3.11 || command -v python3); fi; exec "$task_python" -I -B -c "$1"','cache-inspect',code])
    result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
    if result.returncode:
        atomic_json(job/'results/toolchain-inspection-failure.json',{'status':result.returncode,'stderr':result.stderr[-3000:]})
        raise RuntimeError('Owned read-only toolchain inspection failed')
    report={'passed':True,'recorded_at':now(),'image':image,**json.loads(result.stdout),'npu_launches':0,'readonly':True}
    atomic_json(job/'results/toolchain-inspection.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

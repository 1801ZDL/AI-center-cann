"""Add missing standard LLVM runtime libraries to a new private image layer."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,read_json,now
from docker_runtime import discover,command
assert os.environ.get('LAB_JOB_DIR')
base=ROOT/'runtime/full-build-20261030';state=read_json(base/'control.json')
context=base/'runtime-library-layer';context.mkdir(mode=0o700)
(context/'audit.py').write_text('''import json,subprocess
from pathlib import Path
items=[]
for prefix,names in [('/opt/lab-llvm/bin',['ld.lld','llvm-config','llvm-tblgen','mlir-tblgen']),('/opt/lab-bishengir/bin',['bishengir-compile','bishengir-opt','hivmc'])]:
 for name in names:
  p=Path(prefix)/name
  assert p.is_file(),str(p)
  r=subprocess.run(['ldd',str(p)],capture_output=True,text=True,timeout=15)
  assert 'not found' not in r.stdout+r.stderr,(name,r.stdout,r.stderr)
  items.append({'file':str(p),'ldd':r.stdout+r.stderr})
Path('/opt/lab-toolchain-runtime-audit.json').write_text(json.dumps({'passed':True,'dependencies':items},indent=2))
''')
(context/'audit.py').chmod(0o644)
(context/'Dockerfile').write_text('FROM '+state['image']+'\nLABEL io.ascend-lab.fullbuild="20261030-runtime"\n'
 'RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libtinfo6 libedit2 libffi8 zlib1g libzstd1 libncurses6 libncursesw6 libz3-4 libpfm4 && rm -rf /var/lib/apt/lists/*\n'
 'COPY audit.py /opt/lab-toolchain-runtime-audit.py\nRUN /opt/lab-build-venv/bin/python -I /opt/lab-toolchain-runtime-audit.py && chmod 644 /opt/lab-toolchain-runtime-audit.json\n')
binding,_=discover();argv,env=command(binding,['docker','build','--network','default','--pull=false','--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),
 '--memory','768m','--memory-swap','768m','--iidfile',str(base/'runtime-image-id'),str(context)])
env['DOCKER_BUILDKIT']='0';subprocess.run(argv,env=env,check=True,timeout=300)
image=(base/'runtime-image-id').read_text().strip()
atomic_json(base/'runtime-image.json',{'image':image,'previous_image':state['image'],'recorded_at':now(),'passed':True})
atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/compiler-runtime-image.json',{'passed':True,'image':image,'previous_image':state['image'],
 'scope':'Immutable standard-runtime library dependency fix and ldd audit; not a build or device acceptance'})
print(image,flush=True)

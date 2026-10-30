"""New offline image: accepted full compiler SDK plus existing ccache binary."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import read_json,atomic_json,now
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR')
base=ROOT/'runtime/quota-build-20261030';facts=read_json(base/'prerequisites.json');assert facts['passed']
sdk=facts['images'][0]['image'];cache=facts['images'][1]['image'];context=base/'image-layer';context.mkdir(mode=0o700)
audit=r'''import hashlib,json,subprocess
from pathlib import Path
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
v={'ccache_version':subprocess.check_output(['ccache','--version'],text=True).splitlines()[0],
 'ccache_sha256':sha('/usr/local/bin/ccache'),'hiredis_sha256':sha('/usr/local/lib/libhiredis.so.1.1.0'),
 'ldd':subprocess.check_output(['ldd','/usr/local/bin/ccache'],text=True),
 'original_audits':{n:sha('/opt/'+n) for n in ['lab-pybind-import-audit.json','lab-toolchain-link-audit.json','lab-llvm-manifest.json','lab-bishengir-manifest.json']}}
assert 'not found' not in v['ldd'] and v['ccache_version']=='ccache version 4.9.1'
Path('/opt/lab-ccache-audit.json').write_text(json.dumps(v,indent=2)+'\n')
'''
(context/'audit.py').write_text(audit);(context/'audit.py').chmod(0o644)
(context/'Dockerfile').write_text('FROM '+cache+' AS cachetool\nFROM '+sdk+'\n'
 'LABEL io.ascend-lab.quota-build="20261030-cache-builder"\n'
 'COPY --from=cachetool /usr/bin/ccache /usr/local/bin/ccache\n'
 'COPY --from=cachetool /lib/x86_64-linux-gnu/libhiredis.so.1.1.0 /usr/local/lib/libhiredis.so.1.1.0\n'
 'COPY audit.py /opt/lab-ccache-audit.py\n'
 'RUN ldconfig && /opt/lab-build-venv/bin/python -I /opt/lab-ccache-audit.py && chmod 644 /opt/lab-ccache-audit.json\n')
binding,_=discover();argv,env=command(binding,['docker','build','--network','none','--pull=false',
 '--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),'--memory','384m','--memory-swap','384m',
 '--iidfile',str(base/'image-id'),str(context)])
env['DOCKER_BUILDKIT']='0';subprocess.run(argv,env=env,check=True,timeout=180)
image=(base/'image-id').read_text().strip();assert image.startswith('sha256:')
report={'passed':True,'recorded_at':now(),'image':image,'base_image':sdk,'ccache_source_image':cache,
 'scope':'New immutable offline ccache-enabled full compiler SDK image; existing LLVM/BishengIR and pybind audits retained; not yet a full build acceptance',
 'npu_kernel_launches':0}
atomic_json(base/'image.json',report);atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/quota-build-image.json',report)
print(json.dumps(report))

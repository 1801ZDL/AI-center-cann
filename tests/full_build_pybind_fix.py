"""Queue-only exact pybind version and reentrant module initialization audit.

Reproduce the observed 2.13 failure and require 3.0.2 to pass before a rebuild.
Only a new private image is modified; original toolchains/installations stay put.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import atomic_json,read_json,now
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR')
base=ROOT/'runtime/full-build-20261030';state=read_json(base/'control.json')
context=base/'pybind-layer';context.mkdir(mode=0o700)
deps=context/'dependencies';deps.mkdir(mode=0o755)
subprocess.run([str(ROOT/'.qa-venv/bin/python'),'-I','-m','pip','--isolated','download',
               '--index-url','https://pypi.org/simple','--only-binary=:all:','--no-deps','--dest',str(deps),
               'pybind11==3.0.2'],check=True,timeout=90)
wheel=next(deps.glob('pybind11-3.0.2-*.whl'));checksum=hashlib.sha256(wheel.read_bytes()).hexdigest()
atomic_json(context/'dependency.json',{'name':wheel.name,'sha256':checksum,'version':'3.0.2'});(context/'dependency.json').chmod(0o644)
audit=r'''import hashlib,json,subprocess,sys,sysconfig,tempfile
from pathlib import Path
import pybind11
assert pybind11.__version__=='3.0.2'
source=r"""#include <pybind11/pybind11.h>
namespace py=pybind11;
enum class Padding { ZERO };
PYBIND11_MODULE(lab_reentrant,m) {
 py::enum_<Padding>(m,"PADDING_OPTION",py::module_local()).value("ZERO",Padding::ZERO);
 auto ascend=m.def_submodule("ascend");
 auto parent=py::module_::import("lab_reentrant");
 m.attr("nested_import_same_object")=py::bool_(parent.ptr()==m.ptr());
}
"""
observations=[]
with tempfile.TemporaryDirectory(prefix='lab-pybind-audit-') as tmp:
 root=Path(tmp);cpp=root/'probe.cpp';cpp.write_text(source)
 for version,include in [('2.13.6','/opt/lab-pybind-v213-headers'),('3.0.2',pybind11.get_include())]:
  folder=root/('v'+version);folder.mkdir();module=folder/('lab_reentrant'+sysconfig.get_config_var('EXT_SUFFIX'))
  argv=['c++','-O0','-shared','-fPIC','-std=c++17','-I'+include,'-I'+sysconfig.get_path('include'),str(cpp),'-o',str(module)]
  subprocess.run(argv,check=True,timeout=90)
  code='import sys;sys.path.insert(0,sys.argv[1]);import lab_reentrant;assert lab_reentrant.nested_import_same_object;print("nested import uses same module")'
  result=subprocess.run([sys.executable,'-I','-c',code,str(folder)],capture_output=True,text=True,timeout=15)
  observations.append({'pybind_version':version,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'module_sha256':hashlib.sha256(module.read_bytes()).hexdigest()})
 assert observations[0]['exit_code']!=0 and 'PADDING_OPTION' in observations[0]['stderr'],observations
 assert observations[1]['exit_code']==0,observations
 report={'passed':True,'baseline_failure_reproduced':True,'multiphase_version_passed':True,'selected_pybind_version':'3.0.2',
         'source_sha256':hashlib.sha256(source.encode()).hexdigest(),'observations':observations,
         'scope':'Minimal reentrant import reproducer only; full Triton rebuild/import remains required','npu_kernel_launches':0}
 Path('/opt/lab-pybind-import-audit.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report,indent=2),flush=True)
'''
(context/'audit.py').write_text(audit);(context/'audit.py').chmod(0o644)
(context/'Dockerfile').write_text('FROM '+state['image']+'\nLABEL io.ascend-lab.fullbuild="20261030-pybind"\n'
 'RUN cp -a /opt/lab-build-venv/lib/python3.11/site-packages/pybind11/include /opt/lab-pybind-v213-headers\n'
 'COPY dependencies /opt/lab-pybind-dependencies\nCOPY dependency.json /opt/lab-pybind-dependency.json\n'
 'RUN /opt/lab-build-venv/bin/python -m pip install --no-index --no-deps --upgrade /opt/lab-pybind-dependencies/'+wheel.name+'\n'
 'COPY audit.py /opt/lab-pybind-import-audit.py\nRUN /opt/lab-build-venv/bin/python -I /opt/lab-pybind-import-audit.py && chmod 644 /opt/lab-pybind-import-audit.json\n')
binding,_=discover();argv,env=command(binding,['docker','build','--network','none','--pull=false',
 '--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),'--memory','768m','--memory-swap','768m',
 '--iidfile',str(base/'pybind-image-id'),str(context)])
env['DOCKER_BUILDKIT']='0';subprocess.run(argv,env=env,check=True,timeout=240)
image=(base/'pybind-image-id').read_text().strip()
report={'passed':True,'image':image,'previous_image':state['image'],'recorded_at':now(),
        'pybind_version':'3.0.2','pybind_wheel_sha256':checksum,'development_link_audit':True,'pybind_import_audit':True,
        'scope':'New immutable builder and minimal 2.13/3.0 reentrant-import comparison; not full compiler acceptance'}
atomic_json(base/'runtime-image.json',report);atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/compiler-pybind-image.json',report)
print(json.dumps(report,indent=2),flush=True)

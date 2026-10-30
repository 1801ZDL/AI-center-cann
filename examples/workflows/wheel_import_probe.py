"""Offline IR smoke and import diagnostics of a preserved, newly built wheel.

No compiler rebuild, package modification, shared install, network or NPU.
IR success does not certify the Python runtime; import outcomes stay separate.
"""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

root=Path(os.environ['LAB_JOB_DIR']);results=root/'results';results.mkdir(exist_ok=True)
ir=root/'ir';ir.mkdir(exist_ok=True)
wheel=next(Path.cwd().glob('*.whl'));origin=json.loads(Path('producer.json').read_text())
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):h.update(data)
    return h.hexdigest()
assert digest(wheel)==origin['wheel_sha256']
records=0;native_sha=None
with zipfile.ZipFile(wheel) as z:
    record=next(name for name in z.namelist() if name.endswith('.dist-info/RECORD'))
    for name,checksum,size in csv.reader(io.StringIO(z.read(record).decode())):
        if not checksum:continue
        algorithm,expected=checksum.split('=',1);assert algorithm=='sha256'
        value=hashlib.sha256();total=0
        with z.open(name) as stream:
            for data in iter(lambda:stream.read(1024*1024),b''):value.update(data);total+=len(data)
        assert total==int(size) and base64.urlsafe_b64encode(value.digest()).decode().rstrip('=')==expected
        if name=='triton/_C/libtriton.so':native_sha=value.hexdigest()
        records+=1
assert native_sha and records>1000
text=Path('producer-build.log').read_text(errors='replace')
steps=[int(value) for value in re.findall(r'\[(\d+)/858\]',text)]
assert steps and max(steps)>=800 and 'Successfully installed triton-ascend' in text
target=root/'probe-install'
install=subprocess.run([sys.executable,'-m','pip','--isolated','install','--no-index','--no-deps','--target',target,wheel],
                       capture_output=True,text=True,timeout=150)
(results/'probe-install.log').write_text(install.stdout+'\n'+install.stderr)
assert install.returncode==0
extension=target/'triton/_C/libtriton.so';assert digest(extension)==native_sha

normal=r'''import json,sys
print(json.dumps({'initial_triton_modules':[n for n in sys.modules if n.startswith('triton')]}),flush=True)
sys.path.insert(0,sys.argv[1])
import triton
from triton._C import libtriton
print(json.dumps({'triton':triton.__file__,'extension':libtriton.__file__,'has_ascend':hasattr(libtriton,'ascend')}))
'''
trace=r'''import json,sys,traceback
sys.path.insert(0,sys.argv[1]);seen=0
def observe(frame,event,arg):
 global seen
 if event=='exception' and isinstance(arg[1],ImportError) and seen<64:
  seen+=1;print(json.dumps({'event':'first_chance_import_error','type':type(arg[1]).__name__,'message':str(arg[1])[:500],'file':frame.f_code.co_filename,'line':frame.f_lineno}),flush=True)
 return observe
sys.settrace(observe)
import triton
print(json.dumps({'import_passed':True,'triton':triton.__file__}))
'''
direct=r'''import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('libtriton',sys.argv[1]+'/triton/_C/libtriton.so')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
print(json.dumps({'direct_native_init':True,'has_ir':hasattr(mod,'ir'),'has_ascend':hasattr(mod,'ascend')}))
'''
observations=[]
for name,script in [('normal',normal),('trace',trace),('direct',direct)]:
    value=subprocess.run([sys.executable,'-I','-c',script,str(target)],cwd=root,capture_output=True,text=True,timeout=45)
    (results/(name+'-stdout.log')).write_text(value.stdout);(results/(name+'-stderr.log')).write_text(value.stderr)
    observations.append({'mode':name,'exit_code':value.returncode,'stdout':'results/'+name+'-stdout.log','stderr':'results/'+name+'-stderr.log'})
tool=target/'triton/_C/triton-opt';assert tool.is_file()
source=ir/'wheel-smoke.mlir';out=ir/'wheel-smoke-output.mlir'
source.write_text('module { func.func @fold() -> i32 { %a = arith.constant 20 : i32\n%b = arith.constant 22 : i32\n%c = arith.addi %a, %b : i32\nreturn %c : i32 } }\n')
value=subprocess.run([tool,'--canonicalize','--cse',source,'-o',out],cwd=root,capture_output=True,text=True,timeout=45)
(results/'wheel-smoke.log').write_text(value.stdout+'\n'+value.stderr)
assert value.returncode==0 and 'arith.constant 42' in out.read_text() and 'arith.addi' not in out.read_text()
report={'schema':'ascend-lab-result/v1','kind':'ir','passed':True,
        'producer_job_id':origin['producer_job_id'],'producer_state':'failed','source_commit':origin['source_commit'],
        'producer_failure_stage':'Python import after completed clean source build, wheel RECORD check and pip install',
        'wheel_sha256':digest(wheel),'wheel_bytes':wheel.stat().st_size,'new_extension_sha256':native_sha,
        'wheel_record_files_verified':records,'producer_build_log_sha256':digest(Path('producer-build.log')),
        'producer_ninja_progress_peak':max(steps),'uid':os.getuid(),'npu_kernel_launches':0,'compiler_rebuilt':False,
        'constant_folded_value':42,'import_observations':observations,
        'wheel_runtime_accepted':all(o['exit_code']==0 for o in observations),
        'scope':'Actual preserved-wheel offline triton-opt IR transform and import diagnostics. IR workflow success does not certify Python wheel runtime or NPU execution.',
        'artifacts':['ir/wheel-smoke-output.mlir']}
(results/'wheel-import-observations.json').write_text(json.dumps(report,indent=2)+'\n')
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2),flush=True)

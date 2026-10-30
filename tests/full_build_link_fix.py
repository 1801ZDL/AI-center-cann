"""Queue-only immutable builder development libraries and real SDK link audit."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import atomic_json,read_json,now
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR'), 'Submit through the existing queue'
base=ROOT/'runtime/full-build-20261030';state=read_json(base/'control.json')
context=base/'link-library-layer';context.mkdir(mode=0o700)
audit=r'''import hashlib,json,shlex,subprocess,tempfile
from pathlib import Path
config='/opt/lab-llvm/bin/llvm-config'
def flags(*args):return shlex.split(subprocess.check_output([config,*args],text=True))
with tempfile.TemporaryDirectory(prefix='lab-sdk-link-') as tmp:
 root=Path(tmp);source=root/'probe.cpp';exe=root/'probe'
 source.write_text('#include <llvm/AsmParser/Parser.h>\n#include <llvm/IR/LLVMContext.h>\n#include <llvm/IR/Module.h>\n#include <llvm/IR/Verifier.h>\n#include <llvm/Support/SourceMgr.h>\n#include <zlib.h>\nint main() { llvm::LLVMContext c; llvm::SMDiagnostic e; auto m=llvm::parseAssemblyString("define i32 @answer() { ret i32 42 }",e,c); return !m || llvm::verifyModule(*m) || !zlibVersion(); }\n')
 argv=['c++',*flags('--cxxflags'),str(source),'-o',str(exe),*flags('--ldflags','--link-static','--libs','asmparser','core','support','--system-libs'),'-lz']
 subprocess.run(argv,check=True,timeout=90)
 subprocess.run([str(exe)],check=True,timeout=10)
 packages=subprocess.check_output(['dpkg-query','-W','-f=${Package} ${Version}\n','zlib1g','zlib1g-dev','libxml2','libxml2-dev'],text=True)
 report={'passed':True,'compiler':'c++','llvm_static_link_and_execution':True,'explicit_zlib_link':True,'argv':argv,
         'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'executable_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
         'development_packages':packages.splitlines(),'npu_kernel_launches':0,'scope':'LLVM assembly parse/verify and explicit -lz static SDK link; not full Triton rebuild'}
 Path('/opt/lab-toolchain-link-audit.json').write_text(json.dumps(report,indent=2)+'\n')
'''
(context/'audit.py').write_text(audit);(context/'audit.py').chmod(0o644)
(context/'Dockerfile').write_text('FROM '+state['image']+'\nLABEL io.ascend-lab.fullbuild="20261030-link"\n'
 'RUN apt-get update && apt-get install -y --no-install-recommends zlib1g-dev libxml2-dev && rm -rf /var/lib/apt/lists/*\n'
 'COPY audit.py /opt/lab-toolchain-link-audit.py\nRUN /opt/lab-build-venv/bin/python -I /opt/lab-toolchain-link-audit.py && chmod 644 /opt/lab-toolchain-link-audit.json\n')
binding,_=discover();argv,env=command(binding,['docker','build','--network','default','--pull=false',
 '--cpuset-cpus',','.join(map(str,os.sched_getaffinity(0))),'--memory','1024m','--memory-swap','1024m',
 '--iidfile',str(base/'link-image-id'),str(context)])
env['DOCKER_BUILDKIT']='0';subprocess.run(argv,env=env,check=True,timeout=300)
image=(base/'link-image-id').read_text().strip()
report={'image':image,'previous_image':state['image'],'recorded_at':now(),'passed':True,'development_link_audit':True,
 'scope':'Immutable development dependency fix plus actual LLVM static link/execution; no full build or NPU acceptance'}
atomic_json(base/'runtime-image.json',report)
atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/compiler-link-image.json',report)
print(json.dumps(report,indent=2),flush=True)

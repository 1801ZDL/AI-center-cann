"""Rebuild a frozen Triton-Ascend source tree, verify the new wheel, no devices.

Inputs: source.tar.gz and source.json produced by the queue-only snapshot helper.
External LLVM/BishengIR dependencies are immutable image contents, not rebuilt.
"""
import base64
import argparse
import csv
import hashlib
import io
import json
import os
import posixpath
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

root=Path(os.environ['LAB_JOB_DIR']);results=root/'results';results.mkdir(exist_ok=True)
parser=argparse.ArgumentParser();parser.add_argument('--configure-only',action='store_true');options=parser.parse_args()
origin=json.loads(Path('source.json').read_text());archive=Path('source.tar.gz')
source=Path.cwd()/'source';log=results/'full-build.log';started=time.monotonic()

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()

def save(name,value):(results/name).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')

def run(argv,**kwargs):
    with log.open('ab') as output:
        output.write(('COMMAND '+json.dumps([str(x) for x in argv])+'\n').encode());output.flush()
        command=[str(x) for x in argv]
        with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0,**kwargs) as child:
            while True:
                block=os.read(child.stdout.fileno(),8192)
                if not block:break
                output.write(block);output.flush();sys.stdout.buffer.write(block);sys.stdout.buffer.flush()
            code=child.wait()
            if code:raise subprocess.CalledProcessError(code,command)
            return subprocess.CompletedProcess(command,code)

report={'schema':'ascend-lab-result/v1','kind':'build','passed':False,'source_commit':origin['upstream_commit'],
        'npuir_commit':origin['npuir_commit'],'source_archive_sha256':origin['archive_sha256'],
        'external_dependencies_rebuilt':False,'npu_kernel_launches':0,'uid':os.getuid(),
        'cpu_ids':sorted(os.sched_getaffinity(0)),'phase':'input_validation','artifacts':['results/full-build.log']}
try:
    assert os.getuid()>=1000 and len(os.sched_getaffinity(0))==1
    assert os.environ['CCACHE_DIR']=='/cache/ccache' and Path('/opt/lab-ccache-audit.json').is_file()
    link_audit=Path('/opt/lab-toolchain-link-audit.json')
    link_check=json.loads(link_audit.read_text())
    assert link_check['passed'] and link_check['llvm_static_link_and_execution'] and link_check['explicit_zlib_link']
    binding_audit=Path('/opt/lab-pybind-import-audit.json')
    binding_check=json.loads(binding_audit.read_text())
    assert binding_check['passed'] and binding_check['baseline_failure_reproduced'] and binding_check['multiphase_version_passed']
    assert digest(archive)==origin['archive_sha256'] and not source.exists()
    # The frozen archive is still validated before extraction into the task's own work tree.
    with tarfile.open(archive) as tar:
        members=tar.getmembers()
        assert len(members)<100000 and sum(m.size for m in members)<2*1024**3
        assert all(not m.name.startswith('/') and '..' not in Path(m.name).parts and Path(m.name).parts[0]=='source'
                   and (m.isfile() or m.isdir() or m.issym()) for m in members)
        assert all(not m.issym() or not m.linkname.startswith('/') and posixpath.normpath(str(Path(m.name).parent/m.linkname)).startswith('source/') for m in members)
    run(['tar','--no-same-owner','-xzf',archive,'-C',Path.cwd()])
    assert subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()==origin['upstream_commit']
    assert not list(source.glob('build/**/.ninja_log'))
    env=dict(os.environ)
    env.update(MAX_JOBS='1',CMAKE_BUILD_PARALLEL_LEVEL='1',CCACHE_BASEDIR=str(source),CCACHE_COMPILERCHECK='content',CCACHE_MAXSIZE='5G',LLVM_SYSPATH='/opt/lab-llvm',
               TRITON_ASCEND_BISHENGIR_PATH='/opt/lab-bishengir',TRITON_BUILD_WITH_CCACHE='ON',
               TRITON_BUILD_WITH_CLANG_LLD='OFF',TRITON_BUILD_PROTON='OFF',TRITON_BUILD_TD='OFF',
               TRITON_BUILD_NPUIR='OFF',TRITON_OFFLINE_BUILD='1',PYTHONNOUSERSITE='1',
               TRITON_APPEND_CMAKE_ARGS='-DTRITON_BUILD_UT=OFF -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_WERROR=OFF '
               '-DCMAKE_CXX_FLAGS=-Wno-error=free-nonheap-object '
               '-DCMAKE_LINKER=/opt/lab-llvm/bin/ld.lld -DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=lld '
               '-DCMAKE_MODULE_LINKER_FLAGS=-fuse-ld=lld')
    report['build_configuration']={'max_jobs':1,'ccache_audit_sha256':digest(Path('/opt/lab-ccache-audit.json')),'cmake_args':env['TRITON_APPEND_CMAKE_ARGS'],
                                   'warning_policy':'Keep diagnostics; only free-nonheap-object is not promoted to an error',
                                   'llvm_manifest_sha256':digest(Path('/opt/lab-llvm-manifest.json')),
                                   'development_link_audit_sha256':digest(link_audit),
                                   'pybind_import_audit_sha256':digest(binding_audit),
                                   'pybind_version':binding_check['selected_pybind_version'],
                                   'bishengir_manifest_sha256':digest(Path('/opt/lab-bishengir-manifest.json'))}
    print('Starting clean Triton-Ascend wheel compilation, MAX_JOBS=1, bounded persistent ccache, NPU=0.',flush=True)
    if options.configure_only:
        configure=r'''import json,runpy,subprocess,sys
from pathlib import Path
marker=Path(sys.argv[1]);original=subprocess.check_call
def call(cmd,*args,**kwargs):
 if isinstance(cmd,(list,tuple)) and cmd and str(cmd[0])=='cmake' and '--build' in cmd:
  marker.write_text(json.dumps({'intercepted_cmake_build':list(cmd),'scope':'Configuration only; no full build or wheel'}));return 0
 return original(cmd,*args,**kwargs)
subprocess.check_call=call;sys.argv=['setup.py','build_ext'];runpy.run_path('setup.py',run_name='__main__')
'''
        marker=results/'configure-only.json'
        run([sys.executable,'-c',configure,marker],cwd=source,env=env)
        assert marker.is_file() and not list(source.rglob('libtriton*.so'))
        caches=list(source.glob('build/**/CMakeCache.txt'));assert caches
        for index,path in enumerate(caches[:3]):shutil.copyfile(path,results/('CMakeCache-'+str(index)+'.txt'))
        report.update(passed=True,configure_only=True,full_build_accepted=False,elapsed_seconds=time.monotonic()-started,
                      scope='Actual frozen-source CMake configuration; intercepted build phase, no C++ rebuild, wheel or device acceptance')
        report['artifacts']=['results/full-build.log','results/configure-only.json','results/CMakeCache-0.txt']
        save('full-build-provenance.json',report);Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(report,indent=2)+'\n')
        print('Configuration-only prerequisite passed; full compilation remains unproven.',flush=True)
        raise SystemExit(0)
    report['phase']='source_to_wheel';save('full-build-provenance.json',report)
    report['source_to_wheel_started_unix']=time.time();save('full-build-provenance.json',report)
    run(['ccache','--zero-stats'],cwd=source,env=env)
    run([sys.executable,'setup.py','bdist_wheel','--dist-dir',results],cwd=source,env=env)
    report['source_to_wheel_finished_unix']=time.time()
    cache_output=subprocess.check_output(['ccache','--print-stats'],cwd=source,env=env,text=True)
    report['ccache_stats']={p[0]:int(p[1]) for line in cache_output.splitlines() if len(p:=line.split())==2}
    save('full-build-provenance.json',report)
    report['phase']='wheel_record_validation'
    wheels=list(results.glob('*.whl'));assert len(wheels)==1;wheel=wheels[0]
    record_count=0
    with zipfile.ZipFile(wheel) as z:
        record=next(n for n in z.namelist() if n.endswith('.dist-info/RECORD'))
        for name,checksum,size in csv.reader(io.StringIO(z.read(record).decode())):
            if not checksum:continue
            algorithm,expected=checksum.split('=',1);assert algorithm=='sha256'
            value=hashlib.sha256();total=0
            with z.open(name) as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):value.update(block);total+=len(block)
            assert total==int(size) and base64.urlsafe_b64encode(value.digest()).decode().rstrip('=')==expected
            record_count+=1
    logs=list(source.glob('build/**/.ninja_log'));assert logs
    entries=[line for p in logs for line in p.read_text().splitlines() if line and not line.startswith('#')]
    assert len(entries)>100
    compiled={digest(p) for p in source.rglob('libtriton*.so') if p.is_file()}
    assert compiled
    report.update(build_command_succeeded=True,wheel_file=wheel.name,wheel_bytes=wheel.stat().st_size,
                  wheel_sha256=digest(wheel),record_files_verified=record_count,ninja_entries=len(entries),
                  new_extension_candidates=sorted(compiled),phase='private_install')
    for index,path in enumerate(logs[:3]):shutil.copyfile(path,results/('ninja-log-'+str(index)+'.txt'))
    for index,path in enumerate(source.glob('build/**/CMakeCache.txt')):
        if index>=3:break
        shutil.copyfile(path,results/('CMakeCache-'+str(index)+'.txt'))
    save('full-build-provenance.json',report)
    target=root/'installed-wheel';run([sys.executable,'-m','pip','--isolated','install','--no-index','--no-deps','--target',target,wheel],cwd=root)
    verify=r'''import hashlib,json,sys
from pathlib import Path
target=Path(sys.argv[1]);sys.path.insert(0,str(target))
import triton
from triton._C import libtriton
assert Path(triton.__file__).resolve().is_relative_to(target)
assert Path(libtriton.__file__).resolve().is_relative_to(target)
assert hasattr(libtriton,'ascend') and hasattr(libtriton,'ir')
print(json.dumps({'triton_path':triton.__file__,'extension_path':libtriton.__file__,
 'extension_sha256':hashlib.sha256(Path(libtriton.__file__).read_bytes()).hexdigest(),'version':triton.__version__}))
'''
    report['phase']='python_import';save('full-build-provenance.json',report)
    verified=json.loads(subprocess.check_output([sys.executable,'-I','-c',verify,str(target)],cwd=root,text=True))
    assert verified['extension_sha256'] in compiled
    report.update(phase='offline_ir',new_extension_sha256=verified['extension_sha256'],installed_import=verified)
    save('full-build-provenance.json',report)
    tool=next(target.rglob('triton-opt'),None);assert tool and tool.is_file()
    ir=results/'build-smoke-input.mlir';canonical=results/'build-smoke-output.mlir'
    ir.write_text('module { func.func @fold() -> i32 { %a = arith.constant 20 : i32\n%b = arith.constant 22 : i32\n%c = arith.addi %a, %b : i32\nreturn %c : i32 } }\n')
    run([tool,'--canonicalize','--cse',ir,'-o',canonical],cwd=root)
    text=canonical.read_text();assert 'arith.constant 42' in text and 'arith.addi' not in text
    caches=list(source.glob('build/**/CMakeCache.txt'))
    for index,path in enumerate(caches[:3]):shutil.copyfile(path,results/('CMakeCache-'+str(index)+'.txt'))
    changes=subprocess.check_output(['git','-C',str(source),'diff','--binary'],text=False)
    report.update(passed=True,phase='complete',wheel_file=wheel.name,wheel_bytes=wheel.stat().st_size,wheel_sha256=digest(wheel),
                  record_files_verified=record_count,ninja_entries=len(entries),new_extension_sha256=verified['extension_sha256'],
                  installed_import=verified,compiled_ir_constant=42,elapsed_seconds=time.monotonic()-started,
                  build_generated_patch_sha256=hashlib.sha256(changes).hexdigest(),
                  scope='Clean source-to-wheel Triton-Ascend compilation and private install/new C++ import/IR transform; external LLVM/BishengIR reused, no NPU execution')
    report['artifacts']=[str(p.relative_to(root)) for p in [wheel,log,canonical]]
    save('full-build-provenance.json',report)
    report['artifacts'].append('results/full-build-provenance.json')
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':True,'wheel':wheel.name,'bytes':wheel.stat().st_size,'new_extension':verified['extension_sha256'],'npu_used':False}),flush=True)
except Exception as error:
    report.update(failed_phase=report['phase'],error=type(error).__name__+': '+str(error)[:700],elapsed_seconds=time.monotonic()-started)
    if log.is_file():
        errors=[]
        with log.open(errors='replace') as stream:
            for line in stream:
                if 'error:' in line or line.startswith('FAILED:'):
                    errors.append(line[:1500].rstrip())
                    if len(errors)>=20:break
        report['compiler_errors']=errors
    save('full-build-provenance.json',report)
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(report,indent=2)+'\n')
    print('Full compiler build did not pass; preserved log and provenance.',file=sys.stderr,flush=True)
    raise

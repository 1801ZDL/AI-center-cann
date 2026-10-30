"""Offline DSL-to-ELF and fresh CMake/ccache build in a managed CPU workflow.

Requires an explicitly certified private CPU compatibility image and persistent
cache. Build timings and cache hits are not NPU performance/accuracy results.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024**2),b''):h.update(part)
    return h.hexdigest()


def save(path,value):path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')


def main():
    global triton,tl
    parser=argparse.ArgumentParser();parser.add_argument('--source-variant',type=int,choices=[0,1],default=0);parser.add_argument('--hold',action='store_true');args=parser.parse_args()
    root=Path(os.environ['LAB_JOB_DIR']);results=root/'results';ir=root/'ir'
    assert os.getuid()>=1000 and os.environ['TRITON_CACHE_DIR']=='/cache/triton' and os.environ['CCACHE_DIR']=='/cache/ccache'
    proc={k:v.strip() for k,sep,v in (x.partition(':') for x in Path('/proc/self/status').read_text().splitlines()) if sep}
    assert proc['CapEff']==proc['CapPrm']=='0000000000000000' and proc['NoNewPrivs']=='1'
    assert not list(Path('/dev').glob('davinci*')) and not Path('/var/run/docker.sock').exists()
    audit=json.loads(Path('/opt/ascend-lab-offline-compat.json').read_text())
    assert audit['offline_no_device_limit_query'] and audit['offline_explicit_limit_rejected']
    assert os.environ.get('NPU_DEVICE_LIMIT') is None
    os.environ['TRITON_COMPILE_ONLY']='1'
    source=root/'native-source';build=root/'native-build';assert not source.exists() and not build.exists()
    source.mkdir(mode=0o700)
    (source/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(lab_native LANGUAGES CXX)\nadd_library(calc STATIC calc.cpp)\nadd_executable(answer main.cpp)\ntarget_link_libraries(answer PRIVATE calc)\n')
    (source/'calc.cpp').write_text('int calc(int a,int b){return a+b+'+str(args.source_variant)+';}\n')
    (source/'main.cpp').write_text('#include <cstdio>\nextern int calc(int,int);int main(){int n=calc(20,22);std::printf("%d\\n",n);return n=='+str(42+args.source_variant)+'?0:1;}\n')
    env=dict(os.environ);env.update(CCACHE_BASEDIR=str(source),CCACHE_COMPILERCHECK='content',CCACHE_MAXSIZE='24M')
    commands=[]
    def command(argv):
        try:p=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=45)
        except OSError as error:
            commands.append({'argv':argv,'errno':error.errno});save(results/'cmake-commands.json',commands);raise
        commands.append({'argv':argv,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr});save(results/'cmake-commands.json',commands)
        if p.returncode:raise RuntimeError('Native build command failed: '+p.stderr[-1500:])
        return p.stdout
    command(['cmake','-S',str(source),'-B',str(build),'-G','Ninja','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER_LAUNCHER=ccache'])
    command(['ccache','--zero-stats']);command(['cmake','--build',str(build),'--parallel',os.environ.get('MAX_JOBS','1')])
    answer=int(command([str(build/'answer')]).strip());assert answer==42+args.source_variant
    stats={parts[0]:int(parts[1]) for line in command(['ccache','--print-stats']).splitlines() if len(parts:=line.split())==2}
    cmake={'answer':answer,'fresh_build_directory':True,'cache_misses':stats.get('cache_miss',0),
        'cache_hits':sum(stats.get(k,0) for k in ['direct_cache_hit','preprocessed_cache_hit']),
        'ccache_version':command(['ccache','--version']).splitlines()[0],'stats':stats}
    shutil.copyfile(build/'answer',results/'cmake-answer')
    import triton
    import triton.language as tl
    import torch
    from triton.compiler.compiler import ASTSource,make_backend
    from triton.backends.compiler import GPUTarget
    device_queries=[]
    def forbidden(*a,**kw):
        device_queries.append(True);raise RuntimeError('Offline workflow attempted a live device query')
    torch.npu.current_device=forbidden;torch.npu.get_device_properties=forbidden
    target=GPUTarget('npu','Ascend950PR_9579',0);backend=type(make_backend(target));stages=[];original=backend.add_stages
    def counted(self,mapping,*a,**kw):
        original(self,mapping,*a,**kw)
        for name,fn in list(mapping.items()):
            def wrapped(*x,_name=name,_fn=fn,**y):stages.append(_name);return _fn(*x,**y)
            mapping[name]=wrapped
    backend.add_stages=counted;tools={};run=subprocess.run
    def observed(argv,*a,**kw):
        if isinstance(argv,(list,tuple)) and argv and isinstance(argv[0],(str,os.PathLike)):
            p=shutil.which(str(argv[0])) if not Path(argv[0]).is_absolute() else str(argv[0])
            if p and Path(p).is_file():
                p=str(Path(p).resolve())
                if p not in tools:tools[p]={'path':p,'sha256':sha(p)};save(results/'tool-invocations.json',list(tools.values()))
        return run(argv,*a,**kw)
    subprocess.run=observed
    @triton.jit
    def add(X,Y,Z,N:tl.constexpr,B:tl.constexpr,BIAS:tl.constexpr):
        i=tl.program_id(0)*B+tl.arange(0,B)
        x=tl.load(X+i,i<N,0);y=tl.load(Y+i,i<N,0)
        tl.store(Z+i,x+y+BIAS,i<N)
    def cache_files():
        return {str(p.relative_to('/cache/triton')):{'size':p.stat().st_size,'sha256':sha(p),'mtime_ns':p.stat().st_mtime_ns}
                for p in Path('/cache/triton').rglob('*') if p.is_file() and not p.is_symlink()}
    before=cache_files();started=time.monotonic()
    compiled=triton.compile(ASTSource(add,{'X':'*fp32','Y':'*fp32','Z':'*fp32'},constexprs={'N':64,'B':64,'BIAS':args.source_variant}),
        target=target,options={'enable_dynamic_cv_pipeline':False,'debug':False})
    seconds=time.monotonic()-started;after=cache_files();binary=compiled.asm['npubin']
    assert binary[:4]==b'\x7fELF' and compiled.module is None and compiled.function is None and not device_queries
    (results/'native-kernel.o').write_bytes(binary);asm={}
    for stage,value in compiled.asm.items():
        if stage=='npubin':continue
        path=ir/('kernel.'+stage);path.write_bytes(value if isinstance(value,bytes) else str(value).encode());asm[stage]=str(path.relative_to(root))
    save(results/'kernel-metadata.json',json.loads(json.dumps(compiled.metadata._asdict(),default=vars)))
    save(results/'kernel-manifest.json',{'schema':'ascend-lab-offline-kernel/v1','binary':'results/native-kernel.o','metadata':'results/kernel-metadata.json',
        'asm':asm,'hash':compiled.hash,'signature':{'X':'*fp32','Y':'*fp32','Z':'*fp32'},'constexprs':{'N':64,'B':64,'BIAS':args.source_variant},
        'target':vars(target),'source_sha256':sha(__file__),'binary_sha256':hashlib.sha256(binary).hexdigest()})
    save(results/'tool-invocations.json',list(tools.values()))
    report={'schema':'ascend-lab-result/v1','kind':'build','passed':True,'artifacts':['results/native-kernel.o','results/cmake-answer',
        'results/kernel-metadata.json','results/kernel-manifest.json','results/native-cache-report.json','results/cmake-commands.json','results/tool-invocations.json'],
        'source_variant':args.source_variant,'cmake':cmake,'triton_stages_called':stages,'actual_external_tools':list(tools.values()),
        'binary_bytes':len(binary),'binary_sha256':hashlib.sha256(binary).hexdigest(),'triton_cache_hit':not stages,
        'warm_triton_files_unchanged':bool(before) and before==after,'compiler_seconds':seconds,
        'backend_sha256':sha(sys.modules[backend.__module__].__file__),'backend_compatibility':audit,
        'uid':os.getuid(),'cap_effective':proc['CapEff'],'no_new_privileges':True,'device_queries':0,'kernel_loads':0,'kernel_launches':0,
        'scope':'Actual managed CPU offline build/cache evidence; CMake answer is CPU execution, no Triton numerical or NPU performance acceptance'}
    save(results/'native-cache-report.json',report);save(Path(os.environ['LAB_RESULT_FILE']),report)
    print(json.dumps({'offline_build':True,'triton_cache_hit':not stages,'cmake_hits':cmake['cache_hits'],'kernel_launches':0}),flush=True)
    if args.hold:time.sleep(20)


if __name__=='__main__':main()

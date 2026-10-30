"""Actual IR/CMake peers of the long compiler acceptance; no NPU access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

p=argparse.ArgumentParser();p.add_argument('mode',choices=['ir','cmake']);a=p.parse_args()
root=Path(os.environ['LAB_JOB_DIR']);out=root/'results';out.mkdir(exist_ok=True)
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert os.getuid()>=1000 and status['CapEff'].strip()=='0000000000000000' and status['NoNewPrivs'].strip()=='1'
assert len(os.sched_getaffinity(0))==1 and not Path('/var/run/docker.sock').exists()
report={'schema':'ascend-lab-result/v1','kind':'ir' if a.mode=='ir' else 'build',
        'passed':False,'uid':os.getuid(),'cap_effective':status['CapEff'].strip(),
        'no_new_privileges':True,'cpu_ids':sorted(os.sched_getaffinity(0)),
        'npu_kernel_launches':0,'mode':a.mode,'started_unix':time.time(),'artifacts':[]}
def run(argv,**kwargs):
    done=subprocess.run(list(map(str,argv)),check=True,capture_output=True,text=True,**kwargs)
    with (out/'peer.log').open('a') as f:f.write(done.stdout+done.stderr)
    return done.stdout
if a.mode=='ir':
    src=out/'input.mlir';dst=out/'output.mlir'
    src.write_text('module { func.func @fold() -> i32 { %a = arith.constant 20 : i32\n%b = arith.constant 22 : i32\n%c = arith.addi %a, %b : i32\nreturn %c : i32 } }\n')
    run(['/opt/lab-bishengir/bin/bishengir-opt','--canonicalize','--cse',src,'-o',dst])
    assert 'arith.constant 42' in dst.read_text() and 'arith.addi' not in dst.read_text()
    report.update(answer=42,artifacts=['results/input.mlir','results/output.mlir','results/peer.log'])
else:
    source=Path.cwd()/'small-source';build=Path.cwd()/'small-build'
    assert not source.exists() and not build.exists() and os.environ['CCACHE_DIR']=='/cache/ccache'
    source.mkdir()
    (source/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(peer LANGUAGES CXX)\nset(CMAKE_CXX_COMPILER_LAUNCHER ccache)\nadd_executable(answer main.cpp answer.cpp)\n')
    (source/'main.cpp').write_text('#include <iostream>\nextern int answer(); int main(){std::cout<<answer()<<"\\n";}\n')
    (source/'answer.cpp').write_text('int answer(){return 42;}\n')
    env={**os.environ,'CCACHE_BASEDIR':str(source),'CCACHE_COMPILERCHECK':'content','CCACHE_MAXSIZE':'16M'}
    run(['ccache','--zero-stats'],env=env)
    run(['cmake','-S',source,'-B',build,'-G','Ninja','-DCMAKE_BUILD_TYPE=Release'],env=env)
    run(['cmake','--build',build,'--parallel','1'],env=env)
    assert run([build/'answer']).strip()=='42'
    stats={row[0]:int(row[1]) for line in run(['ccache','--print-stats'],env=env).splitlines() if len(row:=line.split())==2}
    report.update(answer=42,ccache_stats=stats,binary_sha256=hashlib.sha256((build/'answer').read_bytes()).hexdigest(),
                  artifacts=['results/peer.log'])
report.update(passed=True,finished_unix=time.time())
(out/'peer-report.json').write_text(json.dumps(report,indent=2)+'\n')
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'passed':True,'mode':a.mode,'uid':os.getuid(),'answer':42}))

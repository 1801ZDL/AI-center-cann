"""CPU-only three-stage BishengIR pipeline for real artifact handoff checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['build','verify','report']);args=parser.parse_args()
root=Path(os.environ['LAB_JOB_DIR']);out=root/'results';ir=root/'ir'
out.mkdir(exist_ok=True);ir.mkdir(exist_ok=True)
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert os.getuid()>=1000 and status['CapEff'].strip()=='0000000000000000' and status['NoNewPrivs'].strip()=='1'
assert not Path('/var/run/docker.sock').exists()
if args.stage=='build':
    source=ir/'source.mlir'
    source.write_text('module { func.func @answer() -> i32 { %a = arith.constant 20 : i32\n%b = arith.constant 22 : i32\n%c = arith.addi %a, %b : i32\nreturn %c : i32 } }\n')
else:source=Path.cwd()/('upstream.mlir' if args.stage=='verify' else 'verified.mlir')
original=source.read_bytes();target=ir/'output.mlir'
if args.stage in {'build','verify'}:
    done=subprocess.run(['/opt/lab-bishengir/bin/bishengir-opt','--canonicalize','--cse',str(source),'-o',str(target)],capture_output=True,text=True,timeout=25)
    (out/'compiler.log').write_text(done.stdout+done.stderr);done.check_returncode()
else:target.write_bytes(original)
assert 'arith.constant 42' in target.read_text() and 'arith.addi' not in target.read_text()
report={'schema':'ascend-lab-result/v1','kind':'build' if args.stage=='build' else 'ir','passed':True,
    'uid':os.getuid(),'stage':args.stage,'answer':42,'input_sha256':hashlib.sha256(original).hexdigest(),
    'output_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'cap_effective':status['CapEff'].strip(),
    'no_new_privileges':True,'npu_kernel_launches':0,
    'artifacts':['results/graph-stage.json'] if args.stage=='build' else ['ir/output.mlir']}
(out/'graph-stage.json').write_text(json.dumps(report,indent=2)+'\n')
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'passed':True,'stage':args.stage,'uid':os.getuid(),'answer':42}))

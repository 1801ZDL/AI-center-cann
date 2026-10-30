"""Real offline IR cache example. Compiler input SHA selects the inner cache key."""
import hashlib
import json
import os
import errno
from pathlib import Path
import shutil
import subprocess
import sys
import time

root=Path(os.environ['LAB_JOB_DIR']);cache=Path(os.environ['TRITON_CACHE_DIR'])
source=Path('input.mlir');identity=hashlib.sha256(source.read_bytes()).hexdigest();entry=cache/identity
entry.mkdir(mode=0o700,exist_ok=True);output=entry/'canonical.mlir';counter=entry/'invocations.json'
hit=output.exists()
if hit:
    count=json.loads(counter.read_text())['calls'];assert count==1
else:
    tool=shutil.which('bishengir-opt')
    if not tool:raise RuntimeError('Choose a certified environment containing bishengir-opt')
    subprocess.run([tool,'--canonicalize','--cse',str(source),'-o',str(entry/'canonical.mlir.partial')],check=True)
    (entry/'canonical.mlir.partial').rename(output);count=1;counter.write_text(json.dumps({'calls':count}))
content=output.read_bytes();assert b'arith.constant 42' in content and b'arith.addi' not in content
target=root/'ir/canonical.mlir';target.write_bytes(content)
result={'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],
    'cache_hit':hit,'calls':count,'input_sha256':identity,'ir_sha256':hashlib.sha256(content).hexdigest(),
    'scope':'One controlled offline IR example; not Triton JIT/ccache hit-rate or NPU acceptance'}
if '--fill-bytes' in sys.argv:
    filler=cache/'pressure.bin';fd=os.open(filler,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);length=0;failure=None
    try:
        for chunk in [1024**2,4096]:
            while length<64*1024**2:
                try:os.posix_fallocate(fd,length,chunk);length+=chunk
                except OSError as error:failure=error.errno;break
        os.fsync(fd);info=os.fstat(fd)
    finally:os.close(fd)
    assert failure==errno.EDQUOT
    result['pressure']={'kind':'bytes','errno':failure,'files':[{'name':'pressure.bin','device':info.st_dev,'inode':info.st_ino,'bytes':info.st_size}]}
if '--fill-inodes' in sys.argv:
    files=[];failure=None
    for n in range(8192):
        name='pressure-inode-'+str(n)
        try:fd=os.open(cache/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        except OSError as error:failure=error.errno;break
        info=(cache/name).stat();files.append({'name':name,'device':info.st_dev,'inode':info.st_ino,'bytes':info.st_size})
    assert failure==errno.EDQUOT;result['pressure']={'kind':'inodes','errno':failure,'files':files}
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(result));print(json.dumps(result),flush=True)
if '--hold' in sys.argv:time.sleep(20)

"""Real offline Ascend NPU-IR canonicalization example; no device launch."""
import json
import os
from pathlib import Path
import shutil
import subprocess

root=Path(os.environ['LAB_JOB_DIR']);target=root/'ir/canonical.mlir'
tool=shutil.which('bishengir-opt')
if not tool:raise RuntimeError('Image has no registered bishengir-opt; choose a compatible environment')
subprocess.run([tool,'--canonicalize','--cse','input.mlir','-o',str(target)],check=True)
text=target.read_text();assert 'arith.constant 42' in text and 'arith.addi' not in text,text
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({
    'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],
    'tool':tool,'constant_folded_value':42,
    'scope':'Real bishengir-opt MLIR transformation; not NPU code generation or device accuracy'}))
print('Actual bishengir-opt folded 20 + 22 to constant 42.',flush=True)

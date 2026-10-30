"""CPU build example: produces and calls a real shared-library symbol; no NPU."""
import ctypes
import json
import os
from pathlib import Path
import subprocess

root=Path(os.environ['LAB_JOB_DIR'])
try:
    with Path('/receipt/environment.json').open('a') as file:file.write('tamper')
except PermissionError:protected_receipt_unwritable=True
else:raise AssertionError('Protected bootstrap receipt was writable by task')
target=root/'results/libascend_lab_smoke.so'
subprocess.run(['cc','-shared','-fPIC','-O2','kernel.c','-o',str(target)],check=True)
library=ctypes.CDLL(str(target));library.ascend_lab_add.argtypes=[ctypes.c_int,ctypes.c_int]
library.ascend_lab_add.restype=ctypes.c_int
assert library.ascend_lab_add(20,22)==42
result={'schema':'ascend-lab-result/v1','kind':'build','passed':True,
        'artifacts':['results/libascend_lab_smoke.so'],'compiled_symbol_result':42,
        'protected_receipt_unwritable':protected_receipt_unwritable,
        'scope':'Actual small C library build; not a full Triton/NPU-IR wheel build'}
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(result))
print('Actual compiled symbol: 20 + 22 = 42; protected receipt rejected write.',flush=True)

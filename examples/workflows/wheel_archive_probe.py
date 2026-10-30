"""Stream an existing real wheel through a managed output; do not install or rebuild it."""
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

source=Path(sys.argv[1]);target=Path(os.environ['LAB_JOB_DIR'])/'results/package.whl';digest=hashlib.sha256();size=0
with source.open('rb') as reader,target.open('xb') as writer:
    for block in iter(lambda:reader.read(1024*1024),b''):
        writer.write(block);digest.update(block);size+=len(block)
with zipfile.ZipFile(target) as wheel:
    names=wheel.namelist();metadata=next(name for name in names if name.endswith('.dist-info/METADATA'))
    description=wheel.read(metadata).decode();assert 'Name: triton' in description
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build',
    'passed':True,'artifacts':['results/package.whl'],'bytes':size,'sha256':digest.hexdigest(),
    'wheel_entries':len(names),'scope':'Transfer and metadata inspection of an existing wheel; not a rebuild, install, ABI or numerical acceptance'}))
print('Existing real wheel archived:',size,digest.hexdigest(),flush=True)

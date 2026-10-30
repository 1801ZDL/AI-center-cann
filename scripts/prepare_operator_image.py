"""Create a new, explicit Docker context from audited source and locked wheels.

Does not download, build, install, access Docker, or read any credentials.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from operator_workflow import APP_FILES


def prepare(wheels,output):
    wheels=Path(wheels);output=Path(output).absolute()
    if output.exists() or output.is_symlink():raise ValueError('Choose a new image context directory')
    if wheels.is_symlink() or not wheels.is_dir():raise ValueError('Use an explicit local wheel directory')
    requirement=ROOT/'examples/operator_tools/requirements-linux-x86_64-py311.txt'
    records=[]
    for line in requirement.read_text().splitlines():
        if not line or line.startswith('#'):continue
        match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+) --hash=sha256:([a-f0-9]{64})',line)
        if not match:raise ValueError('Unsupported wheel lock syntax')
        records.append(match.groups())
    if not records or len(records)>96 or len({v[0].lower().replace('_','-') for v in records})!=len(records):raise ValueError('Invalid wheel lock')
    expected={v[2] for v in records};found={};total=0
    for path in sorted(wheels.iterdir()):
        if path.is_symlink() or not path.is_file() or not re.fullmatch('[A-Za-z0-9_.+-]+\\.whl',path.name):raise ValueError('Wheel directory contains unexpected entries')
        size=path.stat().st_size;total+=size
        if not 0<size<=64*1024**2 or total>256*1024**2:raise ValueError('Wheel size budget exceeded')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest not in expected or digest in found:raise ValueError('Wheel differs from lock or is duplicated')
        found[digest]=path
    if set(found)!=expected or len(found)!=len(records):raise ValueError('Complete exact locked wheel set required')
    output.parent.mkdir(parents=True,exist_ok=True);stage=Path(tempfile.mkdtemp(prefix='.operator-context-',dir=output.parent))
    try:
        (stage/'app').mkdir();(stage/'wheels').mkdir()
        for name in APP_FILES:shutil.copyfile(ROOT/name,stage/'app'/name)
        shutil.copyfile(requirement,stage/'requirements-linux-x86_64-py311.txt')
        shutil.copyfile(ROOT/'examples/operator_tools/Dockerfile',stage/'Dockerfile')
        for path in found.values():shutil.copyfile(path,stage/'wheels'/path.name)
        manifest={'schema':'ascend-lab-operator-image-context/v1','target':'CPython3.11/Linux x86_64',
            'files':{str(p.relative_to(stage)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(stage.rglob('*')) if p.is_file()},
            'wheel_count':len(records),'wheel_bytes':total,'downloads_performed':False,'credentials_read':False}
        (stage/'context-manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        stage.rename(output);return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--wheels',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    value=prepare(args.wheels,args.output);print(json.dumps({'prepared':True,'wheel_count':value['wheel_count'],'wheel_bytes':value['wheel_bytes'],'docker_called':False}))

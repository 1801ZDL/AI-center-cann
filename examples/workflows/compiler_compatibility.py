"""Bounded parse/verify reproduction for one sealed MLIR file and pinned tool.

This is ordinary unprivileged ir_v1 workload code, never a security receipt or
permission to execute arbitrary compiler/plugin arguments.
"""
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import subprocess
import sys


def sha(raw):return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def main():
    root=Path(os.environ['LAB_JOB_DIR']);config_path=Path('compiler-case.json');source_path=Path('input.mlir')
    if config_path.is_symlink() or source_path.is_symlink() or config_path.stat().st_size>4096 or not 0<source_path.stat().st_size<=8192:
        raise ValueError('Bounded regular compiler input required')
    config=json.loads(config_path.read_text())
    if (not isinstance(config,dict) or set(config)!={'schema','tool','path','sha256'}
        or config['schema']!='ascend-lab-compiler-input/v1' or config['tool'] not in {'triton-mlir-opt','bishengir-opt'}
        or not isinstance(config['path'],str) or len(config['path'])>500 or '\\' in config['path']
        or config['path']!=str(PurePosixPath(config['path'])) or '..' in PurePosixPath(config['path']).parts
        or not config['path'].startswith(('/usr/','/opt/','/data/')) or any(ord(c)<32 for c in config['path'])
        or Path(config['path']).name!=config['tool'] or not isinstance(config['sha256'],str)
        or not re.fullmatch('[a-f0-9]{64}',config['sha256'])):
        raise ValueError('Use one explicit immutable-image MLIR tool; no arguments or plugins')
    tool=Path(config['path']);actual=file_sha(tool)
    task=json.loads(Path('/payload/run.json').read_text())
    if actual!=config['sha256'] or task['parameters']['lock'].get('files',{}).get(str(tool))!=actual:
        raise ValueError('Executed tool must match both sealed configuration and protected environment lock')
    source=source_path.read_bytes();source.decode('utf-8')
    archived=root/'ir/diagnostic-input.mlir';archived.write_bytes(source);output=root/'ir/verified.mlir'
    version=subprocess.run([str(tool),'--version'],text=True,capture_output=True,timeout=10)
    if version.returncode!=0:raise ValueError('Pinned compiler version query failed')
    argv=[str(tool),str(archived),'-o',str(output)]
    timed_out=False
    try:
        completed=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
        code=completed.returncode;out=completed.stdout;error=completed.stderr
    except subprocess.TimeoutExpired as failure:
        timed_out=True;code=124;out=failure.stdout or b'';error=failure.stderr or b''
    # Separate bounded compiler logs from the workflow wrapper's summary.
    (root/'results/compiler-stdout.txt').write_bytes(out[:8192])
    (root/'results/compiler-stderr.txt').write_bytes(error[:8192])
    sys.stdout.buffer.write(out[:8192]);sys.stdout.buffer.flush();sys.stderr.buffer.write(error[:8192]);sys.stderr.buffer.flush()
    produced=output.is_file() and not output.is_symlink() and 0<output.stat().st_size<=8192
    report={'schema':'ascend-lab-compiler-report/v1','purpose':'mlir_parse_and_verify',
        'driver_sha256':file_sha(Path(__file__)),
        'source':{'artifact':'ir/diagnostic-input.mlir','sha256':sha(source),'bytes':len(source)},
        'tool':{'name':config['tool'],'path':str(tool),'realpath':str(tool.resolve()),'sha256':actual,
            'version':(version.stdout+version.stderr)[:4096]},
        'invocation':{'argv':argv,'extra_passes':False},
        'outcome':{'exit_code':code,'timed_out':timed_out,'stderr_bytes':len(error),'stderr_truncated':len(error)>8192},
        'output':{'artifact':'ir/verified.mlir','sha256':sha(output.read_bytes()),'bytes':output.stat().st_size} if produced else None,
        'scope':'Actual parse/verify only. No lowering, object emission, kernel launch, numerical equivalence or general IR migration certification.'}
    (root/'results/compiler-case.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if code!=0:raise SystemExit(code if 0<code<256 else 1)
    if not produced:raise ValueError('Successful compiler invocation did not produce bounded verified IR')
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,
        'artifacts':['ir/verified.mlir'],'purpose':'mlir_parse_and_verify','tool_sha256':actual,
        'scope':report['scope']}))


if __name__=='__main__':main()

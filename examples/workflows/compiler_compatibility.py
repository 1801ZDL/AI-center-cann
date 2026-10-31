"""Bounded parse/verify reproduction for one sealed MLIR file and pinned tool.

This is ordinary unprivileged ir_v1 workload code, never a security receipt or
permission to execute arbitrary compiler/plugin arguments.
"""
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import resource
import selectors
import signal
import subprocess
import sys
import time

LIMITS={'ascend-lab-compiler-input/v1':8192,'ascend-lab-compiler-input/v2':8*1024**2}


def capture(argv,seconds,output_limit):
    """Drain both pipes without retaining compiler-sized diagnostics in RAM."""
    def limits():resource.setrlimit(resource.RLIMIT_FSIZE,(output_limit,output_limit))
    child=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           start_new_session=True,preexec_fn=limits)
    previews={};counts={};poll=selectors.DefaultSelector();deadline=time.monotonic()+seconds;timed_out=False;reaped=False
    for name,stream in [('stdout',child.stdout),('stderr',child.stderr)]:
        previews[name]=bytearray();counts[name]=0;os.set_blocking(stream.fileno(),False);poll.register(stream,selectors.EVENT_READ,name)
    try:
        while poll.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0:
                timed_out=True
                # This group was created by this exact Popen, and remains owned
                # by its unreaped child leader; no external PID is selected.
                os.killpg(child.pid,signal.SIGKILL)
                break
            for key,_ in poll.select(min(.1,remaining)):
                block=os.read(key.fileobj.fileno(),65536)
                if not block:poll.unregister(key.fileobj);continue
                name=key.data;counts[name]+=len(block)
                previews[name].extend(block[:max(0,8192-len(previews[name]))])
        try:code=child.wait(timeout=5 if timed_out else max(.001,deadline-time.monotonic()));reaped=True
        except subprocess.TimeoutExpired:
            timed_out=True;os.killpg(child.pid,signal.SIGKILL);code=child.wait(timeout=5);reaped=True
    finally:
        if not reaped:
            os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)
        poll.close();child.stdout.close();child.stderr.close()
    return 124 if timed_out else code,timed_out,{k:bytes(v) for k,v in previews.items()},counts


def sha(raw):return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def main():
    root=Path(os.environ['LAB_JOB_DIR']);config_path=Path('compiler-case.json');source_path=Path('input.mlir')
    if config_path.is_symlink() or source_path.is_symlink() or not config_path.is_file() or not source_path.is_file() or config_path.stat().st_size>4096:
        raise ValueError('Bounded regular compiler input required')
    config=json.loads(config_path.read_text())
    if (not isinstance(config,dict) or set(config)!={'schema','tool','path','sha256'}
        or config.get('schema') not in LIMITS or config['tool'] not in {'triton-mlir-opt','bishengir-opt'}
        or not isinstance(config['path'],str) or len(config['path'])>500 or '\\' in config['path']
        or config['path']!=str(PurePosixPath(config['path'])) or '..' in PurePosixPath(config['path']).parts
        or not config['path'].startswith(('/usr/','/opt/','/data/')) or any(ord(c)<32 for c in config['path'])
        or Path(config['path']).name!=config['tool'] or not isinstance(config['sha256'],str)
        or not re.fullmatch('[a-f0-9]{64}',config['sha256'])):
        raise ValueError('Use one explicit immutable-image MLIR tool; no arguments or plugins')
    limit=LIMITS[config['schema']]
    if not 0<source_path.stat().st_size<=limit:raise ValueError('IR exceeds the versioned input budget')
    tool=Path(config['path']);actual=file_sha(tool)
    task=json.loads(Path('/payload/run.json').read_text())
    if actual!=config['sha256'] or task['parameters']['lock'].get('files',{}).get(str(tool))!=actual:
        raise ValueError('Executed tool must match both sealed configuration and protected environment lock')
    source=source_path.read_bytes();source.decode('utf-8')
    archived=root/'ir/diagnostic-input.mlir';archived.write_bytes(source);output=root/'ir/verified.mlir'
    version_code,version_timeout,version,_=capture([str(tool),'--version'],10,limit)
    if version_code!=0 or version_timeout:raise ValueError('Pinned compiler version query failed')
    argv=[str(tool),str(archived),'-o',str(output)]
    code,timed_out,logs,counts=capture(argv,20,limit)
    out=logs['stdout'];error=logs['stderr']
    # Separate bounded compiler logs from the workflow wrapper's summary.
    (root/'results/compiler-stdout.txt').write_bytes(out[:8192])
    (root/'results/compiler-stderr.txt').write_bytes(error[:8192])
    sys.stdout.buffer.write(out[:8192]);sys.stdout.buffer.flush();sys.stderr.buffer.write(error[:8192]);sys.stderr.buffer.flush()
    produced=output.is_file() and not output.is_symlink() and 0<output.stat().st_size<=limit
    report={'schema':config['schema'].replace('-input/','-report/'),'purpose':'mlir_parse_and_verify',
        'driver_sha256':file_sha(Path(__file__)),
        'source':{'artifact':'ir/diagnostic-input.mlir','sha256':sha(source),'bytes':len(source)},
        'tool':{'name':config['tool'],'path':str(tool),'realpath':str(tool.resolve()),'sha256':actual,
            'version':(version['stdout']+version['stderr']).decode('utf-8',errors='replace')[:4096]},
        'invocation':{'argv':argv,'extra_passes':False},
        'outcome':{'exit_code':code,'timed_out':timed_out,'stderr_bytes':counts['stderr'],'stderr_truncated':counts['stderr']>8192,
            'stdout_bytes':counts['stdout'],'stdout_truncated':counts['stdout']>8192},
        'output':{'artifact':'ir/verified.mlir','sha256':sha(output.read_bytes()),'bytes':output.stat().st_size} if produced else None,
        'scope':'Actual parse/verify only. No lowering, object emission, kernel launch, numerical equivalence or general IR migration certification.'}
    (root/'results/compiler-case.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if code!=0:raise SystemExit(code if 0<code<256 else 1)
    if not produced:raise ValueError('Successful compiler invocation did not produce bounded verified IR')
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,
        'artifacts':['ir/verified.mlir'],'purpose':'mlir_parse_and_verify','tool_sha256':actual,
        'scope':report['scope']}))


if __name__=='__main__':main()

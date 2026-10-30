"""Unprivileged workflow runner, loaded from read-only /payload inside a managed task."""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def save(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def nonnegative(value):
    return type(value) in {int,float} and math.isfinite(value) and value>=0


def validate_result(kind,result,root):
    """A structured assertion is workload evidence, not a trusted security receipt."""
    if not isinstance(result,dict) or result.get('schema')!='ascend-lab-result/v1' or result.get('kind')!=kind or result.get('passed') is not True:
        raise ValueError('Missing or failed structured workload assertion')
    if kind=='accuracy':
        checks=result.get('checks')
        if not isinstance(checks,list) or not checks:raise ValueError('Accuracy requires numeric check records')
        for check in checks:
            if not isinstance(check,dict) or not all(nonnegative(check.get(k)) for k in ['max_abs_error','atol','rtol']) or check.get('passed') is not True:raise ValueError('Invalid accuracy check')
    elif kind=='performance':
        samples=result.get('samples_us')
        if not isinstance(samples,list) or not 3<=len(samples)<=10000 or any(not nonnegative(x) or x<=0 for x in samples):raise ValueError('Performance requires finite positive raw samples')
        if not isinstance(result.get('conditions'),dict) or not result['conditions']:raise ValueError('Performance requires measurement conditions')
    elif kind in {'build','simulation','ir'}:
        names=result.get('artifacts')
        if not isinstance(names,list) or not names or len(names)>1000:raise ValueError('Workflow requires actual output artifacts')
        if any(not isinstance(name,str) for name in names) or len(set(names))!=len(names):raise ValueError('Artifact names must be unique strings')
        for name in names:
            if not isinstance(name,str) or name.startswith('/') or '..' in Path(name).parts or '\\' in name:raise ValueError('Invalid result artifact name')
            target=root/name
            if not name.startswith({'build':'results/','simulation':'profile/','ir':'ir/'}[kind]) or target.is_symlink() or not target.resolve().is_relative_to(root.resolve()) or not target.is_file() or target.stat().st_size==0:raise ValueError('Declared output artifact is missing or empty')
    return result


def main():
    config=json.loads(Path('/payload/run.json').read_text());parameters=config['parameters']
    kind=config['template'].removesuffix('_v1');root=Path('/work')
    summary={'schema':'ascend-lab-workflow/v1','kind':kind,'passed':False,'command_executed':False,
             'input':config['input'],'assertion_source':'workload-produced; not independent numerical verification'}
    try:
        # No task gets the node's download credentials or writable input source.
        for item in config['input']['files']:
            path=Path('/inputs')/item['name']
            if path.is_symlink() or not path.is_file() or path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:raise ValueError('Immutable input differs from its manifest')
        summary['environment_lock']='checked-by-protected-bootstrap'
        mode=parameters.get('input_mode','copy')
        if mode=='readonly':work=Path('/inputs')
        elif mode=='copy':
            work=root/'source-tree';shutil.copytree('/inputs',work)
            for path in work.rglob('*'):
                if path.is_dir():path.chmod(0o700)
            for item in config['input']['files']:(work/item['name']).chmod(0o700 if item.get('executable') else 0o600)
        else:raise ValueError('Unknown frozen input mode')
        summary['input_mode']=mode
        env=dict(os.environ);env.update(parameters.get('environment',{}))
        env['LAB_RESULT_FILE']=str(root/parameters['result_file'])
        env['CMAKE_BUILD_PARALLEL_LEVEL']=env.get('MAX_JOBS','1')
        argv=list(parameters['argv'])
        if argv[0] in {'python','python3'}:argv[0]=sys.executable
        summary['command_executed']=True;summary['argv']=argv
        start=time.monotonic()
        completed=subprocess.run(argv,cwd=work,env=env,stdin=subprocess.DEVNULL)
        summary['exit_code']=completed.returncode;summary['command_seconds']=time.monotonic()-start
        if completed.returncode!=0:raise ValueError('Workload process exited with code '+str(completed.returncode))
        result_path=root/parameters['result_file']
        if result_path.is_symlink() or not result_path.resolve().is_relative_to(root) or result_path.stat().st_size>2*1024**2:raise ValueError('Invalid structured result path or size')
        result=json.loads(result_path.read_text());validate_result(kind,result,root)
        summary.update(passed=True,result=result)
    except Exception as error:
        summary['error']=str(error)[:1000]
        print('Workflow failed: '+summary['error'],file=sys.stderr,flush=True)
    save(root/'results/workflow-summary.json',summary)
    print(json.dumps({'workflow':kind,'passed':summary['passed'],'command_executed':summary['command_executed']},ensure_ascii=False),flush=True)
    raise SystemExit(0 if summary['passed'] else 1)


if __name__=='__main__':main()

"""Trusted pre-execution probe, invoked unprivileged before any workload command."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys


def device_access(config):
    """Open only approved device files as the task; never initialize ACL or launch work."""
    ids=config.get('npu_ids',[])
    if not ids:return {'schema':'ascend-lab-device-access/v1','status':'not_requested','passed':None}
    result={'schema':'ascend-lab-device-access/v1','status':'failed','passed':False,
            'requested_device_ids':ids,'opened_device_ids':[],'errors':[],
            'scope':'Task-identity device-file access only; ACL, runtime ID mapping and numerical execution are unverified'}
    try:
        visible_ids=config.get('npu_visible_ids')
        if (not isinstance(ids,list) or len(ids)!=1 or type(ids[0]) is not int or not 0<=ids[0]<256
            or not isinstance(visible_ids,list) or len(visible_ids)!=1
            or type(visible_ids[0]) is not int or not 0<=visible_ids[0]<256):
            raise ValueError('Invalid approved single-device mapping')
        status={key:value.strip() for key,sep,value in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
        result.update(uid=os.getuid(),euid=os.geteuid(),gid=os.getgid(),groups=os.getgroups(),
                      cap_effective=status.get('CapEff'),cap_permitted=status.get('CapPrm'),
                      no_new_privileges=status.get('NoNewPrivs'),runtime_visible_ids=os.environ.get('ASCEND_RT_VISIBLE_DEVICES'))
        if (result['uid']<1000 or result['euid']!=result['uid'] or result['cap_effective']!='0000000000000000'
            or result['cap_permitted']!='0000000000000000' or result['no_new_privileges']!='1'):
            raise ValueError('Device access probe requires the non-root zero-capability task identity')
        if result['runtime_visible_ids']!=','.join(map(str,visible_ids)):
            raise ValueError('Runtime visibility differs from the approved mapping')
        files=sorted(int(p.name[7:]) for p in Path('/dev').iterdir() if p.name.startswith('davinci') and p.name[7:].isascii() and p.name[7:].isdigit())
        result['physical_device_files']=files
        if files!=ids:raise ValueError('Exposed NPU device files differ from the approved single device')
        for device in ids:
            path='/dev/davinci'+str(device)
            try:
                descriptor=os.open(path,os.O_RDWR|os.O_NONBLOCK|os.O_CLOEXEC|os.O_NOFOLLOW)
                try:
                    if not stat.S_ISCHR(os.fstat(descriptor).st_mode):raise ValueError('Approved device file is not a character device')
                finally:os.close(descriptor)
                result['opened_device_ids'].append(device)
            except OSError as error:
                result['errors'].append({'device_id':device,'operation':'open O_RDWR|O_NONBLOCK|O_NOFOLLOW',
                                         'errno':error.errno,'error_type':type(error).__name__})
        if not result['errors']:result.update(status='passed',passed=True)
    except (OSError,ValueError) as error:
        result['error']=str(error)[:500]
    return result


def access_confirmed(receipt,ids):
    """Controller/runner validation of the protected access receipt, not workload JSON."""
    access=receipt.get('device_access')
    return (isinstance(ids,list) and len(ids)==1 and type(ids[0]) is int and 0<=ids[0]<256
            and isinstance(access,dict) and access.get('schema')=='ascend-lab-device-access/v1'
            and access.get('status')=='passed' and access.get('passed') is True
            and access.get('requested_device_ids')==ids and access.get('opened_device_ids')==ids
            and access.get('physical_device_files')==ids and access.get('errors')==[]
            and type(access.get('uid')) is int and access['uid']>=1000 and access.get('euid')==access['uid']
            and access.get('cap_effective')==access.get('cap_permitted')=='0000000000000000'
            and access.get('no_new_privileges')=='1')

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def fingerprint(lock):
    packages={}
    for name in sorted({'triton','triton-ascend','torch','torch-npu',*lock.get('packages',{})}):
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    tools={};cache={}
    for name in sorted({'bishengir-compile','bishengir-opt','triton-opt','ccec','clang','cmake','ninja','git','cc',*lock.get('tools',{})}):
        found=shutil.which(name)
        if not found:tools[name]=None;continue
        path=Path(found).resolve()
        if not any(path.is_relative_to(base) for base in [Path('/usr'),Path('/opt'),Path('/bin'),Path('/lib'),Path('/lib64'),Path('/sbin'),Path('/data')]):
            raise ValueError('Tool resolves outside immutable image/toolchain paths')
        if not path.is_file():tools[name]=None;continue
        if str(path) not in cache:cache[str(path)]=digest(path)
        try:
            p=subprocess.run([str(path),'--version'],capture_output=True,text=True,timeout=3)
            version=(p.stdout+'\n'+p.stderr)[:2048]
        except (OSError,subprocess.TimeoutExpired):version=None
        tools[name]={'path':found,'realpath':str(path),'sha256':cache[str(path)],'version_output':version}
    requested_files=set(lock.get('files',{}))
    config=json.loads(Path('/payload/run.json').read_text())
    if config.get('environment_setup'):requested_files.add(config['environment_setup'])
    try:
        distribution=importlib.metadata.distribution('triton')
        for entry in distribution.files or []:
            if str(entry).endswith('triton/backends/ascend/compiler.py'):requested_files.add(str(distribution.locate_file(entry)))
    except importlib.metadata.PackageNotFoundError:pass
    files={}
    for name in sorted(requested_files):
        p=Path(name)
        files[name]={'path':str(p.resolve()),'sha256':digest(p)} if p.is_file() else None
    return {'python':platform.python_version(),'machine':platform.machine(),'python_executable':sys.executable,'packages':packages,
            'tools':tools,'files':files,'image_sha256':json.loads(Path('/payload/run.json').read_text()).get('image_sha256')}


def compare(lock,observed):
    differences=[]
    for name in ['python','image_sha256','machine','npu_name']:
        if name in lock and lock[name]!=observed.get(name):differences.append(name)
    for name,value in lock.get('packages',{}).items():
        if (None if value=='__absent__' else value)!=observed['packages'].get(name):differences.append('packages.'+name)
    for group in ['tools','files']:
        for name,value in lock.get(group,{}).items():
            if (observed[group].get(name) or {}).get('sha256')!=value:differences.append(group+'.'+name)
    return differences

def main():
    config=json.loads(Path('/payload/run.json').read_text());probe=config['template'] in {'environment_probe','npu_access_probe'}
    lock=config['parameters'].get('lock',{})
    query={group:{name:'' for name in config['parameters'].get(group,[])} for group in ['packages','tools','files']} if probe else lock
    result={'schema':'ascend-lab-environment/v1','locked':False,'workload_started':False}
    try:
        result['device_access']=device_access(config)
        if config.get('npu_ids') and not access_confirmed(result,config['npu_ids']):
            result.update(failure_stage='npu_device_access',differences=['device_access'],
                          error='Task-identity NPU device access failed; workload was not started')
            print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
            raise SystemExit(78)
        result['observed']=fingerprint(query)
        if config.get('npu_name'):result['observed']['npu_name']=config['npu_name']
        result['differences']=compare(lock,result['observed'])
        result['locked']=not result['differences']
        if probe:result['probe_only']=True
    except Exception as error:result['error']=str(error)[:500]
    print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
    raise SystemExit(0 if result['locked'] else 78)


if __name__=='__main__':main()

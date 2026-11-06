"""Administrator-approved immutable-image facts used before resource admission."""
import hashlib
import json
from pathlib import PurePosixPath
import re

CAPABILITY='environment-admission-v1'
PROBE_MEMORY_CAPABILITY='environment-probe-memory-v1'
PROBE='environment_probe'
TOOLS={'cc','c++','gcc','g++','clang','clang++','ld','ld.lld','llvm-config','llvm-as','llvm-dis',
       'bishengir-compile','bishengir-opt','triton-opt','ccec','cmake','ninja','git','python3'}


def immutable_path(value):
    if not isinstance(value,str) or len(value)>500 or value!=str(PurePosixPath(value)) or '..' in PurePosixPath(value).parts:return False
    return value.startswith(('/usr/','/opt/','/bin/','/lib/','/lib64/','/sbin/','/data/')) and not any(ord(c)<32 for c in value)


def probe_parameters(value):
    if not isinstance(value,dict) or set(value)-{'node_id','tools','files','packages','memory_mib'}:raise ValueError('Invalid environment probe parameters')
    if not re.fullmatch('worker-[0-9a-f]{12}',str(value.get('node_id',''))):raise ValueError('Probe must target one registered node')
    result={'node_id':value['node_id']}
    if 'memory_mib' in value:
        if type(value['memory_mib']) is not int or not 256<=value['memory_mib']<=65536:raise ValueError('Fixed CPU probe memory must be 256..65536 MiB')
        result['memory_mib']=value['memory_mib']
    for group in ['tools','files','packages']:
        items=value.get(group,[])
        if not isinstance(items,list) or len(items)>32 or any(not isinstance(x,str) or not x or len(x)>500 for x in items) or len(set(items))!=len(items):raise ValueError('Probe queries must be unique bounded names')
        if group=='tools' and not set(items)<=TOOLS:raise ValueError('Unknown probe tool')
        if group=='files' and any(not immutable_path(x) for x in items):raise ValueError('Probe files must be in immutable image/toolchain paths')
        if group=='packages' and any(not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',x) for x in items):raise ValueError('Invalid probe package name')
        result[group]=items
    return result


def validate(value):
    if not isinstance(value,dict) or set(value)!={'format','image_sha256','environment_setup','observed','source'} or value['format']!='ascend-lab-node-environment/v1':raise ValueError('Invalid node environment contract')
    if not re.fullmatch('sha256:[0-9a-f]{64}',str(value['image_sha256'])):raise ValueError('Environment requires a pinned image')
    if value['environment_setup'] is not None and not immutable_path(value['environment_setup']):raise ValueError('Environment setup must be immutable')
    facts=value['observed']
    if (not isinstance(facts,dict) or facts.get('image_sha256')!=value['image_sha256']
        or not re.fullmatch(r'\d+\.\d+\.\d+',str(facts.get('python','')))
        or not immutable_path(facts.get('python_executable'))
        or not re.fullmatch('[A-Za-z0-9_.-]{1,64}',str(facts.get('machine','')))):raise ValueError('Incomplete observed environment identity')
    for group in ['packages','tools','files']:
        items=facts.get(group)
        if not isinstance(items,dict) or len(items)>96:raise ValueError('Invalid environment fact set')
        for name,item in items.items():
            if not isinstance(name,str) or len(name)>500:raise ValueError('Invalid environment fact name')
            if group=='packages':
                if item is not None and (not isinstance(item,str) or len(item)>500):raise ValueError('Invalid package fact')
            elif item is not None:
                if not isinstance(item,dict) or not re.fullmatch('[0-9a-f]{64}',str(item.get('sha256',''))):raise ValueError('Invalid environment file hash')
                if group=='files' and not immutable_path(name):raise ValueError('File fact is outside immutable paths')
                if group=='tools' and not immutable_path(item.get('realpath')):raise ValueError('Tool fact is outside immutable paths')
    if len(json.dumps(value,allow_nan=False).encode())>256*1024:raise ValueError('Environment contract exceeds limit')
    source=value['source']
    if (not isinstance(source,dict) or set(source)!={'job_id','attempt_id','receipt_sha256','node_revision'}
        or not re.fullmatch('job-[0-9a-f]{12}',str(source['job_id']))
        or not re.fullmatch('attempt-[0-9a-f]{32}',str(source['attempt_id']))
        or not re.fullmatch('[0-9a-f]{64}',str(source['receipt_sha256']))
        or type(source['node_revision']) is not int or source['node_revision']<1):raise ValueError('Invalid environment provenance')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(validate(value),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def differences(lock,contract):
    facts=validate(contract)['observed'];result=[]
    for key in ['image_sha256','python','machine']:
        if key in lock and facts.get(key)!=lock[key]:result.append(key)
    for group in ['packages','tools','files']:
        for key,wanted in lock.get(group,{}).items():
            # Missing metadata is unknown, not proof that a package is absent.
            if key not in facts[group]:result.append(group+'.'+key);continue
            actual=facts[group][key]
            if group=='packages':expected=None if wanted=='__absent__' else wanted
            else:actual=(actual or {}).get('sha256');expected=wanted
            if actual!=expected:result.append(group+'.'+key)
    return result


def from_receipt(receipt,metadata,job,revision):
    if (metadata.get('runtime')!='managed-docker' or metadata.get('privileged') is not False
        or metadata.get('read_only_root') is not True or metadata.get('network_mode')!='none'
        or type(metadata.get('task_uid')) is not int or metadata['task_uid']<1000):raise ValueError('Probe execution was not an isolated non-root task')
    if (receipt.get('schema')!='ascend-lab-environment/v1' or receipt.get('probe_only') is not True
        or receipt.get('locked') is not True or receipt.get('workload_started') is not False
        or receipt.get('probe_exit_code')!=0):raise ValueError('Only a protected successful fixed probe can certify an environment')
    observed=receipt['observed'];facts={k:observed[k] for k in ['python','python_executable','machine','image_sha256','packages','tools','files']}
    facts['tools']={name:({k:item[k] for k in ['path','realpath','sha256']} if item else None) for name,item in facts['tools'].items()}
    return validate({'format':'ascend-lab-node-environment/v1','image_sha256':metadata['image'],
      'environment_setup':metadata.get('environment_setup'),'observed':facts,
      'source':{'job_id':job['id'],'attempt_id':job['attempt_id'],'node_revision':revision,
                'receipt_sha256':hashlib.sha256(json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()}})


def verify_config(config,host=True):
    value=config.get('environment_manifest')
    if value is None:return None
    value=validate(value)
    if (value['image_sha256']!=config.get('image') or value['environment_setup']!=config.get('environment_setup')
        or value['observed']['python_executable']!=config.get('python','/usr/bin/python3')):
        raise ValueError('Node image/setup/Python differs from its certified environment')
    if not host:return fingerprint(value)
    from pathlib import Path
    mounts=config.get('npu',{}).get('driver_paths',[]);paths=[]
    for item in value['observed']['tools'].values():
        if item:paths.append((item['realpath'],item['sha256']))
    for name,item in value['observed']['files'].items():
        if item:paths.append((name,item['sha256']))
    for name,wanted in paths:
        if not any(name==base or name.startswith(base+'/') for base in mounts):continue
        digest=hashlib.sha256()
        with Path(name).open('rb') as source:
            for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
        if digest.hexdigest()!=wanted:raise ValueError('A certified host-mounted toolchain fact changed')
    return fingerprint(value)


def lock_for(value):
    facts=validate(value)['observed']
    return {**{key:facts[key] for key in ['python','machine','image_sha256']},
      'packages':{name:version if version is not None else '__absent__' for name,version in facts['packages'].items()},
      **{group:{name:item['sha256'] for name,item in facts[group].items() if item} for group in ['tools','files']}}

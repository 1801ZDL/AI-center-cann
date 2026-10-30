"""Versioned workflow parameters. Execution permissions stay in node policy."""
import re
import hashlib
from pathlib import Path

WORKFLOWS={name+'_v1':name for name in ['accuracy','performance','simulation','build','ir']}
CAPABILITIES={'immutable-input-v1','protected-environment-v1','short-artifact-paths-v1'}
MAX_TIMEOUT_SECONDS=86400
DEADLINE_CAPABILITY='node-deadline-limit-v1'


def runtime_manifest():
    root=Path(__file__).resolve().parent
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in ['container_entry.py','artifact_export.py','environment_probe.py','environment_contract.py','workflow_spec.py','device_qualification.py']}


def cache_request(value):
    if (not isinstance(value,dict) or set(value)-{'mode','min_free_mib','min_free_inodes'}
        or value.get('mode')!='persistent'):raise ValueError('Choose an explicit persistent cache request')
    result={'mode':'persistent','min_free_mib':value.get('min_free_mib',1),'min_free_inodes':value.get('min_free_inodes',8)}
    if (type(result['min_free_mib']) is not int or not 1<=result['min_free_mib']<=1048576
        or type(result['min_free_inodes']) is not int or not 1<=result['min_free_inodes']<=10000000):
        raise ValueError('Invalid minimum persistent cache byte/inode headroom')
    return result


def validate(parameters,kind):
    if not isinstance(parameters,dict) or set(parameters)-{'input_id','argv','resources','timeout_seconds','lock','result_file','environment','workspace','artifacts','input_mode','cache'}:
        raise ValueError('Invalid workflow parameters')
    cache={'cache':cache_request(parameters['cache'])} if 'cache' in parameters else {}
    if not re.fullmatch(r'input-[0-9a-f]{24}',str(parameters.get('input_id',''))):raise ValueError('Workflow requires a sealed input ID')
    argv=parameters.get('argv')
    if not isinstance(argv,list) or not 1<=len(argv)<=128 or any(not isinstance(x,str) or not x or '\0' in x or len(x)>4096 for x in argv):raise ValueError('Provide an argument array, not a shell string')
    resources={'cpu':1,'memory_mib':512,'shm_mib':0,'npu':1 if kind in {'accuracy','performance','simulation'} else 0,
               **parameters.get('resources',{})} if isinstance(parameters.get('resources',{}),dict) else None
    if resources is None or set(resources)!={'cpu','memory_mib','shm_mib','npu'} or any(type(x) is not int for x in resources.values()):raise ValueError('Invalid workflow resources')
    if kind in {'build','ir'} and resources['npu']!=0:raise ValueError('Offline build/IR workflows cannot acquire an NPU')
    if kind in {'accuracy','performance','simulation'} and resources['npu']<1:raise ValueError('Device workflows require an NPU reservation')
    workspace=parameters.get('workspace')
    if 'artifacts' in parameters:
        from io_policy import artifacts
        artifacts(parameters['artifacts'])
    if parameters.get('input_mode','copy') not in {'copy','readonly'}:raise ValueError('Input mode must be copy or readonly')
    if workspace is not None:
        if (not isinstance(workspace,dict) or set(workspace)!={'min_mib','min_inodes','durable'}
            or type(workspace['min_mib']) is not int or not 1<=workspace['min_mib']<=1048576
            or type(workspace['min_inodes']) is not int or not 1<=workspace['min_inodes']<=10000000
            or type(workspace['durable']) is not bool):raise ValueError('Invalid finite workspace requirements')
    timeout=parameters.get('timeout_seconds',300)
    if type(timeout) is not int or not 1<=timeout<=MAX_TIMEOUT_SECONDS:raise ValueError('Invalid workflow timeout')
    lock=parameters.get('lock')
    if not isinstance(lock,dict) or set(lock)-{'python','packages','tools','files','image_sha256','machine','npu_name'} or not isinstance(lock.get('python'),str) or not re.fullmatch(r'\d+\.\d+\.\d+',lock['python']):raise ValueError('Environment lock requires an exact Python version')
    if 'machine' in lock and not re.fullmatch('[A-Za-z0-9_.-]{1,64}',str(lock['machine'])):raise ValueError('Invalid machine constraint')
    if 'npu_name' in lock and (kind not in {'accuracy','performance','simulation'} or not re.fullmatch('[A-Za-z0-9_. -]{1,80}',str(lock['npu_name']))):raise ValueError('NPU name constraint requires a device workflow and exact management name')
    if str(lock.get('npu_name','')).upper()=='UNKNOWN':raise ValueError('Unknown hardware cannot be an NPU target constraint')
    for key in ['packages','tools','files']:
        mapping=lock.get(key,{})
        if not isinstance(mapping,dict) or len(mapping)>32 or any(not isinstance(k,str) or not k or len(k)>500 or not isinstance(v,str) or len(v)>500 for k,v in mapping.items()):raise ValueError('Invalid environment lock mapping')
        if key in {'tools','files'} and any(not re.fullmatch(r'[0-9a-f]{64}',v) for v in mapping.values()):raise ValueError('Tool/file locks require SHA256')
        if key=='tools' and set(mapping)-{'cc','c++','gcc','g++','clang','clang++','ld','ld.lld','llvm-config','llvm-as','llvm-dis','bishengir-compile','bishengir-opt','triton-opt','ccec','cmake','ninja','git','python3'}:raise ValueError('Tool lock names must use registered image tool names')
        if key=='files' and any(not name.startswith('/') or '\0' in name for name in mapping):raise ValueError('File locks require absolute container paths')
    if 'image_sha256' in lock and not re.fullmatch(r'sha256:[0-9a-f]{64}',str(lock['image_sha256'])):raise ValueError('Image lock requires a SHA256 ID')
    environment=parameters.get('environment',{})
    allowed_env={'TRITON_ALWAYS_COMPILE','TRITON_KERNEL_DUMP','LINE_INFO','TRITON_DEBUG','TRITON_PRINT_AUTOTUNING'}
    if not isinstance(environment,dict) or set(environment)-allowed_env or any(v not in {'0','1'} for v in environment.values()):raise ValueError('Unsupported workflow environment overrides')
    result_file=parameters.get('result_file','results/workload-result.json')
    from inputs import safe_name
    if not safe_name(result_file) or not result_file.startswith('results/'):raise ValueError('Structured result must be below results/')
    return {**parameters,**cache,'resources':resources,'timeout_seconds':timeout,'result_file':result_file,'environment':environment}

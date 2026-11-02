"""Queue-owned fixed CANN diagnostic in a new restricted container.

No workload command, tensor allocation, application kernel, stream creation,
device reset, privileged container, host PID/IPC, driver or daemon changes.
Metadata mode exposes no NPU and makes no CANN calls. The actual mode must
reserve one healthy idle card, verifies both mapping directions before creating
one explicit context, then destroys that context and finalizes this process.
This diagnostic cannot grant normal workflow qualification.
"""
import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def protected(path):
    path=Path(path).resolve(strict=True)
    for parent in [path,*path.parents]:
        info=parent.stat()
        if info.st_uid!=0 or info.st_mode&0o022:raise ValueError('CANN/driver sources need protected Root ancestry')
    return path


def capture_logs(root):
    """Bound only this actor's private files; never follow links or devices."""
    result={'files':[],'captured_bytes':0,'scanned_entries':0,'incomplete':False}
    base=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);device=os.fstat(base).st_dev
    def visit(parent,relative,depth):
        if depth>6:result['incomplete']=True;return
        with os.scandir(parent) as entries:
            for entry in entries:
                result['scanned_entries']+=1
                if result['scanned_entries']>128 or len(result['files'])>=8 or result['captured_bytes']>=256*1024:
                    result['incomplete']=True;return
                info=entry.stat(follow_symlinks=False);name=relative+entry.name
                if info.st_dev!=device or info.st_uid!=os.getuid() or len(name)>1024:
                    result['incomplete']=True;continue
                if stat.S_ISDIR(info.st_mode):
                    fd=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                    try:visit(fd,name+'/',depth+1)
                    finally:os.close(fd)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                    fd=os.open(entry.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                    with os.fdopen(fd,'rb') as stream:
                        before=os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.getuid():
                            result['incomplete']=True;continue
                        data=stream.read(min(64*1024,256*1024-result['captured_bytes']));after=os.fstat(stream.fileno())
                    changed=any(getattr(before,k)!=getattr(after,k) for k in ['st_size','st_mtime_ns','st_ctime_ns'])
                    result['files'].append({'name':name,'data_base64':base64.b64encode(data).decode(),
                        'captured_sha256':hashlib.sha256(data).hexdigest(),'size_before':before.st_size,
                        'prefix_truncated':len(data)<before.st_size,'changed_during_read':changed})
                    result['captured_bytes']+=len(data)
                    result['incomplete']|=changed or len(data)<before.st_size
                else:result['incomplete']=True
    try:
        for folder in ['home','tmp','logs','work']:
            try:fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=base)
            except FileNotFoundError:continue
            try:visit(fd,folder+'/',0)
            finally:os.close(fd)
    finally:os.close(base)
    return result


def initialize(library,physical):
    """Fixed C ABI only; binding injection permits hardware-free negative tests."""
    if type(physical) is not int or not 0<=physical<256:raise ValueError('Choose one allocated physical device')
    names={'aclInit':([ctypes.c_char_p],), 'aclFinalize':([],),
        'aclrtGetDeviceCount':([ctypes.POINTER(ctypes.c_uint32)],),
        'aclrtGetLogicDevIdByPhyDevId':([ctypes.c_int32,ctypes.POINTER(ctypes.c_int32)],),
        'aclrtGetPhyDevIdByLogicDevId':([ctypes.c_int32,ctypes.POINTER(ctypes.c_int32)],),
        'aclrtCreateContext':([ctypes.POINTER(ctypes.c_void_p),ctypes.c_int32],),
        'aclrtDestroyContext':([ctypes.c_void_p],)}
    result={'calls':[],'context_created':False,'context_destroyed':False,'acl_initialized':False,
        'application_kernel_api_calls':0,'device_reset_api_calls':0,'runtime_internal_work_unverified':True,
        'qualification_granted':False,'status':'unconfirmed'}
    functions={}
    for name,(arguments,) in names.items():
        try:function=getattr(library,name)
        except AttributeError:result.update(status='api_unavailable',missing_api=name);return result
        function.argtypes=arguments;function.restype=ctypes.c_int;functions[name]=function
    def call(name,*args):
        started=time.monotonic();code=functions[name](*args)
        if type(code) is not int:raise ValueError('CANN returned a non-integer status')
        result['calls'].append({'operation':name,'return_code':code,'seconds':round(time.monotonic()-started,6)})
        if code!=0:
            try:
                error=getattr(library,'aclGetRecentErrMsg');error.argtypes=[];error.restype=ctypes.c_char_p
                message=error()
                result['recent_error_message']=message[:4096].decode('utf-8',errors='replace') if isinstance(message,bytes) else None
            except (OSError,AttributeError,ValueError,TypeError):result['recent_error_message_unavailable']=True
        return code
    if call('aclInit',None)!=0:result.update(status='acl_initialization_failed');return result
    result['acl_initialized']=True;context=ctypes.c_void_p()
    try:
        count=ctypes.c_uint32()
        if call('aclrtGetDeviceCount',ctypes.byref(count))!=0:result['status']='device_count_failed';return result
        result['device_count']=count.value
        if not 1<=count.value<=256:result['status']='device_count_unconfirmed';return result
        logical=ctypes.c_int32(-1);returned=ctypes.c_int32(-1)
        if call('aclrtGetLogicDevIdByPhyDevId',physical,ctypes.byref(logical))!=0:
            result['status']='physical_mapping_failed';return result
        if not 0<=logical.value<256:result['status']='physical_mapping_unconfirmed';return result
        if call('aclrtGetPhyDevIdByLogicDevId',logical.value,ctypes.byref(returned))!=0 or returned.value!=physical:
            result['status']='mapping_roundtrip_unconfirmed';return result
        result['mapping']={'physical_id':physical,'logical_id':logical.value,'roundtrip_physical_id':returned.value,
            'visibility_environment_unset':True}
        if call('aclrtCreateContext',ctypes.byref(context),logical.value)!=0:
            result['status']='context_creation_failed';return result
        if not context.value:result['status']='context_handle_unconfirmed';return result
        result['context_created']=True
        result['context_destroyed']=call('aclrtDestroyContext',context)==0
        result['status']='context_created_and_destroyed' if result['context_destroyed'] else 'context_cleanup_unconfirmed'
        return result
    finally:
        result['acl_finalize_succeeded']=call('aclFinalize')==0
        if not result['acl_finalize_succeeded'] and result['status']=='context_created_and_destroyed':
            result['status']='finalization_unconfirmed'


def actor():
    config=json.loads(Path('/probe-config.json').read_text())
    fields={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    identity={k:fields[k] for k in ['Uid','Gid','Groups','CapEff','CapPrm','NoNewPrivs','Seccomp']}
    assert os.getuid()==23901 and os.geteuid()==23901 and os.getgid()==23901 and 1001 in os.getgroups()
    assert identity['CapEff']==identity['CapPrm']=='0000000000000000' and identity['NoNewPrivs']=='1'
    assert 'ASCEND_RT_VISIBLE_DEVICES' not in os.environ
    devices=sorted(int(p.name[7:]) for p in Path('/dev').glob('davinci[0-9]*') if p.name[7:].isascii() and p.name[7:].isdigit())
    assert devices==([] if config['metadata_only'] else [config['physical_id']])
    for name in ['home','tmp','logs','work']:Path('/work',name).mkdir(mode=0o700,exist_ok=True)
    os.umask(0o077)
    environment={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LD_LIBRARY_PATH','ASCEND_HOME_PATH','ASCEND_OPP_PATH','ASCEND_AICPU_PATH'}}
    environment.update(HOME='/work/home',TMPDIR='/work/tmp',ASCEND_PROCESS_LOG_PATH='/work/logs',ASCEND_WORK_PATH='/work/work')
    environment['LD_LIBRARY_PATH']='/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:/usr/local/dcmi:'+environment.get('LD_LIBRARY_PATH','')
    os.environ.clear();os.environ.update(environment)
    files={name:sha(name) for name in config['source_sha256']}
    assert files==config['source_sha256']
    assert sha('/probe.py')==config['program_sha256']
    report={'metadata_only':config['metadata_only'],'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),
        'identity':identity,'physical_device_files':devices,'source_sha256':files,'program_sha256':config['program_sha256'],'python':sys.version.split()[0],
        'application_kernel_api_calls':0,'device_reset_api_calls':0,'qualification_granted':False}
    if config['metadata_only']:
        report.update(status='metadata_verified',acl_initialized=False,context_created=False)
    else:
        try:
            library=ctypes.CDLL(config['library'],mode=os.RTLD_LOCAL|os.RTLD_NOW)
            report.update(initialize(library,config['physical_id']))
        except OSError as error:
            report.update(status='library_load_failed',error_type=type(error).__name__,error=str(error)[:512],acl_initialized=False,context_created=False)
    try:report['private_logs']=capture_logs('/work')
    except (OSError,ValueError) as error:
        report['private_logs']={'status':'unavailable','error_type':type(error).__name__,'errno':getattr(error,'errno',None)}
    assert files=={name:sha(name) for name in files}
    Path('/work/runtime-result.json').write_text(json.dumps(report,sort_keys=True)+'\n')
    print('CANN_DIAGNOSTIC_RESULT '+json.dumps(report,sort_keys=True),flush=True)


def parent(args):
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from lab_client import read_json,atomic_json,now
    from npu_admission import query,selected_reading
    from docker_runtime import discover,command
    assert os.environ.get('LAB_JOB_DIR'),'Submit the fixed diagnostic through the existing queue'
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json');physical=None
    assert job['resources']['npu']==(0 if args.metadata else 1)
    if not args.metadata:
        assert len(job['allocation']['npu_ids'])==1;physical=job['allocation']['npu_ids'][0]
    assert re.fullmatch('sha256:[0-9a-f]{64}',args.image)
    cann=protected(args.cann_root);setup=protected(cann/'set_env.sh');library=protected(cann/'lib64/libascendcl.so')
    assert library.is_relative_to(cann) and '/devlib/' not in str(library) and '/simulator/' not in str(library)
    paths=[setup,library,protected(cann/'include/acl/acl_rt.h'),protected('/usr/local/Ascend/driver/version.info'),
        protected('/usr/local/Ascend/driver/lib64/driver/libascend_hal.so')]
    sources={str(path):sha(path) for path in paths}
    base=directory/'tmp/cann-runtime';base.mkdir(mode=0o700)
    program=base/'probe.py';program.write_bytes(Path(__file__).read_bytes());program.chmod(0o444)
    configuration=base/'probe-config.json';atomic_json(configuration,{'metadata_only':args.metadata,'physical_id':physical,
        'library':str(library),'source_sha256':sources,'program_sha256':sha(program)});configuration.chmod(0o444)
    binding,_=discover();tag='cann-runtime-'+job['id'];name='ascend-lab-'+tag
    def docker(argv,check=True,timeout=15):
        cmd,env=command(binding,['docker',*argv]);result=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned CANN diagnostic operation failed: '+result.stderr[-600:])
        return result
    def idle():
        if physical is None:return []
        devices,processes=query();return selected_reading([physical],(devices,processes))
    before=idle()
    argv=['create','--pull','never','--name',name,'--label','io.ascend-lab.cann-runtime='+tag,
        '--user','23901:23901','--group-add','1001','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--memory','768m','--memory-swap','768m','--cpus','1',
        '--cpuset-cpus',','.join(map(str,job['allocation']['cpu_ids'])),'--pids-limit','48','--ipc','none',
        '--tmpfs','/work:rw,nosuid,nodev,size=96m,mode=700,uid=23901,gid=23901',
        '--log-opt','max-size=1m','--log-opt','max-file=1','--env','LD_PRELOAD=',
        '--mount','type=bind,src='+str(program)+',dst=/probe.py,readonly',
        '--mount','type=bind,src='+str(configuration)+',dst=/probe-config.json,readonly',
        '--mount','type=bind,src='+str(cann)+',dst='+str(cann)+',readonly',
        '--mount','type=bind,src=/usr/local/Ascend/driver,dst=/usr/local/Ascend/driver,readonly']
    for path in ['/usr/local/dcmi','/etc/ascend_install.info','/etc/vnpu.cfg']:
        if Path(path).exists():protected(path);argv+=['--mount','type=bind,src='+path+',dst='+path+',readonly']
    if physical is not None:
        for path in ['/dev/davinci'+str(physical),'/dev/davinci_manager','/dev/hisi_hdc']:
            assert stat.S_ISCHR(Path(path).stat().st_mode);argv+=['--device',path+':'+path+':rwm']
    argv+=['--entrypoint','/usr/bin/env',args.image,'-i','PATH=/usr/bin:/bin','LANG=C.UTF-8','HOME=/work/home','TMPDIR=/work/tmp',
        '/bin/bash','--noprofile','--norc','-c','source "$1" >/dev/null 2>&1 || exit 126; export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:/usr/local/dcmi:${LD_LIBRARY_PATH:-}; unset ASCEND_RT_VISIBLE_DEVICES; exec /usr/bin/timeout --kill-after=3s 35s /usr/bin/python3 -I -B /probe.py --actor','cann-fixed-probe',str(setup)]
    atomic_json(base/'intent.json',{'container_name':name,'label':tag,'image':args.image,'physical_id':physical,'binding':binding,'source_sha256':sources})
    try:
        docker(argv)
        output=docker(['start','--attach',name],check=False,timeout=45)
        (base/'stdout.log').write_text(output.stdout);(base/'stderr.log').write_text(output.stderr)
        info=json.loads(docker(['inspect',name]).stdout)[0]
        assert info['Config']['Labels']['io.ascend-lab.cann-runtime']==tag
        assert not info['State']['Running'] and info['State']['Pid']==0
        assert not info['HostConfig']['Privileged'] and info['HostConfig']['ReadonlyRootfs'] and info['HostConfig']['NetworkMode']=='none'
        limits=info['HostConfig']
        assert limits['Memory']==limits['MemorySwap']==768*1024**2 and limits['PidsLimit']==48 and limits['NanoCpus']==10**9
        assert limits['CapDrop']==['ALL'] and 'no-new-privileges:true' in limits['SecurityOpt']
        assert info['Config']['User']=='23901:23901'
        assert all(not m['RW'] for m in info['Mounts'] if m['Destination']!='/work')
        lines=[line[len('CANN_DIAGNOSTIC_RESULT '):] for line in output.stdout.splitlines() if line.startswith('CANN_DIAGNOSTIC_RESULT ')]
        if len(lines)!=1:raise RuntimeError('CANN diagnostic exited before a complete result; inspect the original private logs')
        result=json.loads(lines[0]);assert output.returncode==0 and info['State']['ExitCode']==0 and result['source_sha256']==sources
        assert sources=={name:sha(name) for name in sources}
        after=idle()
        report={'diagnostic_completed':True,'recorded_at':now(),'job_id':job['id'],'metadata_only':args.metadata,'physical_id':physical,
            'image':args.image,'container_id':info['Id'],'container_started_at':info['State']['StartedAt'],
            'hard_limits':{k:limits[k] for k in ['Memory','MemorySwap','PidsLimit','NanoCpus','CpusetCpus','CapDrop','SecurityOpt']},
            'device_nodes':limits.get('Devices') or [],'mounts':[{'destination':m['Destination'],'writable':m['RW']} for m in info['Mounts']],
            'result':result,'before':before,'after':after,'container_exit_confirmed':True,
            'application_kernel_api_calls':0,'device_reset_api_calls':0,'qualification_granted':False,
            'existing_containers_or_driver_modified':False,'scope':'Restricted fixed CANN API diagnostic only; no numerical/performance or ordinary workflow qualification'}
        atomic_json(directory/'results/cann-runtime-diagnostic.json',report);print(json.dumps(report,sort_keys=True))
    finally:
        # Inspect the original named/labelled object, including uncertain create.
        response=docker(['inspect',name],check=False)
        if response.returncode==0:
            info=json.loads(response.stdout)[0];assert info['Config']['Labels']['io.ascend-lab.cann-runtime']==tag
            if info['State']['Running']:docker(['stop','--time','3',name],timeout=15)
            info=json.loads(docker(['inspect',name]).stdout)[0]
            assert not info['State']['Running'] and info['State']['Pid']==0,'Preserve original intent; diagnostic termination unconfirmed'
            docker(['rm',name])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor',action='store_true');parser.add_argument('--metadata',action='store_true')
    parser.add_argument('--image');parser.add_argument('--cann-root');args=parser.parse_args()
    if args.actor:actor()
    else:parent(args)

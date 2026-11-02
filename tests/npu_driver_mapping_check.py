"""Maintainer-only queued DSMI query, without ACL or container-service setup.

Compare the current host identity with one new restricted nonroot container.
An explicit Root-protected ABI reference is verified before the three fixed APIs.
Neither mapping success nor management count grants workload qualification.
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


DRIVER=Path('/usr/local/Ascend/driver')
LIBRARY=DRIVER/'lib64/driver/libdrvdsmi_host.so'
ABI={
    'dsmi_get_device_count':('int *device_count',[ctypes.POINTER(ctypes.c_int)]),
    'dsmi_get_logicid_from_phyid':('unsigned int phyid, unsigned int *logicid',
                                [ctypes.c_uint,ctypes.POINTER(ctypes.c_uint)]),
    'dsmi_get_phyid_from_logicid':('unsigned int logicid, unsigned int *phyid',
                                [ctypes.c_uint,ctypes.POINTER(ctypes.c_uint)]),
}


def trace_files():
    """Fixed host tracer closure, mounted away from the tracee's loader paths."""
    if os.uname().machine!='x86_64':raise ValueError('This tracer closure requires x86_64')
    paths=['/usr/bin/strace','/lib64/ld-linux-x86-64.so.2']+[
        '/lib/x86_64-linux-gnu/'+name for name in ['libunwind-ptrace.so.0','libunwind-x86_64.so.8',
        'libc.so.6','liblzma.so.5','libunwind.so.8']]
    values=[{'source':str(protected(path)),'destination':'/trace-runtime/'+Path(path).name} for path in paths]
    if sum(Path(v['source']).stat().st_size for v in values)>64*1024**2:raise ValueError('Tracer runtime exceeds budget')
    return values


def parse_trace(text,physical):
    """Selected device boundaries only; never return PIDs, addresses or payloads."""
    if physical is not None and (type(physical) is not int or not 0<=physical<256):raise ValueError('Invalid physical ID')
    allowed={'/dev/davinci_manager','/dev/hisi_hdc'}
    if physical is not None:allowed.add('/dev/davinci'+str(physical))
    events=[];incomplete=False
    for line in text.splitlines():
        if len(line)>4096:incomplete=True;continue
        match=re.match(r'^(?:\d+\s+)?(openat|open|ioctl|close)\(',line)
        path=re.search(r'["<](/dev/(?:davinci_manager|hisi_hdc|davinci[0-9]+))(?=["<>])',line)
        if not match or not path or path[1] not in allowed:continue
        if ' = ' not in line:incomplete=True;continue
        returned=re.match(r'(-?\d+)(?:\s+([A-Z][A-Z0-9_]+)\b)?',line.rsplit(' = ',1)[-1])
        if not returned:incomplete=True;continue
        if len(events)>=64:incomplete=True;continue
        events.append({'operation':match[1],'path':path[1],'return_value':int(returned[1]),'errno':returned[2]})
    return {'events':events,'parser_incomplete':incomplete,'complete_history':False,
            'scope':'Selected device syscalls in a bounded live prefix; no cause or qualification inference'}


def capture_trace(path,physical):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid():raise ValueError('Trace must be a private regular file')
        raw=stream.read(256*1024);after=os.fstat(stream.fileno())
    return {'captured_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'prefix_truncated':len(raw)<info.st_size,'changed_during_read':after.st_size!=info.st_size,
            'raw_base64':base64.b64encode(raw).decode(),'summary':parse_trace(raw.decode('utf-8',errors='replace'),physical)}


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def protected(path):
    path=Path(path).resolve(strict=True)
    for item in [path,*path.parents]:
        info=item.stat()
        if info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Driver sources need protected Root ancestry')
    return path


def verify_header(text):
    for name,(arguments,_) in ABI.items():
        prototype='int '+name+'('+arguments+');'
        compact=lambda value:re.sub(r'\s+','',value)
        if compact(prototype) not in compact(text):raise ValueError('Installed DSMI header prototype unavailable: '+name)


def mapping(library,physical):
    if type(physical) is not int or not 0<=physical<256:raise ValueError('Choose one allocated physical device')
    result={'calls':[],'status':'unconfirmed','physical_id':physical,'qualification_granted':False,
            'acl_initialized':False,'context_created':False,'application_kernel_api_calls':0,
            'device_reset_api_calls':0,'container_service_setup_calls':0}
    functions={}
    for name,(_,arguments) in ABI.items():
        try:function=getattr(library,name)
        except AttributeError:result.update(status='api_unavailable',missing_api=name);return result
        function.argtypes=arguments;function.restype=ctypes.c_int;functions[name]=function
    def call(name,*arguments):
        start=time.monotonic();code=functions[name](*arguments)
        if type(code) is not int:raise ValueError('DSMI returned a noninteger status')
        result['calls'].append({'operation':name,'return_code':code,'seconds':round(time.monotonic()-start,6)})
        return code
    count=ctypes.c_int(-1)
    if call('dsmi_get_device_count',ctypes.byref(count))!=0:
        result['status']='device_count_failed';return result
    result['device_count']=count.value
    if not 1<=count.value<=256:result['status']='device_count_unconfirmed';return result
    logical=ctypes.c_uint(0xffffffff);returned=ctypes.c_uint(0xffffffff)
    if call('dsmi_get_logicid_from_phyid',physical,ctypes.byref(logical))!=0:
        result['status']='physical_mapping_failed';return result
    if not 0<=logical.value<256:result['status']='logical_id_unconfirmed';return result
    if call('dsmi_get_phyid_from_logicid',logical.value,ctypes.byref(returned))!=0:
        result['status']='reverse_mapping_failed';return result
    if returned.value!=physical:result['status']='mapping_roundtrip_unconfirmed';return result
    result.update(status='mapping_verified',mapping={'physical_id':physical,'logical_id':logical.value,
                                                  'roundtrip_physical_id':returned.value})
    return result


def actor(config_path):
    config=json.loads(Path(config_path).read_text());restricted=config['restricted_container']
    fields={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    identity={k:fields[k] for k in ['Uid','Gid','Groups','CapEff','CapPrm','NoNewPrivs','Seccomp']}
    if restricted:
        assert os.getuid()==os.geteuid()==os.getgid()==23901 and 1001 in os.getgroups()
        assert identity['CapEff']==identity['CapPrm']=='0000000000000000' and identity['NoNewPrivs']=='1'
    assert 'ASCEND_RT_VISIBLE_DEVICES' not in os.environ and 'LD_PRELOAD' not in os.environ
    assert sha(__file__)==config['program_sha256']
    locations=config.get('source_locations',{}) if restricted else {}
    assert config['source_sha256']=={name:sha(locations.get(name,name)) for name in config['source_sha256']}
    verify_header(Path(config['abi_header']).read_text())
    devices=sorted(int(p.name[7:]) for p in Path('/dev').glob('davinci[0-9]*')
                   if p.name[7:].isascii() and p.name[7:].isdigit())
    if restricted:assert devices==([] if config['metadata_only'] else [config['physical_id']])
    report={'restricted_container':restricted,'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),
            'identity':identity,'physical_device_files':devices,'source_sha256':config['source_sha256'],
            'program_sha256':config['program_sha256'],'python':sys.version.split()[0],'metadata_only':config['metadata_only']}
    if config['metadata_only']:
        report.update(status='metadata_verified',calls=[],qualification_granted=False,acl_initialized=False)
    else:
        try:report.update(mapping(ctypes.CDLL(str(LIBRARY),mode=os.RTLD_LOCAL|os.RTLD_NOW),config['physical_id']))
        except OSError as error:report.update(status='library_load_failed',error_type=type(error).__name__,
            error=str(error)[:512],calls=[],qualification_granted=False,acl_initialized=False)
    if restricted and config.get('syscall_trace'):
        try:report['syscall_trace']=capture_trace('/work/syscalls.log',config['physical_id'])
        except (OSError,ValueError) as error:report['syscall_trace']={'status':'unavailable','error_type':type(error).__name__,'errno':getattr(error,'errno',None)}
    assert config['source_sha256']=={name:sha(locations.get(name,name)) for name in config['source_sha256']}
    print('DSMI_MAPPING_RESULT '+json.dumps(report,sort_keys=True),flush=True)


def parent(args):
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from lab_client import atomic_json,read_json,now
    from docker_runtime import discover,command
    from npu_admission import query,selected_reading
    assert os.environ.get('LAB_JOB_DIR'),'Submit this fixed diagnostic through the existing queue'
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json')
    assert job['resources']['npu']==(0 if args.metadata else 1)
    physical=None
    if not args.metadata:
        assert len(job['allocation']['npu_ids'])==1;physical=job['allocation']['npu_ids'][0]
    assert re.fullmatch('sha256:[0-9a-f]{64}',args.image)
    header=protected(args.abi_header);verify_header(header.read_text())
    sources={str(protected(path)):sha(protected(path)) for path in [header,LIBRARY,
        DRIVER/'lib64/driver/libascend_hal.so',DRIVER/'version.info']}
    tracer=trace_files() if args.trace else []
    sources.update({item['source']:sha(item['source']) for item in tracer})
    base=directory/'tmp/driver-mapping';base.mkdir(mode=0o700)
    program=base/'probe.py';program.write_bytes(Path(__file__).read_bytes());program.chmod(0o444)
    config={'physical_id':physical,'metadata_only':args.metadata,'program_sha256':sha(program),
            'source_sha256':sources,'abi_header':str(header),'syscall_trace':args.trace,
            'source_locations':{item['source']:item['destination'] for item in tracer}}
    host_config=base/'host-config.json';atomic_json(host_config,{**config,'restricted_container':False})
    container_config=base/'container-config.json';atomic_json(container_config,{**config,'restricted_container':True});container_config.chmod(0o444)
    binding,_=discover();tag='driver-mapping-'+job['id'];name='ascend-lab-'+tag
    def docker(argv,check=True,timeout=15):
        cmd,env=command(binding,['docker',*argv]);result=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned driver query operation failed: '+result.stderr[-500:])
        return result
    def idle():
        if physical is None:return []
        devices,processes=query();return selected_reading([physical],(devices,processes))
    def result(output,label):
        (base/(label+'-stdout.log')).write_text(output.stdout);(base/(label+'-stderr.log')).write_text(output.stderr)
        lines=[line[len('DSMI_MAPPING_RESULT '):] for line in output.stdout.splitlines() if line.startswith('DSMI_MAPPING_RESULT ')]
        if len(lines)!=1 or output.returncode:raise RuntimeError('Original driver query did not emit a complete result; inspect its private logs')
        value=json.loads(lines[0]);assert value['source_sha256']==sources and value['program_sha256']==config['program_sha256']
        return value
    before=idle()
    clean_env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LD_LIBRARY_PATH':
        '/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common'}
    # A query in the current host identity, not a privileged payload or an
    # assertion that the host subprocess has container cgroup isolation.
    host=result(subprocess.run(['/usr/bin/timeout','--kill-after=2s','15s','/usr/bin/python3','-I','-B',str(program),
        '--actor','--config',str(host_config)],env={**clean_env,'HOME':str(base),'TMPDIR':str(base)},
        capture_output=True,text=True,timeout=20),'host')
    argv=['create','--pull','never','--name',name,'--label','io.ascend-lab.driver-mapping='+tag,
        '--user','23901:23901','--group-add','1001','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--memory','256m','--memory-swap','256m','--cpus','1',
        '--cpuset-cpus',','.join(map(str,job['allocation']['cpu_ids'])),'--pids-limit','32','--ipc','none',
        '--tmpfs','/work:rw,nosuid,nodev,size=16m,mode=700,uid=23901,gid=23901',
        '--log-opt','max-size=1m','--log-opt','max-file=1',
        '--mount','type=bind,src='+str(program)+',dst=/probe.py,readonly',
        '--mount','type=bind,src='+str(container_config)+',dst=/probe-config.json,readonly',
        '--mount','type=bind,src='+str(header)+',dst='+str(header)+',readonly',
        '--mount','type=bind,src='+str(protected(DRIVER))+',dst='+str(DRIVER)+',readonly']
    for item in tracer:argv+=['--mount','type=bind,src='+item['source']+',dst='+item['destination']+',readonly']
    if physical is not None:
        for path in ['/dev/davinci'+str(physical),'/dev/davinci_manager','/dev/hisi_hdc']:
            assert stat.S_ISCHR(Path(path).stat().st_mode);argv+=['--device',path+':'+path+':rwm']
    trace_command=['/trace-runtime/ld-linux-x86-64.so.2','--library-path','/trace-runtime','/trace-runtime/strace',
        '-f','-qq','-yy','-s','128','-e','trace=openat,open,ioctl,close','-o','/work/syscalls.log'] if args.trace else []
    argv+=['--entrypoint','/usr/bin/env',args.image,'-i',*[key+'='+value for key,value in clean_env.items()],
        'HOME=/work','TMPDIR=/work',*trace_command,'/usr/bin/timeout','--kill-after=2s','15s','/usr/bin/python3','-I','-B','/probe.py',
        '--actor','--config','/probe-config.json']
    atomic_json(base/'intent.json',{'container_name':name,'label':tag,'image':args.image,'binding':binding,'physical_id':physical})
    try:
        docker(argv);output=docker(['start','--attach',name],check=False,timeout=25)
        (base/'container-stdout.log').write_text(output.stdout);(base/'container-stderr.log').write_text(output.stderr)
        info=json.loads(docker(['inspect',name]).stdout)[0]
        assert info['Image']==args.image and info['Config']['Labels']['io.ascend-lab.driver-mapping']==tag
        assert not info['State']['Running'] and info['State']['Pid']==0 and info['State']['ExitCode']==0
        limits=info['HostConfig']
        assert not limits['Privileged'] and limits['ReadonlyRootfs'] and limits['NetworkMode']=='none' and limits['IpcMode']=='none'
        assert limits['Memory']==limits['MemorySwap']==256*1024**2 and limits['PidsLimit']==32 and limits['NanoCpus']==10**9
        assert limits['CapDrop']==['ALL'] and 'no-new-privileges:true' in limits['SecurityOpt']
        assert info['Config']['User']=='23901:23901' and all(not m['RW'] for m in info['Mounts'] if m['Destination']!='/work')
        container=result(output,'container');assert sources=={path:sha(path) for path in sources};after=idle()
        report={'recorded_at':now(),'job_id':job['id'],'diagnostic_completed':True,'metadata_only':args.metadata,
            'physical_id':physical,'image':args.image,'syscall_trace_requested':args.trace,'host':host,'container':container,'before':before,'after':after,
            'container_exit_confirmed':True,'device_nodes':limits.get('Devices') or [],
            'hard_limits':{k:limits[k] for k in ['Memory','MemorySwap','PidsLimit','NanoCpus','CpusetCpus','CapDrop','SecurityOpt']},
            'mounts':[{'destination':m['Destination'],'writable':m['RW']} for m in info['Mounts']],
            'qualification_granted':False,'acl_initialized':False,'application_kernel_api_calls':0,'device_reset_api_calls':0,
            'container_service_setup_calls':0,'scope':'DSMI count and selected physical/logical roundtrip only; no ACL or workload qualification'}
        atomic_json(directory/'results/driver-mapping.json',report);print(json.dumps(report,sort_keys=True))
    finally:
        response=docker(['inspect','--format',
            '{"labels":{{json .Config.Labels}},"image":{{json .Image}},"state":{{json .State}}}',name],check=False)
        if response.returncode==0:
            info=json.loads(response.stdout)
            assert info['labels']['io.ascend-lab.driver-mapping']==tag and info['image']==args.image
            if info['state']['Running']:docker(['stop','--time','2',name])
            state=json.loads(docker(['inspect','--format','{{json .State}}',name]).stdout)
            assert not state['Running'] and state['Pid']==0,'Original driver query termination unconfirmed'
            docker(['rm',name])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor',action='store_true');parser.add_argument('--config')
    parser.add_argument('--metadata',action='store_true');parser.add_argument('--trace',action='store_true')
    parser.add_argument('--image');parser.add_argument('--abi-header');args=parser.parse_args()
    if args.actor:actor(args.config)
    else:parent(args)

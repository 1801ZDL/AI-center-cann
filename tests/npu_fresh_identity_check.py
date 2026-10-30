"""Allocated-card read-only identity/namespace comparison in new owned containers.

No ACL/torch imports, kernel, privileged container, host IPC/PID namespace,
global account, driver setting, existing-container change or inherited keys.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def actor(physical,drop_uid=None):
    initial_uid=os.getuid()
    if drop_uid is not None:
        assert initial_uid==0 and drop_uid==23901
        os.setgroups([1001]);os.setgid(drop_uid);os.setuid(drop_uid)
    import pwd,grp
    identity={}
    for line in Path('/proc/self/status').read_text().splitlines():
        key,_,value=line.partition(':')
        if key in {'Uid','Gid','Groups','CapEff','CapPrm','NoNewPrivs','Seccomp'}:identity[key]=value.strip()
    try:user_name=pwd.getpwuid(os.getuid()).pw_name
    except KeyError:user_name=None
    try:group_name=grp.getgrgid(1001).gr_name
    except KeyError:group_name=None
    assert identity['CapEff']=='0000000000000000' and identity['CapPrm']=='0000000000000000'
    assert identity['NoNewPrivs']=='1'
    home=Path(tempfile.mkdtemp(prefix='npu-query-',dir='/tmp'))
    environment={'PATH':'/usr/local/sbin:/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(home),
        'LD_LIBRARY_PATH':'/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:/usr/local/dcmi',
        'ASCEND_PROCESS_LOG_PATH':str(home/'logs'),'ASCEND_WORK_PATH':str(home/'work'),'PYTHONDONTWRITEBYTECODE':'1'}
    started=time.monotonic()
    result=subprocess.run(['/usr/local/sbin/npu-smi','info'],capture_output=True,text=True,env=environment,timeout=15)
    raw=result.stdout+'\n'+result.stderr
    opened=None;error=None
    try:
        fd=os.open('/dev/davinci'+str(physical),os.O_RDWR|os.O_NONBLOCK)
        os.close(fd);opened=True
    except OSError as exception:
        opened=False;error=exception.errno
    output={'initial_uid':initial_uid,'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),
        'user_name':user_name,'driver_group_name':group_name,'identity':identity,
        'management_returncode':result.returncode,'management_output_sha256':hashlib.sha256(raw.encode()).hexdigest(),
        'management_error_codes':sorted(set(re.findall(r'(?:ret\s*=\s*|ret is\s+)(-?\d+)',raw))),
        'seconds':time.monotonic()-started,'device_opened':opened,'device_open_errno':error,
        'physical_files':sorted(int(p.name[7:]) for p in Path('/dev').glob('davinci[0-9]*') if p.name[7:].isdigit()),
        'namespaces':{key:os.readlink('/proc/self/ns/'+key) for key in ['mnt','pid','ipc','user']},
        'acl_initialized':False,'npu_kernel_launches':0,'other_process_identifiers_exported':False}
    print(json.dumps(output),flush=True)


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from lab_client import read_json,atomic_json,now
    from npu_admission import query,selected_reading
    from docker_runtime import discover,command
    assert os.environ.get('LAB_JOB_DIR'),'Submit this fixed diagnostic through the trusted queue'
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json')
    assert job['resources']['npu']==1 and len(job['allocation']['npu_ids'])==1
    physical=job['allocation']['npu_ids'][0];cpus=','.join(map(str,job['allocation']['cpu_ids']))
    base=directory/'tmp/fresh-identity';base.mkdir(mode=0o700)
    program=base/'probe.py';program.write_bytes(Path(__file__).read_bytes());program.chmod(0o444)
    binding,_=discover();image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    tag='ascend-lab-npu-identity-'+job['id'][4:];created=[];cases=[]
    def docker(arguments,check=True,timeout=25):
        argv,env=command(binding,['docker',*arguments])
        result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned diagnostic container operation failed: '+result.stderr[-600:])
        return result
    def idle():
        devices,processes=query();selected_reading([physical],(devices,processes))
        return [{key:d.get(key) for key in ['id','name','health','utilization_pct']} for d in devices if d['id']==physical]
    import pwd
    driver_uid=pwd.getpwnam('HwHiAiUser').pw_uid
    assert driver_uid not in {0,1104,23901}
    nss=base/'nss';nss.mkdir(mode=0o700)
    (nss/'passwd').write_text('root:x:0:0:root:/tmp:/usr/sbin/nologin\n'+
        'HwHiAiUser:x:'+str(driver_uid)+':1001:driver:/tmp:/usr/sbin/nologin\n'+
        'm00951283:x:1104:1104:demo owner:/tmp:/usr/sbin/nologin\n'+
        'demo_npu_task:x:23901:23901:demo task:/tmp:/usr/sbin/nologin\n')
    (nss/'group').write_text('root:x:0:\nHwHiAiUser:x:1001:m00951283,demo_npu_task\nm00951283:x:1104:\ndemo_npu_task:x:23901:\n')
    for file in nss.iterdir():file.chmod(0o444)
    variants=[('numeric-owner','1104:1001',False,None),('named-owner','1104:1001',True,None),
              ('named-task','23901:23901',True,None),('root-then-drop','0:0',True,23901),('root-query-only','0:0',True,None)]
    try:
        for label,user,named,drop in variants:
            before=idle();name=tag+'-'+label
            devices=['/dev/davinci'+str(physical),'/dev/davinci_manager']
            devices.extend(p for p in ['/dev/hisi_hdc','/dev/devmm_svm','/dev/svm0'] if Path(p).exists())
            args=['create','--name',name,'--label','io.ascend-lab.npu-identity='+tag,'--user',user,'--group-add','1001',
                '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--memory','128m','--memory-swap','128m','--cpus','0.25','--cpuset-cpus',cpus,'--pids-limit','48',
                '--ipc','none','--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--log-opt','max-size=1m','--log-opt','max-file=1',
                '--mount','type=bind,src='+str(program)+',dst=/probe.py,readonly',
                '--mount','type=bind,src=/usr/local/sbin/npu-smi,dst=/usr/local/sbin/npu-smi,readonly']
            if drop is not None:args+=['--cap-add','SETUID','--cap-add','SETGID']
            for device in devices:args+=['--device',device+':'+device+':rwm']
            for path in ['/usr/local/Ascend/driver','/usr/local/dcmi','/etc/ascend_install.info','/etc/vnpu.cfg']:
                if Path(path).exists():args+=['--mount','type=bind,src='+path+',dst='+path+',readonly']
            if named:
                for file in ['passwd','group']:args+=['--mount','type=bind,src='+str(nss/file)+',dst=/etc/'+file+',readonly']
            args+=['--entrypoint','/usr/bin/timeout',image,'--kill-after=2s','25s','/usr/bin/python3','-I','-B','/probe.py','--actor',str(physical)]
            if drop is not None:args+=['--drop-uid',str(drop)]
            created.append(name);atomic_json(base/'containers.json',{'tag':tag,'names':created})
            docker(args);result=docker(['start','--attach',name],check=False,timeout=35)
            (base/(label+'-stderr.txt')).write_text(result.stderr)
            details=json.loads(docker(['inspect',name]).stdout)[0]
            assert details['Config']['Labels']['io.ascend-lab.npu-identity']==tag
            assert details['State']['Pid']==0 and not details['State']['Running']
            if result.returncode or details['State']['ExitCode']:
                atomic_json(base/(label+'-error.json'),{'cli_exit':result.returncode,'container_exit':details['State']['ExitCode'],'stderr':result.stderr[-3000:]})
                raise RuntimeError('Fixed diagnostic actor failed before results: '+label)
            record=json.loads(result.stdout);assert record['physical_files']==[physical]
            record.update(label=label,named_nss=named,container_id=details['Id'],started_at=details['State']['StartedAt'],
                exit_code=details['State']['ExitCode'],read_only_root=details['HostConfig']['ReadonlyRootfs'],
                network=details['HostConfig']['NetworkMode'],ipc=details['HostConfig']['IpcMode'],privileged=details['HostConfig']['Privileged'],before=before,after=idle())
            cases.append(record);atomic_json(base/'observations.json',{'cases':cases})
            docker(['rm',name])
        report={'diagnostic_completed':True,'verified_at':now(),'job_id':job['id'],'physical_id':physical,'image':image,
            'cases':cases,'acl_initialized':False,'npu_kernel_launches':0,'existing_containers_modified':False,
            'scope':'Fresh restricted read-only management/raw-open comparison; no runtime initialization, numerical or performance acceptance'}
        atomic_json(directory/'results/fresh-npu-identity.json',report);print(json.dumps(report,indent=2))
    finally:
        for name in created:
            response=docker(['inspect',name],check=False)
            if response.returncode:continue
            details=json.loads(response.stdout)[0];assert details['Config']['Labels']['io.ascend-lab.npu-identity']==tag
            if details['State']['Running']:docker(['stop','--time','3',name])
            details=json.loads(docker(['inspect',name]).stdout)[0];assert details['State']['Pid']==0 and not details['State']['Running']
            docker(['rm',name])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor',type=int);parser.add_argument('--drop-uid',type=int);args=parser.parse_args()
    if args.actor is not None:actor(args.actor,args.drop_uid)
    else:parent()

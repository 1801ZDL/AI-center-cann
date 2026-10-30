"""Actual ext4 user byte/inode quotas for two UIDs on one owned 128MiB disk.

The trusted quota-control container needs SYS_ADMIN and an unconfined seccomp
profile for quotactl_fd; fixed actors drop all identities/capabilities. No socket,
network, host namespaces, NPU or physical disks are exposed to that controller.
This is provider research/acceptance, not yet a production workspace provider.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import uuid


def actor(uid,operation):
    assert os.getuid()==uid and uid in {23801,23802}
    sys.path.insert(0,'/payload');import filesystem_quota as quota
    status={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    assert status['CapEff']==status['CapPrm']=='0000000000000000' and status['NoNewPrivs']=='1'
    root=Path('/quota')/('alice' if uid==23801 else 'bob');results={}
    if operation=='bytes':
        descriptor=os.open(root/'payload.bin',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        written=0;error=None
        try:
            while written<32*1024**2:
                try:written+=os.write(descriptor,b'q'*(256*1024))
                except OSError as exception:error=exception.errno;break
            try:os.fsync(descriptor)
            except OSError as exception:error=error or exception.errno
        finally:os.close(descriptor)
        results={'written':written,'errno':error}
    elif operation=='inodes':
        count=0;error=None
        for index in range(512):
            try:
                descriptor=os.open(root/('inode-'+str(index)),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                os.close(descriptor);count+=1
            except OSError as exception:error=exception.errno;break
        results={'created':count,'errno':error}
    elif operation=='peer':
        path=root/'peer-written.bin'
        with path.open('wb') as stream:stream.write(b'b'*(1024**2));stream.flush();os.fsync(stream.fileno())
        results={'bytes':path.stat().st_size}
    elif operation=='bypass':
        descriptor=os.open('/quota',os.O_RDONLY|os.O_DIRECTORY)
        record=quota.DiskQuota(block_hard=64*1024,block_soft=64*1024,inode_hard=4096,inode_soft=4096,valid=5)
        try:
            try:quota.quota_call(descriptor,quota.Q_SETQUOTA,uid,record);results['quota_raise_errno']=0
            except OSError as exception:results['quota_raise_errno']=exception.errno
        finally:os.close(descriptor)
        path=root/'owner-check';path.touch(mode=0o600)
        try:
            try:os.chown(path,0,0);results['chown_root_errno']=0
            except OSError as exception:results['chown_root_errno']=exception.errno
        finally:path.unlink()
        try:os.link('/quota/bob/private.txt',root/'peer-link');results['cross_user_link_errno']=0
        except OSError as exception:results['cross_user_link_errno']=exception.errno
    else:raise ValueError('Unknown fixed actor operation')
    print(json.dumps({'uid':uid,'operation':operation,'cap_effective':status['CapEff'],
       'cap_permitted':status['CapPrm'],'no_new_privileges':status['NoNewPrivs'],'seccomp':status['Seccomp'],**results}),flush=True)


def controller():
    assert os.geteuid()==0
    sys.path.insert(0,'/payload');import filesystem_quota as quota
    root=Path('/quota');fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    fs=os.fstatvfs(fd);assert fs.f_flag&os.ST_NOSUID and fs.f_flag&os.ST_NODEV
    assert fs.f_blocks*fs.f_frsize>=100*1024**2
    root.chmod(0o755)
    limits={23801:(16*1024**2,128),23802:(24*1024**2,192)}
    original={};records=[]
    try:
        enforcement_before=quota.state(fd);assert enforcement_before['accounting'] and enforcement_before['enforced']
        for uid,(byte_limit,inodes) in limits.items():
            original[uid]=quota.provision(fd,uid,byte_limit,inodes)
            directory=root/('alice' if uid==23801 else 'bob');directory.mkdir(mode=0o700);os.chown(directory,uid,uid)
        private=root/'bob/private.txt';private.write_bytes(b'peer-private');private.chmod(0o600);os.chown(private,23802,23802)
        def run(uid,operation):
            def prepare():
                os.setgroups([]);os.setgid(uid);os.setuid(uid);os.umask(0o077)
                resource.setrlimit(resource.RLIMIT_CORE,(0,0))
            result=subprocess.run(['/usr/bin/python3','-I','-B','/payload/probe.py','--actor',str(uid),'--operation',operation],
                env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/tmp'},capture_output=True,text=True,preexec_fn=prepare,timeout=25)
            if result.returncode:raise RuntimeError('Fixed actor failed: '+result.stderr[-1500:])
            value=json.loads(result.stdout);records.append(value);return value
        bypass=run(23801,'bypass')
        assert bypass['quota_raise_errno']==errno.EPERM and bypass['chown_root_errno']==errno.EPERM
        assert bypass['cross_user_link_errno'] in {errno.EACCES,errno.EPERM}
        quota.require(fd,23801,*limits[23801])
        bob_before=quota.read(fd,23802);byte_result=run(23801,'bytes');alice_bytes=quota.read(fd,23801);bob_after=quota.read(fd,23802)
        assert byte_result['errno']==errno.EDQUOT and 0<byte_result['written']<=limits[23801][0]
        assert alice_bytes['bytes_used']<=limits[23801][0] and bob_before==bob_after
        (root/'alice/payload.bin').unlink()
        inode_result=run(23801,'inodes');alice_inodes=quota.read(fd,23801)
        assert inode_result['errno']==errno.EDQUOT and alice_inodes['inodes_used']==limits[23801][1]
        peer=run(23802,'peer');assert peer['bytes']==1024**2
        actual=quota.require(fd,23802,*limits[23802]);space=os.fstatvfs(fd)
        assert space.f_bavail*space.f_frsize>64*1024**2 and space.f_favail>1024
        wrong_limit_rejected=False
        try:quota.require(fd,23802,limits[23802][0]+1024,limits[23802][1])
        except ValueError:wrong_limit_rejected=True
        assert wrong_limit_rejected
        disable_errno=None;inactive_rejected=False
        try:quota.quota_call(fd,0x800003,0,None)
        except OSError as exception:disable_errno=exception.errno
        if disable_errno is None:
            try:quota.require(fd,23801,*limits[23801])
            except OSError as exception:inactive_rejected=exception.errno in {errno.ESRCH,errno.EINVAL}
            except ValueError:inactive_rejected=True
            assert inactive_rejected
        report={'passed':True,'actor_records':records,'limits':original,'alice_byte_quota':alice_bytes,
            'alice_inode_quota':alice_inodes,'bob_after_peer_write':actual,'bob_unchanged_by_alice':bob_before==bob_after,
            'same_filesystem':True,'filesystem_free_bytes_after_tests':space.f_bavail*space.f_frsize,
            'filesystem_free_inodes_after_tests':space.f_favail,'wrong_limit_rejected':wrong_limit_rejected,
            'quota_disable_errno':disable_errno,'inactive_quota_rejected':inactive_rejected,
            'enforcement_before':enforcement_before,'enforcement_after_disable':quota.state(fd) if disable_errno is None else None,
            'quota_probe_has_no_statvfs_fallback':True,'kernel':os.uname().release,
            'scope':'Fixed unprivileged actors and kernel user quotas on one owned image; provider not registered in production admission'}
        print(json.dumps(report,indent=2),flush=True)
    finally:os.close(fd)


def parent():
    ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
    from lab_client import read_json,atomic_json,now
    from disk_workspace_fixture import OwnedDisk
    from docker_runtime import discover,command
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json');assert job['resources']['npu']==0
    cpus=job['allocation']['cpu_ids'];base=directory/'tmp/user-quota';base.mkdir(mode=0o700)
    payload=base/'payload';payload.mkdir(mode=0o755)
    for source,name in [(Path(__file__),'probe.py'),(ROOT/'filesystem_quota.py','filesystem_quota.py')]:
        target=payload/name;target.write_bytes(source.read_bytes());target.chmod(0o444)
    payload.chmod(0o555)
    binding,_=discover()
    def docker(args,check=True,input=None,timeout=30):
        argv,env=command(binding,['docker',*args]);result=subprocess.run(argv,env=env,capture_output=True,text=True,input=input,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned quota operation failed: '+result.stderr[-1800:])
        return result
    prefix='ascend-lab-quota-'+uuid.uuid4().hex[:8];image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    disk=OwnedDisk(base,prefix,image,cpus,docker,folder_name='quota-workspace',image_mib=128,inodes=2048,quota_type='user')
    volume=prefix+'-volume';name=prefix+'-control';volume_created=False;container_created=False;report=None
    try:
        device=disk.create()
        docker(['volume','create','--label','io.ascend-lab.quota-proof='+prefix,'--driver','local','--opt','type=ext4',
                '--opt','device='+device,'--opt','o=nosuid,nodev,usrquota',volume]);volume_created=True
        docker(['create','--name',name,'--label','io.ascend-lab.quota-proof='+prefix,'--user','0:0','--network','none','--ipc','none',
           '--read-only','--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','SETUID','--cap-add','SETGID',
           '--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined',
           '--memory','256m','--memory-swap','256m','--cpus','0.75','--cpuset-cpus',str(cpus[0]),'--pids-limit','48',
           '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--log-opt','max-size=1m','--log-opt','max-file=1',
           '--mount','type=volume,src='+volume+',dst=/quota', '--mount','type=bind,src='+str(payload)+',dst=/payload,readonly',
           '--entrypoint','/usr/bin/timeout',image,'--kill-after=3s','100s','/usr/bin/python3','-I','-B','/payload/probe.py','--controller'])
        container_created=True;result=docker(['start','--attach',name],check=False,timeout=110)
        details=json.loads(docker(['inspect',name]).stdout)[0]
        (base/'control-stderr.log').write_text(result.stderr)
        assert not details['State']['Running'] and details['State']['Pid']==0
        if result.returncode or details['State']['ExitCode']:
            (base/'control-stdout.log').write_text(result.stdout)
            raise RuntimeError('Quota control failed: '+result.stderr[-2000:])
        report=json.loads(result.stdout);assert report['passed']
        report.update(parent_job=job['id'],verified_at=now(),npu_kernel_launches=0,
                      root_control_seccomp_unconfined=True,no_docker_socket=True,no_host_namespaces=True)
    finally:
        if container_created:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.quota-proof']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            info=json.loads(docker(['inspect',name]).stdout)[0];assert not info['State']['Running'] and info['State']['Pid']==0
            docker(['rm',name])
        if volume_created:docker(['volume','rm',volume])
        disk.cleanup()
    assert report is not None
    report['disk']=disk.report;report['cleanup_confirmed']=disk.report['loop_association_released'] and disk.report['helper_removed']
    atomic_json(directory/'results/user-quota.json',report);print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor',type=int);parser.add_argument('--operation');parser.add_argument('--controller',action='store_true');args=parser.parse_args()
    if args.actor is not None:actor(args.actor,args.operation)
    elif args.controller:controller()
    else:parent()

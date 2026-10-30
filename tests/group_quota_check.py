"""Real group quota and SGID feasibility for one unprivileged central writer.

Runs only as a queued fixed acceptance on an owned file-backed filesystem.
No host accounts/groups, physical devices, existing containers or NPU changes.
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

WRITER_UID=23820
GROUP_A=23831
GROUP_B=23832


def actor(operation):
    sys.path.insert(0,'/payload');import group_quota as quota
    status={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    assert os.geteuid()==WRITER_UID and status['CapEff']==status['CapPrm']=='0000000000000000'
    assert status['NoNewPrivs']=='1' and set(os.getgroups())=={GROUP_A,GROUP_B}
    root=Path('/quota/central');result={}
    with_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:
        if operation=='setup':
            for owner,gid in [('alice',GROUP_A),('bob',GROUP_B)]:
                target=root/owner;target.mkdir(mode=0o700)
                fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY)
                try:quota.assign_empty(fd,gid)
                finally:os.close(fd)
                nested=target/'nested';nested.mkdir(mode=0o700);file=nested/'inherit.txt';file.write_bytes(b'inherited group')
                assert target.stat().st_gid==nested.stat().st_gid==file.stat().st_gid==gid
                assert nested.stat().st_mode&0o2000
                result[owner]={'gid':gid,'nested_sgid':True,'file_group_inherited':True}
        elif operation in {'bytes','inodes'}:
            target=root/'alice'
            if operation=='bytes':
                fd=os.open(target/'fill.bin',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);written=0;code=None
                try:
                    while written<32*1024**2:
                        try:written+=os.write(fd,b'x'*(256*1024))
                        except OSError as error:code=error.errno;break
                    if code is None:os.fsync(fd)
                finally:os.close(fd)
                result={'written':written,'errno':code}
            else:
                (target/'fill.bin').unlink();created=0;code=None
                while created<512:
                    try:(target/('item-'+str(created))).touch(mode=0o600)
                    except OSError as error:code=error.errno;break
                    created+=1
                result={'files_created':created,'errno':code}
        elif operation in {'peer-after-bytes','peer-after-inodes'}:
            target=root/'bob'/(operation+'.bin')
            with target.open('wb') as stream:stream.write(b'b'*(1024**2));stream.flush();os.fsync(stream.fileno())
            result={'bytes':target.stat().st_size,'gid':target.stat().st_gid}
        elif operation=='permissions':
            record=quota.DiskQuota(block_hard=64*1024,block_soft=64*1024,inode_hard=512,inode_soft=512,valid=5)
            try:quota.quota_call(with_fd,quota.Q_SETQUOTA,GROUP_A,record);raise AssertionError('Unprivileged writer raised quota')
            except OSError as error:result['raise_limit_errno']=error.errno
            try:quota.read(with_fd,23833);raise AssertionError('Writer queried a foreign group')
            except OSError as error:result['foreign_group_query_errno']=error.errno
            # A trusted owner can change GIDs among its groups. This is not a
            # sandbox for user code; user code must never get this service UID.
            path=root/'alice'/'retag-check';path.touch(mode=0o600);os.chown(path,-1,GROUP_B)
            result['trusted_writer_can_retag_owned_file']=path.stat().st_gid==GROUP_B;path.unlink()
        else:raise ValueError('Unknown fixed operation')
        result['own_group_read']=quota.read(with_fd,GROUP_A)
    finally:os.close(with_fd)
    print(json.dumps({'operation':operation,'uid':os.geteuid(),'groups':os.getgroups(),
        'cap_effective':status['CapEff'],'cap_permitted':status['CapPrm'],'no_new_privileges':status['NoNewPrivs'],
        'seccomp':status['Seccomp'],**result}),flush=True)


def controller():
    assert os.geteuid()==0
    sys.path.insert(0,'/payload');import group_quota as quota
    root=Path('/quota');root.chmod(0o755)
    central=root/'central';central.mkdir(mode=0o700);os.chown(central,WRITER_UID,WRITER_UID)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY);records=[]
    try:
        limits={GROUP_A:(16*1024**2,128),GROUP_B:(24*1024**2,192)}
        before=quota.state(fd);assert before['accounting'] and before['enforced']
        provisioned={gid:quota.provision(fd,gid,*limit) for gid,limit in limits.items()}
        def run(operation):
            def drop():
                os.setgroups([GROUP_A,GROUP_B]);os.setgid(WRITER_UID);os.setuid(WRITER_UID);os.umask(0o077)
                resource.setrlimit(resource.RLIMIT_CORE,(0,0))
            result=subprocess.run(['/usr/bin/python3','-I','-B','/payload/probe.py','--actor',operation],
                capture_output=True,text=True,preexec_fn=drop,timeout=25,
                env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/tmp'})
            if result.returncode:raise RuntimeError('Fixed central writer failed: '+result.stderr[-1800:])
            record=json.loads(result.stdout);records.append(record);return record
        run('setup');permissions=run('permissions')
        assert permissions['raise_limit_errno']==permissions['foreign_group_query_errno']==errno.EPERM
        assert permissions['trusted_writer_can_retag_owned_file']
        bob_before=quota.read(fd,GROUP_B);a_bytes=run('bytes');a_byte_quota=quota.require(fd,GROUP_A,*limits[GROUP_A])
        assert a_bytes['errno']==errno.EDQUOT and 0<a_bytes['written']<=limits[GROUP_A][0]
        assert quota.read(fd,GROUP_B)==bob_before
        assert run('peer-after-bytes')['bytes']==1024**2
        a_inodes=run('inodes');a_inode_quota=quota.require(fd,GROUP_A,*limits[GROUP_A])
        assert a_inodes['errno']==errno.EDQUOT and a_inode_quota['inodes_used']==128
        assert run('peer-after-inodes')['bytes']==1024**2
        def outsider_drop():os.setgroups([]);os.setgid(23821);os.setuid(23821)
        denied=subprocess.run(['/usr/bin/python3','-I','-c','import os;os.listdir("/quota/central")'],
            preexec_fn=outsider_drop,capture_output=True,text=True,timeout=5)
        assert denied.returncode!=0 and 'PermissionError' in denied.stderr
        fs=os.fstatvfs(fd);assert fs.f_bavail*fs.f_frsize>64*1024**2 and fs.f_favail>1024
        bob_after=quota.require(fd,GROUP_B,*limits[GROUP_B])
        wrong_limit_rejected=False
        try:quota.require(fd,GROUP_A,limits[GROUP_A][0]+1024,128)
        except ValueError:wrong_limit_rejected=True
        assert wrong_limit_rejected
        quota.quota_call(fd,0x800003,0,None);after=quota.state(fd);assert after['accounting'] and not after['enforced']
        inactive_rejected=False
        try:quota.require(fd,GROUP_A,*limits[GROUP_A])
        except ValueError:inactive_rejected=True
        assert inactive_rejected
        print(json.dumps({'passed':True,'actor_records':records,'writer_uid':WRITER_UID,'storage_gids':[GROUP_A,GROUP_B],
            'provisioned':provisioned,'alice_byte_quota':a_byte_quota,'alice_inode_quota':a_inode_quota,'bob_after':bob_after,
            'peer_writes_while_other_full':True,'outsider_private_directory_denied':True,
            'filesystem_free_bytes':fs.f_bavail*fs.f_frsize,'filesystem_free_inodes':fs.f_favail,
            'wrong_limit_rejected':wrong_limit_rejected,'inactive_enforcement_rejected':inactive_rejected,
            'enforcement_before':before,'enforcement_after_disable':after,'kernel':os.uname().release,
            'scope':'Fixed trusted central writer with no capabilities and two supplementary storage groups. Not yet integrated into payload APIs/install/restore; group membership is not an untrusted task sandbox.'},indent=2),flush=True)
    finally:os.close(fd)


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
    from lab_client import read_json,atomic_json,now
    from disk_workspace_fixture import OwnedDisk
    from docker_runtime import discover,command
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json');assert job['resources']['npu']==0
    cpus=job['allocation']['cpu_ids'];base=directory/'tmp/group-quota';base.mkdir(mode=0o700)
    payload=base/'payload';payload.mkdir(mode=0o755)
    for source,name in [(Path(__file__),'probe.py'),(root/'filesystem_quota.py','filesystem_quota.py'),(root/'group_quota.py','group_quota.py')]:
        target=payload/name;target.write_bytes(source.read_bytes());target.chmod(0o444)
    payload.chmod(0o555);binding,_=discover()
    def docker(args,check=True,input=None,timeout=30):
        argv,env=command(binding,['docker',*args]);result=subprocess.run(argv,env=env,capture_output=True,text=True,input=input,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned group-quota operation failed: '+result.stderr[-1800:])
        return result
    prefix='ascend-lab-groupquota-'+uuid.uuid4().hex[:8];image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    disk=OwnedDisk(base,prefix,image,cpus,docker,folder_name='quota-workspace',image_mib=128,inodes=2048,quota_type='group')
    volume=prefix+'-volume';name=prefix+'-control';volume_created=False;container_created=False;report=None
    try:
        device=disk.create()
        docker(['volume','create','--label','io.ascend-lab.groupquota-proof='+prefix,'--driver','local','--opt','type=ext4',
            '--opt','device='+device,'--opt','o=nosuid,nodev,grpquota',volume]);volume_created=True
        docker(['create','--name',name,'--label','io.ascend-lab.groupquota-proof='+prefix,'--user','0:0','--network','none','--ipc','none',
            '--read-only','--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','SETUID','--cap-add','SETGID',
            '--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined',
            '--memory','256m','--memory-swap','256m','--cpus','0.75','--cpuset-cpus',str(cpus[0]),'--pids-limit','48',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--log-opt','max-size=1m','--log-opt','max-file=1',
            '--mount','type=volume,src='+volume+',dst=/quota','--mount','type=bind,src='+str(payload)+',dst=/payload,readonly',
            '--entrypoint','/usr/bin/timeout',image,'--kill-after=3s','100s','/usr/bin/python3','-I','-B','/payload/probe.py','--controller'])
        container_created=True;result=docker(['start','--attach',name],check=False,timeout=110)
        details=json.loads(docker(['inspect',name]).stdout)[0];(base/'control-stderr.log').write_text(result.stderr)
        assert not details['State']['Running'] and details['State']['Pid']==0
        if result.returncode or details['State']['ExitCode']:
            (base/'control-stdout.log').write_text(result.stdout)
            raise RuntimeError('Group-quota fixed control failed: '+result.stderr[-1800:])
        report=json.loads(result.stdout);assert report['passed']
        report.update(parent_job=job['id'],verified_at=now(),npu_kernel_launches=0,
            root_control_seccomp_unconfined=True,no_docker_socket=True,no_host_namespaces=True,
            host_accounts_or_groups_created=False,source_sha256={name:__import__('hashlib').sha256((payload/name).read_bytes()).hexdigest() for name in ['probe.py','filesystem_quota.py','group_quota.py']})
    finally:
        if container_created:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.groupquota-proof']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            info=json.loads(docker(['inspect',name]).stdout)[0];assert not info['State']['Running'] and info['State']['Pid']==0
            docker(['rm',name])
        if volume_created:docker(['volume','rm',volume])
        disk.cleanup()
    assert report is not None
    report['disk']=disk.report;report['cleanup_confirmed']=disk.report['loop_association_released'] and disk.report['helper_removed']
    atomic_json(directory/'results/group-quota.json',report);print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor');parser.add_argument('--controller',action='store_true');args=parser.parse_args()
    if args.actor:actor(args.actor)
    elif args.controller:controller()
    else:parent()

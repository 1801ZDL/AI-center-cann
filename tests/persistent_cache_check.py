"""Native cache-provider acceptance on one owned ext4 image, through the queue.

The fixed trusted Root fixture has no socket/network/host namespaces/NPU. Its
actors run with private UIDs, no capabilities and only a pre-opened cache cwd.
This does not claim the provider is integrated into production job dispatch.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import sys
import uuid


def actor(operation):
    assert os.getuid() in {23801,23802}
    status={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    assert status['CapEff']==status['CapPrm']=='0000000000000000' and status['NoNewPrivs']=='1'
    result={'uid':os.getuid(),'operation':operation,'cap_effective':status['CapEff'],'no_new_privileges':True}
    if operation=='build':
        output=Path('canonical.mlir');count=Path('invocations.json')
        if output.exists():
            calls=json.loads(count.read_text())['calls'];assert calls==1;result['compiled']=False
        else:
            tool=os.environ['LAB_CACHE_TOOL'];assert Path(tool).is_absolute() and Path(tool).is_file()
            subprocess.run([tool,'--canonicalize','--cse','/payload/input.mlir','-o','canonical.mlir.partial'],check=True)
            Path('canonical.mlir.partial').rename(output);calls=1;count.write_text(json.dumps({'calls':calls}));result['compiled']=True
        content=output.read_bytes();assert b'arith.constant 42' in content and b'arith.addi' not in content
        result.update(calls=calls,bytes=len(content),sha256=hashlib.sha256(content).hexdigest())
    elif operation=='bytes':
        fd=os.open('pressure.bin',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);written=0;failure=None
        try:
            for chunk in [1024**2,4096]:
                while written<32*1024**2:
                    try:os.posix_fallocate(fd,written,chunk);written+=chunk
                    except OSError as error:failure=error.errno;break
            os.fsync(fd)
        finally:os.close(fd)
        assert failure==errno.EDQUOT;result.update(errno=failure,allocated_bytes=written)
    elif operation=='inodes':
        created=0;failure=None
        for n in range(256):
            try:fd=os.open('inode-'+str(n),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd);created+=1
            except OSError as error:failure=error.errno;break
        assert failure==errno.EDQUOT;result.update(errno=failure,created=created)
    elif operation=='unlink-pressure':Path('pressure.bin').unlink()
    elif operation=='peer-hidden':
        try:Path('/quota').iterdir().__next__();raise AssertionError('Peer quota parent was readable')
        except PermissionError:result['peer_parent_denied']=True
    elif operation=='symlink':Path('foreign-sentinel').symlink_to('/state/sentinel')
    else:raise ValueError('Unknown fixed cache operation')
    print(json.dumps(result),flush=True)


def controller(parent_id,attempt_id,image):
    assert os.geteuid()==0
    sys.path.insert(0,'/payload')
    from quota_workspace import prepare
    from persistent_cache import Pool,Lease,CacheBusy,CacheUnavailable,fingerprint,namespace
    from environment_contract import fingerprint as env_fingerprint
    from lab_client import atomic_json
    from filesystem_quota import require
    state=Path('/state/control');state.mkdir(mode=0o700);Path('/state/sentinel').write_bytes(b'do not follow user links')
    quotas=[{'uid':23801,'bytes':16*1024**2,'inodes':128},{'uid':23802,'bytes':24*1024**2,'inodes':192}]
    Path('/quota').chmod(0o700)
    policy=prepare('/quota',quotas)['policy'];slots={s['quota_uid']:s for s in policy['slots']}
    tool=shutil.which('bishengir-opt');assert tool
    tool_path=str(Path(tool).resolve());tool_sha=hashlib.sha256(Path(tool_path).read_bytes()).hexdigest()
    setup='/usr/local/Ascend/ascend-toolkit/set_env.sh';assert Path(setup).is_file()
    config={'node_id':'worker-'+'1'*12,'physical_id':'a'*64,'image':image,'python':sys.executable,'environment_setup':setup,
        'users':{'alice':{'uid':23801,'gid':23801},'bob':{'uid':23802,'gid':23802}},'persistent_cache':policy}
    # Administrator-approved fixture facts are measured in this exact image.
    # This is not a controller-issued protected environment certification.
    config['environment_manifest']={'format':'ascend-lab-node-environment/v1','image_sha256':image,'environment_setup':setup,
        'observed':{'image_sha256':image,'python':platform.python_version(),'python_executable':sys.executable,'machine':platform.machine(),
            'packages':{},'tools':{'bishengir-opt':{'realpath':tool_path,'sha256':tool_sha}},'files':{}},
        'source':{'job_id':parent_id,'attempt_id':attempt_id,'receipt_sha256':hashlib.sha256(Path('/payload/probe.py').read_bytes()).hexdigest(),'node_revision':1}}
    untrusted=Path('/state/untrusted');untrusted.mkdir(mode=0o700);os.chown(untrusted,23801,23801)
    try:Pool(untrusted,config).initialize();raise AssertionError('Cache control initialized in a foreign-owned directory')
    except CacheUnavailable:pass
    assert not list(untrusted.iterdir());untrusted.rmdir()
    pool=Pool(state,config);pool.initialize();records=[];checks=['Foreign-owned control directory is rejected before any initialization writes']
    runtime={'provider.py':hashlib.sha256(Path('/payload/persistent_cache.py').read_bytes()).hexdigest()}
    def grant(owner='alice',number=1,environment=None):
        uid=config['users'][owner]['uid'];slot=slots[uid]
        value={'owner':owner,'job_id':'job-'+format(number,'012x'),'attempt_id':'attempt-'+format(number,'032x'),
            'lease_token':'fixed-private-cache-lease-'+str(number),'environment_sha256':env_fingerprint(config['environment_manifest']),
            'runtime_files':runtime,'parameters':{'lock':{'python':platform.python_version(),'tools':{'bishengir-opt':tool_sha}},'environment':environment or {}},
            'cache_policy_sha256':fingerprint(policy),'allocation':{}}
        value['allocation']['cache']={'slot_id':slot['id'],'storage_id':slot['storage_id'],'key':namespace(config,value)}
        return value
    def run(lease,operation):
        uid=config['users'][lease.grant['owner']]['uid'];fd=os.open(lease.path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        def identity():
            os.fchdir(fd);os.close(fd);os.setgroups([]);os.setgid(uid);os.setuid(uid);os.umask(0o077)
            resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        try:
            result=subprocess.run(['/bin/bash','--noprofile','--norc','-c','source "$1" >/dev/null 2>&1 || exit 126; shift; exec "$@"',
                'cache-fixture-env',setup,sys.executable,'-I','-B','/payload/probe.py','--actor',operation],preexec_fn=identity,pass_fds=(fd,),
                env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/tmp','LAB_CACHE_TOOL':tool_path},capture_output=True,text=True,timeout=20)
        finally:os.close(fd)
        if result.returncode:raise RuntimeError('Fixed cache actor failed: '+result.stderr[-1800:])
        value=json.loads(result.stdout);records.append(value);return value
    first=grant();lease=Lease(pool,first);identity=lease.prepare();cold=run(lease,'build');assert cold['compiled']
    checks.append('Actual non-root BishengIR canonicalization populates a UID-quota cache namespace')
    renewed=Lease(Pool(state,config),first);assert renewed.prepare()==identity
    warm=run(renewed,'build');assert not warm['compiled'] and warm['calls']==1 and warm['sha256']==cold['sha256']
    checks.append('Reconstructed provider resumes the original lease and reuses the exact IR without another invocation')
    other=Lease(pool,grant(number=2))
    for action in [other.prepare,lambda:other.release(True),lambda:pool.prune(slots[23801]['id'],lease.key)]:
        try:action();raise AssertionError('Active cache lease was bypassed')
        except CacheBusy:pass
    try:lease.release();raise AssertionError('Release without confirmed termination succeeded')
    except CacheUnavailable:pass
    checks.append('A second attempt, foreign release, GC and unconfirmed release cannot bypass the persistent lease')
    lease.release(True);other.prepare();again=run(other,'build');assert not again['compiled'];other.release(True)
    checks.append('A new confirmed attempt reuses the same owner/environment cache')
    changed=Lease(pool,grant(number=3,environment={'TRITON_DEBUG':'1'}));changed.prepare()
    assert changed.key!=lease.key and run(changed,'build')['compiled'];changed.release(True)
    checks.append('Changed compilation environment obtains a separate namespace')
    active=Lease(pool,grant(number=4));active.prepare();full=run(active,'bytes')
    reading=pool.observe();alice=next(v for v in reading['slots'] if v['id']==slots[23801]['id'])
    assert alice['status']=='ok' and alice['leased'] and alice['free_bytes']==0
    peer=Lease(pool,grant('bob',5));peer.prepare();peer_build=run(peer,'build')
    assert peer_build['compiled'] and run(peer,'peer-hidden')['peer_parent_denied'];peer.release(True)
    try:pool.prune(slots[23801]['id'],active.key);raise AssertionError('Full active cache was pruned')
    except CacheBusy:pass
    run(active,'unlink-pressure');inode=run(active,'inodes')
    with pool.anchor(slots[23801]) as (fd,base):
        quota=require(fd,23801,quotas[0]['bytes'],quotas[0]['inodes']);assert quota['inodes_used']==128
        fs=os.fstatvfs(fd);assert fs.f_favail>1024 and fs.f_bavail*fs.f_frsize>64*1024**2
    active.release(True)
    checks.append('Native per-owner byte and inode EDQUOT preserve control/lease and allow the peer to compile while one owner is full')
    old_plan=pool.prune(slots[23802]['id'],peer.key)['plan']
    peer2=Lease(pool,grant('bob',6));peer2.prepare();run(peer2,'symlink');peer2.release(True)
    try:pool.prune(slots[23802]['id'],peer.key,old_plan);raise AssertionError('Stale GC plan was accepted')
    except CacheUnavailable:pass
    plan=pool.prune(slots[23802]['id'],peer.key)['plan'];assert pool.prune(slots[23802]['id'],peer.key,plan)['removed']
    assert Path('/state/sentinel').read_bytes()==b'do not follow user links'
    checks.append('GC rejects stale generations and unlinks a user symlink without reading or deleting its foreign target')
    unknown=Lease(pool,grant('bob',10,environment={'TRITON_DEBUG':'1'}))
    unknown.path.mkdir(mode=0o700);unknown_info=unknown.path.lstat()
    try:unknown.prepare();raise AssertionError('Unknown Root-owned cache data was retagged')
    except CacheUnavailable:pass
    assert unknown.path.lstat().st_uid==0 and unknown.path.lstat().st_ino==unknown_info.st_ino and not list(unknown.path.iterdir())
    assert next(v for v in pool.observe()['slots'] if v['id']==slots[23802]['id'])['leased'] is False
    unknown.path.rmdir()
    checks.append('Unknown Root-owned namespace is preserved without ownership transfer or a new lease')
    plan=pool.prune(slots[23801]['id'],active.key)['plan'];assert pool.prune(slots[23801]['id'],active.key,plan)['removed']
    alice=next(v for v in pool.observe()['slots'] if v['id']==slots[23801]['id'])
    assert alice['status']=='ok' and not alice['leased'] and alice['free_inodes']>120
    checks.append('Explicit idle-namespace GC returns actual inode quota capacity; other environment namespace remains')
    subprocess.run(['umount','/quota'],check=True,capture_output=True)
    assert not list(Path('/quota').iterdir())
    assert all(v['status']=='error' and v['free_bytes']==v['free_inodes']==0 for v in pool.observe()['slots'])
    lost=Lease(pool,grant(number=7))
    try:lost.prepare();raise AssertionError('Missing native mount fell back to a directory')
    except CacheUnavailable:pass
    assert not list(Path('/quota').iterdir())
    subprocess.run(['mount','--bind','/quota-alias','/quota'],check=True,capture_output=True)
    assert all(v['status']=='ok' for v in pool.observe()['slots'])
    restored=Lease(pool,grant(number=8,environment={'TRITON_DEBUG':'1'}));restored.prepare()
    assert not run(restored,'build')['compiled'];restored.release(True)
    checks.append('Lost native mount reports zero capacity without fallback; original mount restores retained cache reuse')
    report={'passed':True,'checks':checks,'records':records,'cold_ir':cold,'warm_ir':warm,'byte_pressure':full,'inode_pressure':inode,
        'cache_policy':policy,'actual_tool_sha256':tool_sha,'source_sha256':runtime['provider.py'],
        'certification_scope':'Measured administrator fixture facts; not protected node environment certification',
        'dispatch_integrated':False,'node_backup_restore_accepted':False,'npu_kernel_launches':0,
        'scope':'Native cache provider and fixed UID actors only; no production scheduler, Docker /cache bootstrap, ccache or full Triton build acceptance'}
    print(json.dumps(report),flush=True)


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
    from lab_client import read_json,atomic_json,now
    from disk_workspace_fixture import OwnedDisk
    from docker_runtime import discover,command
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json');assert job['resources']['npu']==0
    base=directory/'tmp/persistent-cache';base.mkdir(mode=0o700);payload=base/'payload';payload.mkdir(mode=0o755)
    for source,name in [(Path(__file__),'probe.py'),*( (root/n,n) for n in ['persistent_cache.py','workspace_slots.py','quota_workspace.py','filesystem_quota.py','lab_client.py','environment_contract.py']),
                        (root/'examples/workflows/input.mlir','input.mlir')]:
        target=payload/name;target.write_bytes(source.read_bytes());target.chmod(0o444)
    payload.chmod(0o555);binding,_=discover();cpus=job['allocation']['cpu_ids']
    def docker(args,check=True,input=None,timeout=30):
        argv,env=command(binding,['docker',*args]);result=subprocess.run(argv,env=env,capture_output=True,text=True,input=input,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned cache operation failed: '+result.stderr[-1800:])
        return result
    prefix='ascend-lab-cache-'+uuid.uuid4().hex[:8];image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    disk=OwnedDisk(base,prefix,image,cpus,docker,folder_name='quota-workspace',image_mib=128,inodes=2048,quota_type='user')
    volume=prefix+'-volume';name=prefix+'-control';volume_created=False;container_created=False;report=None
    try:
        device=disk.create()
        docker(['volume','create','--label','io.ascend-lab.cache-proof='+prefix,'--driver','local','--opt','type=ext4',
            '--opt','device='+device,'--opt','o=nosuid,nodev,usrquota',volume]);volume_created=True
        docker(['create','--name',name,'--label','io.ascend-lab.cache-proof='+prefix,'--user','0:0','--network','none','--ipc','none',
            '--read-only','--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','SETUID','--cap-add','SETGID',
            '--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined','--security-opt','apparmor=unconfined',
            '--memory','512m','--memory-swap','512m','--cpus','0.75','--cpuset-cpus',str(cpus[0]),'--pids-limit','48',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--tmpfs','/state:rw,nosuid,nodev,size=16m,mode=0700',
            '--log-opt','max-size=1m','--log-opt','max-file=1',
            '--mount','type=volume,src='+volume+',dst=/quota', '--mount','type=volume,src='+volume+',dst=/quota-alias',
            '--mount','type=bind,src='+str(payload)+',dst=/payload,readonly','--entrypoint','/usr/bin/timeout',image,
            '--kill-after=3s','100s','/usr/bin/python3','-I','-B','/payload/probe.py','--controller',
            '--parent-id',job['id'],'--attempt-id',job['attempt_id'],'--image',image])
        container_created=True;result=docker(['start','--attach',name],check=False,timeout=110)
        info=json.loads(docker(['inspect',name]).stdout)[0];(base/'control-stderr.log').write_text(result.stderr)
        assert not info['State']['Running'] and info['State']['Pid']==0
        if result.returncode or info['State']['ExitCode']:
            (base/'control-stdout.log').write_text(result.stdout)
            raise RuntimeError('Cache provider control failed: '+result.stderr[-2400:])
        report=json.loads(result.stdout);assert report['passed']
        report.update(parent_job=job['id'],verified_at=now(),no_docker_socket=True,no_host_namespaces=True,root_fixture_seccomp_apparmor_unconfined=True)
    finally:
        if container_created:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.cache-proof']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            docker(['rm',name])
        if volume_created:docker(['volume','rm',volume])
        disk.cleanup()
    assert report is not None
    report['disk']=disk.report;report['cleanup_confirmed']=disk.report['loop_association_released'] and disk.report['helper_removed']
    atomic_json(directory/'results/persistent-cache.json',report)
    print(json.dumps({'passed':True,'checks':len(report['checks']),'dispatch_integrated':False,'cleanup_confirmed':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor');parser.add_argument('--controller',action='store_true')
    parser.add_argument('--parent-id');parser.add_argument('--attempt-id');parser.add_argument('--image');args=parser.parse_args()
    if args.actor:actor(args.actor)
    elif args.controller:controller(args.parent_id,args.attempt_id,args.image)
    else:parent()

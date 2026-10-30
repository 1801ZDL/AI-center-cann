"""Queue-only owned ext4/installed Node UID append and SIGKILL recovery acceptance."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import resource
import runpy
import signal
import subprocess
import sys
import uuid

IMAGE='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
BASELINE='34d33c4b835b4aa491ba31297c4827e39761e214'


def controller():
    sys.path.insert(0,'/payload/code')
    import deployment as deploy
    import workspace_quota_admin as admin
    import quota_workspace as native
    import filesystem_quota as quota
    from backends import physical_identity
    from docker_runtime import discover
    from lab_client import atomic_json,read_json
    from state_lock import state_lock
    assert os.geteuid()==0
    root=Path('/quota');root.chmod(0o700)
    if (root/'lost+found').exists():(root/'lost+found').rmdir()
    os.chown('/state',0,0);Path('/state').chmod(0o700)
    initial=native.prepare(str(root),[{'uid':23801,'bytes':16*1024**2,'inodes':128}])['policy']
    config={'node_id':'worker-'+uuid.uuid4().hex[:12],'node_key':uuid.uuid4().hex,'controller':'http://127.0.0.1:1',
        'physical_id':physical_identity(),'users':{'old_owner':{'uid':23801,'gid':23801}},'templates':['cpu_demo'],
        'policy':{'cpu_ids':[min(os.sched_getaffinity(0))],'memory_mib':512,'shm_mib':0,'max_running':1},
        'image':IMAGE,'workspace':initial,'docker_runtime':discover(read_json(Path('/payload/runtime.json')))[0]}
    atomic_json(Path('/state/enroll.json'),config)
    prefix='/opt/uid-node';state='/state/node'
    installation=deploy.install('/payload/server.tar.gz',prefix,state,'node',node_config='/state/enroll.json')
    original_config=read_json(Path(state)/'config.json')
    old_slot=initial['slots'][0];old_leaf=Path(old_slot['path'])/'retained';old_leaf.mkdir(mode=0o700);os.chown(old_leaf,23801,23801)
    def actor(uid,cwd,code):
        def drop():
            os.chdir(cwd);os.setgroups([]);os.setgid(uid);os.setuid(uid)
            resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        result=subprocess.run(['/usr/bin/python3','-I','-c',code],capture_output=True,text=True,timeout=30,
            env={'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8'},preexec_fn=drop)
        assert result.returncode==0,result.stderr[-1000:]
        return json.loads(result.stdout)
    actor(23801,str(old_leaf),'import os,json;f=os.open("old-data",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(f,b"retained-old-owner-data");os.fsync(f);os.close(f);print(json.dumps({"uid":os.getuid()}))')
    old_sha=hashlib.sha256((old_leaf/'old-data').read_bytes()).hexdigest()
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:
        old_quota=quota.require(fd,23801,16*1024**2,128)
        def limits(uid):return [{'uid':uid,'bytes':16*1024**2,'inodes':128}]
        def make(uid):return admin.plan(prefix,str(root),limits(uid))['plan']
        before_bytes=(root/native.MARKER).read_bytes()
        with state_lock(state):
            try:make(23820)
            except RuntimeError:pass
            else:raise AssertionError('A shared service state lock allowed offline UID administration')
        atomic_json(Path('/state/new-limits.json'),{'users':limits(23820)})
        cli=['/usr/bin/python3','-I','-B',prefix+'/current/servicectl']
        preview=subprocess.run([*cli,'workspace-add-plan','--prefix',prefix,'--root',str(root),
            '--limits','/state/new-limits.json','--output','/state/cli-first-plan.json'],check=True,capture_output=True,text=True,timeout=30)
        assert json.loads(preview.stdout)['read_only'];first=read_json(Path('/state/cli-first-plan.json'));stale=make(23821)
        assert (root/native.MARKER).read_bytes()==before_bytes and not Path(state,admin.JOURNAL).exists()
        assert quota.read(fd,23820)['byte_hard']==0
        applied=subprocess.run([*cli,'workspace-add-apply','--prefix',prefix,'--plan','/state/cli-first-plan.json'],
            check=True,capture_output=True,text=True,timeout=30)
        assert json.loads(applied.stdout)['node_and_central_owner_binding_still_required']
        try:admin.apply(prefix,stale)
        except (deploy.DeployError,ValueError):pass
        else:raise AssertionError('Stale native plan changed another UID')
        assert quota.read(fd,23821)['byte_hard']==0
        faults=[];last=None
        for ordinal,stage in enumerate(['journal','intent','quota','directory','marker','receipt','published']):
            uid=23802+ordinal;value=make(uid);path=Path('/state')/('plan-'+stage+'.json');atomic_json(path,value)
            code='''import json,os,signal,sys
sys.path.insert(0,"/payload/code")
import workspace_quota_admin as a
def crash(stage):
 if stage==sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.apply("/opt/uid-node",json.load(open(sys.argv[1])))
'''
            killed=subprocess.run(['/usr/bin/python3','-I','-c',code,str(path),stage],capture_output=True,text=True,timeout=30)
            assert killed.returncode==-signal.SIGKILL,(stage,killed.stderr[-1500:])
            if stage!='published':
                try:deploy.verify(prefix)
                except deploy.DeployError:pass
                else:raise AssertionError('Pending native operation allowed ordinary Node verification')
            completed=admin.apply(prefix,value);assert completed['applied'];last=completed['policy']['slots'][-1]
            assert last['quota_uid']==uid
            assert not Path(state,admin.JOURNAL).exists() and not (root/native.EXTEND_INTENT).exists()
            assert read_json(Path(state)/'config.json')==original_config
            assert quota.require(fd,23801,16*1024**2,128)==old_quota
            assert hashlib.sha256((old_leaf/'old-data').read_bytes()).hexdigest()==old_sha
            deploy.verify(prefix)
            faults.append({'boundary':stage,'sigkill_observed':True,'same_plan_resumed':True,'old_owner_unchanged':True})
        value=make(23831);link=root/value['additions'][0]['id'];link.symlink_to(old_slot['path'],target_is_directory=True)
        try:
            try:admin.apply(prefix,value)
            except (deploy.DeployError,ValueError):pass
            else:raise AssertionError('Preexisting planned path was adopted')
            assert quota.read(fd,23831)['byte_hard']==0 and not Path(state,admin.JOURNAL).exists()
        finally:link.unlink()
        quota.provision(fd,23830,16*1024**2,128)
        try:make(23830)
        except (deploy.DeployError,ValueError):pass
        else:raise AssertionError('Unregistered preexisting native quota was adopted')
        leaf=Path(last['path'])/'new-work';leaf.mkdir(mode=0o700);uid=last['quota_uid'];os.chown(leaf,uid,uid)
        proof=actor(uid,str(leaf),'''import os,json,errno
s={k:v.strip() for k,sep,v in (line.partition(":") for line in open("/proc/self/status")) if sep}
assert s["CapEff"]==s["CapPrm"]=="0000000000000000" and s["NoNewPrivs"]=="1"
f=os.open("payload",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);n=0;e=None
try:
 while n<32*1024**2:
  try:n+=os.write(f,b"x"*(256*1024))
  except OSError as error:e=error.errno;break
finally:os.close(f)
print(json.dumps({"uid":os.getuid(),"written":n,"errno":e,"cap_effective":s["CapEff"],"no_new_privileges":s["NoNewPrivs"]}))
''')
        assert proof['errno']==errno.EDQUOT and 0<proof['written']<=16*1024**2
        # Receipt replay is read-only even after the new allocation contains data.
        admin.apply(prefix,read_json(Path('/state/plan-published.json')))
        assert quota.require(fd,23801,16*1024**2,128)==old_quota
        from maintenance import activate
        before_release=deploy.verify(prefix)[1]['release_id']
        rollback=activate(prefix,archive_path='/payload/server-before-native.tar.gz')
        assert rollback['release_id']!=before_release
        old_verify=subprocess.run(['/usr/bin/python3','-I','-B',prefix+'/current/servicectl','verify','--prefix',prefix],
            check=True,capture_output=True,text=True,timeout=30)
        assert json.loads(old_verify.stdout)['verified']
        assert 'workspace_quota_admin.py' not in read_json(Path(prefix)/'current/release.json')['files']
        try:make(23850)
        except deploy.DeployError:pass
        else:raise AssertionError('New native administration targeted old code without its pending-operation guard')
        assert quota.read(fd,23850)['byte_hard']==0
        activate(prefix,release_id=before_release);deploy.verify(prefix)
        assert read_json(Path(state)/'config.json')==original_config
        assert quota.require(fd,23801,16*1024**2,128)==old_quota
        assert hashlib.sha256((old_leaf/'old-data').read_bytes()).hexdigest()==old_sha
        report={'passed':True,'read_only_plan_observed':True,'installed_node_prefix':True,'faults':faults,
            'installed_cli_plan_and_apply_passed':True,'shared_service_state_lock_rejected':True,
            'genuine_pre_native_code_rollback_and_reupgrade_passed':True,'rollback_baseline_commit':BASELINE,
            'old_code_native_administration_rejected_before_mutation':True,
            'stale_plan_rejected_before_new_quota':True,'existing_path_rejected_before_new_quota':True,
            'unknown_native_quota_not_adopted':True,'new_uid_kernel_quota':proof,'old_owner_data_and_limits_preserved':True,
            'node_configuration_unchanged':True,'receipt_replay_with_new_data_passed':True,
            'accounts_enabled':False,'node_enrollment_changed':False,'controller_contacted':False,
            'scope':'Native UID slot append on installed same-host isolated Node; owner identity, central quota, profile/cache binding and credential delivery remain separate'}
        print(json.dumps(report),flush=True)
    finally:os.close(fd)


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from deployment import bundle,unpack_release
    from docker_runtime import discover,command,binary
    from disk_workspace_fixture import OwnedDisk
    from lab_client import read_json,atomic_json,now
    jobdir=Path(os.environ['LAB_JOB_DIR']);job=read_json(jobdir/'dispatch.json');assert job['resources']['npu']==0
    base=jobdir/'tmp/native-uid-append';base.mkdir(mode=0o700);payload=base/'payload';payload.mkdir(mode=0o755)
    state=base/'control';state.mkdir(mode=0o700)
    archive=payload/'server.tar.gz';release=bundle(archive);code,_=unpack_release(archive,payload)
    code.rename(payload/'code');archive.chmod(0o444)
    # Reconstruct the actual pre-feature public tree; do not label a copy of
    # today's modules as a historical rollback target. No network or checkout.
    historical=base/'historical-source';historical.mkdir(mode=0o700)
    names=subprocess.check_output(['git','ls-tree','-r','--name-only',BASELINE],cwd=root,text=True).splitlines()
    for name in names:
        if '/' not in name or name.startswith(('web/','examples/')):
            target=historical/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(subprocess.check_output(['git','show',BASELINE+':'+name],cwd=root))
    old=runpy.run_path(str(historical/'deployment.py'))
    assert 'workspace_quota_admin.py' not in old['CORE']
    old['bundle'](payload/'server-before-native.tar.gz',source=historical)
    (payload/'probe.py').write_bytes(Path(__file__).read_bytes());(payload/'probe.py').chmod(0o444)
    binding,_=discover();cpus=job['allocation']['cpu_ids']
    atomic_json(payload/'runtime.json',binding);(payload/'runtime.json').chmod(0o444)
    def docker(args,check=True,timeout=45):
        argv,env=command(binding,['docker',*map(str,args)]);r=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
        if check and r.returncode:raise RuntimeError('Owned native UID fixture failed: '+r.stderr[-1500:])
        return r
    prefix='ascend-lab-uidappend-'+uuid.uuid4().hex[:8];name=prefix+'-node';volume=prefix+'-volume'
    disk=OwnedDisk(base,prefix,IMAGE,cpus,docker,folder_name='quota-workspace',image_mib=256,inodes=8192,quota_type='user')
    made_role=made_volume=False;report=None;errors=[]
    try:
        device=disk.create()
        docker(['volume','create','--label','io.ascend-lab.uidappend='+prefix,'--driver','local','--opt','type=ext4',
                '--opt','device='+device,'--opt','o=nosuid,nodev,usrquota',volume]);made_volume=True
        socket=binding['endpoint'].removeprefix('unix://')
        docker(['create','--name',name,'--label','io.ascend-lab.uidappend='+prefix,'--network','none','--ipc','none',
            '--read-only','--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','FOWNER',
            '--cap-add','DAC_OVERRIDE','--cap-add','SETUID','--cap-add','SETGID','--security-opt','no-new-privileges:true',
            '--security-opt','seccomp=unconfined','--memory','512m','--memory-swap','512m','--cpuset-cpus',str(cpus[0]),
            '--cpus','0.8','--pids-limit','64','--tmpfs','/opt:rw,nosuid,nodev,size=32m,mode=755',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--mount','type=volume,src='+volume+',dst=/quota',
            '--mount','type=bind,src='+str(payload)+',dst=/payload,readonly','--mount','type=bind,src='+str(state)+',dst=/state',
            '--mount','type=bind,src='+binary()+',dst=/usr/bin/docker,readonly',
            '--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
            '--mount','type=bind,src='+socket+',dst='+socket,'--entrypoint','/usr/bin/timeout',IMAGE,
            '--kill-after=3s','150s','/usr/bin/python3','-I','-B','/payload/probe.py','--controller']);made_role=True
        result=docker(['start','--attach',name],check=False,timeout=160)
        atomic_json(base/'role-exit.json',{'returncode':result.returncode})
        (base/'role.stdout').write_text(result.stdout);(base/'role.stderr').write_text(result.stderr)
        info=json.loads(docker(['inspect',name]).stdout)[0];assert not info['State']['Running']
        if result.returncode or info['State']['ExitCode']:raise RuntimeError('Native UID append Root acceptance failed; original logs retained')
        report=json.loads(result.stdout);assert report['passed'];report.update(parent_job=job['id'],at=now(),server_release=release['release_id'],npu_kernel_launches=0)
    finally:
        if made_role:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.uidappend']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            # Restore only this fixture's control ownership before removing role.
            repair='import os; p="/state"\nfor base,dirs,files in os.walk(p,followlinks=False):\n for n in dirs+files:os.lchown(os.path.join(base,n),'+str(os.getuid())+','+str(os.getgid())+')\nos.chown(p,'+str(os.getuid())+','+str(os.getgid())+')'
            docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE',
                '--memory','64m','--cpuset-cpus',str(cpus[0]),'--mount','type=bind,src='+str(state)+',dst=/state',
                '--entrypoint','/usr/bin/python3',IMAGE,'-I','-c',repair])
            docker(['rm',name])
        if made_volume:docker(['volume','rm',volume])
        disk.cleanup()
    assert report is not None;report['disk']=disk.report;report['cleanup_confirmed']=disk.report['loop_association_released'] and disk.report['helper_removed']
    atomic_json(jobdir/'results/native-uid-append.json',report);print(json.dumps(report))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--controller',action='store_true');args=parser.parse_args()
    if args.controller:controller()
    else:parent()

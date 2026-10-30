"""Queue-only installed central owner-group append on an owned ext4 image."""
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
BASELINE='60c20025edc552c7138918b1759e416350cb3b2b'
SERVICE=23820


def controller():
    sys.path.insert(0,'/payload/code')
    import deployment as deploy
    import group_quota as quota
    import payload_owner_admin as admin
    import payload_quota_admin as resize
    from payload_groups import prepare,gids
    from payload_storage import Payload,MARKER
    from lab_client import atomic_json,read_json
    from maintenance import activate
    from state_lock import state_lock
    assert os.geteuid()==0
    root=Path('/quota');root.chmod(0o700)
    if (root/'lost+found').exists():(root/'lost+found').rmdir()
    os.chown('/state',0,0);Path('/state').chmod(0o755)
    info=root.stat();space=os.statvfs(root)
    policy={'path':str(root),'filesystem':'ext4','device':info.st_dev,'root_inode':info.st_ino,
        'bytes':space.f_blocks*space.f_frsize,'inodes':space.f_files,'storage_id':uuid.uuid4().hex,
        'owner_groups':{'old_owner':{'gid':23831,'bytes':16*1024**2,'inodes':128},
                        'peer_owner':{'gid':23832,'bytes':16*1024**2,'inodes':128}}}
    prepare(policy);atomic_json(Path('/state/payload.json'),policy)
    prefix='/opt/owner-controller';state=Path('/state/controller')
    deployed=deploy.install('/payload/server.tar.gz',prefix,str(state),'controller',uid=SERVICE,gid=SERVICE,
                           payload_config='/state/payload.json',port=18769)
    cli=['/usr/bin/python3','-I','-B',prefix+'/current/servicectl']

    def command(*args):
        r=subprocess.run([*cli,*map(str,args)],capture_output=True,text=True,timeout=30)
        if r.returncode:raise RuntimeError('Installed owner CLI rejected fixture operation: '+r.stderr[-1400:])
        return json.loads(r.stdout)

    def stage(owner):
        spec=state/(owner+'-spec.json');atomic_json(spec,{'user':owner,'environments':['worker-python'],
            'quota':{'input_storage_mib':4,'archive_storage_mib':4}});os.chown(spec,SERVICE,SERVICE)
        result=command('admin','--prefix',prefix,'--','accessctl','user-stage','--spec',spec)
        assert result['state']=='staged' and not result['credentials_issued'];return result

    def writer(code):
        def drop():
            os.setgroups(gids(deploy.verify(prefix)[1]['payload_storage']));os.setgid(SERVICE);os.setuid(SERVICE)
            os.umask(0o077);resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code],capture_output=True,text=True,timeout=30,
            preexec_fn=drop,env={'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8'})
        if r.returncode:raise RuntimeError('Fixed unprivileged central writer failed: '+r.stderr[-1400:])
        return json.loads(r.stdout)

    writer('''import os,json,sys
sys.path.insert(0,"/opt/owner-controller/current");import group_quota as q
from pathlib import Path
p=Path("/quota/jobs/retained-old");p.mkdir(mode=0o700);fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
q.assign_empty(fd,23831);os.close(fd)
with (p/"old.bin").open("wb") as f:f.write(b"existing-owner-sentinel");f.flush();os.fsync(f.fileno())
print(json.dumps({"uid":os.getuid()}))
''')
    old_path=root/'jobs/retained-old/old.bin';old_sha=hashlib.sha256(old_path.read_bytes()).hexdigest()
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY);service_started=False
    try:
        old_quota=quota.require(fd,23831,16*1024**2,128);peer_quota=quota.require(fd,23832,16*1024**2,128)
        def preserved():
            assert quota.require(fd,23831,16*1024**2,128)==old_quota
            assert quota.require(fd,23832,16*1024**2,128)==peer_quota
            assert hashlib.sha256(old_path.read_bytes()).hexdigest()==old_sha
        stage('cli_owner');stage('stale_owner')
        with state_lock(state):
            try:admin.plan(prefix,'cli_owner',23840,16*1024**2,128)
            except RuntimeError:pass
            else:raise AssertionError('Live service lock allowed owner storage provisioning')
        before_marker=(root/MARKER).read_bytes()
        preview=command('payload-owner-plan','--prefix',prefix,'--owner','cli_owner','--gid',23840,
                        '--mib',16,'--inodes',128,'--output','/state/cli-plan.json')
        assert preview['read_only'] and preview['owner_activation_still_required']
        value=read_json(Path('/state/cli-plan.json'));stale=admin.plan(prefix,'stale_owner',23841,16*1024**2,128)['plan']
        assert (root/MARKER).read_bytes()==before_marker and quota.read(fd,23840)['byte_hard']==0
        result=command('payload-owner-apply','--prefix',prefix,'--plan','/state/cli-plan.json')
        assert result['owner_activation_still_required']
        assert command('admin','--prefix',prefix,'--','accessctl','user-status','cli_owner')['state']=='staged'
        try:admin.apply(prefix,stale)
        except (deploy.DeployError,ValueError):pass
        else:raise AssertionError('Stale plan acquired new group quota')
        assert quota.read(fd,23841)['byte_hard']==0
        faults=[];last=None
        for ordinal,boundary in enumerate(['journal','quota','config','metadata','binding','receipt','published']):
            owner='fault_'+boundary;gid=23850+ordinal;stage(owner)
            value=admin.plan(prefix,owner,gid,16*1024**2,128)['plan'];path=Path('/state')/('plan-'+boundary+'.json');atomic_json(path,value)
            killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import os,signal,json,sys
sys.path.insert(0,"/payload/code");import payload_owner_admin as a
def crash(stage):
 if stage==sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.apply("/opt/owner-controller",json.load(open(sys.argv[1])))
''',str(path),boundary],capture_output=True,text=True,timeout=30)
            assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-1200:])
            if boundary!='published':
                try:deploy.verify(prefix)
                except deploy.DeployError:pass
                else:raise AssertionError('Pending owner operation allowed ordinary startup verification')
                # A previously prepared resize operation cannot interleave via
                # quota_recovery=True while this different journal is pending.
                old_item=value['before']['owner_groups']['old_owner'];after=json.loads(json.dumps(value['before']))
                after['owner_groups']['old_owner']['bytes']+=4096
                foreign={'format':'ascend-lab-payload-quota-plan/v1','prefix':prefix,'state_dir':str(state),'release_id':value['release_id'],
                    'owner':'old_owner','before':value['before'],'after':after,'revision':value['revision'],
                    'config_base_sha256':value['config_base_sha256'],'installation_base_sha256':value['installation_base_sha256'],
                    'usage':value['usage']['old_owner'],'pending_bytes':0}
                foreign['id']=resize.fingerprint(foreign)
                try:resize.apply(prefix,foreign)
                except deploy.DeployError as error:assert 'pending owner' in str(error)
                else:raise AssertionError('Existing-owner resize interleaved with owner append recovery')
            result=admin.apply(prefix,value);assert result['applied'];last=value
            assert not Path(prefix,admin.JOURNAL).exists();Payload(state,read_json(state/'config.json'),uid=SERVICE);preserved()
            assert command('admin','--prefix',prefix,'--','accessctl','user-status',owner)['disabled']
            faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_resumed':True,'old_owners_preserved':True})
        stage('occupied_owner');quota.provision(fd,23890,16*1024**2,128)
        try:admin.plan(prefix,'occupied_owner',23890,16*1024**2,128)
        except deploy.DeployError:pass
        else:raise AssertionError('An unregistered compatible native quota was adopted')
        stage('changed_definition')
        changed=admin.plan(prefix,'changed_definition',23892,16*1024**2,128)['plan']
        import sqlite3,owner_identity
        with sqlite3.connect(state/'identities.sqlite3') as db:
            original=db.execute('SELECT profile FROM users WHERE id=?',('changed_definition',)).fetchone()[0]
            profile=json.loads(original);profile['quota']['archive_storage_mib']=3;profile.pop('provisioning')
            profile['provisioning']={'format':owner_identity.FORMAT,'state':'staged','id':owner_identity.digest({'owner':'changed_definition','profile':profile})}
            db.execute('UPDATE users SET profile=? WHERE id=?',(json.dumps(profile),'changed_definition'))
        try:admin.apply(prefix,changed)
        except deploy.DeployError as error:assert 'Staged owner definition changed' in str(error)
        else:raise AssertionError('A changed staged definition acquired native group quota')
        assert quota.read(fd,23892)['byte_hard']==0 and not Path(prefix,admin.JOURNAL).exists()
        with sqlite3.connect(state/'identities.sqlite3') as db:db.execute('UPDATE users SET profile=? WHERE id=?',(original,'changed_definition'))
        # No actual controller/client privilege is involved in this write: this
        # fixed actor exercises the trusted writer's newly assigned native GID.
        new_gid=last['after']['owner_groups'][last['owner']]['gid']
        proof=writer('''import os,errno,json,sys
sys.path.insert(0,"/opt/owner-controller/current");import group_quota as q
from pathlib import Path
s={k:v.strip() for k,sep,v in (l.partition(":") for l in Path("/proc/self/status").read_text().splitlines()) if sep}
assert s["CapEff"]==s["CapPrm"]=="0000000000000000" and s["NoNewPrivs"]=="1"
p=Path("/quota/jobs/new-group-fill");p.mkdir(mode=0o700);fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY);q.assign_empty(fd,'''+str(new_gid)+''');os.close(fd)
fd=os.open(p/"fill",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);written=0;code=None
try:
 while written<32*1024**2:
  try:written+=os.write(fd,b"x"*(256*1024))
  except OSError as e:code=e.errno;break
finally:os.close(fd)
print(json.dumps({"uid":os.getuid(),"storage_gid":'''+str(new_gid)+''',"written":written,"errno":code,"cap_effective":s["CapEff"],"no_new_privileges":s["NoNewPrivs"]}))
''')
        assert proof['errno']==errno.EDQUOT and 0<proof['written']<=16*1024**2;preserved()
        replay=admin.apply(prefix,last)
        assert replay['already_applied'] and replay['service_restart_required'] and replay['systemd_unit_group_refresh_required']
        peer=writer('''import os,json,sys
sys.path.insert(0,"/opt/owner-controller/current");import group_quota as q
from pathlib import Path
p=Path("/quota/jobs/peer-progress");p.mkdir(mode=0o700);fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY);q.assign_empty(fd,23832);os.close(fd)
with (p/"peer.bin").open("wb") as f:f.write(b"p"*(1024**2));f.flush();os.fsync(f.fileno())
print(json.dumps({"bytes":(p/"peer.bin").stat().st_size}))
''');assert peer['bytes']==1024**2
        start=deploy.start(prefix);assert start['running'];service_started=True
        current=deploy.verify(prefix)[1];pid=start['pid']
        status={k:v.strip() for k,sep,v in (l.partition(':') for l in Path('/proc/'+str(pid)+'/status').read_text().splitlines()) if sep}
        assert int(status['Uid'].split()[1])==SERVICE and set(map(int,status['Groups'].split()))==set(gids(current['payload_storage']))
        assert status['CapEff']==status['CapPrm']=='0000000000000000'
        deploy.stop(prefix);service_started=False
        # Genuine previous release has staged authentication but no new-owner
        # recovery helper. Existing bindings remain readable after completion.
        release=current['release_id'];before_config=read_json(state/'config.json')
        activate(prefix,archive_path='/payload/server-before-owner.tar.gz')
        command('verify','--prefix',prefix)
        try:admin.plan(prefix,'occupied_owner',23891,16*1024**2,128)
        except deploy.DeployError as error:assert 'matching owner administration' in str(error)
        else:raise AssertionError('New helper modified a historical installation without its guards')
        assert quota.read(fd,23891)['byte_hard']==0
        activate(prefix,release_id=release);assert read_json(state/'config.json')==before_config
        assert quota.require(fd,23831,16*1024**2,128)==old_quota
        assert hashlib.sha256(old_path.read_bytes()).hexdigest()==old_sha
        print(json.dumps({'passed':True,'installed_controller':True,'installed_cli_plan_apply_stage_status':True,
            'read_only_plan_observed':True,'shared_state_lock_rejected':True,'faults':faults,
            'resize_interleaving_rejected':True,'stale_plan_rejected_before_quota':True,'compatible_unknown_quota_not_adopted':True,
            'changed_stage_definition_rejected_before_quota':True,
            'old_owner_data_and_limits_preserved':True,'owner_still_staged_disabled':True,'new_group_kernel_quota':proof,
            'peer_wrote_while_new_group_full':True,'receipt_replay_after_new_data':True,
            'service_restart_observed_new_supplementary_groups':True,'service_uid':SERVICE,'service_cap_effective':status['CapEff'],
            'genuine_historical_rollback_reupgrade':True,'rollback_baseline_commit':BASELINE,'old_target_new_administration_rejected':True,
            'full_owner_onboarding_ready':False,'systemd_pid1_tested':False,
            'scope':'Same-host isolated installed controller, actual native group quota and process crash/restart. Node workspace/cache binding and final owner activation remain incomplete.'}),flush=True)
    finally:
        if service_started:deploy.stop(prefix)
        os.close(fd)


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
    from deployment import bundle,unpack_release
    from docker_runtime import discover,command
    from disk_workspace_fixture import OwnedDisk
    from lab_client import read_json,atomic_json,now
    jobdir=Path(os.environ['LAB_JOB_DIR']);job=read_json(jobdir/'dispatch.json');assert job['resources']['npu']==0
    base=jobdir/'tmp/payload-owner-append';base.mkdir(mode=0o700);payload=base/'payload';payload.mkdir(mode=0o755)
    state=base/'control';state.mkdir(mode=0o700)
    archive=payload/'server.tar.gz';release=bundle(archive);code,manifest=unpack_release(archive,payload);code.rename(payload/'code');archive.chmod(0o444)
    historical=base/'historical-source';historical.mkdir(mode=0o700)
    for name in subprocess.check_output(['git','ls-tree','-r','--name-only',BASELINE],cwd=root,text=True).splitlines():
        if '/' not in name or name.startswith(('web/','examples/')):
            path=historical/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(subprocess.check_output(['git','show',BASELINE+':'+name],cwd=root))
    old=runpy.run_path(str(historical/'deployment.py'));assert 'payload_owner_admin.py' not in old['CORE']
    old['bundle'](payload/'server-before-owner.tar.gz',source=historical)
    (payload/'probe.py').write_bytes(Path(__file__).read_bytes());(payload/'probe.py').chmod(0o444)
    binding,_=discover();cpus=job['allocation']['cpu_ids']
    def docker(args,check=True,timeout=45):
        argv,env=command(binding,['docker',*map(str,args)]);result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned central owner fixture failed: '+result.stderr[-1400:])
        return result
    prefix='ascend-lab-ownerappend-'+uuid.uuid4().hex[:8];name=prefix+'-controller';volume=prefix+'-volume'
    disk=OwnedDisk(base,prefix,IMAGE,cpus,docker,folder_name='central-payload',image_mib=256,inodes=8192,quota_type='group')
    made_role=made_volume=False;report=None
    try:
        device=disk.create()
        docker(['volume','create','--label','io.ascend-lab.ownerappend='+prefix,'--driver','local','--opt','type=ext4',
            '--opt','device='+device,'--opt','o=nosuid,nodev,grpquota',volume]);made_volume=True
        docker(['create','--name',name,'--label','io.ascend-lab.ownerappend='+prefix,'--network','none','--ipc','none',
            '--read-only','--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','FOWNER',
            '--cap-add','DAC_OVERRIDE','--cap-add','SETUID','--cap-add','SETGID','--cap-add','KILL',
            '--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined',
            '--memory','512m','--memory-swap','512m','--cpuset-cpus',str(cpus[0]),'--cpus','0.8','--pids-limit','96',
            '--tmpfs','/opt:rw,nosuid,nodev,size=32m,mode=755','--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777',
            '--mount','type=volume,src='+volume+',dst=/quota','--mount','type=bind,src='+str(payload)+',dst=/payload,readonly',
            '--mount','type=bind,src='+str(state)+',dst=/state','--entrypoint','/usr/bin/timeout',IMAGE,
            '--kill-after=3s','210s','/usr/bin/python3','-I','-B','/payload/probe.py','--controller']);made_role=True
        result=docker(['start','--attach',name],check=False,timeout=220)
        atomic_json(base/'role-exit.json',{'returncode':result.returncode})
        (base/'role.stdout').write_text(result.stdout);(base/'role.stderr').write_text(result.stderr)
        info=json.loads(docker(['inspect',name]).stdout)[0];assert not info['State']['Running']
        if result.returncode or info['State']['ExitCode']:raise RuntimeError('Central owner Root acceptance failed; original logs retained')
        report=json.loads(result.stdout);assert report['passed'];report.update(parent_job=job['id'],at=now(),server_release=release['release_id'],
            production_source_sha256={key:item['sha256'] for key,item in manifest['files'].items() if key in __import__('deployment').CORE and not key.endswith('.md')},
            npu_kernel_launches=0,model_invocations=0)
    finally:
        if made_role:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.ownerappend']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            repair='import os;p="/state"\nfor base,dirs,files in os.walk(p,followlinks=False):\n for n in dirs+files:os.lchown(os.path.join(base,n),'+str(os.getuid())+','+str(os.getgid())+')\nos.chown(p,'+str(os.getuid())+','+str(os.getgid())+')'
            docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE',
                '--memory','64m','--cpuset-cpus',str(cpus[0]),'--mount','type=bind,src='+str(state)+',dst=/state',
                '--entrypoint','/usr/bin/python3',IMAGE,'-I','-c',repair]);docker(['rm',name])
        if made_volume:docker(['volume','rm',volume])
        disk.cleanup()
    assert report is not None;report['disk']=disk.report;report['cleanup_confirmed']=disk.report['loop_association_released'] and disk.report['helper_removed']
    atomic_json(jobdir/'results/payload-owner-append.json',report);print(json.dumps(report))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--controller',action='store_true');args=parser.parse_args()
    if args.controller:controller()
    else:parent()

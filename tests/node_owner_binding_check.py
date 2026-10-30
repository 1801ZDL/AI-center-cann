"""Queue-only Node owner/cache metadata migration and warm IR preservation.

Uses fixture stage identity, not a central authorization. One unchanged plan is
interrupted successively at named boundaries; it stays blocked for central ACK.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import runpy
import shutil
import signal
import subprocess
import sys
import uuid

IMAGE='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
BASELINE='9d025e05d25dc419824da0cfc1d8b87c92e09f8d'


def cleanup_owned_attempts(state,base,cpus,docker):
    """End only this fixture's authenticated CPU containers before chown/unmount.

    A failed controller assertion can leave a sibling task alive after the role
    stops. Its protected receipt must retain ownership until that task has ended.
    The original controller lease history is retained; this is fixture teardown,
    never a scheduler completion or permission to retry an uncertain workload.
    """
    reader='''import json,re,sqlite3
from pathlib import Path
items=[]
for node in [Path('/state/node'),Path('/state/expansion-node')]:
 if not (node/'config.json').exists():continue
 assert not node.is_symlink() and not (node/'config.json').is_symlink()
 config=json.loads((node/'config.json').read_text())
 assert config['image']=='''+repr(IMAGE)+'''
 attempts=node/'attempts'
 if not attempts.exists():continue
 assert not attempts.is_symlink()
 for attempt in attempts.iterdir():
  assert not attempt.is_symlink() and re.fullmatch('attempt-[0-9a-f]{32}',attempt.name)
  path=attempt/'control/grant.json'
  if not path.exists():continue
  assert not (attempt/'control').is_symlink() and not path.is_symlink() and path.stat().st_size<=1024**2
  grant=json.loads(path.read_text())
  assert grant['attempt_id']==attempt.name and grant['node_id']==config['node_id'] and grant['physical_id']==config['physical_id']
  assert grant['resources']['npu']==0
  items.append(attempt.name)
database=Path('/state/controller/jobs.sqlite3')
if database.exists():
 with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as db:
  for row in db.execute('SELECT data FROM jobs'):
   job=json.loads(row[0]);attempt=job.get('attempt_id')
   if not job.get('_remote') or not attempt:continue
   assert re.fullmatch('attempt-[0-9a-f]{32}',attempt) and job['resources']['npu']==0
   items.append(attempt)
items=sorted(set(items))
assert len(items)<=64
print(json.dumps(items))
'''
    found=docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','DAC_OVERRIDE',
        '--memory','64m','--cpuset-cpus',str(cpus[0]),'--mount','type=bind,src='+str(state)+',dst=/state,readonly',
        '--entrypoint','/usr/bin/python3',IMAGE,'-I','-c',reader])
    attempts=json.loads(found.stdout);records=[]
    for attempt in attempts:
        if not isinstance(attempt,str) or not re.fullmatch('attempt-[0-9a-f]{32}',attempt):raise RuntimeError('Invalid fixture attempt identity')
        ids=docker(['ps','-aq','--filter','label=io.ascend-lab.attempt='+attempt]).stdout.split()
        for identifier in ids:
            info=json.loads(docker(['inspect',identifier]).stdout)[0]
            assert info['Config']['Labels'].get('io.ascend-lab.attempt')==attempt and info['Config']['Image']==IMAGE
            assert info['Name'].lstrip('/').startswith('ascend-lab-'+attempt.removeprefix('attempt-'))
            before=info['State']
            if before['Running']:docker(['stop','--time','3',identifier])
            info=json.loads(docker(['inspect',identifier]).stdout)[0]
            assert not info['State']['Running'] and info['State']['Pid']==0,'Preserve fixture: task termination is unconfirmed'
            logs=docker(['logs',identifier],check=False)
            (base/(attempt+'-'+identifier+'.log')).write_text(logs.stdout+logs.stderr)
            records.append({'attempt_id':attempt,'container_id':identifier,'before':before,'after':info['State']})
            docker(['rm',identifier])
    (base/'owned-attempt-cleanup.json').write_text(json.dumps({'verified':True,'containers':records,
        'lease_history_preserved':True,'no_task_retry':True,'scope':'Only trusted local CPU fixture grants; no global labels, resets or detach-all'}))


def actor():
    assert os.getuid() in {23801,23802}
    status={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    assert status['CapEff']==status['CapPrm']=='0000000000000000' and status['NoNewPrivs']=='1'
    output=Path('canonical.mlir');calls=Path('calls.json');compiled=not output.exists()
    if compiled:
        subprocess.run([os.environ['CACHE_IR_TOOL'],'--canonicalize','--cse','/payload/input.mlir','-o','canonical.partial'],check=True)
        Path('canonical.partial').rename(output);calls.write_text(json.dumps({'calls':1}))
    assert json.loads(calls.read_text())['calls']==1
    raw=output.read_bytes();assert b'arith.constant 42' in raw and b'arith.addi' not in raw
    print(json.dumps({'uid':os.getuid(),'compiled':compiled,'calls':1,'sha256':hashlib.sha256(raw).hexdigest(),
                      'inode':output.stat().st_ino,'cap_effective':status['CapEff'],'no_new_privileges':status['NoNewPrivs']}))


def controller():
    os.umask(0o077)
    sys.path.insert(0,'/payload/code')
    import deployment as deploy
    import node_owner_admin as admin
    import workspace_quota_admin as slots
    import quota_workspace as native
    from persistent_cache import Pool,Lease,CacheBusy,fingerprint,namespace
    from environment_contract import fingerprint as env_sha
    from backends import physical_identity
    from docker_runtime import discover
    from lab_client import read_json,atomic_json
    from maintenance import activate
    from state_lock import state_lock
    context=read_json(Path('/payload/context.json'));assert os.geteuid()==0
    work=context.get('work_path','/work-quota');private=context.get('cache_path','/cache-quota')
    state=Path(context.get('node_state','/state/node'));prefix='/opt/owner-node'
    os.chown('/state',0,0);Path('/state').chmod(0o700)
    for root in [work,private]:
        Path(root).chmod(0o700)
        if Path(root,'lost+found').exists():Path(root,'lost+found').rmdir()
    limits=lambda uid:[{'uid':uid,'bytes':16*1024**2,'inodes':128}]
    workspace=native.prepare(work,limits(23801))['policy'];persistent=native.prepare(private,limits(23801))['policy']
    tool=str(Path(shutil.which('bishengir-opt')).resolve());tool_sha=hashlib.sha256(Path(tool).read_bytes()).hexdigest()
    setup='/usr/local/Ascend/ascend-toolkit/set_env.sh';assert Path(setup).is_file()
    config={'node_id':'worker-'+uuid.uuid4().hex[:12],'node_key':uuid.uuid4().hex,'controller':'http://127.0.0.1:1',
        'physical_id':physical_identity(),'users':{'old_owner':{'uid':23801,'gid':23801}},
        'templates':['cpu_demo','ir_v1'] if context.get('activate_owner') else ['cpu_demo'],
        'policy':{'cpu_ids':[min(os.sched_getaffinity(0))],'memory_mib':512,'shm_mib':0,'max_running':1},
        'image':IMAGE,'workspace':workspace,'persistent_cache':persistent,'policy_revision':1,
        'python':sys.executable,'environment_setup':setup,'docker_runtime':discover(context['runtime'])[0]}
    config['environment_manifest']={'format':'ascend-lab-node-environment/v1','image_sha256':IMAGE,'environment_setup':setup,
        'observed':{'image_sha256':IMAGE,'python':platform.python_version(),'python_executable':sys.executable,'machine':platform.machine(),
                    'packages':{},'tools':{'bishengir-opt':{'realpath':tool,'sha256':tool_sha}},'files':{}},
        'source':{'job_id':context['job_id'],'attempt_id':context['attempt_id'],
                  'receipt_sha256':hashlib.sha256(Path('/payload/probe.py').read_bytes()).hexdigest(),'node_revision':1}}
    session=None
    if context.get('central_handoff'):
        sys.path.insert(0,'/payload')
        from handoff_fixture import CentralSession
        spool=Path(context.get('spool_path','/spool-quota'));spool.chmod(0o700)
        if (spool/'lost+found').exists():(spool/'lost+found').rmdir()
        info=spool.stat();space=os.statvfs(spool)
        config['spool_storage']={'path':str(spool),'filesystem':'ext4','device':info.st_dev,'root_inode':info.st_ino,
            'bytes':space.f_blocks*space.f_frsize,'inodes':space.f_files,'storage_id':uuid.uuid4().hex}
        session=CentralSession(config,context);config=session.config
    atomic_json(Path('/state/enroll.json'),config)
    deploy.install('/payload/server.tar.gz',prefix,str(state),'node',node_config='/state/enroll.json')
    config=read_json(state/'config.json');old=Pool(state,config);original_config=json.loads(json.dumps(config))
    runtime={'persistent_cache.py':hashlib.sha256(Path('/payload/code/persistent_cache.py').read_bytes()).hexdigest()}

    def grant(config,owner,ordinal):
        uid=config['users'][owner]['uid'];slot=next(s for s in config['persistent_cache']['slots'] if s['quota_uid']==uid)
        value={'owner':owner,'job_id':'job-'+format(ordinal,'012x'),'attempt_id':'attempt-'+format(ordinal,'032x'),
            'lease_token':'private-node-owner-fixture-'+str(ordinal),'environment_sha256':env_sha(config['environment_manifest']),
            'runtime_files':runtime,'parameters':{'lock':{'python':platform.python_version(),'tools':{'bishengir-opt':tool_sha}},'environment':{}},
            'cache_policy_sha256':fingerprint(config['persistent_cache']),'allocation':{}}
        value['allocation']['cache']={'slot_id':slot['id'],'storage_id':slot['storage_id'],'key':namespace(config,value)}
        return value

    def run(lease):
        uid=lease.pool.config['users'][lease.grant['owner']]['uid'];fd=os.open(lease.path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        def drop():
            os.fchdir(fd);os.setgroups([]);os.setgid(uid);os.setuid(uid);os.umask(0o077)
            resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        try:
            r=subprocess.run(['/usr/bin/python3','-I','-B','/payload/probe.py','--actor'],capture_output=True,text=True,timeout=30,
                preexec_fn=drop,env={'PATH':os.environ['PATH'],'LD_LIBRARY_PATH':os.environ.get('LD_LIBRARY_PATH',''),
                                   'HOME':str(lease.path),'LANG':'C.UTF-8','CACHE_IR_TOOL':tool})
        finally:os.close(fd)
        if r.returncode:raise RuntimeError('Fixed cache IR actor failed: '+r.stderr[-1400:])
        return json.loads(r.stdout)

    first=Lease(old,grant(config,'old_owner',1));first.prepare();cold=run(first);first.release(termination_confirmed=True)
    assert cold['compiled'];before_release=deploy.verify(prefix)[1]['release_id']
    activate(prefix,archive_path='/payload/server-before-owner.tar.gz')
    verify_old=subprocess.run(['/usr/bin/python3','-I','-B',prefix+'/current/servicectl','verify','--prefix',prefix],
                              capture_output=True,text=True,check=True,timeout=30)
    assert json.loads(verify_old.stdout)['verified']
    try:admin.plan(prefix,{'owner':'new_owner','stage_id':'c'*64,'uid':23802,'gid':23802,'workspace_slot':'x','cache_slot':'y'})
    except deploy.DeployError as error:assert 'matching owner-binding' in str(error)
    else:raise AssertionError('New owner administration targeted old code without its guards')
    activate(prefix,release_id=before_release);assert read_json(state/'config.json')==original_config
    if context.get('owner_provision'):
        from owner_provision_fixture import run as onboard
        report=onboard(session,prefix,config,cold,run,grant,first)
        print(json.dumps({'passed':True,'owner_provision':report}),flush=True);return
    if context.get('node_provision'):
        from node_provision_fixture import run as provision_node
        value,current,proof=provision_node(session,prefix,config,cold,run,grant,first)
        report={'passed':True,'node_owner_provision':proof,'accounts_enabled':False,'full_owner_onboarding_ready':False}
        report.update(session.finish(prefix,value,current,already_acknowledged=True))
        import node_owner_provision as participant
        later=participant.provision(prefix,read_json(Path('/state/node-provision-spec.json')),'complete')
        assert later['historical_receipt_only'] and later['credentials_issued'] is False
        proof['completed_offline_replay_after_central_disable_is_historical']=True
        print(json.dumps(report),flush=True);return
    work_plan=slots.plan(prefix,work,limits(23802))['plan'];work_result=slots.apply(prefix,work_plan)
    cache_plan=slots.plan(prefix,private,limits(23802))['plan'];cache_result=slots.apply(prefix,cache_plan)
    work_slot=next(s for s in work_result['policy']['slots'] if s['quota_uid']==23802)
    cache_slot=next(s for s in cache_result['policy']['slots'] if s['quota_uid']==23802)
    request={'owner':'new_owner','stage_id':session.stage_id if session else 'c'*64,'uid':23802,'gid':23802,'workspace_slot':work_slot['id'],'cache_slot':cache_slot['id']}
    # The native source append has not changed the old configured policy/key.
    held=Lease(old,grant(config,'old_owner',2));held.prepare()
    try:admin.plan(prefix,request)
    except CacheBusy:pass
    else:raise AssertionError('A live cache lease allowed owner binding')
    held.release(termination_confirmed=True)
    with state_lock(state):
        try:admin.plan(prefix,request)
        except RuntimeError:pass
        else:raise AssertionError('A live Node service lock allowed owner binding')
    atomic_json(Path('/state/owner-spec.json'),request)
    cli=['/usr/bin/python3','-I','-B',prefix+'/current/servicectl']
    preview=subprocess.run([*cli,'node-owner-plan','--prefix',prefix,'--spec','/state/owner-spec.json','--output','/state/first-bind-plan.json'],
                           capture_output=True,text=True,check=True,timeout=30)
    assert json.loads(preview.stdout)['read_only'];stale=read_json(Path('/state/first-bind-plan.json'))
    old_slot=config['persistent_cache']['slots'][0]
    with old.locked(old_slot) as metadata:old.save(old_slot,metadata)
    try:admin.apply(prefix,stale)
    except deploy.DeployError as error:assert 'Old cache metadata/lease changed' in str(error)
    else:raise AssertionError('Stale cache metadata was overwritten by owner binding')
    assert not (state/admin.JOURNAL).exists() and read_json(state/'config.json')==original_config
    subprocess.run([*cli,'node-owner-plan','--prefix',prefix,'--spec','/state/owner-spec.json','--output','/state/bind-plan.json'],
                   capture_output=True,text=True,check=True,timeout=30)
    value=read_json(Path('/state/bind-plan.json'))
    if session:session.approve(Path('/state/bind-plan.json'))
    assert read_json(state/'config.json')==original_config
    key=grant(config,'old_owner',2)['allocation']['cache']['key']
    output=Path(old_slot['path'])/key/'canonical.mlir';original=output.stat();sha=hashlib.sha256(output.read_bytes()).hexdigest()
    faults=[]
    for boundary in ['journal','cache_control','cache_marker','new_control','new_marker','config','metadata','receipt','handoff','published']:
        killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,json,os,signal
sys.path.insert(0,"/payload/code");import node_owner_admin as a
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.apply("/opt/owner-node",json.load(open("/state/bind-plan.json")))
''',boundary],capture_output=True,text=True,timeout=30)
        assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-2000:])
        try:deploy.verify(prefix)
        except deploy.DeployError:pass
        else:raise AssertionError('Pending or unacknowledged binding allowed normal startup/maintenance')
        info=output.stat();assert (info.st_ino,info.st_uid,info.st_gid)==(original.st_ino,original.st_uid,original.st_gid)
        assert hashlib.sha256(output.read_bytes()).hexdigest()==sha
        faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_used':True,'old_data_preserved':True})
    completed=subprocess.run([*cli,'node-owner-apply','--prefix',prefix,'--plan','/state/bind-plan.json'],
                             capture_output=True,text=True,check=True,timeout=30)
    result=json.loads(completed.stdout);assert result['applied'] and result['already_applied'] and result['startup_blocked']
    assert not (state/admin.JOURNAL).exists() and (state/admin.AWAIT).exists()
    current=read_json(state/'config.json');new=Pool(state,current);new.assert_idle()
    from persistent_cache import record
    snapshot=value['caches'][old_slot['storage_id']]['state']
    assert record(new.paths(old_slot)/'state.json')=={**snapshot,'binding':new.binding(old_slot),'generation':snapshot['generation']+1}
    assert current['users']['old_owner']==config['users']['old_owner'] and current['environment_manifest']==config['environment_manifest']
    assert current['workspace']['slots'][:-1]==config['workspace']['slots']
    assert current['persistent_cache']['slots'][:-1]==config['persistent_cache']['slots']
    assert current['workspace']['slots'][-1]==work_slot and current['persistent_cache']['slots'][-1]==cache_slot
    assert current['users']['new_owner']=={'uid':23802,'gid':23802}
    renewed=Lease(new,grant(current,'old_owner',3));renewed.prepare();warm=run(renewed);renewed.release(termination_confirmed=True)
    assert not warm['compiled'] and warm['calls']==1 and warm['sha256']==cold['sha256'] and warm['inode']==cold['inode']
    another=Lease(new,grant(current,'new_owner',4));another.prepare();fresh=run(another);another.release(termination_confirmed=True)
    assert fresh['compiled'] and fresh['uid']==23802 and another.path!=renewed.path
    from worker import Worker
    try:Worker(current)
    except deploy.DeployError:pass
    else:raise AssertionError('Direct Worker startup bypassed pending central acknowledgement')
    try:slots.plan(prefix,work,[{'uid':23803,'bytes':16*1024**2,'inodes':128}])
    except deploy.DeployError:pass
    else:raise AssertionError('Native UID append bypassed pending owner handoff')
    try:activate(prefix,archive_path='/payload/server-before-owner.tar.gz')
    except deploy.DeployError:pass
    else:raise AssertionError('Code rollback bypassed pending central acknowledgement')
    report={'passed':True,'installed_node_cli_plan_apply':True,'fixture_stage_identity_only':True,
        'actual_central_contact':False,'active_lease_rejected':True,'shared_service_lock_rejected':True,'read_only_plan':True,
        'stale_cache_control_plan_rejected_before_mutation':True,'old_cache_metadata_exact_before_warm_reacquire':True,
        'fault_chain':faults,'fault_scope':'Ten successive SIGKILL boundaries in one unchanged plan, followed by installed CLI receipt replay',
        'old_namespace_inode_uid_gid_and_ir_sha_preserved':True,'old_cache_control_keys_preserved':True,
        'cold_ir':cold,'warm_ir':warm,'new_owner_ir':fresh,'same_old_namespace_key':renewed.key==first.key,
        'old_mapping_slots_environment_preserved':True,'new_uid_work_and_cache_bound':True,
        'worker_startup_blocked_before_central_ack':True,'native_append_blocked_before_central_ack':True,
        'rollback_blocked_before_central_ack':True,'genuine_prior_code_rollback_reupgrade_before_binding':True,
        'old_target_new_administration_rejected':True,'rollback_baseline_commit':BASELINE,
        'accounts_enabled':False,'full_owner_onboarding_ready':False,
        'scope':'Same-host installed Node, fixture stage ID, private native cache preservation and Root metadata migration only. Central approval/ACK, final identity activation and real multi-host remain unimplemented.'}
    if session:report.update(session.finish(prefix,value,current))
    print(json.dumps(report),flush=True)


def parent(central_handoff=False,activate_owner=False,unit_refresh=False,activation_abort=False,unit_manager=False,unit_start=False,owner_finish=False,owner_expand=False,node_provision=False,owner_provision=False):
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
    from deployment import bundle,unpack_release,CORE
    from docker_runtime import discover,command,binary
    from disk_workspace_fixture import OwnedDisk
    from lab_client import read_json,atomic_json,now
    jobdir=Path(os.environ['LAB_JOB_DIR']);job=read_json(jobdir/'dispatch.json');assert job['resources']['npu']==0
    base=jobdir/'tmp/node-owner-binding';base.mkdir(mode=0o700);payload=base/'payload';payload.mkdir(mode=0o755);payload.chmod(0o755)
    state=base/'control';state.mkdir(mode=0o700)
    archive=payload/'server.tar.gz';release=bundle(archive);code,manifest=unpack_release(archive,payload);code.rename(payload/'code');archive.chmod(0o444)
    historical=base/'historical-source';historical.mkdir(mode=0o700)
    for name in subprocess.check_output(['git','ls-tree','-r','--name-only',BASELINE],cwd=root,text=True).splitlines():
        if '/' not in name or name.startswith(('web/','examples/')):
            path=historical/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(subprocess.check_output(['git','show',BASELINE+':'+name],cwd=root))
    old=runpy.run_path(str(historical/'deployment.py'));assert 'node_owner_admin.py' not in old['CORE']
    old['bundle'](payload/'server-before-owner.tar.gz',source=historical)
    (payload/'probe.py').write_bytes(Path(__file__).read_bytes());(payload/'probe.py').chmod(0o444)
    if central_handoff:
        (payload/'handoff_fixture.py').write_bytes((root/'tests/owner_handoff_root_fixture.py').read_bytes());(payload/'handoff_fixture.py').chmod(0o444)
    if unit_manager:
        (payload/'unit_manager_fixture.py').write_bytes((root/'tests/unit_manager_root_fixture.py').read_bytes());(payload/'unit_manager_fixture.py').chmod(0o444)
    if unit_start:
        (payload/'unit_start_fixture.py').write_bytes((root/'tests/unit_start_root_fixture.py').read_bytes());(payload/'unit_start_fixture.py').chmod(0o444)
    if owner_finish:
        (payload/'owner_finish_fixture.py').write_bytes((root/'tests/owner_finish_root_fixture.py').read_bytes());(payload/'owner_finish_fixture.py').chmod(0o444)
    if owner_expand:
        (payload/'owner_expand_fixture.py').write_bytes((root/'tests/owner_expand_root_fixture.py').read_bytes());(payload/'owner_expand_fixture.py').chmod(0o444)
    if node_provision:
        (payload/'node_provision_fixture.py').write_bytes((root/'tests/node_provision_root_fixture.py').read_bytes());(payload/'node_provision_fixture.py').chmod(0o444)
    if owner_provision:
        (payload/'owner_provision_fixture.py').write_bytes((root/'tests/owner_provision_root_fixture.py').read_bytes());(payload/'owner_provision_fixture.py').chmod(0o444)
        (payload/'owner_drain_fixture.py').write_bytes((root/'tests/owner_drain_root_fixture.py').read_bytes());(payload/'owner_drain_fixture.py').chmod(0o444)
        (payload/'owner_busy_activation_fixture.py').write_bytes((root/'tests/owner_busy_activation_root_fixture.py').read_bytes());(payload/'owner_busy_activation_fixture.py').chmod(0o444)
    (payload/'input.mlir').write_text('module { func.func @add() -> i32 { %a = arith.constant 19 : i32\n %b = arith.constant 23 : i32\n %r = arith.addi %a, %b : i32\n return %r : i32 } }\n');(payload/'input.mlir').chmod(0o444)
    binding,_=discover();cpus=job['allocation']['cpu_ids']
    context={'job_id':job['id'],'attempt_id':job['attempt_id'],'runtime':binding,'central_handoff':central_handoff,'activate_owner':activate_owner,'unit_refresh':unit_refresh,'activation_abort':activation_abort,'unit_manager':unit_manager,'unit_start':unit_start,'owner_finish':owner_finish,'owner_expand':owner_expand,'node_provision':node_provision,'owner_provision':owner_provision}
    if activate_owner:context['node_state']=str(state/'node')
    def docker(args,check=True,timeout=45):
        argv,env=command(binding,['docker',*map(str,args)]);r=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
        if check and r.returncode:raise RuntimeError('Owned Node owner fixture failed: '+r.stderr[-1600:])
        return r
    prefix='ascend-lab-nodeowner-'+uuid.uuid4().hex[:8];name=prefix+'-node';volumes=[];disks=[];made_role=False;report=None
    try:
        choices=[('quota-workspace','/work-quota',128,'user'),('cache-workspace','/cache-quota',128,'user')]
        if central_handoff:choices += [('central-payload','/central-quota',256,'group'),('spool-workspace','/spool-quota',1024 if activate_owner else 128,None)]
        if owner_expand:choices += [('expand-work','/expand-work',128,'user'),('expand-cache','/expand-cache',128,'user'),('expand-spool','/expand-spool',1024,None)]
        for folder,destination,size,quota_type in choices:
            disk=OwnedDisk(base,prefix+'-'+folder,IMAGE,cpus,docker,folder_name=folder,image_mib=size,
                inodes=262144 if activate_owner and folder in {'spool-workspace','expand-spool'} else 4096,quota_type=quota_type);disks.append(disk)
            device=disk.create();volume=prefix+'-'+folder
            options='nosuid,nodev'+(',grpquota' if quota_type=='group' else ',usrquota' if quota_type=='user' else '')
            docker(['volume','create','--label','io.ascend-lab.nodeowner='+prefix,'--driver','local','--opt','type=ext4',
                '--opt','device='+device,'--opt','o='+options,volume]);volumes.append((volume,destination))
            if activate_owner:
                mountpoint=json.loads(docker(['volume','inspect',volume]).stdout)[0]['Mountpoint']
                volumes[-1]=(volume,mountpoint)
                context[{'quota-workspace':'work_path','cache-workspace':'cache_path','central-payload':'central_path','spool-workspace':'spool_path',
                    'expand-work':'expand_work_path','expand-cache':'expand_cache_path','expand-spool':'expand_spool_path'}[folder]]=mountpoint
        atomic_json(payload/'context.json',context)
        socket=binding['endpoint'].removeprefix('unix://')
        args=['create','--name',name,'--label','io.ascend-lab.nodeowner='+prefix,'--network','none','--ipc','none','--read-only',
            '--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','FOWNER','--cap-add','DAC_OVERRIDE',
            '--cap-add','SETUID','--cap-add','SETGID','--cap-add','KILL','--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined',
            '--memory','512m','--memory-swap','512m','--cpuset-cpus',str(cpus[0]),'--cpus','0.8','--pids-limit','96',
            '--tmpfs','/opt:rw,nosuid,nodev,size=32m,mode=755','--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777',
            *(['--tmpfs','/personal:rw,nosuid,nodev,size=16m,mode=755'] if activate_owner else []),
            '--mount','type=bind,src='+str(payload)+',dst=/payload,readonly','--mount','type=bind,src='+str(state)+',dst=/state',
            '--mount','type=bind,src='+binary()+',dst=/usr/bin/docker,readonly','--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly',
            '--mount','type=bind,src='+socket+',dst='+socket]
        for volume,destination in volumes:args+=['--mount','type=volume,src='+volume+',dst='+destination]
        if activate_owner:args+=['--mount','type=bind,src='+str(state)+',dst='+str(state)]
        args+=['--entrypoint','/usr/bin/timeout',IMAGE,'--kill-after=3s','300s','/usr/bin/python3','-I','-B','/payload/probe.py','--controller']
        docker(args);made_role=True
        result=docker(['start','--attach',name],check=False,timeout=310)
        atomic_json(base/'role-exit.json',{'returncode':result.returncode});(base/'role.stdout').write_text(result.stdout);(base/'role.stderr').write_text(result.stderr)
        info=json.loads(docker(['inspect',name]).stdout)[0];assert not info['State']['Running']
        if result.returncode or info['State']['ExitCode']:raise RuntimeError('Node owner Root acceptance failed; original logs retained')
        report=json.loads(result.stdout);assert report['passed'];report.update(parent_job=job['id'],at=now(),server_release=release['release_id'],
            production_source_sha256={n:r['sha256'] for n,r in manifest['files'].items() if n in CORE and not n.endswith('.md')},
            npu_kernel_launches=0,model_invocations=0)
    finally:
        if made_role:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.nodeowner']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            cleanup_owned_attempts(state,base,cpus,docker)
            repair='import os;p="/state"\nfor base,dirs,files in os.walk(p,followlinks=False):\n for n in dirs+files:os.lchown(os.path.join(base,n),'+str(os.getuid())+','+str(os.getgid())+')\nos.chown(p,'+str(os.getuid())+','+str(os.getgid())+')'
            docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--memory','64m',
                '--cpuset-cpus',str(cpus[0]),'--mount','type=bind,src='+str(state)+',dst=/state','--entrypoint','/usr/bin/python3',IMAGE,'-I','-c',repair]);docker(['rm',name])
        for volume,_ in reversed(volumes):docker(['volume','rm',volume])
        for disk in reversed(disks):disk.cleanup()
    assert report is not None;report['disks']=[d.report for d in disks]
    report['cleanup_confirmed']=all(d.report['loop_association_released'] and d.report['helper_removed'] for d in disks)
    atomic_json(jobdir/'results/node-owner-binding.json',report);print(json.dumps(report))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor',action='store_true');parser.add_argument('--controller',action='store_true')
    parser.add_argument('--with-regression',action='store_true');parser.add_argument('--central-handoff',action='store_true');parser.add_argument('--activate-owner',action='store_true');parser.add_argument('--unit-refresh',action='store_true');parser.add_argument('--activation-abort',action='store_true');parser.add_argument('--unit-manager',action='store_true');parser.add_argument('--unit-start',action='store_true');parser.add_argument('--owner-finish',action='store_true');parser.add_argument('--owner-expand',action='store_true');parser.add_argument('--node-provision',action='store_true');parser.add_argument('--owner-provision',action='store_true');args=parser.parse_args()
    if args.actor:actor()
    elif args.controller:controller()
    else:
        root=Path(__file__).resolve().parents[1]
        if args.owner_provision:args.node_provision=True
        if args.node_provision:args.owner_expand=True
        if args.owner_expand:args.owner_finish=True
        if args.with_regression:
            subprocess.run(['/usr/bin/python3','-m','unittest','discover','-s','tests','-p','test_node_owner_admin.py','-v'],cwd=root,check=True)
        parent(args.central_handoff or args.activate_owner or args.unit_refresh or args.activation_abort or args.unit_manager or args.unit_start or args.owner_finish,
               args.activate_owner or args.unit_refresh or args.activation_abort or args.unit_manager or args.unit_start or args.owner_finish,args.unit_refresh or args.activation_abort or args.unit_manager or args.unit_start or args.owner_finish,
               args.activation_abort or args.unit_manager or args.unit_start or args.owner_finish,args.unit_manager or args.unit_start or args.owner_finish,args.unit_start or args.owner_finish,args.owner_finish,args.owner_expand,args.node_provision,args.owner_provision)
        if args.with_regression:subprocess.run(['bash','scripts/check.sh','/usr/bin/python3'],cwd=root,check=True)

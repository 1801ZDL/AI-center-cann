"""Actual owned Root install/restore, private namespaces and outside mount cover.

Extends the standard CPU installation fixture after both original daemons stop.
No host mount/account/service is created or changed.
"""
import hashlib
import json
from pathlib import Path
import time


def run(ctx):
    docker=ctx['docker'];api=ctx['api'];check=ctx['check'];ctl=ctx['ctl']
    controller=ctx['controller'];node=ctx['node'];prefix='/opt/ascend-lab-native-controller'
    uid=ctx['installed']['uid'];gid=ctx['installed']['gid'];root=ctx['ROOT']
    def monitored(*args,expected=0,user=None,rendered=False):
        result=docker(['exec','--user',str(uid)+':'+str(gid) if user is None else user,controller,
            '/usr/bin/python3','-B','/bootstrap/servicectl','control-monitor','--prefix',prefix,*args],check=False)
        assert result.returncode==expected,result.stderr
        return result.stdout if rendered else json.loads(result.stdout)
    def native(action,*args,check=True):
        try:result=docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl',action,'--prefix',prefix,*args],check=check,timeout=45)
        except Exception:
            diagnostic=r'''import json,sys
from pathlib import Path
p=Path(sys.argv[1]);out={}
for key,path in [('handle',p/'native-control-process.json'),('log',Path('/srv/native0/state/bootstrap.log')),('restored_log',Path('/srv/native1/state/bootstrap.log'))]:
 try:out[key]=path.read_text()[-10000:]
 except OSError:pass
try:
 h=json.loads(out['handle']);pid=h['pid'];q=Path('/proc')/str(pid);out['cmdline']=q.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode();out['process_status']=q.joinpath('status').read_text()
except (OSError,KeyError):pass
print(json.dumps(out))
'''
            detail=docker(['exec',controller,'/usr/bin/python3','-I','-c',diagnostic,prefix],check=False)
            (ctx['base']/'native-control-failure.json').write_text(detail.stdout+detail.stderr)
            raise
        return json.loads(result.stdout) if check else result
    prepare=r'''import json,os,sys,uuid
from pathlib import Path
out=[]
for i in range(6):
 p=Path('/srv/native'+str(i));assert sorted(x.name for x in p.iterdir())==['lost+found']
 (p/'lost+found').rmdir();p.chmod(0o755 if i in {0,1,4} else 0o700);s=p.stat();v=os.statvfs(p)
 q={'path':str(p),'filesystem':'ext4','device':s.st_dev,'root_inode':s.st_ino,'bytes':v.f_blocks*v.f_frsize,'inodes':v.f_files,'storage_id':uuid.uuid4().hex}
 f=Path('/provision/native'+str(i)+'.json');f.write_text(json.dumps(q));f.chmod(0o600);out.append(q)
print(json.dumps(out))
'''
    policies=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',prepare]).stdout)
    args=['--archive','/provision/server-v1.tar.gz','--role','controller','--state-dir','/srv/native0/state',
          '--uid',str(uid),'--gid',str(gid),'--port',str(ctx['port']),
          '--control-config','/provision/native0.json','--payload-config','/provision/native2.json']
    # An empty bind-mounted subdirectory is not a dedicated finite filesystem.
    bound=False
    try:
        fake=r'''import json,os,subprocess,uuid
from pathlib import Path
source=Path('/srv/native4/control-subdir');source.mkdir(mode=0o755);target=Path('/srv/native-subdir');target.mkdir(mode=0o755)
subprocess.run(['mount','--bind',str(source),str(target)],check=True)
s=target.stat();v=os.statvfs(target);q={'path':str(target),'filesystem':'ext4','device':s.st_dev,'root_inode':s.st_ino,'bytes':v.f_blocks*v.f_frsize,'inodes':v.f_files,'storage_id':uuid.uuid4().hex}
p=Path('/provision/native-subdir.json');p.write_text(json.dumps(q));p.chmod(0o600)
'''
        docker(['exec',controller,'/usr/bin/python3','-I','-c',fake]);bound=True
        refused=native('doctor',*[{'/srv/native0/state':'/srv/native-subdir/state','/provision/native0.json':'/provision/native-subdir.json'}.get(x,x) for x in args],check=False)
        refusal=json.loads(refused.stdout)
        assert not refusal['install_ready'] and 'finite_control_filesystem' in refusal['blockers']
    finally:
        if bound:
            docker(['exec',controller,'umount','/srv/native-subdir'])
            docker(['exec',controller,'/usr/bin/python3','-I','-c',"from pathlib import Path;p=Path('/srv/native4/control-subdir');assert not list(p.iterdir());p.rmdir()"])
    # Explicitly test new installation, not only restore into an existing prefix.
    plan=native('doctor',*args);assert plan['install_ready'] and plan['read_only'],plan['blockers']
    installed=native('install',*args);assert installed['state_dir']=='/srv/native0/state'
    assert native('start')['ready'];fresh=native('status');assert fresh['running']
    assert not native('stop')['running']
    check('Fresh Root installation binds separate finite control/payload disks and starts through the protected namespace launcher')
    restored=native('restore','--archive','/root/backups/controller.tar.gz','--new-state-dir','/srv/native1/state',
                    '--control-config','/provision/native1.json','--payload-config','/provision/native3.json')
    assert restored['finite_control_restored'] and restored['dedicated_payload_restored']
    assert native('start')['ready'] and ctl(node,'node','start')['ready']
    # A direct unprivileged start must fail before listening or opening fallback DBs.
    direct=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-B','/bootstrap/servicectl','run','--prefix',prefix],check=False)
    assert direct.returncode!=0
    inspect=r'''import json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]+'/installation.json').read_text());h=json.loads(Path(sys.argv[1]+'/native-control-process.json').read_text());q=Path('/proc/'+str(h['pid']));s={k:v.strip() for k,sep,v in (line.partition(':') for line in q.joinpath('status').read_text().splitlines()) if sep}
e=dict(part.split(b'=',1) for part in q.joinpath('environ').read_bytes().split(b'\0') if b'=' in part);tmp=m['state_dir']+'/tmp';bound=all(e.get(key)==tmp.encode() for key in [b'TMPDIR',b'SQLITE_TMPDIR']) and Path(tmp).stat().st_dev==m['control_filesystem']['device']
print(json.dumps({'uid':list(map(int,s['Uid'].split())),'effective':int(s['CapEff'],16),'permitted':int(s['CapPrm'],16),'private_namespace':h['private_namespace']!=h['origin_namespace'],'temporary_files_bound':bound,'pid':h['pid']}))
'''
    identity=json.loads(docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-I','-c',inspect,prefix]).stdout)
    assert identity['uid']==[uid]*4 and identity['effective']==identity['permitted']==0 and identity['private_namespace'] and identity['temporary_files_bound']
    check('Restored controller is the configured unprivileged UID with zero effective/permitted capabilities; direct launch without protected descriptors is refused')
    healthy=monitored();assert healthy['status']=='ready' and healthy['check_success']
    assert healthy['audit_retention']['maximum_unarchived_rows']==10000 and healthy['audit_retention']['archive_before_drop'] is True
    warned=monitored('--warning-free-mib','300',expected=1);assert warned['status']=='warning' and not warned['admission_blocks_new_work']
    critical=monitored('--warning-free-mib','400','--critical-free-mib','300',expected=2)
    assert critical['status']=='critical' and not critical['admission_blocks_new_work']
    inode=monitored('--warning-free-inodes','10000','--critical-free-inodes','9000',expected=2)
    assert inode['status']=='critical' and 'control_free_inodes' in inode['reason_codes']
    peer=monitored(expected=3,user='23004:23004');assert peer['status']=='unknown' and peer['components'] is None
    text=monitored('--format','prometheus',rendered=True)
    assert 'ascend_lab_control_check_success 1\n' in text and '/srv/' not in text and 'password' not in text
    (ctx['base']/'control-monitor.prom').write_text(text)
    check('Installed service UID exports fixed-file control metrics without HTTP dependency; independent warning/critical byte and inode alerts do not change admission; peer UID is refused')
    from lab_client import Client
    from environment_contract import lock_for
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    source=ctx['base']/'native-control-source';source.mkdir(mode=0o700)
    prologue="""import time,uuid
from pathlib import Path
marker=uuid.uuid4().hex
Path('native-marker').write_text(marker)
print('native-original-marker='+marker,flush=True)
time.sleep(12)
"""
    (source/'ir.py').write_text(prologue+(root/'examples/workflows/ir.py').read_text())
    (source/'input.mlir').write_bytes((root/'examples/workflows/input.mlir').read_bytes())
    bundle=client.upload_input(source)
    request={'template':'ir_v1','environment':'worker-python','idempotency_key':'native-control-original',
             'parameters':{'input_id':bundle['id'],'argv':['python','ir.py'],
                           'lock':lock_for(ctx['configuration']['environment_manifest']),'timeout_seconds':60}}
    status,value=api('/api/jobs',request);assert status==201,value
    identifier=value['job']['id']
    follower_request={'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'native-control-follower'}
    status,value=api('/api/jobs',follower_request);assert status==201,value
    follower=value['job']['id'];deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        detail=api('/api/jobs/'+identifier)[1]
        if detail['job']['state']=='running':break
        time.sleep(.1)
    assert detail['job']['state']=='running',detail['job']['state'];attempt=detail['job']['attempt_id']
    before=api('/api/control-storage')[1];assert before['filesystem_enforced'] and not before['blocks_new_work']
    cover=r'''import json,os,subprocess,sys
from pathlib import Path
p=Path('/srv/native-wrong');p.mkdir();(p/'state').mkdir();os.chown(p/'state',int(sys.argv[1]),int(sys.argv[2]));(p/'state').chmod(0o700)
(p/'state/sentinel').write_bytes(b'owned wrong filesystem; never a controller state')
subprocess.run(['mount','--bind',str(p),'/srv/native1'],check=True)
print(json.dumps({'covered':True,'wrong_device':p.stat().st_dev}))
'''
    covered=False
    try:
        separation=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',cover,str(uid),str(gid)]).stdout);covered=True
        assert separation['wrong_device']!=policies[1]['device']
        pressure=api('/api/control-storage')[1];assert pressure['blocks_new_work'] and pressure['status']=='unavailable'
        observed=monitored(expected=2)
        assert observed['status']=='critical' and observed['reason_codes']==['required_control_filesystem_unavailable'] and observed['components'] is None
        missing_metrics=monitored('--format','prometheus',expected=2,rendered=True)
        assert 'ascend_lab_control_check_success 0\n' in missing_metrics and 'ascend_lab_control_free_bytes 0\n' not in missing_metrics
        (ctx['base']/'control-monitor-unavailable.prom').write_text(missing_metrics)
        assert '/srv/' not in json.dumps(pressure)
        status,blocked=api('/api/jobs',{**follower_request,'idempotency_key':'native-control-rejected'});assert status==503,blocked
        running=native('status');assert running['running'] and not running['ready'] and not running['control_filesystem_available']
        done=ctx['wait'](identifier);assert done['state']=='succeeded' and done['attempt_id']==attempt,done
        waiting=api('/api/jobs/'+follower)[1]['job'];assert waiting['state']=='queued' and not waiting.get('attempt_id'),waiting
        assert not native('stop')['running']
        assert monitored(expected=2)['status']=='critical'
        unchanged=docker(['exec',controller,'/usr/bin/python3','-I','-c',
            "from pathlib import Path;p=Path('/srv/native-wrong/state');assert sorted(x.name for x in p.iterdir())==['sentinel'];assert (p/'sentinel').read_bytes()==b'owned wrong filesystem; never a controller state'"])
        assert unchanged.returncode==0
        check('Outside control-mount replacement pauses new jobs/claims while the original real IR task uploads/completes once on the pinned database; no fallback files appear')
        check('Protected Root process identity stops the exact original controller even while its outside control mount is unavailable')
    finally:
        if covered:docker(['exec',controller,'umount','/srv/native1'])
    assert native('start')['ready']
    final=ctx['wait'](follower);assert final['state']=='succeeded',final
    recovered=api('/api/jobs/'+identifier)[1]['job'];assert recovered['state']=='succeeded' and recovered['attempt_id']==attempt
    status,data=api('/api/jobs/'+identifier+'/artifact?name=ir%2Fcanonical.mlir',raw=True)
    assert status==200 and b'arith.constant 42' in data
    original_replay=client.request('/api/jobs',request)['job'];assert original_replay['id']==identifier
    assert not ctl(node,'node','stop')['running'] and not native('stop')['running']
    assert monitored()['status']=='ready'  # Capacity readiness is not service liveness.
    export=r'''import json,os,sqlite3,sys
from pathlib import Path
source=sqlite3.connect('file:/srv/native1/state/jobs.sqlite3?mode=ro',uri=True);target=Path(sys.argv[1]);assert not target.exists()
with sqlite3.connect(target) as dst:source.backup(dst);assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
rows={j:source.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(j,)).fetchone()[0] for j in sys.argv[2:]};source.close();target.chmod(0o600)
assert set(rows.values())=={1},rows
print(json.dumps(rows))
'''
    audit_proof=None
    if ctx['options'].audit_retention:
        from installed_audit_retention_check import run as audit_run
        audit_proof=audit_run(ctx,native,prefix)
    exported=ctx['controller_state']/'native-control-proof.sqlite3'
    attempts=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',export,str(exported),identifier,follower]).stdout)
    backup=native('backup','--output','/root/backups/native-controller.tar.gz');assert backup['quiescent'] and backup['includes_dedicated_payload']
    guard=docker(['exec',controller,'/usr/bin/python3','-I','-c',
        "import json,tarfile;f=tarfile.open('/root/backups/native-controller.tar.gz');m=json.load(f.extractfile('snapshot.json'));assert m['control_filesystem']['path']=='/srv/native1';print(m['format'])"])
    assert guard.stdout.strip()=='ascend-lab-snapshot/v2'
    check('Uncover/restart retains both original attempts, resumes only the queued follower and produces a stopped SQLite export plus a native-binding snapshot')
    refused=native('restore','--archive','/root/backups/native-controller.tar.gz','--new-state-dir','/srv/native4/state',
                   '--payload-config','/provision/native5.json',check=False)
    assert refused.returncode!=0
    assert docker(['exec',controller,'/usr/bin/python3','-I','-c',"from pathlib import Path;assert not Path('/srv/native4/state').exists();assert not list(Path('/srv/native5').iterdir())"]).returncode==0
    roundtrip=native('restore','--archive','/root/backups/native-controller.tar.gz','--new-state-dir','/srv/native4/state',
                     '--control-config','/provision/native4.json','--payload-config','/provision/native5.json')
    assert roundtrip['finite_control_restored'] and roundtrip['previous_state_retained']=='/srv/native1/state'
    assert native('start')['ready'] and ctl(node,'node','start')['ready']
    recovered=api('/api/jobs/'+identifier)[1]['job'];assert recovered['state']=='succeeded' and recovered['attempt_id']==attempt
    status,roundtrip_ir=api('/api/jobs/'+identifier+'/artifact?name=ir%2Fcanonical.mlir',raw=True)
    assert status==200 and roundtrip_ir==data
    status,value=api('/api/jobs',{**request,'idempotency_key':'native-control-after-native-snapshot'});assert status==201,value
    after_roundtrip=ctx['wait'](value['job']['id']);assert after_roundtrip['state']=='succeeded'
    assert not ctl(node,'node','stop')['running'] and not native('stop')['running']
    if audit_proof:
        refused=native('audit-prune','--archive',audit_proof['archive'],
            '--confirm-sha256',audit_proof['sha256'],check=False)
        assert refused.returncode!=0
        audit_proof['restored_epoch_refused_old_archive']=True
    roundtrip_export=ctx['controller_state']/'native-roundtrip-proof.sqlite3'
    roundtrip_counts=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',export.replace('/srv/native1/state/jobs.sqlite3','/srv/native4/state/jobs.sqlite3'),str(roundtrip_export),identifier,follower,after_roundtrip['id']]).stdout)
    check('Native snapshot refuses an ordinary-directory restore, then rebinds fresh control/payload disks and runs another real IR attempt with original results intact')
    return {'passed':True,'fresh_install':True,'bound_subdirectory_refused':True,'restore_ordinary_snapshot_to_native':True,
        'external_monitor':{'passed':True,'actual_uid':uid,'ready':0,'warning':1,'critical':2,'unknown':3,
            'byte_inode_alerts':True,'admission_unchanged_by_alerts':True,'peer_uid_refused':True,
            'native_mount_loss_critical':True,'no_fallback_stat_or_config':True,'stopped_controller_supported':True,
            'prometheus_exported':True,'audit_archive_required_disclosed':True,'audit_archive_maintenance':False},
        **({'audit_retention':audit_proof} if audit_proof else {}),
        'configured_uid':uid,'actual_identity':identity,'separate_filesystems':True,
        'control_capacity_bytes':policies[1]['bytes'],'control_capacity_inodes':policies[1]['inodes'],
        'outside_mount_cover':True,'fallback_untouched':True,'new_submission_http':503,
        'existing_attempt_completed_during_cover':True,'stop_during_cover':True,'restart_retains_original':True,
        'attempt_counts':attempts,'original_job':identifier,'follower_job':follower,'canonical_ir_sha256':hashlib.sha256(data).hexdigest(),
        'exported_database':str(exported),'native_snapshot_created':True,'native_snapshot_restored':True,
        'restore_requires_explicit_native_contract':True,'roundtrip_database':str(roundtrip_export),'roundtrip_attempt_counts':roundtrip_counts,'scope':'Owned Root containers and numeric service/task UIDs on one physical server; no host accounts, real systemd PID1 or two-host fencing acceptance'}

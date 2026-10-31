"""Offline, quiescent snapshots. No live leases are discarded or re-executed."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tarfile
import tempfile

from lab_client import atomic_json,read_json
from state_lock import state_lock
from deployment import DeployError,digest,load_installation,require_root,safe_relative,secure_parent,unit_text,verify,checked_manifest,regular_json

EXCLUDE={'.state.lock','.service-run.lock','scheduler.lock','worker.lock','service-process.json','server.pid',
         'server-ready.json','worker-process.json','heartbeat-receipt.json','bootstrap.log','server.log','server.log.1','server.log.2','worker.log'}


def tree(root):
    """An explicitly registered root is allowed; nested mounts/links are not."""
    from storage_files import mount
    root=Path(root);fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);result=[]
    try:
        device=os.fstat(fd).st_dev;mount_id=mount(fd)
        def visit(parent,relative,depth):
            if depth>128:raise DeployError('Snapshot tree exceeds maximum depth')
            for name in sorted(os.listdir(parent)):
                if len(result)>=100000:raise DeployError('Snapshot tree exceeds maximum entry count')
                child=os.open(name,os.O_PATH|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    item=os.fstat(child);rel=relative/name
                    if item.st_dev!=device or mount(child)!=mount_id:raise DeployError('Snapshot refuses unregistered nested mounts')
                    if not stat.S_ISDIR(item.st_mode) and (not stat.S_ISREG(item.st_mode) or item.st_nlink!=1):raise DeployError('Snapshot refuses links and special files')
                    result.append((rel,item))
                    if stat.S_ISDIR(item.st_mode):
                        nested=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                        try:visit(nested,rel,depth+1)
                        finally:os.close(nested)
                finally:os.close(child)
        visit(fd,Path(),0);return result
    finally:os.close(fd)


def assert_quiescent(state,role,check_spool=True):
    state=Path(state)
    if role=='node':
        from node_owner_provision import guard
        guard(state)
    if role=='controller':
        if list((state/'owner-activations').glob('*.pending.json')):
            raise DeployError('Finish pending owner activation credential delivery before offline maintenance')
        if list((state/'owner-activation-aborts').glob('*.pending.json')):
            raise DeployError('Finish pending owner activation abort before offline maintenance')
        path=state/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_inventory_holds'").fetchone():
                    if db.execute('SELECT 1 FROM worker_inventory_holds WHERE held=1 LIMIT 1').fetchone():
                        raise DeployError('Reconcile every node inventory hold before offline maintenance')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_node_handoffs'").fetchone():
                    if db.execute("SELECT 1 FROM owner_node_handoffs WHERE state IN ('approved','committed') LIMIT 1").fetchone():
                        raise DeployError('Finish pending owner handoffs before offline controller maintenance')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qualification_compatibility'").fetchone():
                    if db.execute("SELECT 1 FROM qualification_compatibility WHERE state='pending' LIMIT 1").fetchone():
                        raise DeployError('Finish pending qualification compatibility decisions before offline maintenance')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_lifecycle'").fetchone():
                    if db.execute("SELECT 1 FROM storage_lifecycle WHERE state IN ('moving_to_trash','restoring','purging') LIMIT 1").fetchone():
                        raise DeployError('Finish pending storage maintenance before offline backup or activation')
                for state_value,encoded in db.execute('SELECT state,data FROM jobs'):
                    if state_value not in {'succeeded','failed','cancelled','timed_out'} or json.loads(encoded).get('state')!=state_value:
                        raise DeployError('Stop admission and finish/cancel all queued or active jobs before offline maintenance')
    else:
        for path in (state/'attempts').glob('*/control/grant.json'):
            execution=read_json(path.parent/'state.json') or {}
            if execution.get('stage')!='cleaned' or execution.get('controller_acknowledged') is not True:
                raise DeployError('Node has an unfinished or unacknowledged execution; state cannot be backed up or switched')
            if execution.get('artifact_gc_requested') or execution.get('artifact_gc',{}).get('state')=='pending':
                raise DeployError('Finish pending node artifact recovery before backup or code activation')
        config=read_json(state/'config.json') or {}
        if check_spool and config.get('spool_storage'):
            from spool_storage import Spool
            Spool(state,config).assert_empty()
        if check_spool and config.get('persistent_cache'):
            from persistent_cache import Pool
            Pool(state,config).assert_idle()


def snapshot(prefix,output,max_bytes=10*1024**3):
    require_root();prefix,meta,_=verify(prefix);state=Path(meta['state_dir'])
    output=Path(output).absolute()
    if output.exists() or output.is_symlink() or output.is_relative_to(state):raise DeployError('Backup target must be new and outside the state tree')
    output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    parent=output.parent.stat()
    if meta.get('control_filesystem') and parent.st_dev==meta['control_filesystem']['device']:
        raise DeployError('Backup output must use a separate control filesystem')
    if parent.st_uid!=0 or parent.st_mode&0o077:raise DeployError('Backups need a root-owned private parent directory (700)')
    from control_process import offline
    with state_lock(state,exclusive=True),offline(prefix,meta):
        assert_quiescent(state,meta['role'])
        policy=meta.get('payload_storage')
        if policy:
            from payload_storage import Payload
            Payload(state,read_json(state/'config.json'),uid=meta['uid'])
        if policy and output.is_relative_to(Path(policy['path'])):raise DeployError('Backup output cannot be on the source payload filesystem')
        staging=Path(tempfile.mkdtemp(prefix='.snapshot-',dir=output.parent))
        try:
            files={};sources={};total=0;directories=[]
            sources_roots=[state]
            if policy:sources_roots.append(Path(policy['path']))
            from payload_storage import MARKER,TREES
            for source_root in sources_roots:
              for relative,info in tree(source_root):
                name=str(relative);path=source_root/relative
                if source_root!=state and relative.parts[0] not in TREES:continue
                if name in files or name in directories:raise DeployError('Snapshot control/payload namespaces collide')
                if stat.S_ISDIR(info.st_mode):directories.append(name);continue
                if source_root==state and (name in EXCLUDE or len(relative.parts)==1 and name.endswith(('.sqlite3-wal','.sqlite3-shm','.sqlite3-journal'))):continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise DeployError('Snapshot rejects links and special files')
                source=path
                if len(Path(name).parts)==1 and name.endswith('.sqlite3'):
                    source=staging/name
                    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as src,sqlite3.connect(source) as dst:
                        src.backup(dst)
                        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise DeployError('Snapshot database integrity check failed')
                size=source.stat().st_size;total+=size
                if total>max_bytes or len(files)>=100000:raise DeployError('Snapshot exceeds configured size/file budget')
                files[name]={'size':size,'sha256':digest(source),'mode':info.st_mode&0o777};sources[name]=source
            manifest={'format':'ascend-lab-snapshot/v2' if policy else 'ascend-lab-snapshot/v1','role':meta['role'],'release_id':meta['release_id'],
                      'state_schema':meta['state_schema'],'original_state':str(state),'files':files,'directories':directories,'bytes':total,'quiescent':True}
            if policy:manifest['payload_storage']=policy
            if meta.get('control_filesystem'):manifest['control_filesystem']=meta['control_filesystem']
            if meta['role']=='node' and meta.get('spool_storage'):manifest['node_spool_storage']=meta['spool_storage']
            if meta['role']=='node' and meta.get('persistent_cache'):
                manifest['persistent_cache']=meta['persistent_cache'];manifest['persistent_cache_data_included']=False
            temporary=staging/'backup.tar.gz'
            with temporary.open('xb') as out,tarfile.open(fileobj=out,mode='w:gz') as archive:
                data=json.dumps(manifest,sort_keys=True).encode();item=tarfile.TarInfo('snapshot.json');item.size=len(data);item.mode=0o600
                if len(data)>32*1024**2:raise DeployError('Snapshot manifest exceeds restore budget; reduce the snapshot scope before retrying')
                archive.addfile(item,io.BytesIO(data))
                for name in files:
                    fd=os.open(sources[name],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    with os.fdopen(fd,'rb') as source:
                        before=os.fstat(source.fileno())
                        if before.st_size!=files[name]['size']:raise DeployError('Snapshot source size changed before archive read')
                        class CheckedReader:
                            def __init__(self):self.digest=hashlib.sha256();self.count=0
                            def read(self,size):
                                data=source.read(size);self.digest.update(data);self.count+=len(data);return data
                        checked=CheckedReader()
                        item=tarfile.TarInfo('data/'+name);item.size=files[name]['size'];item.mode=0o600
                        archive.addfile(item,checked)
                        if checked.count!=item.size or checked.digest.hexdigest()!=files[name]['sha256']:raise DeployError('Snapshot source changed while being archived')
                        after=os.fstat(source.fileno())
                        if any(getattr(before,key)!=getattr(after,key) for key in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):raise DeployError('Snapshot source metadata changed during archive read')
            temporary.chmod(0o600)
            os.link(temporary,output)
            return {'backup':str(output),'sha256':digest(output),'bytes':total,'files':len(files),'role':meta['role'],'quiescent':True,'includes_dedicated_payload':bool(policy)}
        finally:shutil.rmtree(staging)


def relocate(state,old_state,new_state,role,old_payload=None,new_payload=None):
    old_state=Path(old_state);new_state=Path(new_state)
    def move(value):
        path=Path(value)
        if old_payload and new_payload and path.is_relative_to(Path(old_payload)):
            relative=path.relative_to(Path(old_payload))
            if relative.parts and relative.parts[0] in {'jobs','inputs','storage-trash'}:return str(Path(new_payload)/relative)
        return str(new_state/path.relative_to(old_state)) if path.is_relative_to(old_state) else value
    config=read_json(state/'config.json')
    if not config:raise DeployError('Snapshot has no valid private configuration')
    if config.get('ca_file'):config['ca_file']=move(config['ca_file'])
    if isinstance(config.get('tls'),dict):
        config['tls']={key:move(value) if key in {'certificate','private_key','authority'} else value for key,value in config['tls'].items()}
    if role=='controller':
        if config.get('monitor_local') is not False or config.get('scheduler',{}).get('local_enabled') is not False:
            raise DeployError('Installer restores only dedicated controllers without local execution')
        config['identity_workspace_root']=move(config['identity_workspace_root'])
        config['scheduler']['workspace_root']=move(config['scheduler']['workspace_root'])
        path=state/'identities.sqlite3'
        if path.exists():
            with sqlite3.connect(path) as db:
                for user,encoded in db.execute('SELECT id,profile FROM users').fetchall():
                    import owner_identity
                    profile=json.loads(encoded)
                    try:profile=owner_identity.relocate(user,profile,move(profile['workspace']))
                    except ValueError as error:raise DeployError(str(error)) from None
                    db.execute('UPDATE users SET profile=? WHERE id=?',(json.dumps(profile),user))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_activations'").fetchone():
                    for user,encoded in db.execute('SELECT owner,record FROM owner_activations').fetchall():
                        record=json.loads(encoded);record['credentials']=move(record['credentials'])
                        db.execute('UPDATE owner_activations SET record=? WHERE owner=?',(json.dumps(record),user))
                        receipt=state/'owner-activations'/(user+'.json')
                        if not receipt.is_file() or receipt.is_symlink():raise DeployError('Activated owner snapshot is missing its private delivery receipt')
                        atomic_json(receipt,record)
        path=state/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect(path) as db:
                for job_id,encoded in db.execute('SELECT id,data FROM jobs').fetchall():
                    job=json.loads(encoded)
                    if job.get('archive_dir'):job['archive_dir']=move(job['archive_dir'])
                    db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(job),job_id))
        for path in (state/'workers').glob('*/config.json'):
            worker=read_json(path)
            if worker:
                worker['probe_forbidden_paths']=[move(p) for p in worker.get('probe_forbidden_paths',[])]
                atomic_json(path,worker)
    else:
        config['state_dir']=str(new_state)
        config['probe_forbidden_paths']=[move(p) for p in config.get('probe_forbidden_paths',[])]
    atomic_json(state/'config.json',config)


def restore(prefix,archive_path,new_state,max_bytes=10*1024**3,payload_config=None,spool_config=None,cache_config=None,control_config=None):
    require_root();prefix,meta,_=verify(prefix);old=Path(meta['state_dir']);target=secure_parent(new_state)
    if target.exists() or target.is_symlink() or target.is_relative_to(prefix) or prefix.is_relative_to(target):raise DeployError('Restore requires a new separate state directory')
    archive_info=Path(archive_path).lstat()
    if not stat.S_ISREG(archive_info.st_mode) or archive_info.st_mode&0o077 or archive_info.st_nlink!=1:raise DeployError('Restore requires a private regular snapshot file')
    native=None
    if control_config:
        from control_filesystem import inspect_new
        if meta['role']!='controller' or not payload_config:raise DeployError('Finite control restore requires a controller and separate --payload-config')
        native=regular_json(control_config,private=True)
        inspect_new(native,target,prefix,regular_json(payload_config,private=True))
        if meta.get('control_filesystem',{}).get('device')==native['device']:
            raise DeployError('Restore must retain the current control filesystem and use a new one')
    elif meta.get('control_filesystem'):
        raise DeployError('Finite control restore requires a new --control-config')
    from control_process import offline
    with state_lock(old,exclusive=True),offline(prefix,meta):
        assert_quiescent(old,meta['role'])
        stage=Path(tempfile.mkdtemp(prefix='.restore-',dir=target.parent));seen=set();total=0;payload_stage=None;policy=None
        try:
            with tarfile.open(archive_path,'r:gz') as archive:
                first=archive.next()
                if first is None or first.name!='snapshot.json' or not first.isfile() or first.size>32*1024**2:raise DeployError('Invalid snapshot manifest')
                manifest=json.loads(archive.extractfile(first).read());files=manifest.get('files')
                if (manifest.get('format') not in {'ascend-lab-snapshot/v1','ascend-lab-snapshot/v2'} or manifest.get('quiescent') is not True
                    or manifest.get('role')!=meta['role'] or manifest.get('state_schema')!=meta['state_schema']
                    or not isinstance(files,dict) or len(files)>100000 or 'config.json' not in files):raise DeployError('Snapshot role/schema is incompatible')
                if not isinstance(manifest.get('original_state'),str) or not Path(manifest['original_state']).is_absolute():raise DeployError('Invalid original state identity')
                from payload_storage import TREES,inspect_new,validate
                original_payload=manifest.get('payload_storage')
                original_control=manifest.get('control_filesystem')
                if original_control:
                    from control_filesystem import validate as control_contract
                    control_contract(original_control)
                    if meta['role']!='controller' or native is None:raise DeployError('Finite control snapshot requires a new --control-config')
                    if original_control['storage_id']==native['storage_id']:raise DeployError('Restore requires a fresh control storage ID')
                node_spool=manifest.get('node_spool_storage')
                node_cache=manifest.get('persistent_cache')
                if node_cache or cache_config or meta.get('persistent_cache'):
                    if (meta['role']!='node' or not node_cache or not cache_config
                        or manifest.get('persistent_cache_data_included') is not False):
                        raise DeployError('Persistent cache restore requires an explicit same-node --cache-config; cached data is retained on its existing filesystem')
                    from persistent_cache import validate as cache_contract
                    cache_contract(node_cache)
                    if regular_json(cache_config,private=True)!=node_cache or node_cache!=meta.get('persistent_cache'):
                        raise DeployError('Cache restore cannot change node or native policy; enroll another node for a different cache filesystem')
                if node_spool or spool_config:
                    if meta['role']!='node' or not node_spool or not spool_config:
                        raise DeployError('Finite-spool node restore requires its explicit --spool-config')
                    from spool_storage import validate as spool_contract
                    spool_contract(node_spool)
                    target_spool=regular_json(spool_config,private=True)
                    if target_spool!=node_spool or target_spool!=meta.get('spool_storage'):
                        raise DeployError('Node restore requires the same empty verified spool filesystem; enroll another node for a different filesystem or host')
                if manifest['format']=='ascend-lab-snapshot/v2':
                    if not original_payload or meta['role']!='controller' or not payload_config:raise DeployError('Dedicated-payload snapshot requires a new empty filesystem --payload-config')
                    validate(original_payload)
                elif original_payload:raise DeployError('Unexpected payload binding in a legacy snapshot')
                if payload_config:
                    if meta['role']!='controller':raise DeployError('Only controllers have dedicated payload storage')
                    policy=regular_json(payload_config,private=True);inspect_new(policy,target,prefix)
                    if original_payload and policy['storage_id']==original_payload['storage_id']:raise DeployError('Restore requires a fresh payload storage ID')
                    if meta.get('payload_storage',{}).get('device')==policy['device']:raise DeployError('Restore must retain the current payload filesystem and use a new one')
                    payload_stage=Path(tempfile.mkdtemp(prefix='.restore-',dir=policy['path']))
                def destination(name):return (payload_stage if payload_stage is not None and Path(name).parts[0] in TREES else stage)/name
                directories=manifest.get('directories',[])
                if not isinstance(directories,list) or len(directories)>100000 or any(not safe_relative(name) for name in directories):raise DeployError('Unsafe snapshot directory')
                payload_bytes=sum(record.get('size',0) for name,record in files.items() if Path(name).parts and Path(name).parts[0] in TREES)
                if policy:
                    space=os.statvfs(policy['path'])
                    if space.f_bavail*space.f_frsize<payload_bytes+32*1024**2 or space.f_favail<len(files)+len(directories)+64:raise DeployError('Restore payload filesystem lacks bytes or inodes')
                for name in directories:destination(name).mkdir(mode=0o700,parents=True,exist_ok=True)
                for item in iter(archive.next,None):
                    name=item.name.removeprefix('data/')
                    record=files.get(name,{})
                    if not item.name.startswith('data/') or not safe_relative(name) or name in seen or not item.isfile() or record.get('size')!=item.size or item.size<0:raise DeployError('Unsafe or unexpected snapshot member')
                    seen.add(name);total+=item.size
                    if total>max_bytes:raise DeployError('Restore exceeds configured size budget')
                    path=destination(name);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    with archive.extractfile(item) as source,path.open('xb') as out:shutil.copyfileobj(source,out,65536)
                    if digest(path)!=record.get('sha256'):raise DeployError('Snapshot content checksum differs')
                    path.chmod(record.get('mode',0o600)&0o700 or 0o600)
                if seen!=set(files):raise DeployError('Snapshot is incomplete')
            assert_quiescent(stage,meta['role'],check_spool=False)
            for path in stage.glob('*.sqlite3'):
                with sqlite3.connect(path) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise DeployError('Restored database failed integrity check')
            relocate(stage,manifest['original_state'],target,meta['role'],
                     original_payload['path'] if original_payload else manifest['original_state'],policy['path'] if policy else None)
            restored_config=read_json(stage/'config.json')
            if restored_config.get('scheduler',{}).get('control_filesystem')!=original_control:
                raise DeployError('Snapshot configuration and control manifest differ')
            if native:
                from control_storage import DEFAULT
                scheduler=restored_config.setdefault('scheduler',{})
                scheduler['control_filesystem']=native;scheduler['local_enabled']=False
                if scheduler.get('control_storage') is None:scheduler['control_storage']=dict(DEFAULT)
                atomic_json(stage/'config.json',restored_config)
            require_control_storage(restored_config,meta['role'],read_json(prefix/'releases'/meta['release_id']/'release.json'))
            if node_cache:
                if restored_config.get('persistent_cache')!=node_cache:raise DeployError('Snapshot private cache policy differs from its manifest')
                from persistent_cache import Pool
                Pool(old,read_json(old/'config.json')).rebind(target,restored_config,stage)
            if node_spool:
                if restored_config.get('spool_storage')!=node_spool:raise DeployError('Snapshot node spool configuration differs from its manifest')
                from spool_storage import Spool
                Spool(old,read_json(old/'config.json')).rebind(target,restored_config)
            if policy:
                if restored_config.get('scheduler',{}).get('payload_storage')!=original_payload:raise DeployError('Snapshot configuration and payload manifest differ')
                restored_config['scheduler']['payload_storage']=policy;atomic_json(stage/'config.json',restored_config)
                from payload_storage import initialize
                if original_payload and original_payload.get('owner_groups') and not policy.get('owner_groups'):
                    raise DeployError('Grouped payload restore cannot silently discard owner hard quotas')
                initialize(policy,target,meta['uid'],meta['gid'],staged=payload_stage,ownership_state=stage)
                payload_stage=None
            elif restored_config.get('scheduler',{}).get('payload_storage'):raise DeployError('Dedicated storage cannot silently restore into the control directory')
            (stage/'.state.lock').touch(mode=0o600)
            for path in stage.rglob('*'):
                os.chown(path,meta['uid'],meta['gid'])
                if path.is_dir():path.chmod(0o700)
            stage.chmod(0o700);os.chown(stage,meta['uid'],meta['gid'])
            if meta['role']=='controller':
                from audit_retention import rebind_restored
                rebind_restored(stage/'identities.sqlite3',restored_config)
                from storage_lifecycle import rebind_restored
                rebind_restored(stage,payload_base=policy['path'] if policy else None)
                from device_qualification import invalidate_restored
                invalidate_restored(stage/'jobs.sqlite3')
            stage.rename(target)
            if native:
                from control_filesystem import initialize
                initialize(native,target,prefix,meta['uid'],meta['gid'])
                meta['control_filesystem']=native
            meta['previous_state_dir']=str(old);meta['state_dir']=str(target)
            if policy:meta['payload_storage']=policy
            atomic_json(prefix/'installation.json',meta);(prefix/'installation.json').chmod(0o444)
            unit=prefix/'units'/('ascend-lab-'+meta['role']+'.service');unit.chmod(0o644);unit.write_text(unit_text(prefix,meta));unit.chmod(0o444)
            if native:
                from control_process import initialize as process_initialize
                process_initialize(prefix,meta)
            return {'restored_state':str(target),'previous_state_retained':str(old),'role':meta['role'],'started':False,
                    'dedicated_payload_restored':bool(policy),
                    'finite_control_restored':bool(native),
                    'unit_updated':str(unit),'operator_action':'Reload installed systemd unit before restarting, if using systemd'}
        finally:
            if stage.exists():shutil.rmtree(stage)
            if payload_stage is not None and payload_stage.exists():shutil.rmtree(payload_stage)


def require_control_storage(config,role,manifest):
    if role=='controller' and config.get('audit_retention') is not None:
        from audit_retention import policy
        policy(config['audit_retention'])
        if 'audit_retention.py' not in manifest.get('files',{}):
            raise DeployError('Audit archive policy requires matching support; refusing downgrade')
    if role=='controller' and config.get('scheduler',{}).get('control_filesystem') is not None:
        from control_filesystem import validate
        validate(config['scheduler']['control_filesystem'])
        if not {'control_filesystem.py','control_process.py'}<=set(manifest.get('files',{})):
            raise DeployError('Finite control storage requires a matching launcher and filesystem support; refusing downgrade')
    if role=='controller' and config.get('scheduler',{}).get('control_storage') is not None:
        from control_storage import validate
        validate(config['scheduler']['control_storage'])
        if 'control_storage.py' not in manifest.get('files',{}):
            raise DeployError('Control storage admission requires matching support; refusing downgrade')


def require_features(state,role,manifest):
    config=read_json(Path(state)/'config.json') or {}
    require_control_storage(config,role,manifest)
    if role=='controller' and 'qualification_compatibility.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qualification_compatibility'").fetchone() and db.execute('SELECT 1 FROM qualification_compatibility LIMIT 1').fetchone():
                    raise DeployError('Qualification compatibility audit state requires compatible support; refusing downgrade')
    if role=='controller' and 'owner_probe_adoption.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_probe_adoptions'").fetchone() and db.execute('SELECT 1 FROM owner_probe_adoptions LIMIT 1').fetchone():
                    raise DeployError('Probe adoption history requires compatible fencing/admission support; refusing downgrade')
    if role=='controller' and 'owner_device_probe.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_device_probes'").fetchone() and db.execute('SELECT 1 FROM owner_device_probes LIMIT 1').fetchone():
                    raise DeployError('Staged device probe authorizations require compatible admission support; refusing downgrade')
    if role=='controller' and 'owner_provision.py' not in manifest.get('files',{}) and (Path(state)/'owner-provision').exists():
        raise DeployError('Central owner participant state requires compatible onboarding support; refusing downgrade')
    if role=='node' and 'node_owner_provision.py' not in manifest.get('files',{}):
        if (Path(state)/'node-owner-provision-change.json').exists() or (Path(state)/'node-owner-provision-history').exists():
            raise DeployError('Node participant recovery/history requires compatible provisioning support; refusing downgrade')
    if role=='controller' and 'owner_node_grant.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_node_expansions'").fetchone() and db.execute('SELECT 1 FROM owner_node_expansions LIMIT 1').fetchone():
                    raise DeployError('Owner expansion authorizations require compatible admission support; refusing downgrade')
    if role=='controller' and 'owner_activation_abort.py' not in manifest.get('files',{}):
        if any((Path(state)/'owner-activation-aborts').glob('*.json')):
            raise DeployError('Owner activation abort recovery records require compatible abort support; refusing downgrade')
        path=Path(state)/'identities.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_activation_aborts'").fetchone() and db.execute('SELECT 1 FROM owner_activation_aborts LIMIT 1').fetchone():
                    raise DeployError('Spent activation IDs require compatible abort support; refusing downgrade')
    if role=='controller' and 'owner_activation.py' not in manifest.get('files',{}):
        if any((Path(state)/'owner-activations').glob('*.json')):
            raise DeployError('Owner activation recovery records require compatible identity activation support; refusing downgrade')
        path=Path(state)/'identities.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_activations'").fetchone() and db.execute('SELECT 1 FROM owner_activations LIMIT 1').fetchone():
                    raise DeployError('Owner activation identity records require compatible activation support; refusing downgrade')
    if role=='controller' and 'owner_handoff.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_node_handoffs'").fetchone() and db.execute('SELECT 1 FROM owner_node_handoffs LIMIT 1').fetchone():
                    raise DeployError('Owner handoff receipts require compatible admission and ACK support; refusing downgrade')
    if role=='node' and 'node_owner_ack.py' not in manifest.get('files',{}) and (Path(state)/'node-owner-ack-history').exists():
        raise DeployError('Node owner ACK receipts require compatible recovery support; refusing downgrade')
    if role=='controller' and 'owner_identity.py' not in manifest.get('files',{}):
        identities=Path(state)/'identities.sqlite3'
        if identities.exists():
            with sqlite3.connect('file:'+str(identities)+'?mode=ro',uri=True) as db:
                if any('provisioning' in json.loads(row[0]) for row in db.execute('SELECT profile FROM users')):
                    raise DeployError('Staged owner state requires provisioning-aware identity code; refusing downgrade')
    if role=='controller' and 'dependencies.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
                if any(json.loads(row[0]).get('depends_on') for row in db.execute('SELECT data FROM jobs')):
                    raise DeployError('Dependency jobs require matching stage admission support; refusing downgrade')
    if role=='node' and config.get('persistent_cache') and 'persistent_cache.py' not in manifest.get('files',{}):
        raise DeployError('Persistent cache leases require matching cache lifecycle support; refusing downgrade')
    if role=='controller' and 'persistent_cache.py' not in manifest.get('files',{}):
        path=Path(state)/'jobs.sqlite3'
        if path.exists():
            with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_nodes'").fetchone():
                    if any(json.loads(row[0]).get('persistent_cache') for row in db.execute('SELECT profile FROM worker_nodes')):
                        raise DeployError('Registered persistent cache policies require matching controller admission support')
    if role=='node' and config.get('spool_storage') and not {'spool_storage.py','spool_transfer.py'}<=set(manifest.get('files',{})):
        raise DeployError('Finite node spool state requires matching storage and resumable collection support')
    if role=='controller' and config.get('scheduler',{}).get('require_finite_node_spool') and 'spool_storage.py' not in manifest.get('files',{}):
        raise DeployError('This controller requires finite node spool admission; refusing an incompatible downgrade')
    if role=='controller' and config.get('scheduler',{}).get('payload_storage',{}).get('owner_groups'):
        if not {'group_quota.py','payload_groups.py'}<=set(manifest.get('files',{})):
            raise DeployError('This state requires owner group hard quotas; refusing an incompatible downgrade')
    if role=='controller' and config.get('scheduler',{}).get('payload_storage') and 'payload_storage.py' not in manifest.get('files',{}):
        raise DeployError('Dedicated central payload state requires the matching storage implementation')
    if role=='controller' and config.get('scheduler',{}).get('require_node_spool') and 'node_storage.py' not in manifest.get('files',{}):
        raise DeployError('This state requires node-spool-v1; downgrade would bypass declared staging capacity')
    if role=='controller' and config.get('scheduler',{}).get('require_attempt_inventory'):
        if 'attempt_inventory.py' not in manifest.get('files',{}):
            raise DeployError('This state requires attempt-inventory-v1; downgrade would bypass physical-domain reconciliation')


def activate(prefix,archive_path=None,release_id=None):
    require_root();prefix,meta,_=verify(prefix);state=Path(meta['state_dir'])
    from control_process import offline
    with state_lock(state,exclusive=True),offline(prefix,meta):
        assert_quiescent(state,meta['role'])
        if archive_path:
            from deployment import unpack_release
            stage,manifest=unpack_release(archive_path,prefix/'releases');target=prefix/'releases'/manifest['id']
            if manifest['state_schema']!=meta['state_schema']:shutil.rmtree(stage);raise DeployError('No migration is implemented for this state schema')
            if target.exists():shutil.rmtree(stage)
            else:stage.rename(target)
            release_id=manifest['id']
        if not isinstance(release_id,str) or not __import__('re').fullmatch(r'[0-9a-f]{64}',release_id):raise DeployError('Choose an installed release ID')
        target=prefix/'releases'/release_id;manifest=read_json(target/'release.json')
        checked_manifest(manifest)
        if not manifest or manifest.get('state_schema')!=meta['state_schema']:raise DeployError('Release does not support this state schema')
        require_features(state,meta['role'],manifest)
        # Validate the old/new release before allowing the service manager to execute it.
        for name,record in manifest['files'].items():
            path=target/name
            if not safe_relative(name) or path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o022 or digest(path)!=record['sha256']:raise DeployError('Activation release is not intact/root-owned')
        previous=meta['release_id'];link=prefix/'.current-next'
        if link.exists() or link.is_symlink():raise DeployError('A prior activation needs inspection')
        link.symlink_to('releases/'+release_id);os.replace(link,prefix/'current')
        meta['release_id']=release_id;meta['previous_releases']=list(dict.fromkeys([previous,*meta.get('previous_releases',[])]))
        atomic_json(prefix/'installation.json',meta);(prefix/'installation.json').chmod(0o444)
        return {'release_id':release_id,'previous_release_id':previous,'role':meta['role'],'started':False,'state_preserved':True}

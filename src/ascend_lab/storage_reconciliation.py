"""Explicit offline Root rebind of one interrupted legacy storage inventory.

No rename, unlink, credit, task replay or credential operation is performed.
The original approval and full inventory survive; only validated namespace-local
mount IDs become v2. The owner must still use the normal maintenance endpoint.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import deployment as deploy
from deployment import DeployError
import storage_files as fs
import storage_inventory as inventory

FORMAT='ascend-lab-storage-reconciliation/v1'
TABLE='storage_reconciliations'
MAX_BYTES=32*1024**2
MAX_HISTORY=1024


def _checkpoint(stage):pass


def selection(kind,identifier):
    from storage_lifecycle import identifier as check
    check(kind,identifier)
    return kind,identifier


def record(db,target):
    row=db.execute('SELECT owner,state,revision,record FROM storage_lifecycle WHERE kind=? AND id=?',target).fetchone()
    if row is None:raise DeployError('Selected storage operation does not exist')
    value=json.loads(row[3])
    if (value.get('kind'),value.get('id'),value.get('owner'),value.get('state'),value.get('revision'))!=(*target,*row[:3]):
        raise DeployError('Storage row and retained operation disagree')
    return value


def legacy(value):
    from storage_lifecycle import PENDING
    if (not isinstance(value,dict) or not isinstance(value.get('state'),str) or type(value.get('revision')) is not int
        or value['revision']<0 or not isinstance(value.get('owner'),str)
        or not re.fullmatch('[0-9a-f]{64}',str(value.get('request_sha256')))):
        raise DeployError('Invalid retained legacy operation')
    if value['state'] not in PENDING or inventory.version(value.get('inventory'))!=1:
        raise DeployError('Only a pending legacy inventory can be rebound; normal v2 and completed operations are unchanged')
    if {'moving_to_trash':'trash','restoring':'restore','purging':'purge'}[value['state']]!=value.get('operation'):
        raise DeployError('Pending storage state and original operation disagree')
    if value.get('fingerprint')!=fs.digest(value['inventory']):raise DeployError('Retained legacy fingerprint differs')
    result={**copy.deepcopy(value['inventory']),'format':inventory.FORMAT}
    inventory.persistent(result) # Legacy must itself describe a valid single mount tree.
    if value.get('payload_bytes')!=result['bytes']:raise DeployError('Original payload billing and inventory differ')
    return result


def compare(original,current,partial=False):
    """Do not discard file identity/time/mode while authorizing fresh mount IDs."""
    a=inventory.persistent(original);b=inventory.persistent(current)
    if a['root']!=b['root']:raise DeployError('Storage root device/inode changed; no reconciliation was approved')
    if not partial:
        if a!=b:raise DeployError('Storage content or metadata changed beyond mount IDs')
        return
    approved={v['name']:v for v in a['entries']}
    for value in b['entries']:
        before=approved.get(value['name'])
        keys=['name','dev','ino','mode'] if stat.S_ISDIR(value['mode']) else list(value)
        if before is None or any(value[k]!=before[k] for k in keys):
            raise DeployError('Partial purge has new or changed entries; preserve data and billing')


def observe(base,value):
    source=Path(base)/('jobs' if value['kind']=='archive' else 'inputs')/value['id']
    trash=Path(base)/'storage-trash'/value['kind']/value['id']
    present=[(label,path) for label,path in [('source',source),('trash',trash)] if fs.exists(path)]
    if len(present)!=1 or value['operation']=='purge' and present[0][0]!='trash':
        raise DeployError('Exactly one confirmed original/quarantine location is required; nothing was moved')
    label,path=present[0];current=fs.scan(path)
    original=legacy(value);compare(original,current,value['operation']=='purge')
    # Keep the full approved inventory, including entries already removed by a
    # partial purge. Neither original payload_bytes nor accounting is reduced.
    original['root']['mount']=current['root']['mount']
    for item in original['entries']:item['mount']=current['root']['mount']
    return {'location':label,'current':current,'replacement':original}


def require_legacy_selection(state,target,meta):
    """Code-only repair activation, still offline; recover VFS as service UID."""
    from audit_retention import service_identity
    selection(*target)
    path=Path(state)/'jobs.sqlite3';info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=meta['uid'] or info.st_mode&0o077:
        raise DeployError('Unsafe controller database for code-only repair')
    with service_identity(meta),sqlite3.connect(Path(state)/'jobs.sqlite3',timeout=5) as db:
        db.execute('BEGIN IMMEDIATE');legacy(record(db,target));db.rollback()


@contextmanager
def context(prefix,target):
    from audit_retention import Database,service_identity
    from state_lock import state_lock
    from control_process import offline
    from control_monitor import directory
    from maintenance import assert_quiescent
    from payload_storage import Payload
    deploy.require_root();selection(*target);prefix,meta,release=deploy.verify(prefix)
    if meta['role']!='controller':raise DeployError('Storage reconciliation belongs to one stopped controller')
    if deploy.digest(release/Path(__file__).name)!=deploy.digest(Path(__file__)):
        raise DeployError('Activate matching reconciliation code before rebinding storage')
    state=Path(meta['state_dir'])
    if meta.get('control_filesystem'):
        from control_process import namespaces,context as filesystem_context
        namespaces();filesystem_context(meta).check()
    with service_identity(meta):lock=state_lock(state,exclusive=True)
    with lock,offline(prefix,meta):
        with directory(state,meta['uid']) as parent:
            fd=os.open('jobs.sqlite3',os.O_PATH|os.O_NOFOLLOW,dir_fd=parent)
            try:
                info=os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=meta['uid'] or info.st_mode&0o077
                    or info.st_dev!=os.fstat(parent).st_dev or fs.mount(fd)!=fs.mount(parent)):
                    raise DeployError('Unsafe controller database')
            finally:os.close(fd)
        config=deploy.regular_json(state/'config.json',private=True)
        payload=Payload(state,config,uid=meta['uid'])
        with service_identity(meta):connection=sqlite3.connect(state/'jobs.sqlite3',timeout=5)
        db=Database(connection,meta)
        try:
            db.execute('BEGIN IMMEDIATE')
            assert_quiescent(state,'controller',storage_reconcile=target)
            binding={'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],
                'installation_sha256':deploy.digest(prefix/'installation.json'),'configuration_sha256':deploy.digest(state/'config.json'),
                'database_device':info.st_dev,'database_inode':info.st_ino}
            yield db,binding,payload,meta
            with service_identity(meta):connection.commit()
        except BaseException:
            with service_identity(meta):connection.rollback()
            raise
        finally:
            with service_identity(meta):connection.close()


def allocation(db,target,owner):
    row=db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind=? AND id=?',target).fetchone()
    if row is None or row[0]!=owner or type(row[1]) is not int or row[1]<0:raise DeployError('Original storage billing is unavailable')
    return list(row)


def plan(prefix,kind,identifier):
    target=selection(kind,identifier)
    with context(prefix,target) as (db,binding,payload,meta):
        original=record(db,target);observation=observe(payload.base,original)
        value={'format':FORMAT,'binding':binding,'target':{'kind':kind,'id':identifier},'original':original,
            'allocation':allocation(db,target,original['owner']),'observation':observation}
        value['id']=fs.digest(value);check(value)
        return value


def check(value):
    if (not isinstance(value,dict) or set(value)!={'format','binding','target','original','allocation','observation','id'}
        or value['format']!=FORMAT or not isinstance(value['target'],dict) or set(value['target'])!={'kind','id'}
        or not re.fullmatch('[0-9a-f]{64}',str(value.get('id')))
        or value['id']!=fs.digest({k:v for k,v in value.items() if k!='id'}) or len(fs.encode(value))>MAX_BYTES):
        raise DeployError('Invalid or changed Root storage reconciliation plan')
    selection(value['target']['kind'],value['target']['id'])
    old=value['original'];legacy(old)
    if (old['kind'],old['id'])!=(value['target']['kind'],value['target']['id']):raise DeployError('Reconciliation target differs')
    observation=value['observation']
    if not isinstance(observation,dict) or set(observation)!={'location','current','replacement'} or observation['location'] not in {'source','trash'}:
        raise DeployError('Invalid storage observation')
    compare(legacy(old),observation['current'],old['operation']=='purge')
    if inventory.persistent(observation['replacement'])!=inventory.persistent(legacy(old)):
        raise DeployError('Reconciliation cannot alter the original approved inventory')


def rows(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone():return {}
    result={}
    for key,raw in db.execute('SELECT id,record FROM '+TABLE+' LIMIT ?', (MAX_HISTORY+1,)):
        if len(result)>=MAX_HISTORY or len(raw.encode())>65536 or not re.fullmatch('[0-9a-f]{64}',str(key)):
            raise DeployError('Retained reconciliation history exceeds its bounded schema')
        value=json.loads(raw)
        if value.get('format')!=FORMAT or value.get('plan_id')!=key:raise DeployError('Invalid retained reconciliation receipt')
        result[key]=value
    return result


def apply(prefix,value):
    from audit_retention import service_identity
    from payload_quota_admin import write,sync
    from unit_refresh import _directory
    check(value);target=(value['target']['kind'],value['target']['id'])
    with context(prefix,target) as (db,binding,payload,meta):
        if binding!=value['binding']:raise DeployError('Reconciliation plan installation/database/configuration binding is stale')
        old=value['original'];new={**old,'inventory':value['observation']['replacement'],'revision':old['revision']+1,
            'fingerprint':fs.inventory_digest(value['observation']['replacement'])}
        proof={'id':value['id'],'before_record_sha256':fs.digest(old),'after_inventory_sha256':new['fingerprint'],
            'original_request_sha256':old['request_sha256'],'revision':new['revision']}
        new['legacy_reconciliation']=proof
        receipt={'format':FORMAT,'plan_id':value['id'],'binding':binding,'target':value['target'],'proof':proof,
            'allocation_unchanged':value['allocation'],'payload_mutated':False,'quota_released':False}
        retained=rows(db);replayed=value['id'] in retained
        if replayed:
            if retained[value['id']]!=receipt:raise DeployError('Retained reconciliation receipt differs')
        else:
            if len(retained)>=MAX_HISTORY:raise DeployError('Archive reconciliation history before creating more records')
            if record(db,target)!=old or allocation(db,target,old['owner'])!=value['allocation']:
                raise DeployError('Storage operation or billing changed since the Root plan')
            if observe(payload.base,old)!=value['observation']:raise DeployError('Storage observation changed since the Root plan')
        history=Path(prefix)/'storage-reconcile-history';leaf=history/value['id']
        if not history.exists():history.mkdir(mode=0o700);sync(history.parent)
        _directory(history,private=True)
        if not leaf.exists():leaf.mkdir(mode=0o700);sync(history)
        _directory(leaf,private=True)
        before=leaf/'plan.json'
        if before.exists() or before.is_symlink():
            if before.lstat().st_uid!=0 or deploy.regular_json(before,max_bytes=MAX_BYTES,private=True)!=value:
                raise DeployError('Root original reconciliation plan differs')
        else:write(before,value)
        _checkpoint('prepared')
        if not replayed:
            db.execute('CREATE TABLE IF NOT EXISTS '+TABLE+'(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
            db.execute('INSERT INTO '+TABLE+' VALUES(?,?)',(value['id'],json.dumps(receipt,sort_keys=True)))
            db.execute('UPDATE storage_lifecycle SET revision=?,record=? WHERE kind=? AND id=?',(new['revision'],json.dumps(new,sort_keys=True),*target))
            if target[0]=='archive':
                row=db.execute('SELECT owner,data FROM jobs WHERE id=?',(target[1],)).fetchone()
                if row is None or row[0]!=old['owner']:raise DeployError('Archive owner changed')
                job=json.loads(row[1]);job['archive_status']={k:new[k] for k in ['state','revision','purge_after','metadata_bytes'] if k in new}
                db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(job,ensure_ascii=False),target[1]))
            db.execute('INSERT INTO storage_lifecycle_events(at,actor,kind,id,action,state,revision,detail) VALUES(?,?,?,?,?,?,?,?)',
                (time.time(),'root-storage-reconcile',*target,'rebind-legacy-pending',new['state'],new['revision'],json.dumps(proof,sort_keys=True)))
            _checkpoint('updated')
            with service_identity(meta):db.connection.commit()
        _checkpoint('committed')
        result_file=leaf/'receipt.json'
        if result_file.exists() or result_file.is_symlink():
            if result_file.lstat().st_uid!=0 or deploy.regular_json(result_file,private=True)!=receipt:raise DeployError('Root reconciliation receipt differs')
        else:write(result_file,receipt)
        _checkpoint('receipt')
        return {'reconciled':True,'reused':replayed,'plan_id':value['id'],'operation':old['operation'],
            'owner_resume_required':True,'payload_mutated':False,'quota_released':False,'revision':new['revision']}


def require_compatible(state,role,manifest):
    if role!='controller' or Path(__file__).name in manifest.get('files',{}):return
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if rows(db):raise DeployError('Retained storage reconciliation receipts require compatible code; refusing downgrade')


def require_history(current,staged):
    def read(state):
        path=Path(state)/'jobs.sqlite3'
        if not path.exists():return {}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:return rows(db)
    old=read(current)
    if old:
        new=read(staged)
        if any(new.get(key)!=value for key,value in old.items()):raise DeployError('Restore would lose or alter retained storage reconciliation receipts')

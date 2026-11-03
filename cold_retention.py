"""Explicit Root reader activation, bounded original retirement and read rebind.

The copy and all compute/authorization histories survive. Only a verified warm
tree named in the original copy manifest may be removed, with credit committed
after both possible warm locations are confirmed absent.
"""
import copy
import json
import os
from pathlib import Path
import re
import time

import cold_exports as exports
import cold_reads as reads
import storage_files as fs
from deployment import DeployError,digest,regular_json

FORMAT='ascend-lab-cold-retention/v1'
MAX_CHANGES=10000
ADMIN_UID=0


def _checkpoint(stage):pass


def matching(prefix):
    from deployment import verify
    _,_,release=verify(prefix)
    for path in [Path(__file__),Path(reads.__file__)]:
        if not (release/path.name).is_file() or digest(release/path.name)!=digest(path):raise DeployError('Activate matching cold retention/read code before maintenance')


def seal(value):value['id']=fs.digest(value);return value


def check(value):
    if (not isinstance(value,dict) or value.get('format')!=FORMAT or value.get('action') not in {'activate','retire','resume'}
        or not re.fullmatch('[0-9a-f]{64}',str(value.get('id')))
        or not re.fullmatch('[0-9a-f]{64}',str(value.get('source_inventory_sha256')))
        or len(fs.encode(value))>reads.MAX_RECORD or value['id']!=fs.digest({k:v for k,v in value.items() if k!='id'})):
        raise DeployError('Invalid or changed cold retention plan')


def changes(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_reader_changes'").fetchone():return {}
    result={}
    for key,job_id,raw in db.execute('SELECT id,job_id,record FROM cold_reader_changes LIMIT ?',(MAX_CHANGES+1,)):
        if len(result)>=MAX_CHANGES or len(raw.encode())>reads.MAX_RECORD:raise DeployError('Cold reader change history full')
        value=json.loads(raw)
        if value.get('plan',{}).get('id')!=key or value['plan']['job_id']!=job_id or value.get('phase') not in {'pending','applied'}:raise DeployError('Cold change identity differs')
        check(value['plan']);result[key]=value
    return result


def subject(db,job_id):
    from storage_lifecycle import identifier
    from jobs import TERMINAL
    identifier('archive',job_id)
    row=db.execute('SELECT owner,state,data FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row:raise DeployError('Original cold job is unavailable')
    job=json.loads(row[2])
    if (job.get('id'),job.get('owner'),job.get('state'))!=(job_id,row[0],row[1]) or row[1] not in TERMINAL:raise DeployError('Cold retention needs an intact confirmed terminal job')
    life=db.execute('SELECT state FROM storage_lifecycle WHERE kind="archive" AND id=?',(job_id,)).fetchone()
    if life and life[0]!='available':raise DeployError('Original logical archive is unavailable')
    allocation=db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind="archive" AND id=?',(job_id,)).fetchone()
    if allocation is None or allocation[0]!=job['owner'] or allocation[2]!=1:raise DeployError('Original archive allocation is unconfirmed')
    return job,list(allocation)


def copy_value(db,original):
    exports.check_plan(original,'copy')
    value={'copy_id':original['id'],'pool_id':original['pool_id'],'job_id':original['job_id'],'owner':original['owner'],
        'copy_plan_sha256':fs.digest(original)}
    row=exports.rows(db,'cold_transfers').get(original['id'])
    if row is None:raise DeployError('Cold copy history is missing')
    value['target_inventory_sha256']=row[1].get('target_inventory_sha256')
    pool,transfer=reads.selected(db,value)
    snapshot=exports.verify_object(pool,original)
    if fs.inventory_digest(snapshot)!=value['target_inventory_sha256']:raise DeployError('Verified cold object identity changed')
    return value,pool


def references(db,job_id):
    """Freeze retained references; migration preserves their original identities.

    Active/unknown tasks are separately prohibited by the offline context. Every
    listed archive consumer uses archive_directory; no grant is recreated here.
    """
    import hashlib
    tables={'jobs':'data','worker_nodes':'profile','source_grants':'data','job_graphs':'record',
        'device_schedule_plans':'record','device_schedule_runs':'record','device_evidence_policies':'record',
        'device_evidence_checks':'record','performance_references':'record','device_qualifications':'record',
        'owner_device_probes':'record','owner_probe_adoptions':'record','qualification_compatibility':'record'}
    def contains(value,depth=0):
        if depth>64:raise DeployError('Retained reference nesting exceeds its bound')
        if isinstance(value,dict):return job_id in value or any(contains(v,depth+1) for v in value.values())
        if isinstance(value,list):return any(contains(v,depth+1) for v in value)
        return value==job_id
    found=[];total=0
    for table,column in tables.items():
        if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():continue
        for ordinal,(raw,) in enumerate(db.execute('SELECT '+column+' FROM '+table+' ORDER BY rowid LIMIT 10001')):
            total+=len(raw.encode())
            if ordinal>=10000 or len(raw.encode())>32*1024**2 or total>256*1024**2:raise DeployError('Reference audit exceeds bounded metadata retention')
            if contains(json.loads(raw)):found.append((table,ordinal,hashlib.sha256(raw.encode()).hexdigest()))
    return {'sha256':fs.digest(found),'counts':{table:sum(x[0]==table for x in found) for table in tables if any(x[0]==table for x in found)}}


def paths(payload,original):
    return payload.path('jobs')/original['job_id'],payload.path('storage-trash')/'cold'/original['id']


def source_present(payload,original,owner,expected=None,*,verify_bytes=False):
    source,trash=paths(payload,original)
    if not fs.exists(source) or fs.exists(trash):raise DeployError('Original online archive location is unconfirmed')
    payload.object(source,owner)
    from cold_files import verify
    snapshot=verify(source,original['manifest']) if verify_bytes else fs.scan(source)
    if expected is not None and fs.inventory_digest(snapshot)!=expected:raise DeployError('Approved original archive inventory changed')
    return snapshot


def original_hash(original,reader=None):
    return (reader or {}).get('source_inventory_sha256',fs.inventory_digest(original['manifest']['inventory']))


def inventory_file(prefix,value,*,snapshot=None):
    """Root-private full deletion inventory; SQLite holds its approved digest.

    Restoring a retained warm tree creates new inodes. Read revalidation binds
    its freshly byte-verified inventory; retirement persists that full inventory
    before unlinking anything, so partial-purge recovery never guesses entries.
    """
    from cold_files import MAX_MANIFEST
    from payload_quota_admin import sync
    parent=Path(prefix)/'cold-retire-inventories'
    filename=value['id']+'.json'
    if snapshot is not None:parent.mkdir(mode=0o700,exist_ok=True)
    root=fs.directory(prefix)
    try:
        fd=fs.directory(parent)
        try:
            info=os.fstat(fd)
            if (info.st_uid!=ADMIN_UID or info.st_mode&0o777!=0o700 or info.st_dev!=os.fstat(root).st_dev
                or fs.mount(fd)!=fs.mount(root)):raise DeployError('Unsafe private retirement inventory directory')
            names=os.listdir(fd)
            if len(names)>MAX_CHANGES or snapshot is not None and filename not in names and len(names)>=MAX_CHANGES:raise DeployError('Private retirement inventory history is full')
        finally:os.close(fd)
    finally:os.close(root)
    path=parent/filename
    if snapshot is not None:
        from storage_inventory import persistent
        persistent(snapshot)
        if fs.inventory_digest(snapshot)!=value['source_inventory_sha256']:raise DeployError('Original deletion inventory differs from its approved digest')
        body={'format':'ascend-lab-cold-retire-inventory/v1','operation_id':value['id'],'job_id':value['job_id'],
            'inventory_sha256':value['source_inventory_sha256'],'inventory':snapshot}
        exports.publish_json(path,body);path.chmod(0o400)
        with path.open('rb') as source:os.fsync(source.fileno())
        sync(parent)
    info=path.lstat()
    if info.st_uid!=ADMIN_UID or info.st_mode&0o777!=0o400:raise DeployError('Retirement inventory must remain privately sealed to Root')
    body=regular_json(path,max_bytes=MAX_MANIFEST,private=True)
    if (body.get('format')!='ascend-lab-cold-retire-inventory/v1' or body.get('operation_id')!=value['id']
        or body.get('job_id')!=value['job_id'] or body.get('inventory_sha256')!=value['source_inventory_sha256']
        or fs.inventory_digest(body.get('inventory'))!=value['source_inventory_sha256']):raise DeployError('Original private retirement inventory changed')
    return body['inventory']


def plan(prefix,original,action):
    if action not in {'activate','retire','resume'}:raise DeployError('Invalid cold retention action')
    matching(prefix)
    with exports.context(prefix) as (db,binding,payload,meta):
        value,pool=copy_value(db,original);job,allocation=subject(db,original['job_id']);found=reads.record(db,job['id'])
        source_hash=original_hash(original,found[1] if found else None)
        if action=='activate':
            if found is not None:raise DeployError('This job already has a cold read authority; retain its original history')
            expected={k:original['binding'][k] for k in ['state_dir','database_device','database_inode','uid','gid']}
            if expected!={k:binding[k] for k in expected} or exports.rows(db,'cold_pools')[pool['id']][0]!='registered':raise DeployError('Copy source binding changed before first read activation')
            if fs.digest(job)!=original['job_sha256'] or allocation!=original['allocation']:raise DeployError('Original job or accounting changed since the copy')
            source_present(payload,original,job['owner'],source_hash)
        else:
            if found is None or found[1]['copy_id']!=original['id'] or reads.semantic(job)!=found[1]['job_semantic_sha256']:raise DeployError('Retained cold reader no longer matches its original job')
            if action=='resume':
                if found[0]!='restore_frozen' or found[1]['hot_state']=='retiring':raise DeployError('Only a complete restored reader may be revalidated')
                if (meta['uid'],meta['gid'])!=(pool['binding']['uid'],pool['binding']['gid']):raise DeployError('Reader identity remapping needs a separate cold storage migration')
            elif found[0]!='active' or found[1]['hot_state']!='retained':raise DeployError('Online retirement needs its active retained original reader')
            if found[1]['hot_state']=='retained':
                observed=source_present(payload,original,job['owner'],None if action=='resume' else source_hash,verify_bytes=action=='resume')
                source_hash=fs.inventory_digest(observed)
            elif any(fs.exists(path) for path in paths(payload,original)):raise DeployError('Unexpected original archive exists after its retirement')
        return seal({'format':FORMAT,'action':action,'binding':binding,**value,'job_semantic_sha256':reads.semantic(job),
            'allocation':allocation,'previous_reader_sha256':fs.digest(found[1]) if found else None,
            'source_inventory_sha256':source_hash,'references':references(db,job['id'])})


def save_reader(db,value,phase):
    if len(fs.encode(value))>reads.MAX_RECORD:raise DeployError('Cold reader exceeds bounded control metadata')
    db.execute('INSERT INTO cold_readers VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET phase=excluded.phase,record=excluded.record',
        (value['job_id'],phase,json.dumps(value,sort_keys=True)))


def save_change(db,value):
    db.execute('INSERT INTO cold_reader_changes VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
        (value['plan']['id'],value['plan']['job_id'],json.dumps(value,sort_keys=True)))


def response(value,record,reused=False):
    return {'id':value['id'],'job_id':value['job_id'],'action':value['action'],'reused':reused,'read_pointer_changed':value['action']=='activate',
        'reader_enabled':True,'original_state':record['hot_state'],'original_job_retained':True,
        'online_payload_removed':record['hot_state']=='retired','quota_released':record.get('released_bytes',0)>0,
        'released_reserved_bytes':record.get('released_bytes',0),'history_capacity_released':False}


def apply(prefix,value,maximum_entries=256):
    check(value);matching(prefix)
    if type(maximum_entries) is not int or not 1<=maximum_entries<=256:raise DeployError('Cold retirement step removes at most1..256 approved entries')
    with exports.context(prefix) as (db,binding,payload,meta):
        if value['binding']!=binding:raise DeployError('Cold retention plan binding is stale; preserve the original operation')
        all_changes=changes(db);change=all_changes.get(value['id']);found=reads.record(db,value['job_id'])
        if change:
            if change['plan']!=value or found is None:raise DeployError('Cold original retention plan differs')
            if change['phase']=='applied':return response(value,found[1],True)
        elif len(all_changes)>=MAX_CHANGES:raise DeployError('Cold reader change history is full')
        pool,transfer=reads.selected(db,value);path,original=reads.documents(pool,value)
        signature=exports.verify_object(pool,original)
        if fs.inventory_digest(signature)!=value['target_inventory_sha256']:raise DeployError('Cold verified copy metadata changed')
        job,allocation=subject(db,value['job_id'])
        if reads.semantic(job)!=value['job_semantic_sha256'] or allocation!=value['allocation']:raise DeployError('Original cold job/accounting changed after preview')
        if change is None:
            if (fs.digest(found[1]) if found else None)!=value['previous_reader_sha256']:raise DeployError('Original cold reader changed after preview')
            if references(db,job['id'])!=value['references']:raise DeployError('Retained references changed after preview')
            reads.initialize(db)
            if value['action']=='activate':
                if found is not None:raise DeployError('Cold read authority already exists')
                if value['source_inventory_sha256']!=original_hash(original):raise DeployError('Initial source approval differs from the copied original')
                source_present(payload,original,job['owner'],value['source_inventory_sha256'])
                record={k:value[k] for k in ['copy_id','pool_id','job_id','owner','copy_plan_sha256','target_inventory_sha256','job_semantic_sha256']}
                record.update(format=reads.FORMAT,hot_state='retained',activated_by=value['id'],activated_at=time.time(),last_approval=value['id'],source_inventory_sha256=value['source_inventory_sha256'])
            else:
                if found is None:raise DeployError('Cold read authority is missing')
                record=copy.deepcopy(found[1])
                if value['action']=='resume':
                    if found[0]!='restore_frozen' or record['hot_state']=='retiring':raise DeployError('Cold restored reader cannot be revalidated in this state')
                    if (meta['uid'],meta['gid'])!=(pool['binding']['uid'],pool['binding']['gid']):raise DeployError('Cold reader owner remapping is not approved')
                    if record['hot_state']=='retained':
                        observed=source_present(payload,original,job['owner'],verify_bytes=True)
                        if fs.inventory_digest(observed)!=value['source_inventory_sha256']:raise DeployError('Restored original changed after its byte-verified preview')
                        record['source_inventory_sha256']=value['source_inventory_sha256']
                    elif value['source_inventory_sha256']!=original_hash(original,record):raise DeployError('Retired source provenance changed')
                elif found[0]!='active' or record['hot_state']!='retained':raise DeployError('Cold original retirement state changed')
                record['last_approval']=value['id']
            change={'plan':value,'phase':'applied' if value['action']!='retire' else 'pending'}
            if value['action']=='retire':
                if value['source_inventory_sha256']!=original_hash(original,record):raise DeployError('Original retirement source approval changed')
                approved=source_present(payload,original,job['owner'],value['source_inventory_sha256'])
                inventory_file(binding['prefix'],value,snapshot=approved)
                _checkpoint('retire_inventory_synced')
                record.update(hot_state='retiring',retire_operation=value['id'])
            phase='retiring' if value['action']=='retire' else ('retired' if record['hot_state']=='retired' else 'active')
            save_reader(db,record,phase);save_change(db,change);exports.commit(db,meta);_checkpoint('reader_committed' if value['action']!='retire' else 'retire_prepared')
            if value['action']!='retire':return response(value,record)
        else:
            record=found[1]
            if value['action']!='retire' or found[0]!='retiring' or record.get('retire_operation')!=value['id']:raise DeployError('Resume only the original pending cold retirement')
        source,trash=paths(payload,original)
        approved=inventory_file(binding['prefix'],value)
        from audit_retention import service_identity
        with service_identity(meta):fs.private_tree(payload.base,['storage-trash','cold'])
        if fs.exists(source):
            if fs.exists(trash):raise DeployError('Both original retirement locations exist')
            source_present(payload,original,job['owner'],value['source_inventory_sha256']);fs.move(source,trash);_checkpoint('original_renamed')
        complete,removed=fs.purge(trash,approved,budget=maximum_entries)
        _checkpoint('original_purged_step')
        if not complete:return {**response(value,record),'pending':True,'removed_entries':removed}
        if fs.exists(source) or fs.exists(trash):raise DeployError('Original removal is unconfirmed; allocation remains charged')
        _checkpoint('original_removed')
        retained=65536+len(fs.encode(job))+len(fs.encode(record))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_results'").fetchone():
            row=db.execute('SELECT record FROM workflow_results WHERE job_id=?',(job['id'],)).fetchone()
            if row:retained+=len(row[0].encode())
        retained=min(allocation[1],retained)
        record.update(hot_state='retired',retained_control_bytes=retained,released_bytes=allocation[1]-retained,retired_at=time.time())
        db.execute('UPDATE storage_allocations SET bytes=?,settled=1 WHERE kind="archive" AND id=?',(retained,job['id']))
        change['phase']='applied';save_reader(db,record,'retired');save_change(db,change);exports.commit(db,meta);_checkpoint('retirement_credited')
        return {**response(value,record),'pending':False,'removed_entries':removed}


def require_history(current,staged):
    import sqlite3
    def read(state):
        path=Path(state)/'jobs.sqlite3'
        if not path.exists():return {},{}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            readers={}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_readers'").fetchone():
                for key,raw in db.execute('SELECT job_id,record FROM cold_readers LIMIT 10001'):
                    if len(readers)>=MAX_CHANGES or len(raw.encode())>reads.MAX_RECORD:raise DeployError('Cold reader restore history exceeds its bound')
                    readers[key]=json.loads(raw)
            return readers,changes(db)
    old=read(current);new=read(staged)
    if any(new[n].get(key)!=value for n in range(2) for key,value in old[n].items()):raise DeployError('Restore would lose cold read authority, retirement credit or approval history')


def freeze_restored(state):
    import sqlite3
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_readers'").fetchone():db.execute('UPDATE cold_readers SET phase="restore_frozen"')


def require_compatible(state,role,manifest):
    if role!='controller' or {'cold_reads.py','cold_retention.py'}<=set(manifest.get('files',{})):return
    import sqlite3
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_readers'").fetchone() and db.execute('SELECT 1 FROM cold_readers LIMIT 1').fetchone():raise DeployError('Retained cold read authority requires compatible code; refusing downgrade')


def require_stable_code(state):
    """Even a peer's code-only repair must not invalidate a pending cold plan."""
    import sqlite3
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        for table,column,pending in [('cold_pools','state','registering'),('cold_transfers','state','copying'),('cold_readers','phase','retiring')]:
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone() and db.execute('SELECT 1 FROM '+table+' WHERE '+column+'=? LIMIT 1',(pending,)).fetchone():
                raise DeployError('Finish original cold maintenance before code activation; independent metadata-only recovery remains available')

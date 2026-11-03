"""Finite, administrator-private control snapshots before historical rotation.

These are evidence copies, never live authorization, job read authority or quota
credit. Original SQL/jobs/attempts/results stay in place. Snapshot source bytes
are frozen transactionally before bounded file writes, then discarded from the
staging column only after the sealed copy has been completely verified.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import cold_exports as offline
import storage_files as fs
from deployment import DeployError,digest,verify
from payload_quota_admin import sync

FORMAT='ascend-lab-control-history-export/v1'
DIRECTORY='control-history'
MAX_RAW=32*1024**2
MAX_OPERATIONS=128
CHUNK=256*1024
ADMIN_UID=0
TABLES=('jobs','execution_attempts','workflow_results','storage_allocations','history_capacity_slots',
        'cold_readers','cold_reader_changes','cold_transfers','cold_pools','source_grants',
        'experiment_budgets','experiment_charges','job_graphs','worker_nodes','device_schedule_plans',
        'device_schedule_runs','device_evidence_policies','device_evidence_checks','performance_references',
        'device_qualifications','owner_device_probes','owner_probe_adoptions','qualification_compatibility')


def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def exists(db,name):return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(name,)).fetchone())
def _checkpoint(stage):pass


def budget(value):
    if (not isinstance(value,dict) or set(value)!={'bytes','exports'} or type(value['bytes']) is not int
        or not 1024**2<=value['bytes']<=1024**3 or type(value['exports']) is not int or not 1<=value['exports']<=MAX_OPERATIONS):
        raise DeployError('Control history needs a finite1MiB..1GiB budget and1..128 exports')
    return value


def selection(values):
    from storage_lifecycle import identifier
    if not isinstance(values,list) or not 1<=len(values)<=32 or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values):raise DeployError('Select1..32 distinct original jobs')
    for value in values:identifier('archive',value)
    return sorted(values)


def source(db,identifiers):
    """Selected original rows plus directly related control evidence, not roles.

    Current identities/credentials remain in their own authoritative database.
    Reference copies preserve historical context and must never reactivate grants.
    """
    from jobs import TERMINAL
    from history_capacity import decode
    from cold_reads import semantic
    identifiers=selection(identifiers);keys=set(identifiers);descriptors=[]
    for ident in identifiers:
        row=db.execute('SELECT id,owner,state,data,idem_key,fingerprint FROM jobs WHERE id=?',(ident,)).fetchone()
        if row is None:raise DeployError('Selected original job is unavailable')
        job=decode(row[:4])
        if '_control_history_ref' in job:raise DeployError('Use the original migrated snapshot; chained history copies are not supported')
        if job['state'] not in TERMINAL:raise DeployError('Only confirmed terminal control history can be copied')
        descriptors.append({'id':ident,'owner':job['owner'],'state':job['state'],'fingerprint':row[5],'semantic_sha256':semantic(job)})
        if job.get('experiment_id'):keys.add(job['experiment_id'])
    def related(value,depth=0):
        if depth>64:raise DeployError('Control reference nesting exceeds64 levels')
        if isinstance(value,dict):return any(k in keys or related(v,depth+1) for k,v in value.items())
        if isinstance(value,(list,tuple)):return any(related(v,depth+1) for v in value)
        if isinstance(value,str):
            if value in keys:return True
            if value.startswith(('{','[')):
                try:parsed=json.loads(value)
                except json.JSONDecodeError:pass
                else:return related(parsed,depth+1)
        return False
    tables={};scanned=0;retained=0
    for name in TABLES:
        if not exists(db,name):continue
        columns=[row[1] for row in db.execute('PRAGMA table_info('+name+')')]
        if not columns or len(columns)>64 or any(not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]*',c) for c in columns):raise DeployError('Unsupported control table schema')
        rows=[]
        for index,row in enumerate(db.execute('SELECT * FROM '+name+' ORDER BY rowid LIMIT 10001')):
            raw=encoded(row);scanned+=len(raw)
            if index>=10000 or len(raw)>MAX_RAW or scanned>256*1024**2:raise DeployError('Control source exceeds bounded metadata scan')
            if related(row):
                rows.append(list(row));retained+=len(raw)
                if name=='cold_transfers' and 'pool_id' in columns:keys.add(row[columns.index('pool_id')])
                if retained>MAX_RAW:raise DeployError('Selected control history exceeds32MiB')
        if rows:tables[name]={'columns':columns,'rows':rows}
    if 'jobs' not in tables:raise DeployError('Original job history was not captured')
    body={'format':FORMAT,'selection':identifiers,'scope':'historical-control-copy-not-authorization','tables':tables}
    raw=encoded(body)
    if len(raw)>MAX_RAW:raise DeployError('Control export exceeds32MiB')
    return raw,descriptors


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS control_history_policy(id INTEGER PRIMARY KEY CHECK(id=1),record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS control_history_exports(id TEXT PRIMARY KEY,state TEXT NOT NULL,record TEXT NOT NULL,source BLOB)')


def rows(db):
    if not exists(db,'control_history_exports'):return {}
    result={}
    for ident,state,raw in db.execute('SELECT id,state,record FROM control_history_exports LIMIT 129'):
        if len(result)>=MAX_OPERATIONS or len(raw.encode())>65536 or not re.fullmatch('[0-9a-f]{64}',str(ident)):
            raise DeployError('Invalid or full control history export ledger')
        record=json.loads(raw);check(record['plan'])
        if record['plan']['id']!=ident or state not in {'copying','verified','restore_frozen'}:raise DeployError('Control export identity or state differs')
        result[ident]=(state,record)
    return result


def policy(db):
    if not exists(db,'control_history_policy'):return None
    value=db.execute('SELECT record FROM control_history_policy WHERE id=1').fetchone()
    return budget(json.loads(value[0])) if value else None


def check(value):
    if (not isinstance(value,dict) or set(value)!={'format','binding','budget','selection','source','id'}
        or value.get('format')!=FORMAT or len(encoded(value))>65536
        or not re.fullmatch('[0-9a-f]{64}',str(value.get('id')))
        or value['id']!=sha(encoded({k:v for k,v in value.items() if k!='id'}))):raise DeployError('Invalid or changed control history plan')
    budget(value['budget']);selection(value['selection'])
    s=value['source']
    if (not isinstance(s,dict) or set(s)!={'bytes','sha256','jobs'} or type(s['bytes']) is not int
        or not 1<=s['bytes']<=MAX_RAW or not re.fullmatch('[0-9a-f]{64}',str(s['sha256']))):raise DeployError('Invalid original control snapshot description')


def matching(prefix):
    _,_,release=verify(prefix)
    if not (release/'control_history.py').is_file() or digest(release/'control_history.py')!=digest(Path(__file__)):
        raise DeployError('Activate matching control history export code before maintenance')


@contextmanager
def context(prefix):
    matching(prefix)
    with offline.context(prefix,metadata_export=True) as (db,binding,payload,meta):
        from maintenance import assert_quiescent
        from cold_retention import require_stable_code
        assert_quiescent(Path(meta['state_dir']),'controller',metadata_export=True)
        require_stable_code(Path(meta['state_dir']))
        yield db,binding,meta


def directory(prefix,gid,expected=None,create=False):
    path=Path(prefix)/DIRECTORY
    if create and not fs.exists(path):
        path.mkdir(mode=0o750);os.chown(path,ADMIN_UID,gid);path.chmod(0o750)
        sync(Path(prefix))
    fd=fs.directory(path)
    try:
        info=os.fstat(fd);identity=[info.st_dev,info.st_ino]
        if (info.st_uid!=ADMIN_UID or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=0o750
            or expected is not None and identity!=expected):raise DeployError('Control history directory identity or permissions changed')
        parent=fs.directory(prefix)
        try:
            p=os.fstat(parent)
            if p.st_uid!=ADMIN_UID or p.st_mode&0o022 or p.st_dev!=info.st_dev or fs.mount(parent)!=fs.mount(fd):
                raise DeployError('Control history must retain its protected installation filesystem')
        finally:os.close(parent)
        return path,identity
    finally:os.close(fd)


def inventory(path,records):
    allowed={ident+suffix for ident in records for suffix in ['.json','.partial']}
    if set(os.listdir(path))-allowed:raise DeployError('Untracked control export files require original-operation reconciliation')
    for ident,(state,r) in records.items():
        final=path/(ident+'.json');partial=path/(ident+'.partial')
        if fs.exists(final) and fs.exists(partial):raise DeployError('Both control export locations exist')
        if state in {'verified','restore_frozen'} and (not fs.exists(final) or fs.exists(partial)):raise DeployError('Confirmed control export is missing')


def accounting(records,limit,additional=0):
    used=sum(r['plan']['source']['bytes']+4096 for _,r in records.values())
    if len(records)+(1 if additional else 0)>limit['exports'] or used+additional>limit['bytes']:
        raise DeployError('Finite control history export budget is full; originals were retained')


def plan(prefix,identifiers,limit):
    limit=budget(limit)
    with context(prefix) as (db,binding,meta):
        records=rows(db)
        if any(state=='copying' for state,_ in records.values()):raise DeployError('Resume the original pending control history export first')
        saved=policy(db)
        if saved is not None and saved!=limit:raise DeployError('Existing control history budget is immutable; use its original limits')
        if fs.exists(Path(prefix)/DIRECTORY):
            path,_=directory(prefix,meta['gid']);inventory(path,records)
        raw,jobs=source(db,identifiers)
        value={'format':FORMAT,'binding':binding,'budget':limit,'selection':selection(identifiers),
            'source':{'bytes':len(raw),'sha256':sha(raw),'jobs':jobs}}
        value['id']=sha(encoded(value))
        if value['id'] not in records:accounting(records,limit,len(raw)+4096)
        return value


def save(db,record,state,blob=None):
    db.execute('INSERT INTO control_history_exports VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,record=excluded.record,source=excluded.source',
        (record['plan']['id'],state,encoded(record).decode(),blob))


def checked(path,record):
    value=record['plan'];check(value)
    base,identity=directory(path,value['binding']['gid'],record['directory'])
    filename=base/(value['id']+'.json');parent=fs.directory(base)
    try:
        fd=os.open(filename.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=ADMIN_UID
                or info.st_gid!=value['binding']['gid'] or stat.S_IMODE(info.st_mode)!=0o440
                or info.st_size!=value['source']['bytes'] or info.st_dev!=os.fstat(parent).st_dev or fs.mount(fd)!=fs.mount(parent)):
                raise DeployError('Control export file is not the original sealed object')
            if record.get('file_identity') is not None and [info.st_dev,info.st_ino]!=record['file_identity']:
                raise DeployError('Confirmed control export file identity changed')
            with os.fdopen(os.dup(fd),'rb') as source_file:raw=source_file.read(MAX_RAW+1)
            after=os.fstat(fd)
            if ((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                or sha(raw)!=value['source']['sha256']):raise DeployError('Control history export checksum or read identity differs')
            body=json.loads(raw)
            if body.get('format')!=FORMAT or body.get('selection')!=value['selection'] or encoded(body)!=raw:
                raise DeployError('Control history snapshot schema or canonical bytes differ')
            return body,{'bytes':len(raw),'sha256':sha(raw),'identity':[info.st_dev,info.st_ino],
                'readonly_mount':bool(os.fstatvfs(fd).f_flag&os.ST_RDONLY)}
        finally:os.close(fd)
    finally:os.close(parent)


def response(record,state,reused=False,verified=False):
    value=record['plan']
    return {'id':value['id'],'state':state,'reused':reused,'bytes':value['source']['bytes'],
        'copied_bytes':record['copied_bytes'],'content_verified':verified,'original_jobs_retained':True,
        'normal_read_authority':False,'control_capacity_released':False,'online_capacity_released':False,
        'authorization_restored':False,'source_is_historical_snapshot':True}


def step(prefix,value,maximum_bytes=4*1024**2):
    check(value)
    if type(maximum_bytes) is not int or not 1<=maximum_bytes<=4*1024**2:raise DeployError('One control export step writes1..4MiB')
    with context(prefix) as (db,binding,meta):
        if value['binding']!=binding:raise DeployError('Control history write plan binding is stale; retain the original snapshot')
        records=rows(db);found=records.get(value['id'])
        if found and found[1]['plan']!=value:raise DeployError('Original control history plan differs')
        if found and found[0] in {'verified','restore_frozen'}:
            checked(prefix,found[1]);return response(found[1],found[0],True,True)
        if found is None:
            if any(state=='copying' for state,_ in records.values()):raise DeployError('Finish the original pending control history export')
            saved=policy(db)
            if saved is not None and saved!=value['budget']:raise DeployError('Control history export policy changed')
            raw,jobs=source(db,value['selection'])
            if {'bytes':len(raw),'sha256':sha(raw),'jobs':jobs}!=value['source']:raise DeployError('Original control history changed after preview')
            accounting(records,value['budget'],len(raw)+4096)
            # SQLite may need both staging pages and rollback journal space.
            space=os.statvfs(meta['state_dir'])
            if space.f_bavail*space.f_frsize<2*len(raw)+64*1024**2:raise DeployError('Control filesystem lacks safe metadata staging space')
            target_space=os.statvfs(prefix)
            if target_space.f_bavail*target_space.f_frsize<len(raw)+16*1024**2 or target_space.f_favail<4:
                raise DeployError('Installation archive filesystem lacks reserved bytes or inodes')
            path,identity=directory(prefix,meta['gid'],create=True);inventory(path,records)
            initialize(db)
            if saved is None:db.execute('INSERT INTO control_history_policy VALUES(1,?)',(encoded(value['budget']).decode(),))
            record={'plan':value,'directory':identity,'copied_bytes':0,'created_at':time.time()}
            save(db,record,'copying',raw);offline.commit(db,meta);_checkpoint('reserved')
        else:
            record=found[1];path,_=directory(prefix,meta['gid'],record['directory'])
            raw=db.execute('SELECT source FROM control_history_exports WHERE id=?',(value['id'],)).fetchone()[0]
            if not isinstance(raw,bytes) or len(raw)!=value['source']['bytes'] or sha(raw)!=value['source']['sha256']:
                raise DeployError('Frozen original control history source is unavailable')
        records=rows(db);inventory(path,records)
        partial=path/(value['id']+'.partial');target=path/(value['id']+'.json')
        if not fs.exists(target):
            if not fs.exists(partial):
                if record['copied_bytes']:raise DeployError('Confirmed control export prefix disappeared')
                fd=os.open(partial,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
                os.fchown(fd,ADMIN_UID,meta['gid']);os.fsync(fd);os.close(fd);sync(path);_checkpoint('file_created')
            parent=fs.directory(path)
            mode=os.O_RDONLY if partial.lstat().st_mode&0o777==0o440 else os.O_RDWR
            fd=os.open(partial.name,mode|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:
                info=os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=ADMIN_UID or info.st_gid!=meta['gid']
                    or stat.S_IMODE(info.st_mode) not in {0o600,0o440} or fs.mount(fd)!=fs.mount(parent)):
                    raise DeployError('Unsafe partial control export')
                cursor=record['copied_bytes']
                for offset in range(0,cursor,CHUNK):
                    count=min(CHUNK,cursor-offset)
                    if os.pread(fd,count,offset)!=raw[offset:offset+count]:raise DeployError('Confirmed control export prefix changed; no tail was removed')
                if info.st_size<cursor:raise DeployError('Confirmed control export prefix is incomplete')
                if info.st_size!=cursor:os.ftruncate(fd,cursor);os.fsync(fd)
                spent=0
                while cursor<len(raw) and spent<maximum_bytes:
                    block=raw[cursor:cursor+min(CHUNK,maximum_bytes-spent)];written=0
                    while written<len(block):
                        size=os.pwrite(fd,block[written:],cursor+written)
                        if size<=0:raise DeployError('Control export write made no progress')
                        written+=size
                    os.fsync(fd);_checkpoint('data_synced');cursor+=len(block);spent+=len(block);record['copied_bytes']=cursor
                    db.execute('UPDATE control_history_exports SET record=? WHERE id=?',(encoded(record).decode(),value['id']))
                    offline.commit(db,meta);_checkpoint('progress_committed')
                if cursor<len(raw):return response(record,'copying')
                os.fchmod(fd,0o440);os.fsync(fd);sync(path);_checkpoint('sealed')
            finally:os.close(fd);os.close(parent)
            fs.move(partial,target);sync(path);_checkpoint('published')
        body,proof=checked(prefix,record)
        record.update(verified_at=time.time(),file_sha256=proof['sha256'],file_identity=proof['identity'])
        save(db,record,'verified',None);offline.commit(db,meta);_checkpoint('verified')
        return response(record,'verified',verified=True)


def verify_copy(prefix,value):
    check(value)
    with context(prefix) as (db,binding,meta):
        found=rows(db).get(value['id'])
        if found is None or found[0] not in {'verified','restore_frozen'} or found[1]['plan']!=value:raise DeployError('Control history copy is not confirmed')
        checked(prefix,found[1]);return response(found[1],found[0],True,True)


def status(jobs,principal,identifier):
    from jobs import JobError
    if not principal.admin or not re.fullmatch('[0-9a-f]{64}',identifier):raise JobError('Control export not found',404)
    with jobs.connect() as db:
        found=rows(db).get(identifier)
        migrated=db.execute('SELECT count(*) FROM control_rotation_jobs WHERE export_id=?',(identifier,)).fetchone()[0] if exists(db,'control_rotation_jobs') else 0
    if found is None:raise JobError('Control export not found',404)
    state,record=found;verified=False;proof={}
    if state in {'verified','restore_frozen'}:
        try:body,proof=checked(record['plan']['binding']['prefix'],record);verified=True
        except (DeployError,OSError,ValueError):raise JobError('Historical control snapshot is unavailable',409) from None
    return {**response(record,state,verified=verified),'readonly_mount':proof.get('readonly_mount'),
        'selected_job_count':len(record['plan']['selection']),'normal_read_authority':migrated>0,
        'control_capacity_released':migrated>0,'migrated_job_count':migrated}


def require_stable_code(state):
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if any(s=='copying' for s,_ in rows(db).values()):raise DeployError('Finish the original control history export before backup or code activation')


def require_compatible(state,role,manifest):
    if role!='controller' or 'control_history.py' in manifest.get('files',{}):return
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if rows(db):raise DeployError('Retained private control history exports require compatible code; refusing downgrade')


def require_history(current,staged):
    def read(state):
        path=Path(state)/'jobs.sqlite3'
        if not path.exists():return {},None
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:return {k:v for k,(s,v) in rows(db).items()},policy(db)
    old,old_policy=read(current)
    if not old:return
    new,new_policy=read(staged)
    if old_policy!=new_policy or any(new.get(k)!=value for k,value in old.items()):raise DeployError('Restore would lose original control history export proofs or budget')


def freeze_restored(state):
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path) as db:
            for ident,(phase,r) in rows(db).items():
                if phase not in {'verified','restore_frozen'}:raise DeployError('Pending control history export cannot be restored')
                save(db,r,'restore_frozen',None)

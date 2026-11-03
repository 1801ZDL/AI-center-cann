"""Explicit terminal job-body eviction into verified private control copies.

The compact jobs row retains identity/ACL/order and the original unique key.
Attempts, results, grants, charges and graph records remain live SQL facts.
This is a finite second tier, not deletion of task history or endless retention.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

import control_history as copies
import history_capacity as capacity
from deployment import DeployError,verify,digest

FORMAT='ascend-lab-control-rotation/v1'
REF='_control_history_ref'
MAX_INDEX=4096
FIELDS={'id','owner','state','project','experiment_id','reserved_at','archive_dir','archive_status'}
READ_CONCURRENCY=2
READ_WAIT_SECONDS=2
_read_slots=threading.BoundedSemaphore(READ_CONCURRENCY)
_read_local=threading.local()


@contextmanager
def read_admission(needed=True):
    """Bound whole private-copy read operations within this controller process.

    Nested readers share the current thread's permit. No database permission or
    compute allocation is issued, and a busy read never resubmits a job.
    """
    if not needed or getattr(_read_local,'active',False):
        yield;return
    if not _read_slots.acquire(timeout=READ_WAIT_SECONDS):
        from jobs import JobError
        error=JobError('Private control history readers are busy; retry the same read request',503)
        error.retry_after_seconds=1
        raise error
    _read_local.active=True
    try:yield
    finally:
        _read_local.active=False
        _read_slots.release()


def fail(message):return capacity.error(message)
def encode(value):return capacity.encode(value)
def sha(value):return capacity.sha(value)
def exists(db):return capacity.table(db,'control_rotation_jobs')


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS control_rotation_jobs(job_id TEXT PRIMARY KEY,export_id TEXT NOT NULL,rotation_id TEXT NOT NULL,record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS control_rotation_changes(id TEXT PRIMARY KEY,record TEXT NOT NULL)')


def compact(job,reference):
    result={k:v for k,v in job.items() if k in FIELDS};result[REF]=reference
    if len(encode(result))>16384:raise fail('Compact history index exceeds16KiB; original SQL was retained')
    return result


def original(body,ident):
    table=body.get('tables',{}).get('jobs',{})
    if table.get('columns')!=['id','owner','idem_key','fingerprint','state','data']:raise fail('Unsupported original job snapshot schema')
    rows=[r for r in table.get('rows',[]) if r[0]==ident]
    if len(rows)!=1 or len(rows[0])!=6:raise fail('Original job is missing or duplicated in its private snapshot')
    row=rows[0]
    # Do not chain archived snapshots or hydrate a nested reference.
    job=json.loads(row[5])
    if REF in job or (job.get('id'),job.get('owner'),job.get('state'))!=(row[0],row[1],row[4]):raise fail('Original job snapshot identity differs')
    from jobs import TERMINAL
    if job['state'] not in TERMINAL:raise fail('Archived control history must be terminal')
    return row,job


def row_record(db,identifier):
    if not exists(db):return None
    row=db.execute('SELECT export_id,rotation_id,record FROM control_rotation_jobs WHERE job_id=?',(identifier,)).fetchone()
    if row is None:return None
    if len(row[2].encode())>4096:raise fail('Compact history receipt exceeds its finite bound')
    value=json.loads(row[2])
    if (not isinstance(value,dict) or set(value)!={'job_id','export_id','rotation_id','row_sha256'}
        or value['job_id']!=identifier or (value['export_id'],value['rotation_id'])!=tuple(row[:2])
        or any(not re.fullmatch('[0-9a-f]{64}',str(value[k])) for k in ['export_id','rotation_id','row_sha256'])):
        raise fail('Invalid compact control history index')
    return value


def read(db,raw,*,cache=None):
    with read_admission(REF in json.loads(raw)):
        return _read(db,raw,cache=cache)


def _read(db,raw,*,cache=None):
    """Hydrate only after the caller's current ACL check/filter where possible.

    cache is local to one bounded read, never shared between requests. Each file
    is fully verified once for that operation; future requests check bytes again.
    """
    job=json.loads(raw)
    record=row_record(db,job.get('id'))
    if REF not in job:
        if record is not None:raise fail('A migrated job lost its original compact reference')
        return job
    if record is None or job[REF]!={'format':FORMAT,**record}:raise fail('Compact job and original migration receipt differ')
    row=db.execute('SELECT owner,idem_key,fingerprint,state FROM jobs WHERE id=?',(job['id'],)).fetchone()
    found=copies.rows(db).get(record['export_id'])
    if found is None or found[0] not in {'verified','restore_frozen'}:raise fail('Original private control snapshot is not verified')
    change=db.execute('SELECT record FROM control_rotation_changes WHERE id=?',(record['rotation_id'],)).fetchone()
    if change is None:raise fail('Original control migration approval is missing')
    if len(change[0].encode())>131072:raise fail('Control migration approval exceeds its finite bound')
    approval=json.loads(change[0]);check(approval['plan'])
    if approval['plan']['id']!=record['rotation_id'] or {k:v for k,v in record.items() if k!='rotation_id'} not in approval['plan']['indexes']:raise fail('Job was not in the original migration approval')
    key=record['export_id'];cache={} if cache is None else cache
    if key not in cache:
        cache.clear()  # Bound parsed private snapshots to one file per operation.
        try:cache[key]=copies.checked(found[1]['plan']['binding']['prefix'],found[1])[0]
        except (DeployError,OSError,ValueError):raise fail('Original migrated control snapshot is unavailable; no replacement task was created') from None
    source,restored=original(cache[key],job['id'])
    if sha(source)!=record['row_sha256'] or tuple(source[1:5])!=tuple(row):raise fail('Original migrated job identity or idempotency differs')
    expected=compact(restored,{'format':FORMAT,**record})
    # State restore is allowed to relocate the archive path and rebind lifecycle
    # metadata. These two live overlays never alter immutable execution facts.
    for field in ['archive_dir','archive_status']:
        if field in job:expected[field]=job[field];restored[field]=job[field]
        else:expected.pop(field,None);restored.pop(field,None)
    if job!=expected:raise fail('Compact ACL/order/state differs from the original migrated job')
    return restored


def read_many(db,raws):
    """Group a caller-filtered finite batch by copy, restoring input order.

    Interleaved SQL rows must not repeatedly evict/reparse the same copy. Keep
    only one parsed private snapshot at a time, with no cross-request cache.
    The grouping key is merely a hint: read() still validates every original
    SQL row, approval, reference, source checksum and retained ACL projection.
    Callers must apply current authorization before supplying private rows.
    """
    groups={};count=0
    for index,raw in enumerate(raws):
        value=json.loads(raw);reference=value.get(REF)
        key=reference.get('export_id') if isinstance(reference,dict) else None
        if not isinstance(key,str):key=None
        groups.setdefault(key,[]).append((index,raw));count+=1
    result=[None]*count
    with read_admission(any(key is not None for key in groups)):
        cache={}
        try:
            for members in groups.values():
                for index,raw in members:result[index]=read(db,raw,cache=cache)
        finally:cache.clear()  # Release parsed copies before returning the permit.
    return result


def all_jobs(db):
    with read_admission(exists(db) and count(db)>0):
        cache={}
        try:
            for raw, in db.execute('SELECT data FROM jobs ORDER BY rowid'):
                yield read(db,raw,cache=cache)
        finally:cache.clear()


def guard_write(db,identifier):
    if row_record(db,identifier) is not None:raise fail('Migrated terminal control history is immutable; retain its original receipt')


def count(db):
    if not exists(db):return 0
    value=db.execute('SELECT count(*) FROM control_rotation_jobs').fetchone()[0]
    if value>MAX_INDEX:raise fail('Finite compact history index is full')
    return value


def validate(db):
    # No new SQL JSON dependency for legacy deployments without this feature.
    if not exists(db):return
    n=count(db)
    if not n:
        if db.execute("SELECT 1 FROM jobs WHERE json_type(data,'$."+REF+"') IS NOT NULL LIMIT 1").fetchone():raise fail('Compact history lacks its migration ledger')
        return
    if capacity.current(db) is None:raise fail('Migrated history requires explicit separate capacity limits')
    if db.execute('SELECT 1 FROM control_rotation_jobs c LEFT JOIN jobs j ON j.id=c.job_id WHERE j.id IS NULL LIMIT 1').fetchone():raise fail('Migrated task identity was removed')
    with read_admission():
        cache={}
        try:
            for raw, in db.execute('SELECT j.data FROM jobs j JOIN control_rotation_jobs c ON c.job_id=j.id'):
                read(db,raw,cache=cache)
        finally:cache.clear()


def matching(prefix):
    _,_,release=verify(prefix)
    if not (release/'control_rotation.py').is_file() or digest(release/'control_rotation.py')!=digest(Path(__file__)):
        raise DeployError('Activate matching control rotation code before maintenance')


@contextmanager
def context(prefix):
    matching(prefix)
    with copies.context(prefix) as (db,binding,meta):
        copies.require_stable_code(Path(meta['state_dir']))
        yield db,binding,meta


def check(value):
    if (not isinstance(value,dict) or set(value)!={'format','binding','export_id','before','indexes','id'}
        or value.get('format')!=FORMAT or not re.fullmatch('[0-9a-f]{64}',str(value.get('id')))
        or value['id']!=sha({k:v for k,v in value.items() if k!='id'}) or len(encode(value))>65536
        or not isinstance(value['indexes'],list) or not 1<=len(value['indexes'])<=32):raise fail('Invalid original control rotation plan')
    indexes=value['indexes']
    if (not re.fullmatch('[0-9a-f]{64}',str(value['export_id']))
        or any(not isinstance(v,dict) or set(v)!={'job_id','export_id','row_sha256'} or v['export_id']!=value['export_id']
               or not re.fullmatch('job-[0-9a-f]{12}',str(v['job_id'])) or not re.fullmatch('[0-9a-f]{64}',str(v['row_sha256'])) for v in indexes)
        or [v['job_id'] for v in indexes]!=sorted({v['job_id'] for v in indexes})):
        raise fail('Invalid original control rotation selection')



def selection(db,prefix,export_id):
    if capacity.current(db) is None:raise fail('Enable explicit separate capacity limits before control rotation')
    if not re.fullmatch('[0-9a-f]{64}',str(export_id)):raise fail('Invalid control snapshot identifier')
    found=copies.rows(db).get(export_id)
    if found is None or found[0]!='verified':raise fail('Use an original verified current control snapshot; restored copies remain read-only')
    body,_=copies.checked(prefix,found[1]);indexes=[]
    for identifier in found[1]['plan']['selection']:
        source,job=original(body,identifier)
        current=db.execute('SELECT id,owner,idem_key,fingerprint,state,data FROM jobs WHERE id=?',(identifier,)).fetchone()
        if current is None or list(current)!=source:raise fail('Original job changed since the verified snapshot; retain SQL and prepare a current copy')
        if row_record(db,identifier) is not None:raise fail('Job already has an original control migration')
        proof=capacity.online_proof(db,job)
        slot=db.execute('SELECT online,proof FROM history_capacity_slots WHERE job_id=?',(identifier,)).fetchone()
        if proof['kind']=='online' or slot!=(0,encode(proof).decode()):raise fail('Confirm original archive removal and online slot release before control rotation')
        indexes.append({'job_id':identifier,'export_id':export_id,'row_sha256':sha(source)})
    if count(db)+len(indexes)>MAX_INDEX:raise fail('Finite compact history index is full; preserve original SQL')
    return indexes


def plan(prefix,export_id):
    with context(prefix) as (db,binding,meta):
        indexes=selection(db,prefix,export_id)
        before,_=capacity.snapshot(db)
        value={'format':FORMAT,'binding':binding,'export_id':export_id,'before':before,'indexes':indexes}
        # Receipt indexes include operation identity without a circular plan hash.
        value['id']=sha(value)
        return value


def _checkpoint(stage):pass


def apply(prefix,value):
    check(value)
    with context(prefix) as (db,binding,meta):
        if value['binding']!=binding:raise fail('Original control rotation binding is stale')
        found=db.execute('SELECT record FROM control_rotation_changes WHERE id=?',(value['id'],)).fetchone() if exists(db) else None
        if found:
            if json.loads(found[0])['plan']!=value:raise fail('Original control rotation approval changed')
            validate(db);return response(value,True)
        indexes=selection(db,prefix,value['export_id']);before,_=capacity.snapshot(db)
        if before!=value['before'] or indexes!=value['indexes']:raise fail('Original control history or capacity changed after preview')
        copies_record=copies.rows(db)[value['export_id']][1];body,_=copies.checked(prefix,copies_record)
        initialize(db)
        for index in indexes:
            source,job=original(body,index['job_id']);record={**index,'rotation_id':value['id']}
            stub=compact(job,{'format':FORMAT,**record})
            if len(encode(stub))>=len(source[5].encode()):raise fail('Migration would not reduce the live job body; original SQL was retained')
            db.execute('INSERT INTO control_rotation_jobs VALUES(?,?,?,?)',(record['job_id'],record['export_id'],record['rotation_id'],encode(record).decode()))
            db.execute('UPDATE jobs SET data=? WHERE id=? AND data=?',(encode(stub).decode(),job['id'],source[5]))
        db.execute('INSERT INTO control_rotation_changes VALUES(?,?)',(value['id'],encode({'plan':value,'applied_at':time.time()}).decode()))
        _checkpoint('before_commit');copies.offline.commit(db,meta);_checkpoint('committed')
        return response(value,False)


def response(value,reused):
    return {'id':value['id'],'reused':reused,'migrated_jobs':len(value['indexes']),'normal_read_authority':True,
        'control_slots_released':len(value['indexes']),'online_slots_released':0,'original_identity_rows_retained':True,
        'attempts_results_grants_and_billing_retained':True,'authorization_restored':False,'automatic_rotation_enabled':False}


def require_compatible(state,role,manifest):
    if role!='controller' or 'control_rotation.py' in manifest.get('files',{}):return
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if count(db):raise DeployError('Migrated control history requires matching readers; refusing downgrade')


def require_history(current,staged):
    def saved(state):
        path=Path(state)/'jobs.sqlite3'
        if not path.exists():return None
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not exists(db):return None
            validate(db)
            return {name:list(db.execute('SELECT * FROM '+name+' ORDER BY rowid')) for name in ['control_rotation_jobs','control_rotation_changes']}
    old=saved(current)
    if old is not None and saved(staged)!=old:raise DeployError('Restore would lose original control migration proofs or capacity credits')

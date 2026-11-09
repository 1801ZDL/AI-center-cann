"""Result-body rotation into original verified private job snapshots.

Identity/ACL/kind/order remain live SQL. No job, grant, budget or archive slot is
deleted or credited. Readers must apply current task ACLs before hydrating rows.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import control_history as copies
import control_rotation as jobs
from deployment import DeployError,digest,verify

FORMAT='ascend-lab-result-history/v1'
REF='_result_history_ref'
FIELDS={'schema','job_id','owner','project','kind'}
MAX_ROWS=4096
MAX_CHANGES=128
MAX_RECORD=128*1024
MAX_TOTAL=256*1024


def encode(value):return copies.encoded(value)
def sha(value):return hashlib.sha256(encode(value)).hexdigest()
def raw_sha(raw):return hashlib.sha256(raw.encode()).hexdigest()
def fail(message):return jobs.fail(message)
def exists(db):return copies.exists(db,'result_history_rows')
def _checkpoint(stage):pass


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS result_history_rows(job_id TEXT PRIMARY KEY,export_id TEXT NOT NULL,operation_id TEXT NOT NULL,record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS result_history_changes(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS result_history_export ON result_history_rows(export_id)')


def row_record(db,identifier):
    if not exists(db):return None
    row=db.execute('SELECT export_id,operation_id,record FROM result_history_rows WHERE job_id=?',(identifier,)).fetchone()
    if row is None:return None
    if len(row[2].encode())>4096:raise fail('Result history reference exceeds its bound')
    value=json.loads(row[2])
    fields={'job_id','export_id','operation_id','record_sha256','job_rotation_id'}
    if (not isinstance(value,dict) or set(value)!=fields or value['job_id']!=identifier
        or (value['export_id'],value['operation_id'])!=tuple(row[:2])
        or any(not re.fullmatch('[0-9a-f]{64}',str(value[k])) for k in fields-{'job_id'})):
        raise fail('Invalid result history reference')
    return value


def check(value):
    fields={'format','binding','export_id','selection','rows','id'}
    if (not isinstance(value,dict) or set(value)!=fields or value['format']!=FORMAT
        or len(encode(value))>65536 or value['id']!=sha({k:v for k,v in value.items() if k!='id'})
        or not isinstance(value['binding'],dict) or not re.fullmatch('[0-9a-f]{64}',str(value['export_id']))):
        raise fail('Invalid original result history approval')
    prefix=value['binding'].get('prefix')
    if not isinstance(prefix,str) or not prefix.startswith('/') or str(Path(prefix))!=prefix or '..' in Path(prefix).parts:
        raise fail('Result migration prefix must be canonical')
    copies.selection(value['selection'])
    rows=value['rows']
    if not isinstance(rows,list) or len(rows)>32:raise fail('Result history selects at most32 original rows')
    for item in rows:
        if (not isinstance(item,dict) or set(item)!={'job_id','record_sha256','job_rotation_id','bytes'}
            or item['job_id'] not in value['selection'] or type(item['bytes']) is not int or not 1<=item['bytes']<=MAX_RECORD
            or any(not re.fullmatch('[0-9a-f]{64}',str(item[k])) for k in ['record_sha256','job_rotation_id'])):
            raise fail('Invalid selected result row')
    if [r['job_id'] for r in rows]!=sorted({r['job_id'] for r in rows}) or sum(r['bytes'] for r in rows)>MAX_TOTAL:
        raise fail('Result history selection exceeds its finite budget')
    return value


def original(body,identifier,job):
    table=body.get('tables',{}).get('workflow_results')
    if table is None:return None
    if table.get('columns')!=['job_id','record']:raise fail('Unsupported original result table schema')
    selected=[row for row in table['rows'] if isinstance(row,list) and row and row[0]==identifier]
    if not selected:return None
    if len(selected)!=1 or len(selected[0])!=2 or not isinstance(selected[0][1],str):raise fail('Original result is missing or duplicated')
    raw=selected[0][1]
    if len(raw.encode())>MAX_RECORD:raise fail('Original result exceeds128KiB')
    try:
        def invalid(_):raise ValueError()
        value=json.loads(raw,parse_constant=invalid)
        if (not isinstance(value,dict) or REF in value
            or (value.get('job_id'),value.get('owner'),value.get('project'))!=(identifier,job['owner'],job.get('project','personal:'+job['owner']))):
            raise ValueError()
        from workflow_spec import WORKFLOWS
        if value.get('kind') not in set(WORKFLOWS.values()):raise ValueError()
    except (ValueError,TypeError,RecursionError):raise fail('Original structured result identity or content differs') from None
    return raw,value


def compact(value,reference):
    return {**{k:v for k,v in value.items() if k in FIELDS},REF:{'format':FORMAT,**reference}}


def copied(db,export_id,cache):
    found=copies.rows(db).get(export_id)
    if found is None or found[0] not in {'verified','restore_frozen'}:raise fail('Original result snapshot is not verified')
    if found[1]['plan']['source']['bytes']>MAX_TOTAL:raise fail('Result history uses original copies at most256KiB')
    if export_id not in cache:
        cache.clear()
        try:cache[export_id]=copies.checked(found[1]['plan']['binding']['prefix'],found[1])[0]
        except (OSError,ValueError):raise fail('Original result snapshot is unavailable; retained result was not replaced') from None
    return found,cache[export_id]


def resolve(db,identifier,raw,*,cache=None):
    value=json.loads(raw);record=row_record(db,identifier)
    if not isinstance(value,dict):raise fail('Invalid retained result record')
    if REF not in value:
        if record is not None:raise fail('Migrated result lost its original reference')
        return raw
    with jobs.read_admission():
        if record is None or value[REF]!={'format':FORMAT,**record}:raise fail('Result and original history reference differ')
        cache={} if cache is None else cache
        _,body=copied(db,record['export_id'],cache)
        jobrow=db.execute('SELECT data FROM jobs WHERE id=?',(identifier,)).fetchone()
        migration=jobs.row_record(db,identifier)
        if jobrow is None or migration is None or (migration['export_id'],migration['rotation_id'])!=(record['export_id'],record['job_rotation_id']):
            raise fail('Original migrated job binding differs from result history')
        job=jobs.read(db,jobrow[0],cache=cache)
        saved=original(body,identifier,job)
        if saved is None or raw_sha(saved[0])!=record['record_sha256']:raise fail('Original result snapshot row differs')
        receipt=db.execute('SELECT record FROM result_history_changes WHERE id=?',(record['operation_id'],)).fetchone()
        if receipt is None or len(receipt[0].encode())>131072:raise fail('Original result history approval is missing')
        approval=json.loads(receipt[0])['plan'];check(approval)
        item={'job_id':identifier,'record_sha256':record['record_sha256'],'job_rotation_id':record['job_rotation_id'],'bytes':len(saved[0].encode())}
        if approval['id']!=record['operation_id'] or approval['export_id']!=record['export_id'] or item not in approval['rows']:
            raise fail('Result was not part of the original approval')
        if value!=compact(saved[1],record):raise fail('Retained result identity or filter projection differs')
        return saved[0]


def read_many(db,rows):
    rows=list(rows)
    if len(rows)>200:raise fail('Read at most200 authorized results at a time')
    groups={};result=[None]*len(rows)
    for index,(identifier,raw) in enumerate(rows):
        value=json.loads(raw);reference=value.get(REF) if isinstance(value,dict) else None
        key=reference.get('export_id') if isinstance(reference,dict) else None
        if not isinstance(key,str):key=None
        groups.setdefault(key,[]).append((index,identifier,raw))
    with jobs.read_admission(any(k is not None for k in groups)):
        cache={}
        try:
            for members in groups.values():
                for index,identifier,raw in members:result[index]=json.loads(resolve(db,identifier,raw,cache=cache))
        finally:cache.clear()
    return result


def guard_write(db,identifier):
    if row_record(db,identifier) is not None:raise fail('Migrated result is immutable; retain the original history receipt')


def selection(db,export_id):
    cache={};found,body=copied(db,export_id,cache)
    if found[0]!='verified':raise fail('Restored result snapshots remain read-only')
    identifiers=copies.selection(found[1]['plan']['selection']);selected=[]
    for ident in identifiers:
        jobrow=db.execute('SELECT data FROM jobs WHERE id=?',(ident,)).fetchone();migration=jobs.row_record(db,ident)
        if jobrow is None or migration is None or migration['export_id']!=export_id:
            raise fail('Migrate each original job before rotating its result')
        job=jobs.read(db,jobrow[0],cache=cache);saved=original(body,ident,job)
        current=db.execute('SELECT record FROM workflow_results WHERE job_id=?',(ident,)).fetchone()
        record=row_record(db,ident)
        if record:
            if current is None:raise fail('Migrated result SQL identity is missing')
            resolve(db,ident,current[0],cache=cache);continue
        if saved is None:
            if current is not None:raise fail('Live result was not in the original snapshot')
            continue
        if current is None or current[0]!=saved[0]:raise fail('Result changed after the original verified copy')
        item={'job_id':ident,'record_sha256':raw_sha(saved[0]),'job_rotation_id':migration['rotation_id'],'bytes':len(saved[0].encode())}
        reference={'job_id':ident,'export_id':export_id,'operation_id':'0'*64,**{k:item[k] for k in ['record_sha256','job_rotation_id']}}
        if len(encode(compact(saved[1],reference)))>=item['bytes']:continue
        selected.append(item)
    if sum(r['bytes'] for r in selected)>MAX_TOTAL:raise fail('Selected original results exceed256KiB')
    return identifiers,selected,cache


@contextmanager
def context(prefix):
    _,_,release=verify(prefix)
    if not (release/'result_history.py').is_file() or digest(release/'result_history.py')!=digest(Path(__file__)):
        raise DeployError('Activate matching result history code before maintenance')
    with jobs.context(prefix) as state:yield state


def plan(prefix,export_id):
    if not isinstance(export_id,str) or not re.fullmatch('[0-9a-f]{64}',export_id):raise fail('Invalid original result snapshot ID')
    with context(prefix) as (db,binding,meta),jobs.read_admission():
        identifiers,rows,_=selection(db,export_id)
        value={'format':FORMAT,'binding':binding,'export_id':export_id,'selection':identifiers,'rows':rows}
        value['id']=sha(value)
        return check(value)


def apply(prefix,value):
    check(value)
    with context(prefix) as (db,binding,meta),jobs.read_admission():
        if value['binding']!=binding:raise fail('Original result migration binding is stale')
        receipt=db.execute('SELECT record FROM result_history_changes WHERE id=?',(value['id'],)).fetchone() if exists(db) else None
        if receipt:
            if len(receipt[0].encode())>131072 or json.loads(receipt[0])['plan']!=value:raise fail('Original result approval changed')
            cache={}
            for item in value['rows']:
                expected={'job_id':item['job_id'],'export_id':value['export_id'],'operation_id':value['id'],
                          **{k:item[k] for k in ['record_sha256','job_rotation_id']}}
                if row_record(db,item['job_id'])!=expected:raise fail('Original result migration index is missing or changed')
                row=db.execute('SELECT record FROM workflow_results WHERE job_id=?',(item['job_id'],)).fetchone()
                if row is None:raise fail('Migrated result SQL identity is missing')
                resolve(db,item['job_id'],row[0],cache=cache)
            return response(value,True)
        identifiers,selected,cache=selection(db,value['export_id'])
        if identifiers!=value['selection'] or selected!=value['rows']:raise fail('Original result selection changed after preview')
        if not selected:return response(value,False)
        if exists(db):
            if db.execute('SELECT count(*) FROM result_history_rows').fetchone()[0]+len(selected)>MAX_ROWS:raise fail('Finite result history index is full')
            if db.execute('SELECT count(*) FROM result_history_changes').fetchone()[0]>=MAX_CHANGES:raise fail('Finite result history approvals are full')
        initialize(db);body=cache[value['export_id']]
        for item in selected:
            ident=item['job_id'];_,job=jobs.original(body,ident);raw,record=original(body,ident,job)
            reference={'job_id':ident,'export_id':value['export_id'],'operation_id':value['id'],
                       **{k:item[k] for k in ['record_sha256','job_rotation_id']}}
            db.execute('INSERT INTO result_history_rows VALUES(?,?,?,?)',(ident,value['export_id'],value['id'],encode(reference).decode()))
            cursor=db.execute('UPDATE workflow_results SET record=? WHERE job_id=? AND record=?',(encode(compact(record,reference)).decode(),ident,raw))
            if cursor.rowcount!=1:raise fail('Original result row changed during migration')
        db.execute('INSERT INTO result_history_changes VALUES(?,?)',(value['id'],encode({'plan':value}).decode()))
        _checkpoint('before_commit');copies.offline.commit(db,meta);_checkpoint('committed')
        return response(value,False)


def response(value,reused):
    return {'id':value['id'],'reused':reused,'migrated_results':len(value['rows']),'result_ids_and_order_retained':True,
            'quota_released':False,'authorization_restored':False,'all_related_sql_rotated':False}


def require_compatible(state,role,manifest):
    path=Path(state)/'jobs.sqlite3'
    if role!='controller' or 'result_history.py' in manifest.get('files',{}) or not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if history_present(db) or has_refs(db):
            raise DeployError('Migrated results require matching history readers; refusing downgrade')


def has_refs(db):
    if not copies.exists(db,'workflow_results'):return False
    return bool(db.execute("SELECT 1 FROM workflow_results WHERE json_valid(record) AND json_type(record,'$."+REF+"') IS NOT NULL LIMIT 1").fetchone())


def history_present(db):
    return any(copies.exists(db,name) and db.execute('SELECT 1 FROM '+name+' LIMIT 1').fetchone()
               for name in ['result_history_rows','result_history_changes'])


def require_history(current,staged):
    path=Path(current)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as old:
        if not exists(old):
            if has_refs(old) or history_present(old):raise DeployError('Migrated results lost original migration receipts')
            return
        with sqlite3.connect((Path(staged)/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as new:
            for name in ['result_history_rows','result_history_changes']:
                if not copies.exists(new,name):raise DeployError('Restore would lose original result migration receipts')
                bound=MAX_ROWS if name=='result_history_rows' else MAX_CHANGES
                before=list(old.execute('SELECT * FROM '+name+' ORDER BY rowid LIMIT ?', (bound+1,)))
                after=list(new.execute('SELECT * FROM '+name+' ORDER BY rowid LIMIT ?', (bound+1,)))
                if len(before)>bound or before!=after:raise DeployError('Restore would change original result migration history')
            checked=[]
            for ident, in old.execute('SELECT job_id FROM result_history_rows'):
                before=old.execute('SELECT record FROM workflow_results WHERE job_id=?',(ident,)).fetchone()
                after=new.execute('SELECT record FROM workflow_results WHERE job_id=?',(ident,)).fetchone()
                if before is None or before!=after:raise DeployError('Restore would lose original migrated result projection')
                checked.append((ident,before[0]))
            for start in range(0,len(checked),200):
                read_many(old,checked[start:start+200]);read_many(new,checked[start:start+200])

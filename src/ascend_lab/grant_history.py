"""Inactive source-grant bodies: historical data, never live authorization."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time

import control_history as copies
import control_rotation as jobs
from deployment import DeployError,verify,digest

FORMAT='ascend-lab-grant-history/v1'
REF='_grant_history_ref'
COLUMNS=['id','owner','idem_key','fingerprint','data','source_job_id','expires_at','state']
FIELDS={'schema','id','owner','source_job_id','destination_experiment_id','expires_at'}
OVERLAYS={'state','revoked_at','restore_notice'}
INACTIVE={'revoked','restored_frozen','archived_inactive'}
MAX_ROWS=4096
MAX_CHANGES=128
MAX_BODY=64*1024
MAX_TOTAL=256*1024


def encode(value):return copies.encoded(value)
def sha(value):return copies.sha(encode(value))
def fail(message):return jobs.fail(message)
def exists(db):return copies.exists(db,'grant_history_rows')
def _checkpoint(stage):pass


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS grant_history_rows(grant_id TEXT PRIMARY KEY,export_id TEXT NOT NULL,operation_id TEXT NOT NULL,record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS grant_history_changes(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS grant_history_export ON grant_history_rows(export_id)')


def row_record(db,identifier):
    if not exists(db):return None
    row=db.execute('SELECT export_id,operation_id,record FROM grant_history_rows WHERE grant_id=?',(identifier,)).fetchone()
    if row is None:return None
    if len(row[2].encode())>4096:raise fail('Grant history index exceeds its bound')
    value=json.loads(row[2])
    fields={'grant_id','export_id','operation_id','source_row_sha256','job_rotation_id'}
    if (not isinstance(value,dict) or set(value)!=fields or value['grant_id']!=identifier
        or (value['export_id'],value['operation_id'])!=tuple(row[:2])
        or any(not re.fullmatch('[0-9a-f]{64}',str(value[k])) for k in fields-{'grant_id'})):
        raise fail('Invalid retained grant history index')
    return value


def decoded(row):
    if not isinstance(row,(list,tuple)) or len(row)!=8 or not isinstance(row[4],str) or len(row[4].encode())>MAX_BODY:
        raise fail('Grant row exceeds64KiB or has an invalid shape')
    try:
        def invalid(_):raise ValueError()
        value=json.loads(row[4],parse_constant=invalid)
        if (not isinstance(value,dict) or (value.get('id'),value.get('owner'),value.get('source_job_id'),value.get('expires_at'),value.get('state'))
            !=(row[0],row[1],row[5],row[6],row[7]) or not isinstance(value.get('destination_experiment_id'),str)
            or type(row[6]) not in {int,float} or not math.isfinite(row[6])):raise ValueError()
    except (ValueError,TypeError,RecursionError):raise fail('Grant SQL columns and retained record disagree') from None
    return value


def original(body,identifier):
    table=body.get('tables',{}).get('source_grants')
    if table is None or table.get('columns')!=COLUMNS:raise fail('Original source-grant table is unavailable or incompatible')
    found=[r for r in table['rows'] if isinstance(r,list) and r and r[0]==identifier]
    if len(found)!=1:raise fail('Original source grant is missing or duplicated')
    value=decoded(found[0])
    if REF in value:raise fail('Chained grant history copies are not supported')
    return found[0],value


def projection(value,reference,state=None):
    out={k:v for k,v in value.items() if k in FIELDS|OVERLAYS}
    if state is not None:out['state']=state
    return {**out,REF:{'format':FORMAT,**reference}}


def check(value):
    if (not isinstance(value,dict) or set(value)!={'format','binding','export_id','selection','rows','id'}
        or value['format']!=FORMAT or len(encode(value))>65536 or value['id']!=sha({k:v for k,v in value.items() if k!='id'})
        or not isinstance(value['binding'],dict) or not re.fullmatch('[0-9a-f]{64}',str(value['export_id']))):
        raise fail('Invalid original grant history approval')
    prefix=value['binding'].get('prefix')
    if not isinstance(prefix,str) or not prefix.startswith('/') or str(Path(prefix))!=prefix or '..' in Path(prefix).parts:
        raise fail('Grant history prefix must be canonical')
    copies.selection(value['selection'])
    if not isinstance(value['rows'],list) or len(value['rows'])>32:raise fail('Select at most32 inactive grants')
    for item in value['rows']:
        if (not isinstance(item,dict) or set(item)!={'grant_id','source_row_sha256','live_row_sha256','job_rotation_id','bytes'}
            or not re.fullmatch('source-grant-[0-9a-f]{24}',str(item['grant_id']))
            or type(item['bytes']) is not int or not 1<=item['bytes']<=MAX_BODY
            or any(not re.fullmatch('[0-9a-f]{64}',str(item[k])) for k in ['source_row_sha256','live_row_sha256','job_rotation_id'])):
            raise fail('Invalid original grant selection')
    if [r['grant_id'] for r in value['rows']]!=sorted({r['grant_id'] for r in value['rows']}) or sum(r['bytes'] for r in value['rows'])>MAX_TOTAL:
        raise fail('Grant history selection exceeds its finite bound')
    return value


def read(db,identifier,raw,*,cache=None):
    value=json.loads(raw);reference=row_record(db,identifier)
    if not isinstance(value,dict):raise fail('Invalid retained source grant')
    current=db.execute('SELECT '+','.join(COLUMNS)+' FROM source_grants WHERE id=?',(identifier,)).fetchone()
    if current is None or current[4]!=raw:raise fail('Current source grant is unavailable')
    live=decoded(current)
    if REF not in value:
        if reference is not None:raise fail('Migrated grant lost its original reference')
        return value
    if reference is None or value[REF]!={'format':FORMAT,**reference}:raise fail('Grant history reference differs')
    if live['state'] not in INACTIVE:raise fail('Archived grant must never regain active authority')
    with jobs.read_admission():
        cache={} if cache is None else cache
        try:_,body=copies.bounded_body(db,reference['export_id'],cache)
        except (ValueError,OSError):raise fail('Original grant snapshot is unavailable; authorization was not restored') from None
        source,saved=original(body,identifier)
        if sha(source)!=reference['source_row_sha256'] or tuple(current[:4])+tuple(current[5:7])!=tuple(source[:4])+tuple(source[5:7]):
            raise fail('Original grant admission identity differs')
        parent=db.execute('SELECT data FROM jobs WHERE id=?',(source[5],)).fetchone();migration=jobs.row_record(db,source[5])
        if parent is None or migration is None or (migration['export_id'],migration['rotation_id'])!=(reference['export_id'],reference['job_rotation_id']):
            raise fail('Grant history producer binding differs')
        job=jobs.read(db,parent[0],cache=cache)
        if job['owner']!=source[1]:raise fail('Grant owner and producer differ')
        receipt=db.execute('SELECT record FROM grant_history_changes WHERE id=?',(reference['operation_id'],)).fetchone()
        if receipt is None or len(receipt[0].encode())>131072:raise fail('Original grant history approval is missing')
        approval=json.loads(receipt[0])['plan'];check(approval)
        if approval['id']!=reference['operation_id'] or approval['export_id']!=reference['export_id'] or not any(
            r['grant_id']==identifier and r['source_row_sha256']==reference['source_row_sha256'] and r['job_rotation_id']==reference['job_rotation_id'] for r in approval['rows']):
            raise fail('Grant was not in its original migration approval')
        expected=projection(saved,reference)
        for key in OVERLAYS:
            if key in live:expected[key]=live[key]
            else:expected.pop(key,None)
        if expected!=live:raise fail('Retained grant scope or projection differs')
        restored={**saved}
        for key in OVERLAYS:
            if key in live:restored[key]=live[key]
            else:restored.pop(key,None)
        return {**restored,'history_archived':True,'history_original_state':saved['state']}


def read_many(db,rows):
    rows=list(rows)
    if len(rows)>100:raise fail('Read at most100 authorized grants')
    groups={};out=[None]*len(rows)
    for i,(ident,raw) in enumerate(rows):
        value=json.loads(raw);reference=value.get(REF) if isinstance(value,dict) else None
        key=reference.get('export_id') if isinstance(reference,dict) else None
        groups.setdefault(key if isinstance(key,str) else None,[]).append((i,ident,raw))
    with jobs.read_admission(any(key is not None for key in groups)):
        cache={}
        try:
            for members in groups.values():
                for i,ident,raw in members:out[i]=read(db,ident,raw,cache=cache)
        finally:cache.clear()
    return out


def selected(db,export_id,epoch):
    cache={};found,body=copies.bounded_body(db,export_id,cache)
    if found[0]!='verified':raise fail('Restored grant snapshots remain read-only')
    identifiers=copies.selection(found[1]['plan']['selection']);table=body.get('tables',{}).get('source_grants')
    if table is None:return identifiers,[],cache
    if table.get('columns')!=COLUMNS:raise fail('Unsupported original grant schema')
    rows=[];seen=set()
    for source in table['rows']:
        if len(source)!=8:raise fail('Invalid original grant row')
        if source[5] not in identifiers:continue
        if source[0] in seen:raise fail('Duplicate original grant')
        seen.add(source[0]);saved=decoded(source)
        if REF in saved:raise fail('Chained grant snapshots are not supported')
        current=db.execute('SELECT '+','.join(COLUMNS)+' FROM source_grants WHERE id=?',(source[0],)).fetchone()
        if current is None:raise fail('Original grant identity is missing')
        live=decoded(current)
        if row_record(db,source[0]):read(db,source[0],current[4],cache=cache);continue
        if live['state']=='active' and live['expires_at']>epoch:continue
        if live['state'] not in {'active','revoked','restored_frozen'}:raise fail('Unknown grant lifecycle state')
        if tuple(current[:4])+tuple(current[5:7])!=tuple(source[:4])+tuple(source[5:7]) or {k:v for k,v in live.items() if k not in OVERLAYS}!={k:v for k,v in saved.items() if k not in OVERLAYS}:
            raise fail('Original grant scope changed since its private snapshot')
        parent=db.execute('SELECT data FROM jobs WHERE id=?',(source[5],)).fetchone();migration=jobs.row_record(db,source[5])
        if parent is None or migration is None or migration['export_id']!=export_id:raise fail('Migrate the original producer before grant history')
        if jobs.read(db,parent[0],cache=cache)['owner']!=source[1]:raise fail('Grant and producer owners differ')
        item={'grant_id':source[0],'source_row_sha256':sha(source),'live_row_sha256':sha(current),
              'job_rotation_id':migration['rotation_id'],'bytes':len(current[4].encode())}
        reference={'grant_id':source[0],'export_id':export_id,'operation_id':'0'*64,
                   **{k:item[k] for k in ['source_row_sha256','job_rotation_id']}}
        state='archived_inactive' if live['state']=='active' else live['state']
        if len(encode(projection(live,reference,state)))>=item['bytes']:continue
        rows.append(item)
        if len(rows)>32 or sum(r['bytes'] for r in rows)>MAX_TOTAL:raise fail('Inactive grant selection exceeds32rows/256KiB')
    return identifiers,sorted(rows,key=lambda r:r['grant_id']),cache


@contextmanager
def context(prefix):
    _,_,release=verify(prefix)
    if not (release/'grant_history.py').is_file() or digest(release/'grant_history.py')!=digest(Path(__file__)):
        raise DeployError('Activate matching inactive grant history code')
    with jobs.context(prefix) as value:yield value


def plan(prefix,export_id,*,epoch=None):
    if not isinstance(export_id,str) or not re.fullmatch('[0-9a-f]{64}',export_id):raise fail('Invalid original grant snapshot')
    epoch=time.time() if epoch is None else epoch
    if type(epoch) not in {int,float} or not math.isfinite(epoch):raise fail('Invalid grant selection clock')
    with context(prefix) as (db,binding,meta),jobs.read_admission():
        identifiers,rows,_=selected(db,export_id,epoch)
        value={'format':FORMAT,'binding':binding,'export_id':export_id,'selection':identifiers,'rows':rows}
        value['id']=sha(value);return check(value)


def apply(prefix,value):
    check(value)
    with context(prefix) as (db,binding,meta),jobs.read_admission():
        if value['binding']!=binding:raise fail('Original grant history binding is stale')
        receipt=db.execute('SELECT record FROM grant_history_changes WHERE id=?',(value['id'],)).fetchone() if exists(db) else None
        if receipt:
            if len(receipt[0].encode())>131072 or json.loads(receipt[0])['plan']!=value:raise fail('Original grant approval changed')
            cache={}
            for item in value['rows']:
                expected={'grant_id':item['grant_id'],'export_id':value['export_id'],'operation_id':value['id'],
                          **{k:item[k] for k in ['source_row_sha256','job_rotation_id']}}
                if row_record(db,item['grant_id'])!=expected:raise fail('Original grant migration index is missing')
                raw=db.execute('SELECT data FROM source_grants WHERE id=?',(item['grant_id'],)).fetchone()
                if raw is None:raise fail('Original archived grant is missing')
                read(db,item['grant_id'],raw[0],cache=cache)
            return response(value,True)
        identifiers,rows,_=selected(db,value['export_id'],time.time())
        if identifiers!=value['selection'] or rows!=value['rows']:raise fail('Original inactive grant selection changed after preview')
        if not rows:return response(value,False)
        count=db.execute('SELECT count(*) FROM grant_history_rows').fetchone()[0] if exists(db) else 0
        changes=db.execute('SELECT count(*) FROM grant_history_changes').fetchone()[0] if exists(db) else 0
        if count+len(rows)>MAX_ROWS or changes>=MAX_CHANGES:raise fail('Finite inactive grant history is full')
        initialize(db)
        for item in rows:
            ident=item['grant_id'];current=db.execute('SELECT '+','.join(COLUMNS)+' FROM source_grants WHERE id=?',(ident,)).fetchone()
            live=decoded(current);reference={'grant_id':ident,'export_id':value['export_id'],'operation_id':value['id'],
                  **{k:item[k] for k in ['source_row_sha256','job_rotation_id']}}
            # A retired expired grant cannot become active after clock rollback.
            state='archived_inactive' if live['state']=='active' else live['state']
            db.execute('INSERT INTO grant_history_rows VALUES(?,?,?,?)',(ident,value['export_id'],value['id'],encode(reference).decode()))
            cursor=db.execute('UPDATE source_grants SET data=?,state=? WHERE id=? AND data=? AND state=?',
                (encode(projection(live,reference,state)).decode(),state,ident,current[4],current[7]))
            if cursor.rowcount!=1:raise fail('Original grant changed during migration')
        db.execute('INSERT INTO grant_history_changes VALUES(?,?)',(value['id'],encode({'plan':value}).decode()))
        _checkpoint('before_commit');copies.offline.commit(db,meta);_checkpoint('committed')
        return response(value,False)


def response(value,reused):
    return {'id':value['id'],'reused':reused,'migrated_grants':len(value['rows']),
            'authorization_restored':False,'charged_budget_refunded':False,'all_related_sql_rotated':False}


def history_present(db):
    return any(copies.exists(db,name) and db.execute('SELECT 1 FROM '+name+' LIMIT 1').fetchone() for name in ['grant_history_rows','grant_history_changes'])


def has_refs(db):
    return copies.exists(db,'source_grants') and bool(db.execute("SELECT 1 FROM source_grants WHERE json_valid(data) AND json_type(data,'$."+REF+"') IS NOT NULL LIMIT 1").fetchone())


def require_compatible(state,role,manifest):
    path=Path(state)/'jobs.sqlite3'
    if role!='controller' or 'grant_history.py' in manifest.get('files',{}) or not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if history_present(db) or has_refs(db):raise DeployError('Inactive grant history requires matching readers; refusing downgrade')


def require_history(current,staged):
    path=Path(current)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as old:
        if not exists(old):
            if history_present(old) or has_refs(old):raise DeployError('Archived grants lost original migration receipts')
            return
        with sqlite3.connect((Path(staged)/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as new:
            for name,limit in [('grant_history_rows',MAX_ROWS),('grant_history_changes',MAX_CHANGES)]:
                if not copies.exists(new,name):raise DeployError('Restore would lose inactive grant history')
                before=list(old.execute('SELECT * FROM '+name+' ORDER BY rowid LIMIT ?',(limit+1,)))
                after=list(new.execute('SELECT * FROM '+name+' ORDER BY rowid LIMIT ?',(limit+1,)))
                if len(before)>limit or before!=after:raise DeployError('Restore would change original grant history')
            batch=[]
            for ident, in old.execute('SELECT grant_id FROM grant_history_rows'):
                a=old.execute('SELECT '+','.join(COLUMNS)+' FROM source_grants WHERE id=?',(ident,)).fetchone()
                b=new.execute('SELECT '+','.join(COLUMNS)+' FROM source_grants WHERE id=?',(ident,)).fetchone()
                if a is None or a!=b:raise DeployError('Restore would lose inactive grant state or revocations')
                batch.append((ident,a[4]))
                if len(batch)==100:read_many(old,batch);read_many(new,batch);batch=[]
            if batch:read_many(old,batch);read_many(new,batch)

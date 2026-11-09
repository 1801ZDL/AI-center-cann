"""Finite operator decisions over original events; never performs delivery."""
from contextlib import contextmanager,closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

SCHEMA='ascend-lab-notification-operation/v1'
FIELDS='id,owner,created,payload,delivered,attempts,due,lease_until,lease_token,destination_sha'
MAX_OPERATIONS=1024
MAX_EVENT_OPERATIONS=8  # Last slot is reserved for closing, never another retry.
MAX_ATTEMPTS=64
MAX_PLAN_BYTES=2048
REASONS={'operator_review','transient_failure','destination_update','no_longer_needed','receiver_checked'}
EVENT=re.compile('event-[a-f0-9]{32}')


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()
def exists(db,name):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())
def finite(value):return type(value) in {int,float} and 0<=value<=2**63-1 and math.isfinite(value)
def sha(value):return isinstance(value,str) and bool(re.fullmatch('[a-f0-9]{64}',value))


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS notification_event_control(event_id TEXT PRIMARY KEY,state TEXT NOT NULL,retry_limit INTEGER NOT NULL,revision INTEGER NOT NULL,operation_id TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS notification_operations(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,record TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS notification_operations_event ON notification_operations(event_id,id)')


def selection(db):
    if not exists(db,'notification_event_control'):
        if exists(db,'notification_operations') and db.execute('SELECT 1 FROM notification_operations LIMIT 1').fetchone():raise ValueError('Notification control projection is missing')
        return '',"'active'",'8'
    if not exists(db,'notification_operations'):raise ValueError('Notification decision evidence is missing')
    return ' LEFT JOIN notification_event_control c ON c.event_id=n.id ',"coalesce(c.state,'active')",'coalesce(c.retry_limit,8)'


def check(value):
    keys={'schema','id','binding','event_id','owner','decision','reason','before_sha256','destination_sha','retry_limit','not_before','expires','accept_uncertain'}
    if not isinstance(value,dict) or set(value)!=keys or value['schema']!=SCHEMA or len(encoded(value))>MAX_PLAN_BYTES:
        raise ValueError('Invalid bounded notification operation')
    if not sha(value['id']) or digest({k:v for k,v in value.items() if k!='id'})!=value['id']:raise ValueError('Notification operation digest differs')
    if not isinstance(value['event_id'],str) or not EVENT.fullmatch(value['event_id']) or not isinstance(value['owner'],str) or not 1<=len(value['owner'])<=64:
        raise ValueError('Invalid notification operation identity')
    if (not isinstance(value['decision'],str) or value['decision'] not in {'hold','retry','rebind','close'}
        or not isinstance(value['reason'],str) or value['reason'] not in REASONS or not sha(value['before_sha256'])
        or value['destination_sha'] is not None and not sha(value['destination_sha'])
        or type(value['retry_limit']) is not int or not 8<=value['retry_limit']<=MAX_ATTEMPTS
        or type(value['accept_uncertain']) is not bool):raise ValueError('Invalid notification decision')
    if not finite(value['not_before']) or not finite(value['expires']) or not 0<value['expires']-value['not_before']<=3600:
        raise ValueError('Notification operation requires a finite one-hour validity')
    binding=value['binding']
    if not isinstance(binding,dict) or set(binding)!={'prefix','release_id','state_dir','uid','gid','database_identity'}:raise ValueError('Invalid notification operation binding')
    if any(not isinstance(binding[k],str) or not 1<=len(binding[k])<=256 or not Path(binding[k]).is_absolute() or '..' in Path(binding[k]).parts for k in ['prefix','state_dir']):
        raise ValueError('Invalid installed notification paths')
    if not sha(binding['release_id']) or any(type(binding[k]) is not int or binding[k]<0 for k in ['uid','gid']):raise ValueError('Invalid notification service identity')
    identity=binding['database_identity']
    if not isinstance(identity,list) or len(identity)!=2 or any(type(v) is not int or v<0 for v in identity):raise ValueError('Invalid notification database identity')
    return value


def receipt(db,identifier):
    if not exists(db,'notification_operations'):return None
    row=db.execute('SELECT event_id,CASE WHEN length(CAST(record AS BLOB))<=4096 THEN record END FROM notification_operations WHERE id=?',(identifier,)).fetchone()
    if row is None:return None
    if row[1] is None:raise ValueError('Oversized notification receipt')
    value=json.loads(row[1])
    if not isinstance(value,dict) or set(value)!={'plan','applied_epoch','after_control','after_sha256'}:raise ValueError('Invalid notification receipt')
    plan=check(value['plan']);control=value['after_control']
    if (plan['id']!=identifier or plan['event_id']!=row[0] or not finite(value['applied_epoch']) or not sha(value['after_sha256'])
        or not isinstance(control,list) or len(control)!=5 or control[0]!=row[0] or control[4]!=identifier):raise ValueError('Notification receipt binding differs')
    if (control[1]!=({'hold':'held','close':'closed'}.get(plan['decision'],'active')) or control[2]!=plan['retry_limit']
        or type(control[3]) is not int or not 1<=control[3]<=MAX_EVENT_OPERATIONS):raise ValueError('Notification receipt decision differs')
    return value


def control(db,identifier):
    if not exists(db,'notification_event_control'):
        selection(db);return None
    row=db.execute('SELECT * FROM notification_event_control WHERE event_id=?',(identifier,)).fetchone()
    if row is None:
        if exists(db,'notification_operations') and db.execute('SELECT 1 FROM notification_operations WHERE event_id=? LIMIT 1',(identifier,)).fetchone():raise ValueError('Original event control projection is missing')
        return None
    proof=receipt(db,row[4])
    if proof is None or proof['after_control']!=list(row):raise ValueError('Notification control lost its original decision')
    if db.execute('SELECT count(*) FROM notification_operations WHERE event_id=?',(identifier,)).fetchone()[0]!=row[3]:raise ValueError('Original notification decision history is incomplete')
    return tuple(row)


def current(db,identifier):
    row=db.execute('SELECT '+FIELDS.replace('payload,','CASE WHEN length(CAST(payload AS BLOB))<=4096 THEN payload END,')+' FROM notification_outbox WHERE id=?',(identifier,)).fetchone()
    if row is None:raise ValueError('Original event is archived or missing; do not replace it')
    if row[3] is None or not isinstance(row[0],str) or not EVENT.fullmatch(row[0]):raise ValueError('Invalid original notification')
    payload=json.loads(row[3])
    if not isinstance(payload,dict) or (payload.get('id'),payload.get('owner'))!=row[:2] or payload.get('schema')!='ascend-lab-notification/v1':raise ValueError('Notification payload identity differs')
    if (set(payload)!={'schema','id','owner','event','subject','detail','at'}
        or not isinstance(payload['event'],str) or payload['event'] not in {'job_state','resource_pressure','storage_pressure','node_unavailable'}
        or any(not isinstance(payload[k],str) or len(payload[k])>80 or any(ord(c)<32 for c in payload[k]) for k in ['subject','detail'])
        or not isinstance(payload['at'],str) or len(payload['at'])>64):raise ValueError('Invalid fixed notification summary')
    if not all(finite(row[i]) for i in (2,6,7)) or row[4] is not None and not finite(row[4]):raise ValueError('Invalid notification timing')
    if type(row[5]) is not int or not 0<=row[5]<=MAX_ATTEMPTS or row[9] is not None and not sha(row[9]):raise ValueError('Invalid notification attempt or destination')
    if row[8] is not None and (not isinstance(row[8],str) or not re.fullmatch('[a-f0-9]{32}',row[8])):raise ValueError('Invalid notification lease')
    if (row[8] is None)!=(row[7]==0):raise ValueError('Notification lease identity is incomplete')
    state=control(db,identifier)
    if state is not None and state[1] not in {'active','held','closed'}:raise ValueError('Invalid notification disposition')
    if state is not None and state[1]!='active' and (row[4] is not None or row[8] is not None):raise ValueError('Terminal/held notification still has delivery state')
    return tuple(row),state


def snapshot(row,state):return digest([row,state])


def target(destinations,owner):
    from notifications import destination_fingerprints
    targets=destination_fingerprints(destinations)
    if owner not in targets:raise ValueError('Original owner needs an explicit private destination')
    return targets[owner]


def derive(row,state,decision,destination,retry_limit,accept_uncertain,epoch):
    if row[4] is not None or state is not None and state[1]=='closed':raise ValueError('Delivered or closed events cannot be reopened')
    if row[7]>epoch:raise ValueError('Original delivery lease is still active')
    if (row[8] is not None or decision in {'close','rebind'} and row[5]>0) and not accept_uncertain:
        raise ValueError('Prior delivery may have happened; explicit uncertainty acceptance is required')
    old_limit=state[2] if state else 8
    if not old_limit<=retry_limit<=min(MAX_ATTEMPTS,old_limit+8):raise ValueError('A decision may grant at most eight additional attempts')
    if decision in {'hold','close'}:
        if destination!=row[9] or retry_limit!=old_limit:raise ValueError('Hold/close cannot change delivery authority')
        if decision=='hold' and state is not None and state[1]=='held':raise ValueError('Event is already held')
    else:
        if not sha(destination) or row[5]>=retry_limit:raise ValueError('Explicit finite retry capacity is required')
        if decision=='retry' and row[9] is not None and destination!=row[9]:raise ValueError('Retry must use the original destination')
        if decision=='rebind' and (row[9] is None or destination==row[9]):raise ValueError('Rebind requires an explicitly different original-owner destination')
    revision=(state[3] if state else 0)+1
    if revision>MAX_EVENT_OPERATIONS or revision==MAX_EVENT_OPERATIONS and decision!='close':raise ValueError('Final operation slot is reserved for closing')
    return revision


def plan_in_db(db,binding,identifier,decision,*,reason='operator_review',destinations=None,extra_attempts=0,accept_uncertain=False,epoch=None):
    epoch=time.time() if epoch is None else epoch
    if not isinstance(decision,str) or decision not in {'hold','retry','rebind','close'} or type(extra_attempts) is not int or not 0<=extra_attempts<=8:raise ValueError('Invalid finite notification decision')
    row,state=current(db,identifier);limit=(state[2] if state else 8)+extra_attempts
    destination=target(destinations,row[1]) if decision in {'retry','rebind'} else row[9]
    derive(row,state,decision,destination,limit,accept_uncertain,epoch)
    value={'schema':SCHEMA,'binding':binding,'event_id':row[0],'owner':row[1],'decision':decision,'reason':reason,
           'before_sha256':snapshot(row,state),'destination_sha':destination,'retry_limit':limit,
           'not_before':epoch,'expires':epoch+3600,'accept_uncertain':accept_uncertain}
    value['id']=digest(value);return check(value)


def capacity(db,new_state):
    # Reserve one further receipt for each tracked event that is not closed.
    # Closing consumes that reserved slot, so full ledgers cannot strand holds.
    count=db.execute('SELECT count(*) FROM notification_operations').fetchone()[0]
    reservations=db.execute("SELECT count(*) FROM notification_event_control WHERE state!='closed'").fetchone()[0]
    previous=db.execute('SELECT state FROM notification_event_control WHERE event_id=?',(new_state[0],)).fetchone()
    after=reservations-(1 if previous and previous[0]!='closed' else 0)+(new_state[1]!='closed')
    if count+1+after>MAX_OPERATIONS:raise ValueError('Notification decision ledger is full; archive completed events first')


def _checkpoint(stage):pass


def apply_in_db(db,binding,value,*,destinations=None,epoch=None):
    check(value);epoch=time.time() if epoch is None else epoch
    if db.in_transaction:raise ValueError('Notification operation requires its own idle connection')
    with db:
        db.execute('BEGIN IMMEDIATE')
        old=receipt(db,value['id'])
        if old is not None:
            if old['plan']!=value:raise ValueError('Original notification receipt differs')
            return {'status':'already_applied','operation_id':value['id'],'event_id':value['event_id'],'sent':False}
        if binding!=value['binding'] or not finite(epoch) or not value['not_before']<=epoch<value['expires']:
            raise ValueError('Notification operation binding or validity changed')
        initialize(db)
        row,state=current(db,value['event_id'])
        if row[1]!=value['owner'] or snapshot(row,state)!=value['before_sha256']:raise ValueError('Original notification changed; retain the original decision for reconciliation')
        if value['decision'] in {'retry','rebind'} and target(destinations,row[1])!=value['destination_sha']:raise ValueError('Private destination changed after planning')
        revision=derive(row,state,value['decision'],value['destination_sha'],value['retry_limit'],value['accept_uncertain'],epoch)
        after=(row[0],{'hold':'held','close':'closed'}.get(value['decision'],'active'),value['retry_limit'],revision,value['id'])
        capacity(db,after)
        db.execute('INSERT OR REPLACE INTO notification_event_control VALUES(?,?,?,?,?)',after)
        db.execute('UPDATE notification_outbox SET destination_sha=?,due=?,lease_until=0,lease_token=NULL WHERE id=?',
                   (value['destination_sha'],epoch if value['decision'] in {'retry','rebind'} else row[6],row[0]))
        raw=db.execute('SELECT '+FIELDS+' FROM notification_outbox WHERE id=?',(row[0],)).fetchone()
        saved={'plan':value,'applied_epoch':epoch,'after_control':after,'after_sha256':snapshot(raw,after)}
        db.execute('INSERT INTO notification_operations VALUES(?,?,?)',(value['id'],row[0],encoded(saved).decode()))
        _checkpoint('before_commit')
    _checkpoint('committed')
    return {'status':'applied','operation_id':value['id'],'event_id':value['event_id'],'state':after[1],
            'retry_limit':after[2],'delivery_outcome':'unknown' if row[5] else 'not_attempted','sent':False}


def inspect_events(db,*,destinations=None,after_id='',limit=16,epoch=None):
    if type(limit) is not int or not 1<=limit<=32 or not isinstance(after_id,str) or after_id and not EVENT.fullmatch(after_id):raise ValueError('Invalid notification inspection page')
    from notifications import destination_fingerprints
    targets=destination_fingerprints(destinations) if destinations is not None else {}
    epoch=time.time() if epoch is None else epoch;result=[]
    ids=[r[0] for r in db.execute('SELECT id FROM notification_outbox WHERE id>? ORDER BY id LIMIT ?',(after_id,limit+1))]
    for ident in ids[:limit]:
        row,state=current(db,ident);mode=state[1] if state else 'active';maximum=state[2] if state else 8
        status='delivered' if row[4] is not None else 'closed' if mode=='closed' else 'held' if mode=='held' else 'leased' if row[7]>epoch else 'retry_exhausted' if row[5]>=maximum else 'pending'
        destination='not_checked' if destinations is None else 'not_configured'
        if row[1] in targets:
            configured=targets[row[1]];destination='unbound' if row[9] is None else 'same' if row[9]==configured else 'changed'
        payload=json.loads(row[3])
        result.append({'event_id':ident,'owner':row[1],**{k:payload[k] for k in ['event','subject','detail']},'state':status,'attempts':row[5],'retry_limit':maximum,
                       'destination':destination,'expired_lease_uncertain':row[8] is not None and row[7]<=epoch,
                       'operations':state[3] if state else 0})
    return {'events':result,'next_after':ids[limit-1] if len(ids)>limit else None,'sent':False}


def archive_evidence(db,identifiers):
    controls=[];operations=[]
    for identifier in identifiers:
        state=control(db,identifier)
        if state is not None:controls.append(state)
        if exists(db,'notification_operations'):
            rows=list(db.execute('SELECT id,event_id,record FROM notification_operations WHERE event_id=? ORDER BY id LIMIT ?',
                                 (identifier,MAX_EVENT_OPERATIONS+1)))
            if len(rows)>MAX_EVENT_OPERATIONS:raise ValueError('Notification event history exceeds its finite bound')
            for row in rows:receipt(db,row[0])
            operations.extend(rows)
    return {'event_controls':controls,'operations':operations}


@contextmanager
def context(prefix):
    from deployment import require_root,verify
    from audit_retention import service_identity
    from state_lock import state_lock
    require_root();prefix,meta,release=verify(prefix)
    if meta['role']!='controller':raise ValueError('Notification operations require the installed controller')
    for name in ['notification_ops.py','notifications.py','opsctl']:
        if (release/name).read_bytes()!=Path(__file__).with_name(name).read_bytes():raise ValueError('Use matching installed notification operation code')
    with service_identity(meta),state_lock(meta['state_dir']):
        path=Path(meta['state_dir'])/'jobs.sqlite3';info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=meta['uid']:raise ValueError('Original notification database identity differs')
        binding={k:meta[k] for k in ['release_id','state_dir','uid','gid']}
        binding.update(prefix=str(prefix),database_identity=[info.st_dev,info.st_ino])
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=.2)) as db:
            yield db,binding


def plan(prefix,*args,**kwargs):
    with context(prefix) as (db,binding):
        db.execute('BEGIN')
        return plan_in_db(db,binding,*args,**kwargs)


def apply(prefix,value,**kwargs):
    with context(prefix) as (db,binding):return apply_in_db(db,binding,value,**kwargs)


def inspect(prefix,**kwargs):
    with context(prefix) as (db,_):
        db.execute('BEGIN');return inspect_events(db,**kwargs)

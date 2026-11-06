"""Offline export and SHA-confirmed rotation of unbound fixed-reference history.

No jobs, results, policies or payloads are removed. A bounded chain checkpoint
prevents an older local backup from resurrecting discarded approvals. Archives
must be retained separately; the checkpoint cannot recover their contents.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from audit_retention import Database,encoded,protected_parent,service_identity
from deployment import DeployError

CAPABILITY='fixed-reference-history-rotation-v1'
MAX_BYTES=16*1024**2
ZERO='0'*64


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS performance_reference_ledger(singleton INTEGER PRIMARY KEY CHECK(singleton=1),record TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO performance_reference_ledger VALUES(1,?)',
        (json.dumps({'generation':0,'head_sha256':ZERO,'epoch':uuid.uuid4().hex,'receipt':None}),))


def ledger(db):
    row=db.execute('SELECT record FROM performance_reference_ledger WHERE singleton=1').fetchone()
    if row is None:raise DeployError('Initialize reference history with matching controller code before maintenance')
    value=json.loads(row[0])
    if (set(value)!={'generation','head_sha256','epoch','receipt'} or type(value['generation']) is not int or value['generation']<0
        or not re.fullmatch('[0-9a-f]{64}',value['head_sha256']) or not re.fullmatch('[0-9a-f]{32}',value['epoch'])):
        raise DeployError('Invalid reference history checkpoint')
    return value


def save(db,value):db.execute('UPDATE performance_reference_ledger SET record=? WHERE singleton=1',(json.dumps(value,sort_keys=True),))


def read_ledger(path):
    path=Path(path)
    if not path.exists():return None
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_reference_ledger'").fetchone():return None
        return ledger(db)


def require_history(current,staged):
    old=read_ledger(Path(current)/'jobs.sqlite3');new=read_ledger(Path(staged)/'jobs.sqlite3')
    if old is None:return
    if not old['generation'] and old['receipt'] is None:return
    if (new is None or new['generation']!=old['generation'] or
        new['head_sha256']!=old['head_sha256'] or new['receipt']!=old['receipt']):
        raise DeployError('Retained reference rotation checkpoint or receipt would be lost or rewound')


def rebind(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_reference_ledger'").fetchone():return
    value=ledger(db);value['epoch']=uuid.uuid4().hex
    if value['receipt'] is not None:value['receipt']={**value['receipt'],'state':'invalidated'}
    save(db,value)


def retained(db):
    from performance_reference import MAX_REFERENCES,MAX_EVENTS
    refs=[];events=[]
    for identifier,name,raw in db.execute('SELECT id,name,record FROM performance_references ORDER BY id'):
        if len(refs)>=MAX_REFERENCES:raise DeployError('Reference history exceeds bounded export schema')
        value=json.loads(raw)
        if value.get('id')!=identifier or value.get('spec',{}).get('name')!=name:raise DeployError('Reference history row mismatch')
        refs.append({'id':identifier,'name':name,'record':value})
    for identifier,raw in db.execute('SELECT id,record FROM performance_reference_events ORDER BY id'):
        if len(events)>=MAX_EVENTS:raise DeployError('Reference event history exceeds bounded export schema')
        events.append({'id':identifier,'record':json.loads(raw)})
    value={'references':refs,'events':events}
    if len(encoded(value))>MAX_BYTES:raise DeployError('Reference history exceeds bounded export bytes')
    return value


def require_unbound(db,rows):
    identifiers={r['id'] for r in rows['references']}
    for raw, in db.execute('SELECT record FROM device_evidence_policies'):
        value=json.loads(raw)
        if value['state']!='retired' and identifiers.intersection(value['spec'].get('performance_references',{}).values()):
            raise DeployError('Retire every policy referencing this history before rotation; required gates cannot be removed by maintenance')


def archive_read(path,parent):
    fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or stat.S_IMODE(before.st_mode)!=0o600
            or before.st_nlink!=1 or before.st_size>MAX_BYTES+32768):raise DeployError('Unsafe reference history archive')
        raw=stream.read(MAX_BYTES+32769);after=os.fstat(stream.fileno())
    if any(getattr(before,k)!=getattr(after,k) for k in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):
        raise DeployError('Reference history archive changed during read')
    value=json.loads(raw)
    if (not isinstance(value,dict) or set(value)!={'header','history'} or encoded(value)!=raw
        or value['header'].get('format')!='ascend-lab-reference-history/v1'
        or not re.fullmatch('refarchive-[0-9a-f]{32}',str(value['header'].get('id')))
        or hashlib.sha256(encoded(value['history'])).hexdigest()!=value['header'].get('history_sha256')):
        raise DeployError('Invalid reference history archive')
    return value,hashlib.sha256(raw).hexdigest()


@contextmanager
def context(prefix):
    from deployment import require_root,verify,digest
    from state_lock import state_lock
    from control_process import offline
    from maintenance import assert_quiescent
    from control_monitor import directory
    from storage_files import mount
    require_root();prefix,meta,release=verify(prefix)
    if meta['role']!='controller':raise DeployError('Reference history maintenance belongs to a controller')
    if digest(release/Path(__file__).name)!=digest(Path(__file__)):raise DeployError('Use matching installed reference maintenance code')
    state=Path(meta['state_dir'])
    if meta.get('control_filesystem'):
        from control_process import namespaces,context as filesystem_context
        namespaces();filesystem_context(meta).check()
    with state_lock(state,exclusive=True),offline(prefix,meta):
        with directory(state,meta['uid']) as parent:
            fd=os.open('jobs.sqlite3',os.O_PATH|os.O_NOFOLLOW,dir_fd=parent)
            try:
                info=os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=meta['uid'] or info.st_mode&0o077
                    or info.st_dev!=os.fstat(parent).st_dev or mount(fd)!=mount(parent)):raise DeployError('Unsafe reference history database')
            finally:os.close(fd)
        with service_identity(meta):connection=sqlite3.connect(state/'jobs.sqlite3',timeout=5)
        db=Database(connection,meta)
        try:
            # Recover a hot journal with the service UID before any read-only
            # quiescence readers open the database after an interrupted prune.
            db.execute('BEGIN IMMEDIATE');assert_quiescent(state,'controller');value=ledger(db)
            binding={'prefix':str(prefix),'state_dir':str(state),'db_device':info.st_dev,'db_inode':info.st_ino,
                'epoch':value['epoch'],'generation':value['generation'],'head_sha256':value['head_sha256']}
            yield db,value,binding,meta
            with service_identity(meta):connection.commit()
        except BaseException:
            with service_identity(meta):connection.rollback()
            raise
        finally:
            with service_identity(meta):connection.close()


def export(prefix,output):
    with context(prefix) as (db,value,binding,meta),protected_parent(output) as (path,parent):
        if path.is_relative_to(Path(meta['state_dir'])) or (meta.get('control_filesystem') or {}).get('device')==os.fstat(parent).st_dev:
            raise DeployError('Reference archive must leave the control filesystem/state tree')
        history=retained(db);require_unbound(db,history)
        if os.path.lexists(path):
            document,sha=archive_read(path,parent)
            if document['header']['binding']!=binding:raise DeployError('Existing archive belongs to another original state/request')
        else:
            if value['receipt'] and value['receipt']['state']=='prepared':raise DeployError('Finish the original prepared reference export before selecting another file')
            if not history['references'] and not history['events']:return {'exported':False,'reason':'No retained reference history'}
            document={'header':{'format':'ascend-lab-reference-history/v1','id':'refarchive-'+uuid.uuid4().hex,
                'binding':binding,'history_sha256':hashlib.sha256(encoded(history)).hexdigest()},'history':history}
            raw=encoded(document);sha=hashlib.sha256(raw).hexdigest();name='.reference-'+uuid.uuid4().hex+'.tmp'
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            try:
                with os.fdopen(fd,'wb') as stream:os.fchmod(stream.fileno(),0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
                os.link(name,path.name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False);os.fsync(parent)
            finally:os.unlink(name,dir_fd=parent);os.fsync(parent)
        receipt={'state':'prepared','header':document['header'],'sha256':sha}
        if value['receipt'] and value['receipt']['state']=='prepared' and value['receipt']!=receipt:
            raise DeployError('Another committed reference export is pending')
        if history!=document['history']:raise DeployError('Reference history changed; preserve original archive, no rows removed')
        value['receipt']=receipt;save(db,value)
        return {'exported':True,'archive_id':document['header']['id'],'sha256':sha,
            'references':len(history['references']),'events':len(history['events']),'generation':value['generation']}


def prune(prefix,archive,confirm_sha256):
    if not isinstance(confirm_sha256,str) or not re.fullmatch('[0-9a-f]{64}',confirm_sha256):raise DeployError('Confirm the exact exported SHA256')
    with context(prefix) as (db,value,binding,meta),protected_parent(archive) as (path,parent):
        document,sha=archive_read(path,parent)
        if sha!=confirm_sha256:raise DeployError('Reference archive checksum differs from confirmation')
        receipt=value['receipt'];header=document['header']
        if not receipt or receipt.get('header')!=header or receipt.get('sha256')!=sha:raise DeployError('No matching committed reference export receipt')
        if receipt['state']=='pruned':
            if header['binding']['epoch']!=binding['epoch']:raise DeployError('Reference export belongs to another restore epoch')
            return {'pruned':True,'reused':True,'generation':value['generation'],'head_sha256':value['head_sha256']}
        if receipt['state']!='prepared' or header['binding']!=binding:raise DeployError('Reference export belongs to another database, state or restore epoch')
        history=retained(db);require_unbound(db,history)
        if history!=document['history']:raise DeployError('Retained reference history differs; no rows removed')
        generation=value['generation']+1
        head=hashlib.sha256(encoded({'previous':value['head_sha256'],'archive_sha256':sha,'generation':generation})).hexdigest()
        db.execute('DELETE FROM performance_reference_events');db.execute('DELETE FROM performance_references')
        value.update(generation=generation,head_sha256=head,receipt={**receipt,'state':'pruned'});save(db,value)
        return {'pruned':True,'reused':False,'generation':generation,'head_sha256':head,
            'references':len(history['references']),'events':len(history['events']),'jobs_or_payloads_removed':False}

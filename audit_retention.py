"""Append-preserving audit ledger and explicit offline export/confirmed pruning.

Only the authentication audit table is exported/pruned. Identity, credentials,
resource/attempt tables and specialized recovery receipts are never selected.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from jobs import JobError

CAPABILITY='audit-archive-v1'
DEFAULT={'mode':'archive_required','maximum_unarchived_rows':10000}
HOLD='审计记录等待归档或无法确认，暂停新工作；保留原任务、租约和结果'
MAX_BYTES=16*1024**2
MAX_ROWS=10000
FIELDS=['id','at','actor','action','target','outcome']


def policy(value):
    if value is None:return None
    if (not isinstance(value,dict) or set(value)!=set(DEFAULT) or value['mode']!='archive_required'
        or type(value['maximum_unarchived_rows']) is not int or not 64<=value['maximum_unarchived_rows']<=1000000):
        raise ValueError('Invalid audit archive policy')
    return dict(value)


def summary(value):
    value=policy(value)
    if value is None:
        from auth import AUDIT_ROWS
        return {'kind':'rolling_rows','maximum_rows':AUDIT_ROWS,'archive_before_drop':False}
    return {'kind':'archive_required','maximum_unarchived_rows':value['maximum_unarchived_rows'],
            'archive_before_drop':True,'automatic_drop':False}


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS audit_ledger(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch TEXT NOT NULL,next_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_exports(id TEXT PRIMARY KEY,state TEXT NOT NULL,data TEXT NOT NULL);''')
    db.execute('INSERT OR IGNORE INTO audit_ledger VALUES(1,?,(SELECT COALESCE(MAX(id),0)+1 FROM audit))',(uuid.uuid4().hex,))


def append(db,actor,action,target,outcome):
    if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
    row=db.execute('SELECT epoch,next_id FROM audit_ledger WHERE singleton=1').fetchone()
    if row is None:raise ValueError('Audit ledger is not initialized')
    identity=max(row[1],db.execute('SELECT COALESCE(MAX(id),0)+1 FROM audit').fetchone()[0])
    if type(identity) is not int or not 1<=identity<2**63-1:raise ValueError('Audit sequence exhausted')
    db.execute('INSERT INTO audit(id,at,actor,action,target,outcome) VALUES(?,?,?,?,?,?)',
        (identity,dt.datetime.now(dt.timezone.utc).isoformat(),actor,action,target,outcome))
    db.execute('UPDATE audit_ledger SET next_id=? WHERE singleton=1',(identity+1,))


def status(access):
    value=summary(access.config.get('audit_retention'))
    value.update(capability=CAPABILITY,retained_rows=None,blocks_new_work=False,status='legacy' if access.audit_policy is None else 'ready')
    if access.audit_policy is None:return value
    try:
        with access.connect() as db:
            if not db.execute('SELECT 1 FROM audit_ledger WHERE singleton=1').fetchone():raise ValueError('Missing ledger')
            count=db.execute('SELECT count(*) FROM audit').fetchone()[0]
        blocked=count>=access.audit_policy['maximum_unarchived_rows']
        value.update(retained_rows=count,blocks_new_work=blocked,status='backlog' if blocked else 'ready')
    except (OSError,sqlite3.Error,ValueError,TypeError):value.update(blocks_new_work=True,status='unavailable')
    return value


class Pressure(JobError):
    def __init__(self):super().__init__(HOLD,503)
    def response(self):return {'error':'audit_retention_backlog','message':HOLD,'retryable':True,'original_operation_required':True}


def require(access):
    if access.audit_status()['blocks_new_work']:raise Pressure()


def encoded(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()


def selected(db,limit,maximum,*,last=None):
    rows=[];size=0;digest=hashlib.sha256()
    query='SELECT '+','.join(FIELDS)+' FROM audit'+(' WHERE id<=?' if last is not None else '')+' ORDER BY id LIMIT ?'
    for row in db.execute(query,((last,limit+1) if last is not None else (limit+1,))):
        if len(rows)>=limit:
            if last is not None:raise ValueError('Archive prefix has additional rows')
            break
        value=dict(zip(FIELDS,row))
        if (type(value['id']) is not int or value['id']<=0
            or any(not isinstance(value[k],str) or len(value[k].encode())>65536 for k in FIELDS[1:])):
            raise ValueError('Audit row exceeds the bounded export schema')
        line=encoded(value)
        if size+len(line)>maximum:
            if not rows or last is not None:raise ValueError('Audit prefix exceeds export budget')
            break
        rows.append(value);size+=len(line);digest.update(line)
    return rows,digest.hexdigest(),size


@contextmanager
def protected_parent(path):
    path=Path(path).absolute()
    from deployment import DeployError
    if '..' in path.parts:raise DeployError('Audit archive path must be canonical')
    for parent in [path.parent,*path.parent.parents]:
        item=parent.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid!=0 or item.st_mode&0o022:raise DeployError('Audit archives need protected Root ancestry')
    from control_monitor import directory
    with directory(path.parent,os.geteuid()) as fd:yield path,fd


def read_archive(path,parent,expected_sha=None):
    fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    with os.fdopen(fd,'rb') as file:
        before=os.fstat(file.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or stat.S_IMODE(before.st_mode)!=0o600
            or before.st_nlink!=1 or before.st_size>MAX_BYTES+16384):raise ValueError('Unsafe audit archive')
        raw=file.read(MAX_BYTES+16385);after=os.fstat(file.fileno())
    if any(getattr(before,k)!=getattr(after,k) for k in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):
        raise ValueError('Audit archive changed during read')
    sha=hashlib.sha256(raw).hexdigest()
    if expected_sha is not None and sha!=expected_sha:raise ValueError('Audit archive checksum differs from confirmation')
    lines=raw.splitlines(keepends=True)
    if not lines or len(lines)>MAX_ROWS+1 or any(not line.endswith(b'\n') for line in lines):raise ValueError('Invalid audit archive lines')
    header=json.loads(lines[0]);rows=[json.loads(line) for line in lines[1:]]
    if not isinstance(header,dict) or header.get('format')!='ascend-lab-audit/v1' or len(rows)!=header.get('rows') or not rows:
        raise ValueError('Invalid audit archive manifest')
    if any(not isinstance(row,dict) or set(row)!=set(FIELDS) for row in rows):raise ValueError('Invalid audit row schema')
    if b''.join(encoded(row) for row in rows)!=b''.join(lines[1:]):raise ValueError('Audit rows are not canonical')
    if hashlib.sha256(b''.join(lines[1:])).hexdigest()!=header.get('rows_sha256'):raise ValueError('Audit row checksum differs')
    if header.get('first_id')!=rows[0]['id'] or header.get('last_id')!=rows[-1]['id']:raise ValueError('Audit range differs')
    if not re.fullmatch('audit-[0-9a-f]{32}',str(header.get('archive_id'))):raise ValueError('Invalid archive identity')
    return header,rows,sha


@contextmanager
def service_identity(meta):
    uid,gid=os.geteuid(),os.getegid()
    if uid==meta['uid']:
        yield;return
    try:
        os.setegid(meta['gid']);os.seteuid(meta['uid'])
        yield
    finally:os.seteuid(uid);os.setegid(gid)


class Cursor:
    def __init__(self,cursor,meta):self.cursor=cursor;self.meta=meta
    @property
    def rowcount(self):return self.cursor.rowcount
    def fetchone(self):
        with service_identity(self.meta):return self.cursor.fetchone()
    def __iter__(self):return self
    def __next__(self):
        with service_identity(self.meta):return next(self.cursor)


class Database:
    def __init__(self,connection,meta):self.connection=connection;self.meta=meta
    @property
    def in_transaction(self):return self.connection.in_transaction
    def execute(self,*args):
        with service_identity(self.meta):return Cursor(self.connection.execute(*args),self.meta)


@contextmanager
def context(prefix):
    from deployment import require_root,verify,DeployError
    from control_process import offline
    from state_lock import state_lock
    require_root();prefix,meta,release=verify(prefix)
    if meta['role']!='controller':raise DeployError('Audit maintenance belongs to a controller')
    if hashlib.sha256((release/Path(__file__).name).read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():
        raise DeployError('Use matching installed audit maintenance code')
    state=Path(meta['state_dir'])
    if meta.get('control_filesystem'):
        # SQLite canonicalizes FD aliases: pin the entire maintenance view too.
        from control_process import namespaces,context as filesystem_context
        namespaces();filesystem_context(meta).check()
    with state_lock(state,exclusive=True),offline(prefix,meta):
        from control_monitor import directory,configuration
        with directory(state,meta['uid']) as fd:
            config=configuration(fd,meta['uid'])
            if policy(config.get('audit_retention')) is None:raise DeployError('Audit archive mode must be explicitly enabled; legacy policy is unchanged')
            check=os.open('identities.sqlite3',os.O_PATH|os.O_NOFOLLOW,dir_fd=fd)
            try:
                item=os.fstat(check)
                from storage_files import mount
                if (not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or item.st_uid!=meta['uid']
                    or item.st_dev!=os.fstat(fd).st_dev or mount(check)!=mount(fd)):raise DeployError('Unsafe audit database')
            finally:os.close(check)
        # Every VFS operation uses the service identity, including commit and
        # rollback. A crash must not leave a Root-owned hot journal/WAL behind.
        with service_identity(meta):connection=sqlite3.connect(state/'identities.sqlite3',timeout=5)
        db=Database(connection,meta)
        try:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT epoch,next_id FROM audit_ledger WHERE singleton=1').fetchone()
            if row is None:raise DeployError('Start matching archive-aware code once to initialize the ledger')
            binding={'prefix':str(prefix),'state_dir':str(state),'db_device':item.st_dev,'db_inode':item.st_ino,'epoch':row[0]}
            yield db,binding,meta
            with service_identity(meta):connection.commit()
        except BaseException:
            with service_identity(meta):connection.rollback()
            raise
        finally:
            with service_identity(meta):connection.close()


def export(prefix,output,max_rows=MAX_ROWS,max_bytes=MAX_BYTES):
    if type(max_rows) is not int or not 1<=max_rows<=MAX_ROWS or type(max_bytes) is not int or not 1024<=max_bytes<=MAX_BYTES:
        raise ValueError('Invalid audit export budget')
    with context(prefix) as (db,binding,meta),protected_parent(output) as (path,parent):
        if path.is_relative_to(Path(meta['state_dir'])) or (meta.get('control_filesystem') or {}).get('device')==os.fstat(parent).st_dev:
            raise ValueError('Audit export must leave the control filesystem/state tree')
        existing=os.stat(path.name,dir_fd=parent,follow_symlinks=False) if os.path.lexists(path) else None
        if existing:
            header,rows,sha=read_archive(path,parent)
            if header.get('binding')!=binding or header.get('max_rows')!=max_rows or header.get('max_bytes')!=max_bytes:
                raise ValueError('Existing export belongs to another original request or state')
        else:
            rows,row_sha,size=selected(db,max_rows,max_bytes)
            if not rows:return {'exported':False,'rows':0,'reason':'No retained audit rows'}
            header={'format':'ascend-lab-audit/v1','archive_id':'audit-'+uuid.uuid4().hex,'binding':binding,
                'max_rows':max_rows,'max_bytes':max_bytes,'rows':len(rows),'first_id':rows[0]['id'],'last_id':rows[-1]['id'],'rows_sha256':row_sha}
            data=encoded(header)+b''.join(encoded(row) for row in rows);sha=hashlib.sha256(data).hexdigest()
            name='.audit-'+uuid.uuid4().hex+'.tmp';fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            try:
                with os.fdopen(fd,'wb') as file:os.fchmod(file.fileno(),0o600);file.write(data);file.flush();os.fsync(file.fileno())
                os.link(name,path.name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False);os.fsync(parent)
            finally:os.unlink(name,dir_fd=parent);os.fsync(parent)
        record={'header':header,'sha256':sha}
        saved=db.execute('SELECT state,data FROM audit_exports WHERE id=?',(header['archive_id'],)).fetchone()
        if saved:
            if json.loads(saved[1])!=record:raise ValueError('Audit export receipt conflicts')
            return {'exported':True,'archive_id':header['archive_id'],'sha256':sha,'rows':header['rows'],'reused':True,'state':saved[0]}
        actual,actual_sha,size=selected(db,max_rows,max_bytes,last=header['last_id'])
        if actual!=rows or actual_sha!=header['rows_sha256']:raise ValueError('Original audit prefix changed; preserve archive for inspection')
        db.execute('INSERT INTO audit_exports VALUES(?,?,?)',(header['archive_id'],'prepared',json.dumps(record,sort_keys=True)))
        append(db,'root','audit.export',header['archive_id'],'prepared')
        return {'exported':True,'archive_id':header['archive_id'],'sha256':sha,'rows':len(rows),'reused':False,'state':'prepared'}


def prune(prefix,archive,confirm_sha256):
    if not isinstance(confirm_sha256,str) or not re.fullmatch('[0-9a-f]{64}',confirm_sha256):raise ValueError('Confirm the exact exported SHA256')
    with context(prefix) as (db,binding,meta),protected_parent(archive) as (path,parent):
        header,rows,sha=read_archive(path,parent,confirm_sha256)
        if header.get('binding')!=binding:raise ValueError('Archive is bound to another database, state or restore epoch')
        saved=db.execute('SELECT state,data FROM audit_exports WHERE id=?',(header['archive_id'],)).fetchone()
        record={'header':header,'sha256':sha}
        if not saved or json.loads(saved[1])!=record:raise ValueError('No matching committed export receipt')
        if saved[0]=='pruned':return {'pruned_rows':header['rows'],'archive_id':header['archive_id'],'reused':True}
        if saved[0]!='prepared':raise ValueError('Audit export is not prune eligible')
        actual,actual_sha,size=selected(db,header['max_rows'],header['max_bytes'],last=header['last_id'])
        if actual!=rows or actual_sha!=header['rows_sha256']:raise ValueError('Retained audit prefix differs; no rows removed')
        count=db.execute('DELETE FROM audit WHERE id<=?',(header['last_id'],)).rowcount
        if count!=len(rows):raise ValueError('Audit prefix changed during prune')
        db.execute('UPDATE audit_exports SET state=? WHERE id=?',('pruned',header['archive_id']))
        append(db,'root','audit.prune',header['archive_id'],'confirmed')
        return {'pruned_rows':count,'archive_id':header['archive_id'],'reused':False}


def rebind_restored(path,config):
    if policy(config.get('audit_retention')) is None:return
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_ledger'").fetchone():
            raise ValueError('Archive-aware snapshot has no audit ledger')
        db.execute('UPDATE audit_ledger SET epoch=? WHERE singleton=1',(uuid.uuid4().hex,))
        db.execute("UPDATE audit_exports SET state='invalidated'")

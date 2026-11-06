"""Transactional, owner-private event outbox. No delivery in scheduler threads."""
import datetime as dt
import hashlib
import http.client
import json
import ssl
import time
import uuid
import sqlite3
from pathlib import Path
import os
import stat
from urllib.parse import urlsplit

DEFAULT = {'enabled': False, 'max_pending': 1024}
STATES = {'succeeded', 'failed', 'cancelled', 'timed_out', 'needs_attention'}


def policy(value=None):
    if value is None:
        return dict(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Invalid notification policy')
    result = {**DEFAULT, **value}
    if type(result['enabled']) is not bool or type(result['max_pending']) is not int or not 64 <= result['max_pending'] <= 4096:
        raise ValueError('Invalid notification outbox limit')
    return result


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS notification_outbox(
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
        payload TEXT NOT NULL, delivered REAL, attempts INTEGER NOT NULL DEFAULT 0,
        due REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT, destination_sha TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS notification_owner ON notification_outbox(owner,created)')
    db.execute('''CREATE TABLE IF NOT EXISTS notification_overflow(
        owner TEXT PRIMARY KEY, dropped INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS notification_signals(
        owner TEXT NOT NULL,subject TEXT NOT NULL,event TEXT NOT NULL,detail TEXT NOT NULL,
        PRIMARY KEY(owner,subject,event))''')
    db.execute('''CREATE TABLE IF NOT EXISTS notification_archive_checkpoint(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),record TEXT NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO notification_archive_checkpoint VALUES(1,?)',
               (json.dumps({'epoch':uuid.uuid4().hex,'generation':0,'head':'0'*64},sort_keys=True),))


def append(db, owner, event, subject, detail, settings, *, epoch=None):
    settings = policy(settings)
    if not settings['enabled']:
        return None
    if event not in {'job_state', 'resource_pressure', 'storage_pressure', 'node_unavailable'}:
        raise ValueError('Unregistered notification event')
    # Fixed vocabulary; never include commands, model messages, paths or logs.
    if not isinstance(owner, str) or not 1 <= len(owner) <= 64 or not isinstance(subject, str) or len(subject) > 80:
        raise ValueError('Invalid notification identity')
    if not isinstance(detail, str) or len(detail) > 80 or any(ord(c) < 32 for c in detail):
        raise ValueError('Invalid notification detail')
    count = db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0]
    if count >= settings['max_pending']:
        db.execute('''INSERT INTO notification_overflow VALUES(?,1)
            ON CONFLICT(owner) DO UPDATE SET dropped=dropped+1''', (owner,))
        return None
    ident = 'event-' + uuid.uuid4().hex
    at = time.time() if epoch is None else epoch
    payload = {'schema': 'ascend-lab-notification/v1', 'id': ident,
               'owner': owner, 'event': event, 'subject': subject,
               'detail': detail, 'at': dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()}
    db.execute('INSERT INTO notification_outbox(id,owner,created,payload) VALUES(?,?,?,?)',
               (ident, owner, at, json.dumps(payload, sort_keys=True)))
    return ident


def job_event(db, previous, job, settings):
    if previous != job['state'] and job['state'] in STATES:
        return append(db, job['owner'], 'job_state', job['id'], job['state'], settings)


def observe(db, owner, event, subject, detail, settings):
    """Record only transitions; 'ready' is recovery, 'unknown' is not healthy."""
    if not policy(settings)['enabled']:
        return
    previous = db.execute('SELECT detail FROM notification_signals WHERE owner=? AND subject=? AND event=?',
                          (owner, subject, event)).fetchone()
    if previous and previous[0] == detail:
        return
    if not previous and db.execute('SELECT count(*) FROM notification_signals').fetchone()[0] >= 1024:
        return  # Bounded signal registry; outbox overflow remains independently visible.
    if previous is not None or detail != 'ready':
        append(db, owner, event, subject, detail, settings)
    db.execute('''INSERT INTO notification_signals VALUES(?,?,?,?)
        ON CONFLICT(owner,subject,event) DO UPDATE SET detail=excluded.detail''', (owner, subject, event, detail))


def health_once(db, owner, settings, pressure_config, *, epoch=None, storage=None, after_node=''):
    """At most32 enrolled nodes per invocation; use explicit pagination in ops."""
    epoch = time.time() if epoch is None else epoch
    from resource_pressure import reason,policy as pressure_policy
    monitoring=pressure_policy(pressure_config);monitoring['enabled']=True
    last = None
    for ident, seen, raw in db.execute('SELECT id,last_seen,report FROM worker_nodes WHERE disabled=0 AND id>? ORDER BY id LIMIT 32', (after_node,)):
        last = ident
        report = json.loads(raw)
        observe(db, owner, 'node_unavailable', ident, 'ready' if 0 <= epoch - seen <= 30 else 'heartbeat_stale', settings)
        reading = report.get('resource_pressure')
        blocked = reason(reading, 0, monitoring, epoch=epoch)
        observe(db, owner, 'resource_pressure', ident, 'pressure_or_observation_blocked' if blocked else 'ready', settings)
    if storage is not None:
        observe(db, owner, 'storage_pressure', 'controller', storage, settings)
    return last


def outbox_status(db):
    row=db.execute('''SELECT count(*),coalesce(sum(delivered IS NULL),0),
        coalesce(sum(delivered IS NULL AND attempts>=8),0) FROM notification_outbox''').fetchone()
    dropped=db.execute('SELECT coalesce(sum(dropped),0) FROM notification_overflow').fetchone()[0]
    return {'retained':row[0],'pending':row[1],'retry_exhausted':row[2],'dropped_events':dropped}


def page(db, owner, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid notification page limit')
    rows = db.execute('''SELECT payload,delivered,attempts FROM notification_outbox
        WHERE owner=? ORDER BY created DESC,id DESC LIMIT ?''', (owner, limit)).fetchall()
    overflow = db.execute('SELECT dropped FROM notification_overflow WHERE owner=?', (owner,)).fetchone()
    return {'events': [{**json.loads(row[0]), 'delivered': row[1] is not None,
                        'delivery_attempts': row[2]} for row in rows],
            'dropped_events': overflow[0] if overflow else 0}


def destination(value):
    if not isinstance(value, dict) or set(value) != {'url'}:
        raise ValueError('A notification destination requires one explicit HTTPS URL')
    url = value['url']
    if not isinstance(url, str) or len(url) > 1024 or any(ord(c) < 33 for c in url):
        raise ValueError('Invalid notification URL')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Notification delivery requires HTTPS without embedded credentials or redirects')
    if parsed.port not in {None, 443}:
        raise ValueError('Notification destination must use port 443')
    return parsed


def deliver(url, payload):
    parsed = destination({'url': url})
    # No ambient proxies, redirects, shell or user-selected destination from HTTP.
    connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=2,
                                             context=ssl.create_default_context())
    try:
        body = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        route=(parsed.path or '/')+('?' + parsed.query if parsed.query else '')
        connection.request('POST', route,
                           body, {'Content-Type': 'application/json', 'Idempotency-Key': payload['id']})
        response = connection.getresponse()
        response.read(1025)
        if not 200 <= response.status < 300:
            raise RuntimeError('Notification destination did not acknowledge delivery')
    finally:
        connection.close()


def dispatch_one(db, destinations, *, transport=deliver, epoch=None):
    """One event per invocation; at-least-once delivery with stable event ID.

    Call with a dedicated connection. Never hold a SQLite transaction during IO.
    An interrupted lease is retried after 30 seconds; receivers must deduplicate.
    """
    epoch = time.time() if epoch is None else epoch
    if db.in_transaction:
        raise ValueError('Notification dispatcher needs its own idle connection')
    if not isinstance(destinations, dict) or len(destinations) > 128:
        raise ValueError('Invalid notification destinations')
    for value in destinations.values():
        destination(value)
    if not destinations:
        return {'status': 'no_destinations'}
    token = uuid.uuid4().hex
    with db:
        db.execute('BEGIN IMMEDIATE')
        names = sorted(destinations)
        selected = db.execute('''SELECT id,owner,payload,destination_sha FROM notification_outbox
            WHERE delivered IS NULL AND attempts<8 AND due<=? AND lease_until<=? AND owner IN ('''
            + ','.join('?' for _ in names) + ') ORDER BY created,id LIMIT 1', (epoch, epoch, *names)).fetchone()
        if not selected:
            return {'status': 'idle'}
        ident, owner, raw, previous = selected
        target = destinations.get(owner)
        if target is None:
            return {'status': 'destination_missing', 'event_id': ident}
        fingerprint = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()
        if previous is not None and previous != fingerprint:
            return {'status': 'destination_changed', 'event_id': ident}
        db.execute('''UPDATE notification_outbox SET lease_until=?,lease_token=?,
            destination_sha=?,attempts=attempts+1 WHERE id=?''', (epoch + 30, token, fingerprint, ident))
    success = False
    try:
        transport(target['url'], json.loads(raw))
        success = True
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        pass  # Do not expose URL, remote response or credential query strings.
    with db:
        db.execute('''UPDATE notification_outbox SET delivered=?,due=?,lease_until=0,lease_token=NULL
            WHERE id=? AND lease_token=?''', (epoch if success else None, epoch + 60, ident, token))
    return {'status': 'delivered' if success else 'retry_later', 'event_id': ident}


def table_digest(state, table):
    """Stream bounded-width rows; no table creation or whole-history list."""
    if table not in {'notification_outbox','notification_overflow','notification_signals','notification_archive_checkpoint','debug_inputs'}:
        raise ValueError('Unregistered operational history table')
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return None
    with sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone():return None
        digest=hashlib.sha256();count=0
        # Table and order identifiers are from this fixed allowlist only.
        order={'notification_outbox':'id','notification_overflow':'owner',
               'notification_signals':'owner,subject,event','notification_archive_checkpoint':'singleton','debug_inputs':'job_id,seq'}[table]
        for row in db.execute('SELECT * FROM '+table+' ORDER BY '+order):
            raw=json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            if len(raw)>16384:raise ValueError('Operational history row exceeds its bound')
            digest.update(len(raw).to_bytes(8,'big'));digest.update(raw);count+=1
        return (count,digest.hexdigest()) if count else None


def require_history(current, staged):
    for table in ['notification_outbox','notification_overflow','notification_signals','notification_archive_checkpoint']:
        old=table_digest(current,table)
        if old is not None and old!=table_digest(staged,table):
            raise ValueError('Restore would rewind notification history, delivery or overflow evidence')


def require_compatible(state, role, manifest):
    if role=='controller' and any(table_digest(state,table) is not None for table in ['notification_outbox','notification_overflow','notification_signals','notification_archive_checkpoint']):
        if 'notifications.py' not in manifest.get('files',{}):
            raise ValueError('Retained notification history requires compatible code')


def archive_delivered(db, directory, *, apply=False):
    """Seal <=32 delivered events before removing exactly those outbox rows.

    No pending/error events are discarded. Archive count/size is finite; when
    full, explicit external archive maintenance is required. No transaction is
    held while writing or verifying archive bytes.
    """
    if db.in_transaction:
        raise ValueError('Archive requires its own idle database connection')
    fields='id,owner,created,payload,delivered,attempts,due,lease_until,lease_token,destination_sha'
    rows=db.execute('SELECT '+fields+' FROM notification_outbox WHERE delivered IS NOT NULL ORDER BY created,id LIMIT 32').fetchall()
    saved=db.execute('SELECT record FROM notification_archive_checkpoint WHERE singleton=1').fetchone()
    if not saved:raise ValueError('Notification archive checkpoint is missing')
    previous=json.loads(saved[0])
    if not rows or not apply:
        return {'status':'preview' if not apply else 'idle','rows':len(rows),'writes':False}
    directory=Path(directory)
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise ValueError('Notification archives need an existing private owned directory')
    with os.scandir(directory) as entries:
        count=0
        for entry in entries:
            count+=1
            if count>=256:raise ValueError('Notification archive directory needs explicit rotation')
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith('.json'):
                raise ValueError('Unexpected object in notification archive directory')
    value={'schema':'ascend-lab-notification-archive/v1','previous':previous,'rows':rows}
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>65536:raise ValueError('Notification archive exceeds64KiB')
    digest=hashlib.sha256(raw).hexdigest();name=digest+'.json'
    parent=os.open(directory,os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        try:
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        except FileExistsError:
            fd=None
        if fd is not None:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.fsync(parent)
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            item=os.fstat(stream.fileno())
            if not stat.S_ISREG(item.st_mode) or item.st_uid!=os.geteuid() or item.st_mode&0o077 or item.st_nlink!=1 or item.st_size!=len(raw) or stream.read(65537)!=raw:
                raise ValueError('Notification archive did not verify; all rows retained')
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT record FROM notification_archive_checkpoint WHERE singleton=1').fetchone()[0]!=saved[0]:
                raise ValueError('Notification archive checkpoint changed; retain the original file')
            for row in rows:
                actual=db.execute('SELECT '+fields+' FROM notification_outbox WHERE id=?',(row[0],)).fetchone()
                if actual!=row:raise ValueError('Original delivered event changed; no rows removed')
            for row in rows:db.execute('DELETE FROM notification_outbox WHERE id=?',(row[0],))
            next_value={**previous,'generation':previous['generation']+1,'head':digest}
            db.execute('UPDATE notification_archive_checkpoint SET record=? WHERE singleton=1',(json.dumps(next_value,sort_keys=True),))
        return {'status':'archived','rows':len(rows),'sha256':digest,'generation':next_value['generation']}
    finally:
        os.close(parent)

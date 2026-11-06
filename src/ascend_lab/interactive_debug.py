"""Owner-only stdin for one explicitly scheduled, bounded CPU debug task."""
import base64
import hashlib
import http.client
import json
import re
from pathlib import Path

CAPABILITY = 'bounded-debug-stdin-v1'
TEMPLATE = 'debug_v1'
MAX_FRAMES = 64
MAX_INPUT = 65536


def policy(value=None):
    if value is None:
        return {'enabled': False, 'owners': []}
    if not isinstance(value, dict) or set(value) != {'enabled', 'owners'} or type(value['enabled']) is not bool:
        raise ValueError('Invalid interactive debug policy')
    if not isinstance(value['owners'], list) or len(value['owners']) > 100 or any(
        not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', owner) for owner in value['owners']) or len(set(value['owners'])) != len(value['owners']):
        raise ValueError('Debug owners must be explicitly approved')
    return value


def frame(request):
    if not isinstance(request, dict) or set(request) != {'key', 'data'}:
        raise ValueError('Debug input requires a stable key and base64 data')
    if not isinstance(request['key'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request['key']):
        raise ValueError('Invalid debug input key')
    if not isinstance(request['data'], str) or len(request['data']) > 5500:
        raise ValueError('Debug frame is too large')
    try:
        raw = base64.b64decode(request['data'], validate=True)
    except (ValueError, TypeError):
        raise ValueError('Invalid debug frame encoding') from None
    if not 1 <= len(raw) <= 4096:
        raise ValueError('Debug input must contain 1..4096 bytes')
    return request['key'], raw


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS debug_inputs(
        job_id TEXT NOT NULL, seq INTEGER NOT NULL, request_key TEXT NOT NULL,
        sha256 TEXT NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL,
        PRIMARY KEY(job_id,seq),UNIQUE(job_id,request_key))''')


def require(jobs, job, principal, *, writing=True):
    from jobs import JobError
    principal.require('jobs:read')
    principal.require('jobs:submit')
    if job['owner'] != principal.id or job.get('template') != TEMPLATE or principal.experiment_id is not None:
        raise JobError('Debug session not found', 404)
    settings = policy(jobs.policy.get('interactive_debug'))
    if writing and (not settings['enabled'] or principal.id not in settings['owners']):
        raise JobError('Interactive debugging is not enabled for this owner', 403)


def submit(jobs, principal, job_id, request):
    from jobs import JobError
    key, raw = frame(request)
    sha = hashlib.sha256(raw).hexdigest()
    with jobs.transaction() as db:
        job = jobs.get(job_id, db, principal)
        require(jobs, job, principal)
        initialize(db)
        old = db.execute('SELECT seq,sha256,state FROM debug_inputs WHERE job_id=? AND request_key=?', (job_id, key)).fetchone()
        if old:
            if old[1] != sha:
                raise JobError('Original debug input key has different bytes', 409)
            return {'seq': old[0], 'state': old[2], 'reused': True}
        if job['state'] != 'running' or job.get('_cancel_requested'):
            raise JobError('Debug task is not running; no input was queued', 409)
        if job.get('_debug_capable') is not True:
            raise JobError('Node does not support the debug transport', 409)
        if db.execute("SELECT 1 FROM debug_inputs WHERE job_id=? AND state='delivery_unknown'", (job_id,)).fetchone():
            raise JobError('Previous input delivery is unknown; inspect logs or close this session', 409)
        count, size = db.execute('SELECT count(*),coalesce(sum(length(data)),0) FROM debug_inputs WHERE job_id=?', (job_id,)).fetchone()
        # Base64 accounting deliberately overestimates actual input bytes.
        if count >= MAX_FRAMES or size + len(request['data']) > MAX_INPUT:
            raise JobError('Debug session input budget is exhausted', 409)
        seq = count + 1
        db.execute('INSERT INTO debug_inputs VALUES(?,?,?,?,?,?)', (job_id, seq, key, sha, request['data'], 'queued'))
        return {'seq': seq, 'state': 'queued', 'reused': False}


def status(jobs, principal, job_id):
    with jobs.connect() as db:
        job = jobs.get(job_id, db, principal)
        require(jobs, job, principal, writing=False)
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='debug_inputs'").fetchone()
        rows = db.execute('SELECT seq,state FROM debug_inputs WHERE job_id=? ORDER BY seq', (job_id,)).fetchall() if exists else []
        return {'job_id': job_id, 'state': job['state'], 'inputs': [{'seq': seq, 'state': state} for seq, state in rows],
                'transport': 'line-oriented PTY; stdout/stderr via original authorized job logs',
                'idle_timeout_seconds': 30, 'maximum_seconds': job['timeout_seconds']}


def exchange(db, job, body):
    """Called inside the authenticated attempt's normal update transaction."""
    if job.get('template') != TEMPLATE:
        if body.get('debug_ack') is not None:
            raise ValueError('Debug acknowledgement belongs to another template')
        return {}
    initialize(db)
    ack = body.get('debug_ack')
    if ack is not None:
        if not isinstance(ack, dict) or set(ack) != {'seq', 'state'} or type(ack['seq']) is not int or not 1<=ack['seq']<=MAX_FRAMES or ack['state'] not in {'stdin_sent', 'delivery_unknown'}:
            raise ValueError('Invalid debug delivery acknowledgement')
        previous=db.execute('SELECT state FROM debug_inputs WHERE job_id=? AND seq=?',(job['id'],ack['seq'])).fetchone()
        if previous is None or previous[0] not in {'queued',ack['state']}:
            raise ValueError('Debug acknowledgement does not match the original input state')
        db.execute("UPDATE debug_inputs SET state=? WHERE job_id=? AND seq=? AND state='queued'", (ack['state'], job['id'], ack['seq']))
    if job.get('_cancel_requested') or db.execute("SELECT 1 FROM debug_inputs WHERE job_id=? AND state='delivery_unknown'", (job['id'],)).fetchone():
        return {}
    row = db.execute("SELECT seq,data FROM debug_inputs WHERE job_id=? AND state='queued' ORDER BY seq LIMIT 1", (job['id'],)).fetchone()
    return {'debug_input': {'seq': row[0], 'data': row[1]}} if row else {}


def send_stdin(engine, value):
    """Attach only to this already-running container; never docker exec.

    The caller journals intent first and must never resend an ambiguous frame.
    No arbitrary container name, UID, image or Docker endpoint is accepted.
    """
    from docker_runtime import discover, UnixHTTP, endpoint
    binding = engine.binding()
    discover(binding)
    info = engine.inspect()
    if not info or not info['State']['Running'] or engine.grant['template'] != TEMPLATE:
        raise RuntimeError('Original debug container is not running')
    uid = engine.config['users'][engine.grant['owner']]['uid']
    if uid < 1000 or engine.grant['resources']['npu'] or not info['Config'].get('OpenStdin'):
        raise RuntimeError('Debug container policy differs')
    connection = UnixHTTP(endpoint(binding['endpoint'])[7:])
    connection.timeout = 2
    try:
        route = '/containers/' + info['Id'] + '/attach?stream=1&stdin=1&stdout=0&stderr=0'
        connection.request('POST', route, body=b'', headers={'Connection': 'Upgrade', 'Upgrade': 'tcp'})
        response = connection.getresponse()
        if response.status != 101 or connection.sock is None:
            raise RuntimeError('Docker did not upgrade the original stdin transport')
        connection.sock.sendall((json.dumps(value, separators=(',', ':')) + '\n').encode())
    finally:
        connection.close()


def node_input(engine, value, *, transport=send_stdin):
    if not isinstance(value, dict) or set(value) != {'seq', 'data'} or type(value['seq']) is not int or not 1 <= value['seq'] <= MAX_FRAMES:
        raise ValueError('Invalid assigned debug input')
    frame({'key': str(value['seq']), 'data': value['data']})
    saved = engine.state.get('debug_delivery')
    fingerprint = hashlib.sha256(value['data'].encode()).hexdigest()
    if saved and value['seq'] <= saved['seq']:
        if value['seq'] != saved['seq'] or saved['sha256'] != fingerprint:
            raise ValueError('Debug delivery order or content changed')
        return {'seq': saved['seq'], 'state': saved['state']}
    if saved and (saved['state'] == 'delivery_unknown' or value['seq'] != saved['seq'] + 1) or not saved and value['seq'] != 1:
        raise ValueError('Debug delivery sequence is not continuous')
    engine.state['debug_delivery'] = {'seq': value['seq'], 'sha256': fingerprint, 'state': 'delivery_unknown'}
    engine.save()  # Recovery never retries bytes that may have reached a shell.
    try:
        transport(engine, value)
    except (OSError, RuntimeError, http.client.HTTPException):
        return {'seq': value['seq'], 'state': 'delivery_unknown'}
    engine.state['debug_delivery']['state'] = 'stdin_sent'
    engine.save()
    return {'seq': value['seq'], 'state': 'stdin_sent'}


def require_history(current, staged):
    from notifications import table_digest
    old=table_digest(current,'debug_inputs')
    if old is not None and old!=table_digest(staged,'debug_inputs'):
        raise ValueError('Restore would lose original debug inputs or delivery uncertainty')


def require_compatible(state, role, manifest):
    if role!='controller':return
    from notifications import table_digest
    if table_digest(state,'debug_inputs') is not None and 'interactive_debug.py' not in manifest.get('files',{}):
        raise ValueError('Retained debug delivery history requires compatible code')

"""Opt-in finite control history, online archive slots and exact request replay.

Only the offline administrator changes the policy. Online slots are a durable
projection of confirmed lifecycle/retirement receipts, never directory absence.
Original jobs, idempotency and result rows are retained at either limit.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

FORMAT='ascend-lab-history-capacity/v1'
MAX_JOBS=10000
MAX_CHANGES=128
STATES=('queued','reserved','running','cancelling','needs_attention','succeeded','failed','cancelled','timed_out')
# Index the checked JSON state, including a sentinel for inconsistent records.
# Querying only the scalar state column could hide a still-running JSON record.
EXPR="CASE WHEN json_valid(data) AND json_extract(data,'$.id')=id AND json_extract(data,'$.owner')=owner AND json_extract(data,'$.state')=state AND state IN ('"+"','".join(STATES)+"') THEN state ELSE '__invalid__' END"


def error(message):
    from jobs import JobError
    return JobError(message,409)


def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
def sha(value):return hashlib.sha256(encode(value)).hexdigest()
def table(db,name):return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(name,)).fetchone())


def limits(control,online):
    if type(control) is not int or type(online) is not int or not 1<=online<=control<=MAX_JOBS:
        raise error('History limits require 1 <= online archives <= control jobs <= 10000')
    return {'max_control_jobs':control,'max_online_archives':online}


def current(db):
    if not table(db,'history_capacity_policy'):return None
    rows=list(db.execute('SELECT id,record FROM history_capacity_policy LIMIT 2'))
    if not rows:return None
    if len(rows)!=1 or rows[0][0]!=1 or len(rows[0][1])>4096:raise error('Invalid history capacity policy')
    value=json.loads(rows[0][1])
    if (not isinstance(value,dict) or set(value)!={'format','revision','approval_id','max_control_jobs','max_online_archives'}
        or value['format']!=FORMAT or type(value['revision']) is not int or not 1<=value['revision']<=MAX_CHANGES
        or not re.fullmatch('[0-9a-f]{64}',str(value['approval_id']))):raise error('Invalid history capacity policy')
    limits(value['max_control_jobs'],value['max_online_archives']);return value


def decode(row):
    identifier,owner,state,raw=row
    if len(raw.encode())>32*1024**2:raise error('Job metadata exceeds history audit bound')
    value=json.loads(raw)
    if not isinstance(value,dict) or (value.get('id'),value.get('owner'),value.get('state'))!=(identifier,owner,state) or state not in STATES:
        raise error('Job columns and retained metadata disagree; reconcile before continuing')
    return value


def online_proof(db,job):
    """No cold filesystem read: the atomic confirmed removal receipt is proof.

    A temporarily missing cold mount must not turn already-removed warm bytes
    into new online occupancy. Archive readers still enforce cold availability.
    """
    from jobs import TERMINAL
    from cold_reads import record,semantic,selected
    proof={'kind':'online','job_id':job['id'],'owner':job['owner']}
    reader=record(db,job['id'])
    status=job.get('archive_status',{}).get('state','available')
    if reader and status!='available':raise error('Cold authority and archive lifecycle disagree')
    if reader and reader[1]['hot_state']=='retired':
        r=reader[1];selected(db,r)
        row=db.execute('SELECT job_id,record FROM cold_reader_changes WHERE id=?',(r['retire_operation'],)).fetchone()
        if row is None:raise error('Confirmed cold retirement receipt is missing')
        change=json.loads(row[1]);plan=change.get('plan',{})
        from cold_retention import check
        check(plan)
        if (job['state'] not in TERMINAL or r['owner']!=job['owner'] or r['job_semantic_sha256']!=semantic(job)
            or row[0]!=job['id'] or change.get('phase')!='applied' or plan.get('action')!='retire'
            or plan.get('job_id')!=job['id'] or plan.get('owner')!=job['owner'] or plan.get('id')!=r['retire_operation'] or plan.get('copy_id')!=r['copy_id']
            or plan.get('copy_plan_sha256')!=r['copy_plan_sha256'] or plan.get('target_inventory_sha256')!=r['target_inventory_sha256']):
            raise error('Cold retirement does not confirm this original terminal job')
        proof.update(kind='cold_retired',operation=r['retire_operation'],copy_id=r['copy_id'],
            semantic_sha256=r['job_semantic_sha256'],retained_bytes=r['retained_control_bytes'])
    elif status=='purged':
        row=db.execute('SELECT owner,state,revision,record FROM storage_lifecycle WHERE kind="archive" AND id=?',(job['id'],)).fetchone()
        if row is None:raise error('Purged archive lacks its confirmed lifecycle receipt')
        value=json.loads(row[3])
        if (job['state'] not in TERMINAL or row[:3]!=(job['owner'],'purged',value.get('revision'))
            or value.get('state')!='purged' or value.get('kind')!='archive' or value.get('id')!=job['id'] or value.get('owner')!=job['owner']
            or value.get('inventory') is not None or value.get('completed_at') is None
            or job['archive_status'].get('revision')!=value['revision']
            or job['archive_status'].get('metadata_bytes')!=value.get('metadata_bytes')):
            raise error('Archive lifecycle does not confirm completed removal')
        proof.update(kind='purged',revision=value['revision'],retained_bytes=value['metadata_bytes'])
    if proof['kind']!='online':
        allocation=db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind="archive" AND id=?',(job['id'],)).fetchone()
        if allocation!=(job['owner'],proof['retained_bytes'],1):raise error('Removed archive retains inconsistent control billing')
    return proof


def initialize(db):
    # JSON1 is required only by this explicitly enabled mode.
    if db.execute("SELECT json_extract('{\"ready\":1}','$.ready')").fetchone()[0]!=1:raise error('SQLite JSON support is required')
    db.execute('CREATE TABLE IF NOT EXISTS history_capacity_policy(id INTEGER PRIMARY KEY CHECK(id=1),record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS history_capacity_changes(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS history_capacity_slots(job_id TEXT PRIMARY KEY,online INTEGER NOT NULL CHECK(online IN (0,1)),proof TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS history_capacity_online ON history_capacity_slots(online)')
    db.execute('CREATE INDEX IF NOT EXISTS history_capacity_working ON jobs('+EXPR+')')
    db.execute("CREATE INDEX IF NOT EXISTS history_capacity_fairness ON jobs(owner,json_extract(data,'$.reserved_at'))")


def snapshot(db,*,validate_slots=True):
    policy=current(db);values=[];total=0
    for row in db.execute('SELECT id,owner,state,data FROM jobs ORDER BY rowid LIMIT ?',(MAX_JOBS+1,)):
        total+=len(row[3].encode())
        if len(values)>=MAX_JOBS or total>256*1024**2:raise error('Control history audit exceeds finite metadata budget')
        job=decode(row);proof=online_proof(db,job)
        values.append((job['id'],int(proof['kind']=='online'),encode(proof).decode()))
    if policy and validate_slots:
        saved=list(db.execute('SELECT job_id,online,proof FROM history_capacity_slots ORDER BY rowid LIMIT ?',(MAX_JOBS+1,)))
        if sorted(saved)!=sorted(values):raise error('Online archive projection differs from confirmed retained history')
    history_sha=hashlib.sha256()
    for row in db.execute('SELECT id,owner,idem_key,fingerprint,state,data FROM jobs ORDER BY rowid'):
        raw=encode(tuple(row));history_sha.update(len(raw).to_bytes(8,'big'));history_sha.update(raw)
    approvals=[]
    if table(db,'history_capacity_changes'):
        approvals=list(db.execute('SELECT id,record FROM history_capacity_changes ORDER BY rowid LIMIT ?',(MAX_CHANGES+1,)))
        if len(approvals)>MAX_CHANGES:raise error('History capacity approval history is full')
    return {'policy':policy,'jobs_sha256':history_sha.hexdigest(),'slots_sha256':sha(sorted(values)),
        'approvals_sha256':sha(approvals),'control_jobs':len(values),'online_archives':sum(v[1] for v in values)},values


def release(db,job):
    """Called in the same transaction that commits confirmed removal/credit."""
    if current(db) is None:return
    proof=online_proof(db,job)
    if proof['kind']=='online':raise error('An unconfirmed archive cannot release its online slot')
    old=db.execute('SELECT online,proof FROM history_capacity_slots WHERE job_id=?',(job['id'],)).fetchone()
    expected=encode(proof).decode()
    if old is None or old[0]==0 and old[1]!=expected:raise error('Original online archive slot is missing or changed')
    db.execute('UPDATE history_capacity_slots SET online=0,proof=? WHERE job_id=?',(expected,job['id']))


class Capacity:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            self.policy=current(db)
            if self.policy:snapshot(db)

    def counts(self,db):
        count=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        slots,online=db.execute('SELECT count(*),coalesce(sum(online),0) FROM history_capacity_slots').fetchone()
        if slots!=count or db.execute('SELECT 1 FROM history_capacity_slots s LEFT JOIN jobs j ON j.id=s.job_id WHERE j.id IS NULL LIMIT 1').fetchone():
            raise error('Original history slot projection is incomplete')
        return {'control_jobs':count,'online_archives':online}

    def admit(self,db):
        values=self.counts(db)
        if values['control_jobs']>=self.policy['max_control_jobs']:raise error('Control job history capacity is full; preserve original records and follow administrator retention planning')
        if values['online_archives']>=self.policy['max_online_archives']:raise error('Online archive capacity is full; complete approved archive retirement before accepting new jobs')

    def status(self):
        if self.policy is None:return {'mode':'legacy','max_jobs':self.jobs.policy['max_jobs'],'separate_limits_enabled':False}
        with self.jobs.connect() as db:usage=self.counts(db)
        return {'mode':'separate','separate_limits_enabled':True,'limits':{k:self.policy[k] for k in ['max_control_jobs','max_online_archives']},
            'usage':usage,'blocks_new_jobs':usage['control_jobs']>=self.policy['max_control_jobs'] or usage['online_archives']>=self.policy['max_online_archives'],
            'history_deletion_enabled':False,'automatic_retirement_enabled':False}

    def add(self,db,job):
        if self.policy:
            proof={'kind':'online','job_id':job['id'],'owner':job['owner']}
            db.execute('INSERT INTO history_capacity_slots VALUES(?,1,?)',(job['id'],encode(proof).decode()))

    def working(self,db,states):
        states=tuple(sorted(set(states)))+('__invalid__',)
        return [decode(r) for r in db.execute('SELECT id,owner,state,data FROM jobs WHERE '+EXPR+' IN ('+','.join('?' for _ in states)+') ORDER BY rowid',states)]

    def last_dispatch(self,db):
        # Preserve the original owner rotation, including terminal jobs, using
        # scalar index values rather than loading historical JSON bodies.
        return dict(db.execute("SELECT owner,max(json_extract(data,'$.reserved_at')) FROM jobs WHERE json_extract(data,'$.reserved_at') IS NOT NULL GROUP BY owner"))

    def acl(self,principal,alias='j'):
        predicates=[];args=[]
        if principal.experiment_id:
            predicates.append("json_extract("+alias+".data,'$.experiment_id')=?");args.append(principal.experiment_id)
        if not principal.admin:
            projects=[p['id'] for p in self.jobs.access.projects(principal)]
            predicates.append('('+alias+".owner=? OR (CASE WHEN json_type("+alias+".data,'$.project') IS NULL THEN 'personal:'||"+alias+".owner ELSE json_extract("+alias+".data,'$.project') END) IN (SELECT value FROM json_each(?)))")
            args.extend([principal.id,json.dumps(projects)])
        return ' AND '.join(predicates) or '1',args

    def dashboard(self,db,principal):
        where,args=self.acl(principal)
        active=self.working(db,{'queued','reserved','running','cancelling','needs_attention'})
        pairs={j['id']:(db.execute('SELECT rowid FROM jobs WHERE id=?',(j['id'],)).fetchone()[0],j) for j in active}
        for row in db.execute('SELECT rowid,id,owner,state,data FROM jobs j WHERE '+where+' ORDER BY rowid DESC LIMIT 100',args):
            pairs[row[1]]=(row[0],decode(row[1:]))
        counts=dict.fromkeys(STATES,0)
        counts.update(dict(db.execute('SELECT state,count(*) FROM jobs j WHERE '+where+' GROUP BY state',args)))
        return [job for _,job in sorted(pairs.values(),key=lambda pair:pair[0])],counts

    def page(self,db,principal,limit,offset,state):
        from jobs import public
        where,args=self.acl(principal)
        if state is not None:where+=' AND j.state=?';args.append(state)
        total=db.execute('SELECT count(*) FROM jobs j WHERE '+where,args).fetchone()[0]
        rows=[decode(r) for r in db.execute('SELECT id,owner,state,data FROM jobs j WHERE '+where+' ORDER BY rowid DESC LIMIT ? OFFSET ?',args+[limit,offset])]
        return {'jobs':[public(j) for j in rows],'total':total,'limit':limit,'offset':offset}

    def replay(self,db,request,principal):
        key=request.get('idempotency_key') if isinstance(request,dict) else None
        if not isinstance(key,str) or not 1<=len(key)<=128:return None
        row=db.execute('SELECT id,owner,state,data,fingerprint FROM jobs WHERE owner=? AND idem_key=?',(principal.id,key)).fetchone()
        if not row:return None
        job=decode(row[:4]);receipt=job.get('_request_identity')
        if receipt is None:return None
        if (not isinstance(receipt,dict) or set(receipt)!={'format','request_sha256','fingerprint'}
            or receipt['format']!=FORMAT or receipt['fingerprint']!=row[4]):raise error('Original request replay receipt differs')
        if receipt['request_sha256']!=sha(request):return None
        if not self.jobs.access.can_read(principal,job):
            from jobs import JobError
            raise JobError('Job not found',404)
        from jobs import public
        return public(job),True


def matching(prefix):
    from deployment import verify,digest,DeployError
    _,_,release=verify(prefix)
    if not (release/'history_capacity.py').is_file() or digest(release/'history_capacity.py')!=digest(Path(__file__)):
        raise DeployError('Activate matching history capacity code before maintenance')


@contextmanager
def context(prefix):
    from cold_exports import context as offline_context
    from maintenance import assert_quiescent
    from cold_retention import require_stable_code
    matching(prefix)
    with offline_context(prefix) as (db,binding,payload,meta):
        assert_quiescent(Path(meta['state_dir']),'controller');require_stable_code(Path(meta['state_dir']))
        yield db,binding,meta


def _checkpoint(stage):pass


def plan(prefix,control,online):
    target=limits(control,online)
    with context(prefix) as (db,binding,meta):
        before,values=snapshot(db)
        if before['control_jobs']>control or before['online_archives']>online:raise error('Proposed capacity is below retained usage')
        if before['policy'] and before['policy']['revision']>=MAX_CHANGES:raise error('History capacity approval history is full')
        value={'format':FORMAT,'binding':binding,'before':before,'limits':target}
        value['id']=sha(value);return value


def apply(prefix,value):
    if (not isinstance(value,dict) or set(value)!={'format','binding','before','limits','id'} or value.get('format')!=FORMAT
        or len(encode(value))>65536 or value.get('id')!=sha({k:v for k,v in value.items() if k!='id'})):
        raise error('Invalid or changed history capacity plan')
    if not isinstance(value['limits'],dict) or set(value['limits'])!={'max_control_jobs','max_online_archives'}:raise error('Invalid capacity limit fields')
    target=limits(value['limits'].get('max_control_jobs'),value['limits'].get('max_online_archives'))
    with context(prefix) as (db,binding,meta):
        if value['binding']!=binding:raise error('History capacity plan binding is stale')
        if table(db,'history_capacity_changes'):
            row=db.execute('SELECT record FROM history_capacity_changes WHERE id=?',(value['id'],)).fetchone()
            if row:
                saved=json.loads(row[0])
                if saved['plan']!=value:raise error('Original history capacity approval differs')
                return {'id':value['id'],'reused':True,'policy':current(db),'original_jobs_retained':True}
        before,slots=snapshot(db)
        if before!=value['before']:raise error('Retained history changed after capacity preview')
        if before['policy'] and before['policy']['revision']>=MAX_CHANGES:raise error('History capacity approval history is full')
        if before['control_jobs']>target['max_control_jobs'] or before['online_archives']>target['max_online_archives']:raise error('Capacity is below retained usage')
        initialize(db)
        if before['policy'] is None:
            for slot in slots:db.execute('INSERT INTO history_capacity_slots VALUES(?,?,?)',slot)
        revision=before['policy']['revision']+1 if before['policy'] else 1
        policy={'format':FORMAT,'revision':revision,'approval_id':value['id'],**target}
        db.execute('INSERT INTO history_capacity_policy VALUES(1,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',(encode(policy).decode(),))
        db.execute('INSERT INTO history_capacity_changes VALUES(?,?)',(value['id'],encode({'plan':value,'applied_at':time.time()}).decode()))
        _checkpoint('before_commit')
        from cold_exports import commit
        commit(db,meta);_checkpoint('committed')
        return {'id':value['id'],'reused':False,'policy':policy,'usage':{k:before[k] for k in ['control_jobs','online_archives']},'original_jobs_retained':True}


def require_compatible(state,role,manifest):
    if role!='controller' or 'history_capacity.py' in manifest.get('files',{}):return
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if current(db):
                from deployment import DeployError
                raise DeployError('Retained capacity/replay contracts require compatible code; refusing downgrade')


def require_history(current_state,staged):
    from deployment import DeployError
    path=Path(current_state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as old:
        if current(old) is None:return
        before,slots=snapshot(old)
        with sqlite3.connect((Path(staged)/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as new:
            after,newslots=snapshot(new)
            if before['policy']!=after['policy'] or before['approvals_sha256']!=after['approvals_sha256'] or slots!=newslots:
                raise DeployError('Restore would lose history capacity approvals or confirmed archive slots')
            # Only approved archive relocation/lifecycle metadata may differ.
            # Refuse snapshots that lose any current original job semantics.
            from cold_reads import semantic
            for row in old.execute('SELECT id,owner,idem_key,fingerprint,data FROM jobs'):
                saved=new.execute('SELECT owner,idem_key,fingerprint,data FROM jobs WHERE id=?',(row[0],)).fetchone()
                job=json.loads(row[4]);restored=json.loads(saved[3]) if saved else {}
                if not saved or tuple(saved[:3])!=tuple(row[1:4]) or semantic(job)!=semantic(restored):
                    raise DeployError('Restore would lose original job admission, replay identity, result or attempt')
            if table(old,'execution_attempts'):
                if not table(new,'execution_attempts'):raise DeployError('Restore would lose original attempts')
                for row in old.execute('SELECT id,job_id,node_id,state,created_at FROM execution_attempts'):
                    if new.execute('SELECT id,job_id,node_id,state,created_at FROM execution_attempts WHERE id=?',(row[0],)).fetchone()!=row:
                        raise DeployError('Restore would lose original execution attempt history')

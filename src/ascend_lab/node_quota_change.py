"""Central CAS of one stopped Node's storage contract and fresh observation."""
import copy
import json
from pathlib import Path
import re
import uuid

from jobs import ACTIVE,JobError,now
from node_owner_admin import projection
from node_quota_admin import CAPABILITY,sha,validate_plan


class Changes:
    def __init__(self,nodes):
        self.nodes=nodes;self.jobs=nodes.jobs
        with self.jobs.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS node_quota_changes(plan_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS node_quota_pending ON node_quota_changes(node_id,state)')

    def record(self,key,node_id,db):
        if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key):raise JobError('Invalid Node quota plan ID')
        row=db.execute('SELECT node_id,record FROM node_quota_changes WHERE plan_id=?',(key,)).fetchone()
        if not row or row[0]!=node_id:raise JobError('Node quota approval not found',404)
        return json.loads(row[1])

    def pending(self,node_id,db):
        return db.execute("SELECT plan_id FROM node_quota_changes WHERE node_id=? AND state IN ('approved','ready') LIMIT 1",(node_id,)).fetchone() is not None

    def held_nodes(self,db):
        return {r[0] for r in db.execute("SELECT DISTINCT node_id FROM node_quota_changes WHERE state IN ('approved','ready')")}

    def save(self,value,db):
        db.execute('INSERT INTO node_quota_changes VALUES(?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET state=excluded.state,record=excluded.record',
            (value['plan']['id'],value['plan']['node_id'],value['state'],json.dumps(value,sort_keys=True)))

    def ack(self,value):
        plan=value['plan']
        return {'format':'ascend-lab-node-quota-approval/v1','plan_id':plan['id'],'approval_id':value['approval_id'],
            'node_id':plan['node_id'],'physical_id':plan['physical_id'],'before_sha256':sha(plan['before']),
            'after_sha256':sha(plan['after']),'state':value['state']}

    def approve(self,node_id,plan):
        validate_plan(plan)
        if node_id!=plan['node_id']:raise JobError('Node quota plan belongs to another node',409)
        with self.nodes.handoff.transaction() as (db,identities):
            old=db.execute('SELECT record FROM node_quota_changes WHERE plan_id=?',(plan['id'],)).fetchone()
            if old:
                value=json.loads(old[0])
                if value['plan']!=plan:raise JobError('Node quota plan differs from its original approval',409)
                return {'approval':self.ack(value),'reused':True,'historical_approval_only':value['state']=='observed'}
            node=self.nodes.node(node_id,db);before=node['profile']
            self.nodes.handoff.mutable(node_id,db)
            if projection(before)!=plan['before'] or before['physical_id']!=plan['physical_id']:
                raise JobError('Node quota plan does not match the current enabled enrollment',409)
            if not self.jobs.access.user(plan['owner']):raise JobError('Node quota owner is not active',409)
            if any(j['state'] in ACTIVE and j.get('node_id')==node_id for j in self.jobs.all(db)):
                raise JobError('Finish or reconcile every target Node attempt before quota approval',409)
            if self.jobs.inventory.holds(plan['physical_id'],db):raise JobError('Reconcile physical inventory holds before quota approval',409)
            roots={s['device'] for s in plan['native_before']['slots']};storage={s['storage_id'] for s in plan['native_before']['slots']}
            for other_id,raw in db.execute('SELECT id,profile FROM worker_nodes'):
                if other_id==node_id:continue
                other=json.loads(raw)
                if other['physical_id']!=plan['physical_id']:continue
                slots=[*other.get('workspace',{}).get('slots',[]),*other.get('persistent_cache',{}).get('slots',[]),
                    *([other['spool_storage']] if other.get('spool_storage') else [])]
                if any(s['device'] in roots or s['storage_id'] in storage for s in slots):
                    raise JobError('Quota filesystem is registered by another Node; coordinated multi-Node resize is not supported',409)
            after={**copy.deepcopy(before),**plan['after']};self.jobs.resources.validate_node(after,db)
            from control_storage import require
            require(self.jobs.runtime,self.jobs.policy.get('control_storage'),self.jobs.control_filesystem)
            from audit_retention import require
            require(self.jobs.access)
            if db.execute('SELECT count(*) FROM node_quota_changes WHERE node_id=?',(node_id,)).fetchone()[0]>=1024:
                raise JobError('Node quota change history limit reached',409)
            value={'plan':plan,'approval_id':uuid.uuid4().hex,'state':'approved','approved_at':now()}
            self.save(value,db)
            db.execute('UPDATE worker_nodes SET profile=?,last_seen=0,report=? WHERE id=?',(json.dumps(after),'{}',node_id))
            db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
                (now(),node_id,self.jobs.owner,after['policy_revision'],json.dumps(after['policy'])))
            return {'approval':self.ack(value),'reused':False,'node_native_apply_and_heartbeat_required':True}

    def status(self,node_id,body):
        if not isinstance(body,dict) or set(body)!={'plan_id'}:raise JobError('Node quota status has no overrides')
        with self.jobs.connect() as db:return self.ack(self.record(body['plan_id'],node_id,db))

    def ready(self,node_id,body):
        required={'plan_id','approval_id','after_sha256','receipt_sha256'}
        if (not isinstance(body,dict) or set(body)!=required
            or not re.fullmatch('[0-9a-f]{64}',str(body['receipt_sha256']))):raise JobError('Invalid Node quota ready acknowledgement')
        with self.jobs.transaction() as db:
            value=self.record(body['plan_id'],node_id,db);plan=value['plan'];node=self.nodes.node(node_id,db)
            if (body['approval_id']!=value['approval_id'] or body['after_sha256']!=sha(plan['after'])
                or projection(node['profile'])!=plan['after']):raise JobError('Node quota ready does not match the approved projection',409)
            if value.get('receipt_sha256',body['receipt_sha256'])!=body['receipt_sha256']:raise JobError('Node quota receipt changed',409)
            if value['state']=='approved':value.update(state='ready',receipt_sha256=body['receipt_sha256'],ready_at=now());self.save(value,db)
            return self.ack(value)

    def observe(self,node_id,profile,report,db):
        if not db.execute('SELECT 1 FROM node_quota_changes WHERE node_id=? LIMIT 1',(node_id,)).fetchone():return
        if CAPABILITY not in report['capabilities']:raise JobError('Node quota history requires matching Node capability',409)
        rows=list(db.execute("SELECT record FROM node_quota_changes WHERE node_id=? AND state='ready'",(node_id,)))
        for raw, in rows:
            value=json.loads(raw)
            if value['state']!='ready':continue
            if projection(profile)!=value['plan']['after']:raise JobError('Node quota heartbeat projection changed',409)
            if self.jobs.inventory.holds(profile['physical_id'],db):return
            for name in ['workspace','persistent_cache']:
                if not profile.get(name):continue
                observed=report.get(name,{})
                if any(s.get('status')!='ok' or (s.get('clean') is not True if name=='workspace' else s.get('leased') is not False)
                    for s in observed.get('slots',[])):return
                if not observed.get('slots'):return
            if profile.get('spool_storage') and report.get('finite_spool',{}).get('status')!='ok':return
            if self.jobs.policy.get('require_attempt_inventory') and not report.get('attempt_inventory',{}).get('reconciled'):return
            value.update(state='observed',observed_at=now());self.save(value,db)


def require_compatible(state,role):
    from deployment import DeployError
    import sqlite3
    state=Path(state)
    if role=='node':
        if (state/'node-quota-change.json').exists() or (state/'node-quota-history').exists():
            raise DeployError('Node quota change history requires matching code; refusing downgrade')
        return
    path=state/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_quota_changes'").fetchone()
            and db.execute('SELECT 1 FROM node_quota_changes LIMIT 1').fetchone()):
            raise DeployError('Node quota change history requires matching controller code; refusing downgrade')


def node_history(state):
    """Bounded receipt chain, also usable on a not-yet-published restore stage."""
    import hashlib
    import os
    import stat
    from deployment import DeployError,regular_json
    from node_quota_admin import HISTORY
    directory=Path(state)/HISTORY
    if not directory.exists() and not directory.is_symlink():return {'revision':0,'files':{}}
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise DeployError('Node quota history must be a private receipt directory')
    names=list(directory.iterdir())
    if len(names)>1024:raise DeployError('Node quota history exceeds its bound')
    receipts={};revisions=set();node_id=None
    for path in names:
        value=regular_json(path,max_bytes=65536,private=True)
        if (value.get('format')!='ascend-lab-node-quota-receipt/v1' or not re.fullmatch('[0-9a-f]{64}',str(value.get('plan_id')))
            or path.name!=value['plan_id']+'.json' or type(value.get('revision')) is not int or value['revision']<1
            or value['revision'] in revisions or not re.fullmatch('worker-[0-9a-f]{12}',str(value.get('node_id')))):
            raise DeployError('Invalid Node quota receipt chain')
        if node_id is not None and node_id!=value['node_id']:raise DeployError('Node quota receipt identities differ')
        node_id=value['node_id'];revisions.add(value['revision']);receipts[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    if revisions!=set(range(1,len(names)+1)):raise DeployError('Node quota receipt chain has missing revisions')
    return {'revision':len(names),'files':receipts}


def require_restore_history(current,staged,role,installation,manifest):
    """Do not rewind native changes by restoring older application metadata.

    This compares against this installation's still-retained current state.
    It is not an off-host disaster-recovery authority or multi-controller fence.
    """
    from deployment import DeployError
    if role=='node':
        before=node_history(current);after=node_history(staged)
        floor=installation.get('node_quota_revision',0);saved=manifest.get('node_quota_revision',after['revision'])
        if type(floor) is not int or floor<0 or type(saved) is not int or saved!=after['revision']:
            raise DeployError('Snapshot Node quota history and revision differ')
        if before['revision']!=floor or after['revision']<floor or any(after['files'].get(k)!=v for k,v in before['files'].items()):
            raise DeployError('Snapshot predates or changes retained Node quota history; keep the current state')
        return after['revision']
    import sqlite3
    current=Path(current)/'jobs.sqlite3';staged=Path(staged)/'jobs.sqlite3'
    if not current.exists():return None
    with sqlite3.connect(current.as_uri()+'?mode=ro',uri=True) as old:
        if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_quota_changes'").fetchone():return None
        with sqlite3.connect(staged.as_uri()+'?mode=ro',uri=True) as new:
            available=new.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_quota_changes'").fetchone()
            for plan_id,node_id,state,record in old.execute('SELECT plan_id,node_id,state,record FROM node_quota_changes'):
                row=new.execute('SELECT node_id,state,record FROM node_quota_changes WHERE plan_id=?',(plan_id,)).fetchone() if available else None
                if row!=(node_id,state,record):
                    raise DeployError('Snapshot predates or changes retained Node quota history; keep the current state')
    return None

"""Central approval and authenticated, idempotent Node owner handoff.

The central identity stays disabled. Node commit/ready never issues credentials.
"""
import copy
import json
import re
import uuid
from contextlib import contextmanager

from jobs import ACTIVE,JobError,now,identity_alive,read_json
from node_owner_admin import projection,sha,_plan_check
import owner_identity

CAPABILITY='owner-handoff-v1'
ACK='ascend-lab-owner-node-ack/v1'
ACK_FIELDS={'format','phase','plan_id','approval_id','ack_id','previous_ack_id','node_id','physical_id','owner',
            'stage_id','policy_revision','projection_sha256'}


def ready_projection(prepared,owner):
    value=copy.deepcopy(prepared);value['owner_bindings'][owner]['state']='ready';return value


def validate_ack(value,expected,phase,previous=None):
    if (not isinstance(value,dict) or set(value)!=ACK_FIELDS or value['format']!=ACK or value['phase']!=phase
        or value['previous_ack_id']!=previous or not isinstance(value['approval_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['approval_id'])
        or not isinstance(value['ack_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['ack_id'])
        or type(value['policy_revision']) is not int
        or any(value.get(k)!=v for k,v in expected.items())):raise JobError('Central owner acknowledgement binding differs',409)
    return value


class Handoff:
    def __init__(self,nodes):
        self.nodes=nodes;self.jobs=nodes.jobs
        with self.jobs.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS owner_node_handoffs(plan_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,owner TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS owner_node_expansions(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)')

    def get(self,plan_id,db):
        row=db.execute('SELECT record FROM owner_node_handoffs WHERE plan_id=?',(plan_id,)).fetchone()
        if not row:raise JobError('No central approval exists for this Node owner plan',404)
        return json.loads(row[0])

    @contextmanager
    def transaction(self):
        # All cross-database writers use jobs -> identities. Keep the identity
        # reservation until the jobs commit, including on ACK/heartbeat replay.
        with self.jobs.transaction() as db:
            identities=self.jobs.access.connect()
            try:
                identities.execute('BEGIN IMMEDIATE')
                try:yield db,identities
                except BaseException:raise
                else:db.commit()
            finally:
                identities.rollback();identities.close()

    def reserved_profile(self,node_id,profile,db):
        for row in db.execute('SELECT record FROM owner_node_handoffs WHERE node_id=?',(node_id,)):
            value=json.loads(row[0])
            if value['state']!='ready' or not value.get('node_online_seen'):return value['after_profile']
        return profile

    def authority(self,value,db,identities):
        if value.get('authorization'):
            if not value.get('node_online_seen'):
                from owner_node_grant import Expansion
                Expansion(self).validate(value,db,identities)
            return
        stage=self._stage(value['owner'],value['stage_id'])
        if self._storage(value['owner'],stage)!=value['storage']:
            raise JobError('Central storage binding changed since approval',409)

    def save(self,db,value):
        db.execute('INSERT INTO owner_node_handoffs VALUES(?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET state=excluded.state,record=excluded.record',
                   (value['plan_id'],value['node_id'],value['owner'],value['state'],json.dumps(value)))

    def pending(self,node_id,db):
        for row in db.execute('SELECT record FROM owner_node_handoffs WHERE node_id=?',(node_id,)):
            value=json.loads(row[0])
            if value['state']!='ready' or not value.get('node_online_seen'):return value
        return None

    def held_nodes(self,db):
        return {value['node_id'] for row in db.execute('SELECT record FROM owner_node_handoffs')
                for value in [json.loads(row[0])] if value['state']!='ready' or not value.get('node_online_seen')}

    def mutable(self,node_id,db):
        if self.pending(node_id,db):raise JobError('Finish the approved owner handoff before changing this Node configuration',409)
        for row in db.execute("SELECT record FROM owner_node_handoffs WHERE node_id=? AND state='ready'",(node_id,)):
            if not json.loads(row[0]).get('node_online_seen'):
                raise JobError('Wait for the acknowledged Node healthy heartbeat before changing configuration',409)

    def observe(self,node_id,profile,report,db,identities):
        if self.jobs.inventory.holds(profile['physical_id'],db):return
        if self.jobs.policy.get('require_finite_node_spool') and not profile.get('spool_storage'):return
        if profile.get('spool_storage') and report.get('finite_spool',{}).get('status')!='ok':return
        if self.jobs.policy.get('require_node_spool') and report.get('artifact_gc_ready') is not True:return
        if self.jobs.policy.get('require_attempt_inventory') and not report.get('attempt_inventory',{}).get('reconciled'):return
        if self.jobs.policy.get('require_environment_contract') and not profile.get('environment_manifest'):return
        if self.jobs.policy.get('require_pinned_docker') and 'pinned-docker-runtime-v1' not in report.get('capabilities',[]):return
        if any(s.get('status')!='ok' or not s.get('clean') for s in report.get('workspace',{}).get('slots',[])):return
        if any(s.get('status')!='ok' or s.get('leased') for s in report.get('persistent_cache',{}).get('slots',[])):return
        for row in db.execute("SELECT record FROM owner_node_handoffs WHERE node_id=? AND state='ready'",(node_id,)).fetchall():
            value=json.loads(row[0])
            if not value.get('node_online_seen') and sha(profile)==sha(value['after_profile']):
                if value.get('authorization'):self.authority(value,db,identities)
                value.update(node_online_seen=True,node_online_seen_at=now());self.save(db,value);self._event(db,value,'online-observed')
                if value.get('authorization'):
                    db.execute("UPDATE owner_node_expansions SET state='complete' WHERE id=?",(value['authorization']['id'],))

    def _idle(self,node_id,profile,db):
        if any(j.get('node_id')==node_id and j['state'] in ACTIVE for j in self.jobs.all(db)):
            raise JobError('Reconcile every Node execution before owner handoff',409)
        if self.jobs.inventory.holds(profile['physical_id'],db):raise JobError('Reconcile physical execution inventories before owner handoff',409)

    def _stage(self,owner,stage_id):
        status=self.jobs.access.registration(owner);profile=status.get('profile')
        with self.jobs.access.connect() as db:
            row=db.execute('SELECT disabled,password_hash FROM users WHERE id=?',(owner,)).fetchone()
            count=db.execute('SELECT COUNT(*) FROM tokens WHERE user=?',(owner,)).fetchone()[0]
        if (not row or row!=(1,'!') or count or not owner_identity.staged(owner,profile)
            or profile['provisioning']['id']!=stage_id):raise JobError('A matching disabled, credential-free central stage is required',409)
        return profile

    def _storage(self,owner,stage):
        policy=self.jobs.payload.policy
        if not policy or owner not in policy.get('owner_groups',{}):raise JobError('Provision the central owner hard-quota group before approval',409)
        current=self.jobs.payload.owner_space(owner);item=policy['owner_groups'][owner]
        wanted=sum(stage['quota'][k] for k in ['input_storage_mib','archive_storage_mib'])*1024**2
        if wanted+65536>current['byte_hard']:raise JobError('Central owner hard quota cannot cover the staged storage budgets',409)
        return {'storage_id':policy['storage_id'],**item}

    def _after(self,node_id,profile,value,db):
        if (value['node_id']!=node_id or value['physical_id']!=profile['physical_id'] or projection(profile)!=value['before']):
            raise JobError('Node plan does not match the current registered identity/projection/revision',409)
        after={**profile,**ready_projection(value['after'],value['request']['owner'])}
        if profile['physical_id']==self.jobs.backend.physical_id and value['request']['uid']==self.nodes.controller_uid():
            raise JobError('Task UID cannot alias the central service identity',409)
        from persistent_cache import validate,aliases
        from quota_workspace import capacity,sharing
        validate(after['persistent_cache'],after['users']);combined=list(after['workspace']['slots'])
        held={j.get('node_id') for j in self.jobs.all(db) if j['state'] in ACTIVE}|self.held_nodes(db)
        for peer_id,raw,disabled in db.execute('SELECT id,profile,disabled FROM worker_nodes'):
            peer=self.reserved_profile(peer_id,json.loads(raw),db)
            if peer_id==node_id or peer['physical_id']!=after['physical_id'] or disabled and peer_id not in held:continue
            try:aliases(after,peer)
            except ValueError as error:raise JobError(str(error),409) from None
            for a in after['workspace']['slots']:
                for b in peer.get('workspace',{}).get('slots',[]):
                    if a['storage_id']==b['storage_id'] or a['path']==b['path'] or a['device']==b['device'] and not sharing(a,b):
                        raise JobError('New owner work allocation conflicts with another registered Node',409)
            combined.extend(peer.get('workspace',{}).get('slots',[]))
        try:capacity(combined)
        except ValueError as error:raise JobError(str(error),409) from None
        self.jobs.resources.validate_node(after,db)
        return after

    def approve(self,node_id,value):
        try:_plan_check(value)
        except (ValueError,KeyError,TypeError):raise JobError('Invalid private Node owner plan',400) from None
        ready=read_json(self.jobs.runtime/'server-ready.json') or {}
        if identity_alive(ready) and CAPABILITY not in ready.get('capabilities',[]):
            raise JobError('Reload this controller with owner handoff admission support before approving',409)
        owner=value['request']['owner'];stage_id=value['request']['stage_id']
        if not isinstance(stage_id,str):raise JobError('Stage digest must be a string',400)
        with self.jobs.transaction() as db:
            existing=db.execute('SELECT record FROM owner_node_handoffs WHERE plan_id=?',(value['id'],)).fetchone()
            if existing:
                record=json.loads(existing[0])
                if record['node_id']!=node_id:raise JobError('Plan was approved for another Node',409)
                return {'approval':self.public(record),'reused':True}
            node=self.nodes.node(node_id,db);profile=node['profile'];self.mutable(node_id,db);self._idle(node_id,profile,db)
            stage=self._stage(owner,stage_id);storage=self._storage(owner,stage)
            after=self._after(node_id,profile,value,db)
            record={'format':'ascend-lab-owner-approval/v1','plan_id':value['id'],'approval_id':uuid.uuid4().hex,
                'node_id':node_id,'physical_id':profile['physical_id'],'owner':owner,'stage_id':stage_id,'state':'approved',
                'before_profile_sha256':sha(profile),'prepared_projection':value['after'],'before_projection_sha256':sha(value['before']),
                'after_profile':after,'node_owner_revision':value['revision']+1,'storage':storage,'approved_at':now()}
            self.save(db,record)
            db.execute('UPDATE worker_nodes SET last_seen=0,report=? WHERE id=?',('{}',node_id))
            self._event(db,record,'approved')
            return {'approval':self.public(record),'reused':False}

    def _event(self,db,record,phase):
        db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
            (now(),record['node_id'],self.jobs.owner,record['after_profile']['policy_revision'],
             json.dumps({'kind':'owner-handoff','phase':phase,'owner':record['owner'],'plan_id':record['plan_id']})))

    @staticmethod
    def public(value):return {**{k:value[k] for k in ['plan_id','approval_id','node_id','owner','stage_id','state']},'node_online_seen':bool(value.get('node_online_seen'))}

    def status(self,node_id):
        with self.jobs.connect() as db:
            self.nodes.node(node_id,db)
            return [self.public(json.loads(row[0])) for row in db.execute('SELECT record FROM owner_node_handoffs WHERE node_id=?',(node_id,))]

    def _ack(self,record,phase,previous=None):
        return {'format':ACK,'phase':phase,'plan_id':record['plan_id'],'approval_id':record['approval_id'],'ack_id':uuid.uuid4().hex,
            'previous_ack_id':previous,'node_id':record['node_id'],'physical_id':record['physical_id'],'owner':record['owner'],
            'stage_id':record['stage_id'],'policy_revision':record['after_profile']['policy_revision'],
            'projection_sha256':sha(projection(record['after_profile']))}

    def commit(self,node_id,body):
        if not isinstance(body,dict) or set(body)!={'receipt'} or not isinstance(body['receipt'],dict):raise JobError('Provide the prepared Node receipt')
        receipt=body['receipt'];fields={'format','plan_id','node_id','physical_id','owner','stage_id','revision','policy_revision',
            'before_projection_sha256','after_projection','after_configuration_sha256','completed_at','central_acknowledgement_still_required'}
        if (set(receipt)!=fields or receipt['format']!='ascend-lab-node-owner-receipt/v1'
            or not isinstance(receipt['plan_id'],str) or not re.fullmatch('[0-9a-f]{64}',receipt['plan_id'])
            or any(type(receipt[k]) is not int for k in ['revision','policy_revision'])
            or receipt['central_acknowledgement_still_required'] is not True):raise JobError('Invalid Node owner receipt')
        with self.transaction() as (db,identities):
            record=self.get(receipt['plan_id'],db);node=self.nodes.node(node_id,db)
            if record['node_id']!=node_id:raise JobError('Owner plan belongs to another Node',403)
            expected={'node_id':node_id,'physical_id':record['physical_id'],'owner':record['owner'],'stage_id':record['stage_id'],
                'revision':record['node_owner_revision'],'policy_revision':record['after_profile']['policy_revision'],
                'before_projection_sha256':record['before_projection_sha256'],'after_projection':record['prepared_projection'],
                'central_acknowledgement_still_required':True}
            if (any(receipt.get(k)!=v for k,v in expected.items())
                or not isinstance(receipt['after_configuration_sha256'],str)
                or not re.fullmatch('[0-9a-f]{64}',receipt['after_configuration_sha256'])):raise JobError('Node receipt differs from the approved owner allocation',409)
            if record.get('authorization'):self.authority(record,db,identities)
            if record['state'] in {'committed','ready'}:
                if record['receipt_sha256']!=sha(receipt) or sha(node['profile'])!=sha(record['after_profile']):
                    raise JobError('Committed receipt/profile changed; reconcile the original handoff',409)
                return {'ack':record['commit_ack'],'reused':True}
            if record['state']!='approved' or sha(node['profile'])!=record['before_profile_sha256']:raise JobError('Approved Node profile changed',409)
            self._idle(node_id,node['profile'],db);self.authority(record,db,identities)
            record.update(state='committed',receipt_sha256=sha(receipt),committed_at=now())
            record['commit_ack']=self._ack(record,'committed');self.save(db,record)
            db.execute('UPDATE worker_nodes SET profile=?,last_seen=0,report=? WHERE id=?',(json.dumps(record['after_profile']),'{}',node_id))
            self._event(db,record,'committed')
            return {'ack':record['commit_ack'],'reused':False}

    def ready(self,node_id,body):
        if not isinstance(body,dict) or set(body)!={'plan_id','commit_ack_id'}:raise JobError('Provide the persisted commit acknowledgement ID')
        if any(not isinstance(body[k],str) or not re.fullmatch('[0-9a-f]{'+str(n)+'}',body[k]) for k,n in [('plan_id',64),('commit_ack_id',32)]):
            raise JobError('Invalid owner acknowledgement identifiers')
        with self.transaction() as (db,identities):
            record=self.get(body['plan_id'],db);node=self.nodes.node(node_id,db)
            if record['node_id']!=node_id:raise JobError('Owner plan belongs to another Node',403)
            if record['state'] not in {'committed','ready'} or body['commit_ack_id']!=record['commit_ack']['ack_id']:
                raise JobError('Commit acknowledgement does not match this owner handoff',409)
            if sha(node['profile'])!=sha(record['after_profile']):raise JobError('Committed Node profile changed',409)
            if record.get('authorization'):self.authority(record,db,identities)
            if record['state']=='ready':return {'ack':record['ready_ack'],'reused':True}
            self._idle(node_id,node['profile'],db);self.authority(record,db,identities)
            record.update(state='ready',ready_at=now());record['ready_ack']=self._ack(record,'ready',record['commit_ack']['ack_id']);self.save(db,record)
            db.execute('UPDATE worker_nodes SET last_seen=0,report=? WHERE id=?',('{}',node_id));self._event(db,record,'ready')
            return {'ack':record['ready_ack'],'reused':False}

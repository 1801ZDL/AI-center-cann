"""Explicit resource grants for activated owners joining an additional Node.

The immutable Root Node plan identifies slots; a separate service-UID grant
authorizes their use. No credential is read from disk or issued here.
"""
import copy
import json
import os
from pathlib import Path
import re
import time
import uuid

from jobs import JobError,identity_alive,read_json,now
from node_owner_admin import _plan_check,sha
import owner_identity

CAPABILITY='owner-node-expansion-v1'
FORMAT='ascend-lab-owner-node-grant/v1'
FIELDS={'format','id','nonce','node_id','node_plan','authority_sha256','context_sha256','previous_grant_id','service_uid'}


class Expansion:
    def __init__(self,handoff):
        self.h=handoff;self.jobs=handoff.jobs

    def controller(self):
        ready=read_json(self.jobs.runtime/'server-ready.json') or {}
        if not identity_alive(ready) or CAPABILITY not in ready.get('capabilities',[]):
            raise JobError('A live expansion-aware controller is required',409)
        try:uid=Path('/proc/'+str(ready['pid'])).stat().st_uid
        except OSError:raise JobError('Controller identity disappeared',409) from None
        if uid!=os.geteuid():raise JobError('Expansion administration must use the controller service UID',403)

    def authority(self,owner,stage_id,identities):
        row=identities.execute('SELECT password_hash,salt,profile,disabled FROM users WHERE id=?',(owner,)).fetchone()
        if not row or row[3] or row[0]=='!':raise JobError('Expansion requires an enabled activated owner',409)
        profile=json.loads(row[2]);marker=profile.get('provisioned')
        if not owner_identity.login_allowed(profile) or not marker or marker['stage_id']!=stage_id:
            raise JobError('Expansion requires the matching committed owner activation',409)
        activation=identities.execute('SELECT id,record FROM owner_activations WHERE owner=?',(owner,)).fetchone()
        if not activation or activation[0]!=marker['id']:
            raise JobError('Owner activation record differs',409)
        record=json.loads(activation[1])
        if (record.get('owner')!=owner or record.get('request',{}).get('stage_id')!=stage_id
            or record['request'].get('required_nodes')!=marker['required_nodes']):
            raise JobError('Committed owner definition differs',409)
        epoch=identities.execute('SELECT revision FROM owner_security_revisions WHERE owner=?',(owner,)).fetchone()
        tokens=identities.execute('SELECT digest,scopes,revoked,expires_at FROM tokens WHERE user=? ORDER BY digest',(owner,)).fetchall()
        memberships=identities.execute('SELECT project,role FROM members WHERE user=? ORDER BY project',(owner,)).fetchall()
        storage=self.h._storage(owner,profile)
        digest=sha({'owner':owner,'user':row,'epoch':epoch,'tokens':tokens,'memberships':memberships,
            'live_tokens':[t[0] for t in tokens if not t[2] and t[3]>time.time()],
            'activation':activation,'storage':storage,'configuration':sha(self.jobs.access.config)})
        return digest,storage

    def validate(self,record,db,identities):
        binding=record['authorization']
        grant=db.execute('SELECT state,record FROM owner_node_expansions WHERE id=?',(binding['id'],)).fetchone()
        if not grant or grant[0]!='pending':raise JobError('Expansion authorization is absent or superseded',409)
        plan=json.loads(grant[1])
        current,storage=self.authority(record['owner'],record['stage_id'],identities)
        if (plan['node_plan']['id']!=record['plan_id'] or plan['node_id']!=record['node_id']
            or binding!={'id':plan['id'],'authority_sha256':plan['authority_sha256']}
            or current!=binding['authority_sha256'] or storage!=record['storage']):
            raise JobError('Owner authority changed; explicitly reauthorize the original Node plan',409)

    def context(self,node_id,value,db,reauthorize):
        node=self.h.nodes.node(node_id,db);profile=node['profile']
        self.h._idle(node_id,profile,db)
        row=db.execute('SELECT record FROM owner_node_handoffs WHERE plan_id=?',(value['id'],)).fetchone()
        previous=json.loads(row[0]) if row else None
        if previous:
            if (not reauthorize or previous.get('node_online_seen') or not previous.get('authorization')
                or previous['node_id']!=node_id):
                raise JobError('Expansion already exists; only explicit pending reauthorization is allowed',409)
            expected=previous['before_profile_sha256'] if previous['state']=='approved' else sha(previous['after_profile'])
            if sha(profile)!=expected:raise JobError('Pending expansion Node profile changed',409)
            # Exact plan ID includes all sources, cache generations, slots and
            # original before/after projections; reauthorization cannot edit it.
            after=previous['after_profile']
        else:
            if reauthorize:raise JobError('No pending expansion exists to reauthorize',409)
            self.h.mutable(node_id,db);after=self.h._after(node_id,profile,value,db)
        return previous,after,sha({'profile':profile,'previous':previous,'after':after})

    def plan(self,node_id,value,reauthorize=False):
        try:_plan_check(value)
        except (ValueError,KeyError,TypeError):raise JobError('Invalid private Node owner plan',400) from None
        self.controller()
        with self.h.transaction() as (db,identities):
            previous,after,context=self.context(node_id,value,db,reauthorize)
            authority,_=self.authority(value['request']['owner'],value['request']['stage_id'],identities)
            result={'format':FORMAT,'nonce':uuid.uuid4().hex,'node_id':node_id,'node_plan':copy.deepcopy(value),
                'authority_sha256':authority,'context_sha256':context,
                'previous_grant_id':previous['authorization']['id'] if previous else None,'service_uid':os.geteuid()}
            result['id']=sha(result);return result

    def apply(self,value):
        if (not isinstance(value,dict) or set(value)!=FIELDS or value.get('format')!=FORMAT
            or not isinstance(value.get('id'),str) or not re.fullmatch('[0-9a-f]{64}',value['id'])
            or sha({k:v for k,v in value.items() if k!='id'})!=value['id']
            or value['service_uid']!=os.geteuid()):raise JobError('Invalid expansion grant plan',400)
        try:_plan_check(value['node_plan'])
        except (ValueError,KeyError,TypeError):raise JobError('Invalid bound Node plan',400) from None
        self.controller();node_plan=value['node_plan'];owner=node_plan['request']['owner']
        with self.h.transaction() as (db,identities):
            existing=db.execute('SELECT state,record FROM owner_node_expansions WHERE id=?',(value['id'],)).fetchone()
            if existing:
                if json.loads(existing[1])!=value:raise JobError('Expansion plan record differs',409)
                if existing[0]=='superseded':raise JobError('This grant was superseded; use the new explicit authorization',409)
                record=self.h.get(node_plan['id'],db)
                if existing[0]!='complete':self.validate(record,db,identities)
                return {'grant_id':value['id'],'approval':self.h.public(record),'reused':True,
                        'historical_receipt_only':existing[0]=='complete'}
            previous,after,context=self.context(value['node_id'],node_plan,db,bool(value['previous_grant_id']))
            authority,storage=self.authority(owner,node_plan['request']['stage_id'],identities)
            if authority!=value['authority_sha256'] or context!=value['context_sha256']:
                raise JobError('Expansion plan is stale; inspect current authority and create a new plan',409)
            if (previous['authorization']['id'] if previous else None)!=value['previous_grant_id']:
                raise JobError('Pending expansion authorization changed',409)
            if previous:
                record=previous
                db.execute("UPDATE owner_node_expansions SET state='superseded' WHERE id=?",(value['previous_grant_id'],))
            else:
                profile=self.h.nodes.node(value['node_id'],db)['profile']
                record={'format':'ascend-lab-owner-approval/v1','plan_id':node_plan['id'],'approval_id':uuid.uuid4().hex,
                    'node_id':value['node_id'],'physical_id':node_plan['physical_id'],'owner':owner,
                    'stage_id':node_plan['request']['stage_id'],'state':'approved','before_profile_sha256':sha(profile),
                    'prepared_projection':node_plan['after'],'before_projection_sha256':sha(node_plan['before']),
                    'after_profile':after,'node_owner_revision':node_plan['revision']+1,'approved_at':now()}
            record.update(storage=storage,authorization={'id':value['id'],'authority_sha256':authority})
            db.execute('INSERT INTO owner_node_expansions VALUES(?,?,?,?)',(value['id'],node_plan['id'],'pending',json.dumps(value)))
            self.h.save(db,record)
            if not previous:
                from qualification_compatibility import prepare
                prepare(self.jobs,record,profile,db,identities)
            db.execute('UPDATE worker_nodes SET last_seen=0,report=? WHERE id=?',('{}',value['node_id']))
            self.h._event(db,record,'expansion-reauthorized' if previous else 'expansion-approved')
            return {'grant_id':value['id'],'approval':self.h.public(record),'reused':False,'historical_receipt_only':False}

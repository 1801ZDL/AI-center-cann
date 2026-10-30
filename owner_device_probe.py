"""Local administrator authorization of one fixed probe for a credential-free owner.

The authorization is not a Principal available to authentication or the HTTP API.
Jobs still use the central resource ledger, native owner storage and Node leases.
"""
import json
import re
import sqlite3
import time

from auth import Principal
from device_qualification import PROBE,parameters,decision,digest
from jobs import JobError,now,public
import owner_identity

CAPABILITY='staged-owner-device-probe-v1'
TABLE='owner_device_probes'
FORMAT='ascend-lab-owner-device-probe/v1'
MAX_RECORDS=4096


def _checkpoint(name):pass


def request(value):
    from auth import IDENTIFIER
    fields={'operation_id','owner','stage_id','node_id','plan_id','device_id','memory_mib'}
    if (not isinstance(value,dict) or set(value)!=fields or not isinstance(value['owner'],str)
        or not IDENTIFIER.fullmatch(value['owner']) or any(not isinstance(value[k],str) or not re.fullmatch(p,value[k])
        for k,p in [('operation_id','[0-9a-f]{32}'),('stage_id','[0-9a-f]{64}'),('plan_id','[0-9a-f]{64}')])):
        raise JobError('A staged probe requires an immutable operation, owner, stage, original Node plan and one device',400)
    try:parameters({k:value[k] for k in ['node_id','device_id','memory_mib']})
    except ValueError as error:raise JobError(str(error),400) from None
    return dict(value)


def initialize(jobs):
    with jobs.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS '+TABLE+' (id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation_id TEXT NOT NULL, record TEXT NOT NULL, UNIQUE(owner,operation_id))')


def load(db,identifier):
    row=db.execute('SELECT record FROM '+TABLE+' WHERE id=?',(identifier,)).fetchone()
    if not row:raise JobError('No original administrator staged probe authorization exists',409)
    value=json.loads(row[0]);spec=request(value['request'])
    if (set(value)!={'format','id','request','identity_revision','profile_sha256','node_profile_sha256','storage','access_context','created_at','job_id'}
        or value['format']!=FORMAT or value['id']!=identifier or identifier!=digest({'request':spec})
        or type(value['identity_revision']) is not int or value['identity_revision']<1
        or any(not isinstance(value[k],str) or not re.fullmatch('[0-9a-f]{64}',value[k]) for k in ['profile_sha256','node_profile_sha256','access_context'])
        or value['job_id'] is not None and (not isinstance(value['job_id'],str) or not re.fullmatch('job-[0-9a-f]{12}',value['job_id']))):
        raise JobError('Invalid original staged probe authorization',409)
    return value


def save(db,value):
    spec=value['request']
    db.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
               (value['id'],spec['owner'],spec['operation_id'],json.dumps(value)))


def _identity(jobs,spec,identities):
    row=identities.execute('SELECT disabled,password_hash,profile FROM users WHERE id=?',(spec['owner'],)).fetchone()
    if not row or row[:2]!=(1,'!'):raise JobError('The probe requires the original disabled credential-free staged owner',409)
    profile=json.loads(row[2])
    if (not owner_identity.staged(spec['owner'],profile) or profile['provisioning']['id']!=spec['stage_id']
        or identities.execute('SELECT 1 FROM tokens WHERE user=?',(spec['owner'],)).fetchone()
        or identities.execute('SELECT 1 FROM owner_activations WHERE owner=?',(spec['owner'],)).fetchone()):
        raise JobError('The staged identity changed or already has credentials/activation',409)
    quota=profile['quota']
    if quota['npu']<1 or quota['cpu']<1 or quota['memory_mib']<spec['memory_mib'] or quota['max_running']<1 or quota['max_queued']<1:
        raise JobError('The staged quotas cannot fit the fixed single-device probe',409)
    revision=identities.execute('SELECT revision FROM owner_security_revisions WHERE owner=?',(spec['owner'],)).fetchone()
    if not revision:raise JobError('Missing monotonic staged identity revision',409)
    return profile,revision[0]


def _current(jobs,spec,db,identities):
    profile,revision=_identity(jobs,spec,identities);node=jobs.nodes.node(spec['node_id'],db);np=node['profile']
    handoff=jobs.nodes.handoff.get(spec['plan_id'],db)
    if (handoff['owner']!=spec['owner'] or handoff['node_id']!=node['id'] or handoff['stage_id']!=spec['stage_id']
        or handoff['state']!='ready' or not handoff.get('node_online_seen') or handoff['after_profile']!=np):
        raise JobError('The staged probe requires the unchanged ready original Node owner handoff',409)
    if PROBE not in np['templates'] or spec['device_id'] not in np.get('npu',{}).get('device_ids',[]):
        raise JobError('Explicitly enroll the fixed probe and physical device before staged qualification',409)
    storage=jobs.nodes.handoff._storage(spec['owner'],profile)
    if storage!=handoff['storage']:raise JobError('The original staged owner storage contract changed',409)
    access=decision(node,spec['owner'],spec['device_id'])
    if access['context_sha256'] is None:raise JobError('The Node needs current protected host/runtime observations before staged qualification',409)
    return profile,{'identity_revision':revision,'profile_sha256':digest(profile),'node_profile_sha256':digest(np),
                    'storage':storage,'access_context':access['context_sha256']}


def validate(jobs,identifier,db,identities,job=None):
    value=load(db,identifier);spec=value['request'];profile,current=_current(jobs,spec,db,identities)
    if any(value[k]!=v for k,v in current.items()):raise JobError('Original staged probe authority/environment changed; submit a new explicit operation',409)
    if job is not None:
        if (value['job_id']!=job['id'] or job['owner']!=spec['owner'] or job['environment']!='worker-python'
            or job.get('template')!=PROBE or job.get('parameters')!={k:spec[k] for k in ['node_id','device_id','memory_mib']}
            or job['resources']!={'cpu':1,'memory_mib':spec['memory_mib'],'shm_mib':0,'npu':1}
            or job['timeout_seconds']!=120 or job.get('input') or job.get('depends_on')
            or job.get('project')!='personal:'+spec['owner'] or job['artifact_limits']!={'file_mib':1,'total_mib':2}):
            raise JobError('Staged probe job differs from its original fixed request',409)
        # The Node independently uses its installed fixed launcher. Also pin the
        # central launch definition rather than treating a private marker as a
        # permission for another command.
        expected=jobs.normalize(job_request(spec),principal(profile,spec['owner']))
        if any(job.get(k)!=expected.get(k) for k in ['argv','kind','cwd','output','_environment']):
            raise JobError('Staged probe launcher definition changed',409)
    return principal(profile,spec['owner'])


def principal(profile,owner):
    return Principal(owner,'user',profile['workspace'],tuple(profile['environments']),'templates',profile['quota'],frozenset({'jobs:submit'}))


def job_request(spec):
    return {'name':'管理员预激活设备访问验收','environment':'worker-python','template':PROBE,
            'parameters':{k:spec[k] for k in ['node_id','device_id','memory_mib']},
            'idempotency_key':'staged-device:'+digest({'request':spec})}


def bind_job(jobs,identifier,job,db,identities):
    value=load(db,identifier)
    if value['job_id'] is not None and value['job_id']!=job['id']:raise JobError('Original staged probe already names a different job',409)
    value['job_id']=job['id'];save(db,value);validate(jobs,identifier,db,identities,job)


def authorize(jobs,value):
    """Local service administrator only; never registered as an HTTP/MCP action."""
    spec=request(value);initialize(jobs);identifier=digest({'request':spec})
    from owner_activation import Activation
    Activation(jobs)
    ready=__import__('lab_client').read_json(jobs.runtime/'server-ready.json') or {}
    if __import__('jobs').identity_alive(ready) and CAPABILITY not in ready.get('capabilities',[]):
        raise JobError('Reload the controller with staged probe admission support first',409)
    with jobs.nodes.handoff.transaction() as (db,identities):
        old=db.execute('SELECT id FROM '+TABLE+' WHERE owner=? AND operation_id=?',(spec['owner'],spec['operation_id'])).fetchone()
        if old:
            if old[0]!=identifier:raise JobError('The original operation ID already has another request',409)
            record=load(db,identifier)
            if record['job_id']:
                return {'id':identifier,'job':public(jobs.get(record['job_id'],db)),'historical_receipt_only':True,'credentials_issued':False}
        else:
            if db.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]>=MAX_RECORDS:raise JobError('Staged probe authorization history is full; explicit retention is required',409)
            profile,current=_current(jobs,spec,db,identities)
            from owner_activation import Activation
            # Full native storage/Node health/group readiness, excluding the
            # access evidence this fixed probe is about to acquire.
            Activation(jobs).readiness({'owner':spec['owner'],'stage_id':spec['stage_id'],
                'required_nodes':[{'node_id':spec['node_id'],'plan_id':spec['plan_id']}]},db,profile,device_access=False)
            record={'format':FORMAT,'id':identifier,'request':spec,**current,'created_at':now(),'job_id':None};save(db,record)
    _checkpoint('authorized')
    user=None
    with jobs.nodes.handoff.transaction() as (db,identities):user=validate(jobs,identifier,db,identities)
    job,reused=jobs.submit(job_request(spec),user,_staged_probe=identifier)
    _checkpoint('submitted')
    return {'id':identifier,'job':job,'historical_receipt_only':False,'reused':reused,'credentials_issued':False}


def dispatch_principal(jobs,job,db=None,identities=None):
    identifier=job.get('_staged_device_probe')
    if not identifier:return jobs.access.user(job['owner'])
    if db is None:
        with jobs.nodes.handoff.transaction() as (current,identity_db):return validate(jobs,identifier,current,identity_db,job)
    if identities is None:
        with jobs.access.connect() as identity_db:return validate(jobs,identifier,db,identity_db,job)
    return validate(jobs,identifier,db,identities,job)


def activation_allowed(jobs,node,owner,device,db):
    """A successful file-access result must still have its original stage authority."""
    record=next((r for r in node.get('device_qualifications',[]) if r['owner']==owner and r['device_id']==device),None)
    if not record:return False
    try:
        job=jobs.get(record['job_id'],db)
        if not job.get('_staged_device_probe') or job['state']!='succeeded':return False
        with jobs.access.connect() as identities:validate(jobs,job['_staged_device_probe'],db,identities,job)
        return True
    except (JobError,ValueError,KeyError,TypeError,sqlite3.Error):return False

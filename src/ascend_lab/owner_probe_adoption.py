"""Explicit completed-probe adoption, bound to one immutable parent onboarding flow."""
import json
import re
from device_qualification import decision,digest
from jobs import JobError,now
from owner_device_probe import TABLE,MAX_RECORDS,request,load,save,validate,initialize as device_initialize

CAPABILITY='owner-probe-orchestration-v1'


def _checkpoint(name):pass


def initialize(jobs):
    device_initialize(jobs)
    with jobs.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS owner_probe_adoptions (flow_id TEXT NOT NULL,node_id TEXT NOT NULL,device_id INTEGER NOT NULL,revision INTEGER NOT NULL,record TEXT NOT NULL,PRIMARY KEY(flow_id,node_id,device_id,revision))')


def _adoption_request(value):
    if (not isinstance(value,dict) or set(value)!={'flow_id','parent_sha256','base_request'}
        or any(not isinstance(value[k],str) or not re.fullmatch('[0-9a-f]{64}',value[k]) for k in ['flow_id','parent_sha256'])):
        raise JobError('Adoption requires the original parent flow and explicit base probe request',400)
    request(value['base_request']);return value


def _adoptions(db,value):
    spec=value['base_request'];rows=db.execute('SELECT record FROM owner_probe_adoptions WHERE flow_id=? AND node_id=? AND device_id=? ORDER BY revision',
        (value['flow_id'],spec['node_id'],spec['device_id'])).fetchall();current=spec;result=[]
    for revision,row in enumerate(rows,1):
        record=json.loads(row[0])
        if (set(record)!={'flow_id','parent_sha256','revision','previous_request','request','job_id','id'}
            or record['flow_id']!=value['flow_id'] or record['parent_sha256']!=value['parent_sha256'] or record['revision']!=revision
            or record['previous_request']!=current or record['id']!=digest({k:v for k,v in record.items() if k!='id'})
            or not isinstance(record['job_id'],str) or not re.fullmatch('job-[0-9a-f]{12}',record['job_id'])):
            raise JobError('Original probe adoption history changed',409)
        new=request(record['request'])
        if new['operation_id']==current['operation_id'] or any(new[k]!=current[k] for k in current if k!='operation_id'):
            raise JobError('Probe adoption cannot change its selected resource or identity contract',409)
        result.append(record);current=new
    if len(result)>8:raise JobError('Probe adoption history exceeds its explicit per-card limit',409)
    return result


def adoption_status(jobs,value):
    value=_adoption_request(value);initialize(jobs)
    with jobs.connect() as db:return {'records':_adoptions(db,value)}


def adopt(jobs,value):
    """Adopt an already-successful explicit new probe, never schedule a retry.

One jobs/identities transaction both fences the previous authorization (even an
orphaned pre-submit intent) and records the parent-flow CAS adoption. This DB
record is the recovery authority if the Root caller loses the response.
"""
    if not isinstance(value,dict) or set(value)!={'flow_id','parent_sha256','base_request','expected_authorization_id','job_id'}:
        raise JobError('Explicit adoption requires one completed job and expected previous authorization',400)
    scope=_adoption_request({k:value[k] for k in ['flow_id','parent_sha256','base_request']})
    if (not isinstance(value['job_id'],str) or not re.fullmatch('job-[0-9a-f]{12}',value['job_id'])
        or not isinstance(value['expected_authorization_id'],str) or not re.fullmatch('[0-9a-f]{64}',value['expected_authorization_id'])):
        raise JobError('Choose one existing completed job and its original expected authority',400)
    initialize(jobs)
    with jobs.nodes.handoff.transaction() as (db,identities):
        chain=_adoptions(db,scope)
        if any(item['job_id']==value['job_id'] for item in chain):return {'records':chain,'historical_receipt_only':True}
        old_spec=chain[-1]['request'] if chain else scope['base_request'];old_id=digest({'request':old_spec})
        if old_id!=value['expected_authorization_id']:raise JobError('Probe adoption authority changed; inspect the original chain before retrying',409)
        if len(chain)>=8 or db.execute('SELECT count(*) FROM owner_probe_adoptions').fetchone()[0]>=MAX_RECORDS:
            raise JobError('Explicit adoption history is full; preserve records for administrator retention',409)
        job=jobs.get(value['job_id'],db);new_id=job.get('_staged_device_probe')
        if not new_id or new_id==old_id or job['state']!='succeeded':raise JobError('Adoption requires a different successful administrator fixed probe',409)
        new=load(db,new_id);new_spec=new['request']
        if any(new_spec[k]!=old_spec[k] for k in old_spec if k!='operation_id'):
            raise JobError('Replacement probe must retain the exact owner, stage, Node, plan, card and memory',409)
        validate(jobs,new_id,db,identities,job)
        node=jobs.nodes.node(new_spec['node_id'],db);qualified=decision(node,new_spec['owner'],new_spec['device_id'])
        proof=next((r for r in node.get('device_qualifications',[]) if r['owner']==new_spec['owner'] and r['device_id']==new_spec['device_id']),{})
        if qualified['status']!='verified' or proof.get('job_id')!=job['id'] or proof.get('attempt_id')!=job.get('attempt_id'):
            raise JobError('Replacement probe has no current matching qualification',409)
        row=db.execute('SELECT 1 FROM '+TABLE+' WHERE id=?',(old_id,)).fetchone()
        if row:
            old=load(db,old_id)
            if old.get('superseded_by'):raise JobError('Previous authorization was superseded by another explicit action',409)
            if old['job_id']:
                from jobs import TERMINAL
                if jobs.get(old['job_id'],db)['state'] not in TERMINAL:
                    raise JobError('Cancel or reconcile the previous queued/running/uncertain probe before adopting a replacement',409)
        else:
            if db.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]>=MAX_RECORDS:raise JobError('Probe history lacks space for the original fenced intent',409)
            old={**new,'id':old_id,'request':old_spec,'job_id':None,'created_at':now()}
        old['superseded_by']=new_id;save(db,old)
        record={'flow_id':scope['flow_id'],'parent_sha256':scope['parent_sha256'],'revision':len(chain)+1,
            'previous_request':old_spec,'request':new_spec,'job_id':job['id']};record['id']=digest(record)
        db.execute('INSERT INTO owner_probe_adoptions VALUES(?,?,?,?,?)',
            (scope['flow_id'],old_spec['node_id'],old_spec['device_id'],record['revision'],json.dumps(record)))
    _checkpoint('adopted')
    return {'records':[*chain,record],'historical_receipt_only':False}

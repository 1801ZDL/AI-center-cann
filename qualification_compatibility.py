"""Audited qualification carry across one exact, authenticated owner append.

The protected context still includes the global revision. Only this approved
transition can bridge it; probe identity, receipt and expiry are never renewed.
"""
import copy
from datetime import datetime
import json
import re
import time

from device_qualification import CAPABILITY,PROBE,context,digest
from jobs import JobError,now
import owner_identity

TABLE='qualification_compatibility'
FORMAT='ascend-lab-qualification-compatibility/v1'
MAX_PLANS=1024
MAX_CANDIDATES=4096
MAX_RECORD=2*1024**2
MAX_BYTES=128*1024**2


def _checkpoint(name):pass


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS '+TABLE+' (plan_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,state TEXT NOT NULL,reserved_bytes INTEGER NOT NULL,record TEXT NOT NULL)')


def compatible(before,after,added):
    """Prove full old identities, slots and every non-append field unchanged."""
    from node_owner_admin import PROJECT,projection
    from quota_workspace import is_quota
    try:
        a=projection(before);b=projection(after)
        if (type(a['policy_revision']) is not int or type(b['policy_revision']) is not int
            or b['policy_revision']!=a['policy_revision']+1
            or set(b['users'])!=set(a['users'])|{added} or added in a['users']
            or set(b['owner_bindings'])!=set(a['owner_bindings'])|{added}
            or {k:v for k,v in before.items() if k not in PROJECT}!={k:v for k,v in after.items() if k not in PROJECT}):
            return False
        mapping=b['users'].pop(added)
        if any(item['uid']==mapping['uid'] for item in a['users'].values()):return False
        binding=b['owner_bindings'].pop(added)
        if set(binding)!={'stage_id','state'} or binding['state']!='ready' or not re.fullmatch('[0-9a-f]{64}',str(binding['stage_id'])):return False
        b['policy_revision']=a['policy_revision']
        for key in ['workspace','persistent_cache']:
            slots=b[key]['slots']
            if len(slots)!=len(a[key]['slots'])+1:return False
            slot=slots.pop()
            if not is_quota(slot) or slot['quota_uid']!=mapping['uid']:return False
        return a==b
    except (KeyError,TypeError,ValueError):return False


def authority(owner,identities):
    row=identities.execute('SELECT profile,disabled FROM users WHERE id=?',(owner,)).fetchone()
    epoch=identities.execute('SELECT revision FROM owner_security_revisions WHERE owner=?',(owner,)).fetchone()
    if not row or row[1] or not epoch or type(epoch[0]) is not int:return None
    profile=json.loads(row[0])
    if (not owner_identity.login_allowed(profile) or profile.get('role')!='user'
        or not {'worker-python','*'}&set(profile.get('environments',[])) or profile.get('quota',{}).get('npu',0)<1):return None
    return {'identity_revision':epoch[0],'profile_sha256':digest(profile)}


def source(jobs,node_id,value,db):
    try:
        job=jobs.get(value['job_id'],db)
        from workflow_spec import runtime_manifest
        return (job['state']=='succeeded' and job.get('template')==PROBE and job['owner']==value['owner']
            and job.get('node_id')==node_id and job.get('attempt_id')==value['attempt_id']
            and job['resources']['npu']==1 and job['allocation']['npu_ids']==[value['device_id']]
            and job.get('_npu_access_context')==value.get('probe_context_sha256',value['context_sha256'])
            and job.get('runtime_files')==runtime_manifest())
    except (JobError,KeyError,TypeError,ValueError):return False


def pending(db,node_id=None):
    return db.execute('SELECT 1 FROM '+TABLE+" WHERE state='pending'"+(' AND node_id=?' if node_id else '')+' LIMIT 1',
        (node_id,) if node_id else ()).fetchone() is not None


def _save(db,plan_id,node_id,state,value,reserved):
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(raw.encode())>reserved or reserved>MAX_RECORD:raise JobError('Qualification compatibility record exceeds its reserved audit budget',409)
    db.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET state=excluded.state,record=excluded.record',
        (plan_id,node_id,state,reserved,raw))


def prepare(jobs,approval,before,db,identities):
    if db.execute('SELECT 1 FROM '+TABLE+' WHERE plan_id=?',(approval['plan_id'],)).fetchone():
        raise JobError('Qualification compatibility intent already exists; replay the original approval',409)
    candidates=[]
    for record in jobs.nodes.qualifications.records(approval['node_id'],db):
        owner=record['owner'];identity=authority(owner,identities)
        if (owner==approval['owner'] or owner not in before['users'] or identity is None
            or record['status']!='verified' or record['expires_at']<=time.time()
            or not source(jobs,approval['node_id'],record,db)):continue
        candidates.append({'source':copy.deepcopy(record),'authority':identity})
    if not candidates:return
    if len(candidates)>MAX_CANDIDATES or not compatible(before,approval['after_profile'],approval['owner']):
        raise JobError('Only an exact append can preserve existing qualifications within the bounded audit budget',409)
    intent={'format':FORMAT,'plan_id':approval['plan_id'],'approval_id':approval['approval_id'],
        'node_id':approval['node_id'],'added_owner':approval['owner'],'before_profile':copy.deepcopy(before),
        'before_profile_sha256':digest(before),'after_profile_sha256':digest(approval['after_profile']),
        'candidates':candidates,'prepared_at':now()}
    value={'intent':intent,'intent_sha256':digest(intent),'completed_at':None,'outcomes':None}
    reserved=len(json.dumps(value,allow_nan=False).encode())+4096+len(candidates)*2048
    count,total=db.execute('SELECT count(*),coalesce(sum(reserved_bytes),0) FROM '+TABLE).fetchone()
    if count>=MAX_PLANS or total+reserved>MAX_BYTES or reserved>MAX_RECORD:
        raise JobError('Explicitly retain/archive qualification compatibility history before more owner appends',409)
    _save(db,approval['plan_id'],approval['node_id'],'pending',value,reserved)


def _intent(value,approval):
    if (not isinstance(value,dict) or set(value)!={'intent','intent_sha256','completed_at','outcomes'}
        or not isinstance(value['intent'],dict) or digest(value['intent'])!=value['intent_sha256']):
        raise JobError('Original qualification compatibility intent changed',409)
    v=value['intent']
    if (set(v)!={'format','plan_id','approval_id','node_id','added_owner','before_profile','before_profile_sha256','after_profile_sha256','candidates','prepared_at'}
        or v['format']!=FORMAT or v['plan_id']!=approval['plan_id'] or v['approval_id']!=approval['approval_id']
        or v['node_id']!=approval['node_id'] or v['added_owner']!=approval['owner']
        or digest(v['before_profile'])!=v['before_profile_sha256'] or v['before_profile_sha256']!=approval['before_profile_sha256']
        or not isinstance(v['candidates'],list) or not 1<=len(v['candidates'])<=MAX_CANDIDATES
        or v['after_profile_sha256']!=digest(approval['after_profile'])
        or not compatible(v['before_profile'],approval['after_profile'],approval['owner'])):
        raise JobError('Qualification compatibility no longer describes the original approved append',409)
    return v


def observe(jobs,approval,profile,report,db,identities):
    row=db.execute('SELECT state,reserved_bytes,record FROM '+TABLE+' WHERE plan_id=?',(approval['plan_id'],)).fetchone()
    if not row or row[0]!='pending':return
    value=json.loads(row[2]);intent=_intent(value,approval)
    if (approval['state']!='ready' or not approval.get('node_online_seen')
        or digest(profile)!=intent['after_profile_sha256']):
        raise JobError('Wait for the exact ready Node before deciding qualification compatibility',409)
    if jobs.inventory.reason(profile['physical_id'],db):return
    from workflow_spec import runtime_manifest
    runtime=runtime_manifest();host=report.get('access_host_sha256');npu=report.get('npu',{})
    try:fresh=0<=time.time()-datetime.fromisoformat(npu['sampled_at']).timestamp()<=15
    except (KeyError,TypeError,ValueError):fresh=False
    healthy=(CAPABILITY in report.get('capabilities',[]) and report.get('access_runtime_files')==runtime
        and isinstance(host,str) and re.fullmatch('[0-9a-f]{64}',host) and fresh and npu.get('status')=='ok')
    outcomes=[]
    for candidate in intent['candidates']:
        if not isinstance(candidate,dict) or set(candidate)!={'source','authority'}:
            raise JobError('Invalid original qualification compatibility candidate',409)
        old=candidate['source'];owner=old['owner'];device=old['device_id'];reason=None
        current=db.execute('SELECT record FROM device_qualifications WHERE node_id=? AND owner=? AND device_id=?',
            (approval['node_id'],owner,device)).fetchone()
        if not current or digest(json.loads(current[0]))!=digest(old):reason='The original qualification changed; no replacement was overwritten'
        elif authority(owner,identities)!=candidate['authority']:reason='The original owner authority changed, including monotonic revisions'
        elif old['expires_at']<=time.time() or not source(jobs,approval['node_id'],old,db):reason='The original qualification expired or lost its successful source'
        elif not healthy or not any(d['id']==device and d.get('health')=='OK' for d in npu.get('devices',[])):
            reason='Current protected host/runtime and healthy device observations do not prove compatibility'
        else:
            expected=context(intent['before_profile'],approval['node_id'],owner,device,host,runtime)
            if expected!=old['context_sha256']:reason='The original host/runtime/environment/access context changed'
        outcome={'owner':owner,'device_id':device,'source_job':old['job_id'],'source_record_sha256':digest(old),
            'source_context_sha256':old['context_sha256'],'expires_at':old['expires_at'],'state':'skipped' if reason else 'carried','reason':reason}
        if not reason:
            new_context=context(profile,approval['node_id'],owner,device,host,runtime)
            outcome['context_sha256']=new_context
            carried={**old,'context_sha256':new_context,'probe_context_sha256':old.get('probe_context_sha256',old['context_sha256']),
                'compatibility':{'plan_id':approval['plan_id'],'intent_sha256':value['intent_sha256'],'source_record_sha256':digest(old)}}
            db.execute('UPDATE device_qualifications SET record=? WHERE node_id=? AND owner=? AND device_id=?',
                (json.dumps(carried),approval['node_id'],owner,device))
        outcomes.append(outcome);_checkpoint('candidate')
    value.update(completed_at=now(),outcomes=outcomes)
    _save(db,approval['plan_id'],approval['node_id'],'complete',value,row[1]);_checkpoint('complete')
    db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
        (now(),approval['node_id'],'qualification-compatibility',profile['policy_revision'],json.dumps({
            'kind':'qualification-compatible-append','plan_id':approval['plan_id'],'intent_sha256':value['intent_sha256'],
            'carried':sum(v['state']=='carried' for v in outcomes),'skipped':sum(v['state']=='skipped' for v in outcomes)})))


def status(jobs,node_id,limit=20,offset=0):
    if not isinstance(node_id,str) or not re.fullmatch('worker-[0-9a-f]{12}',node_id):raise JobError('Choose one registered Node',400)
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=MAX_PLANS:raise JobError('Choose bounded audit pagination',400)
    with jobs.connect() as db:
        if not db.execute('SELECT 1 FROM worker_nodes WHERE id=?',(node_id,)).fetchone():raise JobError('Unknown Node',404)
        rows=db.execute('SELECT plan_id,state,record FROM '+TABLE+' WHERE node_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (node_id,limit+1,offset)).fetchall();records=[]
        for plan,state,encoded in rows[:limit]:
            v=json.loads(encoded);outcomes=v['outcomes'] or []
            records.append({'plan_id':plan,'state':state,'intent_sha256':v['intent_sha256'],'completed_at':v['completed_at'],
                'candidates':len(v['intent']['candidates']),'carried':sum(v['state']=='carried' for v in outcomes),
                'skipped':sum(v['state']=='skipped' for v in outcomes)})
    return {'format':FORMAT,'node_id':node_id,'records':records,'more':len(rows)>limit}

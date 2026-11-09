"""Finite offline retention selection. Produces an approval, never applies one."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import time

FORMAT='ascend-lab-maintenance-policy/v1'
DEFAULT={
    'format':FORMAT,'enabled':False,'min_age_seconds':604800,
    'keep_recent_per_owner':5,'control_high_watermark':1000,'target_control_jobs':900,
    'max_jobs':16,'scan_rows':128,'hold_job_ids':[],
    'selection_start_minute_utc':120,'selection_window_minutes':60,
    'archive_budget':{'bytes':16*1024**2,'exports':32},
}
MAX_SCAN_BYTES=512*1024
MAX_JOB_BYTES=64*1024


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def policy(value=None):
    if value is None:return json.loads(json.dumps(DEFAULT))
    if not isinstance(value,dict) or set(value)-set(DEFAULT):raise ValueError('Invalid maintenance selection policy')
    result={**json.loads(json.dumps(DEFAULT)),**value}
    if result['format']!=FORMAT or type(result['enabled']) is not bool:raise ValueError('Invalid maintenance policy identity')
    bounds={'min_age_seconds':(60,31536000),'keep_recent_per_owner':(0,32),
            'control_high_watermark':(1,10000),'target_control_jobs':(0,9999),
            'max_jobs':(1,32),'scan_rows':(1,256),'selection_start_minute_utc':(0,1439),
            'selection_window_minutes':(1,180)}
    for key,(low,high) in bounds.items():
        if type(result[key]) is not int or not low<=result[key]<=high:raise ValueError('Invalid maintenance bound: '+key)
    if result['target_control_jobs']>=result['control_high_watermark']:raise ValueError('Maintenance target must be below high watermark')
    held=result['hold_job_ids']
    if not isinstance(held,list) or len(held)>128 or any(not isinstance(v,str) for v in held) or len(set(held))!=len(held):raise ValueError('Invalid maintenance hold list')
    from control_history import selection,budget
    for ident in held:selection([ident])
    budget(result['archive_budget'])
    return json.loads(json.dumps(result))


@contextmanager
def sql_budget(db):
    """Fresh offline connection only; cap all scans, including copy planning."""
    calls=0
    def stop():
        nonlocal calls
        calls+=1
        return int(calls>2000)
    db.set_progress_handler(stop,1000)
    try:yield
    finally:db.set_progress_handler(None,0)


def window(settings,epoch):
    if type(epoch) not in {int,float} or not math.isfinite(epoch) or not 0<=epoch<=253402300799:raise ValueError('Invalid selection time')
    current=dt.datetime.fromtimestamp(epoch,dt.timezone.utc)
    minute=current.hour*60+current.minute
    return (minute-settings['selection_start_minute_utc'])%1440<settings['selection_window_minutes']


def select(db,value,*,epoch,after_rowid=0):
    """Must run in the caller's original stopped-controller transaction."""
    settings=policy(value)
    if type(after_rowid) is not int or not 0<=after_rowid<2**63:raise ValueError('Invalid maintenance cursor')
    result={'policy_sha256':hashlib.sha256(encode(settings)).hexdigest(),'selected':[],
            'scanned_rows':0,'scanned_bytes':0,'next_after_rowid':None,'skipped':{},
            'writes':False,'starts_workload':False,'automatic_execution':False}
    if not settings['enabled']:return {**result,'status':'disabled'}
    if not window(settings,epoch):return {**result,'status':'outside_selection_window'}
    import history_capacity as capacity
    import control_rotation as rotation
    from jobs import TERMINAL
    if capacity.current(db) is None:raise ValueError('Enable separate history capacity before retention selection')
    total=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
    migrated=rotation.count(db)
    if not 0<=migrated<=total<=capacity.MAX_JOBS+rotation.MAX_INDEX:raise ValueError('History inventory exceeds selection bound')
    # Existing compact receipts still count against their own finite index.
    result['control_jobs']=total-migrated
    if total-migrated<settings['control_high_watermark']:return {**result,'status':'below_high_watermark'}
    count=min(settings['max_jobs'],total-migrated-settings['target_control_jobs'])
    query='SELECT rowid,id,owner,state,CASE WHEN length(CAST(data AS BLOB))<=? THEN data ELSE NULL END FROM jobs WHERE rowid>? ORDER BY rowid LIMIT ?'
    rows=db.execute(query,(MAX_JOB_BYTES,after_rowid,settings['scan_rows']+1))
    recent={};last=after_rowid
    def skip(reason):result['skipped'][reason]=result['skipped'].get(reason,0)+1
    for index,(rowid,ident,owner,state,raw) in enumerate(rows):
        if index==settings['scan_rows']:
            result['next_after_rowid']=last;break
        last=rowid;result['scanned_rows']+=1
        if raw is None:skip('oversized_metadata');continue
        result['scanned_bytes']+=len(raw.encode())
        if result['scanned_bytes']>MAX_SCAN_BYTES:raise ValueError('Maintenance metadata scan exceeds512KiB')
        job=capacity.decode((ident,owner,state,raw))
        if rotation.REF in job:skip('already_migrated');continue
        if state not in TERMINAL:skip('non_terminal');continue
        if ident in settings['hold_job_ids']:skip('explicit_hold');continue
        if owner not in recent:
            recent[owner]={r[0] for r in db.execute('SELECT id FROM jobs WHERE owner=? ORDER BY rowid DESC LIMIT ?',
                                                  (owner,settings['keep_recent_per_owner']))}
        if ident in recent[owner]:skip('recent_owner_job');continue
        try:
            finished=dt.datetime.fromisoformat(job['finished_at'].replace('Z','+00:00'))
            if finished.tzinfo is None:raise ValueError()
            age=epoch-finished.timestamp()
        except (KeyError,TypeError,ValueError,AttributeError,OverflowError):
            skip('unknown_completion_time');continue
        if age<settings['min_age_seconds']:skip('retention_age');continue
        proof=capacity.online_proof(db,job)
        if proof['kind']=='online':skip('original_not_retired');continue
        slot=db.execute('SELECT online,proof FROM history_capacity_slots WHERE job_id=?',(ident,)).fetchone()
        if slot!=(0,capacity.encode(proof).decode()):raise ValueError('Original retirement and slot receipt disagree')
        result['selected'].append(ident)
        if len(result['selected'])==count:
            result['next_after_rowid']=rowid;break
    result.update(status='selected' if result['selected'] else ('continue_scan' if result['next_after_rowid'] else 'no_eligible_jobs'))
    result['selected'].sort()
    return result


def plan(prefix,value,*,after_rowid=0,epoch=None,execution=None):
    """Root/matching release/stopped state remain the original copy guards."""
    import control_history as copies
    from maintenance_runner import plan as maintenance_plan,execution_contract,MAX_SOURCE_BYTES
    settings=policy(value);epoch=time.time() if epoch is None else epoch
    if execution is not None:execution=execution_contract(execution)
    # Disabled/out-of-window previews do not open the controller database.
    if not settings['enabled'] or not window(settings,epoch):
        return select(None,settings,epoch=epoch,after_rowid=after_rowid)
    with copies.context(prefix) as (db,binding,meta),sql_budget(db):
        result=select(db,settings,epoch=epoch,after_rowid=after_rowid)
        if result['selected']:
            original=copies.plan_in_context(db,binding,meta,result['selected'],settings['archive_budget'])
            if original['source']['bytes']>MAX_SOURCE_BYTES:raise ValueError('Selected related history exceeds256KiB; reduce max_jobs')
            result['maintenance_plan']=maintenance_plan(prefix,[{'action':action,'plan':original}
                for action in ['control_copy','control_verify','control_rotate']],execution=execution)
        return result

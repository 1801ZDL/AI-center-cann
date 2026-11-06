"""Bounded pre-payload deferral for the cooperative executor, not Root attestation."""
import datetime as dt
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import time

from jobs import atomic_json,read_json,identity,now

SCHEMA='ascend-lab-cooperative-prestart/v1'
DEFERRED='prestart_deferred'
MAX_DEFERRALS=16
SOURCES=('cooperative_start.py','executor.py','npu_admission.py','jobs.py')


def contract():
    root=Path(__file__).resolve().parent
    return {'schema':SCHEMA,'max_deferrals':MAX_DEFERRALS,
            'sources':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in SOURCES}}


def enabled(job):
    value=job.get('_cooperative_start')
    return (isinstance(value,dict) and value.get('schema')==SCHEMA and value.get('max_deferrals')==MAX_DEFERRALS
        and isinstance(value.get('sources'),dict) and set(value['sources'])==set(SOURCES)
        and all(isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) for v in value['sources'].values())
        and job.get('_environment',{}).get('type') in {'host','docker'} and not job.get('_remote')
        and bool(job.get('allocation',{}).get('npu_ids')))


def folder(directory,job):
    if not re.fullmatch('attempt-[a-f0-9]{32}',job.get('attempt_id','')):raise ValueError('Invalid prestart attempt')
    return Path(directory)/'prestart'/job['attempt_id']


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def save(path,value):
    path=Path(path);path.parent.parent.mkdir(mode=0o700,exist_ok=True);sync_directory(path.parent.parent.parent)
    path.parent.mkdir(mode=0o700,exist_ok=True);sync_directory(path.parent.parent)
    atomic_json(path,value);sync_directory(path.parent)


def binding(job):
    return {key:job[key] for key in ['id','attempt_id','node_id','_token','_cooperative_start']}


def intent(directory,job):
    if not enabled(job):return
    if job['_cooperative_start']!=contract():raise RuntimeError('Cooperative executor sources changed before launch')
    path=folder(directory,job)/'launch.json'
    if os.path.lexists(path):raise RuntimeError('This attempt already recorded a payload launch intent')
    save(path,{**binding(job),'schema':SCHEMA,'recorded_at':now(),'producer':identity(os.getpid())})


def defer(directory,job):
    if not enabled(job) or job['_cooperative_start']!=contract():return False
    base=folder(directory,job)
    if os.path.lexists(base/'launch.json') or os.path.lexists(Path(directory)/'command-process.json'):return False
    save(base/'candidate.json',{**binding(job),'schema':SCHEMA,'reason_code':'npu_observation_changed',
                              'deferred_at':now(),'producer':identity(os.getpid()),'payload_started':False})
    return True


def candidate(directory,job):
    if not enabled(job):return None
    try:base=folder(directory,job)
    except ValueError:return None
    value=read_json(base/'candidate.json')
    if (not isinstance(value,dict) or any(value.get(k)!=v for k,v in binding(job).items())
        or value.get('schema')!=SCHEMA or value.get('reason_code')!='npu_observation_changed'
        or value.get('payload_started') is not False or not isinstance(value.get('producer'),dict)
        or os.path.lexists(base/'launch.json') or os.path.lexists(Path(directory)/'command-process.json')):return None
    try:
        stamp=dt.datetime.fromisoformat(value['deferred_at'])
        if stamp.tzinfo is None:return None
    except (KeyError,TypeError,ValueError):return None
    return value


def acknowledge(directory,job,transport):
    value=candidate(directory,job)
    if value is None:raise RuntimeError('Prestart deferral has no matching no-launch candidate')
    if transport not in {'host','docker-exec-exit0'}:raise ValueError('Unconfirmed prestart transport')
    result={'status':DEFERRED,'started_at':None,'finished_at':None,'exit_code':None,
            'reason':'NPU 启动前观测改变；负载尚未启动，等待原任务回队',
            'failure_stage':'prestart_admission','prestart':{**value,'transport':transport,
            'supervisor':identity(os.getpid()),'candidate_sha256':hashlib.sha256((folder(directory,job)/'candidate.json').read_bytes()).hexdigest()}}
    atomic_json(Path(directory)/'completion.json',{'job_id':job['id'],'token':job['_token'],
        'attempt_id':job['attempt_id'],'node_id':job['node_id'],'result':result});sync_directory(directory)


def definitely_stopped(saved):
    """Permission/observation errors are unknown, not proof of process death."""
    if not isinstance(saved,dict) or any(type(saved.get(k)) is not int for k in ['pid','uid','start_ticks']):return False
    if saved['pid']<=0 or saved['uid']!=os.getuid():return False
    try:
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:return False
    if not isinstance(saved.get('boot_id'),str) or not saved['boot_id']:return False
    if boot!=saved['boot_id']:return True
    try:
        path=Path('/proc')/str(saved['pid']);text=(path/'stat').read_text();owner=path.stat().st_uid
        fields=text.rsplit(')',1)[1].split()
        return fields[0]=='Z' or int(fields[19])!=saved['start_ticks']
    except FileNotFoundError:return True
    except (OSError,ValueError,IndexError):return False


def reconcile(store,db,job,result):
    """Return pending/invalid/queued/exhausted; caller retains unknown resources."""
    directory=store.directory(job['id']);value=candidate(directory,job);proof=result.get('prestart')
    if value is None or not isinstance(proof,dict):return 'invalid'
    if any(proof.get(k)!=v for k,v in value.items()):return 'invalid'
    if proof.get('candidate_sha256')!=hashlib.sha256((folder(directory,job)/'candidate.json').read_bytes()).hexdigest():return 'invalid'
    supervisor=read_json(directory/'supervisor.json')
    if not isinstance(supervisor,dict) or supervisor.get('token')!=job['_token']:return 'invalid'
    if proof.get('supervisor')!={k:v for k,v in supervisor.items() if k!='token'}:return 'invalid'
    transport=proof.get('transport')
    if job['_environment']['type']=='host':
        if transport!='host' or proof['producer']!=proof['supervisor']:return 'invalid'
    else:
        ready=read_json(directory/'inside-ready.json')
        if (transport!='docker-exec-exit0' or not isinstance(ready,dict) or ready.get('token')!=job['_token']
            or proof['producer']!={k:v for k,v in ready.items() if k!='token'}):return 'invalid'
    if not definitely_stopped(proof['supervisor']):return 'pending'
    if os.path.lexists(directory/'npu-admission.sock'):return 'invalid'
    count=job.get('_cooperative_deferral_count',0)
    if type(count) is not int or not 0<=count<MAX_DEFERRALS:return 'invalid'
    count+=1
    job['_cooperative_deferral_count']=count
    job['prestart_deferrals']={'count':count,'last_attempt_id':job['attempt_id'],
                              'last_deferred_at':value['deferred_at'],'payload_started':False}
    cancelled=job['state']=='cancelling' or (read_json(directory/'cancel.json') or {}).get('token')==job['_token']
    if cancelled or count>=MAX_DEFERRALS:
        from executor import finish
        terminal={'status':'cancelled' if cancelled else 'failed','started_at':None,'exit_code':None,
                  'failure_stage':'prestart_cancelled' if cancelled else 'prestart_retry_limit',
                  'reason':'用户取消，负载尚未启动' if cancelled else '启动前资源竞争已达16次上限；负载从未启动，请检查共享资源后明确重新提交'}
        finish(directory,job,terminal)
        job.update(state=terminal['status'],result=terminal,reason=terminal['reason'],started_at=None,finished_at=terminal['finished_at'])
        store.write(db,job);return 'exhausted'
    db.execute('UPDATE execution_attempts SET state=? WHERE id=? AND job_id=?',(DEFERRED,job['attempt_id'],job['id']))
    for key in ['attempt_id','node_id','_launch_attempted','_reserved_epoch','reserved_at','heartbeat_at','_aging_target']:job.pop(key,None)
    job.update(state='queued',allocation={},result=None,started_at=None,finished_at=None,
        reason='NPU 启动前占用或健康改变；原任务退避等待新观测',_token=secrets.token_hex(24),
        _prestart_not_before=time.time()+min(60,5*2**min(count-1,4)),_prestart_sample_after=value['deferred_at'])
    store.write(db,job);return 'queued'


def wait_reason(job,snapshot):
    if not job.get('_cooperative_deferral_count'):return None
    until=job.get('_prestart_not_before')
    if type(until) not in {int,float} or not math.isfinite(until) or time.time()<until:return '启动前资源竞争后退避等待；不会重跑已启动的负载'
    try:
        sample=dt.datetime.fromisoformat(snapshot['npu']['sampled_at']);previous=dt.datetime.fromisoformat(job['_prestart_sample_after'])
        if sample<=previous:return '等待资源竞争之后的新 NPU 观测'
    except (KeyError,TypeError,ValueError):return '等待资源竞争之后的新 NPU 观测'
    return None

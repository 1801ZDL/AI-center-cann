"""Age-aware owner rotation and short-lived, persisted admission intent.

Intent pauses new grants only. It never grants, releases or preempts resources.
It is recorded only after the real placement code proves that a queued job could
run with this entry idle, under current observations and administrator policy.
"""
from __future__ import annotations

import datetime as dt
import json
import time

PROOF_SECONDS=15
WAIT_REASON='等待较早任务积累足够资源；暂停插队，不中断已有任务'


def threshold(policy):
    value=policy.get('aging_seconds',300)
    if type(value) is not int or not 1<=value<=86400:
        raise ValueError('aging_seconds must be an integer between 1 and 86400')
    return value


def age(job,epoch=None):
    try:
        created=dt.datetime.fromisoformat(job['created_at'])
        if created.tzinfo is None:return 0
        return max(0,(time.time() if epoch is None else epoch)-created.timestamp())
    except (KeyError,TypeError,ValueError,OverflowError):return 0


def ordered(rows,policy,epoch=None):
    epoch=time.time() if epoch is None else epoch;limit=threshold(policy);last={}
    for job in rows:
        if job.get('reserved_at'):last[job['owner']]=max(last.get(job['owner'],''),job['reserved_at'])
    def key(job):
        if age(job,epoch)>=limit:return (0,job['created_at'],job['id'])
        return (1,last.get(job['owner'],''),job['created_at'],job['id'])
    return sorted((job for job in rows if job['state']=='queued'),key=key)


def valid_target(jobs,job,db,epoch=None):
    epoch=time.time() if epoch is None else epoch;target=job.get('_aging_target')
    if not isinstance(target,dict) or not isinstance(target.get('checked_epoch'),(int,float)):return None
    if not 0<=epoch-target['checked_epoch']<=PROOF_SECONDS:return None
    domain=jobs.resources.get(target['physical_id'],db)
    if domain['draining'] or domain['revision']!=target['domain_revision']:return None
    if target['node_id']==jobs.backend.node_id:
        if not jobs.policy['local_enabled'] or target['physical_id']!=jobs.backend.physical_id:return None
    else:
        row=db.execute('SELECT profile,last_seen,disabled FROM worker_nodes WHERE id=?',(target['node_id'],)).fetchone()
        if not row or row[2] or not 0<=epoch-row[1]<=30:return None
        profile=json.loads(row[0])
        if (profile['physical_id']!=target['physical_id'] or profile.get('policy_revision',1)!=target['node_revision']
            or job['owner'] not in profile['users'] or job.get('template') not in profile['templates']):return None
    return target


def note_wait(jobs,job,node_id,domain,node_revision,db,eligible_when_idle):
    """Only a fresh successful idle placement may establish/refresh protection."""
    target=job.get('_aging_target');current=valid_target(jobs,job,db)
    if not eligible_when_idle or age(job)<threshold(jobs.policy):
        if target and target.get('node_id')==node_id:job.pop('_aging_target',None)
        return
    # Protect one physical domain per queued job. Another entry may still run it.
    if current and current['node_id']!=node_id:return
    job['_aging_target']={'node_id':node_id,'physical_id':domain['physical_id'],
        'domain_revision':domain['revision'],'node_revision':node_revision,'checked_epoch':time.time()}


def protected(jobs,rows,physical_id,db,identities=None):
    from jobs import ACTIVE
    epoch=time.time()
    active=[job for job in rows if job['state'] in ACTIVE]
    for job in ordered(rows,jobs.policy,epoch):
        if age(job,epoch)<threshold(jobs.policy):break
        target=valid_target(jobs,job,db,epoch)
        if target and target['physical_id']==physical_id and not jobs.dispatch_permission(job,active,db,identities):return job
    return None

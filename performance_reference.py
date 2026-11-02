"""Explicit empirical references for the fixed NPU microcheck recipe.

References compare reported Event samples under a matching protected context.
They do not certify device health, stable clocks or arbitrary operator speedups.
"""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

from jobs import JobError,ACTIVE,now
from device_qualification import digest

CAPABILITY='fixed-performance-reference-v1'
MAX_REFERENCES=32
MAX_EVENTS=2048


def epoch(value):
    if not isinstance(value,str) or len(value)>64:raise ValueError('Invalid execution clock')
    value=dt.datetime.fromisoformat(value)
    if value.tzinfo is None:raise ValueError('Execution clock needs timezone')
    return value.timestamp()
def finite(value):
    try:return type(value) in {float,int} and math.isfinite(value)
    except OverflowError:return False
def median(values):
    ordered=sorted(values);lo=ordered[(len(ordered)-1)//2];hi=ordered[len(ordered)//2]
    return lo+(hi-lo)/2
def approval_sha(value):return digest({k:value[k] for k in ['id','revision','approval','approved_at','spec','context','sources','center_us','limits_us','expires_at']})
def event(db,value,action,restoring=False):
    if db.execute('SELECT count(*) FROM performance_reference_events').fetchone()[0]>=MAX_EVENTS-(0 if restoring else MAX_REFERENCES):
        raise JobError('Performance reference event history full; explicit maintenance required',409)
    record={'reference_id':value['id'],'revision':value['revision'],'state':value['state'],'action':action,
        'at':now(),'administrator_uid':os.geteuid(),'approval_sha256':approval_sha(value)}
    db.execute('INSERT INTO performance_reference_events(record) VALUES(?)',(json.dumps(record),))


def specification(value):
    from auth import IDENTIFIER
    if not isinstance(value,dict) or set(value)!={'name','source_job_ids','ratio_bounds','max_round_ratio','max_age_seconds'}:raise JobError('Provide the complete fixed performance reference definition')
    if not isinstance(value['name'],str) or not IDENTIFIER.fullmatch(value['name']):raise JobError('Invalid performance reference name')
    sources=value['source_job_ids'];bounds=value['ratio_bounds'];spread=value['max_round_ratio']
    if (not isinstance(sources,list) or not 3<=len(sources)<=8 or len(set(map(str,sources)))!=len(sources)
        or any(not isinstance(j,str) or not re.fullmatch('job-[0-9a-f]{12}',j) for j in sources)):raise JobError('Choose 3..8 distinct original fixed microcheck jobs')
    if not isinstance(bounds,list) or len(bounds)!=2 or not all(finite(v) for v in bounds) or not 0<bounds[0]<1<bounds[1]<=4:raise JobError('Explicit finite ratio bounds must straddle one, with upper bound at most four')
    if not finite(spread) or not 1<=spread<=4:raise JobError('Approve an explicit round/inter-run dispersion bound in 1..4')
    if type(value['max_age_seconds']) is not int or not 60<=value['max_age_seconds']<=604800:raise JobError('Reference age must be 60..604800 seconds from the oldest source execution')
    return {**value,'source_job_ids':sorted(sources)}


def invalidate_restored(path):
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_references'").fetchone():return
        for identifier,raw in db.execute('SELECT id,record FROM performance_references').fetchall():
            value=json.loads(raw)
            if value['state']!='retired':
                value.update(state='restore_paused',approval=None,revision=value['revision']+1)
                event(db,value,'restore-frozen',True)
                db.execute('UPDATE performance_references SET record=? WHERE id=?',(json.dumps(value),identifier))


def require_restore_history(current,staged,role):
    if role!='controller':return
    from deployment import DeployError
    def read(directory):
        path=Path(directory)/'jobs.sqlite3'
        if not path.exists():return {},{}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_references'").fetchone():return {},{}
            return {i:json.loads(raw) for i,raw in db.execute('SELECT id,record FROM performance_references')},dict(db.execute('SELECT id,record FROM performance_reference_events'))
    old,events=read(current);new,other=read(staged)
    immutable=['spec','sources','context','center_us','limits_us','expires_at']
    if any(i not in new or any(new[i].get(k)!=v.get(k) for k in immutable) or new[i]['revision']<v['revision'] for i,v in old.items()):
        raise DeployError('Retained performance reference history would be lost or rewound')
    if any(other.get(i)!=raw for i,raw in events.items()):raise DeployError('Retained performance reference approval history would be lost')
    if len(other)+sum(v['state']!='retired' for v in new.values())>MAX_EVENTS:raise DeployError('Performance reference restore event history full')


class References:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS performance_references(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS performance_reference_events(id INTEGER PRIMARY KEY,record TEXT NOT NULL)')

    def get(self,identifier,db):
        row=db.execute('SELECT record FROM performance_references WHERE id=?',(identifier,)).fetchone()
        if row is None:raise JobError('Performance reference not found',404)
        return json.loads(row[0])

    def context(self,job,db,identities):
        from device_evidence import hardware
        from environment_contract import fingerprint
        from npu_measurement import source_sha as measurement_sha
        from npu_selfcheck import source_sha,reference_sha
        node=self.jobs.nodes.node(job['node_id'],db);profile=node['profile'];owner=job['owner']
        row=identities.execute('SELECT disabled,profile FROM users WHERE id=?',(owner,)).fetchone()
        revision=identities.execute('SELECT revision FROM owner_security_revisions WHERE owner=?',(owner,)).fetchone()
        if not row or row[0] or not revision or owner not in profile['users']:raise JobError('Original performance source identity is unavailable',409)
        if (not self.jobs.device_schedules.fresh(node,epoch(now())) or not profile.get('environment_manifest')
            or job.get('node_policy_revision')!=profile.get('policy_revision',1)
            or job.get('environment_sha256')!=fingerprint(profile['environment_manifest'])
            or node['report'].get('measurement_source_sha256')!=measurement_sha()
            or job.get('_measurement_contract',{}).get('source_sha256')!=measurement_sha()):raise JobError('Original performance source Node/environment/window context changed',409)
        card=job['allocation']['npu_ids'][0];hw=hardware(node,card)
        if hw!=job.get('_device_hardware_sha256'):raise JobError('Original performance hardware context changed',409)
        return {'node_id':node['id'],'owner':owner,'project':job['project'],'owner_revision':revision[0],
            'owner_profile_sha256':digest(json.loads(row[1])),'node_profile_sha256':digest(profile),
            'physical_id':profile['physical_id'],'device_id':card,'hardware_sha256':hw,
            'environment_sha256':job['environment_sha256'],'driver_sha256':source_sha(),'reference_sha256':reference_sha(),
            'measurement_source_sha256':measurement_sha(),'resources':job['resources']}

    def source(self,job,db,identities):
        from archive_api import open_artifact
        from npu_selfcheck import DRIVER,REPORT,source_sha,validate_report
        row=job.get('workflow_result') or {};parameters=job.get('parameters',{})
        if (job['state']!='succeeded' or job.get('template')!='accuracy_v1' or job.get('execution_backend')!='managed-docker'
            or job['resources']['npu']!=1 or len(job['allocation'].get('npu_ids',[]))!=1 or not job.get('_artifact_receipt')
            or not all(row.get(k) is True for k in ['passed','environment_locked','command_executed','device_access_confirmed'])
            or parameters.get('argv')!=['python','-I','-B',DRIVER,'--run'] or parameters.get('input_mode')!='readonly'
            or job.get('placement')!={'node_id':job['node_id'],'device_ids':job['allocation']['npu_ids']}
            or job.get('input',{}).get('files')!=[{'name':DRIVER,'size':Path(__file__).with_name(DRIVER).stat().st_size,'sha256':source_sha(),'executable':False}]):
            raise JobError('Choose original completed fixed managed NPU microchecks with protected execution evidence',409)
        conditions=job.get('device_conditions') or {};measured=conditions.get('measurement') or {}
        windows=[w for w in measured.get('windows',[]) if w.get('id')=='mm512_fp16']
        if (conditions.get('status')!='sampled_no_alerts' or measured.get('status')!='recorded' or len(windows)!=1
            or windows[0].get('status')!='boundary_observations_only' or windows[0].get('heartbeat_retention_incomplete')
            or measured.get('job_id')!=job['id'] or measured.get('attempt_id')!=job['attempt_id']
            or measured.get('node_id')!=job['node_id'] or measured.get('physical_id')!=job.get('_physical_id')
            or measured.get('device_ids')!=job['allocation']['npu_ids']
            or measured.get('source_sha256')!=job.get('_measurement_contract',{}).get('source_sha256')):
            raise JobError('Fixed performance source needs confirmed Node markers and no observed adverse/unknown conditions',409)
        completed=(job.get('result') or {}).get('execution_finished_at')
        try:
            if not epoch(job['reserved_at'])-15<=epoch(completed)<=epoch(now())+1:raise ValueError()
            self.jobs.lifecycle.available('archive',job['id'],db)
            self.jobs.lifecycle.available('input',job['input']['id'],db)
            with open_artifact(self.jobs.directory(job['id']),REPORT,128*1024) as stream:raw=stream.read()
            if hashlib.sha256(raw).hexdigest()!=job.get('_fixed_microcheck_report_sha256'):raise ValueError()
            report=json.loads(raw);checked=validate_report(report);timing=checked['performance']
            marker=report.get('timing',{}).get('measurement_window',{})
            if (not checked['precision_passed'] or timing.get('samples')!=30 or marker!={'id':'mm512_fp16','begin':'open','end':'closed'}
                or not finite(timing.get('round_median_ratio'))):raise ValueError()
            medians=timing['round_medians_us']
            if len(medians)!=3 or any(not finite(v) or v<=0 for v in medians):raise ValueError()
            event_seconds=math.fsum(v/1000000 for round_values in report['timing']['rounds_us'] for v in round_values)
            window_seconds=windows[0].get('duration_seconds')
            # This fixed driver synchronizes each of its thirty non-overlapping
            # Event pairs before asking Node to close the marker interval.
            if not finite(window_seconds) or not 0<event_seconds<=window_seconds:raise ValueError()
        except (ValueError,KeyError,TypeError,OSError,OverflowError):raise JobError('Invalid or unavailable original fixed performance observations',409) from None
        context=self.context(job,db,identities)
        return {'job_id':job['id'],'attempt_id':job['attempt_id'],'input_id':job['input']['id'],'report_sha256':hashlib.sha256(raw).hexdigest(),
            'execution_finished_at':completed,'context':context,'round_medians_us':medians,
            'round_median_ratio':timing['round_median_ratio'],'job_median_us':median(medians),
            'total_event_seconds':event_seconds,'node_window_seconds':window_seconds,
            'frequency_samples_in_window':windows[0].get('frequency_samples_in_window',0),
            'performance_qualified':False}

    def capture(self,job,db,identities):
        try:return {'status':'captured','sample':self.source(job,db,identities)}
        except (JobError,ValueError,TypeError,KeyError):return {'status':'unconfirmed'}

    def create(self,value):
        spec=specification(value)
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            old=db.execute('SELECT id FROM performance_references WHERE name=?',(spec['name'],)).fetchone()
            if old:
                record=self.get(old[0],db)
                if record['spec']!=spec:raise JobError('Reference name already has another immutable definition',409)
                return record
            if db.execute('SELECT count(*) FROM performance_references').fetchone()[0]>=MAX_REFERENCES:raise JobError('Performance reference history full',409)
            sources=[self.source(self.jobs.get(identifier,db),db,identities) for identifier in spec['source_job_ids']]
            context=sources[0]['context']
            if any(s['context']!=context for s in sources) or len({s['attempt_id'] for s in sources})!=len(sources):raise JobError('References need independent attempts in one exact device/environment/source identity context',409)
            if any(s['round_median_ratio']>spec['max_round_ratio'] for s in sources):raise JobError('Reference source rounds exceed the explicitly approved dispersion bound',409)
            centers=[s['job_median_us'] for s in sources]
            if max(centers)/min(centers)>spec['max_round_ratio']:raise JobError('Independent reference runs exceed the approved dispersion bound',409)
            center=median(centers);limits=[center*r for r in spec['ratio_bounds']]
            if any(not finite(v) or v<=0 for v in limits) or not limits[0]<center<limits[1]:raise JobError('Reference limits are not representable positive finite durations',409)
            expires=min(epoch(s['execution_finished_at']) for s in sources)+spec['max_age_seconds']
            if epoch(now())>=expires:raise JobError('Reference sources have already expired',409)
            record={'id':'perfref-'+uuid.uuid4().hex[:24],'spec':spec,'sources':sources,'context':context,'center_us':center,
                'limits_us':limits,'expires_at':expires,'state':'draft','revision':0,'approval':None,'approved_at':None,'created_at':now()}
            db.execute('INSERT INTO performance_references VALUES(?,?,?)',(record['id'],spec['name'],json.dumps(record)));event(db,record,'created')
            return record

    def valid(self,record,db,identities,at=None,coverage=0):
        at=epoch(now()) if at is None else at
        if record['state']!='active' or not record['approval']:return 'approval_required'
        if at<epoch(record['approved_at']):return 'invalid_approval_time'
        if at>=record['expires_at']:return 'expired'
        if at+coverage>record['expires_at']:return 'insufficient_validity'
        try:
            for source in record['sources']:
                job=self.jobs.get(source['job_id'],db)
                if self.context(job,db,identities)!=record['context']:return 'context_changed'
                self.jobs.lifecycle.available('archive',job['id'],db)
        except (JobError,ValueError,TypeError,KeyError):return 'context_unavailable'
        return 'active'

    def control(self,identifier,action):
        if action not in {'approve','pause','retire'}:raise JobError('Invalid performance reference operation')
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            value=self.get(identifier,db)
            if value['state']=='retired':
                if action=='retire':return value
                raise JobError('Retired performance reference cannot be enabled',409)
            if action=='approve':
                if value['state']=='restore_paused':raise JobError('Restored reference is historical; prepare a fresh reference from new measurements',409)
                if value['state']=='active':return value
                actual=[self.source(self.jobs.get(s['job_id'],db),db,identities) for s in value['sources']]
                if actual!=value['sources'] or epoch(now())>=value['expires_at']:raise JobError('Reference evidence/context changed or expired before approval',409)
                value.update(state='active',approval=uuid.uuid4().hex,approved_at=now())
            else:value.update(state='paused' if action=='pause' else 'retired',approval=None)
            value['revision']+=1;db.execute('UPDATE performance_references SET record=? WHERE id=?',(json.dumps(value),identifier));event(db,value,action)
            return value

    def for_plan(self,identifier,plan,card,db,identities):
        value=self.get(identifier,db);context=value['context']
        if (context['node_id']!=plan['spec']['node_id'] or context['owner']!=plan['spec']['owner']
            or context['project']!=plan['spec']['project'] or context['device_id']!=card):raise JobError('Performance reference does not match the exact source plan/card',409)
        if self.valid(value,db,identities)!='active':raise JobError('Explicitly approve a current, unexpired performance reference first',409)
        return {'id':identifier,'approval_sha256':approval_sha(value)}

    def compare(self,anchor,check,db,identities,at=None,coverage=0):
        value=self.get(anchor['id'],db)
        result={'status':'unconfirmed','reference_id':value['id'],'approval_sha256':approval_sha(value),
            'reference_center_us':value['center_us'],'ratio_bounds':value['spec']['ratio_bounds'],
            'expires_at':value['expires_at'],'performance_qualified':False,
            'scope':'Approved empirical fixed-recipe envelope only; not stable clocks, full hardware health or arbitrary operator qualification.'}
        state=self.valid(value,db,identities,at,coverage)
        if state!='active' or result['approval_sha256']!=anchor['approval_sha256']:
            result['reason']=state if state!='active' else 'approval_changed';return result
        captured=check.get('performance_sample',{})
        if captured.get('status')!='captured':result['reason']='missing_fixed_performance_sample';return result
        sample=captured['sample'];result['source_job_id']=sample['job_id']
        if (sample['job_id'] in value['spec']['source_job_ids'] or epoch(sample['execution_finished_at'])<epoch(value['approved_at'])):
            result['reason']='independent_post_approval_sample_required';return result
        if sample['context']!=value['context']:result['reason']='measurement_context_changed';return result
        if sample['round_median_ratio']>value['spec']['max_round_ratio']:result['status']='unstable_samples';return result
        medians=sample['round_medians_us'];ratios=[m/value['center_us'] for m in medians]
        result.update(round_medians_us=medians,round_ratios=[r if finite(r) else None for r in ratios],
            frequency_samples_in_window=sample['frequency_samples_in_window'],interval_clock_stability='unverified')
        result['status']='outside_reference_envelope' if any(not value['limits_us'][0]<=m<=value['limits_us'][1] for m in medians) else 'within_reference_envelope'
        return result

    def pins(self,kind,identifier,db):
        if kind not in {'archive','input'}:return False
        linked=set()
        for raw, in db.execute('SELECT record FROM device_evidence_policies'):
            p=json.loads(raw)
            if p['state']!='retired' and p['generation']>0:linked.update(p['spec'].get('performance_references',{}).values())
        for job in self.jobs.all(db):
            if job['state'] in ACTIVE:
                for key in ['_selfcheck_before','_selfcheck_reservation']:
                    ref=job.get(key,{}).get('performance_reference',{}).get('reference_id')
                    if ref:linked.add(ref)
        return any((identifier in value['spec']['source_job_ids'] if kind=='archive' else any(s.get('input_id')==identifier for s in value['sources'])) and (value['state']!='retired' or value['id'] in linked)
            for value in (json.loads(raw) for raw, in db.execute('SELECT record FROM performance_references')))

    def public(self,principal,limit=4,offset=0):
        principal.require('jobs:read')
        if type(limit) is not int or not 1<=limit<=8 or type(offset) is not int or offset<0:raise JobError('Invalid performance reference page')
        if principal.experiment_id:return {'references':[],'total':0,'limit':limit,'offset':offset}
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            visible=[v for v in (json.loads(raw) for raw, in db.execute('SELECT record FROM performance_references'))
                if principal.admin or self.jobs.access.project_role(principal,v['context']['project'])]
            values=[{'id':v['id'],'name':v['spec']['name'],'state':v['state'],'validity':self.valid(v,db,identities),
                'node_id':v['context']['node_id'],'device_id':v['context']['device_id'],'source_job_ids':v['spec']['source_job_ids'],
                'center_us':v['center_us'],'ratio_bounds':v['spec']['ratio_bounds'],'expires_at':v['expires_at'],
                'performance_qualified':False} for v in visible[offset:offset+limit]]
            return {'references':values,'total':len(visible),'limit':limit,'offset':offset,'capability':CAPABILITY}

"""Opt-in central daily NPU microchecks. No timer, credentials or Root execution.

Plans are local-administrator approvals, not HTTP submissions. Ordinary robot
quotas and every Node admission check still apply. Job/action/budget binding is
one transaction; uncertainty always follows the original execution handle.
"""
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from jobs import JobError,TERMINAL,ACTIVE,now
from npu_selfcheck import DRIVER,REPORT,source_sha,reference_sha,request_for,validate_report

CAPABILITY='central-device-schedule-v1'
MAX_PLANS=32
MAX_RUNS=4096


def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def _checkpoint(name):pass


def specification(value):
    fields={'name','owner','project','node_id','device_ids','timezone','at','window_minutes','memory_mib',
            'max_probes_per_card','daily_npu_seconds','total_npu_seconds'}
    if not isinstance(value,dict) or set(value)!=fields:raise JobError('Provide the complete fixed daily microcheck plan')
    from auth import IDENTIFIER
    if any(not isinstance(value[k],str) or not IDENTIFIER.fullmatch(value[k]) for k in ['name','owner']):raise JobError('Invalid schedule name/owner')
    if not isinstance(value['project'],str) or not 1<=len(value['project'])<=100:raise JobError('Invalid explicit robot project')
    from placement import validate
    try:validate({'node_id':value['node_id']},0)
    except ValueError as error:raise JobError(str(error)) from None
    ids=value['device_ids']
    if not isinstance(ids,list) or not 1<=len(ids)<=256 or any(type(i) is not int or not 0<=i<256 for i in ids) or len(set(ids))!=len(ids):raise JobError('Choose distinct physical cards')
    if not isinstance(value['timezone'],str) or len(value['timezone'])>80:raise JobError('Invalid timezone')
    try:ZoneInfo(value['timezone'])
    except (ZoneInfoNotFoundError,ValueError):raise JobError('Timezone database/zone unavailable') from None
    if not isinstance(value['at'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value['at']):raise JobError('Daily time must be HH:MM')
    for key,lo,hi in [('window_minutes',1,720),('memory_mib',512,65536),('max_probes_per_card',1,3),
                      ('daily_npu_seconds',300,1000000),('total_npu_seconds',300,1000000000)]:
        if type(value[key]) is not int or not lo<=value[key]<=hi:raise JobError('Invalid schedule '+key)
    if value['daily_npu_seconds']>value['total_npu_seconds']:raise JobError('Daily budget exceeds total budget')
    if value['daily_npu_seconds']<300*len(ids):raise JobError('Daily budget cannot cover one access probe and UT per selected card')
    return {**value,'device_ids':sorted(ids)}


def window(spec,epoch):
    zone=ZoneInfo(spec['timezone']);local=dt.datetime.fromtimestamp(epoch,zone)
    hour,minute=map(int,spec['at'].split(':'))
    anchor=dt.datetime.combine(local.date(),dt.time(hour,minute),zone).replace(fold=0)
    if epoch<anchor.timestamp():anchor=anchor-dt.timedelta(days=1)
    start=anchor.timestamp();return anchor.date().isoformat(),start,start+spec['window_minutes']*60


def invalidate_restored(path):
    """A restored snapshot cannot resurrect a later-revoked unattended approval."""
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_schedule_plans'").fetchone():return
        for identifier,raw in db.execute('SELECT id,record FROM device_schedule_plans').fetchall():
            value=json.loads(raw)
            if value['state']!='retired':value.update(state='restore_paused',authorization=None,reason='Historical restored plan; explicitly create a new plan and budget')
            db.execute('UPDATE device_schedule_plans SET record=? WHERE id=?',(json.dumps(value),identifier))


def require_restore_history(current,staged,role):
    """Refuse loss of locally retained charges/actions before filesystem rebinding."""
    if role!='controller':return
    from deployment import DeployError
    def read(directory):
        path=Path(directory)/'jobs.sqlite3'
        if not path.exists():return {},{},{}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_schedule_plans'").fetchone():return {},{},{}
            return ({i:json.loads(v) for i,v in db.execute('SELECT id,record FROM device_schedule_plans')},
                    {(p,d):s for p,d,s in db.execute('SELECT plan_id,day,seconds FROM device_schedule_days')},
                    {i:json.loads(v) for i,v in db.execute('SELECT id,record FROM device_schedule_runs')})
    old,old_days,old_runs=read(current);new,new_days,new_runs=read(staged)
    for identifier,plan in old.items():
        other=new.get(identifier)
        if (not other or any(other.get(k)!=plan[k] for k in ['id','spec','input_id','input_manifest_sha256'])
            or other['total_seconds']<plan['total_seconds'] or other['generation']<plan['generation']):
            raise DeployError('Older snapshot drops retained daily schedule authority or charges')
    if any(new_days.get(key,-1)<value for key,value in old_days.items()):raise DeployError('Older snapshot would rewind daily self-check budgets')
    for identifier,run in old_runs.items():
        other=new_runs.get(identifier)
        if not other or len(other['actions'])<len(run['actions']):raise DeployError('Older snapshot drops daily check action history')
        for a,b in zip(run['actions'],other['actions']):
            if a['request']!=b['request'] or a['kind']!=b['kind'] or a['job_id'] and (b['job_id']!=a['job_id'] or b['reserved_seconds']!=a['reserved_seconds']):
                raise DeployError('Older snapshot loses an original daily check job/charge')


class Schedules:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS device_schedule_plans(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,record TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS device_schedule_runs(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,day TEXT NOT NULL,record TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS device_schedule_days(plan_id TEXT NOT NULL,day TEXT NOT NULL,seconds INTEGER NOT NULL,PRIMARY KEY(plan_id,day));''')

    def plan(self,identifier,db):
        row=db.execute('SELECT record FROM device_schedule_plans WHERE id=?',(identifier,)).fetchone()
        if row is None:raise JobError('Device schedule not found',404)
        return json.loads(row[0])

    def run(self,identifier,db):
        row=db.execute('SELECT record FROM device_schedule_runs WHERE id=?',(identifier,)).fetchone()
        if row is None:raise JobError('Device schedule run not found',404)
        return json.loads(row[0])

    def save_plan(self,db,plan):
        value=json.dumps(plan);db.execute('UPDATE device_schedule_plans SET record=? WHERE id=? AND record!=?',(value,plan['id'],value))
    def save_run(self,db,run):
        value=json.dumps(run);db.execute('UPDATE device_schedule_runs SET record=? WHERE id=? AND record!=?',(value,run['id'],value))

    def context(self,spec,db,identities):
        if not self.jobs.policy['npu_enabled']:raise JobError('Controller has not enabled managed NPU jobs',409)
        owner=spec['owner'];principal=self.jobs.access.user(owner)
        row=identities.execute('SELECT disabled,profile FROM users WHERE id=?',(owner,)).fetchone()
        revision=identities.execute('SELECT revision FROM owner_security_revisions WHERE owner=?',(owner,)).fetchone()
        if owner==self.jobs.owner or not row or row[0] or not principal or principal.legacy or not revision:
            raise JobError('Daily checks require a distinct active provisioned robot identity',409)
        profile=json.loads(row[1]);quota=profile['quota']
        if (principal.environments!=('worker-python',) or principal.mode!='templates' or quota['max_running']!=1
            or quota['max_queued']<2 or quota['cpu']<1 or quota['npu']<1 or quota['memory_mib']<spec['memory_mib']
            or self.jobs.access.project_role(principal,spec['project']) not in {'editor','manager'}):
            raise JobError('Robot environment, serial quota or project permission cannot fit the plan',409)
        node=self.jobs.nodes.node(spec['node_id'],db);np=node['profile']
        if (owner not in np['users'] or not {'accuracy_v1','npu_access_probe'}<=set(np['templates'])
            or not set(spec['device_ids'])<=set(np.get('npu',{}).get('device_ids',[])) or not np.get('environment_manifest')):
            raise JobError('Explicitly prepare the robot, certified environment, fixed templates and cards on this Node',409)
        if self.jobs.policy.get('require_bounded_workspace') and not np.get('workspace'):raise JobError('Daily checks require prepared bounded Node workspaces',409)
        if self.jobs.policy.get('require_finite_node_spool') and not np.get('spool_storage'):raise JobError('Daily checks require prepared finite Node spool',409)
        from resources import require_managed_capacity
        require_managed_capacity(np,self.jobs.resources.get(np['physical_id'],db),spec['memory_mib'])
        from environment_contract import lock_for,fingerprint
        lock=lock_for(np['environment_manifest'])
        if not lock.get('packages',{}).get('torch') or not lock.get('packages',{}).get('torch_npu'):
            raise JobError('Certified golden environment must query torch and torch_npu package versions',409)
        from device_qualification import decision
        contexts={str(card):decision(node,owner,card)['context_sha256'] for card in spec['device_ids']}
        if any(v is None for v in contexts.values()):raise JobError('Current protected host/runtime identity is unavailable',409)
        names={str(row['id']):row.get('name') for row in node['report'].get('npu',{}).get('devices',[]) if row['id'] in spec['device_ids']}
        if set(names)!={str(i) for i in spec['device_ids']} or any(not n or n=='UNKNOWN' for n in names.values()):raise JobError('Physical card management names unavailable',409)
        for card in spec['device_ids']:request_for('input-'+'0'*24,{**lock,'npu_name':names[str(card)]},node['id'],card,'2000-01-01',spec['memory_mib'])
        return {'owner_revision':revision[0],'owner_profile_sha256':sha(profile),'node_profile_sha256':sha(np),
            'physical_id':np['physical_id'],'environment_sha256':fingerprint(np['environment_manifest']),
            'lock':lock,'card_names':names,'access_contexts':contexts,'driver_sha256':source_sha(),'reference_sha256':reference_sha()}

    def fresh(self,node,epoch):
        if not 0<=epoch-node['last_seen']<=30:return False
        try:return node['report']['npu']['status']=='ok' and 0<=epoch-dt.datetime.fromisoformat(node['report']['npu']['sampled_at']).timestamp()<=15
        except (KeyError,TypeError,ValueError):return False

    def create(self,value):
        spec=specification(value)
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            old=db.execute('SELECT id,record FROM device_schedule_plans WHERE name=?',(spec['name'],)).fetchone()
            if old:
                original=json.loads(old[1])
                if original['spec']!=spec:raise JobError('Schedule name already has a different immutable definition',409)
                return original
            if db.execute('SELECT COUNT(*) FROM device_schedule_plans').fetchone()[0]>=MAX_PLANS:raise JobError('Schedule history limit reached',409)
            context=self.context(spec,db,identities)
            if not self.fresh(self.jobs.nodes.node(spec['node_id'],db),time.time()):raise JobError('Wait for fresh authenticated Node observations',409)
        # Ordinary owner storage reservations; interrupted preparation can resume
        # the identical content-addressed input without creating executable jobs.
        principal=self.jobs.access.user(spec['owner']);raw=(Path(__file__).parent/DRIVER).read_bytes()
        bundle,_=self.jobs.inputs.create(principal,{'files':[{'name':DRIVER,'size':len(raw),'sha256':source_sha()}]})
        if not bundle['sealed']:
            offset=bundle['uploaded'].get(DRIVER,0)
            while offset<len(raw):
                block=raw[offset:offset+256*1024];self.jobs.inputs.upload(bundle['id'],principal,{'name':DRIVER,'offset':offset,'data':base64.b64encode(block).decode()});offset+=len(block)
            bundle=self.jobs.inputs.seal(bundle['id'],principal)
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            if self.context(spec,db,identities)!=context:raise JobError('Context changed during plan preparation',409)
            self.jobs.lifecycle.available('input',bundle['id'],db)
            if not self.jobs.inputs.get(bundle['id'],db=db,owner=spec['owner'])['sealed']:raise JobError('Fixed input changed during preparation',409)
            if db.execute('SELECT COUNT(*) FROM device_schedule_plans').fetchone()[0]>=MAX_PLANS:raise JobError('Schedule history limit reached',409)
            if db.execute('SELECT 1 FROM device_schedule_plans WHERE name=?',(spec['name'],)).fetchone():raise JobError('Plan was concurrently created; inspect the original',409)
            plan={'id':'schedule-'+uuid.uuid4().hex[:24],'spec':spec,'context':context,'input_id':bundle['id'],
                'input_manifest_sha256':bundle['manifest_sha256'],'state':'paused','authorization':None,'generation':0,
                'total_seconds':0,'watermark':None,'reason':'Explicit enable required','created_at':now()}
            db.execute('INSERT INTO device_schedule_plans VALUES(?,?,?)',(plan['id'],spec['name'],json.dumps(plan)))
            return plan

    def control(self,identifier,action):
        if action not in {'enable','pause','retire'}:raise JobError('Unknown schedule operation')
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            plan=self.plan(identifier,db)
            if plan['state']=='retired':
                if action=='retire':return plan
                raise JobError('Retired schedule cannot be reactivated',409)
            owned=[j for j in self.jobs.all(db) if j.get('_device_schedule',{}).get('plan_id')==identifier]
            if action=='enable':
                if plan['state']=='active':return plan
                if plan['state']=='restore_paused':raise JobError('Restored plans are historical only; explicitly create a new plan and budget',409)
                if any(j['state'] not in TERMINAL for j in owned):raise JobError('Reconcile/cancel original schedule jobs before new approval',409)
                if plan['total_seconds']>=plan['spec']['total_npu_seconds']:raise JobError('Lifetime self-check budget exhausted',409)
                context=self.context(plan['spec'],db,identities)
                if context['driver_sha256']!=plan['context']['driver_sha256']:raise JobError('Recipe changed; explicitly create a new immutable plan',409)
                if not self.fresh(self.jobs.nodes.node(plan['spec']['node_id'],db),time.time()):raise JobError('Fresh Node observations required',409)
                for raw, in db.execute('SELECT record FROM device_schedule_plans WHERE id!=?',(identifier,)):
                    other=json.loads(raw)
                    if other['state']=='active' and other['context']['physical_id']==context['physical_id']:raise JobError('One active daily plan per physical resource domain',409)
                plan.update(state='active',authorization=uuid.uuid4().hex,generation=plan['generation']+1,context=context,reason='')
            else:
                if action=='retire' and any(j['state'] not in TERMINAL for j in owned):raise JobError('Original jobs still hold scheduling state',409)
                plan.update(state='retired' if action=='retire' else 'paused',authorization=None,reason='Explicit administrator '+action)
            self.save_plan(db,plan)
        if action=='pause':self.cancel_queued(identifier)
        return plan

    def cancel_queued(self,identifier):
        for job in self.jobs.all():
            if job['state']=='queued' and job.get('_device_schedule',{}).get('plan_id')==identifier:self.jobs.cancel(job['id'],_queued_only=True)

    def load_action(self,marker,db):
        if not isinstance(marker,dict) or set(marker)!={'plan_id','run_id','index'} or type(marker['index']) is not int:raise JobError('Invalid private schedule binding',409)
        plan=self.plan(marker['plan_id'],db);run=self.run(marker['run_id'],db)
        if run['plan_id']!=plan['id'] or not 0<=marker['index']<len(run['actions']):raise JobError('Schedule action not found',409)
        return plan,run,run['actions'][marker['index']]

    def validate_action(self,marker,db,identities,job=None):
        plan,run,action=self.load_action(marker,db)
        self.jobs.lifecycle.available('input',plan['input_id'],db)
        bundle=self.jobs.inputs.get(plan['input_id'],db=db,owner=plan['spec']['owner'])
        if not bundle['sealed'] or bundle['manifest_sha256']!=plan['input_manifest_sha256']:raise JobError('Fixed self-check input changed',409)
        if (plan['state']!='active' or plan['authorization']!=run['authorization'] or not run['not_before']<=time.time()<=run['deadline']
            or self.context(plan['spec'],db,identities)!=plan['context']):raise JobError('Original daily-check authority/window/context changed',409)
        if job is not None and (job.get('_device_schedule')!=marker or job['owner']!=plan['spec']['owner']
            or action['job_id']!=job['id']):raise JobError('Job differs from its bound daily action',409)
        return plan,run,action

    def bind(self,marker,job,db,identities):
        plan,run,action=self.validate_action(marker,db,identities)
        if action['job_id'] is not None:raise JobError('Schedule action already has its original job',409)
        charge=job['resources']['npu']*job['timeout_seconds']
        old=db.execute('SELECT seconds FROM device_schedule_days WHERE plan_id=? AND day=?',(plan['id'],run['day'])).fetchone()
        daily=old[0] if old else 0
        if daily+charge>plan['spec']['daily_npu_seconds'] or plan['total_seconds']+charge>plan['spec']['total_npu_seconds']:
            raise JobError('Daily or lifetime self-check budget exhausted',409)
        action['job_id']=job['id'];action['reserved_seconds']=charge;plan['total_seconds']+=charge
        db.execute('INSERT INTO device_schedule_days VALUES(?,?,?) ON CONFLICT(plan_id,day) DO UPDATE SET seconds=excluded.seconds',(plan['id'],run['day'],daily+charge))
        self.save_run(db,run);self.save_plan(db,plan)
        _checkpoint('job_bound')

    def permission(self,job,active,db,identities):
        marker=job.get('_device_schedule')
        if not marker or job.get('_start_authorized'):return None
        if db is None or identities is None:
            with self.jobs.nodes.handoff.transaction() as (connection,users):return self.permission(job,active,connection,users)
        try:plan,run,_=self.validate_action(marker,db,identities,job)
        except (JobError,ValueError,KeyError,TypeError):return '每日自检原授权、窗口或环境已变更，等待明确协调'
        physical=plan['context']['physical_id']
        if any(j['id']!=job['id'] and not j.get('_device_schedule') and j.get('_physical_id',self.jobs.backend.physical_id)==physical for j in active):
            return '每日自检等待物理资源域的前台工作结束'
        domain=self.jobs.resources.get(physical,db)
        candidates=[]
        for node_id,raw,seen,disabled in db.execute('SELECT id,profile,last_seen,disabled FROM worker_nodes'):
            if not disabled and json.loads(raw)['physical_id']==physical and 0<=time.time()-seen<=30:
                candidates.append(self.jobs.nodes.node(node_id,db))
        for other in self.jobs.all(db):
            if other['state']!='queued' or other.get('_device_schedule') or other['_environment']['type']!='worker':continue
            for node in candidates:
                if other['owner'] not in node['profile']['users'] or other.get('template') not in node['profile']['templates']:continue
                # Includes another image/entry on this same physical host. No
                # schedule marker on the candidate, so this cannot recurse.
                if not self.jobs.nodes.placement(other,node,domain,[],db,identities)['reason']:
                    return '每日自检让位于同物理域可运行的前台等待任务'
        return None

    def _prepare_runs(self,db,plan,epoch):
        day,start,end=window(plan['spec'],epoch)
        if not start<=epoch<=end or plan['watermark'] and day<plan['watermark']:return
        plan['watermark']=day;self.save_plan(db,plan)
        needed=[]
        for card in plan['spec']['device_ids']:
            identifier='run-'+sha({'plan':plan['id'],'authorization':plan['authorization'],'day':day,'device':card})[:24]
            if not db.execute('SELECT 1 FROM device_schedule_runs WHERE id=?',(identifier,)).fetchone():needed.append((identifier,card))
        if db.execute('SELECT COUNT(*) FROM device_schedule_runs').fetchone()[0]+len(needed)>MAX_RUNS:
            plan.update(state='history_full',authorization=None,reason='Daily run history full; explicit maintenance required');self.save_plan(db,plan);return
        for identifier,card in needed:
            run={'id':identifier,'plan_id':plan['id'],'authorization':plan['authorization'],'day':day,'device_id':card,
                'not_before':start,'deadline':end,'state':'waiting_idle','actions':[],'context_sha256':sha(plan['context']),
                'access_context_sha256':plan['context']['access_contexts'][str(card)],'outcome':None}
            db.execute('INSERT INTO device_schedule_runs VALUES(?,?,?,?)',(identifier,plan['id'],day,json.dumps(run)))

    def outcome(self,plan,run,job):
        row=job.get('workflow_result',{})
        if job['state'] not in {'succeeded','failed'} or not row.get('environment_locked') or not row.get('command_executed') or not row.get('device_access_confirmed'):
            return {'state':'execution_failed','job_id':job['id'],'original_state':job['state'],'health_certified':False,'performance_qualified':False}
        try:
            from archive_api import open_artifact
            with open_artifact(self.jobs.directory(job['id']),REPORT,128*1024) as stream:raw=stream.read()
            comparison=validate_report(json.loads(raw))
            if job.get('_npu_access_context')!=run['access_context_sha256'] or job['node_id']!=plan['spec']['node_id'] or job['allocation']['npu_ids']!=[run['device_id']]:raise ValueError('Execution context differs')
            status='microchecks_passed' if comparison['precision_passed'] else 'microchecks_failed'
            if status=='microchecks_passed' and (job['state']!='succeeded' or not row.get('passed')):status='execution_failed'
            if status=='microchecks_passed' and job.get('device_conditions',{}).get('status')!='sampled_no_alerts':status='conditions_unconfirmed'
            return {'state':status,'job_id':job['id'],'attempt_id':job['attempt_id'],'report_sha256':hashlib.sha256(raw).hexdigest(),
                'comparison':comparison,'device_conditions':job.get('device_conditions'),'context_sha256':run['context_sha256'],
                'original_state':job['state'],'health_certified':False,'performance_qualified':False,'scope':'Fixed bounded observations, not full hardware qualification'}
        except (OSError,ValueError,KeyError,TypeError):return {'state':'evidence_invalid','job_id':job['id'],'health_certified':False,'performance_qualified':False}

    def _step(self,db,identities,plan,run,epoch):
        if run['state'] in {'microchecks_passed','microchecks_failed','conditions_unconfirmed','execution_failed','evidence_invalid','access_failed','expired_unmeasured','paused','probe_limit','budget_exhausted'}:return None
        actions=run['actions'];ut=next((a for a in actions if a['kind']=='ut'),None)
        if ut and ut['job_id']:
            job=self.jobs.get(ut['job_id'],db)
            if job['state'] in TERMINAL:
                run['outcome']=self.outcome(plan,run,job);run['state']=run['outcome']['state'];self.save_run(db,run);return None
            if job['state']!='queued':run['state']='following_original';self.save_run(db,run);return None
        probe=next((a for a in reversed(actions) if a['kind']=='probe'),None)
        if probe and probe['job_id'] is not None:
            job=self.jobs.get(probe['job_id'],db)
            if job['state'] not in TERMINAL and job['state']!='queued':run['state']='following_original_probe';self.save_run(db,run);return None
            if job['state'] in TERMINAL and job['state']!='succeeded':run['state']='access_failed';self.save_run(db,run);return None
        if epoch>run['deadline']:
            run['state']='expired_unmeasured';self.save_run(db,run);return None
        if plan['state']!='active' or plan['authorization']!=run['authorization']:
            run['state']='paused';self.save_run(db,run);return None
        if epoch<run['not_before']:run['state']='waiting_window';self.save_run(db,run);return None
        for raw, in db.execute('SELECT record FROM device_schedule_runs WHERE plan_id=? AND id!=?',(plan['id'],run['id'])):
            previous=json.loads(raw)
            if previous['device_id']!=run['device_id']:continue
            for action in previous['actions']:
                if action['job_id'] and self.jobs.get(action['job_id'],db)['state'] not in TERMINAL:
                    run['state']='waiting_prior_execution';run['prior_job_id']=action['job_id'];self.save_run(db,run);return None
        if probe and probe['job_id'] is not None and self.jobs.get(probe['job_id'],db)['state']=='queued':
            run['state']='following_original_probe';self.save_run(db,run);return None
        if self.jobs.control_storage()['blocks_new_work'] or self.jobs.access.audit_status()['blocks_new_work']:
            run['state']='waiting_control_capacity';self.save_run(db,run);return None
        if epoch<run.get('retry_after',0):return None
        if probe and probe['job_id'] is None:return {'plan_id':plan['id'],'run_id':run['id'],'index':actions.index(probe)}
        node=self.jobs.nodes.node(plan['spec']['node_id'],db)
        if not self.fresh(node,epoch):run['state']='waiting_node';self.save_run(db,run);return None
        reading=next((d for d in node['report']['npu']['devices'] if d['id']==run['device_id']),{})
        if reading.get('health')!='OK' or reading.get('process_count')!=0 or reading.get('utilization_pct')!=0:
            run['state']='waiting_idle';self.save_run(db,run);return None
        from device_qualification import decision
        access=decision(node,plan['spec']['owner'],run['device_id'])
        if not probe or access['status']!='verified':
            count=sum(a['kind']=='probe' for a in actions)
            if count>=plan['spec']['max_probes_per_card']:run['state']='probe_limit';self.save_run(db,run);return None
            kind='probe';request={'template':'npu_access_probe','environment':'worker-python','project':plan['spec']['project'],
                'parameters':{'node_id':node['id'],'device_id':run['device_id']}}
        elif ut:
            if ut['job_id'] is None:return {'plan_id':plan['id'],'run_id':run['id'],'index':actions.index(ut)}
            run['state']='following_original';self.save_run(db,run);return None
        else:
            kind='ut';lock={**plan['context']['lock'],'npu_name':plan['context']['card_names'][str(run['device_id'])]}
            request=request_for(plan['input_id'],lock,node['id'],run['device_id'],run['day'],plan['spec']['memory_mib']);request['project']=plan['spec']['project']
        request['idempotency_key']='daily-'+sha({'run':run['id'],'index':len(actions),'kind':kind})
        actions.append({'kind':kind,'request':request,'job_id':None,'reserved_seconds':0})
        run['state']='submission_pending';self.save_run(db,run)
        return {'plan_id':plan['id'],'run_id':run['id'],'index':len(actions)-1}

    def tick(self):
        with self.jobs.connect() as db:
            if not db.execute('SELECT 1 FROM device_schedule_plans LIMIT 1').fetchone():return
        epoch=time.time();pending=[];cancel=[]
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            plans=[json.loads(raw) for raw, in db.execute('SELECT record FROM device_schedule_plans')]
            for plan in plans:
                if plan['state']=='active':
                    try:node=self.jobs.nodes.node(plan['spec']['node_id'],db)
                    except JobError:
                        node=None;plan.update(state='context_changed',authorization=None,reason='Original Node enrollment revoked');self.save_plan(db,plan)
                    if node and self.fresh(node,epoch):
                        try:valid=self.context(plan['spec'],db,identities)==plan['context']
                        except (JobError,ValueError,KeyError,TypeError):valid=False
                        if not valid:plan.update(state='context_changed',authorization=None,reason='Explicit fresh approval required');self.save_plan(db,plan)
                    if plan['state']=='active' and not self.jobs.control_storage()['blocks_new_work'] and not self.jobs.access.audit_status()['blocks_new_work']:
                        self._prepare_runs(db,plan,epoch)
                for raw, in db.execute('SELECT record FROM device_schedule_runs WHERE plan_id=? ORDER BY rowid',(plan['id'],)).fetchall():
                    run=json.loads(raw)
                    candidate=self._step(db,identities,plan,run,epoch)
                    self.jobs.device_evidence.record(plan,run,db,identities)
                    if candidate:pending.append((run.get('last_submit_attempt',0),run['id'],candidate))
                    if plan['state']!='active' or epoch>run['deadline'] or run['state'] in {'access_failed','probe_limit'}:
                        for action in run['actions']:
                            if action['job_id'] and self.jobs.get(action['job_id'],db)['state']=='queued':cancel.append(action['job_id'])
        for identifier in cancel:self.jobs.cancel(identifier,_queued_only=True)
        if pending:
            pending=min(pending)[2]
            with self.jobs.transaction() as db:
                run=self.run(pending['run_id'],db);run['last_submit_attempt']=epoch;self.save_run(db,run)
            with self.jobs.connect() as db:plan,run,action=self.load_action(pending,db)
            principal=self.jobs.access.user(plan['spec']['owner'])
            if principal is None:return
            try:
                _checkpoint('before_submit')
                self.jobs.submit(action['request'],principal,_scheduled=pending)
                _checkpoint('after_submit')
            except JobError as error:
                with self.jobs.transaction() as db:
                    run=self.run(run['id'],db);run['reason']=str(error)[:300]
                    run['retry_after']=epoch+15
                    if 'budget exhausted' in str(error):run['state']='budget_exhausted'
                    self.save_run(db,run)

    def public(self,principal,limit=4,offset=0):
        principal.require('jobs:read')
        if type(limit) is not int or not 1<=limit<=8 or type(offset) is not int or offset<0:raise JobError('Invalid daily plan page')
        if principal.experiment_id:return {'plans':[],'capability':CAPABILITY,'total':0,'limit':limit,'offset':offset}
        with self.jobs.connect() as db:
            result=[]
            for raw, in db.execute('SELECT record FROM device_schedule_plans'):
                plan=json.loads(raw);spec=plan['spec']
                if not principal.admin and principal.id!=spec['owner'] and not self.jobs.access.project_role(principal,spec['project']):continue
                runs=[json.loads(v) for v, in db.execute('SELECT record FROM device_schedule_runs WHERE plan_id=? ORDER BY rowid DESC LIMIT 256',(plan['id'],))]
                def latest(card):
                    run=next((r for r in runs if r['device_id']==card),None)
                    if run is None:return None
                    outcome=run['outcome']
                    return {**{k:run[k] for k in ['id','day','state','deadline']},'reason':run.get('reason',''),
                        'current_window':plan['state']=='active' and run['authorization']==plan['authorization'] and run['not_before']<=time.time()<=run['deadline'],
                        'prior_job_id':run.get('prior_job_id'),
                        'jobs':[{'kind':a['kind'],'job_id':a['job_id']} for a in run['actions'] if a['job_id']],
                        'outcome':{k:outcome[k] for k in ['state','job_id','attempt_id','report_sha256','health_certified','performance_qualified'] if k in outcome} if outcome else None}
                result.append({'id':plan['id'],'name':spec['name'],'owner':spec['owner'],'project':spec['project'],'node_id':spec['node_id'],
                    'timezone':spec['timezone'],'at':spec['at'],'state':plan['state'],'reason':plan['reason'],'generation':plan['generation'],
                    'reserved_npu_seconds':plan['total_seconds'],'total_npu_seconds':spec['total_npu_seconds'],
                    'cards':[{'device_id':card,'latest':latest(card)} for card in spec['device_ids']],
                    'health_certified':False,'performance_qualified':False})
            return {'plans':result[offset:offset+limit],'capability':CAPABILITY,'timer_installed':False,'total':len(result),'limit':limit,'offset':offset}

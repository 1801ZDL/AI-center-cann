"""Explicit project policies binding fixed daily microchecks to managed attempts.

This is bounded-reference evidence, never full numerical/hardware/performance
certification. No workload or HTTP caller can publish or approve a check.
"""
import copy
import datetime as dt
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from jobs import JobError,ACTIVE,TERMINAL,now
from device_qualification import digest

CAPABILITY='device-selfcheck-binding-v1'
MAX_POLICIES=32
MAX_CHECKS=4096
MAX_EVENTS=2048
START_MARGIN=60
FINAL_RUNS={'microchecks_passed','microchecks_failed','conditions_unconfirmed','execution_failed','evidence_invalid',
            'access_failed','expired_unmeasured','probe_limit','budget_exhausted'}


def epoch(value):
    parsed=dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:raise ValueError('Timestamp needs a timezone')
    return parsed.timestamp()


def approval_sha(policy):return digest({'approval':policy['approval'],'revision':policy['revision'],'anchor':policy['anchor']})


def event(db,policy,action,restoring=False):
    ceiling=MAX_EVENTS if restoring else MAX_EVENTS-MAX_POLICIES
    if db.execute('SELECT count(*) FROM device_evidence_events').fetchone()[0]>=ceiling:raise JobError('Self-check approval event history full; explicit maintenance required',409)
    record={'policy_id':policy['id'],'revision':policy['revision'],'generation':policy['generation'],'state':policy['state'],
        'action':action,'at':now(),'administrator_uid':os.geteuid(),'approval_sha256':approval_sha(policy),
        'spec_sha256':digest(policy['spec']),'source_context_sha256':policy['anchor'].get('source_context_sha256'),
        'target_context_sha256':digest(policy['anchor'].get('targets',{}))}
    db.execute('INSERT INTO device_evidence_events(record) VALUES(?)',(json.dumps(record),))


def specification(value):
    from auth import IDENTIFIER
    required={'name','source_plan_id','target_node_ids','projects','max_age_seconds','mode'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'performance_references'}:
        raise JobError('Provide the complete project self-check evidence policy')
    if not isinstance(value['name'],str) or not IDENTIFIER.fullmatch(value['name']):raise JobError('Invalid evidence policy name')
    if not re.fullmatch('schedule-[0-9a-f]{24}',str(value['source_plan_id'])):raise JobError('Choose one fixed daily source plan')
    nodes=value['target_node_ids'];projects=value['projects']
    if not isinstance(nodes,list) or not 1<=len(nodes)<=16 or any(not isinstance(i,str) or not re.fullmatch('worker-[0-9a-f]{12}',i) for i in nodes) or len(set(nodes))!=len(nodes):raise JobError('Choose distinct enrolled consumer nodes')
    if not isinstance(projects,list) or not 1<=len(projects)<=16 or any(not isinstance(i,str) or not 1<=len(i)<=100 for i in projects) or len(set(projects))!=len(projects):raise JobError('Choose explicit consumer projects')
    if type(value['max_age_seconds']) is not int or not 60<=value['max_age_seconds']<=86400:raise JobError('Self-check lifetime must be 60..86400 seconds')
    if not isinstance(value['mode'],str) or value['mode'] not in {'annotate','require'}:raise JobError('Choose annotate or require')
    if 'performance_references' in value:
        refs=value['performance_references']
        if (not isinstance(refs,dict) or not 1<=len(refs)<=256
            or any(not isinstance(k,str) or not re.fullmatch('0|[1-9][0-9]{0,2}',k) or int(k)>255
                   or not isinstance(v,str) or not re.fullmatch('perfref-[0-9a-f]{24}',v) for k,v in refs.items())):
            raise JobError('Choose an explicit performance reference for every source physical card')
    return {**value,'target_node_ids':sorted(nodes),'projects':sorted(projects)}


def hardware(node,device):
    from npu_device import fingerprint
    from workflow_spec import runtime_manifest
    profile=node['profile'];report=node['report']
    if ('npu-access-qualification-v1' not in report.get('capabilities',[])
        or not re.fullmatch('[0-9a-f]{64}',str(report.get('access_host_sha256')))
        or report.get('access_runtime_files')!=runtime_manifest()):raise JobError('Current protected hardware/runtime identity unavailable',409)
    reading=next((r for r in report.get('npu',{}).get('devices',[]) if r['id']==device),{})
    if not reading.get('name') or reading['name']=='UNKNOWN':raise JobError('Current physical card identity unavailable',409)
    return digest({'physical_id':profile['physical_id'],'device_id':device,'name':reading['name'],
        'policy_sha256':fingerprint(profile['npu']),'host_sha256':report['access_host_sha256'],'runtime_files':runtime_manifest()})


def invalidate_restored(path):
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_evidence_policies'").fetchone():return
        for identifier,raw in db.execute('SELECT id,record FROM device_evidence_policies').fetchall():
            policy=json.loads(raw)
            if policy['state']!='retired':
                policy.update(state='restore_paused',approval=None,revision=policy['revision']+1)
                event(db,policy,'restore-frozen',restoring=True)
            db.execute('UPDATE device_evidence_policies SET record=? WHERE id=?',(json.dumps(policy),identifier))


def require_restore_history(current,staged,role):
    if role!='controller':return
    from deployment import DeployError
    def read(directory):
        path=Path(directory)/'jobs.sqlite3'
        if not path.exists():return {},{},{}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_evidence_policies'").fetchone():return {},{},{}
            policies={i:json.loads(r) for i,r in db.execute('SELECT id,record FROM device_evidence_policies')}
            expected={(p['id'],n,project) for p in policies.values() for n in p['spec']['target_node_ids'] for project in p['spec']['projects']}
            if set(db.execute('SELECT policy_id,node_id,project FROM device_evidence_targets'))!=expected:
                raise DeployError('Self-check policy/check history target projection differs')
            return policies,{i:json.loads(r) for i,r in db.execute('SELECT id,record FROM device_evidence_checks')},{i:r for i,r in db.execute('SELECT id,record FROM device_evidence_events')}
    old,checks,events=read(current);new,other_checks,other_events=read(staged)
    if any(i not in new or new[i]['spec']!=p['spec'] or new[i]['revision']<p['revision'] or new[i]['generation']<p['generation'] for i,p in old.items()):
        raise DeployError('Self-check policy/check history would be lost or rewound by this snapshot')
    if any(other_checks.get(i)!=value for i,value in checks.items()):raise DeployError('Self-check policy/check history would discard retained check evidence')
    if any(other_events.get(i)!=value for i,value in events.items()):raise DeployError('Self-check policy/check history would discard approval events')
    if len(other_events)+sum(p['state']!='retired' for p in new.values())>MAX_EVENTS:raise DeployError('Self-check restore event history full; explicit maintenance required')


class Evidence:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS device_evidence_policies(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,record TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS device_evidence_targets(policy_id TEXT NOT NULL,node_id TEXT NOT NULL,project TEXT NOT NULL,PRIMARY KEY(policy_id,node_id,project));
              CREATE INDEX IF NOT EXISTS device_evidence_target ON device_evidence_targets(node_id,project);
              CREATE TABLE IF NOT EXISTS device_evidence_checks(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,device_id INTEGER NOT NULL,record TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS device_evidence_check ON device_evidence_checks(plan_id,device_id);''')
            db.execute('CREATE TABLE IF NOT EXISTS device_evidence_events(id INTEGER PRIMARY KEY,record TEXT NOT NULL)')

    def policy(self,identifier,db):
        row=db.execute('SELECT record FROM device_evidence_policies WHERE id=?',(identifier,)).fetchone()
        if row is None:raise JobError('Self-check evidence policy not found',404)
        return json.loads(row[0])

    def save(self,policy,db,action):
        db.execute('UPDATE device_evidence_policies SET record=? WHERE id=?',(json.dumps(policy),policy['id']));event(db,policy,action)

    def source_plan(self,spec,db,identities):
        plan=self.jobs.device_schedules.plan(spec['source_plan_id'],db)
        if plan['state']!='active' or not plan['authorization']:raise JobError('Explicitly activate a prepared golden daily source plan first',409)
        if self.jobs.device_schedules.context(plan['spec'],db,identities)!=plan['context']:raise JobError('Golden source approval/context changed',409)
        return plan

    def anchor(self,spec,db,identities):
        plan=self.source_plan(spec,db,identities)
        source=self.jobs.nodes.node(plan['spec']['node_id'],db)
        from environment_contract import fingerprint
        targets={}
        for identifier in spec['target_node_ids']:
            node=self.jobs.nodes.node(identifier,db);profile=node['profile']
            if not self.jobs.device_schedules.fresh(node,time.time()) or not profile.get('environment_manifest'):
                raise JobError('Fresh protected consumer node and certified environment required',409)
            cards={str(card):hardware(node,card) for card in plan['spec']['device_ids']}
            if any(cards[str(card)]!=hardware(source,card) for card in plan['spec']['device_ids']):raise JobError('Consumer and source hardware contexts differ',409)
            targets[identifier]={'environment_sha256':fingerprint(profile['environment_manifest']),'cards':cards}
        for project in spec['projects']:
            exists=identities.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone()
            personal=project.removeprefix('personal:') if project.startswith('personal:') else None
            if not exists and not (personal and (personal==self.jobs.owner or identities.execute('SELECT 1 FROM users WHERE id=?',(personal,)).fetchone())):
                raise JobError('Create the explicit consumer projects first',409)
        anchor={'source_authorization':plan['authorization'],'source_context_sha256':digest(plan['context']),
                'source_generation':plan['generation'],'targets':targets}
        if 'performance_references' in spec:
            if set(spec['performance_references'])!={str(c) for c in plan['spec']['device_ids']}:raise JobError('Performance references must cover every source plan card',409)
            anchor['performance_references']={card:self.jobs.performance_references.for_plan(identifier,plan,int(card),db,identities)
                for card,identifier in spec['performance_references'].items()}
        return anchor

    def create(self,value):
        spec=specification(value)
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            row=db.execute('SELECT id FROM device_evidence_policies WHERE name=?',(spec['name'],)).fetchone()
            if row:
                policy=self.policy(row[0],db)
                if policy['spec']!=spec:raise JobError('Evidence policy name already has another immutable definition',409)
                return policy
            if db.execute('SELECT count(*) FROM device_evidence_policies').fetchone()[0]>=MAX_POLICIES:raise JobError('Self-check policy history limit reached',409)
            anchor=self.anchor(spec,db,identities)
            policy={'id':'evidence-'+uuid.uuid4().hex[:24],'spec':spec,'anchor':anchor,'state':'paused','approval':None,
                    'revision':0,'generation':0,'created_at':now()}
            db.execute('INSERT INTO device_evidence_policies VALUES(?,?,?)',(policy['id'],spec['name'],json.dumps(policy)))
            db.executemany('INSERT INTO device_evidence_targets VALUES(?,?,?)',[(policy['id'],n,p) for n in spec['target_node_ids'] for p in spec['projects']])
            event(db,policy,'created')
            return policy

    def applicable(self,node_id,project,db):
        records=[self.policy(i,db) for i, in db.execute('SELECT policy_id FROM device_evidence_targets WHERE node_id=? AND project=?',(node_id,project))]
        return [p for p in records if p['generation']>0 and p['state']!='retired']

    def control(self,identifier,action,replaces=()):
        if (action not in {'enable','pause','retire'} or not isinstance(replaces,(list,tuple)) or len(replaces)>MAX_POLICIES
            or any(not isinstance(i,str) or not re.fullmatch('evidence-[0-9a-f]{24}',i) for i in replaces)
            or len(set(replaces))!=len(replaces)):raise JobError('Invalid evidence policy operation')
        if replaces and action!='enable':raise JobError('Replacement is only valid with enable')
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            policy=self.policy(identifier,db)
            if policy['state']=='retired':
                if action=='retire':return policy
                raise JobError('Retired policy cannot be enabled',409)
            if action=='enable':
                if policy['state']=='restore_paused':raise JobError('Restored evidence policy is historical; create a fresh policy and explicit replacement',409)
                if policy['state']=='active':
                    if replaces:raise JobError('Active approval cannot silently replace another policy',409)
                    return policy
                anchor=self.anchor(policy['spec'],db,identities);conflicts={}
                targets={(n,p) for n in policy['spec']['target_node_ids'] for p in policy['spec']['projects']}
                for n,p in targets:
                    for other in self.applicable(n,p,db):
                        if other['id']!=identifier:conflicts[other['id']]=other
                if set(conflicts)!=set(replaces):raise JobError('Explicitly replace every overlapping retained approval in one transaction',409)
                for other in conflicts.values():
                    old_targets={(n,p) for n in other['spec']['target_node_ids'] for p in other['spec']['projects']}
                    if not old_targets<=targets:raise JobError('Replacement must preserve every old consumer scope; retire separately to remove a scope',409)
                    other.update(state='retired',approval=None,revision=other['revision']+1);self.save(other,db,'replaced')
                policy.update(state='active',approval=uuid.uuid4().hex,anchor=anchor,generation=policy['generation']+1)
            else:policy.update(state='paused' if action=='pause' else 'retired',approval=None)
            policy['revision']+=1;self.save(policy,db,action);return policy

    def record(self,plan,run,db,identities=None):
        if run['state'] not in FINAL_RUNS or db.execute('SELECT 1 FROM device_evidence_checks WHERE id=?',(run['id'],)).fetchone():return
        if db.execute('SELECT count(*) FROM device_evidence_checks').fetchone()[0]>=MAX_CHECKS:raise JobError('Self-check evidence history full',409)
        ut=next((a for a in run['actions'] if a['kind']=='ut' and a['job_id']),None)
        source=self.jobs.get(ut['job_id'],db) if ut else None
        outcome=run.get('outcome') or {}
        completed=(source.get('result') or {}).get('execution_finished_at') if source else None
        interval=False
        if source:
            try:interval=epoch(source['reserved_at'])-15<=epoch(completed)<=time.time()+1
            except (ValueError,TypeError,KeyError,OverflowError):pass
        record={'id':run['id'],'plan_id':plan['id'],'authorization':run['authorization'],'device_id':run['device_id'],
            'state':run['state'],'source_job_id':source['id'] if source else None,'source_attempt_id':source.get('attempt_id') if source else None,
            'source_finished_at':completed,'recorded_at':now(),
            'source_context_sha256':run['context_sha256'],'hardware_sha256':source.get('_device_hardware_sha256') if source else None,
            'report_sha256':outcome.get('report_sha256'),'source_environment_sha256':source.get('environment_sha256') if source else None,
            'bounded_microchecks_passed':run['state']=='microchecks_passed' and source is not None and bool(source.get('_artifact_receipt')) and interval
                and bool(re.fullmatch('[0-9a-f]{64}',str(source.get('_device_hardware_sha256')))),
            'full_hardware_certified':False,'performance_qualified':False}
        if source and identities is not None:
            record['performance_sample']=self.jobs.performance_references.capture(source,db,identities)
        db.execute('INSERT INTO device_evidence_checks VALUES(?,?,?,?)',(run['id'],plan['id'],run['device_id'],json.dumps(record)))

    def latest(self,plan_id,device,db):
        row=db.execute('SELECT record FROM device_evidence_checks WHERE plan_id=? AND device_id=? ORDER BY rowid DESC LIMIT 1',(plan_id,device)).fetchone()
        return json.loads(row[0]) if row else None

    def decision(self,job,node,device,db,identities,at=None,coverage=0,cache=None):
        cache={} if cache is None else cache
        at=time.time() if at is None else at
        policies=self.applicable(node['id'],job['project'],db)
        if not policies:return {'status':'not_configured','required':False}
        if identities is None:
            # Feasibility previews hold a read snapshot, never a start permit.
            # Real claim/start/finalization supply the shared identity transaction.
            with self.jobs.access.connect() as users:
                return self.decision(job,node,device,db,users,at,coverage,cache)
        if len(policies)!=1:return {'status':'conflicting_approvals','required':True}
        policy=policies[0];spec=policy['spec'];anchor=policy['anchor']
        tag=('approval',policy['id'],policy['revision'])
        if tag not in cache:cache[tag]=approval_sha(policy)
        result={'status':'approval_required','required':spec['mode']=='require','policy_id':policy['id'],
                'approval_sha256':cache[tag],'policy_revision':policy['revision'],'policy_generation':policy['generation'],
                'max_age_seconds':spec['max_age_seconds'],'full_hardware_certified':False,'performance_qualified':False}
        if policy['state']!='active' or not policy['approval']:return result
        try:
            key=('source',spec['source_plan_id'])
            if key not in cache:cache[key]=self.source_plan(spec,db,identities)
            plan=cache[key]
            if plan['authorization']!=anchor['source_authorization'] or digest(plan['context'])!=anchor['source_context_sha256']:
                result['status']='context_changed';return result
            if not self.jobs.device_schedules.fresh(node,time.time()):result['status']='context_unavailable';return result
            key=('hardware',node['id'],device)
            if key not in cache:cache[key]=hardware(node,device)
            current=cache[key];target=anchor['targets'][node['id']]
            from environment_contract import fingerprint
            if (not node['profile'].get('environment_manifest')
                or fingerprint(node['profile']['environment_manifest'])!=target['environment_sha256']
                or node['report'].get('environment_sha256')!=target['environment_sha256']):
                result['status']='context_changed';return result
            if target['cards'].get(str(device))!=current:result['status']='context_changed';return result
        except (JobError,ValueError,KeyError,TypeError):result['status']='context_unavailable';return result
        result['hardware_sha256']=current
        check=self.latest(spec['source_plan_id'],device,db)
        if check is None:result['status']='missing';return result
        result.update(check_id=check['id'],source_job_id=check['source_job_id'],source_attempt_id=check['source_attempt_id'],
                      source_environment_sha256=check['source_environment_sha256'],report_sha256=check['report_sha256'],
                      source_check_state=check['state'],bounded_microchecks_passed=check['bounded_microchecks_passed'])
        if check['authorization']!=anchor['source_authorization'] or check['source_context_sha256']!=anchor['source_context_sha256']:
            result['status']='context_changed';return result
        if not check['bounded_microchecks_passed']:result['status']='latest_check_unconfirmed';return result
        if check['hardware_sha256']!=current:result['status']='context_changed';return result
        try:
            completed=epoch(check['source_finished_at']);expires=completed+spec['max_age_seconds']
            if not 0<=at-completed:result['status']='invalid_check_time';return result
            self.jobs.lifecycle.available('archive',check['source_job_id'],db)
        except (JobError,ValueError,TypeError,OverflowError):result['status']='evidence_unavailable';return result
        result.update(source_finished_at=check['source_finished_at'],expires_at=expires)
        result['status']='expired' if at>=expires else 'insufficient_validity' if result['required'] and at+coverage>expires else 'recent_microchecks'
        if str(device) in anchor.get('performance_references',{}):
            ref=anchor['performance_references'][str(device)];key=('performance-reference',ref['id'],check['id'],at,coverage)
            if key not in cache:cache[key]=self.jobs.performance_references.compare(ref,check,db,identities,at,coverage if result['required'] else 0)
            result['performance_reference']=cache[key]
            if result['performance_reference']['status']!='within_reference_envelope' and result['status']=='recent_microchecks':
                result['status']='performance_reference_unconfirmed'
        return result

    def eligible(self,job):
        return bool(job['resources']['npu'] and job.get('template') in {'accuracy_v1','performance_v1','simulation_v1'} and not job.get('_device_schedule'))

    def gate(self,job,node,device,db,identities,cache=None):
        if not self.eligible(job):return None
        result=self.decision(job,node,device,db,identities,coverage=job['timeout_seconds']+START_MARGIN,cache=cache)
        return None if not result['required'] or result['status']=='recent_microchecks' else '等待项目要求的固定逐卡自检证据：'+result['status']

    def aging_blocked(self,job,target,db,identities):
        if not self.eligible(job) or target['node_id']==self.jobs.backend.node_id:return False
        if not any(p['spec']['mode']=='require' for p in self.applicable(target['node_id'],job['project'],db)):return False
        try:
            node=self.jobs.nodes.node(target['node_id'],db);domain=self.jobs.resources.get(target['physical_id'],db)
            return bool(self.jobs.nodes.placement(job,node,domain,[],db,identities)['reason'])
        except (JobError,ValueError,KeyError,TypeError):return True

    def reserve(self,job,node,db,identities):
        if not self.eligible(job):return
        result=self.decision(job,node,job['allocation']['npu_ids'][0],db,identities)
        if result['status']!='not_configured':job['_selfcheck_reservation']=result

    def start(self,job,node,db,identities):
        if not self.eligible(job):return None
        result=self.decision(job,node,job['allocation']['npu_ids'][0],db,identities,coverage=job['timeout_seconds']+START_MARGIN)
        if result['required'] and result['status']!='recent_microchecks':return '项目自检证据在领取后失效；未授予启动许可：'+result['status']
        if result['status']!='not_configured':job['_selfcheck_before']={**result,'captured_at':now()}
        return None

    def finish(self,job,result,node,db,identities,condition_status):
        if not self.eligible(job):return None
        before=job.get('_selfcheck_before')
        if before is None:
            if not job.get('_selfcheck_reservation') and not self.applicable(node['id'],job['project'],db):return None
            return {'status':'not_captured_before_start','full_hardware_certified':False,'performance_qualified':False}
        value={'status':'unconfirmed','before':copy.deepcopy(before),'full_hardware_certified':False,'performance_qualified':False}
        if before['status']!='recent_microchecks':value['status']='unconfirmed_before_start';return value
        try:
            end=epoch(result.get('execution_finished_at'))
            if not job.get('_start_authorized') or not epoch(before['captured_at'])-15<=end<=time.time()+1:raise ValueError()
        except (ValueError,TypeError,OverflowError):value['status']='execution_interval_unconfirmed';return value
        value['execution_finished_at']=result['execution_finished_at']
        if end>=before['expires_at']:value['status']='expired_during_execution';return value
        current=self.decision(job,node,job['allocation']['npu_ids'][0],db,identities,at=end)
        value['at_completion']={k:v for k,v in current.items() if k not in {'max_age_seconds'}}
        if current.get('approval_sha256')!=before.get('approval_sha256') or current.get('hardware_sha256')!=before.get('hardware_sha256'):
            value['status']='approval_or_context_changed';return value
        # A later successful check cannot repair a missing/expired original
        # start proof, but it also does not erase an already valid original.
        later_pass=current['status']=='invalid_check_time' and current.get('bounded_microchecks_passed') is True
        if current['status']!='recent_microchecks' and not later_pass:value['status']='latest_check_unconfirmed';return value
        if condition_status!='sampled_no_alerts':value['status']='task_conditions_unconfirmed';return value
        value['status']='linked_microchecks';return value

    def pins(self,kind,identifier,db):
        if kind!='archive':return False
        if any(j['state'] in ACTIVE and any(j.get(key,{}).get('source_job_id')==identifier for key in ['_selfcheck_reservation','_selfcheck_before']) for j in self.jobs.all(db)):return True
        for raw, in db.execute('SELECT record FROM device_evidence_policies'):
            policy=json.loads(raw)
            if policy['state']!='retired' and policy['generation']>0:
                for card in policy['anchor']['targets'].values():
                    for device in card['cards']:
                        check=self.latest(policy['spec']['source_plan_id'],int(device),db)
                        if check and check['source_job_id']==identifier:return True
        return False

    def public(self,principal,limit=4,offset=0):
        principal.require('jobs:read')
        if type(limit) is not int or not 1<=limit<=8 or type(offset) is not int or offset<0:raise JobError('Invalid evidence policy page')
        if principal.experiment_id:return {'policies':[],'total':0,'limit':limit,'offset':offset}
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            visible=[]
            for raw, in db.execute('SELECT record FROM device_evidence_policies'):
                policy=json.loads(raw);spec=policy['spec'];projects=[p for p in spec['projects'] if principal.admin or self.jobs.access.project_role(principal,p)]
                if projects:visible.append((policy,projects))
            values=[];cache={}
            for policy,projects in visible[offset:offset+limit]:
                spec=policy['spec']
                cards=[]
                for node_id,target in policy['anchor']['targets'].items():
                    try:node=self.jobs.nodes.node(node_id,db)
                    except JobError:node=None
                    for card in target['cards']:
                        probe={'project':projects[0]}
                        status=(self.decision(probe,node,int(card),db,identities,cache=cache) if node else {'status':'context_unavailable'}) if policy['state']!='retired' and policy['generation'] else {'status':policy['state']}
                        cards.append({'node_id':node_id,'device_id':int(card),'evidence':status})
                values.append({'id':policy['id'],'name':spec['name'],'mode':spec['mode'],'state':policy['state'],'generation':policy['generation'],
                    'projects':projects,'max_age_seconds':spec['max_age_seconds'],'cards':cards})
            return {'policies':values,'total':len(visible),'limit':limit,'offset':offset,'capability':CAPABILITY}

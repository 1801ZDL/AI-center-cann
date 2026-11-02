"""Central daily state machines using synthetic authenticated device receipts.

No device files, Torch, model, timer or host administrator account is used.
"""
import copy
import base64
import hashlib
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import test_device_qualification as devices
import test_environment_contract as environments
import test_npu_selfcheck as micro
import device_schedule as schedule
from environment_contract import CAPABILITY as ENV,fingerprint
from jobs import JobError,JobStore
from deployment import CORE,DeployError
from maintenance import require_features
from queue_policy import ordered,protected
from lab_client import Client,ClientError
from server import ThreadingHTTPServer,handler_class
from unittest.mock import Mock


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=devices.QualificationTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.jobs=self.fixture.jobs;self.root=self.fixture.root;self.node=self.fixture.node
        self.epoch=time.time();self.clock=patch('device_schedule.time.time',side_effect=lambda:self.epoch);self.clock.start();self.addCleanup(self.clock.stop)
        self.jobs.access.add_member('device-health','alice','manager')
        self.manifest=environments.contract();self.manifest['observed']['packages'].update(torch='2.6.0',torch_npu='2.6.0')
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.node,db);profile=node['profile'];profile.update(environment_manifest=self.manifest,environment_setup=None,python='/usr/bin/python3')
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.node))
        self.beat()
        wall=dt.datetime.fromtimestamp(self.epoch,dt.timezone.utc)
        self.spec={'name':'daily-test','owner':'alice','project':'device-health','node_id':self.node,'device_ids':[0,1],
            'timezone':'UTC','at':wall.strftime('%H:%M'),'window_minutes':240,'memory_mib':512,
            'max_probes_per_card':2,'daily_npu_seconds':840,'total_npu_seconds':1680}
    def beat(self,busy=None):
        self.fixture.beat(capabilities=['start-ticket-v1','cleanup-before-complete-v1','npu-device-whitelist-v1',devices.CAPABILITY,ENV,*devices.CAPABILITIES],
            image_sha256=self.manifest['image_sha256'],environment_setup=None,python_executable='/usr/bin/python3',environment_sha256=fingerprint(self.manifest),
            npu_observation={'status':'ok','sampled_at':dt.datetime.fromtimestamp(self.epoch-.001,dt.timezone.utc).isoformat(),'policy_sha256':devices.npu_sha(self.fixture.policy),
                'devices':[{'id':i,'name':'Ascend-test','health':'OK','utilization_pct':0,'process_count':1 if i==busy else 0} for i in [0,1]]})
    def create(self,**change):
        plan=self.jobs.device_schedules.create({**self.spec,**change});self.identifier=plan['id'];return plan
    def enable(self,**change):
        self.create(**change);return self.jobs.device_schedules.control(self.identifier,'enable')
    def plan(self):
        with self.jobs.connect() as db:return self.jobs.device_schedules.plan(self.identifier,db)
    def runs(self):
        with self.jobs.connect() as db:return [json.loads(raw) for raw, in db.execute('SELECT record FROM device_schedule_runs ORDER BY rowid')]
    def tick(self):self.beat();self.jobs.device_schedules.tick()
    def one_probe(self):
        self.enable(device_ids=[0]);self.tick();return self.runs()[0]['actions'][0]['job_id']
    def reopen(self):
        self.jobs.access.close();self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        self.fixture.jobs=self.jobs;self.addCleanup(self.jobs.access.close)
    def test_default_empty_and_created_plan_is_paused_without_jobs(self):
        self.jobs.device_schedules.tick();self.assertFalse(self.jobs.all())
        plan=self.create();self.assertEqual(plan['state'],'paused');self.jobs.device_schedules.tick();self.assertFalse(self.jobs.all())
        self.assertTrue(self.jobs.inputs.get(plan['input_id'],self.jobs.access.user('alice'))['sealed'])
    def test_outside_window_no_job_and_busy_card_does_not_hide_idle_card(self):
        self.enable();self.epoch-=3600;self.beat();self.jobs.device_schedules.tick();self.assertFalse(self.jobs.all())
        self.epoch+=3600;self.beat(busy=0);self.jobs.device_schedules.tick()
        self.assertEqual(len(self.jobs.all()),1);self.assertEqual(self.jobs.all()[0]['parameters']['device_id'],1)
    def test_repeated_ticks_and_restart_bind_one_job_and_one_budget_charge(self):
        first=self.one_probe()
        for _ in range(3):self.tick()
        self.reopen();self.tick()
        self.assertEqual([j['id'] for j in self.jobs.all()],[first]);self.assertEqual(self.plan()['total_seconds'],120)
        self.assertEqual(self.runs()[0]['actions'][0]['job_id'],first)
    def test_interrupt_after_atomic_binding_recovers_same_job_without_second_charge(self):
        self.enable(device_ids=[0])
        with patch('device_schedule._checkpoint',side_effect=lambda stage:(_ for _ in ()).throw(RuntimeError('lost acknowledgement')) if stage=='after_submit' else None):
            with self.assertRaises(RuntimeError):self.tick()
        original=self.jobs.all()[0]['id'];self.reopen();self.tick()
        self.assertEqual(self.jobs.all()[0]['id'],original);self.assertEqual(self.plan()['total_seconds'],120)
    def test_rollback_inside_job_binding_leaves_intent_but_no_job_or_charge(self):
        self.enable(device_ids=[0])
        with patch('device_schedule._checkpoint',side_effect=lambda stage:(_ for _ in ()).throw(RuntimeError('interrupted transaction')) if stage=='job_bound' else None):
            with self.assertRaises(RuntimeError):self.tick()
        self.assertFalse(self.jobs.all());self.assertEqual(self.plan()['total_seconds'],0)
        self.tick();self.assertEqual(len(self.jobs.all()),1);self.assertEqual(self.plan()['total_seconds'],120)
    def test_foreground_precedes_aged_background_and_unknown_keeps_original_lease(self):
        first=self.one_probe();foreground,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('bob'))
        rows=self.jobs.all();self.assertEqual(ordered(rows,self.jobs.policy)[0]['id'],foreground['id'])
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],foreground['id'])
        self.jobs.nodes.complete(self.node,{**{k:grant[k] for k in ['job_id','attempt_id','lease_token']},'result':{'status':'cancelled','termination_confirmed':True,'execution_cleanup_confirmed':True},'artifacts':[]})
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],first)
        self.jobs.nodes.update(self.node,{**{k:grant[k] for k in ['job_id','attempt_id','lease_token']},'attention':True})
        self.epoch+=15000;self.tick();self.assertEqual(len(self.jobs.all()),2)
        self.assertEqual(self.jobs.get(first)['state'],'needs_attention');self.assertEqual(self.jobs.summary()['reserved']['npu_ids'],[0])
    def test_probe_failure_stops_this_card_without_automatic_reexecution(self):
        first=self.one_probe();self.fixture.complete_probe(False)
        self.tick();self.tick();self.assertEqual(len(self.jobs.all()),1);self.assertEqual(self.runs()[0]['state'],'access_failed')
        self.assertEqual(self.plan()['total_seconds'],120)
    def test_successful_access_then_queued_ut_expiry_reprobes_but_keeps_ut_id(self):
        self.one_probe();self.fixture.complete_probe();self.tick()
        ut=next(j for j in self.jobs.all() if j['template']=='accuracy_v1');self.assertEqual(ut['placement']['device_ids'],[0])
        self.epoch+=1801;self.tick();self.assertEqual(len(self.jobs.all()),3)
        self.assertEqual(self.jobs.get(ut['id'])['state'],'queued');self.assertEqual(self.plan()['total_seconds'],420)
        self.fixture.complete_probe();self.tick();self.assertEqual(len(self.jobs.all()),3)
        self.assertEqual(self.jobs.get(ut['id'])['state'],'queued')
    def test_probe_limit_does_not_loop_and_unmeasured_ut_is_cancelled_without_attempt(self):
        self.one_probe();self.fixture.complete_probe();self.tick();ut=next(j for j in self.jobs.all() if j['template']=='accuracy_v1')
        self.epoch+=1801;self.tick();self.fixture.complete_probe();self.epoch+=1801;self.tick()
        self.assertEqual(len(self.jobs.all()),3);self.assertEqual(self.runs()[0]['state'],'probe_limit')
        self.assertEqual(self.jobs.get(ut['id'])['state'],'cancelled');self.assertFalse(self.jobs.attempts(ut['id']))
    def test_daily_budget_survives_pause_and_new_authorization_same_day(self):
        self.enable(device_ids=[0],daily_npu_seconds=300);self.tick();self.fixture.complete_probe();self.tick()
        self.assertEqual(self.plan()['total_seconds'],300)
        self.jobs.device_schedules.control(self.identifier,'pause');self.jobs.device_schedules.control(self.identifier,'enable');self.tick()
        self.assertEqual(self.plan()['total_seconds'],300);self.assertEqual(len(self.jobs.all()),2)
        self.assertEqual(self.runs()[-1]['state'],'budget_exhausted')
    def test_identity_disable_reenable_never_restores_authority_automatically(self):
        first=self.one_probe();self.jobs.access.disable_user('alice');self.tick();self.assertEqual(self.plan()['state'],'context_changed')
        self.assertEqual(self.jobs.get(first)['state'],'cancelled')
        self.jobs.access.disable_user('alice',False);self.tick();self.assertEqual(self.plan()['state'],'context_changed')
    def test_new_day_gets_new_runs_clock_backwards_does_not_backfill(self):
        self.enable(device_ids=[0]);self.tick();self.jobs.device_schedules.control(self.identifier,'pause')
        self.jobs.device_schedules.control(self.identifier,'enable');self.epoch+=86400;self.tick();count=len(self.runs())
        self.epoch-=172800;self.tick();self.assertEqual(len(self.runs()),count)
    def test_restore_freezes_unattended_approval_and_preserves_charge(self):
        self.one_probe();self.jobs.device_schedules.control(self.identifier,'pause')
        snapshot=self.root/'snapshot.sqlite3'
        with self.jobs.connect() as source,sqlite3.connect(snapshot) as target:source.backup(target)
        schedule.invalidate_restored(snapshot)
        with sqlite3.connect(snapshot) as db:
            value=json.loads(db.execute('SELECT record FROM device_schedule_plans').fetchone()[0])
            self.assertEqual(value['state'],'restore_paused');self.assertIsNone(value['authorization']);self.assertEqual(value['total_seconds'],120)
    def test_older_snapshot_cannot_drop_retained_charges_or_original_actions(self):
        self.enable(device_ids=[0]);old=self.root/'older';old.mkdir()
        with self.jobs.connect() as source,sqlite3.connect(old/'jobs.sqlite3') as target:source.backup(target)
        self.tick()
        with self.assertRaises(DeployError):schedule.require_restore_history(self.root,old,'controller')
        fresh=self.root/'latest';fresh.mkdir()
        with self.jobs.connect() as source,sqlite3.connect(fresh/'jobs.sqlite3') as target:source.backup(target)
        schedule.require_restore_history(self.root,fresh,'controller')
    def test_input_pinned_until_retire_and_old_code_downgrade_refused(self):
        plan=self.create()
        with self.assertRaises(JobError):self.jobs.lifecycle.plan(self.jobs.access.user('alice'),{'kind':'input','id':plan['input_id'],'action':'trash'})
        with self.assertRaisesRegex(DeployError,'Daily-check'):
            require_features(self.root,'controller',{'files':{n:{} for n in CORE if n!='device_schedule.py'}})
        self.jobs.device_schedules.control(plan['id'],'retire')
        self.jobs.lifecycle.plan(self.jobs.access.user('alice'),{'kind':'input','id':plan['input_id'],'action':'trash'})
    def test_project_state_private_to_owner_members_and_no_http_write_authority(self):
        self.create();self.assertEqual(self.jobs.device_schedules.public(self.jobs.access.user('alice'))['total'],1)
        self.assertEqual(self.jobs.device_schedules.public(self.jobs.access.user('bob'))['total'],0)
        self.jobs.access.add_member('device-health','bob','viewer')
        data=self.jobs.device_schedules.public(self.jobs.access.user('bob'));self.assertEqual(data['total'],1)
        self.assertNotIn('context',json.dumps(data));self.assertFalse(data['plans'][0]['health_certified'])
    def test_original_request_cannot_be_changed_or_public_marker_forged(self):
        self.enable(device_ids=[0]);self.tick();run=self.runs()[0];marker={'plan_id':self.identifier,'run_id':run['id'],'index':0}
        request=copy.deepcopy(run['actions'][0]['request']);request['parameters']['device_id']=1
        with self.assertRaises(JobError):self.jobs.submit(request,self.jobs.access.user('alice'),_scheduled=marker)
        with self.assertRaises(JobError):self.jobs.submit({**request,'_device_schedule':marker},self.jobs.access.user('alice'))
    def test_next_day_unknown_execution_does_not_create_a_replacement_probe(self):
        first=self.one_probe();grant=self.jobs.nodes.claim(self.node)['grant']
        self.jobs.nodes.update(self.node,{**{k:grant[k] for k in ['job_id','attempt_id','lease_token']},'attention':True})
        self.epoch+=86400;self.tick();self.assertEqual(len(self.jobs.all()),1)
        self.assertEqual(self.runs()[-1]['state'],'waiting_prior_execution');self.assertEqual(self.runs()[-1]['prior_job_id'],first)
    def test_atomic_queued_only_cancel_does_not_interrupt_a_racing_grant(self):
        first=self.one_probe();grant=self.jobs.nodes.claim(self.node)['grant']
        self.jobs.cancel(first,_queued_only=True)
        job=self.jobs.get(first);self.assertEqual(job['state'],'reserved');self.assertFalse(job.get('_cancel_requested'))
    def complete_microcheck(self,precision=True,adverse=False):
        self.one_probe();self.fixture.complete_probe();self.tick()
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['template'],'accuracy_v1')
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']};self.jobs.nodes.authorize_start(self.node,auth)
        mapping=self.jobs.nodes.node(self.node)['profile']['users']['alice'];ids=grant['allocation']['npu_ids']
        access={'schema':'ascend-lab-device-access/v1','status':'passed','passed':True,'requested_device_ids':ids,'opened_device_ids':ids,
            'physical_device_files':ids,'errors':[],'uid':mapping['uid'],'euid':mapping['uid'],'cap_effective':'0000000000000000','cap_permitted':'0000000000000000','no_new_privileges':'1'}
        receipt={'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'workload_exit_code':0 if precision else 1,
            'observed':{**self.manifest['observed'],'npu_name':'Ascend-test'},'device_access':access}
        report=micro.report()
        if not precision:report['checks'][0]['values'][0]+=1
        result={'schema':'ascend-lab-result/v1','kind':'accuracy','passed':precision,'checks':[
            {'passed':precision,'max_abs_error':0 if precision else 1,'atol':0,'rtol':0}]}
        meta={'runtime':'managed-docker','task_uid':mapping['uid'],'task_gid':mapping['gid'],'privileged':False,'read_only_root':True,
            'network_mode':'none','image':self.manifest['image_sha256'],'runtime_files':grant['runtime_files']}
        files=[]
        for name,value in [('results/_lab/environment.json',receipt),('environment.json',meta),('results/workload-result.json',result),('results/device-microcheck.json',report)]:
            raw=json.dumps(value).encode();self.jobs.nodes.upload(self.node,{**auth,'name':name,'offset':0,'data':base64.b64encode(raw).decode()})
            files.append({'name':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        reading={'sampled_at':dt.datetime.fromtimestamp(self.epoch-.001,dt.timezone.utc).isoformat(),'policy_sha256':devices.npu_sha(self.fixture.policy),
            'devices':[{'id':ids[0],'health':'OK','process_count':0,'utilization_pct':0}]}
        if adverse:
            self.epoch+=.01
            body={'status':'ok','sampled_at':dt.datetime.fromtimestamp(self.epoch-.001,dt.timezone.utc).isoformat(),'policy_sha256':devices.npu_sha(self.fixture.policy),
                  'devices':[{'id':i,'name':'Ascend-test','health':'Warning' if i==ids[0] else 'OK','process_count':0,'utilization_pct':0} for i in [0,1]]}
            self.fixture.beat(npu_observation=body)
        self.jobs.nodes.complete(self.node,{**auth,'artifacts':files,'result':{'status':'succeeded' if precision else 'failed','exit_code':0 if precision else 1,
            'termination_confirmed':True,'execution_cleanup_confirmed':True,'npu_release_confirmed':True,'npu_release_basis':'clear-device-observation',
            'npu_admission_observation':reading,'npu_release_observation':reading}})
        self.tick();return self.jobs.get(grant['job_id']),self.runs()[0]
    def test_complete_probe_to_microcheck_pipeline_recomputes_archive_without_health_certification(self):
        job,run=self.complete_microcheck();self.assertEqual(job['state'],'succeeded',job.get('workflow_result'))
        self.assertEqual(run['state'],'microchecks_passed');self.assertEqual(self.plan()['total_seconds'],300)
        self.assertFalse(run['outcome']['health_certified']);self.assertFalse(run['outcome']['performance_qualified'])
    def test_precision_failure_preserved_and_adverse_conditions_not_promoted(self):
        job,run=self.complete_microcheck(False);self.assertEqual(job['state'],'failed');self.assertEqual(run['state'],'microchecks_failed')
        self.assertEqual(run['outcome']['comparison']['checks'][0]['mismatches'],1)
    def test_adverse_management_snapshot_is_not_promoted_by_matching_values(self):
        job,run=self.complete_microcheck(adverse=True)
        self.assertEqual(job['state'],'succeeded');self.assertEqual(run['state'],'conditions_unconfirmed')
    def test_window_expiry_cancels_only_known_queued_probe_without_allocating(self):
        first=self.one_probe();self.epoch+=15000;self.tick()
        self.assertEqual(self.runs()[0]['state'],'expired_unmeasured');self.assertEqual(self.jobs.get(first)['state'],'cancelled')
        self.assertFalse(self.jobs.attempts(first))
    def test_restored_plan_cannot_be_reenabled_to_resurrect_snapshot_budget(self):
        self.create()
        schedule.invalidate_restored(self.jobs.db_path)
        with self.assertRaisesRegex(JobError,'historical only'):self.jobs.device_schedules.control(self.identifier,'enable')
    def test_bad_timezone_duplicate_cards_and_insufficient_daily_budget_rejected_before_input(self):
        for change in [{'timezone':'missing/timezone'},{'device_ids':[[0]]},{'device_ids':[0,0]}, {'daily_npu_seconds':300}]:
            with self.assertRaises(JobError):self.create(**change)
        self.assertFalse(self.jobs.all())
    def test_two_tick_callers_bind_the_same_pending_action_once(self):
        self.enable(device_ids=[0]);barrier=threading.Barrier(2);errors=[]
        def checkpoint(stage):
            if stage=='before_submit':barrier.wait(5)
        def work():
            try:self.jobs.device_schedules.tick()
            except Exception as error:errors.append(error)
        with patch('device_schedule._checkpoint',side_effect=checkpoint):
            threads=[threading.Thread(target=work) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(10)
        self.assertFalse(errors,errors);self.assertEqual(len(self.jobs.all()),1);self.assertEqual(self.plan()['total_seconds'],120)
    def test_foreground_on_another_entry_of_same_physical_host_gets_priority(self):
        self.one_probe();profile=copy.deepcopy(self.jobs.nodes.node(self.node)['profile']);profile['name']='other-entry'
        peer,_=self.jobs.nodes.enroll(profile)
        original=self.fixture.node;self.fixture.node=peer;self.beat();self.fixture.node=original
        foreground,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python','placement':{'node_id':peer}},self.jobs.access.user('bob'))
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.assertEqual(self.jobs.nodes.claim(peer)['grant']['job_id'],foreground['id'])
    def test_http_read_is_paged_and_owner_scoped_and_has_no_admin_write(self):
        token=self.jobs.access.issue_token('alice',{'jobs:read'});other=self.jobs.access.issue_token('bob',{'jobs:read'})
        self.create()
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),{'username':'operator','password':'offline-only'},self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        url='http://127.0.0.1:'+str(server.server_port);owner=Client({'controller':url,'token':token});peer=Client({'controller':url,'token':other})
        self.assertEqual(owner.request('/api/device-schedules?limit=1')['total'],1)
        self.assertEqual(peer.request('/api/device-schedules')['total'],0)
        for suffix in ['?limit=9','?limit=no','?other=1']:
            with self.assertRaises(ClientError):owner.request('/api/device-schedules'+suffix)
        with self.assertRaises(ClientError):owner.request('/api/device-schedules',{'action':'enable','id':self.identifier})
        self.assertEqual(self.plan()['state'],'paused')


class ClockTests(unittest.TestCase):
    def test_timezone_dst_repeat_and_cross_midnight_window(self):
        spec={'timezone':'America/New_York','at':'01:30','window_minutes':120}
        a=dt.datetime(2026,11,1,5,45,tzinfo=dt.timezone.utc).timestamp();b=a+3600
        first=schedule.window(spec,a);second=schedule.window(spec,b)
        self.assertEqual(first,second);self.assertEqual(first[0],'2026-11-01')
        spec={'timezone':'Asia/Shanghai','at':'23:30','window_minutes':120}
        epoch=dt.datetime(2026,10,31,16,10,tzinfo=dt.timezone.utc).timestamp()
        day,start,end=schedule.window(spec,epoch);self.assertEqual(day,'2026-10-31');self.assertTrue(start<=epoch<=end)


if __name__=='__main__':unittest.main()

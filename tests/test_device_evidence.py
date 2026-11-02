"""Project self-check binding with synthetic protected captures, never a device run."""
import base64
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import test_device_schedule as daily_cases
import device_evidence as evidence
from jobs import JobError,JobStore
from deployment import CORE,DeployError
from maintenance import require_features
from server import ThreadingHTTPServer,handler_class
from lab_client import Client,ClientError


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.daily=daily_cases.DailyTests('runTest');self.daily.setUp();self.addCleanup(self.daily.doCleanups)
        self.daily.epoch=dt.datetime.fromtimestamp(self.daily.epoch,dt.timezone.utc).timestamp()
        self.jobs=self.daily.jobs;self.node=self.daily.node
        for name in ['jobs.now','nodes.now','executor.now','device_schedule.now','device_evidence.now']:
            clock=patch(name,side_effect=self.iso);clock.start();self.addCleanup(clock.stop)
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.node,db);node['profile']['templates']=sorted(set(node['profile']['templates'])|{'performance_v1','simulation_v1'})
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(node['profile']),self.node))
        self.daily.beat()
    def iso(self):return dt.datetime.fromtimestamp(self.daily.epoch,dt.timezone.utc).isoformat()
    def source(self,complete=True,precision=True):
        if complete:
            job,_=self.daily.complete_microcheck(precision);self.source_job=job;return job
        self.daily.enable(device_ids=[0])
    def policy(self,mode='require',ttl=3600,name='business-check',enable=True,targets=None):
        value=self.jobs.device_evidence.create({'name':name,'source_plan_id':self.daily.identifier,
            'target_node_ids':targets or [self.node],'projects':['personal:bob'],'max_age_seconds':ttl,'mode':mode})
        self.policy_id=value['id']
        return self.jobs.device_evidence.control(value['id'],'enable') if enable else value
    def bob_access(self):
        self.daily.fixture.submit_probe('bob',0);self.daily.fixture.complete_probe()
    def consumer(self,kind='accuracy',timeout=30):
        user=self.jobs.access.user('bob');raw=b'pass\n';sha=hashlib.sha256(raw).hexdigest()
        bundle,_=self.jobs.inputs.create(user,{'files':[{'name':'main.py','size':len(raw),'sha256':sha}]})
        if not bundle['sealed']:
            self.jobs.inputs.upload(bundle['id'],user,{'name':'main.py','offset':0,'data':base64.b64encode(raw).decode()});self.jobs.inputs.seal(bundle['id'],user)
        return self.jobs.submit({'template':kind+'_v1','environment':'worker-python','parameters':{'input_id':bundle['id'],
            'argv':['python','main.py'],'lock':{'python':'3.10.12'},'timeout_seconds':timeout,
            'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':1}}},user)[0]
    def decide(self,job):
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            return self.jobs.device_evidence.decision(job,self.jobs.nodes.node(self.node,db),0,db,identities)
    def grant(self,job,start=True):
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertIsNotNone(grant,self.jobs.get(job['id']).get('reason'))
        self.assertEqual(grant['job_id'],job['id']);auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        if start:self.assertTrue(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted'])
        return grant,auth
    def advance(self,seconds):
        while seconds>0:
            step=min(seconds,10);self.daily.epoch+=step;seconds-=step;self.daily.beat()
    def complete(self,grant,auth,upload_delay=0,adverse=False):
        self.jobs.nodes.update(self.node,{**auth,'running':True});start=self.jobs.get(grant['job_id'])['_selfcheck_before']['captured_at'] if self.jobs.get(grant['job_id']).get('_selfcheck_before') else self.iso()
        mapping=self.jobs.nodes.node(self.node)['profile']['users']['bob'];ids=grant['allocation']['npu_ids']
        access={'schema':'ascend-lab-device-access/v1','status':'passed','passed':True,'requested_device_ids':ids,'opened_device_ids':ids,
            'physical_device_files':ids,'errors':[],'uid':mapping['uid'],'euid':mapping['uid'],'cap_effective':'0000000000000000',
            'cap_permitted':'0000000000000000','no_new_privileges':'1'}
        receipt={'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'workload_exit_code':0,
                 'observed':{**self.daily.manifest['observed'],'npu_name':'Ascend-test'},'device_access':access}
        meta={'runtime':'managed-docker','task_uid':mapping['uid'],'task_gid':mapping['gid'],'privileged':False,'read_only_root':True,
              'network_mode':'none','image':self.daily.manifest['image_sha256'],'runtime_files':grant['runtime_files']}
        kind=grant['template'].removesuffix('_v1')
        output={'schema':'ascend-lab-result/v1','kind':kind,'passed':True,
                **({'samples_us':[30,31,30.5],'conditions':{'source':'synthetic fixture only'}} if kind=='performance' else {'checks':[{'passed':True,'max_abs_error':0,'atol':0,'rtol':0}]})}
        files=[]
        for name,value in [('results/_lab/environment.json',receipt),('environment.json',meta),('results/workload-result.json',output)]:
            raw=json.dumps(value).encode();self.jobs.nodes.upload(self.node,{**auth,'name':name,'offset':0,'data':base64.b64encode(raw).decode()})
            files.append({'name':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        self.advance(1);end=self.iso()
        reading={'sampled_at':self.iso(),'policy_sha256':__import__('npu_device').fingerprint(self.daily.fixture.policy),
                 'devices':[{'id':0,'health':'OK','process_count':0,'utilization_pct':0}]}
        admission={**reading,'sampled_at':start}
        if adverse:
            original=self.daily.fixture.beat
            def warning(**change):
                change['npu_observation']['devices'][0]['health']='Warning';return original(**change)
            with patch.object(self.daily.fixture,'beat',side_effect=warning):self.daily.beat()
        self.advance(upload_delay)
        self.jobs.nodes.complete(self.node,{**auth,'artifacts':files,'result':{'status':'succeeded','exit_code':0,'execution_finished_at':end,
            'termination_confirmed':True,'execution_cleanup_confirmed':True,'npu_release_confirmed':True,
            'npu_release_basis':'clear-device-observation','npu_admission_observation':admission,'npu_release_observation':reading}})
        return self.jobs.get(grant['job_id'])
    def test_default_does_not_change_existing_npu_workflow(self):
        self.bob_access();job=self.consumer();grant,auth=self.grant(job);done=self.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['device_conditions']['daily_ut'],'not_linked')
    def test_fixed_source_index_binds_original_job_report_and_execution_end(self):
        source=self.source()
        with self.jobs.connect() as db:check=self.jobs.device_evidence.latest(self.daily.identifier,0,db)
        self.assertTrue(check['bounded_microchecks_passed']);self.assertEqual(check['source_job_id'],source['id'])
        self.assertEqual(check['source_finished_at'],source['result']['execution_finished_at']);self.assertFalse(check['performance_qualified'])
    def test_required_unmeasured_business_queues_without_deadlocking_source(self):
        self.source(False);self.policy();self.bob_access();job=self.consumer()
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        source=self.source();grant,auth=self.grant(job);done=self.complete(grant,auth)
        self.assertEqual(done['device_conditions']['daily_ut'],'linked_microchecks')
        self.assertEqual(done['device_conditions']['selfcheck']['before']['source_job_id'],source['id'])
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)
    def test_annotate_missing_preserves_original_success_and_does_not_backfill(self):
        self.source(False);self.policy('annotate');self.bob_access();job=self.consumer();grant,auth=self.grant(job)
        done=self.complete(grant,auth);self.assertTrue(done['workflow_result']['passed'],done.get('workflow_result'))
        self.assertEqual(done['device_conditions']['daily_ut'],'unconfirmed_before_start')
        original=json.dumps(done['device_conditions'],sort_keys=True);self.source()
        self.assertEqual(json.dumps(self.jobs.get(job['id'])['device_conditions'],sort_keys=True),original)
    def test_performance_samples_preserved_with_bounded_reference_link(self):
        self.source();self.policy();self.bob_access();job=self.consumer('performance');grant,auth=self.grant(job)
        done=self.complete(grant,auth);self.assertTrue(done['workflow_result']['passed'])
        self.assertEqual(done['workflow_result']['metrics']['mean_us'],30.5)
        self.assertEqual(done['device_conditions']['daily_ut'],'linked_microchecks')
        self.assertEqual(done['device_conditions']['performance_qualification'],'unverified')
        self.assertFalse(done['device_conditions']['selfcheck']['performance_qualified'])
    def test_expiry_while_queued_never_allocates_or_reexecutes(self):
        self.source();self.policy(ttl=100);self.bob_access();job=self.consumer(timeout=1)
        self.advance(101);self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.assertEqual(self.jobs.get(job['id'])['state'],'queued');self.assertFalse(self.jobs.attempts(job['id']))
        self.assertEqual(self.decide(job)['status'],'expired')
    def test_insufficient_duration_queues_and_start_recheck_denies_late_preparation(self):
        self.source();self.policy(ttl=100);self.bob_access();too_long=self.consumer(timeout=101)
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.jobs.cancel(too_long['id'])
        job=self.consumer(timeout=1);grant,auth=self.grant(job,start=False);self.advance(45)
        denied=self.jobs.nodes.authorize_start(self.node,auth);self.assertFalse(denied['start_permitted']);self.assertNotIn('wait',denied)
        self.assertFalse(self.jobs.get(job['id'])['_start_authorized']);self.assertEqual(self.jobs.get(job['id'])['state'],'reserved')
    def test_annotation_expires_during_execution_without_rewriting_result(self):
        self.source();self.policy('annotate',ttl=60);self.bob_access();job=self.consumer(timeout=180);grant,auth=self.grant(job)
        self.advance(61);done=self.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['device_conditions']['daily_ut'],'expired_during_execution')
    def test_delayed_business_upload_does_not_expire_an_on_time_execution(self):
        self.source();self.policy(ttl=100);self.bob_access();job=self.consumer(timeout=1);grant,auth=self.grant(job)
        done=self.complete(grant,auth,upload_delay=120)
        self.assertEqual(done['device_conditions']['daily_ut'],'linked_microchecks')
    def test_delayed_source_upload_does_not_renew_its_age(self):
        original=self.jobs.nodes.complete
        def complete(node,body):
            job=self.jobs.get(body['job_id'])
            if job.get('_device_schedule') and job['template']=='accuracy_v1':self.advance(120)
            return original(node,body)
        with patch.object(self.jobs.nodes,'complete',side_effect=complete):source=self.source()
        self.assertEqual(source['state'],'succeeded');self.policy(ttl=100);self.bob_access();job=self.consumer(timeout=1)
        self.assertEqual(self.decide(job)['status'],'expired');self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
    def test_source_revocation_even_reenabled_bytes_does_not_restore_old_approval(self):
        self.source();self.policy();self.bob_access();job=self.consumer()
        self.jobs.access.disable_user('alice');self.jobs.access.disable_user('alice',False)
        self.assertNotEqual(self.decide(job)['status'],'recent_microchecks');self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
    def test_task_alert_prevents_linked_label_but_keeps_original_result(self):
        self.source();self.policy();self.bob_access();job=self.consumer();grant,auth=self.grant(job);done=self.complete(grant,auth,adverse=True)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['device_conditions']['daily_ut'],'task_conditions_unconfirmed',done['device_conditions'])
    def test_paused_requirement_still_blocks_and_replacement_is_atomic(self):
        self.source();old=self.policy();self.jobs.device_evidence.control(old['id'],'pause');self.bob_access();job=self.consumer()
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        new=self.policy(name='replacement',enable=False)
        with self.assertRaises(JobError):self.jobs.device_evidence.control(new['id'],'enable')
        self.jobs.device_evidence.control(new['id'],'enable',[old['id']])
        self.assertEqual(self.decide(job)['policy_id'],new['id']);self.assertIsNotNone(self.jobs.nodes.claim(self.node)['grant'])
    def test_restore_freezes_requirement_instead_of_bypassing_it(self):
        self.source();policy=self.policy();self.bob_access();job=self.consumer()
        evidence.invalidate_restored(self.jobs.db_path)
        self.assertTrue(self.decide(job)['required']);self.assertEqual(self.decide(job)['status'],'approval_required')
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        with self.assertRaisesRegex(JobError,'historical'):self.jobs.device_evidence.control(policy['id'],'enable')
    def backup(self,label):
        destination=self.daily.root/label;destination.mkdir()
        with self.jobs.connect() as source,sqlite3.connect(destination/'jobs.sqlite3') as target:source.backup(target)
        return destination
    def test_old_snapshot_check_loss_or_target_projection_loss_rejected(self):
        self.source();old=self.backup('before-policy');self.policy()
        with self.assertRaises(DeployError):evidence.require_restore_history(self.daily.root,old,'controller')
        fresh=self.backup('same');evidence.require_restore_history(self.daily.root,fresh,'controller')
        with sqlite3.connect(fresh/'jobs.sqlite3') as db:db.execute('DELETE FROM device_evidence_targets')
        with self.assertRaises(DeployError):evidence.require_restore_history(self.daily.root,fresh,'controller')
    def test_original_archive_pinned_for_active_attempt_even_after_policy_retire(self):
        source=self.source();policy=self.policy();self.bob_access();job=self.consumer();grant,auth=self.grant(job)
        principal=self.jobs.access.user('alice');request={'kind':'archive','id':source['id'],'action':'trash'}
        with self.assertRaisesRegex(JobError,'self-check'):self.jobs.lifecycle.plan(principal,request)
        self.jobs.device_evidence.control(policy['id'],'retire')
        with self.assertRaisesRegex(JobError,'self-check'):self.jobs.lifecycle.plan(principal,request)
        self.complete(grant,auth);self.jobs.lifecycle.plan(principal,request)
    def test_trash_before_policy_can_be_restored_and_never_qualifies_while_missing(self):
        source=self.source();owner=self.jobs.access.user('alice');request={'kind':'archive','id':source['id'],'action':'trash'}
        plan=self.jobs.lifecycle.plan(owner,request);self.jobs.lifecycle.apply(owner,plan['plan'])
        self.policy();self.bob_access();job=self.consumer();self.assertEqual(self.decide(job)['status'],'evidence_unavailable')
        restore=self.jobs.lifecycle.plan(owner,{**request,'action':'restore'});self.jobs.lifecycle.apply(owner,restore['plan'])
        self.assertEqual(self.decide(job)['status'],'recent_microchecks')
    def test_historical_source_code_cannot_be_activated_without_guard_support(self):
        self.source();self.policy()
        with self.assertRaisesRegex(DeployError,'Self-check'):require_features(self.daily.root,'controller',{'files':{n:{} for n in CORE if n!='device_evidence.py'}})
    def test_public_metadata_follows_consumer_projects_without_source_archive_access(self):
        source=self.source();self.policy();bob=self.jobs.access.user('bob');alice=self.jobs.access.user('alice')
        public=self.jobs.device_evidence.public(bob);self.assertEqual(public['total'],1)
        self.assertEqual(self.jobs.device_evidence.public(alice)['total'],0)
        with self.assertRaises(JobError):self.jobs.get(source['id'],principal=bob)
        self.assertNotIn('owner_revision',json.dumps(public));self.assertNotIn('driver_paths',json.dumps(public))
    def test_newer_failed_check_never_falls_back_to_still_fresh_old_pass(self):
        self.daily.epoch+=300;self.daily.beat();source=self.source();self.policy(ttl=86400)
        day,start,end=__import__('device_schedule').window(self.daily.spec,self.daily.epoch)
        self.daily.epoch=start+86400+10;self.daily.beat();failed=self.source(precision=False)
        self.assertLess(self.daily.epoch,evidence.epoch(source['result']['execution_finished_at'])+86400)
        self.assertEqual(failed['state'],'failed');self.bob_access();job=self.consumer()
        state=self.decide(job);self.assertEqual(state['status'],'latest_check_unconfirmed')
        self.assertEqual(state['source_job_id'],failed['id']);self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
    def test_failed_access_attempt_invalidates_the_new_required_source(self):
        self.source(False);self.policy();self.bob_access();job=self.consumer();self.daily.tick()
        self.daily.fixture.complete_probe(False);self.daily.tick()
        decision=self.decide(job);self.assertEqual(decision['status'],'latest_check_unconfirmed')
        self.assertEqual(decision['source_check_state'],'access_failed');self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
    def test_clock_rollback_does_not_make_a_future_check_valid(self):
        self.source();self.policy();self.bob_access();self.daily.epoch-=1;self.daily.beat();job=self.consumer()
        self.assertEqual(self.decide(job)['status'],'invalid_check_time');self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
    def test_two_competing_replacements_leave_exactly_one_explicit_approval(self):
        self.source();old=self.policy();one=self.policy(name='replace-one',enable=False);two=self.policy(name='replace-two',enable=False)
        barrier=threading.Barrier(2);results=[]
        def replace(identifier):
            barrier.wait()
            try:self.jobs.device_evidence.control(identifier,'enable',[old['id']]);results.append('enabled')
            except JobError:results.append('refused')
        threads=[threading.Thread(target=replace,args=(p['id'],)) for p in [one,two]]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertEqual(sorted(results),['enabled','refused'])
        with self.jobs.connect() as db:self.assertEqual(len(self.jobs.device_evidence.applicable(self.node,'personal:bob',db)),1)
    def test_approval_events_bind_start_proof_and_old_snapshot_cannot_drop_them(self):
        self.source();self.policy();self.bob_access();job=self.consumer();grant,auth=self.grant(job)
        before=self.jobs.get(job['id'])['_selfcheck_before']
        with self.jobs.connect() as db:
            events=[json.loads(raw) for raw, in db.execute('SELECT record FROM device_evidence_events')]
        self.assertTrue(any(r['action']=='enable' and r['approval_sha256']==before['approval_sha256'] and r['revision']==before['policy_revision'] for r in events))
        fresh=self.backup('event-loss')
        with sqlite3.connect(fresh/'jobs.sqlite3') as db:db.execute('DELETE FROM device_evidence_events')
        with self.assertRaises(DeployError):evidence.require_restore_history(self.daily.root,fresh,'controller')
    def test_shared_hardware_context_does_not_borrow_a_consumer_access_permission(self):
        self.source();profile=copy.deepcopy(self.jobs.nodes.node(self.node)['profile']);profile['name']='peer-consumer'
        peer,_=self.jobs.nodes.enroll(profile);original=self.daily.fixture.node
        self.daily.fixture.node=peer;self.daily.beat();self.daily.fixture.node=original
        self.policy(targets=[self.node,peer]);self.bob_access();job=self.consumer()
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            result=self.jobs.device_evidence.decision(job,self.jobs.nodes.node(peer,db),0,db,identities)
        self.assertEqual(result['status'],'recent_microchecks');self.assertIsNone(self.jobs.nodes.claim(peer)['grant'])
        # An unrelated target entry going offline must not invalidate this entry.
        with self.jobs.connect() as db:db.execute('UPDATE worker_nodes SET last_seen=0 WHERE id=?',(peer,))
        self.assertEqual(self.decide(job)['status'],'recent_microchecks')
    def test_http_pagination_acl_and_write_refusal_and_budget_scope(self):
        from dataclasses import replace
        bob_token=self.jobs.access.issue_token('bob',{'jobs:read'});alice_token=self.jobs.access.issue_token('alice',{'jobs:read'})
        source=self.source();self.policy()
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),{'username':'operator','password':'offline-only'},self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        url='http://127.0.0.1:'+str(server.server_port);bob=Client({'controller':url,'token':bob_token});alice=Client({'controller':url,'token':alice_token})
        self.assertIn(evidence.CAPABILITY,bob.request('/api/me')['npu_condition_capabilities'])
        self.assertEqual(bob.request('/api/device-evidence?limit=1')['total'],1)
        self.assertEqual(alice.request('/api/device-evidence')['total'],0)
        for path in ['/api/device-evidence?limit=9','/api/device-evidence?limit=no','/api/device-evidence?bad=1']:
            with self.assertRaises(ClientError):bob.request(path)
        with self.assertRaises(ClientError):bob.request('/api/device-evidence',{'mode':'require'})
        with self.assertRaises(ClientError):bob.request('/api/jobs/'+source['id'])
        self.assertEqual(self.jobs.device_evidence.public(replace(self.jobs.access.user('bob'),experiment_id='experiment-synthetic'))['total'],0)
    def test_user_cannot_forge_a_private_before_start_reference(self):
        self.source();self.policy();self.bob_access();job=self.consumer()
        request={'template':job['template'],'environment':'worker-python','parameters':job['parameters'],
                 '_selfcheck_before':{'status':'recent_microchecks','source_job_id':self.source_job['id']}}
        with self.assertRaises(JobError):self.jobs.submit(request,self.jobs.access.user('bob'))
    def test_feasibility_preview_reads_guard_without_creating_an_attempt(self):
        self.source(False);self.policy();self.bob_access();job=self.consumer()
        request={'template':job['template'],'environment':'worker-python','parameters':job['parameters']}
        before=len(self.jobs.all());preview=self.jobs.preview(request,self.jobs.access.user('bob'))
        self.assertEqual(preview['status'],'blocked');self.assertIn('自检',preview['candidates'][0]['reason'])
        self.assertEqual(len(self.jobs.all()),before);self.assertFalse(self.jobs.attempts(job['id']))
    def test_old_aging_hold_on_another_entry_cannot_deadlock_its_selfcheck_producer(self):
        from queue_policy import note_wait,protected
        with self.jobs.transaction() as db:
            source=self.jobs.nodes.node(self.node,db);profile=source['profile'];bob=profile['users'].pop('bob')
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.node))
        peer_profile=copy.deepcopy(profile);peer_profile.update(name='bob-only-entry',users={'bob':bob})
        peer,_=self.jobs.nodes.enroll(peer_profile);self.daily.fixture.node=peer;self.daily.beat();self.bob_access()
        self.daily.fixture.node=self.node;self.source(False);job=self.consumer()
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            job=self.jobs.get(job['id'],db);job['created_at']=dt.datetime.fromtimestamp(self.daily.epoch-600,dt.timezone.utc).isoformat()
            node=self.jobs.nodes.node(peer,db);domain=self.jobs.resources.get(node['profile']['physical_id'],db)
            idle=self.jobs.nodes.placement(job,node,domain,[],db,identities)
            self.assertIsNone(idle['reason'])
            note_wait(self.jobs,job,peer,domain,node['profile'].get('policy_revision',1),db,True);self.jobs.write(db,job)
            self.assertEqual(protected(self.jobs,self.jobs.all(db),domain['physical_id'],db,identities)['id'],job['id'])
        self.policy(targets=[peer])
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            self.assertIsNone(protected(self.jobs,self.jobs.all(db),domain['physical_id'],db,identities))
        source=self.source();self.assertEqual(source['state'],'succeeded')
        self.assertEqual(self.jobs.nodes.claim(peer)['grant']['job_id'],job['id'])

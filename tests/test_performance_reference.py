"""Fixed reference lifecycle and queue integration; device captures are synthetic."""
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import test_device_evidence as cases
import test_npu_selfcheck as micro
import performance_reference as reference
from deployment import CORE,DeployError
from jobs import JobError
from maintenance import require_features
from lab_client import Client,ClientError
from server import ThreadingHTTPServer,handler_class


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.f=cases.EvidenceTests('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.daily=self.f.daily;self.jobs=self.f.jobs;self.node=self.f.node;self.refs=self.jobs.performance_references
        self.clock=patch('performance_reference.now',side_effect=self.f.iso);self.clock.start();self.addCleanup(self.clock.stop)
        original_beat=self.daily.fixture.beat
        def beat(**body):
            import npu_measurement
            body['capabilities']+=[npu_measurement.CAPABILITY];body['measurement_source_sha256']=npu_measurement.source_sha()
            return original_beat(**body)
        p=patch.object(self.daily.fixture,'beat',side_effect=beat);p.start();self.addCleanup(p.stop)
        complete=self.jobs.nodes.complete
        def with_window(node,body):
            job=self.jobs.get(body['job_id'])
            if job.get('template')=='accuracy_v1' and job.get('_device_schedule'):
                import npu_measurement
                before=self.daily.epoch;self.daily.epoch+=1;self.daily.beat()
                iso=lambda t:dt.datetime.fromtimestamp(t,dt.timezone.utc).isoformat()
                def reading(t,idle=False):
                    return {'status':'sampled','sampled_at':iso(t),
                        'devices':[{'id':0,'health':'OK','utilization_pct':0 if idle else 70,'process_count':0 if idle else 1,
                          'frequency':{'schema':'ascend-lab-npu-frequency/v1','status':'sampled','source':'npu-smi info -t common',
                            'device_id':0,'sampled_at':iso(t),'current_mhz':1650,'rated_mhz':1650}}]}
                binding={'job_id':job['id'],'attempt_id':job['attempt_id'],'node_id':job['node_id'],'physical_id':job['_physical_id'],
                    'environment_sha256':job['environment_sha256'],'npu_policy_sha256':job['_npu_policy_sha256'],'device_ids':[0]}
                doc={'schema':npu_measurement.SCHEMA,'source_sha256':npu_measurement.source_sha(),'binding':binding,'queries':2,'restarts':0,
                    'windows':[{'id':'mm512_fp16','state':'closed','started_at':iso(before+.2),'ended_at':iso(before+.8),
                        'started_monotonic':100.,'ended_monotonic':100.6,'before':reading(before),'after':reading(before+.9)}]}
                release={**reading(before+1,True),'policy_sha256':job['_npu_policy_sha256']};release.pop('status')
                body={**body,'result':{**body['result'],'measurement_evidence':doc,'execution_finished_at':self.f.iso(),'npu_release_observation':release}}
            elif job.get('_measurement_contract'):
                import npu_measurement
                binding={'job_id':job['id'],'attempt_id':job['attempt_id'],'node_id':job['node_id'],'physical_id':job['_physical_id'],
                    'environment_sha256':job.get('environment_sha256'),'npu_policy_sha256':job.get('_npu_policy_sha256'),'device_ids':job['allocation']['npu_ids']}
                body={**body,'result':{**body['result'],'measurement_evidence':{'schema':npu_measurement.SCHEMA,
                    'source_sha256':npu_measurement.source_sha(),'binding':binding,'queries':0,'restarts':0,'windows':[]}}}
            return complete(node,body)
        p=patch.object(self.jobs.nodes,'complete',side_effect=with_window);p.start();self.addCleanup(p.stop)
        self.daily.beat();self.sources=[]

    def sample(self,value=25,rounds=None,precision=True):
        if self.sources:self.daily.epoch+=86400;self.daily.beat()
        original=micro.report
        def report():
            body=original();body['timing']['rounds_us']=[[v]*10 for v in (rounds or [value]*3)]
            body['timing']['measurement_window']={'id':'mm512_fp16','begin':'open','end':'closed'};return body
        with patch.object(micro,'report',side_effect=report):job=self.f.source(precision=precision)
        self.sources.append(job['id']);return job
    def collect(self,values=(25,25,25)):
        for value in values:self.sample(value)
    def create(self,approve=True,**change):
        spec={'name':'known-reference','source_job_ids':self.sources[:3],'ratio_bounds':[.8,1.2],
              'max_round_ratio':1.1,'max_age_seconds':604800,**change}
        value=self.refs.create(spec);self.ref_id=value['id']
        return self.refs.control(value['id'],'approve') if approve else value
    def policy(self,mode='require',**change):
        spec={'name':'with-reference','source_plan_id':self.daily.identifier,'target_node_ids':[self.node],
              'projects':['personal:bob'],'max_age_seconds':86400,'mode':mode,
              'performance_references':{'0':self.ref_id},**change}
        value=self.jobs.device_evidence.create(spec);self.policy_id=value['id']
        return self.jobs.device_evidence.control(value['id'],'enable')
    def compare(self):
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            check=self.jobs.device_evidence.latest(self.daily.identifier,0,db)
            value=self.refs.get(self.ref_id,db)
            return self.refs.compare({'id':self.ref_id,'approval_sha256':reference.approval_sha(value)},check,db,identities)
    def snapshot(self,name):
        target=self.daily.root/name;target.mkdir()
        with self.jobs.connect() as source,sqlite3.connect(target/'jobs.sqlite3') as dest:source.backup(dest)
        return target

    def test_default_empty_and_daily_fixed_sample_has_original_receipt_hash(self):
        self.assertEqual(self.refs.public(self.jobs.access.local)['total'],0)
        job=self.sample()
        with self.jobs.connect() as db:check=self.jobs.device_evidence.latest(self.daily.identifier,0,db)
        self.assertEqual(check['performance_sample']['status'],'captured')
        self.assertEqual(check['performance_sample']['sample']['report_sha256'],job['_fixed_microcheck_report_sha256'])
    def test_draft_approval_center_expiry_and_event_are_explicit(self):
        self.collect((25,26,25));draft=self.create(False)
        self.assertEqual(draft['state'],'draft');self.assertIsNone(draft['approval']);self.assertEqual(draft['center_us'],25)
        self.assertEqual(draft['expires_at'],min(reference.epoch(s['execution_finished_at']) for s in draft['sources'])+604800)
        approved=self.refs.control(draft['id'],'approve');self.assertEqual(approved['state'],'active')
        with self.jobs.connect() as db:
            events=[json.loads(raw) for raw, in db.execute('SELECT record FROM performance_reference_events')]
        self.assertEqual(events[-1]['approval_sha256'],reference.approval_sha(approved))
    def test_sources_cannot_qualify_themselves_then_independent_fresh_sample_matches(self):
        self.collect();self.create();self.assertEqual(self.compare()['reason'],'independent_post_approval_sample_required')
        self.sample(26);compared=self.compare();self.assertEqual(compared['status'],'within_reference_envelope')
        self.assertFalse(compared['performance_qualified']);self.assertEqual(compared['round_ratios'],[1.04]*3)
    def test_slow_and_abnormally_fast_samples_both_depart_without_rewriting_precision(self):
        self.collect();self.create()
        for value in [40,10]:
            job=self.sample(value);self.assertTrue(job['workflow_result']['passed'])
            self.assertEqual(self.compare()['status'],'outside_reference_envelope')
    def test_unstable_candidate_is_not_hidden_by_average(self):
        self.collect();self.create();self.sample(rounds=[20,25,30])
        self.assertEqual(self.compare()['status'],'unstable_samples')
    def test_reference_rejects_unstable_source_rounds_and_independent_runs(self):
        self.sample(rounds=[20,25,30]);self.sample();self.sample()
        with self.assertRaises(JobError):self.create()
    def test_independent_run_drift_cannot_be_hidden_in_the_reference_center(self):
        self.collect((20,25,25))
        with self.assertRaises(JobError):self.create()
    def test_impossible_event_durations_are_rejected_without_creating_an_approval(self):
        self.collect((1e308,1e308,1e308))
        with self.assertRaises(JobError):self.create(ratio_bounds=[.5,4])
    def test_reenabled_owner_does_not_restore_old_reference_authority(self):
        self.collect();self.create();self.sample();self.assertEqual(self.compare()['status'],'within_reference_envelope')
        self.jobs.access.disable_user('alice');self.jobs.access.disable_user('alice',False)
        self.assertNotEqual(self.compare()['status'],'within_reference_envelope')
    def test_approval_clock_rollback_and_duration_margin_are_unconfirmed(self):
        self.collect();value=self.create();self.sample()
        with self.jobs.nodes.handoff.transaction() as (db,identities):
            check=self.jobs.device_evidence.latest(self.daily.identifier,0,db);anchor={'id':value['id'],'approval_sha256':reference.approval_sha(value)}
            early=self.refs.compare(anchor,check,db,identities,at=reference.epoch(value['approved_at'])-1)
            short=self.refs.compare(anchor,check,db,identities,at=value['expires_at']-30,coverage=60)
        self.assertEqual(early['reason'],'invalid_approval_time');self.assertEqual(short['reason'],'insufficient_validity')
    def test_annotation_expiring_during_execution_preserves_original_report(self):
        self.collect();value=self.create();self.daily.epoch+=3*86400;self.daily.beat();self.sample();self.policy('annotate')
        self.daily.epoch=value['expires_at']-120;self.daily.beat();self.f.bob_access();job=self.f.consumer(timeout=180)
        grant,auth=self.f.grant(job);self.f.advance(120);done=self.f.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['device_conditions']['selfcheck']['status'],'latest_check_unconfirmed')
        self.assertEqual(done['device_conditions']['selfcheck']['before']['performance_reference']['status'],'within_reference_envelope')
    def test_invalid_definitions_cannot_create_approval(self):
        for extra in [{'source_job_ids':['job-'+'1'*12]*3},{'source_job_ids':['job-'+'1'*12]*2},
                      {'ratio_bounds':[1,2]},{'ratio_bounds':[.5,float('inf')]},{'max_round_ratio':True},{'max_age_seconds':True}]:
            with self.subTest(extra=extra),self.assertRaises(JobError):self.create(False,**extra)
    def test_foreign_unavailable_and_cpu_source_cannot_be_approved(self):
        with self.assertRaises(JobError):self.create(False,source_job_ids=['job-'+'1'*12,'job-'+'2'*12,'job-'+'3'*12])
        self.collect();self.jobs.access.disable_user('alice')
        with self.assertRaises(JobError):self.create()
    def test_modified_archived_report_cannot_be_used_to_cherry_pick_a_baseline(self):
        self.collect();job=self.jobs.get(self.sources[0]);path=self.jobs.directory(job['id'])/'results/device-microcheck.json'
        body=json.loads(path.read_text());body['timing']['rounds_us']=[[24]*10 for _ in range(3)];path.write_text(json.dumps(body))
        with self.assertRaises(JobError):self.create()
    def test_source_change_between_draft_and_approve_is_rejected(self):
        self.collect();draft=self.create(False);self.jobs.access.disable_user('alice');self.jobs.access.disable_user('alice',False)
        with self.assertRaises(JobError):self.refs.control(draft['id'],'approve')
    def test_expiry_uses_execution_not_approval_time_and_cannot_be_renewed(self):
        self.collect();draft=self.create(False);self.daily.epoch=draft['expires_at']+1;self.daily.beat()
        with self.assertRaises(JobError):self.refs.control(draft['id'],'approve')
    def test_policy_requires_an_independent_check_then_original_business_can_run(self):
        self.collect();self.create();self.policy();self.f.bob_access();job=self.f.consumer()
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        fresh=self.sample(25);self.f.bob_access();grant,auth=self.f.grant(job);done=self.f.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['device_conditions']['daily_ut'],'linked_microchecks')
        before=done['device_conditions']['selfcheck']['before'];self.assertEqual(before['source_job_id'],fresh['id'])
        self.assertEqual(before['performance_reference']['status'],'within_reference_envelope')
    def test_departure_queues_required_business_but_annotation_preserves_original_result(self):
        self.collect();self.create();self.sample(40);self.policy();self.f.bob_access();job=self.f.consumer()
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.jobs.device_evidence.control(self.policy_id,'retire');self.policy('annotate',name='annotation')
        grant,auth=self.f.grant(job);done=self.f.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['device_conditions']['daily_ut'],'unconfirmed_before_start')
    def test_pause_and_reapproval_do_not_silently_reapprove_a_bound_rule(self):
        self.collect();self.create();self.sample();self.policy();self.f.bob_access();job=self.f.consumer()
        self.refs.control(self.ref_id,'pause');self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.refs.control(self.ref_id,'approve');self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.assertEqual(self.f.decide(job)['performance_reference']['reason'],'approval_changed')
    def test_every_plan_card_needs_its_own_explicit_reference(self):
        self.collect();self.create()
        with self.assertRaises(JobError):self.policy(performance_references={'1':self.ref_id})
    def test_source_archives_remain_pinned_after_retire_while_rule_is_bound(self):
        self.collect();self.create();self.policy();self.refs.control(self.ref_id,'retire')
        owner=self.jobs.access.user('alice');request={'kind':'archive','id':self.sources[0],'action':'trash'}
        with self.assertRaises(JobError):self.jobs.lifecycle.plan(owner,request)
        self.jobs.device_evidence.control(self.policy_id,'retire')
        self.assertIn('plan',self.jobs.lifecycle.plan(owner,request))
    def test_restore_freezes_reference_and_old_snapshot_or_event_loss_is_rejected(self):
        self.collect();old=self.snapshot('before');self.create()
        with self.assertRaises(DeployError):reference.require_restore_history(self.daily.root,old,'controller')
        new=self.snapshot('event-loss')
        with sqlite3.connect(new/'jobs.sqlite3') as db:db.execute('DELETE FROM performance_reference_events')
        with self.assertRaises(DeployError):reference.require_restore_history(self.daily.root,new,'controller')
        reference.invalidate_restored(self.jobs.db_path)
        with self.assertRaises(JobError):self.refs.control(self.ref_id,'approve')
    def test_missing_support_code_cannot_activate_retained_references(self):
        self.collect();self.create()
        with self.assertRaises(DeployError):require_features(self.daily.root,'controller',{'files':{n:{} for n in CORE if n!='performance_reference.py'}})
    def test_readonly_project_scope_pagination_and_no_http_authority(self):
        from dataclasses import replace
        alice=self.jobs.access.issue_token('alice',{'jobs:read'},lifetime_seconds=604800);bob=self.jobs.access.issue_token('bob',{'jobs:read'},lifetime_seconds=604800)
        self.collect();self.create()
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),{'username':'operator','password':'offline-only'},self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        url='http://127.0.0.1:'+str(server.server_port);a=Client({'controller':url,'token':alice});b=Client({'controller':url,'token':bob})
        self.assertEqual(a.request('/api/performance-references')['total'],1);self.assertEqual(b.request('/api/performance-references')['total'],0)
        self.assertEqual(self.refs.public(replace(self.jobs.access.user('alice'),experiment_id='budget-bound'))['total'],0)
        for path in ['/api/performance-references?limit=9','/api/performance-references?x=1']:
            with self.assertRaises(ClientError):a.request(path)
        with self.assertRaises(ClientError):a.request('/api/performance-references',{'approve':self.ref_id})


if __name__=='__main__':unittest.main()

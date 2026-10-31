"""Adverse history, bounded long runs, archive authority, restart and owner ACL.

Device observations in this file are synthetic; no hardware is opened.
"""
import copy
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import npu_conditions as conditions
import test_npu_device as fixture
from jobs import ROOT,JobError,JobStore,atomic_json,now,public
from archive_api import manifest
from worker import Worker


class ConditionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name)
        self.base=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=2)
        self.job={'id':'job-unit','attempt_id':'attempt-unit','node_id':'worker-unit',
            '_physical_id':'physical-unit','resources':{'npu':1},'allocation':{'npu_ids':[0]},
            '_npu_policy_sha256':'a'*64,'_start_authorized':True,'reserved_at':self.iso(0)}
        self.store=Mock();self.store.directory.return_value=self.root
    def tearDown(self):self.temp.cleanup()
    def iso(self,t):return (self.base+dt.timedelta(seconds=t)).isoformat()
    def reading(self,t=0,health='OK',**extra):
        return {'status':'ok','sampled_at':self.iso(t),'policy_sha256':'a'*64,
                'devices':[{'id':0,'health':health,'process_count':0,'utilization_pct':0,**extra}]}
    def finish(self,pre=None,post=None):
        return conditions.finalize(self.store,self.job,{'npu_admission_observation':pre or self.reading(),
            'npu_release_observation':post or self.reading(10),'npu_release_basis':'clear-device-observation'})
    def test_busy_running_task_is_not_inferred_to_be_external_interference(self):
        conditions.capture(self.job,self.reading(5,process_count=3,utilization_pct=95,temperature_c=63,power_w=145))
        row=self.finish();self.assertEqual(row['status'],'sampled_no_alerts')
        self.assertEqual(row['numerical_qualification'],'unverified');self.assertEqual(row['performance_qualification'],'unverified')
        self.assertEqual(row['daily_ut'],'not_linked');self.assertIsNone(row['frequency_mhz'])
        self.assertEqual(row['sha256'],hashlib.sha256((self.root/conditions.FILE).read_bytes()).hexdigest())
    def test_dropped_warning_is_retained_without_inventing_a_retention_gap(self):
        for t in range(101):conditions.capture(self.job,self.reading(t*10,'Warning' if t==1 else 'OK'))
        row=self.finish(post=self.reading(1000));trace=self.job['_npu_conditions']
        self.assertEqual(len(trace['samples']),64);self.assertEqual(trace['sample_count'],101)
        self.assertEqual(trace['dropped_samples'],37);self.assertEqual(row['alert_sample_count'],1)
        self.assertEqual(row['status'],'adverse_observed');self.assertEqual(row['max_gap_seconds'],10)
        self.assertNotIn('sampling_gap',row['reasons'])
    def test_large_gap_remains_after_bad_interval_was_dropped(self):
        conditions.capture(self.job,self.reading(0))
        for t in range(100,900,10):conditions.capture(self.job,self.reading(t))
        row=self.finish(post=self.reading(900));self.assertIn('sampling_gap',row['reasons'])
        self.assertEqual(row['max_gap_seconds'],100);self.assertEqual(row['status'],'incomplete')
    def test_cached_samples_deduplicate_but_conflicting_alert_is_not_lost(self):
        conditions.capture(self.job,self.reading());conditions.capture(self.job,self.reading())
        self.assertEqual(self.job['_npu_conditions']['sample_count'],1)
        conditions.capture(self.job,self.reading(0,'Alarm'))
        row=self.finish();self.assertEqual(row['status'],'adverse_observed')
        self.assertIn('conflicting_sample_count',row['reasons'])
    def test_query_error_unknown_and_backwards_time_make_evidence_incomplete(self):
        conditions.capture(self.job,self.reading(5,'UNKNOWN'))
        conditions.capture(self.job,{'status':'error'})
        conditions.capture(self.job,self.reading(4))
        row=self.finish();self.assertEqual(row['status'],'incomplete')
        for reason in ['management_error_count','unknown_health_count','out_of_order_count']:self.assertIn(reason,row['reasons'])
    def test_out_of_order_alert_is_retained_as_adverse(self):
        conditions.capture(self.job,self.reading(5))
        conditions.capture(self.job,self.reading(4,'Alarm'))
        row=self.finish();self.assertEqual(row['status'],'adverse_observed')
        self.assertIn('out_of_order_count',row['reasons'])
    def test_wrong_contract_card_time_and_missing_boundary_never_pass(self):
        for bad in [None,{},self.reading(-100),{**self.reading(),'policy_sha256':'b'*64},
                    {**self.reading(),'devices':[]},{**self.reading(),'sampled_at':'2026-01-01T00:00:00'}]:
            result={'npu_admission_observation':bad,'npu_release_observation':self.reading(10)}
            row=conditions.finalize(self.store,self.job,result)
            self.assertEqual(row['status'],'incomplete');self.assertIn('admission_missing_or_invalid',row['reasons'])
    def test_boundary_order_and_idle_are_distinct_from_running_occupancy(self):
        row=self.finish(pre=self.reading(20,process_count=1),post=self.reading(10))
        self.assertEqual(row['status'],'incomplete')
        self.assertIn('admission_not_idle',row['reasons']);self.assertIn('boundary_order_invalid',row['reasons'])
    def test_private_process_fields_and_other_cards_never_enter_evidence(self):
        record=self.reading(5,pid=123,command='private',frequency_mhz={'bad':'sensor'},temperature_c=float('nan'))
        record['devices'].append({'id':99,'command':'other-private'})
        conditions.capture(self.job,record);self.finish()
        text=(self.root/conditions.FILE).read_text()
        self.assertNotIn('private',text);self.assertNotIn('NaN',text);self.assertNotIn('sensor',text)
        self.assertIsNone(json.loads(text)['telemetry']['samples'][0]['devices'][0]['temperature_c'])
    def test_no_start_and_cpu_do_not_claim_device_qualification(self):
        self.job['_start_authorized']=False;row=self.finish();self.assertEqual(row['status'],'incomplete')
        self.job['resources']['npu']=0;row=self.finish();self.assertEqual(row['status'],'not_applicable')


class ConditionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.NpuDeviceTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown);self.jobs=self.fixture.jobs;self.root=self.fixture.root
        self.node=self.fixture.nodes[0]
    def run_case(self,health='Warning',pre=True):
        job=self.fixture.submit();grant=self.jobs.nodes.claim(self.node)['grant']
        auth={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
        self.jobs.nodes.authorize_start(self.node,auth)
        self.jobs.nodes.update(self.node,{**auth,'running':True})
        reading=self.fixture.reading();reading['devices'][0].update(health=health,process_count=2,utilization_pct=90)
        self.fixture.beat(self.node,reading)
        record=self.fixture.reading();record.pop('status')
        result={'status':'failed','exit_code':1,'termination_confirmed':True,'execution_cleanup_confirmed':True,
            'npu_release_confirmed':True,'npu_release_basis':'clear-device-observation','npu_release_observation':record,
            'reason':'Synthetic protocol only; no NPU workload was executed'}
        if pre:result['npu_admission_observation']=record
        self.jobs.nodes.complete(self.node,{**auth,'result':result,'artifacts':[]})
        return self.jobs.get(job['id']),auth,result
    def test_warning_is_archived_and_does_not_rewrite_workload_failure_or_attempt(self):
        job,auth,result=self.run_case();row=job['device_conditions']
        self.assertEqual(row['status'],'adverse_observed');self.assertEqual(job['state'],'failed')
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)
        archive=manifest(self.jobs.directory(job['id']),job)
        evidence=next(item for item in archive['files'] if item['name']==conditions.FILE)
        self.assertEqual(evidence['sha256'],row['sha256'])
        self.assertEqual(job['_artifact_receipt']['manifest_sha256'],__import__('node_storage').manifest_sha256([]))
        self.assertNotIn('_npu_conditions',public(job));self.assertNotIn('npu_admission_observation',job['result'])
        document=json.loads((self.jobs.directory(job['id'])/conditions.FILE).read_text())
        self.assertEqual(document['physical_id'],job['_physical_id'])
        before=(self.jobs.directory(job['id'])/conditions.FILE).read_bytes()
        self.jobs.nodes.complete(self.node,{**auth,'result':result,'artifacts':[]})
        self.assertEqual(before,(self.jobs.directory(job['id'])/conditions.FILE).read_bytes())
    def test_old_node_missing_admission_is_incomplete_and_still_completes(self):
        job,_,_=self.run_case('OK',False)
        self.assertEqual(job['state'],'failed');self.assertEqual(job['device_conditions']['status'],'incomplete')
        self.assertIn('admission_missing_or_invalid',job['device_conditions']['reasons'])
    def test_node_cannot_upload_controller_evidence(self):
        job=self.fixture.submit();grant=self.jobs.nodes.claim(self.node)['grant']
        with self.assertRaises(JobError):self.jobs.nodes.upload(self.node,{**{k:grant[k] for k in ['job_id','attempt_id','lease_token']},
            'name':conditions.FILE,'offset':0,'data':'e30='})
        self.assertFalse((self.jobs.directory(job['id'])/conditions.FILE).exists())
    def test_controller_reopen_during_attempt_retains_alert_and_same_lease(self):
        job=self.fixture.submit();grant=self.jobs.nodes.claim(self.node)['grant']
        auth={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
        self.jobs.nodes.authorize_start(self.node,auth)
        reading=self.fixture.reading();reading['devices'][0]['health']='Alarm';self.fixture.beat(self.node,reading)
        self.jobs.access.close()
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        self.addCleanup(self.jobs.access.close);self.fixture.jobs=self.jobs
        self.fixture.beat(self.node)
        recovered=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(recovered,grant)
        record=self.fixture.reading();record.pop('status')
        result={'status':'failed','exit_code':1,'termination_confirmed':True,'execution_cleanup_confirmed':True,
                'npu_release_confirmed':True,'npu_release_basis':'clear-device-observation',
                'npu_admission_observation':record,'npu_release_observation':record}
        self.jobs.nodes.complete(self.node,{**auth,'result':result,'artifacts':[]})
        final=self.jobs.get(job['id']);self.assertEqual(final['device_conditions']['status'],'adverse_observed')
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)
    def test_restart_keeps_observed_alert_and_owner_scope_in_result_csv(self):
        job,_,_=self.run_case()
        row={'job_id':job['id'],'owner':job['owner'],'project':job['project'],'kind':'accuracy','name':'Synthetic condition fixture',
            'state':'failed','passed':False,'device_conditions':job['device_conditions']}
        with self.jobs.connect() as db:db.execute('INSERT INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row)))
        self.jobs.access.close()
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        self.addCleanup(self.jobs.access.close)
        self.assertEqual(self.jobs.get(job['id'])['device_conditions']['status'],'adverse_observed')
        self.assertEqual(self.jobs.results.rows(self.jobs.access.user('bob'))['total'],0)
        data=self.jobs.results.csv(self.jobs.access.user('alice')).decode('utf-8-sig')
        rows=list(csv.DictReader(io.StringIO(data)))
        self.assertEqual(json.loads(rows[0]['device_conditions'])['status'],'adverse_observed')
    def test_legacy_rows_get_unverified_without_being_rewritten_or_promoted(self):
        job,_,_=self.run_case('OK')
        legacy={'job_id':job['id'],'kind':'performance','passed':True,'metrics':{'mean_us':5}}
        with self.jobs.connect() as db:db.execute('INSERT INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(legacy)))
        row=self.jobs.results.rows(self.jobs.access.user('alice'))['rows'][0]
        self.assertEqual(row['device_conditions']['status'],'unverified');self.assertTrue(row['passed'])
        with self.jobs.connect() as db:self.assertEqual(json.loads(db.execute('SELECT record FROM workflow_results').fetchone()[0]),legacy)
    def test_worker_reads_private_receipt_but_uncleared_device_still_blocks_cleanup(self):
        worker=object.__new__(Worker);worker.upload=Mock()
        engine=Mock();engine.control=self.root/'control';engine.control.mkdir()
        engine.grant={'resources':{'npu':1}};engine.state={'started_epoch':1};engine.config={'npu':self.fixture.npu}
        receipt=self.fixture.reading();atomic_json(engine.control/'npu-admission.json',receipt)
        result={'status':'failed'}
        with patch('npu_device.check',side_effect=RuntimeError('uncleared device')):
            with self.assertRaises(RuntimeError):worker.finish_attempt(engine,result)
        self.assertEqual(result['npu_admission_observation'],receipt)
        engine.cleanup.assert_not_called();worker.upload.assert_not_called()

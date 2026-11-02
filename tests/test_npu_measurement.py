"""Real local sockets/clock journals; device readings are synthetic, no kernel."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import npu_measurement as measurement
import npu_conditions
from jobs import atomic_json


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-measure-');self.addCleanup(self.temp.cleanup)
        self.control=Path(self.temp.name);self.calls=0
        self.grant={'job_id':'job-measure','attempt_id':'attempt-'+'1'*32,'node_id':'worker-'+'1'*12,
            'physical_id':'physical-test','environment_sha256':'d'*64,'npu_policy_sha256':'a'*64,
            'measurement_contract':measurement.contract(),'allocation':{'npu_ids':[0]},'timeout_seconds':60}
        self.job={**self.grant,'id':self.grant['job_id'],'_physical_id':self.grant['physical_id'],
            '_npu_policy_sha256':self.grant['npu_policy_sha256'],'_measurement_contract':self.grant['measurement_contract'],
            'resources':{'npu':1},'_start_authorized':True,
            'reserved_at':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1)).isoformat()}
    def reading(self,health='OK'):
        self.calls+=1;instant=measurement.now()
        return {'status':'ok','policy_sha256':self.grant.get('npu_policy_sha256','a'*64),'sampled_at':instant,
            'devices':[{'id':0,'health':health,'utilization_pct':70,'process_count':1,'pid':123,'command':'private',
                'frequency':{'schema':'ascend-lab-npu-frequency/v1','status':'sampled','source':'npu-smi info -t common',
                    'sampled_at':instant,'device_id':0,'current_mhz':1650,'rated_mhz':1650}},
                       {'id':99,'command':'other-private'}]}
    def broker(self,probe=None):
        broker=measurement.Broker(self.control,self.grant,os.getuid(),probe or self.reading).open()
        self.addCleanup(broker.close);return broker
    def configuration(self,broker):
        binding=broker.public();binding['socket']='/proc/self/fd/'+str(broker.fd)+'/'+measurement.SOCKET
        return {'job_id':self.grant['job_id'],'measurement':binding}
    def call(self,broker,op='begin',identifier='bench'):
        return measurement.request(op,identifier,self.configuration(broker))
    def closed(self,broker):
        self.assertTrue(self.call(broker)['ok']);self.assertTrue(self.call(broker,'end')['ok']);return broker.document()
    def test_real_socket_idempotency_and_no_unallocated_process_data(self):
        broker=self.broker();first=self.call(broker);self.assertEqual(self.call(broker),first)
        last=self.call(broker,'end');self.assertEqual(self.call(broker,'end'),last)
        self.assertEqual(self.calls,2);document=broker.document()
        self.assertNotIn('token',document);self.assertNotIn('private',json.dumps(document));self.assertNotIn('99',json.dumps(document['windows'][0]['before']['devices']))
        row=measurement.normalize(self.job,document);self.assertEqual(row['status'],'recorded');self.assertFalse(row['performance_qualified'])
        self.assertEqual(row['windows'][0]['frequency_samples_in_window'],0)
    def test_peer_uid_token_job_and_attempt_are_all_bound(self):
        broker=self.broker();value={'operation':'begin','window_id':'bench','token':broker.state['token'],
            'job_id':self.grant['job_id'],'attempt_id':self.grant['attempt_id']}
        for field,bad in [('token','foreign'),('job_id','foreign'),('attempt_id','foreign'),('query','reset')]:
            with self.subTest(field=field),self.assertRaises(ValueError):broker.handle({**value,field:bad},os.getuid())
        with self.assertRaises(ValueError):broker.handle(value,os.getuid()+1)
        self.assertEqual(self.calls,0);self.assertEqual(broker.state['windows'],[])
    def test_client_checks_server_uid(self):
        broker=self.broker();config=self.configuration(broker);config['measurement']['node_uid']=os.getuid()+1
        with self.assertRaises(ValueError):measurement.request('begin','bench',config)
        self.assertEqual(self.calls,0)
    def test_overlap_missing_begin_and_four_window_budget(self):
        broker=self.broker();self.assertFalse(self.call(broker,'end')['ok']);self.call(broker)
        self.assertFalse(self.call(broker,'begin','overlap')['ok']);self.call(broker,'end')
        for i in range(3):self.call(broker,'begin','bench'+str(i));self.call(broker,'end','bench'+str(i))
        self.assertFalse(self.call(broker,'begin','overflow')['ok']);self.assertEqual(self.calls,8)
    def test_query_failure_keeps_marker_but_not_confirmed_observations(self):
        broker=self.broker(Mock(side_effect=RuntimeError('must-not-leak')));document=self.closed(broker)
        row=measurement.normalize(self.job,document);self.assertEqual(row['status'],'unconfirmed')
        self.assertNotIn('must-not-leak',json.dumps(row));self.assertEqual(document['queries'],2)
    def test_restart_never_closes_or_requeries_an_interrupted_window(self):
        broker=self.broker();self.call(broker);broker.close();recovered=self.broker()
        self.assertFalse(self.call(recovered,'end')['ok']);self.assertFalse(self.call(recovered)['ok'])
        self.assertEqual(self.calls,1);self.assertEqual(recovered.state['queries'],1)
        self.assertEqual(measurement.normalize(self.job,recovered.document())['status'],'unconfirmed')
    def test_closed_reply_survives_restart_without_more_queries(self):
        broker=self.broker();self.closed(broker);last=self.call(broker,'end');broker.close();recovered=self.broker()
        self.assertEqual(self.call(recovered,'end'),last);self.assertEqual(self.calls,2)
    def test_old_close_cannot_rewind_a_recovered_journal(self):
        broker=self.broker();self.closed(broker);broker.close();recovered=self.broker()
        before=self.path_bytes();broker.close();self.assertEqual(self.path_bytes(),before)
        self.assertEqual(recovered.state['restarts'],1)
    def path_bytes(self):return (self.control/'measurement.json').read_bytes()
    def test_inflight_query_charge_survives_crash_intent(self):
        broker=self.broker();self.call(broker);broker.state['windows'][0]['state']='begin_pending';broker.save();broker.close()
        recovered=self.broker();self.assertEqual(recovered.state['queries'],1)
        self.assertFalse(self.call(recovered)['ok']);self.assertEqual(self.calls,1)
    def test_second_listener_and_foreign_endpoint_cannot_replace_first(self):
        broker=self.broker();identity=broker.identity
        with self.assertRaises(BlockingIOError):measurement.Broker(self.control,self.grant,os.getuid()).open()
        self.assertEqual([os.stat(broker.directory/measurement.SOCKET).st_dev,os.stat(broker.directory/measurement.SOCKET).st_ino],identity)
        self.assertTrue(self.call(broker)['ok'])
    def test_preexisting_endpoint_is_not_deleted_without_owned_intent(self):
        directory=self.control/'measurement-endpoint';directory.mkdir(mode=0o711)
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as foreign:
                foreign.bind('/proc/self/fd/'+str(fd)+'/'+measurement.SOCKET);inode=(directory/measurement.SOCKET).stat().st_ino
                with self.assertRaises(ValueError):measurement.Broker(self.control,self.grant,os.getuid()).open()
                self.assertEqual((directory/measurement.SOCKET).stat().st_ino,inode)
        finally:os.close(fd)
    def test_cpu_windows_never_query_or_acquire_a_device(self):
        self.grant['allocation']['npu_ids']=[];self.grant.pop('npu_policy_sha256');self.job.update(allocation={'npu_ids':[]},resources={'npu':0});self.job.pop('_npu_policy_sha256')
        broker=self.broker();row=measurement.normalize(self.job,self.closed(broker))
        self.assertEqual(row['windows'][0]['status'],'not_applicable');self.assertEqual(self.calls,0);self.assertEqual(broker.state['queries'],0)
    def test_missing_old_or_foreign_contract_never_creates_a_valid_window(self):
        self.assertEqual(measurement.normalize(self.job,None)['status'],'unconfirmed')
        broker=self.broker();value=self.closed(broker);value['binding']['attempt_id']='foreign'
        self.assertEqual(measurement.normalize(self.job,value)['status'],'unconfirmed')
        old=copy.deepcopy(self.job);old.pop('_measurement_contract');self.assertIsNone(measurement.normalize(old,value))
    def test_window_end_outside_execution_or_clock_jump_is_unconfirmed(self):
        broker=self.broker();value=self.closed(broker)
        early=self.job['reserved_at'];self.assertEqual(measurement.normalize(self.job,value,execution_finished_at=early)['status'],'unconfirmed')
        value['windows'][0]['ended_monotonic']+=10
        self.assertEqual(measurement.normalize(self.job,value)['status'],'unconfirmed')
    def test_workload_report_cannot_extend_scope_or_replace_sample_times(self):
        broker=self.broker();value=self.closed(broker)
        value['windows'][0]['after']['sampled_at']=self.job['reserved_at']
        self.assertEqual(measurement.normalize(self.job,value)['status'],'unconfirmed')
    def test_only_actual_inner_timestamps_count_and_trimmed_history_stays_incomplete(self):
        broker=self.broker();self.call(broker);reading=self.reading();self.call(broker,'end')
        from npu_conditions import sample
        inner=sample(reading,self.job);inner['devices'][0]['frequency']['sampled_at']=self.job['reserved_at']
        row=measurement.normalize(self.job,broker.document(),{'samples':[inner],'dropped_samples':1})['windows'][0]
        self.assertEqual(row['heartbeat_samples_in_window'],1);self.assertEqual(row['frequency_samples_in_window'],0)
        self.assertTrue(row['heartbeat_retention_incomplete'])
    def test_boundary_warning_changes_condition_column_without_changing_workload_status(self):
        broker=self.broker(lambda:self.reading('Warning'));document=self.closed(broker)
        store=Mock();store.directory.return_value=self.control
        clear=self.reading();clear['devices'][0].update(health='OK',process_count=0,utilization_pct=0)
        result={'status':'succeeded','exit_code':0,'measurement_evidence':document,'npu_admission_observation':clear,'npu_release_observation':clear}
        summary=npu_conditions.finalize(store,self.job,result)
        self.assertEqual(result['status'],'succeeded');self.assertEqual(summary['status'],'adverse_observed')
        self.assertNotIn('measurement_evidence',result);self.assertFalse(result['measurement_windows']['performance_qualified'])
        self.assertEqual(summary['measurement']['observed_alerts'],2)
    def test_authenticated_protocol_preserves_samples_archive_and_blocks_incompatible_code(self):
        import test_device_evidence as cases
        from deployment import CORE,DeployError
        from maintenance import require_features
        fixture=cases.EvidenceTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=fixture.daily.fixture.beat
        def capable(**body):
            body['capabilities']+=[measurement.CAPABILITY];body['measurement_source_sha256']=measurement.source_sha()
            return original(**body)
        with patch.object(fixture.daily.fixture,'beat',side_effect=capable),patch('npu_measurement.now',side_effect=fixture.iso):
            fixture.daily.beat();fixture.bob_access();job=fixture.consumer('performance');grant,auth=fixture.grant(job)
            self.grant=grant;self.job=fixture.jobs.get(job['id']);broker=self.broker();document=self.closed(broker)
            complete=fixture.jobs.nodes.complete
            def with_marker(node,body):return complete(node,{**body,'result':{**body['result'],'measurement_evidence':document}})
            with patch.object(fixture.jobs.nodes,'complete',side_effect=with_marker):done=fixture.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertEqual(done['workflow_result']['metrics']['mean_us'],30.5)
        self.assertEqual(done['device_conditions']['measurement']['status'],'recorded',json.dumps({'normalized':done['device_conditions']['measurement'],'document':document}))
        raw=json.loads((fixture.jobs.directory(done['id'])/'npu-conditions.json').read_text())
        self.assertEqual(raw['summary']['measurement'],done['device_conditions']['measurement'])
        self.assertNotIn('token',json.dumps(done['device_conditions']['measurement']))
        with self.assertRaises(DeployError):require_features(fixture.daily.root,'controller',{'files':{n:{} for n in CORE if n!='npu_measurement.py'}})
    def test_incompatible_optional_node_measurement_does_not_break_old_workflows(self):
        import test_device_evidence as cases
        fixture=cases.EvidenceTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=fixture.daily.fixture.beat
        def old_runtime(**body):
            body['capabilities']+=[measurement.CAPABILITY];body['measurement_source_sha256']='0'*64
            return original(**body)
        with patch.object(fixture.daily.fixture,'beat',side_effect=old_runtime):
            fixture.daily.beat();fixture.bob_access();job=fixture.consumer();grant,auth=fixture.grant(job)
            self.assertNotIn('measurement_contract',grant);done=fixture.complete(grant,auth)
        self.assertTrue(done['workflow_result']['passed']);self.assertNotIn('measurement',done['device_conditions'])


if __name__=='__main__':unittest.main()

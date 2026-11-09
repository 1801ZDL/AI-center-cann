"""Small fixed-file/SQLite checks for CPU/PID admission; no workload or device."""
import copy
from contextlib import nullcontext
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import resource_pressure as pressure
from jobs import JobError,Scheduler


def base(epoch=100):
    return {'schema':pressure.CAPABILITY,'sampled_epoch':epoch,'status':'ready','available_bytes':8*1024**3,
            'swap_free_bytes':0,'memory_full_avg10':0,'io_full_avg10':0,'ancestor_count':3}


def observation(epoch=100,*,cpu=0,pids=160,status='limited'):
    return {'schema':pressure.EXECUTION_CAPABILITY,'sampled_epoch':epoch,'ancestor_count':3,
            'cpu_some_avg10':cpu,'pids_status':status,'pids_available':pids}


class ExecutionPressureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-execution-pressure-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.proc=self.root/'proc';self.cg=self.root/'cgroup'
        (self.proc/'self').mkdir(parents=True);(self.proc/'pressure').mkdir()
        (self.proc/'self/cgroup').write_text('0::/user/task\n');(self.proc/'pressure/cpu').write_text('some avg10=0\nfull avg10=100\n')
        for relative in ['', 'user','user/task']:
            path=self.cg/relative;path.mkdir(parents=True,exist_ok=True)
            (path/'cpu.pressure').write_text('some avg10=0\nfull avg10=100\n')
            if relative:
                (path/'pids.max').write_text('max');(path/'pids.current').write_text('5')

    def sample(self):return pressure.sample_execution(self.proc,self.cg,epoch=100)

    def reason(self,value,**settings):
        return pressure.reason(base(),0,{'enabled':True,**settings},epoch=100,execution=value,requested_pids=128)

    def test_parent_pid_limit_beats_leaf_and_abundant_memory(self):
        (self.cg/'user/pids.max').write_text('200');(self.cg/'user/pids.current').write_text('180')
        (self.cg/'user/task/pids.max').write_text('256')
        value=self.sample();self.assertEqual((value['pids_status'],value['pids_available']),('limited',20))
        self.assertIn('PID余量不足',self.reason(value,reserve_pids=32))

    def test_maximum_cpu_some_across_host_and_ancestors(self):
        (self.proc/'pressure/cpu').write_text('some avg10=25\n')
        (self.cg/'user/cpu.pressure').write_text('some avg10=70.5\n')
        self.assertEqual(self.sample()['cpu_some_avg10'],70.5)
        self.assertIn('CPU等待压力过高',self.reason(self.sample(),cpu_some_avg10=70))

    def test_cpu_some_is_not_cpu_full_or_utilization(self):
        self.assertEqual(self.sample()['cpu_some_avg10'],0)
        self.assertIsNone(self.reason(self.sample(),cpu_some_avg10=1))

    def test_root_without_pid_files_and_all_max_is_not_claimed_finite_capacity(self):
        value=self.sample();self.assertEqual((value['pids_status'],value['pids_available']),('no_visible_limit',None))
        self.assertIn('上限或余量未知',self.reason(value,reserve_pids=0))

    def test_only_root_uses_host_psi_but_does_not_invent_pid_capacity(self):
        (self.proc/'self/cgroup').write_text('0::/\n');(self.cg/'cpu.pressure').unlink()
        value=self.sample();self.assertEqual(value['ancestor_count'],1);self.assertEqual(value['cpu_some_avg10'],0)
        self.assertEqual(value['pids_status'],'no_visible_limit');pressure.validate_execution(value)

    def test_missing_pid_controller_does_not_disable_cpu_observation(self):
        (self.cg/'user/pids.max').unlink();value=self.sample()
        self.assertEqual(value['pids_status'],'unknown');self.assertIsNotNone(self.reason(value,reserve_pids=1))
        self.assertIsNone(self.reason(value,cpu_some_avg10=90))

    def test_missing_cpu_controller_does_not_disable_pid_observation(self):
        (self.cg/'user/cpu.pressure').unlink();(self.cg/'user/pids.max').write_text('512')
        value=self.sample();self.assertIsNone(value['cpu_some_avg10']);self.assertEqual(value['pids_available'],507)
        self.assertIsNone(self.reason(value,reserve_pids=32));self.assertIn('CPU等待压力观测未知',self.reason(value,cpu_some_avg10=90))

    def test_partial_root_pid_interface_is_unknown(self):
        (self.cg/'pids.current').write_text('1');self.assertEqual(self.sample()['pids_status'],'unknown')

    def test_bad_pid_counter_never_becomes_unlimited(self):
        for value in ['-1','bad',str(2**63),'1.5']:
            (self.cg/'user/pids.current').write_text(value)
            with self.subTest(value=value):self.assertEqual(self.sample()['pids_status'],'unknown')

    def test_current_over_limit_clamps_headroom_to_zero(self):
        (self.cg/'user/pids.max').write_text('1');self.assertEqual(self.sample()['pids_available'],0)

    def test_limit_change_during_read_uses_tighter_limit(self):
        original=pressure.text;calls=0
        def read(path):
            nonlocal calls
            if path==self.cg/'user/pids.max':
                calls+=1;return '200' if calls==1 else '4'
            return original(path)
        with patch.object(pressure,'text',side_effect=read):self.assertEqual(self.sample()['pids_available'],0)

    def test_malformed_cgroup_or_too_many_ancestors_are_unknown(self):
        for line in ['0::relative','0::/user/../escape','0::/user\n0::/task','0::/'+('/'.join(['x']*32))]:
            (self.proc/'self/cgroup').write_text(line);value=self.sample()
            with self.subTest(line=line):
                self.assertEqual(value['ancestor_count'],0);self.assertEqual(value['pids_status'],'unknown');self.assertIsNone(value['cpu_some_avg10'])

    def test_oversized_or_nonfinite_cpu_data_is_unknown(self):
        for raw in ['x'*16385,'some avg10=nan','some avg10=inf','some avg10=101','full avg10=0']:
            (self.cg/'user/cpu.pressure').write_text(raw)
            with self.subTest(raw=raw[:30]):self.assertIsNone(self.sample()['cpu_some_avg10'])

    def test_default_policy_and_legacy_sample_format_are_unchanged(self):
        self.assertEqual(pressure.validate(base()),base())
        self.assertIsNone(pressure.reason(base(),0,{'enabled':True},epoch=100))
        self.assertIsNone(pressure.reason(None,-1,{'enabled':False,'reserve_pids':32},execution={'bad':True}))
        self.assertIsNone(pressure.policy()['reserve_pids']);self.assertIsNone(pressure.policy()['cpu_some_avg10'])

    def test_threshold_configuration_is_typed_and_bounded(self):
        for settings in [{'cpu_some_avg10':True},{'cpu_some_avg10':0},{'cpu_some_avg10':101},{'cpu_some_avg10':50.0},
                         {'reserve_pids':True},{'reserve_pids':-1},{'reserve_pids':65537},{'extra':1}]:
            with self.subTest(settings=settings),self.assertRaises(ValueError):pressure.policy(settings)
        pressure.policy({'cpu_some_avg10':100,'reserve_pids':0})

    def test_invalid_observation_shapes_and_metrics_are_rejected(self):
        for changes in [{'schema':'old'},{'sampled_epoch':float('nan')},{'sampled_epoch':True},{'sampled_epoch':10**400},
                        {'ancestor_count':False},{'ancestor_count':33},{'ancestor_count':0},
                        {'cpu_some_avg10':True},{'cpu_some_avg10':101},{'cpu_some_avg10':float('inf')},{'cpu_some_avg10':10**400},
                        {'pids_status':[]},{'pids_available':True},{'pids_available':-1},{'pids_available':2**63},
                        {'pids_status':'unknown','pids_available':0},{'extra':'private-path'}]:
            with self.subTest(changes=list(changes)),self.assertRaises(ValueError):pressure.validate_execution({**observation(),**changes})

    def test_missing_stale_future_and_malformed_reports_pause_new_admission(self):
        for value in [None,{},observation(epoch=79),observation(epoch=106),{'schema':'bad'}]:
            with self.subTest(value=value):self.assertIsNotNone(self.reason(value,reserve_pids=32))
        self.assertIsNone(self.reason(observation(epoch=95),reserve_pids=32))

    def test_managed_pid_envelope_and_reserve_are_both_counted(self):
        from resources import MANAGED_PIDS_LIMIT
        self.assertEqual(MANAGED_PIDS_LIMIT,128)
        self.assertIsNotNone(self.reason(observation(pids=159),reserve_pids=32))
        self.assertIsNone(self.reason(observation(pids=160),reserve_pids=32))

    def test_memory_request_cannot_bypass_guard_with_negative_or_boolean(self):
        for value in [-1,True,1.5,2**63]:
            with self.subTest(value=value):self.assertIsNotNone(pressure.reason(base(),value,{'enabled':True},epoch=100))

    def test_legacy_untrusted_metrics_raise_value_error_not_overflow_or_type_error(self):
        for changes in [{'status':[]},{'sampled_epoch':10**400},{'memory_full_avg10':10**400},{'io_full_avg10':True}]:
            with self.subTest(changes=list(changes)),self.assertRaises(ValueError):pressure.validate({**base(),**changes})

    def test_unneeded_metric_may_be_unknown_but_requested_metric_cannot(self):
        self.assertIsNone(self.reason(observation(cpu=None),reserve_pids=32))
        self.assertIsNone(self.reason(observation(pids=None,status='unknown'),cpu_some_avg10=90))
        self.assertIsNotNone(self.reason(observation(cpu=None),reserve_pids=32,cpu_some_avg10=90))

    def test_output_contains_no_cgroup_paths_or_process_identity(self):
        value=self.sample();self.assertNotIn(str(self.root),json.dumps(value));self.assertNotIn('pid',value)
        pressure.validate_execution(value)

    def test_local_placement_uses_cpu_pid_feedback_without_starting_executor(self):
        scheduler=object.__new__(Scheduler)
        scheduler.jobs=SimpleNamespace(dispatch_permission=lambda *_:None,inventory=SimpleNamespace(reason=lambda *_:None),
            backend=SimpleNamespace(physical_id='host'),policy={'local_enabled':True,'resource_pressure':{'enabled':True,'reserve_pids':32}})
        job={'id':'job-fixture','resources':{'cpu':1,'memory_mib':128,'npu':0,'shm_mib':0}}
        domain={'physical_id':'host','draining':False,'policy':{'cpu_ids':[0],'memory_mib':1024,'shm_mib':0,'max_running':1}}
        snapshot={'sampled_at':dt.datetime.now(dt.timezone.utc).isoformat(),'host':{'resource_pressure':base(),
            'execution_pressure':observation(pids=32),'memory_available_bytes':8*1024**3,'workspace_disk':{'free_bytes':8*1024**3}}}
        with patch('resource_pressure.time.time',return_value=100):
            self.assertIn('PID余量不足',scheduler.placement(job,[],snapshot,domain)['reason'])

    def test_notification_health_records_pressure_and_recovery_without_delivery(self):
        import notifications
        with sqlite3.connect(':memory:') as db:
            notifications.initialize(db);db.execute('CREATE TABLE worker_nodes(id TEXT,last_seen REAL,report TEXT,disabled INTEGER)')
            report={'resource_pressure':base(),'execution_pressure':observation(cpu=90)}
            db.execute('INSERT INTO worker_nodes VALUES(?,?,?,?)',('node',100,json.dumps(report),0))
            settings=notifications.policy({'enabled':True})
            notifications.health_once(db,'alice',settings,{'cpu_some_avg10':80},epoch=100)
            report['execution_pressure']=observation(cpu=0);db.execute('UPDATE worker_nodes SET report=?',(json.dumps(report),))
            notifications.health_once(db,'alice',settings,{'cpu_some_avg10':80},epoch=100)
            events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM notification_outbox')]
            self.assertEqual(len(events),2);self.assertEqual([e['detail'] for e in events],['pressure_or_observation_blocked','ready'])

    def test_worker_heartbeat_transports_both_observations_without_docker_or_network(self):
        import worker
        from test_environment_contract import IMAGE
        from io_policy import limits
        engine=object.__new__(worker.Worker)
        engine.config={'node_id':'node-fixture','image':IMAGE,'docker_runtime':{'fixture':True}}
        engine.root=self.root;engine.spool=None;engine.cache=None;engine.previous_cpu=None;engine.artifact_gc_ready=True
        engine.inventory_lock=nullcontext();engine.client=Mock()
        engine.client.call.return_value={'accepted':True,'node_id':'node-fixture'}
        original=Path.read_text
        def read(path,*args,**kwargs):
            if path==Path('/proc/meminfo'):return 'MemAvailable: 8388608 kB\n'
            if path==Path('/proc/stat'):return 'cpu 1 2 3 4 5 6 7 8\n'
            return original(path,*args,**kwargs)
        with patch('docker_runtime.discover'),patch('docker_runtime.image_available'), \
             patch('environment_contract.verify_config',return_value='e'*64), \
             patch('backends.physical_identity',return_value='host-fixture'), \
             patch.object(worker,'physical_identity',return_value='host-fixture'), \
             patch.object(Path,'read_text',read),patch.object(pressure,'sample',return_value=base()), \
             patch.object(pressure,'sample_execution',return_value=observation()), \
             patch('attempt_inventory.local',return_value={'fixture':True}), \
             patch('npu_measurement.source_sha',return_value='f'*64), \
             patch.object(worker,'atomic_json'),patch.object(worker,'identity',return_value={}):
            engine.heartbeat()
        action,body=engine.client.call.call_args.args
        self.assertEqual(action,'heartbeat');self.assertEqual(body['resource_pressure'],base())
        self.assertEqual(body['execution_pressure'],observation());self.assertIn(pressure.EXECUTION_CAPABILITY,body['capabilities'])
        self.assertEqual(body['io_limits'],limits(None))

    def test_real_node_heartbeat_waits_then_claims_original_queued_job(self):
        import test_dependencies as fixtures
        repository=Path(os.environ.get('ASCEND_LAB_REPOSITORY_ROOT',Path(__file__).resolve().parents[1]))
        with patch.object(fixtures,'ROOT',repository):
            fixture=fixtures.DependencyTests();fixture.setUp()
        self.addCleanup(fixture.tearDown);jobs=fixture.jobs
        jobs.policy['resource_pressure']=pressure.policy({'enabled':True,'reserve_pids':32,'cpu_some_avg10':80})
        job=jobs.submit(fixture.request(),fixture.alice)[0]
        from test_environment_contract import IMAGE,contract
        from environment_contract import CAPABILITY,fingerprint
        from workflow_spec import CAPABILITIES
        profile=jobs.nodes.node(fixture.node)['profile']
        body={'physical_id':profile['physical_id'],'protocol_version':1,'capabilities':['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY,*CAPABILITIES],
              'cpu_ids':jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
              'image_sha256':IMAGE,'environment_setup':None,'python_executable':'/usr/bin/python3','environment_sha256':fingerprint(contract()),
              'resource_pressure':base(100)}
        with patch('resource_pressure.time.time',return_value=100):
            jobs.nodes.heartbeat(fixture.node,body)
            self.assertIsNone(jobs.nodes.claim(fixture.node)['grant']);self.assertEqual(jobs.attempts(job['id']),[])
            body['capabilities'].append(pressure.EXECUTION_CAPABILITY);body['execution_pressure']=observation(pids=159)
            jobs.nodes.heartbeat(fixture.node,body)
            self.assertIsNone(jobs.nodes.claim(fixture.node)['grant']);self.assertEqual(jobs.attempts(job['id']),[])
            body['execution_pressure']=observation(pids=160,cpu=90);jobs.nodes.heartbeat(fixture.node,body)
            self.assertIsNone(jobs.nodes.claim(fixture.node)['grant'])
            body['execution_pressure']=observation(pids=160);jobs.nodes.heartbeat(fixture.node,body)
            report=jobs.nodes.node(fixture.node)['report'];self.assertEqual(report['execution_pressure'],body['execution_pressure'])
            grant=jobs.nodes.claim(fixture.node)['grant'];self.assertEqual(grant['job_id'],job['id']);self.assertEqual(len(jobs.attempts(job['id'])),1)
            saved=jobs.get(job['id']);body['execution_pressure']=observation(pids=0,cpu=90);jobs.nodes.heartbeat(fixture.node,body)
            self.assertEqual(jobs.get(job['id']),saved);self.assertEqual(len(jobs.attempts(job['id'])),1)
            with self.assertRaises(JobError):jobs.nodes.heartbeat(fixture.node,{**body,'execution_pressure':{'bad':True}})
            self.assertEqual(jobs.nodes.node(fixture.node)['report']['execution_pressure'],body['execution_pressure'])
            legacy=copy.deepcopy(body);legacy['capabilities'].remove(pressure.EXECUTION_CAPABILITY)
            with self.assertRaises(JobError):jobs.nodes.heartbeat(fixture.node,legacy)
            missing=copy.deepcopy(body);del missing['execution_pressure']
            with self.assertRaises(JobError):jobs.nodes.heartbeat(fixture.node,missing)


if __name__=='__main__':unittest.main()

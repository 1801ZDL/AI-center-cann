import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import pwd
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobs import ACTIVE, JobError, JobStore, ROOT, Scheduler, atomic_json, now, public, defaults
from server import ThreadingHTTPServer, handler_class
from executor import pump, stop_group


def fresh_snapshot(devices=None):
    return {'sampled_at': now(), 'host': {'memory_available_bytes': 8*1024**3,
                                        'shm': {'free_bytes': 4*1024**3},
                                        'workspace_disk': {'free_bytes': 10*1024**3}},
            'npu': {'status': 'ok', 'sampled_at': now(), 'devices': devices or []}}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'runtime', prefix='test-queue-')
        self.runtime = Path(self.temp.name)
        self.config = {'username': pwd.getpwuid(os.getuid()).pw_name, 'container': 'zdl'}
        self.jobs = JobStore(self.runtime, self.config)
        self.observations = Mock()
        self.observations.latest.return_value = fresh_snapshot()
        self.scheduler = Scheduler(self.jobs, self.observations, threading.Event())

    def tearDown(self):
        if self.scheduler.lock:
            self.scheduler.lock.close()
        self.temp.cleanup()

    def spec(self, key='test-key', **kw):
        return {'argv': ['/usr/bin/python3', '-c', 'print("CPU only")'], 'idempotency_key': key, **kw}

    def test_concurrent_idempotent_submission_returns_one_job(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.jobs.submit(self.spec()), range(4)))
        self.assertEqual(len({r[0]['id'] for r in results}), 1)
        self.assertEqual(len(self.jobs.all()), 1)
        with self.assertRaises(JobError) as error:
            self.jobs.submit(self.spec(cpu=2))
        self.assertEqual(error.exception.status, 409)

    def test_owner_workspace_budget_and_shell_validation(self):
        for changed in [{'owner': 'root'}, {'cwd': '/etc'}, {'cpu': True}, {'memory_mib': 999999},
                        {'kind': 'performance', 'npu': 0}, {'argv': ['echo', 'hello', '|', 'sh']}]:
            with self.subTest(changed=changed), self.assertRaises(JobError):
                self.jobs.submit(self.spec(**changed))
        self.assertEqual(self.jobs.all(), [])

    def test_npu_device_group_is_registered_policy_and_never_client_selected(self):
        env=self.jobs.environments['zdl-mxh-dev'];env['npu_gid']=1001
        normal=self.jobs.normalize(self.spec(environment='zdl-mxh-dev'),self.jobs.access.local)
        self.assertEqual(normal['_environment']['gid'],os.getgid())
        with patch('jobs.os.getgroups',return_value=[1001]):
            spec=self.jobs.normalize(self.spec(environment='zdl-mxh-dev',npu=1),self.jobs.access.local)
        self.assertEqual(spec['_environment']['gid'],1001)
        self.assertEqual(spec['_environment']['uid'],os.getuid())
        with patch('jobs.os.getgroups',return_value=[]),patch('jobs.os.getgid',return_value=1104):
            with self.assertRaises(JobError):self.jobs.normalize(self.spec(environment='zdl-mxh-dev',npu=1),self.jobs.access.local)
        with self.assertRaises(JobError):self.jobs.submit(self.spec(environment='zdl-mxh-dev',npu=1,npu_gid=1001))

    def test_environment_policy_override_preserves_registered_runtime(self):
        _,environments=defaults({**self.config,'environments':{'zdl-mxh-dev':{'npu_gid':1001}}})
        env=environments['zdl-mxh-dev']
        self.assertEqual(env['npu_gid'],1001)
        self.assertEqual(env['type'],'docker')
        self.assertEqual(env['python'],'/data/miniconda3/envs/mxh_dev/bin/python')

    def test_atomic_reservation_obeys_one_running_slot(self):
        a, _ = self.jobs.submit(self.spec('a'))
        b, _ = self.jobs.submit(self.spec('b'))
        with patch('jobs.subprocess.Popen') as launch:
            launch.return_value.poll.return_value = None
            self.scheduler.tick()
        self.assertEqual(self.jobs.get(a['id'])['state'], 'reserved')
        self.assertEqual(self.jobs.get(b['id'])['state'], 'queued')
        self.assertIn('运行名额', self.jobs.get(b['id'])['reason'])
        self.assertEqual(launch.call_count, 1)

    def test_external_npu_occupancy_blocks_dispatch(self):
        job, _ = self.jobs.submit(self.spec(npu=1))
        self.observations.latest.return_value = fresh_snapshot([
            {'id': 0, 'health': 'OK', 'utilization_pct': 0, 'scoped_process_count': 0, 'external_process_count': 1},
            {'id': 1, 'health': 'OK', 'utilization_pct': 100, 'scoped_process_count': 0, 'external_process_count': 0}])
        with patch('jobs.subprocess.Popen') as launch:
            self.scheduler.tick()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'queued')
        self.assertIn('不抢占', self.jobs.get(job['id'])['reason'])
        launch.assert_not_called()

    def test_two_npu_reservations_do_not_get_same_device(self):
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(domain['physical_id'],{**domain['policy'],'max_running':2},domain['revision'])
        self.jobs.submit(self.spec('a', npu=1))
        self.jobs.submit(self.spec('b', npu=1))
        self.observations.latest.return_value = fresh_snapshot([
            {'id': n, 'health': 'OK', 'utilization_pct': 0, 'scoped_process_count': 0, 'external_process_count': 0} for n in [0, 1]])
        with patch('jobs.subprocess.Popen') as launch:
            launch.return_value.poll.return_value = None
            self.scheduler.tick()
        self.assertEqual([j['allocation']['npu_ids'] for j in self.jobs.all()], [[0], [1]])

    def test_stale_monitor_data_does_not_authorize_launch(self):
        job, _ = self.jobs.submit(self.spec())
        self.observations.latest.return_value['sampled_at'] = '2026-01-01T00:00:00+00:00'
        with patch('jobs.subprocess.Popen') as launch:
            self.scheduler.tick()
        self.assertIn('新鲜', self.jobs.get(job['id'])['reason'])
        launch.assert_not_called()

    def test_cancel_queued_job_preserves_record_without_execution(self):
        job, _ = self.jobs.submit(self.spec())
        result = self.jobs.cancel(job['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue((self.jobs.directory(job['id'])/'result.json').is_file())
        self.assertEqual(self.jobs.summary()['reserved']['cpu'], 0)

    def test_shared_memory_is_included_in_total_memory(self):
        with self.assertRaises(JobError):
            self.jobs.submit(self.spec(memory_mib=128, shm_mib=256))

    def test_shared_memory_budget_blocks_without_double_counting_total_memory(self):
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(domain['physical_id'],{**domain['policy'],'max_running':2,'shm_mib':512},domain['revision'])
        first, _ = self.jobs.submit(self.spec('shm-a', memory_mib=512, shm_mib=384))
        second, _ = self.jobs.submit(self.spec('shm-b', memory_mib=512, shm_mib=384))
        with patch('jobs.subprocess.Popen') as launch:
            launch.return_value.poll.return_value = None
            self.scheduler.tick()
        self.assertEqual(self.jobs.get(first['id'])['state'], 'reserved')
        self.assertIn('共享内存申报', self.jobs.get(second['id'])['reason'])
        self.assertEqual(self.jobs.summary()['reserved']['memory_mib'], 512)
        self.assertEqual(self.jobs.summary()['reserved']['shm_mib'], 384)

    def test_observed_shared_memory_capacity_blocks_dispatch(self):
        self.observations.latest.return_value['host']['shm']['free_bytes'] = 128*1024**2
        job, _ = self.jobs.submit(self.spec(memory_mib=512, shm_mib=256))
        with patch('jobs.subprocess.Popen') as launch:
            self.scheduler.tick()
        self.assertIn('共享内存容量不足', self.jobs.get(job['id'])['reason'])
        launch.assert_not_called()

    def test_log_capture_has_a_bound(self):
        source = self.runtime/'stream'
        target = self.runtime/'output.log'
        source.write_bytes(b'x'*4096)
        pump(source.open('rb'), target, limit=128)
        output = target.read_bytes()
        self.assertEqual(output.count(b'x'), 128)
        self.assertIn(b'log limit reached', output)
        self.assertLess(len(output), 256)

    def test_reused_pid_does_not_signal_an_unrelated_process_group(self):
        with patch('executor.identity', return_value={'start_ticks': 999, 'uid': os.getuid()}), \
             patch('executor.os.killpg') as send:
            self.assertFalse(stop_group(12345, 123))
        send.assert_not_called()

    def test_uncertain_executor_keeps_resource_reservation(self):
        job, _ = self.jobs.submit(self.spec())
        with self.jobs.transaction() as db:
            internal = self.jobs.get(job['id'], db)
            internal.update(state='reserved', allocation={'cpu_ids': [self.jobs.policy['cpu_ids'][0]], 'npu_ids': [], 'memory_mib': 256}, _reserved_epoch=time.time()-30, _launch_attempted=True)
            self.jobs.write(db, internal)
        self.scheduler.reconcile()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'needs_attention')
        self.assertEqual(self.jobs.summary()['reserved']['cpu'], 1)

    def test_mismatched_completion_token_cannot_release_resources(self):
        job, _ = self.jobs.submit(self.spec())
        with self.jobs.transaction() as db:
            internal = self.jobs.get(job['id'], db)
            internal.update(state='reserved', allocation={'cpu_ids': [self.jobs.policy['cpu_ids'][0]], 'npu_ids': [], 'memory_mib': 256}, _reserved_epoch=time.time()-30, _launch_attempted=True)
            self.jobs.write(db, internal)
        atomic_json(self.jobs.directory(job['id'])/'completion.json', {'token': 'wrong', 'job_id': job['id'], 'result': {'status': 'succeeded'}})
        self.scheduler.reconcile()
        self.assertEqual(self.jobs.summary()['reserved']['cpu'], 1)

    def test_invalid_completion_state_cannot_requeue_running_job(self):
        job, _ = self.jobs.submit(self.spec())
        with self.jobs.transaction() as db:
            internal = self.jobs.get(job['id'], db)
            internal.update(state='reserved', allocation={'cpu_ids': [self.jobs.policy['cpu_ids'][0]], 'npu_ids': [], 'memory_mib': 256}, _reserved_epoch=time.time()-30, _launch_attempted=True)
            self.jobs.write(db, internal)
        atomic_json(self.jobs.directory(job['id'])/'completion.json', {'token': internal['_token'], 'job_id': job['id'], 'result': {'status': 'queued'}})
        self.scheduler.reconcile()
        self.assertEqual(self.jobs.get(job['id'])['state'], 'needs_attention')
        self.assertEqual(self.jobs.summary()['reserved']['cpu'], 1)

    def test_scheduler_leader_lock_is_exclusive(self):
        self.scheduler.acquire()
        other = Scheduler(self.jobs, self.observations, threading.Event())
        with self.assertRaises(RuntimeError):
            other.acquire()

    def test_source_entrypoint_is_fingerprinted_and_copied(self):
        source = self.runtime/'entry.py'
        source.write_text('print("v1")\n')
        job, _ = self.jobs.submit(self.spec(argv=['python', str(source)]))
        copy = self.jobs.directory(job['id'])/'source/entry.py'
        source.write_text('print("v2")\n')
        self.assertEqual(copy.read_text(), 'print("v1")\n')
        self.assertEqual(job['source']['path'], str(source))

    def test_unreadable_source_returns_error_without_orphan_job_directory(self):
        if os.getuid()==0:self.skipTest('Requires an unprivileged source reader')
        source=self.runtime/'private.py';source.write_text('print(1)\n');source.chmod(0)
        try:
            with self.assertRaises(JobError) as error:self.jobs.submit(self.spec(argv=['python',str(source)]))
            self.assertEqual(error.exception.status,403)
            self.assertFalse(list(self.jobs.root.iterdir()))
        finally:source.chmod(0o600)


class JobHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'runtime', prefix='test-api-')
        self.config = {'username': pwd.getpwuid(os.getuid()).pw_name, 'container': 'zdl', 'password': 'test-only', 'interval': 15}
        self.jobs = JobStore(Path(self.temp.name), self.config)
        observations = Mock()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_class(observations, self.config, self.jobs))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, body=None, auth=True, origin=None, content_type='application/json'):
        headers = {}
        if auth:
            headers['Authorization'] = 'Basic '+base64.b64encode((self.config['username']+':test-only').encode()).decode()
        if origin:
            headers['Origin'] = origin
        if body is not None:
            headers['Content-Type'] = content_type
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
                                         data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error

    def test_submit_requires_auth_and_json_and_rejects_cross_origin(self):
        body = {'argv': ['python', '-c', 'print(1)']}
        for kw, expected in [({'auth': False}, 401), ({'origin': 'https://other.example'}, 403), ({'content_type': 'text/plain'}, 415)]:
            with self.subTest(kw=kw), self.call('/api/jobs', body, **kw) as response:
                self.assertEqual(response.status, expected)
        self.assertEqual(self.jobs.all(), [])

    def test_submit_list_details_cancel_and_private_artifact_denial(self):
        with self.call('/api/jobs', {'argv': ['python', '-c', 'print(1)'], 'idempotency_key': 'api-case'}) as response:
            self.assertEqual(response.status, 201)
            job = json.load(response)['job']
            self.assertNotIn('_token', job)
        with self.call('/api/jobs/'+job['id']) as response:
            self.assertEqual(json.load(response)['job']['state'], 'queued')
        with self.call('/api/jobs/'+job['id']+'/cancel', {}) as response:
            self.assertEqual(json.load(response)['job']['state'], 'cancelled')
        with self.call('/api/jobs/'+job['id']+'/artifact?name=../../config.json') as response:
            self.assertEqual(response.status, 404)


if __name__ == '__main__':
    unittest.main()

"""Small CPU-only checks. No controllers, Docker, jobs, shells, devices or IO delivery."""
import base64
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resource_pressure as pressure
import notifications
import interactive_debug as debug
import maintenance_runner as upkeep
from probe_guard import ProbeGuard


class PressureTests(unittest.TestCase):
    def fixture(self, root):
        proc, cg = root / 'proc', root / 'cgroup'
        (proc / 'self').mkdir(parents=True)
        (proc / 'pressure').mkdir()
        (proc / 'meminfo').write_text('MemAvailable: 1000000 kB\nSwapFree: 100000 kB\n')
        (proc / 'self/cgroup').write_text('0::/user/task\n')
        for key in ['memory', 'io']:
            (proc / 'pressure' / key).write_text('full avg10=0.00 avg60=0.00\n')
        for relative in ['', 'user', 'user/task']:
            path = cg / relative
            path.mkdir(parents=True, exist_ok=True)
            values = {'memory.current': '100', 'memory.pressure': 'full avg10=0.00\n', 'io.pressure': 'full avg10=0.00\n'}
            if relative:
                values.update({'memory.high': 'max', 'memory.max': 'max', 'memory.swap.max': 'max', 'memory.swap.current': '0'})
            for name, value in values.items():
                (path / name).write_text(value)
        return proc, cg

    def test_parent_high_overrides_abundant_host_memory_and_root_has_no_limit_files(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, cg = self.fixture(Path(folder))
            (cg / 'user/memory.current').write_text('700')
            (cg / 'user/memory.high').write_text('640')
            value = pressure.sample(proc, cg, epoch=100)
            self.assertEqual((value['status'], value['available_bytes'], value['ancestor_count']), ('ready', 0, 3))
            self.assertIsNotNone(pressure.reason(value, 1, {'enabled': True}, epoch=100))
            self.assertIsNone(pressure.reason(value, 1, epoch=100))

    def test_ancestor_psi_and_swap_are_preserved_without_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            proc, cg = self.fixture(Path(folder))
            (cg / 'user/memory.pressure').write_text('full avg10=80.5\n')
            (cg / 'user/memory.swap.max').write_text('10')
            (cg / 'user/memory.swap.current').write_text('10')
            value = pressure.sample(proc, cg, epoch=100)
            self.assertEqual(value['swap_free_bytes'], 0)
            self.assertEqual(value['memory_full_avg10'], 80.5)
            self.assertIsNotNone(pressure.reason(value, 0, {'enabled': True}, epoch=100))
            self.assertNotIn(folder, json.dumps(value))

    def test_missing_malformed_stale_and_future_observations_fail_closed_only_when_enabled(self):
        for value in [None, {}, {'status': 'ready'}]:
            self.assertIsNotNone(pressure.reason(value, 0, {'enabled': True}, epoch=100))
        with tempfile.TemporaryDirectory() as folder:
            proc, cg = self.fixture(Path(folder))
            value = pressure.sample(proc, cg, epoch=100)
            self.assertIsNotNone(pressure.reason(value, 0, {'enabled': True}, epoch=200))
            self.assertIsNotNone(pressure.reason(value, 0, {'enabled': True}, epoch=0))
            (cg / 'user/memory.high').unlink()
            self.assertEqual(pressure.sample(proc, cg)['status'], 'unknown')


class ProbeTests(unittest.TestCase):
    def test_unknown_launch_blocks_a_second_probe_without_forking(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'probe.json').write_text('{"phase":"launching"}')
            (path / 'probe.json').chmod(0o600)
            with patch('probe_guard.subprocess.Popen') as spawn:
                with self.assertRaises(RuntimeError):
                    ProbeGuard(path).run(['npu-smi', 'info'])
                spawn.assert_not_called()

    def test_kernel_stuck_probe_is_journaled_and_never_restarted(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock(pid=123, returncode=None)
            process.poll.return_value = None
            process.communicate.side_effect = subprocess.TimeoutExpired(['npu-smi'], 1)
            ident = {'pid': 123, 'uid': os.getuid(), 'start': 42, 'boot': 'test', 'state': 'D'}
            with patch('probe_guard.subprocess.Popen', return_value=process) as spawn, \
                 patch('probe_guard.identity', return_value=ident), patch('probe_guard.os.killpg') as kill:
                with self.assertRaises(RuntimeError):
                    ProbeGuard(folder).run(['npu-smi', 'info'], 1)
                self.assertEqual(json.loads((Path(folder) / 'probe.json').read_text())['phase'], 'blocked_after_kill')
                with self.assertRaises(RuntimeError):
                    ProbeGuard(folder).run(['npu-smi', 'info'], 1)
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(kill.call_count, 1)

    def test_container_exec_and_arbitrary_shell_are_never_spawned(self):
        with tempfile.TemporaryDirectory() as folder, patch('probe_guard.subprocess.Popen') as spawn:
            for argv in [['docker', 'exec', 'tenant', 'df'], ['bash', '-c', 'anything'], ['python3', '-c', 'anything']]:
                with self.assertRaises(RuntimeError):ProbeGuard(folder).run(argv)
            spawn.assert_not_called()

    def test_fast_probe_failure_opens_a_persisted_cooldown_without_repeated_spawn(self):
        with tempfile.TemporaryDirectory() as folder:
            process=Mock(pid=123,returncode=1)
            process.communicate.return_value=('','private driver error')
            process.poll.return_value=1
            with patch('probe_guard.subprocess.Popen',return_value=process) as spawn, \
                 patch('probe_guard.identity',return_value={'pid':123,'uid':os.getuid(),'start':42,'boot':'test','state':'R'}), \
                 patch('probe_guard.time.time',return_value=100):
                with self.assertRaises(RuntimeError):ProbeGuard(folder).run(['npu-smi','info'])
                saved=json.loads((Path(folder)/'probe.json').read_text())
                self.assertEqual((saved['phase'],saved['until_epoch']),('cooldown',160))
                with self.assertRaises(RuntimeError):ProbeGuard(folder).run(['npu-smi','info'])
                self.assertEqual(spawn.call_count,1)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        notifications.initialize(self.db)
        self.db.commit()
        self.settings = {'enabled': True, 'max_pending': 64}

    def tearDown(self):
        self.db.close()

    def emit(self, owner='alice', epoch=1):
        return notifications.append(self.db, owner, 'job_state', 'job-' + 'a' * 12, 'failed', self.settings, epoch=epoch)

    def test_outbox_rolls_back_with_job_transition_and_owner_views_do_not_leak(self):
        self.db.execute('BEGIN')
        self.emit()
        self.db.rollback()
        self.assertEqual(notifications.page(self.db, 'alice')['events'], [])
        self.emit('alice')
        self.emit('bob')
        self.assertEqual(len(notifications.page(self.db, 'alice')['events']), 1)
        self.assertEqual(notifications.page(self.db, 'other')['events'], [])

    def test_finite_outbox_retains_unsent_and_reports_overflow(self):
        for _ in range(65):self.emit()
        self.assertEqual(self.db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0], 64)
        self.assertEqual(notifications.page(self.db, 'alice')['dropped_events'], 1)

    def test_delivery_failure_keeps_original_id_backoff_and_does_not_block_other_configured_owner(self):
        self.emit('unconfigured', epoch=0)
        ident = self.emit('alice')
        self.db.commit()
        destination = {'alice': {'url': 'https://example.invalid/hook'}}
        transport = Mock(side_effect=OSError('remote detail must stay private'))
        first = notifications.dispatch_one(self.db, destination, transport=transport, epoch=2)
        self.assertEqual(first, {'status': 'retry_later', 'event_id': ident})
        self.assertEqual(notifications.dispatch_one(self.db, destination, transport=transport, epoch=3)['status'], 'idle')
        transport.side_effect = None
        second = notifications.dispatch_one(self.db, destination, transport=transport, epoch=63)
        self.assertEqual(second, {'status': 'delivered', 'event_id': ident})
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(transport.call_args.args[1]['id'], ident)
        self.assertEqual(notifications.page(self.db, 'alice')['events'][0]['delivery_attempts'], 2)

    def test_signal_changes_recover_and_reject_unsafe_destinations(self):
        for value in ['unknown', 'unknown', 'ready']:
            notifications.observe(self.db, 'alice', 'storage_pressure', 'controller', value, self.settings)
        self.assertEqual(len(notifications.page(self.db, 'alice')['events']), 2)
        for url in ['http://example.invalid', 'https://user:secret@example.invalid', 'https://example.invalid:8443', 'https://example.invalid/#fragment']:
            with self.assertRaises(ValueError):notifications.destination({'url': url})

    def test_archive_verifies_delivered_rows_and_keeps_pending_events(self):
        ident=self.emit('alice');pending=self.emit('bob')
        self.db.execute('UPDATE notification_outbox SET delivered=1 WHERE id=?',(ident,));self.db.commit()
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(notifications.archive_delivered(self.db,folder)['writes'])
            self.assertEqual(list(Path(folder).iterdir()),[])
            result=notifications.archive_delivered(self.db,folder,apply=True)
            self.assertEqual((result['status'],result['rows'],result['generation']),('archived',1,1))
            value=json.loads((Path(folder)/(result['sha256']+'.json')).read_text())
            self.assertEqual(value['rows'][0][0],ident)
            self.assertEqual(self.db.execute('SELECT id FROM notification_outbox').fetchall(),[(pending,)])

    def test_corrupt_archive_never_removes_the_original_delivered_event(self):
        ident=self.emit('alice');self.db.execute('UPDATE notification_outbox SET delivered=1 WHERE id=?',(ident,));self.db.commit()
        row=self.db.execute('SELECT * FROM notification_outbox WHERE id=?',(ident,)).fetchone()
        checkpoint=self.db.execute('SELECT record FROM notification_archive_checkpoint').fetchone()[0]
        with tempfile.TemporaryDirectory() as folder:
            result=notifications.archive_delivered(self.db,folder,apply=True)
            # Reconstruct a crash before the SQL commit in this tiny fixture.
            self.db.execute('INSERT INTO notification_outbox VALUES(?,?,?,?,?,?,?,?,?,?)',row)
            self.db.execute('UPDATE notification_archive_checkpoint SET record=?',(checkpoint,));self.db.commit()
            (Path(folder)/(result['sha256']+'.json')).write_text('corrupted')
            with self.assertRaises(ValueError):notifications.archive_delivered(self.db,folder,apply=True)
            self.assertEqual(self.db.execute('SELECT id FROM notification_outbox').fetchall(),[(ident,)])

    def test_job_write_and_notification_are_one_actual_sql_transaction(self):
        from jobs import JobStore
        self.db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT,data TEXT)')
        self.db.execute("INSERT INTO jobs VALUES('job-aaaaaaaaaaaa','running','{}')");self.db.commit()
        store=JobStore.__new__(JobStore);store.notification_policy=self.settings
        store.storage=SimpleNamespace(settle=lambda *_:None)
        self.db.execute('BEGIN')
        store.write(self.db,{'id':'job-aaaaaaaaaaaa','owner':'alice','state':'failed'})
        self.assertEqual(len(notifications.page(self.db,'alice')['events']),1)
        self.db.rollback()
        self.assertEqual(notifications.page(self.db,'alice')['events'],[])
        self.assertEqual(self.db.execute('SELECT state FROM jobs').fetchone()[0],'running')


class MaintenanceTests(unittest.TestCase):
    def test_preview_never_writes_and_each_call_advances_one_exact_step(self):
        value = upkeep.plan('/fixed/prefix', [{'action': 'audit_export', 'output': '/protected/audit.json'},
                                                {'action': 'audit_prune', 'export_step': 0}])
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'journal.json'
            run = Mock(return_value=({'sha256': 'a' * 64}, True))
            self.assertFalse(upkeep.run_once(value, journal, executor=run)['writes'])
            self.assertFalse(journal.exists())
            self.assertEqual(upkeep.run_once(value, journal, apply=True, executor=run)['next_step'], 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(upkeep.run_once(value, journal, apply=True, executor=run)['status'], 'complete')
            self.assertEqual(upkeep.run_once(value, journal, apply=True, executor=run)['status'], 'complete')
            self.assertEqual(run.call_count, 2)

    def test_failure_resumes_original_step_and_changed_plan_is_rejected(self):
        value = upkeep.plan('/fixed/prefix', [{'action': 'audit_export', 'output': '/protected/audit.json'}])
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'journal.json'
            run = Mock(side_effect=OSError('interrupted'))
            with self.assertRaises(OSError):upkeep.run_once(value, journal, apply=True, executor=run)
            self.assertEqual(json.loads(journal.read_text())['pending'], 0)
            changed = upkeep.plan('/fixed/prefix', [{'action': 'audit_export', 'output': '/protected/other.json'}])
            with self.assertRaises(ValueError):upkeep.run_once(changed, journal, apply=True, executor=run)
            run.side_effect = None
            run.return_value = ({'sha256': 'a' * 64}, True)
            self.assertEqual(upkeep.run_once(value, journal, apply=True, executor=run)['status'], 'complete')

    def test_rotation_preimage_is_persisted_before_apply_and_not_replanned_after_commit_loss(self):
        value={'prefix':'/fixed/prefix','id':'a'*64,'steps':[{'action':'control_rotate','plan':{'id':'original-copy'}}]}
        prepared={'id':'fixed-cas-before-image'};seen=[]
        def execute(prefix,step,results,steps):
            seen.append(copy.deepcopy(results))
            if len(seen)==1:raise OSError('commit succeeded but response was lost')
            return {'reused':True},True
        with tempfile.TemporaryDirectory() as folder,patch('maintenance_runner.checked',return_value=value):
            journal=Path(folder)/'journal.json';prepare=Mock(return_value=prepared)
            self.assertTrue(upkeep.run_once(value,journal,apply=True,executor=execute,prepare=prepare)['prepared'])
            self.assertEqual(seen,[])
            with self.assertRaises(OSError):upkeep.run_once(value,journal,apply=True,executor=execute,prepare=prepare)
            self.assertEqual(upkeep.run_once(value,journal,apply=True,executor=execute,prepare=prepare)['status'],'complete')
            self.assertEqual(seen[0]['0']['prepared_rotation_plan'],prepared)
            self.assertEqual(seen[1]['0']['prepared_rotation_plan'],prepared)
            self.assertEqual(prepare.call_count,1)


class DebugTests(unittest.TestCase):
    def value(self, seq=1, text='echo hello\n'):
        return {'seq': seq, 'data': base64.b64encode(text.encode()).decode()}

    def engine(self):
        engine = SimpleNamespace(state={}, snapshots=[])
        engine.save = lambda: engine.snapshots.append(copy.deepcopy(engine.state))
        return engine

    def test_stdin_intent_is_saved_before_delivery_and_duplicate_frame_is_not_sent(self):
        engine = self.engine()
        transport = Mock()
        result = debug.node_input(engine, self.value(), transport=transport)
        self.assertEqual(engine.snapshots[0]['debug_delivery']['state'], 'delivery_unknown')
        self.assertEqual(result['state'], 'stdin_sent')
        debug.node_input(engine, self.value(), transport=transport)
        self.assertEqual(transport.call_count, 1)
        with self.assertRaises(ValueError):debug.node_input(engine, self.value(text='different'), transport=transport)

    def test_uncertain_delivery_is_not_replayed_after_reconnect(self):
        engine = self.engine()
        transport = Mock(side_effect=OSError('lost connection'))
        first = debug.node_input(engine, self.value(), transport=transport)
        self.assertEqual(first['state'], 'delivery_unknown')
        self.assertEqual(debug.node_input(engine, self.value(), transport=transport), first)
        with self.assertRaises(ValueError):debug.node_input(engine, self.value(2), transport=transport)
        self.assertEqual(transport.call_count, 1)

    def test_normal_task_cannot_receive_debug_input_and_runtime_rejects_replays(self):
        with self.assertRaises(ValueError):debug.exchange(None, {'template': 'ir_v1'}, {'debug_ack': {'seq': 1}})
        from debug_runtime import decode
        self.assertEqual(decode(self.value(), 0), b'echo hello\n')
        with self.assertRaises(ValueError):decode(self.value(), 1)
        with self.assertRaises(ValueError):decode(self.value(3), 0)

    def test_transport_uses_original_container_id_and_stdin_attach_never_exec(self):
        connection=Mock();connection.getresponse.return_value.status=101
        engine=SimpleNamespace(binding=lambda:{'endpoint':'unix:///tmp/fixed-test.sock','daemon_id':'test'},
            inspect=lambda:{'Id':'fixed-container-id','State':{'Running':True},'Config':{'OpenStdin':True}},
            grant={'template':'debug_v1','owner':'alice','resources':{'npu':0}},config={'users':{'alice':{'uid':1104}}})
        with patch('docker_runtime.discover') as discover,patch('docker_runtime.UnixHTTP',return_value=connection):
            debug.send_stdin(engine,self.value())
        discover.assert_called_once()
        self.assertEqual(connection.request.call_args.args[0],'POST')
        self.assertTrue(connection.request.call_args.args[1].startswith('/containers/fixed-container-id/attach?'))
        self.assertNotIn('exec',connection.request.call_args.args[1])
        self.assertEqual(json.loads(connection.sock.sendall.call_args.args[0]),self.value())
        connection.close.assert_called_once()

    def test_debug_template_requires_fixed_shell_no_npu_and_finite_lease(self):
        from templates import expand_template
        request = {'template': 'debug_v1', 'environment': 'worker-python',
                   'parameters': {'input_id': 'input-' + 'a' * 24, 'argv': ['/bin/bash'],
                                  'lock': {'python': '3.11.0'}, 'timeout_seconds': 60}}
        result = expand_template(request)
        self.assertEqual(result['kind'], 'command')
        self.assertTrue(result['argv'][-1].endswith('debug_runtime.py'))
        for overrides in [{'argv': ['sh']}, {'timeout_seconds': 301}, {'resources': {'npu': 1}}, {'resources': {'cpu': 3}}]:
            candidate = copy.deepcopy(request)
            candidate['parameters'].update(overrides)
            with self.assertRaises(ValueError):expand_template(candidate)

    def test_owner_only_mailbox_retry_and_unknown_delivery_gate(self):
        from contextlib import contextmanager
        from jobs import JobError
        db=sqlite3.connect(':memory:')
        @contextmanager
        def transaction():
            with db:yield db
        job={'id':'job-aaaaaaaaaaaa','owner':'alice','template':'debug_v1','state':'running',
             '_debug_capable':True,'timeout_seconds':60}
        jobs=SimpleNamespace(transaction=transaction,get=lambda *_:job,
                             policy={'interactive_debug':{'enabled':True,'owners':['alice']}})
        principal=SimpleNamespace(id='bob',experiment_id=None,require=lambda *_:None)
        request={'key':'first','data':self.value()['data']}
        with self.assertRaises(JobError):debug.submit(jobs,principal,job['id'],request)
        principal.id='alice'
        self.assertEqual(debug.submit(jobs,principal,job['id'],request)['seq'],1)
        self.assertTrue(debug.submit(jobs,principal,job['id'],request)['reused'])
        self.assertEqual(db.execute('SELECT count(*) FROM debug_inputs').fetchone()[0],1)
        response=debug.exchange(db,job,{})
        self.assertEqual(response['debug_input']['seq'],1)
        debug.exchange(db,job,{'debug_ack':{'seq':1,'state':'delivery_unknown'}})
        db.commit()
        with self.assertRaises(JobError):debug.submit(jobs,principal,job['id'],{'key':'second','data':request['data']})
        with self.assertRaises(ValueError):debug.exchange(db,job,{'debug_ack':{'seq':1,'state':'stdin_sent'}})
        with self.assertRaises(ValueError):debug.exchange(db,job,{'debug_ack':{'seq':2,'state':'stdin_sent'}})
        db.close()


class HistoryGuardTests(unittest.TestCase):
    def test_restore_and_downgrade_cannot_drop_notification_or_debug_delivery_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            old,new=Path(folder)/'old',Path(folder)/'new';old.mkdir();new.mkdir()
            with sqlite3.connect(old/'jobs.sqlite3') as db:
                notifications.initialize(db);debug.initialize(db)
                notifications.append(db,'alice','job_state','job-aaaaaaaaaaaa','failed',{'enabled':True},epoch=1)
                db.execute("INSERT INTO debug_inputs VALUES('job-aaaaaaaaaaaa',1,'key','sha','eA==','delivery_unknown')")
            source=sqlite3.connect(old/'jobs.sqlite3');target=sqlite3.connect(new/'jobs.sqlite3')
            source.backup(target);target.close();source.close()
            notifications.require_history(old,new);debug.require_history(old,new)
            with self.assertRaises(ValueError):notifications.require_compatible(old,'controller',{'files':{}})
            with self.assertRaises(ValueError):debug.require_compatible(old,'controller',{'files':{}})
            with sqlite3.connect(new/'jobs.sqlite3') as db:
                db.execute('DELETE FROM notification_outbox');db.execute('DELETE FROM debug_inputs')
            with self.assertRaises(ValueError):notifications.require_history(old,new)
            with self.assertRaises(ValueError):debug.require_history(old,new)


if __name__ == '__main__':
    unittest.main()

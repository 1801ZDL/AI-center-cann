import copy
import base64
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from cooperative_start import DEFERRED,MAX_DEFERRALS,acknowledge,candidate,contract,defer,folder,intent,wait_reason,definitely_stopped
from executor import supervisor,run_command
from jobs import ROOT,JobStore,Scheduler,atomic_json,read_json,now
from npu_admission import NPUNotReady
from test_jobs import fresh_snapshot


class PrestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-prestart-');self.root=Path(self.tmp.name)
        self.config={'username':pwd.getpwuid(os.getuid()).pw_name,'container':'zdl'}
        self.store=JobStore(self.root,self.config);self.obs=Mock()
        self.obs.latest.return_value=fresh_snapshot([{'id':1,'health':'OK','utilization_pct':0,'scoped_process_count':0,'external_process_count':0}])
        self.scheduler=Scheduler(self.store,self.obs,threading.Event())
        self.job,_=self.store.submit({'argv':['/usr/bin/python3','-c','raise SystemExit(0)'],'npu':1,'idempotency_key':'prestart-original'})
        self.reserve()

    def tearDown(self):self.tmp.cleanup()

    def reserve(self):
        with patch('jobs.subprocess.Popen') as process:
            process.return_value.poll.return_value=None;self.scheduler.tick()
        self.job=self.store.get(self.job['id']);self.directory=self.store.directory(self.job['id'])

    def refuse(self):
        with patch('executor.fingerprint',return_value={}),patch('executor.os.access',return_value=True),\
             patch('npu_admission.query',side_effect=NPUNotReady('observed busy')),patch('executor.subprocess.Popen') as payload:
            supervisor(self.directory,self.job)
        payload.assert_not_called()
        self.assertFalse((folder(self.directory,self.job)/'launch.json').exists())
        return read_json(self.directory/'completion.json')

    def reconcile(self):
        with patch('cooperative_start.definitely_stopped',return_value=True):self.scheduler.reconcile()
        return self.store.get(self.job['id'])

    def test_busy_before_popen_requeues_original_and_preserves_idempotency_budget(self):
        original=copy.deepcopy(self.job)
        with self.store.connect() as db:storage=list(db.execute('SELECT * FROM storage_allocations'))
        completion=self.refuse()
        self.assertEqual(completion['result']['status'],DEFERRED)
        job=self.reconcile();self.assertEqual(job['state'],'queued');self.assertEqual(job['allocation'],{})
        self.assertEqual(job['prestart_deferrals']['count'],1)
        self.assertNotEqual(job['_token'],original['_token'])
        for name in ['id','owner','argv','environment','resources','created_at','idempotency_key']:self.assertEqual(job[name],original[name])
        self.assertEqual(self.store.attempts(job['id'])[0]['state'],DEFERRED)
        same,reused=self.store.submit({'argv':original['argv'],'npu':1,'idempotency_key':'prestart-original'})
        self.assertTrue(reused);self.assertEqual(same['id'],job['id']);self.assertEqual(len(self.store.all()),1)
        self.assertEqual(self.store.summary()['reserved']['cpu'],0)
        with self.store.connect() as db:self.assertEqual(storage,list(db.execute('SELECT * FROM storage_allocations')))

    def test_original_supervisor_must_exit_before_resources_are_released(self):
        self.refuse();self.scheduler.reconcile();job=self.store.get(self.job['id'])
        self.assertEqual(job['state'],'reserved');self.assertEqual(self.store.summary()['user_reserved']['npu'],1)

    def test_text_only_error_or_unavailable_query_does_not_defer(self):
        with patch('executor.fingerprint',return_value={}),patch('executor.os.access',return_value=True),\
             patch('npu_admission.query',side_effect=RuntimeError('NPU health or occupancy changed before launch; user workload was not started')):
            supervisor(self.directory,self.job)
        self.assertEqual(read_json(self.directory/'completion.json')['result']['status'],'failed')
        self.assertIsNone(candidate(self.directory,self.job))

    def test_source_change_never_creates_a_valid_deferral(self):
        self.job['_cooperative_start']['sources']['executor.py']='0'*64
        self.assertFalse(defer(self.directory,self.job))
        with self.assertRaises(RuntimeError):intent(self.directory,self.job)

    def test_intent_precedes_any_popen_and_closes_the_deferral_path(self):
        with patch('executor.fingerprint',return_value={}),patch('executor.os.access',return_value=True),\
             patch('npu_admission.query',return_value=([{'id':1,'health':'OK','utilization_pct':0}],[])),\
             patch('executor.subprocess.Popen',side_effect=NPUNotReady('late error')) as process:
            run_command(self.directory,self.job)
        process.assert_called_once();self.assertTrue((folder(self.directory,self.job)/'launch.json').is_file())
        self.assertFalse(defer(self.directory,self.job))
        self.assertEqual(read_json(self.directory/'completion.json')['result']['status'],'failed')

    def test_existing_command_identity_blocks_deferral_even_without_new_intent(self):
        atomic_json(self.directory/'command-process.json',{'pid':123})
        self.assertFalse(defer(self.directory,self.job))

    def test_dangling_launch_marker_is_not_interpreted_as_no_launch(self):
        base=folder(self.directory,self.job);base.mkdir(parents=True)
        (base/'launch.json').symlink_to(base/'absent')
        self.assertFalse(defer(self.directory,self.job))
        with self.assertRaises(RuntimeError):intent(self.directory,self.job)

    def test_deferral_discards_stale_idle_aging_protection(self):
        with self.store.transaction() as db:
            self.job=self.store.get(self.job['id'],db);self.job['_aging_target']={'stale':'no longer eligible'};self.store.write(db,self.job)
        self.refuse();job=self.reconcile();self.assertNotIn('_aging_target',job)

    def test_forged_or_mismatched_candidate_retains_resources(self):
        self.refuse();path=folder(self.directory,self.job)/'candidate.json';value=read_json(path);value['_token']='wrong';atomic_json(path,value)
        job=self.reconcile();self.assertEqual(job['state'],'needs_attention');self.assertEqual(self.store.summary()['user_reserved']['npu'],1)

    def test_stale_completion_cannot_release_a_new_attempt(self):
        old=self.refuse();self.reconcile()
        with self.store.transaction() as db:
            job=self.store.get(self.job['id'],db);job['_prestart_not_before']=0;self.store.write(db,job)
        self.obs.latest.return_value=fresh_snapshot([{'id':1,'health':'OK','utilization_pct':0,'scoped_process_count':0,'external_process_count':0}])
        self.reserve();self.assertNotEqual(self.job['attempt_id'],old['attempt_id'])
        atomic_json(self.directory/'completion.json',old);self.scheduler.reconcile()
        self.assertEqual(self.store.get(self.job['id'])['state'],'reserved')

    def test_restart_and_duplicate_ack_do_not_double_count_or_launch(self):
        old=self.refuse();self.reconcile();self.store=JobStore(self.root,self.config)
        self.scheduler=Scheduler(self.store,self.obs,threading.Event());atomic_json(self.directory/'completion.json',old)
        self.scheduler.reconcile();job=self.store.get(self.job['id'])
        self.assertEqual(job['_cooperative_deferral_count'],1);self.assertEqual(len(self.store.attempts(job['id'])),1)
        self.assertEqual(job['state'],'queued')

    def test_cancel_before_ack_is_terminal_and_not_requeued(self):
        self.refuse();self.store.cancel(self.job['id']);job=self.reconcile()
        self.assertEqual(job['state'],'cancelled');self.assertEqual(self.store.summary()['user_reserved']['npu'],0)

    def test_cooldown_does_not_block_an_independent_cpu_job(self):
        self.refuse();self.reconcile();cpu,_=self.store.submit({'argv':['/usr/bin/true'],'idempotency_key':'cpu-follows'})
        with patch('jobs.subprocess.Popen') as process:
            process.return_value.poll.return_value=None;self.scheduler.tick()
        self.assertEqual(self.store.get(self.job['id'])['state'],'queued');self.assertEqual(self.store.get(cpu['id'])['state'],'reserved')

    def test_fresh_snapshot_and_backoff_are_both_required(self):
        self.refuse();job=self.reconcile()
        self.assertIsNotNone(wait_reason(job,self.obs.latest()))
        job['_prestart_not_before']=0;old=copy.deepcopy(self.obs.latest());old['npu']['sampled_at']='2000-01-01T00:00:00+00:00'
        self.assertIsNotNone(wait_reason(job,old));self.assertIsNone(wait_reason(job,fresh_snapshot()))
        for value in [None,float('nan'),True]:
            job['_prestart_not_before']=value;self.assertIsNotNone(wait_reason(job,fresh_snapshot()))

    def test_retry_history_is_bounded_without_any_payload_retry(self):
        with self.store.transaction() as db:
            self.job=self.store.get(self.job['id'],db);self.job['_cooperative_deferral_count']=MAX_DEFERRALS-1;self.store.write(db,self.job)
        self.refuse();job=self.reconcile();self.assertEqual(job['state'],'failed')
        self.assertEqual(job['result']['failure_stage'],'prestart_retry_limit');self.assertIsNone(job['started_at'])
        self.assertEqual(job['prestart_deferrals']['count'],MAX_DEFERRALS)

    def test_database_rollback_replays_the_same_receipt_once(self):
        self.refuse();original=self.store.write
        def crash(db,job):
            original(db,job)
            if job.get('_cooperative_deferral_count'):raise RuntimeError('simulated process exit before commit')
        with patch.object(self.store,'write',side_effect=crash),patch('cooperative_start.definitely_stopped',return_value=True):
            with self.assertRaises(RuntimeError):self.scheduler.reconcile()
        self.assertEqual(self.store.get(self.job['id'])['state'],'reserved')
        job=self.reconcile();self.assertEqual(job['_cooperative_deferral_count'],1)

    def test_absent_proc_boot_observation_is_not_death(self):
        from jobs import identity
        saved=identity(os.getpid())
        with patch('cooperative_start.Path.read_text',side_effect=PermissionError('no observation')):self.assertFalse(definitely_stopped(saved))

    def test_retained_contract_blocks_code_without_support(self):
        from maintenance import require_features,DeployError
        with self.assertRaises(DeployError):require_features(self.root,'controller',{'files':{}})

    def test_requeued_task_rechecks_a_revoked_real_application_identity(self):
        runtime=self.root/'revocation';runtime.mkdir()
        self.store=JobStore(runtime,self.config);access=self.store.access
        password=access.create_user('prestart_alice',self.root,['host-python'],{'npu':1})
        with access.connect() as db:
            value=json.loads(db.execute('SELECT profile FROM users WHERE id=?',('prestart_alice',)).fetchone()[0]);value['mode']='commands'
            db.execute('UPDATE users SET profile=? WHERE id=?',(json.dumps(value),'prestart_alice'))
        principal=access.authenticate('Basic '+base64.b64encode(('prestart_alice:'+password).encode()).decode())
        self.job,_=self.store.submit({'argv':['/usr/bin/true'],'cwd':str(self.root),'npu':1,'idempotency_key':'revoked-start'},principal)
        self.scheduler=Scheduler(self.store,self.obs,threading.Event());self.reserve();self.refuse();self.reconcile()
        access.disable_user('prestart_alice')
        with self.store.transaction() as db:
            value=self.store.get(self.job['id'],db);value['_prestart_not_before']=0;self.store.write(db,value)
        with patch('jobs.subprocess.Popen') as launch:self.scheduler.tick()
        launch.assert_not_called();self.assertEqual(self.store.get(self.job['id'])['state'],'queued')

    def test_wrong_transport_or_launch_intent_invalidates_the_candidate(self):
        self.refuse();value=read_json(self.directory/'completion.json');value['result']['prestart']['transport']='docker-exec-exit0'
        atomic_json(self.directory/'completion.json',value)
        self.assertEqual(self.reconcile()['state'],'needs_attention')


if __name__=='__main__':unittest.main()

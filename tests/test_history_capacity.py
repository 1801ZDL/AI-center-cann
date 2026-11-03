"""Finite admission, exact replay, ACL pagination and offline policy recovery.

Offline context here is an explicit unit stand-in. Native installed acceptance
separately exercises real Root/service identities, SQLite VFS and SIGKILLs.
"""
import copy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_dependencies as builder
import history_capacity as h
from deployment import DeployError
from jobs import JobError,JobStore,Scheduler
from auth import AccessError
from maintenance import require_features


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DependencyTests();self.base.setUp();self.jobs=self.base.jobs;self.alice=self.base.alice;self.bob=self.base.bob
        self.binding={'fixture':'offline-unit-context'}
        @contextmanager
        def context(prefix):
            with self.jobs.transaction() as db:yield db,self.binding,{}
        def commit(db,meta):db.commit();db.execute('BEGIN IMMEDIATE')
        self.patches=[patch.object(h,'context',context),patch('cold_exports.commit',commit)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.base.tearDown()
    def enable(self,control=20,online=10):
        value=h.plan('unit-only',control,online);result=h.apply('unit-only',value);self.jobs.history=h.Capacity(self.jobs);return value,result
    def request(self,key='one',**changes):return self.base.request(idempotency_key=key,**changes)
    def submit(self,key='one',principal=None,**changes):return self.jobs.submit(self.request(key,**changes),principal or self.alice)
    def cancel(self,key='one',principal=None):
        p=principal or self.alice;job,_=self.submit(key,p);return self.jobs.cancel(job['id'],p)
    def usage(self):
        with self.jobs.connect() as db:return self.jobs.history.counts(db)
    def purge(self,job):
        self.jobs.policy['storage_trash_grace_seconds']=1
        def step(action):
            plan=self.jobs.lifecycle.plan(self.alice,{'kind':'archive','id':job['id'],'action':action})['plan']
            return self.jobs.lifecycle.apply(self.alice,plan)
        step('trash')
        with patch('storage_lifecycle.time.time',return_value=time.time()+10):return step('purge')
    def test_legacy_policy_is_unchanged_until_explicit_offline_enable(self):
        self.assertIsNone(self.jobs.history.policy);self.jobs.policy['max_jobs']=1;self.cancel()
        with self.assertRaisesRegex(JobError,'上限'):self.submit('two')
        self.assertEqual(self.jobs.history.status()['mode'],'legacy')
        self.enable(2,2);self.assertEqual(self.submit('two')[0]['state'],'queued')
    def test_policy_apply_is_idempotent_and_does_not_change_jobs_or_config(self):
        self.cancel();original=self.jobs.all();config=copy.deepcopy(self.base.config)
        plan,result=self.enable(3,1);self.assertFalse(result['reused'])
        self.assertTrue(h.apply('unit-only',plan)['reused']);self.assertEqual(self.jobs.all(),original);self.assertEqual(config,self.base.config)
        self.assertEqual(self.usage(),{'control_jobs':1,'online_archives':1})
    def test_warm_terminal_archives_hold_online_slots_and_replay_at_full_is_free(self):
        self.enable(3,1);job=self.cancel();before=self.usage()
        with self.assertRaisesRegex(JobError,'Online archive'):self.submit('two')
        replay,reused=self.submit();self.assertTrue(reused);self.assertEqual(replay['id'],job['id']);self.assertEqual(self.usage(),before)
        self.assertFalse(self.jobs.history.status()['history_deletion_enabled'])
    def test_confirmed_purge_releases_online_but_keeps_finite_control_history(self):
        self.enable(2,1);first=self.cancel();self.purge(first)
        self.assertEqual(self.usage(),{'control_jobs':1,'online_archives':0})
        second=self.cancel('two');self.purge(second)
        self.assertEqual(self.usage(),{'control_jobs':2,'online_archives':0})
        with self.assertRaisesRegex(JobError,'Control job history'):self.submit('three')
        self.assertEqual(self.submit()[0]['id'],first['id']);self.assertEqual(len(self.jobs.all()),2)
        h.Capacity(self.jobs)
    def test_trash_and_unconfirmed_directory_loss_do_not_release_online_slots(self):
        self.enable(3,1);job=self.cancel()
        plan=self.jobs.lifecycle.plan(self.alice,{'kind':'archive','id':job['id'],'action':'trash'})['plan']
        self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(self.usage()['online_archives'],1)
        with self.assertRaisesRegex(JobError,'Online archive'):self.submit('two')
    def test_partial_purge_keeps_slot_until_confirmed_complete(self):
        self.enable(3,1);job=self.cancel();directory=self.jobs.directory(job['id'])
        for n in range(300):(directory/('sample'+str(n))).write_bytes(b'x')
        first=self.purge(job);self.assertTrue(first['pending']);self.assertEqual(self.usage()['online_archives'],1)
        plan=self.jobs.lifecycle.plan(self.alice,{'kind':'archive','id':job['id'],'action':'resume'})['plan']
        self.jobs.lifecycle.apply(self.alice,plan);self.assertEqual(self.usage()['online_archives'],0)
    def test_purged_json_without_original_receipt_never_backfills_a_free_slot(self):
        self.enable(3,1);job=self.cancel()
        with self.jobs.transaction() as db:
            original=self.jobs.get(job['id'],db);original['archive_status']={'state':'purged','revision':4,'metadata_bytes':100}
            db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(original),job['id']))
        with self.assertRaisesRegex(JobError,'lifecycle'):h.Capacity(self.jobs)
        with self.assertRaisesRegex(JobError,'Online archive'):self.submit('two')
    def test_missing_projection_is_rejected_before_new_admission_or_restart(self):
        self.enable();self.cancel()
        with self.jobs.transaction() as db:db.execute('DELETE FROM history_capacity_slots')
        with self.assertRaisesRegex(JobError,'projection'):self.submit('two')
        with self.assertRaisesRegex(JobError,'projection'):h.Capacity(self.jobs)
    def test_two_concurrent_users_cannot_overbook_one_online_slot(self):
        self.enable(3,1);bob_input=self.base.input(self.bob)
        bodies=[(self.request('alice'),self.alice),(self.request('bob',parameters={**self.request()['parameters'],'input_id':bob_input['id']}),self.bob)]
        def submit(pair):
            try:return self.jobs.submit(*pair)[0]['id']
            except JobError as e:return e.status
        with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(submit,bodies))
        self.assertEqual(sum(isinstance(v,str) for v in values),1);self.assertIn(409,values);self.assertEqual(self.usage()['online_archives'],1)
    def test_raw_replay_after_workspace_move_keeps_original_id_and_private_receipt(self):
        self.enable(3,1);body=self.request(cwd=self.alice.workspace);job,_=self.jobs.submit(body,self.alice);self.jobs.cancel(job['id'],self.alice)
        source=Path(self.alice.workspace);moved=source.with_name('alice-relocated');source.rename(moved)
        principal=replace(self.alice,workspace=str(moved));before=self.usage()
        with patch.object(self.jobs,'normalize',side_effect=AssertionError('Exact accepted replay must resolve before workspace validation')):
            replay,reused=self.jobs.submit(dict(reversed(list(body.items()))),principal)
        self.assertTrue(reused);self.assertEqual(replay['id'],job['id']);self.assertEqual(self.usage(),before);self.assertNotIn('_request_identity',replay)
        changed={**body,'name':'changed'}
        with self.assertRaises(JobError):self.jobs.submit(changed,principal)
    def test_replay_never_crosses_experiment_bound_identity(self):
        self.enable();body=self.request();job,_=self.jobs.submit(body,self.alice)
        agent=replace(self.alice,experiment_id='experiment-not-this-job')
        with self.assertRaises(JobError) as e:self.jobs.submit(body,agent)
        self.assertEqual(e.exception.status,404);self.assertEqual(self.usage()['control_jobs'],1)
    def test_same_key_from_different_owner_never_returns_foreign_job(self):
        self.enable();first,_=self.submit();b=self.base.input(self.bob)
        body=self.request(parameters={**self.request()['parameters'],'input_id':b['id']})
        other,_=self.jobs.submit(body,self.bob);self.assertNotEqual(first['id'],other['id'])
    def test_replay_keeps_scope_and_global_admission_gates(self):
        self.enable();self.cancel()
        with self.assertRaises(AccessError):self.submit(principal=replace(self.alice,scopes=frozenset({'jobs:read'})))
        with patch('unit_start.admission_reason',return_value='maintenance'):
            with self.assertRaises(JobError) as e:self.submit()
        self.assertEqual(e.exception.status,503)
    def test_changed_normalized_equivalent_request_still_uses_original_fingerprint(self):
        self.enable();first,_=self.submit();other,reused=self.submit(name=first['name'])
        self.assertTrue(reused);self.assertEqual(first['id'],other['id'])
        with self.assertRaisesRegex(JobError,'幂等键'):self.submit(name='different')
    def test_capacity_plan_rejects_stale_history_and_invalid_limits(self):
        for values in [(0,0),(10001,1),(2,3),(True,1),(2,False)]:
            with self.assertRaises(JobError):h.plan('unit-only',*values)
        plan=h.plan('unit-only',2,2);self.cancel()
        with self.assertRaisesRegex(JobError,'changed after'):h.apply('unit-only',plan)
        with self.assertRaises(JobError):self.enable(1,0)
    def test_offline_transaction_interruption_recovers_original_plan(self):
        plan=h.plan('unit-only',2,1)
        for stage in ['before_commit','committed']:
            def stop(where):
                if where==stage:raise InterruptedError(stage)
            with patch.object(h,'_checkpoint',side_effect=stop):
                with self.assertRaises(InterruptedError):h.apply('unit-only',plan)
            with self.jobs.connect() as db:
                self.assertEqual(h.current(db) is not None,stage=='committed')
        self.assertTrue(h.apply('unit-only',plan)['reused'])
    def test_compatible_code_and_latest_snapshot_preserve_slots_receipts_and_results(self):
        self.enable();self.cancel();stage=self.base.root/'snapshot';stage.mkdir()
        with self.jobs.connect() as db,sqlite3.connect(stage/'jobs.sqlite3') as dest:db.backup(dest)
        h.require_history(self.jobs.runtime,stage)
        with self.assertRaisesRegex(DeployError,'downgrade'):require_features(self.jobs.runtime,'controller',{'files':{}})
        with sqlite3.connect(stage/'jobs.sqlite3') as db:
            data=json.loads(db.execute('SELECT data FROM jobs').fetchone()[0]);data['_request_identity']['request_sha256']='0'*64
            db.execute('UPDATE jobs SET data=?',(json.dumps(data),))
        with self.assertRaisesRegex(DeployError,'replay'):h.require_history(self.jobs.runtime,stage)
    def test_old_snapshot_cannot_drop_original_jobs_or_capacity_history(self):
        self.enable();stage=self.base.root/'snapshot';stage.mkdir()
        with self.jobs.connect() as db,sqlite3.connect(stage/'jobs.sqlite3') as dest:db.backup(dest)
        self.cancel()
        with self.assertRaises(DeployError):h.require_history(self.jobs.runtime,stage)
    def test_history_pages_and_summary_preserve_project_and_experiment_acl(self):
        self.enable(200,200)
        self.jobs.access.add_member('shared',self.bob.id,'viewer')
        first=self.cancel('shared');self.cancel('private')
        with self.jobs.transaction() as db:
            data=self.jobs.get(first['id'],db);data['project']='shared';self.jobs.write(db,data)
        page=self.jobs.history_page(self.bob);self.assertEqual(page['total'],1);self.assertEqual(page['jobs'][0]['id'],first['id'])
        with patch.object(self.jobs,'all',side_effect=AssertionError('History pages and dashboard must not decode all jobs')):
            self.assertEqual(self.jobs.summary(self.bob)['counts']['cancelled'],1)
            self.assertEqual(self.jobs.history_page(self.bob,1)['total'],1)
        self.jobs.access.remove_member('shared',self.bob.id)
        self.assertEqual(self.jobs.history_page(self.bob)['total'],0)
        bound=replace(self.alice,experiment_id='different');self.assertEqual(self.jobs.history_page(bound)['total'],0)
    def test_indexed_working_set_retains_unknown_attempts_and_detects_column_disagreement(self):
        self.enable();job,_=self.submit()
        with self.jobs.transaction() as db:
            value=self.jobs.get(job['id'],db);value['state']='needs_attention';self.jobs.write(db,value)
        with patch.object(self.jobs,'all',side_effect=AssertionError('Hot paths must not decode terminal history')):
            self.assertEqual(self.jobs.working()[0]['state'],'needs_attention')
            self.assertEqual(self.jobs.activity()['jobs'][0]['id'],job['id'])
        with self.jobs.transaction() as db:db.execute('UPDATE jobs SET state="cancelled" WHERE id=?',(job['id'],))
        with self.assertRaisesRegex(JobError,'disagree'):self.jobs.working()
    def test_working_query_uses_the_state_index(self):
        self.enable();self.cancel()
        with self.jobs.connect() as db:
            detail=' '.join(str(row) for row in db.execute('EXPLAIN QUERY PLAN SELECT data FROM jobs WHERE '+h.EXPR+' IN (?,?)',('queued','__invalid__')))
        self.assertIn('history_capacity_working',detail)
    def test_result_pages_decode_only_requested_acl_rows_and_keep_formula_escape(self):
        self.enable()
        for n in range(3):
            job=self.cancel(str(n))
            row={'job_id':job['id'],'owner':'alice','project':'personal:alice','kind':'ir','name':'=formula','state':'cancelled','passed':False,'metrics':{}}
            with self.jobs.transaction() as db:db.execute('INSERT INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row)))
        with patch.object(self.jobs,'all',side_effect=AssertionError('Result pages must not decode all history')):
            page=self.jobs.results.rows(self.alice,limit=1,offset=1);self.assertEqual(page['total'],3);self.assertEqual(len(page['rows']),1)
            self.assertEqual(self.jobs.results.rows(self.bob)['total'],0)
            self.assertIn("'=formula",self.jobs.results.csv(self.alice).decode())
    def test_atomic_graph_late_capacity_failure_rolls_back_all_slots_and_charges(self):
        self.enable(3,1)
        body={'idempotency_key':'capacity-graph','nodes':[{'key':'one','job':self.request('a')},{'key':'two','job':self.request('b')}]}
        for node in body['nodes']:node['job'].pop('idempotency_key')
        with self.assertRaisesRegex(JobError,'Online archive'):self.jobs.graphs.submit(body,self.alice)
        self.assertEqual(self.usage(),{'control_jobs':0,'online_archives':0})
        self.assertEqual(list(self.jobs.root.iterdir()),[])
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM storage_allocations WHERE kind="archive"').fetchone()[0],0)
    def test_completed_owner_turn_is_preserved_without_loading_terminal_job_bodies(self):
        self.enable();one,_=self.submit();self.submit('alice-two');bundle=self.base.input(self.bob)
        bob,_=self.jobs.submit(self.request('bob',parameters={**self.request()['parameters'],'input_id':bundle['id']}),self.bob)
        first=self.jobs.nodes.claim(self.base.node)['grant'];self.assertEqual(first['job_id'],one['id'])
        with self.jobs.transaction() as db:
            job=self.jobs.get(one['id'],db);job['state']='cancelled';self.jobs.write(db,job)
        with patch.object(self.jobs,'all',side_effect=AssertionError('Owner rotation must use its scalar history index')):
            next_job=self.jobs.nodes.claim(self.base.node)['grant']
        self.assertEqual(next_job['job_id'],bob['id'])
    def test_snapshot_supports_actual_service_identity_cursor_adapter(self):
        self.enable();self.cancel()
        from audit_retention import Database
        with self.jobs.connect() as db:
            snapshot,_=h.snapshot(Database(db,{'uid':os.getuid(),'gid':os.getgid()}))
        self.assertEqual(snapshot['control_jobs'],1)
    def test_csv_limit_accounts_for_actual_quoting_and_keeps_paged_access(self):
        self.enable();job=self.cancel()
        row={'job_id':job['id'],'owner':'alice','project':'personal:alice','kind':'ir','name':'quoted','state':'cancelled','passed':False,'metrics':{'text':'"'*300}}
        with self.jobs.transaction() as db:db.execute('INSERT INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row)))
        with patch('workflow_results.MAX_CSV_BYTES',1024):
            with self.assertRaisesRegex(JobError,'CSV export'):self.jobs.results.csv(self.alice)
        self.assertEqual(self.jobs.results.rows(self.alice)['total'],1)


if __name__=='__main__':unittest.main()

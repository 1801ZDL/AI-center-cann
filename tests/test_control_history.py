"""Private control snapshots retain exact source records and never grant rights.

Unit Root/context stand-ins are explicit; installed native service, numeric UID
denial and actual SIGKILLs are separate acceptance evidence.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_dependencies as builder
import control_history as h
from deployment import DeployError
from jobs import JobError
from maintenance import assert_quiescent,require_features


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DependencyTests();self.base.setUp();self.jobs=self.base.jobs
        self.prefix=self.base.root/'prefix';self.prefix.mkdir(mode=0o700)
        job,_=self.jobs.submit(self.base.request(idempotency_key='control-history-original'),self.base.alice)
        self.job=self.jobs.cancel(job['id'],self.base.alice);self.limit={'bytes':4*1024**2,'exports':8}
        self.binding={'prefix':str(self.prefix),'uid':os.getuid(),'gid':os.getgid(),'fixture':'unit-only'}
        @contextmanager
        def context(prefix):
            with self.jobs.transaction() as db:yield db,self.binding,{'state_dir':str(self.jobs.runtime),'uid':os.getuid(),'gid':os.getgid()}
        def commit(db,meta):db.commit();db.execute('BEGIN IMMEDIATE')
        self.patches=[patch.object(h,'context',context),patch.object(h,'ADMIN_UID',os.getuid()),patch('cold_exports.commit',commit)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.base.tearDown()
    def plan(self):return h.plan(self.prefix,[self.job['id']],self.limit)
    def step(self,plan,size=4*1024**2):return h.step(self.prefix,plan,size)
    def row(self):
        with self.jobs.connect() as db:return tuple(db.execute('SELECT id,owner,idem_key,fingerprint,state,data FROM jobs WHERE id=?',(self.job['id'],)).fetchone())
    def record(self,plan):
        with self.jobs.connect() as db:return h.rows(db)[plan['id']]
    def finish(self,plan):
        for _ in range(32):
            result=self.step(plan)
            if result['state']=='verified':return result
        self.fail('Original bounded export did not converge')
    def test_snapshot_preserves_raw_job_idempotency_result_and_private_fields(self):
        before=self.row();plan=self.plan();result=self.finish(plan);record=self.record(plan)[1]
        body,proof=h.checked(self.prefix,record)
        self.assertEqual(body['tables']['jobs']['columns'],['id','owner','idem_key','fingerprint','state','data'])
        self.assertIn(list(before),body['tables']['jobs']['rows'])
        self.assertEqual(self.row(),before);self.assertTrue(result['content_verified'])
        for key in ['normal_read_authority','control_capacity_released','online_capacity_released','authorization_restored']:
            self.assertFalse(result[key])
        with self.jobs.connect() as db:self.assertIsNone(db.execute('SELECT source FROM control_history_exports').fetchone()[0])
        self.assertEqual(proof['sha256'],plan['source']['sha256'])
        self.assertIn('_token',json.loads(body['tables']['jobs']['rows'][0][-1]))
    def test_confirmed_attempt_and_result_rows_are_copied_with_original_identity(self):
        with self.jobs.transaction() as db:
            db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',('attempt-fixture',self.job['id'],self.base.node,'cancelled','2026-11-03T00:00:00+00:00'))
            result={'job_id':self.job['id'],'owner':'alice','kind':'ir','passed':False,'metrics':{}}
            db.execute('INSERT INTO workflow_results VALUES(?,?)',(self.job['id'],json.dumps(result)))
        plan=self.plan();self.finish(plan);body,_=h.checked(self.prefix,self.record(plan)[1])
        self.assertEqual(body['tables']['execution_attempts']['rows'][0][0],'attempt-fixture')
        self.assertEqual(json.loads(body['tables']['workflow_results']['rows'][0][1]),result)
    def test_reference_dict_keys_and_revoked_grants_are_historical_not_reactivated(self):
        with self.jobs.transaction() as db:
            db.execute('CREATE TABLE extra_control_fixture(id TEXT,data TEXT)')
            # Use a real allowed reference table; no new authorization is issued.
            db.execute('INSERT INTO job_graphs VALUES(?,?,?,?,?,?,?)',('graph-fixture','alice','frozen','f'*64,'submitted',0,json.dumps({'original_nodes':{self.job['id']:{'revoked':True}}})))
        plan=self.plan();self.finish(plan);body,_=h.checked(self.prefix,self.record(plan)[1])
        self.assertEqual(body['tables']['job_graphs']['rows'][0][0],'graph-fixture')
        self.assertNotIn('extra_control_fixture',body['tables'])
    def test_live_unknown_or_changed_original_rows_refuse_first_copy(self):
        plan=self.plan()
        with self.jobs.transaction() as db:
            row=self.jobs.get(self.job['id'],db);row['reason']='changed after preview';self.jobs.write(db,row)
        with self.assertRaisesRegex(DeployError,'changed after'):self.step(plan)
        for state in ['queued','running','needs_attention']:
            with self.jobs.transaction() as db:
                row=self.jobs.get(self.job['id'],db);row['state']=state;db.execute('UPDATE jobs SET state=?,data=? WHERE id=?',(state,json.dumps(row),row['id']))
            with self.assertRaises(DeployError):self.plan()
    def test_small_steps_freeze_source_before_other_metadata_can_change(self):
        plan=self.plan();first=self.step(plan,1);self.assertEqual(first['state'],'copying')
        before=self.row()
        with self.jobs.transaction() as db:
            row=self.jobs.get(self.job['id'],db);row['reason']='newer metadata after source freeze';self.jobs.write(db,row)
        self.finish(plan);body,_=h.checked(self.prefix,self.record(plan)[1])
        self.assertIn(list(before),body['tables']['jobs']['rows']);self.assertNotEqual(self.row(),before)
    def test_same_plan_replay_does_not_duplicate_files_or_allocations(self):
        self.limit['exports']=1;plan=self.plan();self.finish(plan)
        self.assertEqual(self.plan(),plan);self.assertTrue(self.step(plan)['reused'])
        self.assertEqual([p.name for p in (self.prefix/h.DIRECTORY).iterdir()],[plan['id']+'.json'])
        with self.jobs.connect() as db:self.assertEqual(len(h.rows(db)),1)
    def test_each_commit_and_file_boundary_resumes_the_same_original_snapshot(self):
        plan=self.plan()
        for boundary in ['reserved','file_created','data_synced','progress_committed','sealed','published','verified']:
            def stop(where):
                if where==boundary:raise InterruptedError(boundary)
            with patch.object(h,'_checkpoint',side_effect=stop):
                with self.assertRaises(InterruptedError):self.step(plan)
        self.assertTrue(self.step(plan)['reused']);self.assertTrue(h.verify_copy(self.prefix,plan)['content_verified'])
        with self.jobs.connect() as db:self.assertEqual(len(h.rows(db)),1)
    def test_extra_unconfirmed_tail_is_removed_only_after_confirmed_prefix_matches(self):
        plan=self.plan();self.step(plan,10);partial=self.prefix/h.DIRECTORY/(plan['id']+'.partial')
        with partial.open('ab') as out:out.write(b'unconfirmed-tail')
        self.finish(plan);self.assertFalse(partial.exists())
        body,_=h.checked(self.prefix,self.record(plan)[1]);self.assertEqual(body['selection'],[self.job['id']])
    def test_changed_confirmed_prefix_is_preserved_and_never_truncated(self):
        plan=self.plan();self.step(plan,10);partial=self.prefix/h.DIRECTORY/(plan['id']+'.partial')
        with partial.open('r+b') as out:out.write(b'!');out.seek(0,os.SEEK_END);out.write(b'extra')
        before=partial.read_bytes()
        with self.assertRaisesRegex(DeployError,'no tail'):self.step(plan)
        self.assertEqual(partial.read_bytes(),before)
    def test_links_and_unknown_export_files_cannot_be_used_as_destinations(self):
        plan=self.plan();self.step(plan,1);path=self.prefix/h.DIRECTORY
        (path/'unknown').write_bytes(b'keep')
        with self.assertRaisesRegex(DeployError,'Untracked'):self.step(plan)
        self.assertEqual((path/'unknown').read_bytes(),b'keep')
    def test_corrupt_sealed_copy_never_verifies_or_becomes_normal_read_authority(self):
        plan=self.plan();self.finish(plan);target=self.prefix/h.DIRECTORY/(plan['id']+'.json')
        target.chmod(0o600);raw=target.read_bytes();target.write_bytes(b'!'+raw[1:]);target.chmod(0o440)
        with self.assertRaisesRegex(DeployError,'checksum'):h.verify_copy(self.prefix,plan)
        self.assertEqual(self.jobs.get(self.job['id'])['state'],'cancelled')
    def test_pending_export_blocks_backup_and_all_code_activation(self):
        plan=self.plan();self.step(plan,1)
        with self.assertRaisesRegex(DeployError,'control history'):assert_quiescent(self.jobs.runtime,'controller')
        with self.assertRaises(DeployError):h.require_stable_code(self.jobs.runtime)
        assert_quiescent(self.jobs.runtime,'controller',metadata_export=True)
    def test_budget_and_binding_are_finite_and_do_not_guess_new_authority(self):
        for value in [{'bytes':True,'exports':1},{'bytes':1024,'exports':1},{'bytes':2**30,'exports':129}]:
            with self.assertRaises(DeployError):h.plan(self.prefix,[self.job['id']],value)
        for ids in [[],[self.job['id'],self.job['id']],[[]],['../bad']]:
            with self.assertRaises((DeployError,JobError)):h.plan(self.prefix,ids,self.limit)
        plan=self.plan();self.binding={**self.binding,'fixture':'new binding'}
        with self.assertRaisesRegex(DeployError,'binding'):self.step(plan)
    def test_restore_preserves_original_proofs_freezes_write_and_keeps_historical_verification(self):
        plan=self.plan();self.finish(plan);staged=self.base.root/'restore';staged.mkdir()
        with self.assertRaises(DeployError):h.require_history(self.jobs.runtime,staged)
        with self.jobs.connect() as db,sqlite3.connect(staged/'jobs.sqlite3') as dest:db.backup(dest)
        h.require_history(self.jobs.runtime,staged);h.freeze_restored(self.jobs.runtime)
        self.assertEqual(self.record(plan)[0],'restore_frozen');self.binding={**self.binding,'fixture':'restored current binding'}
        with self.assertRaisesRegex(DeployError,'binding'):self.step(plan)
        self.assertTrue(h.verify_copy(self.prefix,plan)['content_verified'])
    def test_admin_status_never_exposes_snapshot_tokens_and_old_code_is_refused(self):
        plan=self.plan();self.finish(plan)
        status=h.status(self.jobs,self.jobs.access.local,plan['id']);self.assertTrue(status['content_verified'])
        private=self.jobs.get(self.job['id'])['_token'];self.assertNotIn(private,json.dumps(status))
        self.assertNotIn('selection',status)
        with self.assertRaises(JobError) as e:h.status(self.jobs,self.base.bob,plan['id'])
        self.assertEqual(e.exception.status,404)
        with self.assertRaisesRegex(DeployError,'downgrade'):require_features(self.jobs.runtime,'controller',{'files':{}})


if __name__=='__main__':unittest.main()

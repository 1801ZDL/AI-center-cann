"""Job-body migration keeps identity, current ACL and finite admission coherent.

Root context and UID are explicit unit stand-ins. Installed acceptance must
exercise actual service RO reads and SIGKILL before/after the SQL transaction.
"""
import copy,json,os,shutil,sqlite3,sys,time,unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_control_history as copies_fixture
import control_rotation as r
import control_history as h
import history_capacity as c
from jobs import JobError,JobStore
from deployment import DeployError
from maintenance import require_features

class RotationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=copies_fixture.ExportTests();self.fixture.setUp();self.jobs=self.fixture.jobs;self.base=self.fixture.base
        self.prefix=self.fixture.prefix
        self.patches=[patch.object(r,'context',h.context),patch.object(c,'context',h.context)]
        for p in self.patches:p.start()
        approval=c.plan(self.prefix,2,1);c.apply(self.prefix,approval);self.jobs.history=c.Capacity(self.jobs)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.fixture.tearDown()
    def request(self,key='new'):return self.base.request(idempotency_key=key)
    def purge(self):
        self.jobs.policy['storage_trash_grace_seconds']=1
        for action in ['trash','purge']:
            with patch('storage_lifecycle.time.time',return_value=time.time()+(10 if action=='purge' else 0)):
                plan=self.jobs.lifecycle.plan(self.base.alice,{'kind':'archive','id':self.fixture.job['id'],'action':action})['plan']
                result=self.jobs.lifecycle.apply(self.base.alice,plan)
        self.assertFalse(result.get('pending',False))
    def prepare(self):
        self.purge();self.original=self.jobs.get(self.fixture.job['id']);self.before=self.fixture.row()
        plan=self.fixture.plan();self.fixture.finish(plan);self.copy=plan
        return r.plan(self.prefix,plan['id'])
    def usage(self):
        with self.jobs.connect() as db:return self.jobs.history.counts(db)
    def test_migration_removes_full_body_credits_control_once_and_keeps_original_reads(self):
        plan=self.prepare();before=self.usage();self.assertEqual(before,{'control_jobs':1,'online_archives':0})
        result=r.apply(self.prefix,plan);self.assertEqual(result['control_slots_released'],1)
        self.assertEqual(self.usage(),{'control_jobs':0,'online_archives':0})
        row=self.fixture.row();self.assertEqual(row[:5],self.before[:5]);self.assertLess(len(row[5]),len(self.before[5]));self.assertNotIn('_token',json.loads(row[5]))
        self.assertEqual(self.jobs.get(self.original['id'],principal=self.base.alice),self.original)
        self.assertEqual(self.jobs.all(),[self.original]);self.assertTrue(r.apply(self.prefix,plan)['reused'])
        state=h.status(self.jobs,self.jobs.access.local,self.copy['id']);self.assertTrue(state['normal_read_authority']);self.assertEqual(state['migrated_job_count'],1)
        self.assertEqual(self.usage(),{'control_jobs':0,'online_archives':0});c.Capacity(self.jobs)
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_rotation_changes').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
            self.assertEqual(c.snapshot(db)[0]['control_jobs'],0)
    def test_same_key_and_request_still_resolve_original_id_without_new_directory(self):
        plan=self.prepare();r.apply(self.prefix,plan)
        request=self.base.request(idempotency_key='control-history-original')
        job,reused=self.jobs.submit(request,self.base.alice);self.assertTrue(reused);self.assertEqual(job['id'],self.original['id'])
        self.assertEqual(self.usage()['control_jobs'],0)
        with self.assertRaises(JobError):self.jobs.submit({**request,'name':'different'},self.base.alice)
        new,_=self.jobs.submit(self.request(),self.base.alice);self.assertNotEqual(new['id'],job['id']);self.assertEqual(self.usage(),{'control_jobs':1,'online_archives':1})
    def test_raw_request_replay_after_workspace_relocation_preserves_migrated_receipt(self):
        self.purge()
        request=self.request('raw-migrated');request['cwd']=self.base.alice.workspace
        submitted,_=self.jobs.submit(request,self.base.alice);self.fixture.job=self.jobs.cancel(submitted['id'],self.base.alice)
        plan=self.prepare();r.apply(self.prefix,plan)
        source=Path(self.base.alice.workspace);target=source.with_name('relocated-alice');source.rename(target)
        principal=replace(self.base.alice,workspace=str(target))
        with patch.object(self.jobs,'normalize',side_effect=AssertionError('Accepted raw replay must not resubmit')):
            replay,reused=self.jobs.submit(dict(reversed(list(request.items()))),principal)
        self.assertTrue(reused);self.assertEqual(replay['id'],self.original['id']);self.assertEqual(self.usage()['control_jobs'],1)

    def test_current_acl_is_checked_before_opening_missing_private_snapshot(self):
        plan=self.prepare();r.apply(self.prefix,plan)
        target=self.prefix/h.DIRECTORY/(self.copy['id']+'.json');target.unlink()
        with self.assertRaises(JobError) as e:self.jobs.get(self.original['id'],principal=self.base.bob)
        self.assertEqual(e.exception.status,404)
        with self.assertRaisesRegex(JobError,'unavailable'):self.jobs.get(self.original['id'],principal=self.base.alice)
        restricted=replace(self.base.alice,experiment_id='another-experiment')
        with self.jobs.connect() as db:
            with self.assertRaises(JobError) as denied:self.jobs.history.replay(db,{'idempotency_key':'control-history-original'},restricted)
        self.assertEqual(denied.exception.status,404)

        with self.assertRaises(JobError):c.Capacity(self.jobs)
    def test_current_project_and_experiment_acl_pagination_uses_compact_projection(self):
        plan=self.prepare();r.apply(self.prefix,plan)
        with self.jobs.connect() as db:
            page=self.jobs.history.page(db,self.base.alice,1,0,None);peer=self.jobs.history.page(db,self.base.bob,1,0,None)
            self.assertEqual(page['total'],1);self.assertEqual(page['jobs'][0]['id'],self.original['id']);self.assertEqual(peer['total'],0)
            rows,counts=self.jobs.history.dashboard(db,self.base.alice);self.assertEqual(rows,[self.original]);self.assertEqual(counts['cancelled'],1)
            self.assertEqual(self.jobs.history.working(db,{'queued','running'}),[])
    def test_warm_or_unconfirmed_originals_never_release_control_slots(self):
        plan=self.fixture.plan();self.fixture.finish(plan)
        with self.assertRaisesRegex(JobError,'removal'):r.plan(self.prefix,plan['id'])
        self.assertEqual(self.usage(),{'control_jobs':1,'online_archives':1});self.assertEqual(self.fixture.row()[-1],json.dumps(self.jobs.get(self.fixture.job['id']),ensure_ascii=False))
    def test_changed_snapshot_or_preview_binding_keeps_original_full_sql(self):
        plan=self.prepare()
        with self.jobs.transaction() as db:
            job=self.jobs.get(self.original['id'],db);job['reason']='newer';self.jobs.write(db,job)
        with self.assertRaisesRegex(JobError,'changed'):r.apply(self.prefix,plan)
        self.assertEqual(self.usage()['control_jobs'],1)
    def test_before_commit_rollback_and_after_commit_replay_preserve_same_plan(self):
        plan=self.prepare()
        def stop(stage):raise InterruptedError(stage)
        with patch.object(r,'_checkpoint',side_effect=stop):
            with self.assertRaises(InterruptedError):r.apply(self.prefix,plan)
        self.assertEqual(self.fixture.row(),self.before);self.assertEqual(self.usage()['control_jobs'],1)
        def after(stage):
            if stage=='committed':raise InterruptedError(stage)
        with patch.object(r,'_checkpoint',side_effect=after):
            with self.assertRaises(InterruptedError):r.apply(self.prefix,plan)
        self.assertEqual(self.usage()['control_jobs'],0);self.assertTrue(r.apply(self.prefix,plan)['reused'])
        self.assertEqual(self.jobs.get(self.original['id']),self.original)
    def test_corrupt_copy_never_returns_old_sql_fallback_or_creates_replacement(self):
        plan=self.prepare();r.apply(self.prefix,plan);target=self.prefix/h.DIRECTORY/(self.copy['id']+'.json')
        target.chmod(0o600);raw=target.read_bytes();target.write_bytes(raw[:-1]+b'!');target.chmod(0o440)
        with self.assertRaisesRegex(JobError,'unavailable'):self.jobs.get(self.original['id'])
        with self.assertRaises(JobError):self.jobs.submit(self.base.request(idempotency_key='control-history-original'),self.base.alice)
        self.assertEqual(self.usage()['control_jobs'],0)
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
    def test_latest_restore_retains_ledgers_and_can_relocate_archive_metadata_only(self):
        plan=self.prepare();r.apply(self.prefix,plan);staged=self.base.root/'staged';staged.mkdir()
        with self.jobs.connect() as db,sqlite3.connect(staged/'jobs.sqlite3') as dest:db.backup(dest)
        r.require_history(self.jobs.runtime,staged);c.require_history(self.jobs.runtime,staged)
        with sqlite3.connect(staged/'jobs.sqlite3') as db:
            job=json.loads(db.execute('SELECT data FROM jobs').fetchone()[0]);job['archive_dir']='/relocated/jobs/'+self.original['id']
            db.execute('UPDATE jobs SET data=?',(json.dumps(job),))
        r.require_history(self.jobs.runtime,staged);c.require_history(self.jobs.runtime,staged)
        h.freeze_restored(staged)
        with sqlite3.connect(staged/'jobs.sqlite3') as db:
            job=r.read(db,db.execute('SELECT data FROM jobs').fetchone()[0]);self.assertEqual(job['archive_dir'],'/relocated/jobs/'+self.original['id'])
        with sqlite3.connect(staged/'jobs.sqlite3') as db:db.execute('DELETE FROM control_rotation_jobs')
        with self.assertRaises((JobError,DeployError)):r.require_history(self.jobs.runtime,staged)
    def test_compact_acl_changes_and_missing_approval_are_rejected(self):
        plan=self.prepare();r.apply(self.prefix,plan)
        with self.jobs.transaction() as db:
            stub=json.loads(db.execute('SELECT data FROM jobs').fetchone()[0]);stub['project']='wrong';db.execute('UPDATE jobs SET data=?',(json.dumps(stub),))
        with self.assertRaisesRegex(JobError,'ACL'):self.jobs.get(self.original['id'])
    def test_migrated_terminal_execution_facts_cannot_be_rewritten_or_chained(self):
        plan=self.prepare();r.apply(self.prefix,plan)
        with self.jobs.transaction() as db:
            with self.assertRaisesRegex(JobError,'immutable'):self.jobs.write(db,self.original)
        with self.assertRaises(DeployError):self.fixture.plan()
        with self.assertRaisesRegex(DeployError,'downgrade'):require_features(self.jobs.runtime,'controller',{'files':{'control_history.py':{},'history_capacity.py':{}}})
    def test_original_attempt_results_graph_and_revoked_grants_stay_live(self):
        with self.jobs.transaction() as db:
            db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',('attempt-snapshot',self.fixture.job['id'],self.base.node,'cancelled','2026-11-03T00:00:00Z'))
            db.execute('INSERT INTO workflow_results VALUES(?,?)',(self.fixture.job['id'],json.dumps({'job_id':self.fixture.job['id'],'owner':'alice','kind':'ir','passed':False,'metrics':{}})))
        plan=self.prepare()
        with self.jobs.connect() as db:before={name:list(db.execute('SELECT * FROM '+name)) for name in ['execution_attempts','workflow_results','source_grants','job_graphs','storage_allocations']}
        r.apply(self.prefix,plan)
        with self.jobs.connect() as db:self.assertEqual(before,{name:list(db.execute('SELECT * FROM '+name)) for name in before})
        self.assertEqual(self.jobs.attempts(self.original['id'],self.base.alice)[0]['id'],'attempt-snapshot')

    def test_unused_rotation_does_not_add_legacy_startup_json_dependency(self):
        with self.jobs.connect() as db:
            self.assertFalse(r.exists(db))
            def unavailable(*args):raise sqlite3.OperationalError('Explicit fixture: JSON SQL function unavailable')
            db.create_function('json_type',2,unavailable)
            r.validate(db)

    def test_page_verifies_one_shared_snapshot_once_without_persistent_cache(self):
        self.purge();first=self.fixture.job['id']
        submitted,_=self.jobs.submit(self.request('second-copy-member'),self.base.alice)
        self.fixture.job=self.jobs.cancel(submitted['id'],self.base.alice);self.purge()
        copied=h.plan(self.prefix,[first,self.fixture.job['id']],self.fixture.limit);self.fixture.finish(copied)
        migration=r.plan(self.prefix,copied['id']);r.apply(self.prefix,migration)
        with patch.object(h,'checked',wraps=h.checked) as checked:
            with self.jobs.connect() as db:page=self.jobs.history.page(db,self.base.alice,100,0,None)
            self.assertEqual(page['total'],2);self.assertEqual(checked.call_count,1)
            with self.jobs.connect() as db:self.jobs.history.page(db,self.base.alice,100,0,None)
            self.assertEqual(checked.call_count,2)
    def test_migrated_measurement_facts_still_block_missing_feature_support(self):
        with self.jobs.transaction() as db:
            job=self.jobs.get(self.fixture.job['id'],db);job['_measurement_contract']={'explicit_unit_fixture':True};self.jobs.write(db,job)
        plan=self.prepare();r.apply(self.prefix,plan)
        from deployment import CORE
        manifest={'files':{name:{} for name in CORE if name!='npu_measurement.py'}}
        with self.assertRaisesRegex(DeployError,'measurement'):require_features(self.jobs.runtime,'controller',manifest)

if __name__=='__main__':unittest.main()

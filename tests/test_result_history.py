"""Small SQLite migration/reads; Root context/copy verifier replaced.

Disposable databases use memory journals and no synchronous flush: these tests
exercise SQL rollback/replay, never power-loss durability or installed Root IO.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import control_history as copies
import control_rotation as rotation
import history_capacity as capacity
import result_history as history
import maintenance_runner
from deployment import DeployError
from jobs import JobError
from workflow_results import ResultStore


class ResultHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-result-history-');self.root=Path(self.temp.name)
        self.state=self.root/'state';self.state.mkdir(mode=0o700);self.path=self.state/'jobs.sqlite3'
        self.binding={'prefix':str(self.root),'uid':os.getuid(),'gid':os.getgid(),'fixture':'mock-offline-context'};self.copy_state='verified'
        self.seed()
        @contextmanager
        def offline(prefix):
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                yield db,self.binding,{'uid':os.getuid(),'gid':os.getgid()}
        def commit(db,meta):db.commit();db.execute('BEGIN IMMEDIATE')
        self.checker=Mock(side_effect=lambda *_:(copy.deepcopy(self.body),{}))
        self.patches=[patch.object(history,'context',offline),
                      patch.object(copies,'rows',side_effect=lambda db:{self.export_id:(self.copy_state,self.copy_record)}),
                      patch.object(copies,'checked',self.checker),patch('cold_exports.commit',commit)]
        for item in self.patches:item.start()
        access=SimpleNamespace(projects=lambda p:[{'id':ident} for ident in p.projects])
        self.jobs=SimpleNamespace(connect=self.connect,access=access)
        self.jobs.history=object.__new__(capacity.Capacity);self.jobs.history.jobs=self.jobs;self.jobs.history.policy={'enabled':True}
        self.results=ResultStore(self.jobs)

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path)
        db.execute('PRAGMA synchronous=OFF');db.execute('PRAGMA journal_mode=MEMORY')
        try:
            with db:yield db
        finally:db.close()

    def seed(self,padding=1024):
        if self.path.exists():self.path.unlink()
        jobs=[];results=[]
        for index,owner in enumerate(['alice','bob'],1):
            ident='job-'+format(index,'012x')
            job={'id':ident,'owner':owner,'project':'team-lab' if owner=='alice' else 'personal:bob',
                 'state':'succeeded','archive_status':{'state':'purged'},'payload':'j'*1024,'resources':{'npu':1}}
            jobs.append([ident,owner,str(index),'fingerprint-'+str(index),'succeeded',json.dumps(job)])
            row={'schema':'ascend-lab-record/v1','job_id':ident,'owner':owner,'project':job['project'],
                 'kind':'performance' if owner=='alice' else 'accuracy','name':'original','state':'succeeded',
                 'passed':True,'metrics':{'mean_us':10+index},'recorded_at':'2026-11-09T00:00:00Z',
                 'diagnostic':'d'*(padding[index-1] if isinstance(padding,list) else padding)}
            results.append([ident,json.dumps(row)])
        self.original_results={key:raw for key,raw in results};self.ids=[r[0] for r in jobs]
        self.body={'format':copies.FORMAT,'selection':self.ids,'scope':'historical-control-copy-not-authorization',
                   'tables':{'jobs':{'columns':['id','owner','idem_key','fingerprint','state','data'],'rows':jobs},
                             'workflow_results':{'columns':['job_id','record'],'rows':results}}}
        raw=copies.encoded(self.body)
        self.copy_plan={'format':copies.FORMAT,'binding':self.binding,'budget':{'bytes':4*1024**2,'exports':8},
                        'selection':self.ids,'source':{'bytes':len(raw),'sha256':copies.sha(raw),'jobs':[]}}
        self.copy_plan['id']=copies.sha(copies.encoded(self.copy_plan));self.export_id=self.copy_plan['id']
        self.copy_record={'plan':self.copy_plan}
        indexes=[{'job_id':row[0],'export_id':self.export_id,'row_sha256':rotation.sha(row)} for row in jobs]
        rotation_plan={'format':rotation.FORMAT,'binding':self.binding,'export_id':self.export_id,'before':{},'indexes':indexes}
        rotation_plan['id']=rotation.sha(rotation_plan)
        with self.connect() as db:
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,idem_key TEXT,fingerprint TEXT,state TEXT,data TEXT)')
            db.execute('CREATE TABLE workflow_results(job_id TEXT PRIMARY KEY,record TEXT)')
            db.execute('CREATE TABLE execution_attempts(id TEXT,job_id TEXT,node_id TEXT,state TEXT,created_at TEXT)')
            rotation.initialize(db)
            for row,item in zip(jobs,indexes):
                reference={**item,'rotation_id':rotation_plan['id']}
                compact=rotation.compact(json.loads(row[-1]),{'format':rotation.FORMAT,**reference})
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(*row[:-1],json.dumps(compact)))
                db.execute('INSERT INTO control_rotation_jobs VALUES(?,?,?,?)',(row[0],self.export_id,rotation_plan['id'],rotation.encode(reference).decode()))
                db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',('attempt-'+row[0],row[0],'node-original','succeeded','2026-11-09T00:00:00Z'))
            db.execute('INSERT INTO control_rotation_changes VALUES(?,?)',(rotation_plan['id'],json.dumps({'plan':rotation_plan})))
            db.executemany('INSERT INTO workflow_results VALUES(?,?)',results)

    def principal(self,owner,*,projects=(),experiment=None,admin=False):
        return SimpleNamespace(id=owner,projects=list(projects),experiment_id=experiment,admin=admin,require=lambda scope:None)

    def plan(self):return history.plan(self.root,self.export_id)
    def migrate(self):
        value=self.plan();history.apply(self.root,value);return value

    def records(self):
        with self.connect() as db:return list(db.execute('SELECT rowid,job_id,record FROM workflow_results ORDER BY rowid'))

    def test_real_sql_migration_shrinks_bodies_and_preserves_ids_order_attempts(self):
        before=self.records()
        with self.connect() as db:attempts=list(db.execute('SELECT * FROM execution_attempts'))
        value=self.plan();self.assertEqual(len(value['rows']),2);result=history.apply(self.root,value)
        after=self.records();self.assertEqual([r[:2] for r in after],[r[:2] for r in before])
        self.assertLess(sum(len(r[2]) for r in after),sum(len(r[2]) for r in before))
        self.assertEqual(result['migrated_results'],2);self.assertFalse(result['quota_released'])
        with self.connect() as db:
            self.assertEqual(list(db.execute('SELECT * FROM execution_attempts')),attempts)
            restored=history.read_many(db,[(r[1],r[2]) for r in after])
        self.assertEqual(restored,[json.loads(r[2]) for r in before])

    def test_result_pages_kind_filters_and_csv_are_unchanged(self):
        owner=self.principal('alice');admin=self.principal('root-reader',admin=True)
        before=self.results.rows(owner);csv_before=self.results.csv(admin)
        self.migrate()
        self.assertEqual(self.results.rows(owner),before);self.assertEqual(self.results.csv(admin),csv_before)
        self.assertEqual(self.results.rows(owner,kind='performance')['total'],1)
        self.assertEqual(self.results.rows(owner,kind='accuracy')['rows'],[])
        page=self.results.rows(admin,limit=1,offset=1)
        self.assertEqual(page['rows'][0]['job_id'],self.ids[0]);self.assertEqual(page['total'],2)

    def test_acl_denial_and_kind_exclusion_precede_missing_private_copy_reads(self):
        self.migrate();self.checker.reset_mock();self.checker.side_effect=OSError('private copy missing')
        for principal,kind in [(self.principal('carol'),None),(self.principal('alice',experiment='another'),None),
                               (self.principal('alice'),'accuracy')]:
            self.assertEqual(self.results.rows(principal,kind=kind)['rows'],[])
        self.checker.assert_not_called()
        with self.assertRaisesRegex(JobError,'unavailable'):self.results.rows(self.principal('alice'))

    def test_current_project_membership_is_applied_after_migration(self):
        self.migrate();principal=self.principal('carol',projects=['team-lab'])
        self.assertEqual(self.results.rows(principal)['total'],1)
        principal.projects=[];self.checker.reset_mock()
        self.assertEqual(self.results.rows(principal)['total'],0);self.checker.assert_not_called()

    def test_one_page_verifies_a_shared_copy_once_and_next_request_verifies_again(self):
        self.migrate();self.checker.reset_mock()
        self.results.rows(self.principal('root-reader',admin=True));self.assertEqual(self.checker.call_count,1)
        self.results.rows(self.principal('root-reader',admin=True));self.assertEqual(self.checker.call_count,2)

    def test_changed_live_result_is_not_migrated_or_overwritten(self):
        value=self.plan()
        with self.connect() as db:db.execute('UPDATE workflow_results SET record=? WHERE job_id=?',(json.dumps({'changed':True}),self.ids[0]))
        before=self.records()
        with self.assertRaisesRegex(JobError,'changed'):history.apply(self.root,value)
        self.assertEqual(self.records(),before)

    def test_copy_failure_preserves_original_sql_results(self):
        before=self.records();self.checker.side_effect=OSError('original copy checksum differs')
        with self.assertRaisesRegex(JobError,'unavailable'):self.plan()
        self.assertEqual(self.records(),before)

    def test_restored_copy_is_readable_but_new_rotation_plans_are_rejected(self):
        self.migrate();self.copy_state='restore_frozen'
        self.assertEqual(self.results.rows(self.principal('alice'))['rows'][0]['metrics'],{'mean_us':11})
        with self.assertRaisesRegex(JobError,'read-only'):self.plan()

    def test_precommit_failure_rolls_back_and_same_approval_can_resume(self):
        value=self.plan();before=self.records()
        def stop(stage):
            if stage=='before_commit':raise InterruptedError('unit boundary before commit')
        with patch.object(history,'_checkpoint',side_effect=stop):
            with self.assertRaises(InterruptedError):history.apply(self.root,value)
        self.assertEqual(self.records(),before)
        self.assertEqual(history.apply(self.root,value)['migrated_results'],2)

    def test_commit_response_loss_replays_once_without_duplicate_history(self):
        value=self.plan()
        def stop(stage):
            if stage=='committed':raise InterruptedError('unit response lost after commit')
        with patch.object(history,'_checkpoint',side_effect=stop):
            with self.assertRaises(InterruptedError):history.apply(self.root,value)
        self.assertTrue(history.apply(self.root,value)['reused'])
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM result_history_changes').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM result_history_rows').fetchone()[0],2)

    def test_projection_tampering_and_lost_index_fail_instead_of_returning_stub(self):
        self.migrate();rows=self.records();changed=json.loads(rows[0][2]);changed['owner']='someone-else'
        with self.connect() as db:
            with self.assertRaisesRegex(JobError,'projection'):history.resolve(db,self.ids[0],json.dumps(changed))
            db.execute('DELETE FROM result_history_rows WHERE job_id=?',(self.ids[0],))
            with self.assertRaisesRegex(JobError,'reference'):history.resolve(db,self.ids[0],rows[0][2])

    def test_plain_record_cannot_replace_an_indexed_result(self):
        self.migrate()
        with self.connect() as db:
            with self.assertRaisesRegex(JobError,'lost its original'):history.resolve(db,self.ids[0],self.original_results[self.ids[0]])

    def test_replay_cannot_claim_completion_after_index_and_stub_were_lost(self):
        value=self.migrate()
        with self.connect() as db:
            db.execute('DELETE FROM result_history_rows WHERE job_id=?',(self.ids[0],))
            db.execute('UPDATE workflow_results SET record=? WHERE job_id=?',(self.original_results[self.ids[0]],self.ids[0]))
        with self.assertRaisesRegex(JobError,'index is missing'):history.apply(self.root,value)

    def test_normal_result_writers_reject_migrated_rows_before_file_operations(self):
        self.migrate()
        with self.connect() as db:
            for method,args in [(self.results.record,({'id':self.ids[0]},{})),
                                (self.results.unstarted_dependency,({'id':self.ids[0]},{}))]:
                with self.assertRaisesRegex(JobError,'immutable'):method(*args,db)

    def test_small_results_are_retained_and_empty_plans_create_no_history_tables(self):
        self.seed(padding=0);value=self.plan();self.assertEqual(value['rows'],[])
        before=self.records();self.assertEqual(history.apply(self.root,value)['migrated_results'],0)
        self.assertEqual(self.records(),before)
        with self.connect() as db:self.assertFalse(history.exists(db))

    def test_already_migrated_rows_do_not_allocate_a_second_copy_or_approval(self):
        self.migrate();value=self.plan();self.assertEqual(value['rows'],[])
        history.apply(self.root,value)
        with self.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM result_history_changes').fetchone()[0],1)

    def test_finite_index_and_approval_budgets_preserve_sql(self):
        value=self.plan();before=self.records()
        with self.connect() as db:history.initialize(db)
        with patch.object(history,'MAX_ROWS',1):
            with self.assertRaisesRegex(JobError,'index is full'):history.apply(self.root,value)
        with self.connect() as db:db.execute('INSERT INTO result_history_changes VALUES(?,?)',('old','{}'))
        with patch.object(history,'MAX_CHANGES',1):
            with self.assertRaisesRegex(JobError,'approvals are full'):history.apply(self.root,value)
        self.assertEqual(self.records(),before)

    def test_large_original_copy_and_record_respect_low_memory_bounds(self):
        self.copy_plan['source']['bytes']=history.MAX_TOTAL+1;self.checker.reset_mock()
        with self.assertRaisesRegex(JobError,'256KiB'):self.plan()
        self.checker.assert_not_called()
        self.seed(padding=[history.MAX_RECORD+1,1024])
        with self.assertRaisesRegex(JobError,'128KiB'):self.plan()

    def test_downgrade_is_refused_even_if_index_table_was_lost(self):
        self.migrate()
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):history.require_compatible(self.state,'controller',{'files':{}})
        with self.connect() as db:db.execute('DROP TABLE result_history_rows')
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):history.require_compatible(self.state,'controller',{'files':{}})
        with self.connect() as db:db.executemany('UPDATE workflow_results SET record=? WHERE job_id=?',[(raw,ident) for ident,raw in self.original_results.items()])
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):history.require_compatible(self.state,'controller',{'files':{}})

    def test_restore_requires_original_projections_indexes_and_readable_copies(self):
        self.migrate();staged=self.root/'staged';staged.mkdir()
        with self.connect() as old,sqlite3.connect(staged/'jobs.sqlite3') as new:old.backup(new)
        history.require_history(self.state,staged)
        with sqlite3.connect(staged/'jobs.sqlite3') as new:new.execute('UPDATE workflow_results SET record=? WHERE job_id=?',('{}',self.ids[0]))
        with self.assertRaisesRegex(DeployError,'projection'):history.require_history(self.state,staged)

    def test_restore_rejects_missing_receipts_or_copy_even_when_stubs_match(self):
        self.migrate();staged=self.root/'staged';staged.mkdir()
        with self.connect() as old,sqlite3.connect(staged/'jobs.sqlite3') as new:old.backup(new)
        self.checker.side_effect=OSError('copy unavailable')
        with self.assertRaisesRegex(JobError,'unavailable'):history.require_history(self.state,staged)
        self.checker.side_effect=lambda *_:(copy.deepcopy(self.body),{})
        with sqlite3.connect(staged/'jobs.sqlite3') as new:new.execute('DELETE FROM result_history_changes')
        with self.assertRaisesRegex(DeployError,'history'):history.require_history(self.state,staged)

    def test_batch_limit_and_original_job_binding_fail_closed(self):
        self.migrate();row=self.records()[0]
        with self.connect() as db:
            with self.assertRaisesRegex(JobError,'200'):history.read_many(db,[(row[1],row[2])]*201)
            db.execute('DELETE FROM control_rotation_jobs WHERE job_id=?',(self.ids[0],))
            with self.assertRaisesRegex(JobError,'job binding'):history.resolve(db,row[1],row[2])

    def test_maintenance_prepare_and_apply_reuse_original_result_plan_after_response_loss(self):
        original=maintenance_runner.plan(self.root,[{'action':'result_rotate','plan':self.copy_plan}])
        journal=self.root/'maintenance.json'
        self.assertTrue(maintenance_runner.run_once(original,journal,apply=True)['prepared'])
        def stop(stage):
            if stage=='committed':raise InterruptedError('after result commit')
        with patch.object(history,'_checkpoint',side_effect=stop):
            with self.assertRaises(InterruptedError):maintenance_runner.run_once(original,journal,apply=True)
        with patch.object(history,'plan',side_effect=AssertionError('Do not replan after commit')):
            self.assertEqual(maintenance_runner.run_once(original,journal,apply=True)['status'],'complete')

    def test_real_small_copy_verifier_and_tamper_detection_under_current_uid(self):
        directory=self.root/copies.DIRECTORY;directory.mkdir();directory.chmod(0o750)
        path=directory/(self.export_id+'.json');path.write_bytes(copies.encoded(self.body));path.chmod(0o440)
        info=directory.stat();self.copy_record['directory']=[info.st_dev,info.st_ino]
        info=path.stat();self.copy_record['file_identity']=[info.st_dev,info.st_ino]
        with patch.object(copies,'checked',copies._checked),patch.object(copies,'ADMIN_UID',os.getuid()):
            self.migrate()
            self.assertEqual(self.results.rows(self.principal('alice'))['rows'][0]['metrics'],{'mean_us':11})
            before=self.records();raw=path.read_bytes();path.chmod(0o600)
            path.write_bytes(b'!'+raw[1:]);path.chmod(0o440)
            with self.assertRaisesRegex(JobError,'unavailable'):self.results.rows(self.principal('alice'))
            self.assertEqual(self.records(),before)


if __name__=='__main__':unittest.main()

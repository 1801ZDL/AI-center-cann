"""Graph restore/admission invariants using tiny disposable SQLite databases.

No service, process, transport or device. Memory journals and synchronous=OFF
apply only to fixtures; these checks do not establish power-loss durability.
"""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import job_graphs as graphs
from jobs import JobError
from deployment import DeployError


class GraphHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-graph-history-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.old=self.root/'current';self.new=self.root/'staged'
        self.old.mkdir();self.new.mkdir()
        self.gid='graph-'+'a'*24;self.ids=['job-'+format(i,'012x') for i in (1,2)]
        self.graph={'id':self.gid,'owner':'alice','name':'build then accuracy','created_at':'2026-11-09T00:00:00Z',
                    'state':'submitted','experiment_id':None,'jobs':dict(zip(['build','accuracy'],self.ids)),
                    'preparation_failures':0,'retry_after':0,'prepared_at':'2026-11-09T00:00:01Z','reason':None}
        with self.db(self.old) as db:
            db.execute('CREATE TABLE job_graphs(id TEXT PRIMARY KEY,owner TEXT,idem_key TEXT,fingerprint TEXT,state TEXT,retry_after REAL,record TEXT)')
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,idem_key TEXT,fingerprint TEXT,state TEXT,data TEXT)')
            db.execute('CREATE TABLE execution_attempts(id TEXT PRIMARY KEY,job_id TEXT,node_id TEXT,state TEXT,created_at TEXT)')
            db.execute('INSERT INTO job_graphs VALUES(?,?,?,?,?,?,?)',self.row())
            for i,(label,identifier) in enumerate(self.graph['jobs'].items()):
                state='succeeded' if i==0 else 'queued'
                job={'id':identifier,'owner':'alice','state':state,'experiment_id':None,'_graph_id':self.gid,
                     'submission_graph':{'id':self.gid,'node':label},'source':{'sha256':'f'*64},
                     'resources':{'cpu':1,'memory_mib':128,'npu':0},'archive_dir':'/fixture/original',
                     'depends_on':[] if i==0 else [self.ids[0]]}
                if i==0:job.update(attempt_id='attempt-original',node_id='node-original',result={'exit_code':0})
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(identifier,'alice',label,'f'*64,state,json.dumps(job)))
                db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',
                           ('attempt-original' if i==0 else 'attempt-deferred',identifier,'node-original',
                            'succeeded' if i==0 else 'prestart_deferred','2026-11-09T00:00:00Z'))
        self.reset_stage()
        self.api=object.__new__(graphs.Graphs)

    @contextmanager
    def db(self,directory):
        db=sqlite3.connect(directory/'jobs.sqlite3')
        db.execute('PRAGMA synchronous=OFF');db.execute('PRAGMA journal_mode=MEMORY')
        try:
            with db:yield db
        finally:db.close()

    def row(self):return (self.gid,'alice','original-key','a'*64,self.graph['state'],self.graph['retry_after'],json.dumps(self.graph))

    def reset_stage(self):
        with self.db(self.old) as source,self.db(self.new) as target:source.backup(target)

    def change_graph(self,updates,*,directory=None,sql=True):
        with self.db(directory or self.new) as db:
            value=json.loads(db.execute('SELECT record FROM job_graphs').fetchone()[0]);value.update(updates)
            db.execute('UPDATE job_graphs SET record=?',(json.dumps(value),))
            if sql:db.execute('UPDATE job_graphs SET state=?,retry_after=?',(value['state'],value['retry_after']))

    def change_job(self,updates,*,index=0,directory=None):
        with self.db(directory or self.new) as db:
            value=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(self.ids[index],)).fetchone()[0]);value.update(updates)
            db.execute('UPDATE jobs SET state=?,data=? WHERE id=?',(value['state'],json.dumps(value),self.ids[index]))

    def restore(self):graphs.require_restore_history(self.old,self.new,'controller')

    def test_unchanged_graph_and_deferred_attempt_survive(self):self.restore()

    def test_restore_preserves_every_graph_metadata_field(self):
        for updates in [{'name':'changed'},{'created_at':'2026-11-08T00:00:00Z'},{'prepared_at':'2026-11-08T00:00:00Z'},
                        {'experiment_id':'experiment-'+'b'*24},{'preparation_failures':3},
                        {'retry_after':5},{'reason':'new reason'},{'extension':{'generation':1}}]:
            with self.subTest(updates=updates):
                self.reset_stage();self.change_graph(updates)
                with self.assertRaises(DeployError):self.restore()

    def test_preserve_job_source_resources_dependencies_and_result(self):
        for updates in [{'source':{'sha256':'0'*64}},{'resources':{'cpu':2}},
                        {'depends_on':['job-'+'9'*12]},{'result':{'exit_code':1}},{'extra_input':True}]:
            with self.subTest(updates=updates):
                self.reset_stage();self.change_job(updates)
                with self.assertRaisesRegex(DeployError,'original attempts'):self.restore()

    def test_archive_relocation_overlays_remain_supported(self):
        self.change_job({'archive_dir':'/fixture/relocated','archive_status':{'state':'restored'}});self.restore()

    def test_graph_missing_rejected(self):
        with self.db(self.new) as db:db.execute('DELETE FROM job_graphs')
        with self.assertRaisesRegex(DeployError,'graph admission'):self.restore()

    def test_job_and_attempt_missing_rejected(self):
        for table in ['jobs','execution_attempts']:
            self.reset_stage()
            with self.db(self.new) as db:db.execute('DELETE FROM '+table)
            with self.subTest(table=table),self.assertRaises(DeployError):self.restore()

    def test_attempt_node_state_and_timestamp_changes_rejected(self):
        for field,value in [('node_id','other-node'),('state','failed'),('created_at','changed')]:
            self.reset_stage()
            with self.db(self.new) as db:db.execute('UPDATE execution_attempts SET '+field+'=? WHERE id=?',(value,'attempt-original'))
            with self.subTest(field=field),self.assertRaises(DeployError):self.restore()

    def test_dispatch_holds_wrong_graph_node_experiment_or_orphan_payload(self):
        with self.db(self.old) as db:
            job=json.loads(db.execute('SELECT data FROM jobs LIMIT 1').fetchone()[0])
            self.assertIsNone(self.api.reason(job,db))
            variants=[{'_graph_id':None},{'submission_graph':None},{'submission_graph':{'id':'graph-'+'b'*24,'node':'build'}},
                      {'submission_graph':{'id':self.gid,'node':[]}},{'experiment_id':'experiment-'+'b'*24},
                      {'owner':'bob'},{'_graph_payload':{}},{'id':self.ids[1]}]
            for updates in variants:
                with self.subTest(updates=updates):self.assertIsNotNone(self.api.reason({**job,**updates},db))
            self.assertIsNone(self.api.reason({'id':'ordinary-job'},db))

    def test_sql_json_dispatch_state_mismatch_blocks_loading_and_dispatch(self):
        self.change_graph({'state':'preparing'},directory=self.old,sql=False)
        with self.db(self.old) as db:
            with self.assertRaises(JobError):self.api.load(self.gid,db)
            job=json.loads(db.execute('SELECT data FROM jobs LIMIT 1').fetchone()[0])
            self.assertIsNotNone(self.api.reason(job,db))

    def test_oversized_graph_is_not_returned_from_sqlite(self):
        self.change_graph({'padding':'x'*65536},directory=self.old)
        with self.db(self.old) as db:
            self.assertIsNone(db.execute('SELECT '+graphs.graph_columns()+' FROM job_graphs').fetchone()[-1])
            with self.assertRaises(JobError):self.api.load(self.gid,db)

    def test_malformed_job_json_is_sanitized_and_state_retained(self):
        for raw in ['[]','null','{invalid']:
            self.reset_stage()
            with self.db(self.new) as db:db.execute('UPDATE jobs SET data=?',(raw,))
            with self.subTest(raw=raw),self.assertRaises(DeployError):self.restore()

    def test_current_attempt_must_match_job_even_if_both_snapshots_agree(self):
        for updates in [{'attempt_id':'attempt-missing'},{'node_id':'wrong-node'},{'state':'failed'}]:
            with self.subTest(updates=updates):
                with self.db(self.old) as db:original=db.execute('SELECT state,data FROM jobs WHERE id=?',(self.ids[0],)).fetchone()
                self.change_job(updates,directory=self.old);self.reset_stage()
                with self.assertRaises(DeployError):self.restore()
                with self.db(self.old) as db:db.execute('UPDATE jobs SET state=?,data=? WHERE id=?',(*original,self.ids[0]))

    def test_owner_acl_precedes_json_decode(self):
        with self.db(self.old) as db,patch.object(graphs,'decode_graph',side_effect=AssertionError('Private JSON decoded')):
            with self.assertRaises(JobError) as error:self.api.load(self.gid,db,SimpleNamespace(id='bob',experiment_id=None))
            self.assertEqual(error.exception.status,404)

    def test_bound_experiment_acl_remains_enforced(self):
        with self.db(self.old) as db:
            with self.assertRaises(JobError) as error:self.api.load(self.gid,db,SimpleNamespace(id='alice',experiment_id='experiment-'+'b'*24))
            self.assertEqual(error.exception.status,404)

    def test_preparing_and_needs_attention_never_dispatch(self):
        for state in ['preparing','needs_attention']:
            self.change_graph({'state':state},directory=self.old)
            with self.db(self.old) as db:
                job=json.loads(db.execute('SELECT data FROM jobs LIMIT 1').fetchone()[0]);self.assertIsNotNone(self.api.reason(job,db))

    def test_save_preserves_graph_identity_and_published_state(self):
        with self.db(self.old) as db:
            for updates in [{'name':'changed'},{'owner':'bob'},{'jobs':{'build':self.ids[0]}},
                            {'experiment_id':'experiment-'+'b'*24},{'state':'preparing'}]:
                value=copy.deepcopy(self.graph);value.update(updates)
                with self.subTest(updates=updates),self.assertRaises(JobError):self.api.save(db,value)
            self.assertEqual(self.api.load(self.gid,db),self.graph)

    def test_manual_preparation_retries_above_automatic_limit_are_valid(self):
        self.change_graph({'state':'needs_attention','preparation_failures':100},directory=self.old)
        with self.db(self.old) as db:
            value=self.api.load(self.gid,db);value.update(state='preparing',retry_after=5,preparation_failures=101)
            self.api.save(db,value);self.assertEqual(self.api.load(self.gid,db)['preparation_failures'],101)

    def test_nonfinite_retry_wrong_shape_and_oversized_records_rejected(self):
        for updates in [{'retry_after':float('nan')},{'retry_after':True},{'jobs':[]},
                        {'jobs':{'build':self.ids[0],'accuracy':self.ids[0]}},{'padding':'x'*65536},
                        {'preparation_failures':True},{'experiment_id':'other'},{'reason':['invalid']}]:
            value=copy.deepcopy(self.graph);value.update(updates)
            row=(*self.row()[:4],value['state'],value['retry_after'],json.dumps(value))
            with self.subTest(updates=list(updates)),self.assertRaises(JobError):graphs.decode_graph(row)

    def test_fresh_restore_still_audits_stage_bindings(self):
        empty=self.root/'empty';empty.mkdir()
        graphs.require_restore_history(empty,self.new,'controller')
        self.change_job({'submission_graph':{'id':self.gid,'node':'accuracy'}})
        with self.assertRaises(DeployError):graphs.require_restore_history(empty,self.new,'controller')

    def test_new_graphs_allowed_only_with_valid_stages(self):
        with self.db(self.old) as db:db.execute('DELETE FROM job_graphs')
        self.restore()
        self.change_job({'experiment_id':'experiment-'+'b'*24})
        with self.assertRaises(DeployError):self.restore()

    def test_orphan_active_attempt_blocks_even_identical_snapshots(self):
        with self.db(self.old) as db:db.execute("UPDATE execution_attempts SET state='reserved' WHERE id='attempt-deferred'")
        self.reset_stage()
        with self.assertRaises(DeployError):self.restore()

    def test_preparation_payload_is_preserved_and_cannot_be_published_early(self):
        for directory in [self.old,self.new]:
            self.change_graph({'state':'preparing'},directory=directory)
            self.change_job({'_graph_payload':{'request':{'frozen':'original'},'source':None}},index=1,directory=directory)
        self.restore()
        self.change_job({'_graph_payload':{'request':{'frozen':'changed'},'source':None}},index=1)
        with self.assertRaises(DeployError):self.restore()
        self.reset_stage();self.change_graph({'state':'submitted'},directory=self.old);self.reset_stage()
        with self.assertRaises(DeployError):self.restore()

    def test_cancelled_before_start_without_current_attempt_is_valid(self):
        for directory in [self.old,self.new]:self.change_job({'state':'cancelled'},index=1,directory=directory)
        self.restore()

    def test_audit_bounds_fail_closed(self):
        for field,limit in [('MAX_RESTORE_BYTES',1),('MAX_GRAPHS',0),('MAX_RESTORE_ATTEMPTS',0),('MAX_JOB_BYTES',1)]:
            with self.subTest(field=field),patch.object(graphs,field,limit),self.assertRaises(DeployError):self.restore()
        # A few real SQL VM steps, with a deliberately tiny instruction budget.
        with patch.object(graphs,'MAX_RESTORE_SQL_STEPS',0):
            with self.db(self.old) as db:
                db.executemany('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',
                    [('unrelated-'+str(i),'job-'+'9'*12,'node','succeeded','time') for i in range(150)])
            with self.assertRaises(DeployError):self.restore()

    def test_readonly_restore_and_non_controller_do_not_mutate_state(self):
        with self.db(self.old) as db:before=list(db.iterdump())
        self.restore();graphs.require_restore_history(self.old,self.new,'worker')
        with self.db(self.old) as db:self.assertEqual(before,list(db.iterdump()))

    def test_materialize_rejects_stage_binding_before_any_file_access(self):
        self.change_graph({'state':'preparing'},directory=self.old)
        self.change_job({'state':'queued','attempt_id':None,'_graph_payload':{'request':{},'source':None},
                         'submission_graph':{'id':self.gid,'node':'accuracy'}},directory=self.old)
        def get(identifier,db):return json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(identifier,)).fetchone()[0])
        self.api.jobs=SimpleNamespace(transaction=lambda:self.db(self.old),get=get,
                                      directory=Mock(side_effect=AssertionError('Unexpected payload IO')))
        self.api.materialize(self.gid,force=True)
        self.api.jobs.directory.assert_not_called()
        with self.db(self.old) as db:
            value=self.api.load(self.gid,db);self.assertEqual(value['state'],'preparing');self.assertEqual(value['preparation_failures'],1)

    def test_migrated_terminal_stages_reuse_one_copy_per_database_and_reverify(self):
        import control_history as copies
        import control_rotation as rotation
        self.change_job({'state':'succeeded'},index=1,directory=self.old)
        with self.db(self.old) as db:
            original=[list(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]
            body={'tables':{'jobs':{'columns':['id','owner','idem_key','fingerprint','state','data'],'rows':original}}}
            export='c'*64
            indexes=[{'job_id':r[0],'export_id':export,'row_sha256':rotation.sha(r)} for r in original]
            plan={'format':rotation.FORMAT,'binding':{'prefix':str(self.root)},'export_id':export,'before':{},'indexes':indexes}
            plan['id']=rotation.sha(plan);rotation.initialize(db)
            for row,item in zip(original,indexes):
                reference={**item,'rotation_id':plan['id']}
                stub=rotation.compact(json.loads(row[-1]),{'format':rotation.FORMAT,**reference})
                db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(stub),row[0]))
                db.execute('INSERT INTO control_rotation_jobs VALUES(?,?,?,?)',(row[0],export,plan['id'],json.dumps(reference)))
            db.execute('INSERT INTO control_rotation_changes VALUES(?,?)',(plan['id'],json.dumps({'plan':plan})))
        self.reset_stage();record={'plan':{'binding':{'prefix':str(self.root)}}}
        with patch.object(copies,'rows',return_value={export:('verified',record)}), \
             patch.object(copies,'checked',side_effect=lambda *_:(copy.deepcopy(body),{})) as checker:
            self.restore();self.assertEqual(checker.call_count,2)
            self.restore();self.assertEqual(checker.call_count,4)  # No cross-restore cache.
            body['tables']['jobs']['rows'][0][-1]=json.dumps({'id':self.ids[0],'owner':'alice','state':'succeeded'})
            with self.assertRaises(DeployError):self.restore()
        with patch.object(copies,'rows',return_value={}):
            with self.assertRaises(DeployError):self.restore()

    def test_original_jobstore_atomic_submission_and_preparation_recovery(self):
        # Two selected existing integration checks: actual JobStore/Node ticket
        # logic, no HTTP server, threads, executor or container start.
        import test_job_graphs as original
        names=['test_one_transaction_maps_local_edges_and_preserves_ordinary_queue',
               'test_after_commit_reopen_resumes_same_ids_and_never_starts_partial_graph']
        # Legacy fixtures assume a flat test tree. Their own TemporaryDirectory
        # stays under the authorized repository runtime, never src/.../runtime.
        import os
        repository=Path(os.environ.get('ASCEND_LAB_REPOSITORY_ROOT',Path(__file__).resolve().parents[1]))
        with patch.object(original.builder,'ROOT',repository):
            for name in names:
                case=original.GraphTests(name);case.setUp()
                try:
                    with self.subTest(name=name):getattr(case,name)()
                finally:case.tearDown()


if __name__=='__main__':unittest.main()

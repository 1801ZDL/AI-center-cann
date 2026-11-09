"""Small SQLite selection/approval checks, with offline Root context mocked."""
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import control_history as copies
import history_capacity as capacity
import maintenance_policy as retention
import maintenance_runner as upkeep

NOW=dt.datetime(2026,11,9,2,30,tzinfo=dt.timezone.utc).timestamp()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,idem_key TEXT,fingerprint TEXT,state TEXT,data TEXT);
            CREATE TABLE storage_lifecycle(kind TEXT,id TEXT,owner TEXT,state TEXT,revision INTEGER,record TEXT);
            CREATE TABLE storage_allocations(kind TEXT,id TEXT,owner TEXT,bytes INTEGER,settled INTEGER);
        ''')
        capacity.initialize(self.db)
        settings={'format':capacity.FORMAT,'revision':1,'approval_id':'a'*64,'max_control_jobs':100,'max_online_archives':100}
        self.db.execute('INSERT INTO history_capacity_policy VALUES(1,?)',(json.dumps(settings),))
        self.settings=retention.policy({'enabled':True,'control_high_watermark':1,'target_control_jobs':0,
                                        'keep_recent_per_owner':0,'min_age_seconds':3600})

    def tearDown(self):self.db.close()

    def add(self,index,*,owner='alice',state='succeeded',age=86400,purged=True,payload=0):
        ident='job-'+format(index,'012x')
        job={'id':ident,'owner':owner,'state':state,
             'finished_at':dt.datetime.fromtimestamp(NOW-age,dt.timezone.utc).isoformat(),
             'archive_status':{'state':'purged','revision':1,'metadata_bytes':10} if purged else {'state':'available'},
             'payload':'x'*payload}
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(ident,owner,str(index),'f'*64,state,json.dumps(job)))
        if purged:
            receipt={'state':'purged','kind':'archive','id':ident,'owner':owner,'revision':1,
                     'inventory':None,'completed_at':job['finished_at'],'metadata_bytes':10}
            self.db.execute('INSERT INTO storage_lifecycle VALUES(?,?,?,?,?,?)',('archive',ident,owner,'purged',1,json.dumps(receipt)))
            self.db.execute('INSERT INTO storage_allocations VALUES(?,?,?,?,?)',('archive',ident,owner,10,1))
        proof=capacity.online_proof(self.db,job)
        self.db.execute('INSERT INTO history_capacity_slots VALUES(?,?,?)',(ident,int(not purged),capacity.encode(proof).decode()))
        return ident

    def select(self,**kwargs):
        return retention.select(self.db,self.settings,epoch=NOW,**kwargs)

    def change_job(self,ident,**fields):
        raw=self.db.execute('SELECT data FROM jobs WHERE id=?',(ident,)).fetchone()[0]
        job=json.loads(raw);job.update(fields)
        self.db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(job),ident))

    def test_disabled_and_outside_window_do_not_open_root_context(self):
        with patch.object(copies,'context',side_effect=AssertionError('No database expected')):
            self.assertEqual(retention.plan('/unused',None,epoch=NOW)['status'],'disabled')
            self.assertEqual(retention.plan('/unused',self.settings,epoch=NOW+3600)['status'],'outside_selection_window')

    def test_strict_bounds_and_mutable_defaults_are_independent(self):
        for value in [{'enabled':1},{'scan_rows':257},{'max_jobs':True},{'keep_recent_per_owner':-1},
                      {'hold_job_ids':[{}]},{'hold_job_ids':['job-000000000001']*2},
                      {'target_control_jobs':1000},{'archive_budget':{'bytes':0,'exports':1}},{'unknown':1}]:
            with self.assertRaises(ValueError):retention.policy(value)
        first=retention.policy();first['hold_job_ids'].append('anything')
        self.assertEqual(retention.policy()['hold_job_ids'],[])

    def test_window_wraps_midnight_and_rejects_invalid_clocks(self):
        config={**self.settings,'selection_start_minute_utc':1410,'selection_window_minutes':60}
        self.assertTrue(retention.window(config,NOW-2*3600-15*60))
        self.assertFalse(retention.window(config,NOW-2*3600))
        for epoch in [True,float('nan'),float('inf'),-1]:
            with self.assertRaises(ValueError):retention.window(config,epoch)

    def test_plan_keeps_existing_copy_guards_and_produces_only_readonly_approval(self):
        ident=self.add(1);before=list(self.db.iterdump());changes=self.db.total_changes
        with tempfile.TemporaryDirectory() as folder:
            @contextmanager
            def context(prefix):
                yield self.db,{'prefix':folder},SimpleNamespace(gid=0)
            with patch.object(copies,'context',context):
                result=retention.plan(folder,self.settings,epoch=NOW)
        self.assertEqual(result['selected'],[ident]);self.assertFalse(result['automatic_execution'])
        value=upkeep.checked(result['maintenance_plan'])
        self.assertEqual([s['action'] for s in value['steps']],['control_copy','control_verify','control_rotate','result_rotate'])
        self.assertEqual(value['steps'][0]['plan']['selection'],[ident])
        copies.check(value['steps'][0]['plan'])
        self.assertEqual(list(self.db.iterdump()),before);self.assertEqual(self.db.total_changes,changes)

    def test_live_and_unretired_jobs_are_retained(self):
        self.add(1,purged=False,state='needs_attention');self.add(2,purged=False)
        self.assertEqual(self.select()['skipped'],{'non_terminal':1,'original_not_retired':1})

    def test_each_owners_most_recent_submission_is_retained(self):
        first=self.add(1);self.add(2);third=self.add(3,owner='bob');self.add(4,owner='bob')
        self.settings['keep_recent_per_owner']=1
        self.assertEqual(self.select()['selected'],[first,third])

    def test_explicit_hold_retention_age_and_future_completion(self):
        held=self.add(1);self.add(2,age=30);self.add(3,age=-3600)
        self.settings['hold_job_ids']=[held]
        self.assertEqual(self.select()['skipped'],{'explicit_hold':1,'retention_age':2})

    def test_unknown_and_naive_completion_times_are_not_guessed(self):
        ident=self.add(1)
        for value in [None,'bad','2026-10-01T00:00:00']:
            self.change_job(ident,finished_at=value)
            self.assertEqual(self.select()['skipped'],{'unknown_completion_time':1})

    def test_high_watermark_target_and_max_jobs_limit_selection(self):
        first=self.add(1);self.add(2);self.settings.update(control_high_watermark=3,target_control_jobs=1)
        self.assertEqual(self.select()['status'],'below_high_watermark')
        self.add(3);self.settings.update(max_jobs=1)
        self.assertEqual(self.select()['selected'],[first])

    def test_bounded_keyset_cursor_makes_progress_past_protected_rows(self):
        held=self.add(1);second=self.add(2);third=self.add(3)
        self.settings.update(scan_rows=1,hold_job_ids=[held])
        page=self.select();self.assertEqual(page['status'],'continue_scan')
        page=self.select(after_rowid=page['next_after_rowid']);self.assertEqual(page['selected'],[second])
        page=self.select(after_rowid=page['next_after_rowid']);self.assertEqual(page['selected'],[third])
        self.assertIsNone(page['next_after_rowid'])
        with self.assertRaises(ValueError):self.select(after_rowid=True)

    def test_oversized_job_body_stays_sql_without_loading_it(self):
        self.add(1,payload=retention.MAX_JOB_BYTES+1)
        value=self.select();self.assertEqual(value['scanned_bytes'],0)
        self.assertEqual(value['skipped'],{'oversized_metadata':1})

    def test_total_scanned_metadata_budget_fails_without_mutation(self):
        for index in range(9):self.add(index+1,payload=63000,purged=False,state='running')
        before=self.db.total_changes
        with self.assertRaisesRegex(ValueError,'512KiB'):self.select()
        self.assertEqual(self.db.total_changes,before)

    def test_missing_retirement_receipt_or_mismatched_slot_fails_closed(self):
        ident=self.add(1);self.db.execute('UPDATE history_capacity_slots SET online=1 WHERE job_id=?',(ident,))
        with self.assertRaisesRegex(ValueError,'slot receipt'):self.select()
        self.db.execute('DELETE FROM storage_lifecycle')
        with self.assertRaisesRegex(ValueError,'receipt'):self.select()

    def test_column_mismatch_and_missing_capacity_policy_are_rejected(self):
        ident=self.add(1);self.change_job(ident,owner='someone-else')
        with self.assertRaisesRegex(ValueError,'disagree'):self.select()
        self.db.execute('DELETE FROM history_capacity_policy')
        with self.assertRaisesRegex(ValueError,'separate history'):self.select()

    def test_sql_instruction_budget_interrupts_then_removes_its_handler(self):
        with self.assertRaises(sqlite3.OperationalError):
            with retention.sql_budget(self.db):
                self.db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n').fetchone()
        self.assertEqual(self.db.execute('SELECT 1').fetchone(),(1,))

    def test_existing_pending_copy_and_archive_budget_cannot_be_overridden(self):
        ident=self.add(1)
        with tempfile.TemporaryDirectory() as folder:
            @contextmanager
            def context(prefix):yield self.db,{'prefix':folder},SimpleNamespace(gid=0)
            with patch.object(copies,'context',context):
                with patch.object(copies,'rows',return_value={'old':('copying',{})}):
                    with self.assertRaisesRegex(ValueError,'pending'):retention.plan(folder,self.settings,epoch=NOW)
                with patch.object(copies,'policy',return_value={'bytes':1024**2,'exports':1}):
                    with self.assertRaisesRegex(ValueError,'immutable'):retention.plan(folder,self.settings,epoch=NOW)

    def test_related_source_too_large_cannot_create_a_partial_plan(self):
        self.add(1)
        with tempfile.TemporaryDirectory() as folder:
            @contextmanager
            def context(prefix):yield self.db,{'prefix':folder},SimpleNamespace(gid=0)
            with patch.object(copies,'context',context),patch.object(copies,'source',return_value=(b'x'*(256*1024+1),[])):
                with self.assertRaisesRegex(ValueError,'256KiB'):retention.plan(folder,self.settings,epoch=NOW)


if __name__=='__main__':unittest.main()

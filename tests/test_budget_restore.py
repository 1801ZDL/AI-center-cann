"""Known immutable debits must survive restore; small disposable databases."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import experiment_budget as budgets
from deployment import DeployError


class BudgetRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-budget-restore-');self.root=Path(self.temp.name)
        self.old=self.root/'current';self.new=self.root/'staged';self.old.mkdir();self.new.mkdir()
        with sqlite3.connect(self.old/'jobs.sqlite3') as db:
            db.execute('PRAGMA synchronous=OFF')
            db.execute('CREATE TABLE experiment_budgets(id TEXT PRIMARY KEY,owner TEXT,idem_key TEXT,fingerprint TEXT,data TEXT)')
            db.execute('CREATE TABLE experiment_charges(job_id TEXT PRIMARY KEY,budget_id TEXT,cpu_seconds INTEGER,npu_seconds INTEGER)')
            db.execute('INSERT INTO experiment_budgets VALUES(?,?,?,?,?)',('experiment-a','alice','key','original',json.dumps({'state':'closed','limits':{'jobs':2}})))
            db.executemany('INSERT INTO experiment_charges VALUES(?,?,?,?)',[('job-a','experiment-a',10,1),('job-b','experiment-a',20,2)])
        with sqlite3.connect(self.old/'jobs.sqlite3') as old,sqlite3.connect(self.new/'jobs.sqlite3') as new:old.backup(new)

    def tearDown(self):self.temp.cleanup()
    def change(self,sql,args=()):
        with sqlite3.connect(self.new/'jobs.sqlite3') as db:
            db.execute('PRAGMA synchronous=OFF');db.execute(sql,args)

    def test_exact_known_history_is_readonly(self):
        before=(self.new/'jobs.sqlite3').read_bytes();budgets.require_restore_history(self.old,self.new)
        self.assertEqual((self.new/'jobs.sqlite3').read_bytes(),before)

    def test_missing_or_lowered_charge_cannot_refund_history(self):
        self.change('DELETE FROM experiment_charges WHERE job_id=?',('job-b',))
        with self.assertRaisesRegex(DeployError,'no refund'):budgets.require_restore_history(self.old,self.new)

    def test_amount_and_budget_binding_are_immutable(self):
        for sql in ['UPDATE experiment_charges SET cpu_seconds=0 WHERE job_id="job-a"',
                    'UPDATE experiment_charges SET npu_seconds=9 WHERE job_id="job-a"',
                    'UPDATE experiment_charges SET budget_id="another" WHERE job_id="job-a"']:
            self.change(sql)
            with self.assertRaisesRegex(DeployError,'no refund'):budgets.require_restore_history(self.old,self.new)
            self.change('UPDATE experiment_charges SET budget_id="experiment-a",cpu_seconds=10,npu_seconds=1 WHERE job_id="job-a"')

    def test_missing_approval_or_charge_table_is_rejected(self):
        self.change('DROP TABLE experiment_charges')
        with self.assertRaisesRegex(DeployError,'lose known'):budgets.require_restore_history(self.old,self.new)

    def test_limits_state_owner_key_and_fingerprint_cannot_be_replaced(self):
        for field,value in [('data','{}'),('owner','bob'),('idem_key','new'),('fingerprint','replacement')]:
            with sqlite3.connect(self.old/'jobs.sqlite3') as db:original=db.execute('SELECT '+field+' FROM experiment_budgets').fetchone()[0]
            self.change('UPDATE experiment_budgets SET '+field+'=?',(value,))
            with self.assertRaisesRegex(DeployError,'no refund'):budgets.require_restore_history(self.old,self.new)
            self.change('UPDATE experiment_budgets SET '+field+'=?',(original,))

    def test_additional_staged_history_does_not_erase_original_rows(self):
        self.change('INSERT INTO experiment_charges VALUES(?,?,?,?)',('job-0','experiment-a',1,0))
        budgets.require_restore_history(self.old,self.new)
        self.change('DELETE FROM experiment_charges WHERE job_id="job-a"')
        with self.assertRaises(DeployError):budgets.require_restore_history(self.old,self.new)

    def test_audit_row_and_byte_budgets_are_finite(self):
        with patch.object(budgets,'MAX_RESTORE_ROWS',1):
            with self.assertRaisesRegex(DeployError,'audit exceeds'):budgets.require_restore_history(self.old,self.new)
        with patch.object(budgets,'MAX_RESTORE_BYTES',1):
            with self.assertRaisesRegex(DeployError,'audit exceeds'):budgets.require_restore_history(self.old,self.new)

    def test_legacy_database_without_budget_rows_is_unchanged(self):
        empty=self.root/'legacy';empty.mkdir()
        with sqlite3.connect(empty/'jobs.sqlite3') as db:db.execute('CREATE TABLE old_fixture(x INTEGER)')
        budgets.require_restore_history(empty,self.new)


if __name__=='__main__':unittest.main()

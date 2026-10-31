"""No silent drop, bounded archives, confirmed prefix pruning and replay safety."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import audit_retention as audit
from auth import AccessControl
from lab_client import atomic_json


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='audit-test-');self.base=Path(self.temp.name)
        self.state=self.base/'state';self.state.mkdir(mode=0o700);self.output=self.base/'exports';self.output.mkdir(mode=0o700)
        self.config={'username':'root-test','password':'fixture-not-published','audit_retention':dict(audit.DEFAULT)}
        atomic_json(self.state/'config.json',self.config)
        self.access=AccessControl(self.state,self.config)
        self.meta={'role':'controller','uid':os.geteuid(),'gid':os.getegid(),'state_dir':str(self.state)}
        self.verify=patch('deployment.verify',return_value=(ROOT,self.meta,ROOT));self.verify.start()
        self.root=patch('deployment.require_root');self.root.start()
        self.parent=patch('audit_retention.protected_parent',self.parent_open);self.parent.start()

    @contextmanager
    def parent_open(self,path):
        from control_monitor import directory
        path=Path(path)
        with directory(path.parent,os.geteuid()) as fd:yield path,fd

    def tearDown(self):
        self.access.close();self.parent.stop();self.root.stop();self.verify.stop();self.temp.cleanup()

    def rows(self):
        with sqlite3.connect(self.state/'identities.sqlite3') as db:return list(db.execute('SELECT id,at,actor,action,target,outcome FROM audit ORDER BY id'))

    def fill(self,n=70):
        for i in range(n):self.access.record(self.access.local,'fixture','target-'+str(i))

    def stopped(self):self.access.close()

    def test_policy_rejects_ambiguous_modes_and_legacy_remains_rolling(self):
        for value in [{},True,{'mode':'rolling','maximum_unarchived_rows':10000},{**audit.DEFAULT,'maximum_unarchived_rows':True},{**audit.DEFAULT,'maximum_unarchived_rows':63},{**audit.DEFAULT,'unknown':1}]:
            with self.subTest(value=value),self.assertRaises(ValueError):audit.policy(value)
        self.assertFalse(audit.summary(None)['archive_before_drop'])

    def test_archive_mode_does_not_drop_old_rows_and_reports_backlog(self):
        self.access.audit_policy=audit.policy({**audit.DEFAULT,'maximum_unarchived_rows':64})
        self.access.config['audit_retention']=self.access.audit_policy
        self.fill(70);value=self.access.audit_status()
        self.assertEqual(len(self.rows()),70);self.assertTrue(value['blocks_new_work'])
        with self.assertRaises(audit.Pressure):audit.require(self.access)
        self.access.record(self.access.local,'cancel','existing-job')
        self.assertEqual(len(self.rows()),71)

    def test_live_service_lock_refuses_export_without_creating_output(self):
        self.fill()
        with self.assertRaises(RuntimeError):audit.export(ROOT,self.output/'batch.jsonl')
        self.assertFalse((self.output/'batch.jsonl').exists())

    def test_export_only_audit_fields_no_sensitive_tables_and_confirmed_prune(self):
        self.fill();original=self.rows();self.stopped();path=self.output/'batch.jsonl'
        result=audit.export(ROOT,path,max_rows=64)
        self.assertEqual(result['rows'],64);self.assertEqual(len(self.rows()),71)
        lines=path.read_bytes().splitlines();header=json.loads(lines[0]);self.assertEqual(header['rows'],64)
        self.assertEqual(set(json.loads(lines[1])),set(audit.FIELDS))
        self.assertNotIn(b'fixture-not-published',path.read_bytes());self.assertNotIn(b'password_hash',path.read_bytes())
        with self.assertRaises(ValueError):audit.prune(ROOT,path,'0'*64)
        self.assertEqual(len(self.rows()),71)
        value=audit.prune(ROOT,path,result['sha256']);self.assertEqual(value['pruned_rows'],64)
        remaining=self.rows();self.assertEqual(remaining[:6],original[64:])
        count=len(remaining);self.assertTrue(audit.prune(ROOT,path,result['sha256'])['reused']);self.assertEqual(len(self.rows()),count)
        self.assertTrue(audit.export(ROOT,path,max_rows=64)['reused'])

    def test_orphan_published_file_recovers_same_export_after_receipt_failure(self):
        self.fill();self.stopped();path=self.output/'batch.jsonl'
        with patch('audit_retention.append',side_effect=RuntimeError('receipt failure')):
            with self.assertRaises(RuntimeError):audit.export(ROOT,path,max_rows=64)
        saved=path.read_bytes();self.assertEqual(len(self.rows()),70)
        result=audit.export(ROOT,path,max_rows=64);self.assertEqual(path.read_bytes(),saved)
        self.assertEqual(result['sha256'],hashlib.sha256(saved).hexdigest());self.assertEqual(len(self.rows()),71)

    def test_prune_failure_rolls_back_rows_and_receipt_then_same_confirmation_recovers(self):
        self.fill();self.stopped();path=self.output/'batch.jsonl';result=audit.export(ROOT,path,max_rows=64);original=self.rows()
        with patch('audit_retention.append',side_effect=RuntimeError('after-delete failure')):
            with self.assertRaises(RuntimeError):audit.prune(ROOT,path,result['sha256'])
        self.assertEqual(self.rows(),original)
        self.assertFalse(audit.prune(ROOT,path,result['sha256'])['reused'])

    def test_changed_retained_rows_or_archive_cannot_prune(self):
        self.fill();self.stopped();path=self.output/'batch.jsonl';result=audit.export(ROOT,path,max_rows=64)
        with sqlite3.connect(self.state/'identities.sqlite3') as db:db.execute('UPDATE audit SET target=? WHERE id=1',('changed',))
        with self.assertRaises(ValueError):audit.prune(ROOT,path,result['sha256'])
        self.assertEqual(len(self.rows()),71)
        path.write_bytes(path.read_bytes()+b'corruption')
        with self.assertRaises(ValueError):audit.prune(ROOT,path,result['sha256'])

    def test_restore_epoch_invalidates_old_export_and_sequence_does_not_reuse_ids(self):
        self.fill();self.stopped();path=self.output/'batch.jsonl';result=audit.export(ROOT,path)
        old_last=self.rows()[-1][0];audit.prune(ROOT,path,result['sha256'])
        self.assertGreater(self.rows()[-1][0],old_last)
        audit.rebind_restored(self.state/'identities.sqlite3',self.config)
        with self.assertRaises(ValueError):audit.prune(ROOT,path,result['sha256'])
        self.assertEqual(len(self.rows()),2)

    def test_size_budget_multiple_batches_and_unsafe_archive_types(self):
        self.fill();self.stopped();path=self.output/'batch.jsonl';result=audit.export(ROOT,path,max_rows=64,max_bytes=2048)
        self.assertLess(result['rows'],64)
        with self.assertRaises(ValueError):audit.export(ROOT,path,max_rows=65,max_bytes=2048)
        with self.assertRaises(ValueError):audit.export(ROOT,self.output/'invalid',max_bytes=100000000)
        link=self.output/'linked';link.symlink_to(path)
        with self.assertRaises((ValueError,OSError)):audit.prune(ROOT,link,result['sha256'])

    def test_legacy_config_cannot_silently_enable_archive_or_prune(self):
        self.stopped();atomic_json(self.state/'config.json',{'username':'legacy','password':'unchanged'})
        with self.assertRaises(ValueError):audit.export(ROOT,self.output/'legacy.jsonl')
        self.assertFalse((self.output/'legacy.jsonl').exists())


class AdmissionTests(unittest.TestCase):
    def test_backlog_preserves_replay_grant_and_unconsumed_start(self):
        import test_nodes as builder
        base=builder.NodeTests(methodName='runTest');base.setUp()
        try:
            jobs=base.jobs;access=jobs.access;access.config['audit_retention']={**audit.DEFAULT,'maximum_unarchived_rows':64};access.audit_policy=audit.policy(access.config['audit_retention'])
            with access.connect() as db:audit.initialize(db)
            request={'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'audit-before-backlog'}
            job,_=jobs.submit(request,base.alice);grant=jobs.nodes.claim(base.a,{})['grant']
            for i in range(64):access.record(base.alice,'fixture',str(i))
            repeated,reused=jobs.submit(request,base.alice);self.assertTrue(reused);self.assertEqual(repeated['id'],job['id'])
            with self.assertRaises(audit.Pressure):jobs.submit({**request,'idempotency_key':'blocked'},base.alice)
            recovered=jobs.nodes.claim(base.a,{})['grant'];self.assertEqual(grant['attempt_id'],recovered['attempt_id'])
            auth={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
            result=jobs.nodes.authorize_start(base.a,auth);self.assertTrue(result['wait']);self.assertFalse(result['already_authorized'])
            with access.connect() as db:db.execute('DELETE FROM audit')
            self.assertTrue(jobs.nodes.authorize_start(base.a,auth)['start_permitted'])
        finally:base.tearDown()


if __name__=='__main__':unittest.main()

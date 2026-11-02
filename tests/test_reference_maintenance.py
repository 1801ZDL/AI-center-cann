"""Real offline SQLite/files with explicitly synthetic reference history."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import reference_maintenance as history
from deployment import CORE,DeployError
from lab_client import atomic_json
from maintenance import require_features
import performance_reference as references


class ReferenceMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='reference-rotation-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.state=self.base/'state';self.state.mkdir(mode=0o700)
        self.output=self.base/'exports';self.output.mkdir(mode=0o700);self.path=self.output/'history.json'
        atomic_json(self.state/'config.json',{})
        self.meta={'role':'controller','uid':os.geteuid(),'gid':os.getegid(),'state_dir':str(self.state)}
        for p in [patch('deployment.verify',return_value=(ROOT,self.meta,ROOT)),patch('deployment.require_root'),
                  patch('reference_maintenance.protected_parent',self.parent)]:
            p.start();self.addCleanup(p.stop)
        self.database=self.state/'jobs.sqlite3'
        with sqlite3.connect(self.database) as db:
            db.executescript('''CREATE TABLE jobs(id TEXT,state TEXT,data TEXT);
              CREATE TABLE device_evidence_policies(id TEXT,record TEXT);
              CREATE TABLE performance_references(id TEXT PRIMARY KEY,name TEXT UNIQUE,record TEXT);
              CREATE TABLE performance_reference_events(id INTEGER PRIMARY KEY,record TEXT);''')
            history.initialize(db)
            db.execute('INSERT INTO jobs VALUES(?,?,?)',('job-'+'1'*12,'succeeded',json.dumps({'state':'succeeded','original_result':42})))
        self.database.chmod(0o600);self.fill()

    @contextmanager
    def parent(self,path):
        from control_monitor import directory
        path=Path(path)
        with directory(path.parent,os.geteuid()) as fd:yield path,fd

    def fill(self,name='one'):
        with sqlite3.connect(self.database) as db:
            value={'id':'perfref-'+hashlib.sha256(name.encode()).hexdigest()[:24],'spec':{'name':name},'state':'active','approval':'synthetic-not-real'}
            db.execute('INSERT INTO performance_references VALUES(?,?,?)',(value['id'],name,json.dumps(value)))
            db.execute('INSERT INTO performance_reference_events(record) VALUES(?)',(json.dumps({'reference_id':value['id'],'action':'synthetic-fixture'}),))
        self.identifier=value['id']

    def export(self):return history.export(ROOT,self.path)
    def prune(self,value):return history.prune(ROOT,self.path,value['sha256'])
    def checkpoint(self):return history.read_ledger(self.database)
    def rows(self):
        with sqlite3.connect(self.database) as db:return history.retained(db)
    def snapshot(self,name):
        target=self.base/name;target.mkdir()
        with sqlite3.connect(self.database) as src,sqlite3.connect(target/'jobs.sqlite3') as dst:src.backup(dst)
        return target
    def policy(self,state='paused'):
        value={'state':state,'spec':{'performance_references':{'0':self.identifier}}}
        with sqlite3.connect(self.database) as db:db.execute('INSERT INTO device_evidence_policies VALUES(?,?)',('rule',json.dumps(value)))

    def test_full_export_no_delete_then_confirmed_rotation_and_replay(self):
        original=self.rows();value=self.export();self.assertEqual(self.rows(),original)
        self.assertEqual(json.loads(self.path.read_text())['history'],original)
        self.assertEqual(self.export(),value);self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        result=self.prune(value);self.assertFalse(result['reused']);self.assertEqual(result['generation'],1)
        self.assertEqual(self.rows(),{'references':[],'events':[]});self.assertTrue(self.prune(value)['reused'])
        with sqlite3.connect(self.database) as db:self.assertEqual(json.loads(db.execute('SELECT data FROM jobs').fetchone()[0])['original_result'],42)
        self.fill('new');self.assertEqual(len(self.rows()['references']),1)
        self.assertTrue(self.prune(value)['reused']);self.assertEqual(len(self.rows()['references']),1)

    def test_rotation_is_bounded_checkpoint_and_can_repeat_new_generations(self):
        first=self.export();self.prune(first);head=self.checkpoint()['head_sha256']
        self.fill('second');self.path=self.output/'second.json';second=self.export();self.prune(second)
        self.assertEqual(self.checkpoint()['generation'],2);self.assertNotEqual(self.checkpoint()['head_sha256'],head)
        self.assertEqual(len(self.checkpoint()),4)
        with sqlite3.connect(self.database) as db:self.assertEqual(db.execute('SELECT count(*) FROM performance_reference_ledger').fetchone()[0],1)
        with self.assertRaises(DeployError):history.prune(ROOT,self.output/'history.json',first['sha256'])

    def test_paused_draft_active_policy_references_block_both_operations(self):
        for state in ['paused','active','draft','restore_paused']:
            with self.subTest(state=state):
                self.policy(state)
                with self.assertRaisesRegex(DeployError,'Retire every policy'):self.export()
                with sqlite3.connect(self.database) as db:db.execute('DELETE FROM device_evidence_policies')
        value=self.export();self.policy('active')
        with self.assertRaisesRegex(DeployError,'Retire every policy'):self.prune(value)
        with sqlite3.connect(self.database) as db:db.execute('UPDATE device_evidence_policies SET record=?',(json.dumps({'state':'retired','spec':{'performance_references':{'0':self.identifier}}}),))
        self.assertTrue(self.prune(value)['pruned'])

    def test_inflight_job_and_live_service_lock_block_maintenance(self):
        with sqlite3.connect(self.database) as db:db.execute('UPDATE jobs SET state=?,data=?',('running',json.dumps({'state':'running'})))
        with self.assertRaisesRegex(DeployError,'queued or active'):self.export()
        with sqlite3.connect(self.database) as db:db.execute('DELETE FROM jobs')
        from state_lock import state_lock
        with state_lock(self.state):
            with self.assertRaises(Exception):self.export()

    def test_mismatched_confirmation_tamper_and_links_never_delete(self):
        value=self.export();original=self.rows()
        with self.assertRaises(DeployError):history.prune(ROOT,self.path,'0'*64)
        raw=self.path.read_bytes();self.path.write_bytes(raw+b' ')
        with self.assertRaises(DeployError):self.prune(value)
        self.path.write_bytes(raw);self.path.chmod(0o644)
        with self.assertRaises(DeployError):self.prune(value)
        self.path.chmod(0o600);os.link(self.path,self.output/'hardlink')
        with self.assertRaises(DeployError):self.prune(value)
        (self.output/'hardlink').unlink();self.assertEqual(self.rows(),original)

    def test_changed_rows_reject_and_pending_export_cannot_be_replaced(self):
        value=self.export();self.fill('second')
        with self.assertRaises(DeployError):self.prune(value)
        with self.assertRaises(DeployError):self.export()
        with self.assertRaisesRegex(DeployError,'original prepared'):history.export(ROOT,self.output/'other.json')
        self.assertEqual(len(self.rows()['references']),2)

    def test_archive_publication_survives_lost_database_receipt(self):
        original=history.save
        with patch.object(history,'save',side_effect=RuntimeError('injected after file fsync')):
            with self.assertRaises(RuntimeError):self.export()
        self.assertTrue(self.path.exists());self.assertIsNone(self.checkpoint()['receipt'])
        value=self.export();self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),value['sha256'])
        self.prune(value)

    def test_prune_failure_rolls_back_rows_chain_and_receipt(self):
        value=self.export();original=self.rows();checkpoint=self.checkpoint()
        with patch.object(history,'save',side_effect=RuntimeError('injected before commit')):
            with self.assertRaises(RuntimeError):self.prune(value)
        self.assertEqual(self.rows(),original);self.assertEqual(self.checkpoint(),checkpoint)
        self.assertTrue(self.prune(value)['pruned'])

    def test_old_backup_cannot_resurrect_pruned_history_and_epoch_invalidates_file(self):
        before=self.snapshot('before');value=self.export();pending=self.snapshot('pending');self.prune(value)
        for old in [before,pending]:
            with self.assertRaisesRegex(DeployError,'checkpoint'):references.require_restore_history(self.state,old,'controller')
        latest=self.snapshot('latest');references.require_restore_history(self.state,latest,'controller')
        references.invalidate_restored(self.database)
        self.assertEqual(self.checkpoint()['receipt']['state'],'invalidated')
        with self.assertRaises(DeployError):self.prune(value)
        with self.assertRaises(DeployError):references.require_restore_history(self.state,latest,'controller')

    def test_pending_receipt_backup_and_downgrade_guard_survive_empty_tables(self):
        before=self.snapshot('before');value=self.export()
        with self.assertRaises(DeployError):references.require_restore_history(self.state,before,'controller')
        self.prune(value)
        with self.assertRaisesRegex(DeployError,'rotation checkpoint'):require_features(self.state,'controller',{'files':{n:{} for n in CORE if n!='reference_maintenance.py'}})

    def test_old_backup_without_ledger_and_divergent_generation_rejected(self):
        self.prune(self.export());old=self.snapshot('old')
        with sqlite3.connect(old/'jobs.sqlite3') as db:db.execute('DROP TABLE performance_reference_ledger')
        with self.assertRaises(DeployError):references.require_restore_history(self.state,old,'controller')
        future=self.snapshot('unproven-future')
        with sqlite3.connect(future/'jobs.sqlite3') as db:
            value=history.ledger(db);value['generation']+=1;history.save(db,value)
        with self.assertRaises(DeployError):references.require_restore_history(self.state,future,'controller')

    def test_new_export_after_restore_and_empty_history_no_file(self):
        self.prune(self.export());references.invalidate_restored(self.database)
        empty=self.output/'empty.json';self.assertFalse(history.export(ROOT,empty)['exported']);self.assertFalse(empty.exists())
        self.fill('new-after-restore');self.path=self.output/'new.json';self.prune(self.export())
        self.assertEqual(self.checkpoint()['generation'],2)


if __name__=='__main__':unittest.main()

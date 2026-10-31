"""Monitoring must remain read-only, path-free, bounded and independent of DB IO."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import control_monitor as monitor
from control_filesystem import Unavailable
from lab_client import atomic_json


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='control-monitor-test-')
        self.state=Path(self.tmp.name);self.meta={'role':'controller','uid':os.geteuid(),'state_dir':str(self.state)}
        self.config={'password':'fixture-private-do-not-print','scheduler':{}}
        atomic_json(self.state/'config.json',self.config)
        self.verify=patch('deployment.verify_code',return_value=(ROOT,self.meta,ROOT));self.verify.start()

    def tearDown(self):self.verify.stop();self.tmp.cleanup()

    def test_fixed_file_snapshot_does_not_open_databases_walk_or_change_files(self):
        (self.state/'identities.sqlite3').write_bytes(b'fake database; inspect only metadata')
        (self.state/'jobs.sqlite3-journal').write_bytes(b'preserve original hot journal')
        nested=self.state/'ignored';nested.mkdir();(nested/'secret').write_text('must not traverse')
        before={str(p):p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        with patch('sqlite3.connect',side_effect=AssertionError('Opened database')),patch('os.scandir',side_effect=AssertionError('Walked tree')),patch('os.listdir',side_effect=AssertionError('Listed directory')):
            value=monitor.inspect(ROOT)
        self.assertTrue(value['check_success']);self.assertEqual(value['status'],'ready')
        self.assertEqual(value['components']['identities']['present_files'],1)
        self.assertEqual(value['components']['journals']['apparent_bytes'],len(b'preserve original hot journal'))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.state.rglob('*') if p.is_file()})
        self.assertNotIn(str(self.state),json.dumps(value));self.assertNotIn(self.config['password'],json.dumps(value))
        self.assertFalse(value['audit_retention']['archive_before_drop']);self.assertEqual(value['audit_retention']['maximum_rows'],10000)

    def test_sparse_size_is_not_allocated_bytes_and_component_warning_uses_allocation(self):
        path=self.state/'jobs.sqlite3'
        with path.open('wb') as file:file.truncate(256*monitor.MIB)
        value=monitor.inspect(ROOT,component_warning_mib=1)
        self.assertEqual(value['status'],'ready');self.assertEqual(value['components']['jobs']['apparent_bytes'],256*monitor.MIB)
        with path.open('r+b') as file:os.posix_fallocate(file.fileno(),0,monitor.MIB)
        value=monitor.inspect(ROOT,component_warning_mib=1)
        self.assertEqual(value['status'],'warning');self.assertIn('component_jobs',value['reason_codes'])

    def test_symlink_hardlink_fifo_and_directory_components_are_not_followed(self):
        path=self.state/'jobs.sqlite3';secret=self.state/'secret';secret.write_text('private-target-contents')
        for kind in ['symlink','hardlink','fifo','directory']:
            with self.subTest(kind=kind):
                if kind=='symlink':path.symlink_to(secret)
                elif kind=='hardlink':os.link(secret,path)
                elif kind=='fifo':os.mkfifo(path)
                else:path.mkdir()
                value=monitor.inspect(ROOT);self.assertEqual(value['status'],'unknown');self.assertFalse(value['check_success'])
                self.assertNotIn('private-target-contents',json.dumps(value))
                if kind=='directory':path.rmdir()
                else:path.unlink()

    def test_configuration_permissions_type_size_and_foreign_uid_fail_closed(self):
        path=self.state/'config.json';original=path.read_bytes()
        path.chmod(0o644);self.assertEqual(monitor.inspect(ROOT)['status'],'unknown')
        path.chmod(0o600);path.write_bytes(b'x'*(2*monitor.MIB+1));self.assertEqual(monitor.inspect(ROOT)['status'],'unknown')
        path.write_bytes(original)
        self.meta['uid']=os.geteuid()+1000
        with patch('control_monitor.directory',side_effect=AssertionError('Read another identity state')):
            self.assertEqual(monitor.inspect(ROOT)['status'],'unknown')

    def test_native_mount_loss_returns_critical_before_reading_any_config_or_fallback(self):
        self.meta['control_filesystem']={'device':1}
        with patch('control_process.context',return_value=Mock(check=Mock(side_effect=Unavailable()))),patch('control_monitor.directory',side_effect=AssertionError('Touched fallback directory')):
            result=monitor.inspect(ROOT)
        self.assertEqual((result['status'],result['exit_code']),('critical',2));self.assertFalse(result['check_success'])
        self.assertIsNone(result['free_bytes']);self.assertIn('required_control_filesystem_unavailable',result['reason_codes'])

    def test_node_role_and_installed_code_mismatch_are_unknown(self):
        self.meta['role']='node';self.assertEqual(monitor.inspect(ROOT)['status'],'unknown')
        self.meta['role']='controller'
        with patch('control_monitor.Path.is_file',return_value=False):self.assertEqual(monitor.inspect(ROOT)['status'],'unknown')

    def test_invalid_thresholds_do_not_read_state_and_unknown_never_emits_zero_free_space(self):
        for change in [{'warning_free_mib':False},{'critical_free_mib':0},{'warning_free_mib':1,'critical_free_mib':2},{'critical_free_inodes':513},{'component_warning_mib':'1'}]:
            with self.subTest(change=change),patch('control_monitor.directory',side_effect=AssertionError('Invalid thresholds read state')):
                value=monitor.inspect(ROOT,**change);self.assertEqual(value['exit_code'],3)
                rendered=monitor.prometheus(value)
                self.assertNotIn('ascend_lab_control_free_bytes 0',rendered)
                self.assertIn('ascend_lab_control_check_success 0',rendered)
                self.assertIn('severity="unknown"} 1',rendered)

    def test_counter_boundaries_readonly_and_admission_are_separate_from_warning(self):
        limits=monitor.thresholds(128,64,512,256,32)
        def sample(mib=128,inodes=512,flags=0):return SimpleNamespace(f_frsize=4096,f_bavail=mib*256,f_favail=inodes,f_files=8192,f_flag=flags)
        for space,level in [(sample(),'ready'),(sample(mib=127),'warning'),(sample(mib=64),'warning'),(sample(mib=63),'critical'),(sample(inodes=255),'critical'),(sample(flags=os.ST_RDONLY),'critical')]:
            value=monitor.classify(space,{},limits,None);self.assertEqual(value['status'],level);self.assertIsNone(value['admission_blocks_new_work'])
        value=monitor.classify(sample(mib=200),{},limits,{'minimum_free_mib':256,'minimum_free_inodes':256})
        self.assertEqual(value['status'],'critical');self.assertTrue(value['admission_blocks_new_work'])

    def test_unusable_counters_do_not_become_a_healthy_sample(self):
        for field,value in [('f_frsize',0),('f_bavail',-1),('f_favail',-1),('f_files',0)]:
            space={'f_frsize':4096,'f_bavail':1024,'f_favail':1024,'f_files':8192,'f_flag':0};space[field]=value
            with self.subTest(field=field),patch('control_monitor.os.fstatvfs',return_value=SimpleNamespace(**space)):
                result=monitor.inspect(ROOT);self.assertEqual(result['status'],'unknown');self.assertFalse(result['check_success'])

    def test_existing_audit_policy_rolls_rows_without_claiming_an_archive(self):
        from auth import AccessControl,AUDIT_ROWS
        access=AccessControl(self.state,{'username':'test','password':'test-only'})
        try:
            with access.connect() as db:
                db.executemany('INSERT INTO audit(at,actor,action,target,outcome) VALUES(?,?,?,?,?)',
                    [('t','test','fixture','old'+str(n),'allowed') for n in range(AUDIT_ROWS+5)])
            access.record(access.local,'fixture','new')
            with access.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM audit').fetchone()[0],AUDIT_ROWS)
                self.assertEqual(db.execute('SELECT count(*) FROM audit WHERE target="old0"').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT target FROM audit ORDER BY id DESC LIMIT 1').fetchone()[0],'new')
        finally:access.close()


if __name__=='__main__':unittest.main()

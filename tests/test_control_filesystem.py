"""Finite control startup/admission contracts; actual namespaces tested separately."""
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from control_filesystem import Filesystem,Unavailable,validate
import control_storage
from jobs import JobStore
from auth import AccessControl
from server import Store
from storage_io import exhausted
from test_control_storage import counters


class Contracts(unittest.TestCase):
    def policy(self):
        return {'path':'/srv/control','filesystem':'ext4','device':101,'root_inode':2,
            'bytes':256*1024**2,'inodes':8192,'storage_id':'a'*32}

    def config(self):
        return {'scheduler':{'control_filesystem':self.policy(),'local_enabled':False,
            'control_storage':dict(control_storage.DEFAULT),'payload_storage':{'device':102}}}

    def test_legacy_does_not_probe_or_enable_native_storage(self):
        with patch('control_filesystem.root_fd',side_effect=AssertionError('Unexpected disk probe')):
            fs=Filesystem(ROOT,{})
            self.assertIsNone(fs.check());self.assertIsNone(fs.runtime());self.assertIsNone(fs.origin())
            self.assertEqual(fs.temporary_environment(),{})

    def test_temporary_storage_uses_the_control_device_and_refuses_symlink_escape(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='native-control-tmp-') as folder:
            state=Path(folder);outside=state/'outside';outside.mkdir();fs=Filesystem(state,{})
            fs.policy={'device':state.stat().st_dev}
            with patch.object(fs,'check',return_value=None):
                result=fs.temporary_environment()
                self.assertEqual(result,{'TMPDIR':str(state/'tmp'),'SQLITE_TMPDIR':str(state/'tmp')})
                self.assertEqual((state/'tmp').stat().st_mode&0o777,0o700)
                (state/'tmp').rmdir();(state/'tmp').symlink_to(outside,target_is_directory=True)
                with self.assertRaises(OSError):fs.temporary_environment()
                self.assertEqual(list(outside.iterdir()),[])

    def test_rejects_unknown_fields_undersized_and_unsafe_role_configuration(self):
        valid=self.policy();validate(valid)
        for change in [{'bytes':127*1024**2},{'inodes':1023},{'owner_groups':{}},{'device':True}]:
            with self.subTest(change=change),self.assertRaises(ValueError):validate({**valid,**change})
        for change in [{'local_enabled':True},{'control_storage':None},{'payload_storage':{'device':101}}]:
            value=self.config();value['scheduler'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):Filesystem(ROOT,value)

    def test_outside_cover_blocks_new_work_with_path_free_observation(self):
        fs=Mock(policy=self.policy());fs.origin.side_effect=Unavailable()
        value=control_storage.observe(ROOT,control_storage.DEFAULT,fs)
        self.assertTrue(value['blocks_new_work']);self.assertTrue(value['filesystem_enforced'])
        self.assertEqual(value['status'],'unavailable')
        self.assertNotIn('/srv/',json.dumps(value));self.assertNotIn('storage_id',json.dumps(value))
        with self.assertRaises(control_storage.Pressure):control_storage.require(ROOT,control_storage.DEFAULT,fs)

    def test_healthy_origin_uses_real_headroom_counters(self):
        fs=Mock(policy=self.policy())
        with patch('control_storage.os.fstatvfs',return_value=counters()):
            value=control_storage.observe(ROOT,control_storage.DEFAULT,fs)
        self.assertFalse(value['blocks_new_work']);fs.origin.assert_called_once_with()

    def test_private_check_failure_precedes_database_or_payload_creation(self):
        with patch.object(Filesystem,'check',side_effect=Unavailable()),patch('sqlite3.connect',side_effect=AssertionError('Fallback SQLite opened')):
            with self.assertRaises(Unavailable):JobStore(ROOT,{'username':'test','container':'unused'})
            with self.assertRaises(Unavailable):AccessControl(ROOT,{'username':'test'})
            with self.assertRaises(Unavailable):Store(ROOT/'must-not-exist.sqlite3',Filesystem(ROOT,{}))

    def test_without_protected_runtime_handle_native_start_is_refused(self):
        fs=Filesystem(ROOT,self.config())
        with patch.object(fs,'check',return_value=ROOT),patch('control_filesystem.protected',side_effect=FileNotFoundError):
            with self.assertRaises(Unavailable):fs.runtime()

    def test_only_typed_binding_unavailable_extends_storage_failure_classification(self):
        self.assertTrue(exhausted(Unavailable()))
        self.assertFalse(exhausted(OSError(errno.ENODEV,'unrelated device')))
        self.assertFalse(exhausted(PermissionError('unrelated permissions')))

    def test_offline_guard_rejects_locked_or_live_native_launcher_before_maintenance(self):
        from control_process import offline
        from deployment import DeployError
        meta={'control_filesystem':self.policy()}
        with patch('deployment.require_root'),patch('control_process.process_lock',side_effect=DeployError('busy')):
            with self.assertRaises(DeployError):
                with offline(ROOT,meta):self.fail('Entered maintenance during startup')
        with patch('deployment.require_root'),patch('control_process.process_lock',return_value=99),patch('control_process.protected',return_value={'pid':42}),patch('control_process.alive',return_value=True),patch('control_process.os.close') as close:
            with self.assertRaises(DeployError):
                with offline(ROOT,meta):self.fail('Replaced a live protected handle')
            close.assert_called_once_with(99)

    def test_native_state_cannot_downgrade_to_a_launcherless_release(self):
        from maintenance import require_control_storage
        from deployment import DeployError,unit_text
        config=self.config()
        with self.assertRaises(DeployError):require_control_storage(config,'controller',{'files':{'control_storage.py':{}}})
        require_control_storage(config,'controller',{'files':{n:{} for n in ['control_storage.py','control_process.py','control_filesystem.py']}})
        text=unit_text(Path('/opt/native'),{'role':'controller','uid':999,'gid':999,'state_dir':'/srv/control/state','control_filesystem':self.policy()})
        self.assertIn('User=0\nGroup=0',text);self.assertIn('native-run --prefix',text)
        self.assertIn('CAP_SETUID CAP_SETGID',text)

    def test_capability_limited_root_stops_through_the_original_service_pidfd(self):
        import control_process as process
        meta={'uid':999,'gid':999};saved={'pid':42};running={'running':True,'pid':42,'signal_verified':True}
        with patch('deployment.require_root'),patch('deployment.verify_code',return_value=(ROOT,meta,ROOT)),patch.object(process,'status',side_effect=[running,{'running':False}]),patch.object(process,'protected',return_value=saved),patch.object(process,'alive',return_value=True),patch.object(process.os,'pidfd_open',return_value=99),patch.object(process.os,'close') as close,patch.object(process.signal,'pidfd_send_signal',side_effect=PermissionError),patch.object(process.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as child:
            self.assertFalse(process.stop(ROOT)['running'])
            self.assertEqual(child.call_args.kwargs['pass_fds'],(99,));self.assertEqual(child.call_args.kwargs['timeout'],5)
            close.assert_called_once_with(99)

    def test_exit_during_status_read_is_confirmed_before_rejecting_empty_cmdline(self):
        import control_process as process
        meta={'role':'controller','uid':999,'state_dir':'/srv/control/state','release_id':'a'*64}
        saved={'phase':'issued','format':'ascend-lab-native-process/v1','pid':42,'uid':999,'release_id':'a'*64}
        for final_alive in [False,True]:
            with patch('deployment.load_installation',return_value=(ROOT,meta,ROOT)),patch.object(process,'protected',return_value=saved),patch.object(process,'context',return_value=Mock()),patch.object(process,'alive',side_effect=[True,final_alive]),patch.object(Path,'read_bytes',return_value=b''),patch.object(Path,'read_text',return_value='Uid:\t999\t999\t999\t999'):
                result=process.status(ROOT)
                self.assertEqual(result['running'],final_alive);self.assertFalse(result['signal_verified'])

    def test_stop_waits_for_verified_readback_and_never_signals_replacement(self):
        import control_process as process
        from deployment import DeployError
        meta={'uid':999,'gid':999};saved={'pid':42};blank={'running':True,'pid':42,'signal_verified':False};verified={**blank,'signal_verified':True}
        events=[];states=iter([blank,verified,{'running':False}])
        def readback(_):
            result=next(states);events.append(('status',result.get('signal_verified')));return result
        with patch('deployment.require_root'),patch('deployment.verify_code',return_value=(ROOT,meta,ROOT)),patch.object(process,'status',side_effect=readback),patch.object(process,'protected',return_value=saved),patch.object(process,'alive',return_value=True),patch.object(process.os,'pidfd_open',return_value=99),patch.object(process.os,'close'),patch.object(process.time,'sleep'),patch.object(process.signal,'pidfd_send_signal',side_effect=lambda *args:events.append(('signal',args[0]))):
            self.assertFalse(process.stop(ROOT)['running'])
        self.assertEqual(events[:3],[('status',False),('status',True),('signal',99)])
        with patch('deployment.require_root'),patch('deployment.verify_code',return_value=(ROOT,meta,ROOT)),patch.object(process,'status',return_value=verified),patch.object(process,'protected',side_effect=[saved,{'pid':43}]),patch.object(process.os,'pidfd_open') as opened,patch.object(process.signal,'pidfd_send_signal') as signal:
            with self.assertRaises(DeployError):process.stop(ROOT)
            opened.assert_not_called();signal.assert_not_called()


if __name__=='__main__':unittest.main()

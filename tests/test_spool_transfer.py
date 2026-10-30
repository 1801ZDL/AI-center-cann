"""Storage interruptions preserve retained work and completed output prefixes."""
import errno
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT
from container_engine import DockerAttempt
from spool_transfer import publish,reset_scratch,SpoolIntegrityError


class SpoolTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='spool-transfer-test-');self.root=Path(self.temp.name)
        self.scratch=self.root/'scratch';self.target=self.root/'artifacts/results/data.bin'

    def tearDown(self):self.temp.cleanup()

    def test_complete_file_is_atomically_published_and_replay_needs_no_extra_space(self):
        payload=b'stable result'*10000
        self.assertFalse(publish(io.BytesIO(payload),self.target,len(payload),self.scratch)['reused'])
        identity=self.target.stat().st_ino
        with patch('spool_transfer.reset_scratch',side_effect=OSError(errno.ENOSPC,'full')):
            self.assertTrue(publish(io.BytesIO(payload),self.target,len(payload),self.scratch)['reused'])
        self.assertEqual(self.target.stat().st_ino,identity);self.assertEqual(self.target.read_bytes(),payload)

    def test_partial_import_is_not_published_and_can_resume_original_export(self):
        class Interrupted(io.BytesIO):
            def read(self,size=-1):
                if self.tell():raise OSError(errno.ENOSPC,'full')
                return super().read(min(size,17))
        data=b'x'*1000
        with self.assertRaises(OSError):publish(Interrupted(data),self.target,len(data),self.scratch)
        self.assertFalse(self.target.exists());self.assertFalse((self.scratch/'partial').exists())
        self.assertFalse(publish(io.BytesIO(data),self.target,len(data),self.scratch)['reused'])
        self.assertEqual(self.target.read_bytes(),data)

    def test_conflicting_existing_output_and_unknown_scratch_are_preserved(self):
        publish(io.BytesIO(b'original'),self.target,8,self.scratch)
        with self.assertRaises(SpoolIntegrityError):publish(io.BytesIO(b'changed!'),self.target,8,self.scratch)
        self.assertEqual(self.target.read_bytes(),b'original')
        (self.scratch/'unknown').write_bytes(b'evidence')
        with self.assertRaises(SpoolIntegrityError):reset_scratch(self.scratch)
        self.assertEqual((self.scratch/'unknown').read_bytes(),b'evidence')

    def test_scratch_symlink_is_not_followed_or_deleted(self):
        self.scratch.mkdir(mode=0o700);original=self.root/'private';original.write_bytes(b'kept')
        (self.scratch/'partial').symlink_to(original)
        with self.assertRaises((OSError,SpoolIntegrityError)):reset_scratch(self.scratch)
        self.assertEqual(original.read_bytes(),b'kept');self.assertTrue((self.scratch/'partial').is_symlink())

    def test_storage_error_keeps_original_execution_result_work_and_no_final_result(self):
        grant={'attempt_id':'attempt-'+'a'*32,'owner':'alice','timeout_seconds':30,'template':'cpu_demo'}
        engine=DockerAttempt(self.root/'node',grant,{'image':'sha256:'+'0'*64,'users':{'alice':{'uid':23001,'gid':23001}}})
        (engine.work/'computed.bin').write_bytes(b'one computation')
        engine.state.update(stage='running',started_at='original-start',started_epoch=1)
        info={'State':{'Running':False,'Pid':0,'Status':'exited','ExitCode':0,'OOMKilled':False}}
        with patch.object(engine,'inspect',return_value=info),patch.object(engine,'follow_logs'),patch.object(engine,'stop_log_client'),\
             patch.object(engine,'collect_outputs',side_effect=OSError(errno.ENOSPC,'full')),patch('container_engine.run') as docker:
            with self.assertRaises(OSError):engine.tick()
            first=dict(engine.state['execution_result'])
            with self.assertRaises(OSError):engine.tick()
            docker.assert_not_called()
        self.assertEqual(engine.state['execution_result'],first)
        self.assertEqual(first['status'],'succeeded');self.assertEqual(engine.state['output_pending']['kind'],'capacity')
        self.assertFalse((engine.control/'result.json').exists());self.assertEqual((engine.work/'computed.bin').read_bytes(),b'one computation')

    def test_cancel_of_running_container_does_not_require_spool_availability(self):
        grant={'attempt_id':'attempt-'+'b'*32,'owner':'alice','timeout_seconds':30,'template':'cpu_demo'}
        engine=DockerAttempt(self.root/'cancel-node',grant,{'image':'sha256:'+'0'*64,'users':{'alice':{'uid':23001,'gid':23001}}})
        engine.state.update(stage='running',started_epoch=1)
        with patch.object(engine,'inspect',return_value={'State':{'Running':True}}),\
             patch.object(engine,'prepare_spool',side_effect=RuntimeError('missing spool')) as storage,\
             patch.object(engine,'binding',return_value={}),patch('container_engine.run') as docker:
            self.assertIsNone(engine.tick(cancel=True));storage.assert_not_called()
            self.assertEqual(docker.call_args.args[0][:2],['docker','stop'])
        self.assertEqual(engine.state['stop_reason'],'cancelled')


if __name__=='__main__':unittest.main()

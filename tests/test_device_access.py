"""Device-access failures cannot unlock a user command or a successful NPU result."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import environment_probe as probe


class DeviceAccessTests(unittest.TestCase):
    def context(self,paths=None,uid=23001,caps='0000000000000000'):
        stack=contextlib.ExitStack()
        stack.enter_context(patch.object(probe.Path,'iterdir',return_value=iter(paths or [Path('/dev/davinci0')])))
        stack.enter_context(patch.object(probe.Path,'read_text',return_value='CapEff:\t'+caps+'\nCapPrm:\t'+caps+'\nNoNewPrivs:\t1\n'))
        for name,value in [('getuid',uid),('geteuid',uid),('getgid',23001),('getgroups',[1001])]:
            stack.enter_context(patch.object(probe.os,name,return_value=value))
        stack.enter_context(patch.dict(os.environ,{'ASCEND_RT_VISIBLE_DEVICES':'0'}))
        return stack

    def test_cpu_probe_does_not_enumerate_or_open_devices(self):
        with patch.object(probe.Path,'iterdir') as listing,patch.object(probe.os,'open') as opening:
            value=probe.device_access({})
        listing.assert_not_called();opening.assert_not_called();self.assertIsNone(value['passed'])
        self.assertEqual(value['status'],'not_requested')

    def test_busy_open_is_structured_and_never_confirms_access(self):
        with self.context(),patch.object(probe.os,'open',side_effect=OSError(errno.EBUSY,'busy')):
            value=probe.device_access({'npu_ids':[0],'npu_visible_ids':[0]})
        self.assertFalse(value['passed']);self.assertEqual(value['errors'][0]['errno'],errno.EBUSY)
        self.assertEqual(value['opened_device_ids'],[])
        self.assertFalse(probe.access_confirmed({'device_access':value},[0]))

    def test_extra_exposed_device_fails_before_any_open(self):
        with self.context([Path('/dev/davinci0'),Path('/dev/davinci1')]),patch.object(probe.os,'open') as opening:
            value=probe.device_access({'npu_ids':[0],'npu_visible_ids':[0]})
        opening.assert_not_called();self.assertFalse(value['passed'])
        self.assertIn('Exposed',value['error'])

    def test_root_or_capabilities_do_not_count_as_task_access(self):
        for uid,caps in [(0,'0000000000000000'),(23001,'0000000000000001')]:
            with self.context(uid=uid,caps=caps),patch.object(probe.os,'open') as opening:
                value=probe.device_access({'npu_ids':[0],'npu_visible_ids':[0]})
            opening.assert_not_called();self.assertFalse(value['passed'])

    def test_fd_is_closed_and_regular_file_never_counts_as_a_device(self):
        with self.context(),patch.object(probe.os,'open',return_value=37),\
             patch.object(probe.os,'fstat',return_value=SimpleNamespace(st_mode=stat.S_IFREG)),\
             patch.object(probe.os,'close') as close:
            value=probe.device_access({'npu_ids':[0],'npu_visible_ids':[0]})
        close.assert_called_once_with(37);self.assertFalse(value['passed'])

    def test_success_is_bound_to_allocated_device_and_unprivileged_identity(self):
        with self.context(),patch.object(probe.os,'open',return_value=37) as opening,\
             patch.object(probe.os,'fstat',return_value=SimpleNamespace(st_mode=stat.S_IFCHR)),patch.object(probe.os,'close'):
            value=probe.device_access({'npu_ids':[0],'npu_visible_ids':[0]})
        self.assertTrue(probe.access_confirmed({'device_access':value},[0]))
        self.assertFalse(probe.access_confirmed({'device_access':value},[1]))
        self.assertFalse(probe.access_confirmed({'device_access':value},[]))
        self.assertTrue(opening.call_args.args[1]&os.O_NOFOLLOW)
        self.assertTrue(opening.call_args.args[1]&os.O_NONBLOCK)

    def test_main_stops_before_fingerprinting_when_device_access_fails(self):
        config={'template':'accuracy_v1','parameters':{'lock':{}},'npu_ids':[0]}
        output=io.StringIO()
        with patch.object(probe.Path,'read_text',return_value=json.dumps(config)),\
             patch.object(probe,'device_access',return_value={'passed':False}),patch.object(probe,'fingerprint') as fingerprint,\
             contextlib.redirect_stdout(output),self.assertRaises(SystemExit) as stopped:
            probe.main()
        self.assertEqual(stopped.exception.code,78);fingerprint.assert_not_called()
        value=json.loads(output.getvalue());self.assertFalse(value['locked']);self.assertFalse(value['workload_started'])
        self.assertEqual(value['failure_stage'],'npu_device_access')


if __name__=='__main__':unittest.main()

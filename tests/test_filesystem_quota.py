"""Do not accept accounting-only, unknown ABI/state, or ordinary statvfs as a quota."""
import ctypes
import errno
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import filesystem_quota as quota


class FilesystemQuotaTests(unittest.TestCase):
    def test_native_uapi_structures_have_expected_alignment_and_offsets(self):
        self.assertEqual(ctypes.sizeof(quota.DiskQuota),72)
        self.assertEqual(ctypes.sizeof(quota.QuotaInfo),24)
        self.assertEqual(ctypes.sizeof(quota.QuotaState),160)
        self.assertEqual(quota.QuotaState.flags.offset,2)
        self.assertEqual(quota.QuotaState.user.offset,8)

    def test_accounting_without_enforcement_is_rejected_before_reading_limits(self):
        calls=[]
        def system(fd,command,uid,record):
            calls.append(command);record.flags=1
        with patch.object(quota,'quota_call',side_effect=system):
            with self.assertRaisesRegex(ValueError,'enforcement'):quota.read(20,23801)
        self.assertEqual(calls,[quota.Q_XGETQSTATV])

    def test_unknown_state_or_permission_failure_never_falls_back_to_filesystem_size(self):
        with patch.object(quota,'quota_call',side_effect=OSError(errno.EOPNOTSUPP,'unsupported')),\
             patch.object(quota.os,'statvfs') as fallback:
            with self.assertRaises(OSError):quota.read(20,23801)
            fallback.assert_not_called()
        def wrong_version(fd,command,uid,record):record.version=2
        with patch.object(quota,'quota_call',side_effect=wrong_version):
            with self.assertRaisesRegex(ValueError,'layout'):quota.state(20)

    def test_exact_limits_and_restrictive_soft_limit_are_checked(self):
        current={'byte_hard':16*1024**2,'byte_soft':16*1024**2,'inode_hard':128,'inode_soft':128}
        with patch.object(quota,'read',return_value=current):
            quota.require(20,23801,16*1024**2,128)
            for byte_limit,inodes in [(32*1024**2,128),(16*1024**2,256),(True,128)]:
                with self.assertRaises(ValueError):quota.require(20,23801,byte_limit,inodes)
        with patch.object(quota,'read',return_value={**current,'byte_soft':8*1024**2}):
            with self.assertRaises(ValueError):quota.require(20,23801,16*1024**2,128)

    def test_unprivileged_provision_and_existing_limit_are_not_changed(self):
        with patch.object(quota.os,'geteuid',return_value=23801),patch.object(quota,'quota_call') as system:
            with self.assertRaises(PermissionError):quota.provision(20,23801,16*1024**2,128)
            system.assert_not_called()
        with patch.object(quota.os,'geteuid',return_value=0),patch.object(quota,'read',return_value={
            'byte_hard':1024,'byte_soft':0,'inode_hard':0,'inode_soft':0}),patch.object(quota,'quota_call') as system:
            with self.assertRaisesRegex(ValueError,'existing'):quota.provision(20,23801,16*1024**2,128)
            system.assert_not_called()

    def test_root_or_boolean_uid_cannot_become_a_task_quota(self):
        with patch.object(quota,'quota_call') as system:
            for uid in [0,True,999,2**31]:
                with self.assertRaises(ValueError):quota.read(20,uid)
            system.assert_not_called()


if __name__=='__main__':unittest.main()

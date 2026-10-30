"""Dedicated storage groups must not gain Root or silently lose enforcement."""
import ctypes
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import group_quota as quota


class GroupQuotaTests(unittest.TestCase):
    def test_group_flags_are_distinct_from_user_quota_flags(self):
        def call(flags):
            def system(fd,command,gid,record):record.flags=flags
            return system
        for flags in [0,3,4,8,7]:
            with patch.object(quota,'quota_call',side_effect=call(flags)):
                with self.assertRaisesRegex(ValueError,'enforcement'):quota.read(20,23831)
        with patch.object(quota,'quota_call',side_effect=call(12)):
            self.assertEqual(quota.state(20),{'accounting':True,'enforced':True,'flags':12})

    def test_native_call_uses_group_type_not_user_type(self):
        class Libc:
            def __init__(self):
                from unittest.mock import Mock
                self.quotactl_fd=Mock(return_value=0)
        libc=Libc()
        with patch.object(quota.ctypes,'CDLL',return_value=libc):quota.quota_call(20,quota.Q_GETQUOTA,23831,quota.DiskQuota())
        self.assertEqual(libc.quotactl_fd.call_args.args[:3],(20,(quota.Q_GETQUOTA<<8)|1,23831))

    def test_nonmember_permissions_unknown_abi_or_inactive_state_never_use_statvfs(self):
        with patch.object(quota,'quota_call',side_effect=OSError(errno.EPERM,'denied')),patch.object(quota.os,'statvfs') as fallback:
            with self.assertRaises(OSError):quota.read(20,23831)
            fallback.assert_not_called()
        def invalid(fd,command,gid,record):record.version=2
        with patch.object(quota,'quota_call',side_effect=invalid):
            with self.assertRaisesRegex(ValueError,'layout'):quota.state(20)

    def test_limits_and_usage_cannot_be_silently_replaced(self):
        current={'byte_hard':16*1024**2,'byte_soft':16*1024**2,'inode_hard':128,'inode_soft':128}
        with patch.object(quota,'read',return_value=current):
            quota.require(20,23831,16*1024**2,128)
            with self.assertRaises(ValueError):quota.require(20,23831,32*1024**2,128)
        for change in [{'byte_soft':8*1024**2},{'inode_soft':64}]:
            with patch.object(quota,'read',return_value={**current,**change}):
                with self.assertRaises(ValueError):quota.require(20,23831,16*1024**2,128)
        empty={key:0 for key in ['byte_hard','byte_soft','inode_hard','inode_soft','bytes_used','inodes_used']}
        for change in [{'bytes_used':1},{'inodes_used':1},{'byte_hard':1024}]:
            with patch.object(quota.os,'geteuid',return_value=0),patch.object(quota,'read',return_value={**empty,**change}),patch.object(quota,'quota_call') as system:
                with self.assertRaisesRegex(ValueError,'existing'):quota.provision(20,23831,16*1024**2,128)
                system.assert_not_called()

    def test_unprivileged_provision_is_rejected_before_a_system_call(self):
        with patch.object(quota.os,'geteuid',return_value=23820),patch.object(quota,'quota_call') as system:
            with self.assertRaisesRegex(ValueError,'administrator'):quota.provision(20,23831,16*1024**2,128)
            system.assert_not_called()

    def test_system_or_boolean_group_ids_are_rejected(self):
        with patch.object(quota,'quota_call') as system:
            for gid in [0,999,True,2**31]:
                with self.assertRaises(ValueError):quota.read(20,gid)
            system.assert_not_called()

    def test_directory_assignment_refuses_nonmember_or_existing_contents(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path);root.chmod(0o700);fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:
                with patch.object(quota.os,'getegid',return_value=1104),patch.object(quota.os,'getgroups',return_value=[]),patch.object(quota.os,'fchown') as change:
                    with self.assertRaisesRegex(ValueError,'belong'):quota.assign_empty(fd,23831)
                    change.assert_not_called()
                (root/'already-written').touch()
                with patch.object(quota.os,'fchown') as change:
                    with self.assertRaisesRegex(ValueError,'empty'):quota.assign_empty(fd,23831)
                    change.assert_not_called()
            finally:os.close(fd)


if __name__=='__main__':unittest.main()

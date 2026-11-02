"""Prerequisite failure must not become permission to mutate the system manager."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import systemd_preflight as pre
import preflight


class SystemdPreflightTests(unittest.TestCase):
    def test_retired_enable_is_rejected_before_verifying_or_creating_prefix(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
            result=subprocess.run([str(ROOT/'servicectl'),'unit-install','--enable','--prefix',temp+'/missing','--unit-dir',temp+'/units'],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1);self.assertIn('retired before any write',result.stderr)
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_compatibility_script_is_rejected_without_executing(self):
        from io import BytesIO
        from types import SimpleNamespace
        class Source(BytesIO):
            def fileno(self):return 42
        with patch('pathlib.Path.resolve',return_value=Path('/usr/bin/systemctl')), \
             patch('pathlib.Path.stat',return_value=SimpleNamespace(st_mode=0o40755,st_uid=0)), \
             patch('systemd_preflight.os.open',return_value=42), \
             patch('systemd_preflight.os.fdopen',return_value=Source(b'#!/usr/bin/python3\n')), \
             patch('systemd_preflight.os.fstat',return_value=SimpleNamespace(st_mode=0o100755,st_uid=0)), \
             patch('subprocess.run') as execute:
            with self.assertRaisesRegex(ValueError,'native ELF'):pre.native_binary('/usr/bin/systemctl')
        execute.assert_not_called()

    def test_pid1_name_alone_is_insufficient(self):
        with patch('pathlib.Path.read_text',return_value='systemd\n'),patch('os.readlink',return_value='/usr/bin/python3'),patch.object(pre,'native_binary') as binary:
            with self.assertRaises(ValueError):pre.pid1()
        binary.assert_not_called()

    def test_missing_prerequisites_never_contact_bus(self):
        with patch.object(pre,'pid1',side_effect=PermissionError()),patch.object(pre,'native_binary',side_effect=FileNotFoundError()), \
             patch.object(pre,'cgroups',return_value={'mount_read_only':True}),patch('unit_manager.Bus') as bus:
            value=pre.assess()
        bus.assert_not_called();self.assertFalse(value['prerequisites_ready'])
        self.assertEqual(value['blockers'],['native_systemd_pid1','native_busctl','system_manager_identity'])
        self.assertFalse(value['production_ready']);self.assertFalse(value['service_start_verified'])

    def test_identity_observation_never_loads_reloads_or_starts(self):
        with patch.object(pre,'pid1',return_value={'native_elf':True}),patch.object(pre,'native_binary',return_value={'native_elf':True}), \
             patch.object(pre,'cgroups',return_value={'mount_read_only':True}),patch('unit_manager.Bus') as bus:
            bus.return_value.anchor.return_value={'pid1':{'pid':1,'uid':0}}
            value=pre.assess()
        self.assertEqual(bus.return_value.method_calls,[unittest.mock.call.anchor()])
        self.assertTrue(value['prerequisites_ready']);self.assertFalse(value['unit_configuration_verified'])
        self.assertFalse(value['cgroup_delegation_verified']);self.assertFalse(value['service_start_verified'])

    def test_bus_refusal_is_redacted_and_blocks(self):
        with patch.object(pre,'pid1',return_value={}),patch.object(pre,'native_binary',return_value={}), \
             patch.object(pre,'cgroups',side_effect=ValueError()),patch('unit_manager.Bus') as bus:
            bus.return_value.anchor.side_effect=ValueError('private system-bus detail')
            value=pre.assess()
        self.assertFalse(value['prerequisites_ready']);self.assertNotIn('private system-bus detail',json.dumps(value))

    def test_readonly_mount_wins_over_writable_superblock(self):
        text=b'101 100 0:28 / /sys/fs/cgroup ro,nosuid,nodev - cgroup2 cgroup rw\n'
        from io import BytesIO
        with patch('pathlib.Path.open',return_value=BytesIO(text)):value=pre.cgroups()
        self.assertTrue(value['mount_read_only']);self.assertEqual(value['version'],2)
        self.assertFalse(value['delegation_verified']);self.assertFalse(value['writability_tested'])

    def test_duplicate_or_missing_cgroup_mount_is_unknown(self):
        line=b'101 100 0:28 / /sys/fs/cgroup ro - cgroup2 cgroup rw\n'
        from io import BytesIO
        for value in [b'',line+line]:
            with self.subTest(value=value),patch('pathlib.Path.open',return_value=BytesIO(value)),self.assertRaises(ValueError):pre.cgroups()

    def test_required_check_adds_blocker_without_installing(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
            before=list(Path(temp).iterdir())
            with patch('systemd_preflight.assess',return_value={'prerequisites_ready':False,'blockers':['native_systemd_pid1']}), \
                 patch.object(preflight,'paths',return_value={'prefix':temp+'/prefix','state_dir':temp+'/state'}):
                report=preflight.plan('controller',temp+'/prefix',temp+'/state',uid=24001,gid=24001,require_systemd=True)
            self.assertIn('systemd_prerequisites',report['blockers']);self.assertEqual(list(Path(temp).iterdir()),before)

    def test_actual_cli_returns_report_and_help_exposes_opt_in(self):
        result=subprocess.run([str(ROOT/'servicectl'),'systemd-doctor'],capture_output=True,text=True,timeout=45)
        value=json.loads(result.stdout);self.assertEqual(result.returncode,0 if value['prerequisites_ready'] else 2)
        self.assertTrue(value['read_only']);self.assertFalse(value['service_start_verified'])
        help_result=subprocess.run([str(ROOT/'servicectl'),'doctor','--help'],capture_output=True,text=True,timeout=10)
        self.assertEqual(help_result.returncode,0);self.assertIn('--require-systemd',help_result.stdout)

import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deployment import DeployError
from jobs import JobError
from lab_client import atomic_json
import storage_files as fs
import storage_inventory as inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime')
        self.root=Path(self.temp.name);self.target=self.root/'tree';self.target.mkdir()
        (self.target/'payload').write_bytes(b'abc')

    def tearDown(self):self.temp.cleanup()

    def renumber(self,value):
        value=copy.deepcopy(value);value['root']['mount']+=100000
        for entry in value['entries']:entry['mount']+=100000
        return value

    def test_v2_fingerprint_survives_complete_mount_id_renumbering(self):
        original=fs.scan(self.target);other=self.renumber(original)
        self.assertNotEqual(original,other)
        self.assertEqual(fs.inventory_digest(original),fs.inventory_digest(other))

    def test_legacy_hash_remains_exact_and_namespace_bound(self):
        original=fs.scan(self.target);original.pop('format');other=self.renumber(original)
        self.assertEqual(fs.inventory_digest(original),fs.digest(original))
        self.assertNotEqual(fs.inventory_digest(original),fs.inventory_digest(other))
        self.assertNotIn('format',fs.scan_like(self.target,original))

    def test_partial_mount_change_cannot_be_normalized(self):
        value=fs.scan(self.target);value['entries'][0]['mount']+=1
        with self.assertRaises(JobError):fs.inventory_digest(value)

    def test_file_identity_permissions_and_times_stay_in_the_fingerprint(self):
        original=fs.scan(self.target)
        for key in ['ino','mode','mtime','ctime']:
            changed=copy.deepcopy(original);changed['entries'][0][key]+=1
            with self.subTest(key=key):self.assertNotEqual(fs.inventory_digest(original),fs.inventory_digest(changed))
        changed=copy.deepcopy(original);changed['root']['ino']+=1
        self.assertNotEqual(fs.inventory_digest(original),fs.inventory_digest(changed))

    def test_unknown_schema_and_malformed_fields_fail_closed(self):
        original=fs.scan(self.target)
        for changed in [{**original,'format':'unknown'},{**original,'extra':True},{**original,'bytes':True}]:
            with self.assertRaises(JobError):fs.inventory_digest(changed)

    def test_renumbered_v2_can_remove_only_original_actual_files(self):
        original=self.renumber(fs.scan(self.target))
        complete,removed=fs.purge(self.target,original)
        self.assertTrue(complete);self.assertEqual(removed,2);self.assertFalse(self.target.exists())

    def test_partial_purge_resumes_with_original_inventory_and_bounded_budget(self):
        nested=self.target/'nested';nested.mkdir();(nested/'child').write_bytes(b'child')
        original=self.renumber(fs.scan(self.target));complete,removed=fs.purge(self.target,original,budget=1)
        self.assertFalse(complete);self.assertEqual(removed,1)
        complete,removed=fs.purge(self.target,original,budget=8)
        self.assertTrue(complete);self.assertEqual(removed,3)

    def test_legacy_inventory_does_not_gain_namespace_portability(self):
        original=self.renumber(fs.scan(self.target));original.pop('format')
        with self.assertRaises(JobError):fs.purge(self.target,original)
        self.assertEqual((self.target/'payload').read_bytes(),b'abc')

    def test_mount_replacement_after_scan_is_rejected_before_unlink(self):
        original=fs.scan(self.target);real_mount=fs.mount
        with patch.object(fs,'scan_like',return_value=copy.deepcopy(original)),patch.object(fs,'mount',side_effect=lambda fd:real_mount(fd)+1):
            with self.assertRaises(JobError):fs.purge(self.target,original)
        self.assertTrue((self.target/'payload').exists())

    def test_child_bind_mount_after_scan_is_rejected_before_unlink(self):
        original=fs.scan(self.target);real_mount=fs.mount
        def changed(fd):
            return real_mount(fd)+(1 if os.readlink('/proc/self/fd/'+str(fd))==str(self.target/'payload') else 0)
        with patch.object(fs,'scan_like',return_value=copy.deepcopy(original)),patch.object(fs,'mount',side_effect=changed):
            with self.assertRaises(JobError):fs.purge(self.target,original)
        self.assertTrue((self.target/'payload').exists())

    def test_object_root_mount_boundary_is_still_rejected(self):
        real_mount=fs.mount
        def changed(fd):
            path=os.readlink('/proc/self/fd/'+str(fd))
            return real_mount(fd)+(1 if path==str(self.target) or path.startswith(str(self.target)+'/') else 0)
        with patch.object(fs,'mount',side_effect=changed):
            with self.assertRaisesRegex(JobError,'mount boundary'):fs.scan(self.target)

    def test_same_device_nested_mount_is_still_rejected(self):
        nested=self.target/'nested';nested.mkdir();(nested/'child').write_text('child');real_mount=fs.mount
        def changed(fd):
            path=os.readlink('/proc/self/fd/'+str(fd))
            return real_mount(fd)+(1 if path==str(nested) or path.startswith(str(nested)+'/') else 0)
        with patch.object(fs,'mount',side_effect=changed):
            with self.assertRaisesRegex(JobError,'nested mounts'):fs.scan(self.target)

    def test_controller_cannot_downgrade_retained_v2_inventory(self):
        value=fs.scan(self.target)
        with sqlite3.connect(self.root/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE storage_lifecycle(record TEXT)')
            db.execute('INSERT INTO storage_lifecycle VALUES(?)',(json.dumps({'inventory':value}),))
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):inventory.require_compatible(self.root,'controller',{'files':{}})
        inventory.require_compatible(self.root,'controller',{'files':{'storage_inventory.py':{}}})

    def test_node_cannot_downgrade_pending_v2_cleanup(self):
        path=self.root/'attempts/attempt-fixture/control/state.json';path.parent.mkdir(parents=True)
        atomic_json(path,{'artifact_gc':{'state':'pending','inventory':fs.scan(self.target)}})
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):inventory.require_compatible(self.root,'node',{'files':{}})

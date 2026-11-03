import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.publication_manifest import native_fixture_config


class NativePrivateArchiveTests(unittest.TestCase):
    def archive(self,root,members):
        path=root/'private.tar.gz'
        with tarfile.open(path,'w:gz') as archive:
            for name,value,kind in members:
                member=tarfile.TarInfo(name);member.type=kind
                raw=json.dumps(value).encode();member.size=len(raw) if kind==tarfile.REGTYPE else 0
                archive.addfile(member,io.BytesIO(raw) if member.size else None)
        return path

    def test_only_fixed_member_read_without_extracting_other_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=self.archive(root,[('../../outside',{'password':'not-selected'},tarfile.REGTYPE),
                ('control/config.json',{'password':'fixture-only-value'},tarfile.REGTYPE)])
            self.assertEqual(native_fixture_config(path),{'password':'fixture-only-value'})
            self.assertEqual({p.name for p in root.iterdir()},{'private.tar.gz'})

    def test_archive_before_installation_can_lack_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.archive(Path(directory),[])
            self.assertIsNone(native_fixture_config(path))

    def test_duplicate_or_linked_config_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for entries in [[('control/config.json',{},tarfile.SYMTYPE)],
                [('control/config.json',{},tarfile.REGTYPE)]*2]:
                path=self.archive(root,entries)
                with self.assertRaises(AssertionError):native_fixture_config(path)

    def test_inflated_size_bounded_before_tar_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.tar.gz'
            with gzip.open(path,'wb') as out:out.write(b'\0'*(16*1024**2+1))
            with self.assertRaisesRegex(AssertionError,'inflated limit'):native_fixture_config(path)

    def test_opt_in_control_snapshot_parses_only_known_json_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            payload={'format':'ascend-lab-control-history-export/v1','tables':{'jobs':{
                'columns':['idem_key','data'],'rows':[['{literal-key',json.dumps({'_token':'private-control-fixture-token'})]]}}}
            path=self.archive(Path(directory),[('control/config.json',{'password':'fixture-only-value'},tarfile.REGTYPE),
                ('installation/control-history/'+'a'*64+'.json',payload,tarfile.REGTYPE),
                ('control/personas/history-peer/client.json',{'username':'history-peer','password':'private-peer-fixture-value'},tarfile.REGTYPE),
                ('control/.config/opencode/auth.json',{'token':'must-not-read-model-token'},tarfile.REGTYPE)])
            self.assertEqual(native_fixture_config(path),{'password':'fixture-only-value'})
            value=native_fixture_config(path,include_control=True)
            self.assertIn({'_token':'private-control-fixture-token'},value['control_history'])
            self.assertIn({'username':'history-peer','password':'private-peer-fixture-value'},value['control_history'])
            self.assertNotIn('must-not-read-model-token',json.dumps(value))

    def test_duplicate_or_linked_control_snapshot_members_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);name='installation/control-history/'+'b'*64+'.json'
            payload={'format':'ascend-lab-control-history-export/v1','tables':{}}
            for entries in [[(name,payload,tarfile.SYMTYPE)],[(name,payload,tarfile.REGTYPE)]*2]:
                path=self.archive(root,entries)
                with self.assertRaises(AssertionError):native_fixture_config(path,include_control=True)

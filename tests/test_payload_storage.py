"""Dedicated storage contracts, no fallback and offline snapshot boundaries."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobError
from payload_storage import Payload,validate,root_fd,disjoint,MARKER,TREES
from maintenance import tree,relocate
from deployment import DeployError,unit_text
import test_nodes as node_fixture


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='payload-test-');self.root=Path(self.temp.name)
        self.state=self.root/'control';self.state.mkdir(mode=0o700)
        self.disk=self.root/'disk';self.disk.mkdir(mode=0o700)
        self.policy={'path':str(self.disk),'filesystem':'ext4','storage_id':'a'*32,'device':self.disk.stat().st_dev,
            'root_inode':self.disk.stat().st_ino,'bytes':1024**3,'inodes':10000}

    def tearDown(self):self.temp.cleanup()

    def test_invalid_explicit_contract_never_becomes_legacy(self):
        for value in [{},{**self.policy,'filesystem':'tmpfs'},{**self.policy,'path':'/'},{**self.policy,'path':'/x/../z'},
                      {**self.policy,'storage_id':'wrong'},{**self.policy,'bytes':0},{**self.policy,'extra':1}]:
            with self.subTest(value=value),self.assertRaises(ValueError):Payload(self.state,{'monitor_local':False,'scheduler':{'local_enabled':False,'payload_storage':value}})
        legacy=Payload(self.state,{})
        self.assertEqual(legacy.path('jobs'),self.state/'jobs')

    def test_ordinary_directory_cannot_be_declared_as_a_finite_mount(self):
        fake=SimpleNamespace(f_blocks=1024**3//4096,f_frsize=4096,f_files=10000,f_flag=os.ST_NOSUID|os.ST_NODEV)
        with patch('payload_storage.os.fstatvfs',return_value=fake),self.assertRaisesRegex(ValueError,'dedicated ext4 mount'):
            with root_fd(self.policy,os.geteuid()):pass
        for path in [self.state,self.state/'nested',self.state.parent]:
            with self.assertRaises(ValueError):disjoint({**self.policy,'path':str(path)},self.state)

    def test_binding_and_unknown_files_are_rejected_before_path_use(self):
        for name in TREES:(self.disk/name).mkdir(mode=0o700)
        marker=self.disk/MARKER;marker.write_text(json.dumps({'format':'central-payload/v1','storage_id':'a'*32,'state_dir':str(self.state)}));marker.chmod(0o600)
        @contextmanager
        def fake_root(*_):
            fd=os.open(self.disk,os.O_RDONLY|os.O_DIRECTORY)
            try:yield fd
            finally:os.close(fd)
        config={'monitor_local':False,'scheduler':{'local_enabled':False,'payload_storage':self.policy}}
        with patch('payload_storage.root_fd',fake_root):
            # Same physical storage as control is refused even with a valid marker.
            with self.assertRaises(JobError):Payload(self.state,config)
        with patch('payload_storage.Payload.check'):
            (self.state/'jobs').mkdir()
            with self.assertRaisesRegex(ValueError,'cannot be mixed'):Payload(self.state,config)
        self.assertEqual(list((self.state/'jobs').iterdir()),[])

    def test_snapshot_inventory_rejects_symlinks_hardlinks_and_nested_mount_ids(self):
        f=self.state/'file';f.write_bytes(b'kept')
        self.assertEqual(len(tree(self.state)),1)
        link=self.state/'link';link.symlink_to(f)
        with self.assertRaises(DeployError):tree(self.state)
        link.unlink();os.link(f,link)
        with self.assertRaises(DeployError):tree(self.state)
        link.unlink()
        with patch('storage_files.mount',side_effect=[1,2]),self.assertRaisesRegex(DeployError,'nested mounts'):tree(self.state)

    def test_relocation_maps_only_payload_trees_and_preserves_personal_identity_paths(self):
        import sqlite3
        old='/var/lib/control-old';new='/var/lib/control-new';old_payload='/srv/payload-old';new_payload='/srv/payload-new'
        (self.state/'config.json').write_text(json.dumps({'monitor_local':False,'identity_workspace_root':old+'/users',
            'scheduler':{'local_enabled':False,'workspace_root':old+'/users'}}))
        with sqlite3.connect(self.state/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE jobs(id TEXT,data TEXT)');db.execute('INSERT INTO jobs VALUES(?,?)',('j',json.dumps({'archive_dir':old_payload+'/jobs/j'})))
        relocate(self.state,old,new,'controller',old_payload,new_payload)
        self.assertEqual(json.loads((self.state/'config.json').read_text())['identity_workspace_root'],new+'/users')
        with sqlite3.connect(self.state/'jobs.sqlite3') as db:self.assertEqual(json.loads(db.execute('SELECT data FROM jobs').fetchone()[0])['archive_dir'],new_payload+'/jobs/j')
        text=unit_text('/opt/lab',{'uid':24001,'gid':24001,'state_dir':new,'role':'controller','payload_storage':self.policy})
        self.assertIn('RequiresMountsFor="'+str(self.disk)+'"',text);self.assertIn('ReadWritePaths='+new+' "'+str(self.disk)+'"',text)

    def test_storage_loss_blocks_new_execution_without_freeing_existing_attempt(self):
        fixture=node_fixture.NodeTests(methodName='runTest');fixture.setUp()
        try:
            job=fixture.submit()
            with patch.object(fixture.jobs.payload,'check',side_effect=JobError('storage missing',503)):
                self.assertIsNone(fixture.nodes.claim(fixture.a)['grant'])
                self.assertFalse(fixture.jobs.attempts(job['id']))
            grant=fixture.nodes.claim(fixture.a)['grant'];auth=fixture.auth(grant)
            with patch.object(fixture.jobs.payload,'check',side_effect=JobError('storage missing',503)):
                fixture.nodes.update(fixture.a,{**auth,'running':False,'attention':True})
                self.assertEqual(fixture.jobs.get(job['id'])['state'],'needs_attention')
                self.assertEqual(fixture.jobs.summary()['reserved']['cpu'],1)
            self.assertEqual(fixture.nodes.claim(fixture.a)['grant']['attempt_id'],grant['attempt_id'])
        finally:fixture.tearDown()

    def test_public_summary_does_not_disclose_the_mount_or_storage_identity(self):
        fixture=node_fixture.NodeTests(methodName='runTest');fixture.setUp()
        try:
            fixture.jobs.policy['payload_storage']=self.policy
            shown=fixture.jobs.summary(fixture.alice)['policy']['payload_storage']
            self.assertEqual(set(shown),{'enforced','filesystem','bytes','inodes'})
            self.assertNotIn(str(self.disk),json.dumps(shown));self.assertNotIn('a'*32,json.dumps(shown))
        finally:fixture.tearDown()

    def test_reupload_after_purge_uses_payload_root_without_creating_legacy_inputs(self):
        import test_storage_lifecycle as lifecycle_fixture
        fixture=lifecycle_fixture.LifecycleTests(methodName='runTest');fixture.setUp()
        try:
            value,body=fixture.bundle(b'original');fixture.act('input',value['id'],'trash')
            with fixture.expired():fixture.act('input',value['id'],'purge')
            (fixture.runtime/'inputs').rmdir()
            fixture.jobs.payload.base=self.disk
            revived,_=fixture.jobs.inputs.create(fixture.alice,body)
            self.assertEqual(revived['uploaded'],{})
            self.assertTrue((self.disk/'inputs'/value['id']).is_dir())
            self.assertFalse((fixture.runtime/'inputs').exists())
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()

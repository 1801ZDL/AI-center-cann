"""No data loss on receipt/cleanup interruption; conservative spool placement."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,read_json,JobError
import node_storage as spool
import storage_files as fs


class NodeSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-node-spool-');self.root=Path(self.temp.name)
        self.attempt='attempt-'+'a'*32;self.control=self.root/'attempts'/self.attempt/'control'
        for path in [self.root/'attempts',self.control.parent,self.control]:path.mkdir(mode=0o700)
        self.artifacts=self.control/'artifacts';self.artifacts.mkdir(mode=0o700)
        self.grant={'job_id':'job-'+'1'*12,'attempt_id':self.attempt}
        atomic_json(self.control/'grant.json',self.grant)
    def tearDown(self):self.temp.cleanup()
    def prepare(self,count=2):
        manifest=[]
        for index in range(count):
            name='file-'+str(index);data=('fixture-'+str(index)).encode();(self.artifacts/name).write_bytes(data)
            manifest.append({'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        receipt=spool.receipt({'id':self.grant['job_id'],'attempt_id':self.attempt,'state':'succeeded'},manifest)
        atomic_json(self.control/'artifact-transfer.json',{'manifest':manifest})
        atomic_json(self.control/'state.json',{'stage':'cleaned','controller_acknowledged':True,'artifact_receipt':receipt,'artifact_gc_requested':True})
        return manifest
    def state(self,**changes):
        state=read_json(self.control/'state.json');state.update(changes);atomic_json(self.control/'state.json',state)
    def test_prune_keeps_control_receipt_and_clears_only_confirmed_payload(self):
        self.prepare();(self.artifacts/'empty').touch()
        self.assertTrue(spool.prune(self.root,self.attempt));self.assertFalse(self.artifacts.exists())
        state=read_json(self.control/'state.json');self.assertEqual(state['artifact_gc']['state'],'purged')
        self.assertFalse(state['artifact_gc_requested']);self.assertTrue(state['artifact_receipt'])
        self.assertTrue((self.control/'grant.json').exists());self.assertTrue(spool.prune(self.root,self.attempt))
    def test_active_or_unacknowledged_archive_is_never_removed(self):
        self.prepare()
        for changes in [{'stage':'finished'},{'controller_acknowledged':False}]:
            self.state(stage='cleaned',controller_acknowledged=True)
            self.state(**changes)
            with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
            self.assertTrue(self.artifacts.exists())
    def test_wrong_attempt_digest_or_nonterminal_receipt_preserves_copy(self):
        manifest=self.prepare();good=read_json(self.control/'state.json')['artifact_receipt']
        for key,value in [('job_id','job-'+'2'*12),('attempt_id','attempt-'+'b'*32),('manifest_sha256','0'*64),('state','needs_attention')]:
            self.state(artifact_receipt={**good,key:value})
            with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
            self.assertTrue(self.artifacts.exists())
        self.assertEqual(spool.manifest_sha256(manifest),spool.manifest_sha256(list(reversed(manifest))))
    def test_altered_or_extra_unuploaded_bytes_are_not_garbage(self):
        self.prepare();path=self.artifacts/'file-0';original=path.read_bytes();path.write_bytes(b'corrupted')
        with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
        path.write_bytes(original);(self.artifacts/'extra').write_text('unuploaded')
        with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
        self.assertEqual((self.artifacts/'extra').read_text(),'unuploaded')
    def test_symlinks_hardlinks_and_unsafe_parent_are_refused(self):
        self.prepare();sentinel=self.root/'outside';sentinel.write_text('keep')
        link=self.artifacts/'link';link.symlink_to(sentinel)
        with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
        link.unlink();import os;os.link(sentinel,link)
        with self.assertRaises(JobError):spool.prune(self.root,self.attempt)
        link.unlink();self.control.chmod(0o755)
        with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
        self.assertEqual(sentinel.read_text(),'keep')
    def test_crash_after_rename_resumes_original_journal(self):
        self.prepare();move=fs.move
        def crash(a,b):move(a,b);raise RuntimeError('injected after rename')
        with patch('node_storage.fs.move',side_effect=crash),self.assertRaises(RuntimeError):spool.prune(self.root,self.attempt)
        self.assertFalse(self.artifacts.exists());self.assertEqual(spool.pending(self.root),[self.attempt])
        self.assertTrue(spool.prune(self.root,self.attempt));self.assertEqual(spool.pending(self.root),[])
    def test_bounded_partial_delete_holds_ready_until_complete(self):
        self.prepare(600);self.assertFalse(spool.prune(self.root,self.attempt))
        self.assertEqual(spool.pending(self.root),[self.attempt]);self.assertTrue((self.control/'artifacts-retired').exists())
        self.assertFalse(spool.prune(self.root,self.attempt));self.assertTrue(spool.prune(self.root,self.attempt))
        self.assertFalse((self.control/'artifacts-retired').exists())
    def test_mutation_during_partial_delete_preserves_remaining_evidence(self):
        self.prepare(600);self.assertFalse(spool.prune(self.root,self.attempt))
        target=next((self.control/'artifacts-retired').iterdir());target.write_text('new evidence')
        with self.assertRaises(JobError):spool.prune(self.root,self.attempt)
        self.assertEqual(target.read_text(),'new evidence');self.assertEqual(spool.pending(self.root),[self.attempt])
    def test_collision_does_not_overwrite_another_spool_location(self):
        self.prepare();other=self.control/'artifacts-retired';other.mkdir();(other/'sentinel').write_text('keep')
        with self.assertRaises(ValueError):spool.prune(self.root,self.attempt)
        self.assertEqual((other/'sentinel').read_text(),'keep')
    def test_receipt_without_pending_marker_can_be_explicitly_resumed(self):
        self.prepare();self.state(artifact_gc_requested=False)
        self.assertTrue(spool.prune(self.root,self.attempt))
    def test_offline_node_maintenance_rejects_pending_artifact_recovery(self):
        from maintenance import assert_quiescent
        from deployment import DeployError
        self.prepare()
        with self.assertRaisesRegex(DeployError,'pending node artifact'):assert_quiescent(self.root,'node')
        self.assertTrue(spool.prune(self.root,self.attempt));assert_quiescent(self.root,'node')
    def test_pressure_accounts_for_declared_input_output_and_other_attempts(self):
        job={'artifact_limits':{'file_mib':1024,'total_mib':2048},'input':{'bytes':1024**3,'files':[]}}
        small={'artifact_limits':{'file_mib':64,'total_mib':128}}
        report={'disk_free_bytes':3*1024**3,'capabilities':[spool.CAPABILITY],'disk_free_inodes':10**7,'artifact_gc_ready':True}
        self.assertIn('暂存空间',spool.pressure(job,report,[]))
        report['disk_free_bytes']=4*1024**3;self.assertIsNone(spool.pressure(job,report,[]))
        self.assertIn('暂存空间',spool.pressure(job,report,[job]))
        report['disk_free_inodes']=0;self.assertIn('inode',spool.pressure(small,report,[]))
        report['disk_free_inodes']=10**7;report['artifact_gc_ready']=False
        self.assertIn('回收',spool.pressure(small,report,[]))


class ControllerSpoolTests(unittest.TestCase):
    def setUp(self):
        import test_nodes
        self.fixture=test_nodes.NodeTests();self.fixture.setUp();self.jobs=self.fixture.jobs;self.nodes=self.fixture.nodes
    def tearDown(self):self.fixture.tearDown()
    def beat(self,**overrides):
        body={'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
              'capabilities':['start-ticket-v1','cleanup-before-complete-v1',spool.CAPABILITY],
              'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,
              'disk_free_bytes':8*1024**3,'disk_free_inodes':10**7,'artifact_gc_ready':True,**overrides}
        return self.nodes.heartbeat(self.fixture.a,body)
    def test_missing_inode_or_malformed_ready_observation_is_rejected(self):
        for overrides in [{'disk_free_inodes':None},{'disk_free_inodes':True},{'disk_free_inodes':-1},{'artifact_gc_ready':'true'}]:
            with self.assertRaises(JobError):self.beat(**overrides)
    def test_strict_old_capability_and_inode_pressure_leave_zero_attempts(self):
        job=self.fixture.submit();self.jobs.policy['require_node_spool']=True
        self.assertIsNone(self.nodes.claim(self.fixture.a)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.beat(disk_free_inodes=0);self.assertIsNone(self.nodes.claim(self.fixture.a)['grant'])
        self.beat(artifact_gc_ready=False);self.assertIsNone(self.nodes.claim(self.fixture.a)['grant'])
        self.beat();self.assertEqual(self.nodes.claim(self.fixture.a)['grant']['job_id'],job['id'])
    def test_existing_grant_recovers_even_if_spool_fills(self):
        job=self.fixture.submit();grant=self.nodes.claim(self.fixture.a)['grant']
        self.beat(disk_free_inodes=0,disk_free_bytes=0,artifact_gc_ready=False)
        self.assertEqual(self.nodes.claim(self.fixture.a)['grant'],grant)
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)
    def test_only_checksum_verified_terminal_completion_issues_replayable_receipt(self):
        self.fixture.submit();grant=self.nodes.claim(self.fixture.a)['grant'];auth=self.fixture.auth(grant)
        self.nodes.authorize_start(self.fixture.a,auth)
        data=b'{"runtime":"managed-docker"}';manifest=[{'name':'environment.json','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]
        self.nodes.upload(self.fixture.a,{**auth,'name':'environment.json','offset':0,'data':base64.b64encode(data).decode()})
        body={**auth,'result':{'status':'succeeded','exit_code':0,'termination_confirmed':True,'execution_cleanup_confirmed':True},'artifacts':manifest}
        with self.assertRaises(JobError):self.nodes.complete(self.fixture.a,{**body,'artifacts':[{**manifest[0],'sha256':'0'*64}]})
        receipt=self.nodes.complete(self.fixture.a,body)['artifact_receipt'];self.assertTrue(spool.confirmed(grant,manifest,receipt))
        self.assertEqual(self.nodes.complete(self.fixture.a,body)['artifact_receipt'],receipt)
        self.assertNotIn('_artifact_receipt',self.jobs.summary(self.fixture.alice)['jobs'][0])

"""Finite spool identity and admission cannot become host free-space fallback."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError
from spool_storage import validate,fingerprint,observation,CAPABILITY,Spool,SpoolUnavailable


class SpoolStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='spool-storage-test-');self.root=Path(self.temp.name)
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        folder=self.root/'alice';folder.mkdir();self.jobs.access.create_user('alice',folder,environments=['worker-python'])
        self.policy={'path':'/srv/spool0','filesystem':'ext4','storage_id':'a'*32,'device':12345,'root_inode':2,
            'bytes':1024**3,'inodes':262144}
        self.profile={'name':'finite-spool','physical_id':self.jobs.backend.physical_id,'spool_storage':self.policy,
            'users':{'alice':{'uid':23001,'gid':23001}},'templates':['cpu_demo'],
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1}}

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def beat(self,node,status='ok',cap=True,host_free=None):
        reading={'status':status,'policy_sha256':fingerprint(self.policy),'free_bytes':900*1024**2 if status=='ok' else 0,'free_inodes':250000 if status=='ok' else 0}
        return self.jobs.nodes.heartbeat(node,{'physical_id':self.profile['physical_id'],'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1','node-spool-v1',*([CAPABILITY] if cap else [])],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,
            'disk_free_bytes':reading['free_bytes'] if host_free is None else host_free,'disk_free_inodes':reading['free_inodes'],
            'artifact_gc_ready':True,'spool_observation':reading})

    def test_contract_and_observation_reject_unknown_or_unbounded_fallback(self):
        validate(self.policy)
        for value in [{**self.policy,'filesystem':'tmpfs'},{**self.policy,'owner_groups':{}},{**self.policy,'path':'/'}]:
            with self.assertRaises(ValueError):validate(value)
        for change in [{'free_bytes':2*1024**3},{'free_inodes':True},{'status':'error','free_bytes':1}]:
            value={'status':'ok','policy_sha256':fingerprint(self.policy),'free_bytes':0,'free_inodes':0,**change}
            with self.assertRaises(ValueError):observation(value,self.policy)

    def test_source_failure_keeps_inventory_but_blocks_all_new_attempts(self):
        node,_=self.jobs.nodes.enroll(self.profile);self.beat(node,'error')
        job,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('alice'))
        self.assertIsNone(self.jobs.nodes.claim(node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.beat(node);grant=self.jobs.nodes.claim(node)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['spool_policy_sha256'],fingerprint(self.policy))

    def test_missing_capability_or_host_disk_free_instead_of_spool_is_rejected(self):
        node,_=self.jobs.nodes.enroll(self.profile)
        with self.assertRaises(JobError):self.beat(node,cap=False)
        with self.assertRaises(JobError):self.beat(node,host_free=10*1024**4)

    def test_same_physical_spool_cannot_be_registered_twice_or_overlap_workspace(self):
        self.jobs.nodes.enroll(self.profile)
        for change in [{},{'path':'/srv/alias','storage_id':'b'*32},{'device':45678,'path':'/srv/other'}]:
            other=copy.deepcopy(self.profile);other['name']='peer';other['spool_storage'].update(change)
            with self.assertRaises(JobError):self.jobs.nodes.enroll(other)

    def test_new_controller_finite_requirement_does_not_release_a_waiting_job(self):
        profile=copy.deepcopy(self.profile);profile.pop('spool_storage');node,_=self.jobs.nodes.enroll(profile)
        self.jobs.nodes.heartbeat(node,{'physical_id':profile['physical_id'],'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],'cpu_ids':self.jobs.policy['cpu_ids'],
            'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        self.jobs.policy['require_finite_node_spool']=True
        job,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('alice'))
        self.assertIsNone(self.jobs.nodes.claim(node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)

    def test_unavailable_storage_reports_zero_without_creating_directories(self):
        config={**self.profile,'node_id':'worker-'+'b'*12,'spool_storage':self.policy}
        spool=Spool(self.root,config,check=False)
        with patch.object(spool,'check',side_effect=SpoolUnavailable('missing')):
            self.assertEqual(spool.observe()['free_bytes'],0);self.assertEqual(spool.observe()['status'],'error')

    def test_descriptor_anchor_cannot_fall_through_to_a_replaced_path(self):
        disk=self.root/'anchor-disk';disk.mkdir();(disk/'attempts').mkdir()
        spool=Spool.__new__(Spool);spool.base=disk;spool.policy={};spool.expected={'bound':'test'};spool.check=lambda:None
        @contextmanager
        def opened(*args):
            fd=os.open(disk,os.O_RDONLY|os.O_DIRECTORY)
            try:yield fd
            finally:os.close(fd)
        with patch('spool_storage.root_fd',opened),patch('spool_storage.private_record',return_value=spool.expected):
            with spool.anchor() as anchor:
                disk.rename(self.root/'detached-disk');disk.mkdir()
                (anchor/'attempts/retained').write_bytes(b'original filesystem descriptor')
        self.assertFalse((disk/'attempts').exists())
        self.assertEqual((self.root/'detached-disk/attempts/retained').read_bytes(),b'original filesystem descriptor')


if __name__=='__main__':unittest.main()

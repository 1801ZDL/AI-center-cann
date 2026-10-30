"""Owner/group routing, transaction visibility, admission and safe migration."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,atomic_json
from deployment import unit_text,DeployError
from maintenance import require_features
from payload_groups import validate,gids,require_membership,restored_gid,preparing_identity


class PayloadGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='payload-groups-test-');self.root=Path(self.temp.name)
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        for name in ['alice','bob','unprovisioned']:
            folder=self.root/name;folder.mkdir();self.jobs.access.create_user(name,folder,environments=['worker-python'])
        self.policy={'path':'/srv/lab-payload','filesystem':'ext4','device':12345,'root_inode':2,
            'bytes':512*1024**2,'inodes':8192,'storage_id':'a'*32,
            'owner_groups':{'alice':{'gid':23831,'bytes':192*1024**2,'inodes':1024},
                            'bob':{'gid':23832,'bytes':256*1024**2,'inodes':2048}}}

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def configured_fixture(self):
        self.jobs.payload.policy=copy.deepcopy(self.policy);self.jobs.policy['payload_storage']=self.jobs.payload.policy
        self.jobs.payload.check=lambda:None
        self.operations=[]
        def space(owner):
            if owner not in self.policy['owner_groups']:raise JobError('No dedicated central storage quota',409)
            item=self.policy['owner_groups'][owner]
            return {'gid':item['gid'],'byte_hard':item['bytes'],'bytes_used':0,'inode_hard':item['inodes'],'inodes_used':0}
        def path(target,owner,create=False):
            space(owner);self.operations.append((target.name,owner,create))
        self.jobs.payload.owner_space=space;self.jobs.payload.object=path

    def test_dedicated_gids_and_aggregate_backing_capacity(self):
        self.assertEqual(gids(self.policy),[23831,23832])
        with self.assertRaises(ValueError):validate({**self.policy,'owner_groups':None})
        for change in [{'gid':0},{'gid':23831},{'gid':True},{'bytes':400*1024**2},{'inodes':8192}]:
            value=copy.deepcopy(self.policy);value['owner_groups']['bob'].update(change)
            with self.assertRaises(ValueError):validate(value)

    def test_new_input_uses_the_uncommitted_rows_owner_and_never_guesses(self):
        self.configured_fixture();alice=self.jobs.access.user('alice')
        bundle,_=self.jobs.inputs.create(alice,{'files':[{'name':'empty','size':0,'sha256':hashlib.sha256(b'').hexdigest()}]})
        self.assertEqual(bundle['owner'],'alice')
        self.assertIn((bundle['id'],'alice',True),self.operations)
        self.assertTrue(all(item[1]=='alice' for item in self.operations))
        self.jobs.inputs.seal(bundle['id'],alice)
        self.assertTrue(self.jobs.inputs.get(bundle['id'],alice)['sealed'])
        with self.assertRaises(JobError):self.jobs.inputs.get(bundle['id'],self.jobs.access.user('bob'))

    def test_queued_archive_receives_authenticated_owner_before_any_content(self):
        self.configured_fixture();alice=self.jobs.access.user('alice')
        job,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},alice)
        self.assertIn((job['id'],'alice',True),self.operations)
        self.assertEqual(self.jobs.directory(job['id']),self.jobs.root/job['id'])
        with self.assertRaises(JobError):self.jobs.directory('job-'+'f'*12)

    def test_pending_inputs_and_archives_share_one_owner_hard_budget(self):
        self.configured_fixture();alice=self.jobs.access.user('alice')
        self.jobs.inputs.MAX_FILE=128*1024**2
        self.jobs.inputs.create(alice,{'files':[{'name':'large','size':96*1024**2,'sha256':'a'*64}]})
        with self.assertRaisesRegex(JobError,'filesystem quota'):
            self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},alice)
        self.assertFalse(self.jobs.all());self.assertFalse(list(self.jobs.root.iterdir()))
        bob,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('bob'))
        self.assertEqual(bob['owner'],'bob')

    def test_new_owner_without_provisioned_storage_has_no_fallback(self):
        self.configured_fixture()
        with self.assertRaisesRegex(JobError,'dedicated central'):
            self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('unprovisioned'))
        self.assertFalse(self.jobs.all());self.assertFalse(list(self.jobs.root.iterdir()))

    def test_user_task_cannot_run_as_the_local_controller_uid(self):
        profile={'name':'unsafe-uid','physical_id':self.jobs.backend.physical_id,
            'users':{'alice':{'uid':23001,'gid':23001}},'templates':['cpu_demo'],
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1}}
        with patch('nodes.os.geteuid',return_value=23001):
            with self.assertRaisesRegex(JobError,'controller service UID'):self.jobs.nodes.enroll(profile)

    def test_previously_enrolled_identity_conflict_cannot_receive_new_work(self):
        profile={'name':'historical-uid','physical_id':self.jobs.backend.physical_id,
            'users':{'alice':{'uid':23001,'gid':23001}},'templates':['cpu_demo'],
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1}}
        node,_=self.jobs.nodes.enroll(profile)
        self.jobs.nodes.heartbeat(node,{'physical_id':profile['physical_id'],'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],'cpu_ids':self.jobs.policy['cpu_ids'],
            'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        job,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('alice'))
        with patch.object(self.jobs.nodes,'controller_uid',return_value=23001):
            self.assertIsNone(self.jobs.nodes.claim(node)['grant'])
        self.assertFalse(self.jobs.attempts(job['id']));self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)

    def test_changed_object_group_is_unavailable_instead_of_a_fallback(self):
        from payload_storage import Payload
        payload=Payload(self.root,{});payload.policy=self.policy;payload.owner_space=lambda owner:{}
        with patch('payload_groups.verify_object',side_effect=ValueError('changed')):
            with self.assertRaises(JobError) as caught:payload.object(self.root/'object','alice')
        self.assertEqual(caught.exception.status,503)

    def test_nonroot_writer_needs_all_groups_but_never_sys_admin(self):
        with patch('payload_groups.os.geteuid',return_value=999),patch('payload_groups.os.getegid',return_value=999),patch('payload_groups.os.getgroups',return_value=[23831]):
            with self.assertRaisesRegex(ValueError,'supplementary'):require_membership(self.policy)
        text=unit_text('/opt/lab',{'uid':999,'gid':999,'role':'controller','state_dir':str(self.root),'payload_storage':self.policy})
        self.assertIn('SupplementaryGroups=23831 23832\n',text)
        self.assertIn('CapabilityBoundingSet=\n',text)

    def test_root_preparation_membership_is_scoped_and_restored_even_on_failure(self):
        with patch('payload_groups.os.geteuid',return_value=0),patch('payload_groups.os.getegid',return_value=0),\
             patch('payload_groups.os.getgroups',return_value=[0]),patch('payload_groups.os.setgroups') as change:
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                with preparing_identity(self.policy):raise RuntimeError('interrupted')
            self.assertEqual([call.args[0] for call in change.call_args_list],[[0,23831,23832],[0]])
        with patch('payload_groups.os.geteuid',return_value=999),patch('payload_groups.os.setgroups') as change:
            with self.assertRaisesRegex(ValueError,'administrator'):
                with preparing_identity(self.policy):pass
            change.assert_not_called()

    def test_restore_routes_original_and_quarantined_objects_by_database_owner(self):
        owners={'jobs':{'job-x':'alice'},'inputs':{'input-y':'bob'}}
        for path,gid in [('jobs/job-x/ir/a.mlir',23831),('inputs/input-y/deep/source.py',23832),
                         ('storage-trash/archive/job-x/source/a.py',23831),('storage-trash/input/input-y/a',23832)]:
            self.assertEqual(restored_gid(path,owners,self.policy,999),gid)
        with self.assertRaises(ValueError):restored_gid('jobs/job-unregistered/file',owners,self.policy,999)
        self.assertEqual(restored_gid('storage-trash/archive',owners,self.policy,999),999)

    def test_downgrade_and_public_views_cannot_drop_or_disclose_group_binding(self):
        self.configured_fixture();atomic_json(self.root/'config.json',{'scheduler':{'payload_storage':self.policy}})
        with self.assertRaisesRegex(DeployError,'group hard quotas'):require_features(self.root,'controller',{'files':{'payload_storage.py':{}}})
        summary=self.jobs.summary(self.jobs.access.user('alice'))
        self.assertNotIn('owner_groups',summary['policy']['payload_storage'])
        self.assertNotIn('gid',summary['storage']['filesystem_quota'])
        self.assertEqual(summary['storage']['filesystem_quota']['byte_hard'],192*1024**2)


if __name__=='__main__':unittest.main()

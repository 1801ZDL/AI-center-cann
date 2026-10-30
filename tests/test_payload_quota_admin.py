"""Quota CAS, retained/pending capacity, stale plans and recovery gates."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment
import group_quota as quota
import payload_quota_admin as admin
from lab_client import atomic_json
from payload_storage import binding


class QuotaAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=deployment.ROOT/'runtime',prefix='quota-admin-unit-')
        self.root=Path(self.temp.name);self.state=self.root/'state';self.state.mkdir()
        self.policy={'path':'/srv/payload','filesystem':'ext4','device':123,'root_inode':2,
            'bytes':512*1024**2,'inodes':8192,'storage_id':'a'*32,
            'owner_groups':{'alice':{'gid':23831,'bytes':192*1024**2,'inodes':1024},
                            'bob':{'gid':23832,'bytes':256*1024**2,'inodes':2048}}}
        self.after=copy.deepcopy(self.policy);self.after['owner_groups']['alice'].update(bytes=224*1024**2,inodes=1536)
        self.meta={'role':'controller','uid':999,'gid':999,'state_dir':str(self.state),'release_id':'b'*64,'payload_storage':self.policy}
        self.config={'password':'unit-fixture-not-a-secret','scheduler':{'payload_storage':self.policy}}
        self.plan={'format':'ascend-lab-payload-quota-plan/v1','prefix':str(self.root),'state_dir':str(self.state),
            'release_id':'b'*64,'owner':'alice','before':self.policy,'after':self.after,'revision':0,
            'config_base_sha256':admin._base_config(self.config),'installation_base_sha256':admin._base_meta(self.meta),
            'usage':{'bytes_used':4096,'inodes_used':2},'pending_bytes':0}
        self.plan['id']=admin.fingerprint(self.plan)

    def tearDown(self):self.temp.cleanup()

    def test_plan_digest_and_single_existing_owner_only(self):
        admin._validate_plan(self.plan)
        changed=copy.deepcopy(self.plan);changed['after']['owner_groups']['bob']['bytes']-=1024**2
        changed['id']=admin.fingerprint({k:v for k,v in changed.items() if k!='id'})
        with self.assertRaisesRegex(deployment.DeployError,'only one'):admin._validate_plan(changed)
        changed=copy.deepcopy(self.plan);changed['after']['owner_groups']['alice']['gid']=24000
        changed['id']=admin.fingerprint({k:v for k,v in changed.items() if k!='id'})
        with self.assertRaisesRegex(deployment.DeployError,'only one'):admin._validate_plan(changed)
        changed=copy.deepcopy(self.plan);changed['revision']+=1
        with self.assertRaisesRegex(deployment.DeployError,'Invalid quota'):admin._validate_plan(changed)

    def test_capacity_reserves_unsealed_uploads_and_metadata(self):
        current={'bytes_used':15*1024**2,'inodes_used':120}
        with self.assertRaises(deployment.DeployError):admin._capacity(current,2*1024**2,{'bytes':16*1024**2,'inodes':256})
        with self.assertRaises(deployment.DeployError):admin._capacity(current,0,{'bytes':32*1024**2,'inodes':128})
        admin._capacity(current,2*1024**2,{'bytes':32*1024**2,'inodes':256})

    def test_partial_config_metadata_and_marker_are_resumable_but_unrelated_edits_are_not(self):
        for config_new,meta_new,marker_new in [(False,False,False),(True,False,False),(True,True,False),(True,True,True)]:
            config=copy.deepcopy(self.config);meta=copy.deepcopy(self.meta)
            if config_new:config['scheduler']['payload_storage']=self.after
            if meta_new:meta.update(payload_storage=self.after,payload_quota_revision=1)
            with patch('payload_quota_admin.regular_json',return_value=binding(self.after if marker_new else self.policy,self.state)):
                admin._documents(self.root,meta,self.state,config,self.plan)
        config=copy.deepcopy(self.config);config['password']='external-edit'
        with self.assertRaisesRegex(deployment.DeployError,'changed installation'):admin._documents(self.root,self.meta,self.state,config,self.plan)
        meta={**self.meta,'payload_quota_revision':2}
        with self.assertRaisesRegex(deployment.DeployError,'stale'):admin._documents(self.root,meta,self.state,self.config,self.plan)

    def test_shared_state_users_exclude_the_offline_quota_operation(self):
        from state_lock import state_lock
        with state_lock(self.state):
            with self.assertRaises(RuntimeError):state_lock(self.state,exclusive=True)

    def test_pending_journal_and_symlink_block_normal_deployment_verification(self):
        release=self.root/'release';release.mkdir();manifest={'id':'b'*64,'state_schema':1,'files':{}}
        atomic_json(release/'release.json',manifest);atomic_json(self.state/'config.json',self.config)
        self.meta['state_schema']=1
        with patch('deployment.load_installation',return_value=(self.root,self.meta,release)),patch('deployment.checked_manifest'):
            deployment.verify(self.root)
            for linked in [False,True]:
                journal=self.root/admin.JOURNAL
                if linked:journal.symlink_to(self.root/'missing')
                else:atomic_json(journal,self.plan)
                with self.assertRaisesRegex(deployment.DeployError,'pending payload quota'):deployment.verify(self.root)
                deployment.verify(self.root,quota_recovery=True);journal.unlink()

    def current(self):
        return {'byte_hard':192*1024**2,'byte_soft':192*1024**2,'inode_hard':1024,'inode_soft':1024,
                'bytes_used':4096,'inodes_used':2}

    def test_native_quota_cas_does_not_write_usage_or_change_peer_group(self):
        before={'bytes':192*1024**2,'inodes':1024};after={'bytes':224*1024**2,'inodes':1536}
        with patch('group_quota.os.geteuid',return_value=0),patch('group_quota.read',return_value=self.current()),\
             patch('group_quota.require',return_value={}),patch('group_quota.quota_call') as call:
            quota.resize(9,23831,before,after)
            self.assertEqual(call.call_count,2);args=call.call_args_list[0].args
            self.assertEqual(args[:3],(9,quota.Q_SETQUOTA,23831))
            record=args[3];self.assertEqual(record.valid,5);self.assertEqual(record.space_used,0);self.assertEqual(record.inodes_used,0)
            self.assertEqual(call.call_args_list[1].args,(9,quota.Q_SYNC,0,None))

    def test_native_quota_cas_rejects_foreign_limits_and_under_usage_without_syscall(self):
        before={'bytes':192*1024**2,'inodes':1024};after={'bytes':224*1024**2,'inodes':1536}
        for change in [{'byte_hard':200*1024**2},{'byte_soft':123},{'bytes_used':225*1024**2},{'inodes_used':1537}]:
            with patch('group_quota.os.geteuid',return_value=0),patch('group_quota.read',return_value={**self.current(),**change}),\
                 patch('group_quota.quota_call') as call:
                with self.assertRaises(ValueError):quota.resize(9,23831,before,after)
                call.assert_not_called()

    def test_native_quota_retry_only_syncs_when_target_already_applied(self):
        before={'bytes':192*1024**2,'inodes':1024};after={'bytes':224*1024**2,'inodes':1536}
        current={**self.current(),'byte_hard':after['bytes'],'byte_soft':after['bytes'],'inode_hard':1536,'inode_soft':1536}
        with patch('group_quota.os.geteuid',return_value=0),patch('group_quota.read',return_value=current),\
             patch('group_quota.require',return_value=current),patch('group_quota.quota_call') as call:
            quota.resize(9,23831,before,after);call.assert_called_once_with(9,quota.Q_SYNC,0,None)

    def test_nonroot_quota_change_cannot_reach_kernel(self):
        with patch('group_quota.os.geteuid',return_value=23003),patch('group_quota.quota_call') as call:
            with self.assertRaises(ValueError):quota.resize(9,23831,{'bytes':1024,'inodes':1},{'bytes':2048,'inodes':2})
            call.assert_not_called()


if __name__=='__main__':unittest.main()

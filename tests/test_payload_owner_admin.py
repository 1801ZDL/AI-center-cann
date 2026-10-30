"""Owner append plans, staged-identity gate, old-owner preservation and exclusion."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from auth import AccessControl
from deployment import CORE,DeployError
from lab_client import atomic_json
from payload_storage import binding
import deployment
import payload_owner_admin as admin
import payload_quota_admin as resize


class PayloadOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-owner-storage-')
        self.root=Path(self.temp.name);self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.config={'username':'owner_admin','scheduler':{'workspace_root':str(self.state)}}
        self.workspace=self.state/'new';self.workspace.mkdir(mode=0o700)
        access=AccessControl(self.state,self.config)
        self.staged=access.stage_user('new_owner',self.workspace,quota={'input_storage_mib':4,'archive_storage_mib':4})
        access.close()
        self.before={'path':'/srv/payload','filesystem':'ext4','device':123,'root_inode':2,'bytes':512*1024**2,
                     'inodes':8192,'storage_id':'a'*32,'owner_groups':{'old_owner':{'gid':23831,'bytes':16*1024**2,'inodes':128}}}
        self.after=copy.deepcopy(self.before);self.after['owner_groups']['new_owner']={'gid':23832,'bytes':16*1024**2,'inodes':128}
        self.config['scheduler']['payload_storage']=self.before
        self.meta={'role':'controller','uid':os.getuid(),'gid':os.getgid(),'state_dir':str(self.state),'release_id':'b'*64,'payload_storage':self.before}
        self.value={'format':admin.FORMAT,'prefix':str(self.root),'state_dir':str(self.state),'release_id':'b'*64,
            'owner':'new_owner','stage_id':self.staged['stage_id'],'before':self.before,'after':self.after,'revision':0,
            'config_base_sha256':resize._base_config(self.config),'installation_base_sha256':resize._base_meta(self.meta),
            'control_identity':admin._control(self.state),'usage':{'old_owner':{'bytes_used':4096,'inodes_used':1}}}
        self.sign(self.value)

    def tearDown(self):self.temp.cleanup()

    def sign(self,value):value['id']=resize.fingerprint({k:v for k,v in value.items() if k!='id'});return value

    def test_exactly_one_append_without_old_owner_changes(self):
        admin._validate_plan(self.value)
        for change in ['old-limit','old-gid','extra-owner','existing-owner']:
            value=copy.deepcopy(self.value)
            if change=='old-limit':value['after']['owner_groups']['old_owner']['bytes']+=4096
            elif change=='old-gid':value['after']['owner_groups']['old_owner']['gid']=23900
            elif change=='extra-owner':value['after']['owner_groups']['another']={'gid':23901,'bytes':16*1024**2,'inodes':128}
            else:value['owner']='old_owner'
            with self.subTest(change=change),self.assertRaises(DeployError):admin._validate_plan(self.sign(value))

    def test_digest_typed_identity_and_complete_usage_are_required(self):
        for key,value in [('revision',True),('stage_id','bad'),('control_identity',{'device':True,'inode':1}),
                          ('usage',{}),('state_dir','/srv/../elsewhere')]:
            changed={**self.value,key:value}
            with self.subTest(key=key),self.assertRaises(DeployError):admin._validate_plan(self.sign(changed))
        with self.assertRaises(DeployError):admin._validate_plan({**self.value,'stage_id':'c'*64})
        changed=copy.deepcopy(self.value);changed['before'].pop('owner_groups')
        with self.assertRaisesRegex(DeployError,'Explicit existing'):admin._validate_plan(self.sign(changed))

    def test_disabled_stage_without_any_credential_is_required(self):
        profile=admin._stage(self.state,'new_owner',os.getuid());self.assertEqual(profile['provisioning']['id'],self.staged['stage_id'])
        import sqlite3
        with sqlite3.connect(self.state/'identities.sqlite3') as db:db.execute('UPDATE users SET disabled=0')
        with self.assertRaisesRegex(DeployError,'credential-free'):admin._stage(self.state,'new_owner',os.getuid())
        with sqlite3.connect(self.state/'identities.sqlite3') as db:
            db.execute('UPDATE users SET disabled=1')
            db.execute('INSERT INTO tokens(digest,user,scopes,revoked,expires_at) VALUES(?,?,?,?,?)',('0'*64,'new_owner','[]',1,0))
        with self.assertRaisesRegex(DeployError,'credential-free'):admin._stage(self.state,'new_owner',os.getuid())

    def test_logical_storage_budget_must_fit_the_new_native_group(self):
        profile=admin._stage(self.state,'new_owner',os.getuid());item=self.after['owner_groups']['new_owner']
        admin._storage_budget(profile,item);profile['quota']['archive_storage_mib']=16
        with self.assertRaisesRegex(DeployError,'budgets'):admin._storage_budget(profile,item)

    def test_no_adoption_even_when_existing_native_limits_match(self):
        admin._unassigned(dict.fromkeys(admin.ZERO,0))
        for key in admin.ZERO:
            current=dict.fromkeys(admin.ZERO,0);current[key]=1
            with self.subTest(key=key),self.assertRaisesRegex(DeployError,'refusing adoption'):admin._unassigned(current)

    def test_partial_documents_resumable_but_stage_and_revision_cas_hold(self):
        for config_new,meta_new,marker_new in [(False,False,False),(True,False,False),(True,True,False),(True,True,True)]:
            config=copy.deepcopy(self.config);meta=copy.deepcopy(self.meta)
            if config_new:config['scheduler']['payload_storage']=self.after
            if meta_new:meta.update(payload_storage=self.after,payload_quota_revision=1)
            with patch('payload_owner_admin.regular_json',return_value=binding(self.after if marker_new else self.before,self.state)):
                admin._documents(self.root,meta,self.state,config,self.value)
        with patch('payload_owner_admin.regular_json',return_value=binding(self.before,self.state)):
            with self.assertRaisesRegex(DeployError,'Staged owner definition'):
                admin._documents(self.root,self.meta,self.state,self.config,self.sign({**self.value,'stage_id':'c'*64}))
            with self.assertRaisesRegex(DeployError,'revision'):
                admin._documents(self.root,{**self.meta,'payload_quota_revision':2},self.state,self.config,self.value)

    def test_competing_resize_journal_and_symlink_block_owner_append(self):
        for symlink in [False,True]:
            path=self.root/resize.JOURNAL
            if symlink:path.symlink_to(self.root/'missing')
            else:atomic_json(path,{})
            with self.assertRaises(DeployError):admin._exclusive_operation(self.root)
            path.unlink()

    def test_pending_owner_append_blocks_normal_installed_verification(self):
        release=self.root/'release';release.mkdir();atomic_json(release/'release.json',{'id':'b'*64,'state_schema':1,'files':{}})
        atomic_json(self.state/'config.json',self.config);meta={**self.meta,'state_schema':1}
        with patch('deployment.load_installation',return_value=(self.root,meta,release)),patch('deployment.checked_manifest'):
            for symlink in [False,True]:
                path=self.root/admin.JOURNAL
                if symlink:path.symlink_to(self.root/'missing')
                else:atomic_json(path,self.value)
                with self.assertRaisesRegex(DeployError,'pending owner'):deployment.verify(self.root)
                deployment.verify(self.root,quota_recovery=True);path.unlink()

    def test_nonroot_is_rejected_before_context_or_kernel_access(self):
        with patch('payload_owner_admin.os.geteuid',return_value=23003),patch('payload_owner_admin._context') as context:
            with self.assertRaises(DeployError):admin.apply(self.root,self.value)
            context.assert_not_called()
        self.assertIn('payload_owner_admin.py',CORE)


if __name__=='__main__':unittest.main()

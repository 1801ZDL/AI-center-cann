"""Credential identity/transport and no-overwrite contracts; Root UID I/O separate."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import owner_delivery as delivery
import owner_finish as finish
import deployment as deploy
from deployment import DeployError
from lab_client import atomic_json
from payload_quota_admin import fingerprint
import test_owner_activation as builder


class OwnerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=builder.OwnerActivationTests();self.fixture.setUp();self.fixture.run_activation()
        self.jobs=self.fixture.jobs;self.root=self.fixture.root;self.meta={'uid':os.geteuid()}
        atomic_json(self.root/'config.json',self.jobs.access.config)
        # These cases test DB/credential contracts. Trusted ancestry is tested
        # with actual Root directories in the installed multi-UID fixture.
        self.walk=patch('owner_delivery._walk',side_effect=lambda path,owners:(os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW),[]));self.walk.start()
    def tearDown(self):self.walk.stop();self.fixture.tearDown()

    def material(self):
        with delivery.material(self.root,self.meta,'bob') as result:return result

    def test_existing_committed_credentials_only_and_no_secrets_in_binding(self):
        binding,content=self.material();client=json.loads(content['client.json'])
        encoded=json.dumps(binding)
        self.assertNotIn(client['password'],encoded);self.assertNotIn(client['token'],encoded)
        self.assertEqual(set(binding['files']),{'client.json'})
        self.assertEqual(binding['activation_id'],self.fixture.run_activation()['id'])

    def test_disabled_revoked_and_expired_credentials_cannot_be_delivered(self):
        for command in ["UPDATE users SET disabled=1 WHERE id='bob'","UPDATE tokens SET revoked=1 WHERE user='bob'","UPDATE tokens SET expires_at=0 WHERE user='bob'"]:
            with self.jobs.access.connect() as db:
                db.execute("UPDATE users SET disabled=0 WHERE id='bob'");db.execute("UPDATE tokens SET revoked=0,expires_at=9999999999 WHERE user='bob'");db.execute(command)
            with self.subTest(command=command),self.assertRaises(DeployError):self.material()

    def test_changed_password_owner_transport_or_external_ca_refuses(self):
        path=self.root/'personas/bob/client.json';original=json.loads(path.read_text())
        for change in [{'password':'changed'},{'username':'alice'},{'controller':'http://127.0.0.1:9'},
                       {'ca_file':'/etc/private-key.pem'},{'model_key':'not-an-authorized-field'}]:
            atomic_json(path,{**original,**change})
            with self.subTest(change=change),self.assertRaises(DeployError):self.material()
        atomic_json(path,original)

    def test_relative_public_ca_material_is_included_without_private_key(self):
        cert=self.root/'certificate.pem';key=self.root/'private-test.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=delivery-fixture','-days','1'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=15)
        path=self.root/'personas/bob/client.json';client=json.loads(path.read_text());client['ca_file']='controller-ca.pem';atomic_json(path,client)
        ca=path.parent/'controller-ca.pem';ca.write_bytes(cert.read_bytes());ca.chmod(0o600)
        binding,content=self.material();self.assertEqual(content['controller-ca.pem'],cert.read_bytes())
        self.assertNotIn(key.read_bytes(),b''.join(content.values()));self.assertIn('controller-ca.pem',binding['files'])

    def test_source_links_hardlinks_and_insecure_modes_refuse(self):
        path=self.root/'personas/bob/client.json';data=path.read_bytes();path.chmod(0o644)
        with self.assertRaises(DeployError):self.material()
        path.chmod(0o600);alias=path.with_name('alias');os.link(path,alias)
        with self.assertRaises(DeployError):self.material()
        alias.unlink();path.unlink();alias.write_bytes(data);alias.chmod(0o600);path.symlink_to(alias)
        with self.assertRaises(OSError):self.material()

    def test_real_rename_never_overwrites_existing_directory(self):
        parent=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:
            (self.root/'stage').mkdir();(self.root/'existing').mkdir();(self.root/'existing/sentinel').write_text('preserved')
            with self.assertRaises(OSError):delivery._rename(parent,'stage',parent,'existing')
            self.assertEqual((self.root/'existing/sentinel').read_text(),'preserved')
            delivery._rename(parent,'stage',parent,'new');self.assertTrue((self.root/'new').is_dir())
        finally:os.close(parent)

    def test_request_paths_and_identity_are_explicit(self):
        spec={'owner':'bob','parent':'/home/bob','name':'agent','uid':23004,'gid':23004,'spool':'/home/.lab-spool'};delivery.request(spec)
        for change in [{'uid':0},{'gid':True},{'parent':'/home/../root'},{'name':'../profile'},{'spool':'relative'}]:
            with self.subTest(change=change),self.assertRaises(DeployError):delivery.request({**spec,**change})

    def test_non_root_commands_fail_before_source_or_target_access(self):
        with patch('owner_delivery.deploy.require_root',side_effect=DeployError('Root required')):
            with self.assertRaises(DeployError):delivery.plan('/missing',{'owner':'bob','parent':'/home/bob','name':'agent','uid':23004,'gid':23004,'spool':'/home/.spool'})
            with self.assertRaises(DeployError):delivery.apply('/missing',{})
            with self.assertRaises(DeployError):finish.finish('/missing',{})

    def test_finish_requires_same_owner_and_exact_activation_nodes(self):
        spec={'operation_id':'a'*32,'activation':self.fixture.spec,'delivery':{'owner':'bob','parent':'/home/bob','name':'agent','uid':23004,'gid':23004,'spool':'/home/.spool'}}
        finish.request(spec)
        bad=copy.deepcopy(spec);bad['delivery']['owner']='alice'
        with self.assertRaises(DeployError):finish.request(bad)
        bad=copy.deepcopy(spec);bad['activation']['required_nodes']=[]
        with self.assertRaises(ValueError):finish.request(bad)
        bad=copy.deepcopy(spec);bad.pop('operation_id')
        with self.assertRaises(DeployError):finish.request(bad)

    def test_pending_delivery_and_finish_guards_are_independent(self):
        prefix=self.root/'prefix';release=self.root/'release';prefix.mkdir();release.mkdir()
        meta={'role':'controller','uid':os.getuid(),'state_dir':str(self.root),'release_id':'a'*64,'state_schema':1}
        manifest={'id':'a'*64,'state_schema':1,'files':{}}
        with patch('deployment.load_installation',return_value=(prefix,meta,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
            for filename,flag in [(delivery.JOURNAL,'delivery_recovery'),(finish.JOURNAL,'finish_recovery')]:
                (prefix/filename).touch()
                for other in [{},{'manager_recovery':True},{'startup_recovery':True},{'quota_recovery':True}]:
                    with self.subTest(file=filename,other=other),self.assertRaises(DeployError):deploy.verify(prefix,**other)
                deploy.verify(prefix,**{flag:True});(prefix/filename).unlink()

    def test_service_restart_bypasses_only_credential_journals(self):
        prefix=self.root/'service-prefix';release=self.root/'service-release';prefix.mkdir();release.mkdir()
        meta={'role':'controller','uid':os.getuid(),'state_dir':str(self.root),'release_id':'a'*64,'state_schema':1}
        manifest={'id':'a'*64,'state_schema':1,'files':{}}
        with patch('deployment.load_installation',return_value=(prefix,meta,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
            (prefix/finish.JOURNAL).touch();(prefix/delivery.JOURNAL).touch()
            self.assertEqual(deploy._service_verify(prefix)[1],meta)
            for resource_journal in ['payload-owner-change.json','unit-refresh-change.json','unit-manager-change.json','unit-start-change.json']:
                (prefix/resource_journal).touch()
                with self.subTest(journal=resource_journal),self.assertRaises(DeployError):deploy._service_verify(prefix)
                (prefix/resource_journal).unlink()


if __name__=='__main__':unittest.main()

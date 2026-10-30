"""Uncommitted credential cleanup, spent IDs and cross-generation recovery."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_activation as builder
from owner_activation_abort import Abort,_plan_check,_sha,DIRECTORY
from owner_activation import _save
from jobs import JobError,identity
from lab_client import atomic_json
from maintenance import assert_quiescent,require_features
from deployment import DeployError


class Crash(Exception):pass


class ActivationAbortTests(unittest.TestCase):
    def setUp(self):
        self.fixture=builder.OwnerActivationTests();self.fixture.setUp();self.jobs=self.fixture.jobs;self.root=self.fixture.root
        self.abort=Abort(self.jobs);self.intent_path=self.root/'owner-activations/bob.pending.json';self.client_path=self.root/'personas/bob/client.json'

    def tearDown(self):self.fixture.tearDown()

    def pending(self,boundary='client'):
        def crash(name):
            if name==boundary:raise Crash()
        with patch('owner_activation._checkpoint',side_effect=crash),self.assertRaises(Crash):self.fixture.run_activation()
        return json.loads(self.intent_path.read_text())

    def plan(self,intent):return self.abort.plan('bob',intent['id'])['plan']

    def test_expired_intent_can_be_aborted_without_generating_credentials(self):
        intent=self.pending();intent['expires_at']=-1;_save(self.intent_path,intent)
        with self.assertRaisesRegex(JobError,'expired'):self.fixture.run_activation()
        with patch('owner_activation.secrets.token_urlsafe',side_effect=AssertionError('No credentials during abort')):
            plan=self.plan(intent);first=self.abort.apply(plan);again=self.abort.apply(plan)
        self.assertFalse(first['reused']);self.assertTrue(again['reused']);self.assertFalse(self.intent_path.exists());self.assertFalse(self.client_path.exists())
        self.assertIsNone(self.jobs.access.user('bob'));self.assertFalse(first['credentials_issued'])
        with self.jobs.access.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tokens WHERE user='bob'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM owner_activation_aborts').fetchone()[0],1)

    def test_same_abort_plan_survives_eight_successive_crashes(self):
        intent=self.pending();plan=self.plan(intent)
        for boundary in ['journal','client','trust','before_commit','committed','receipt','intent_removed','published']:
            def crash(name):
                if name==boundary:raise Crash()
            with patch('owner_activation_abort._checkpoint',side_effect=crash),self.assertRaises(Crash):self.abort.apply(plan)
            if boundary!='published':
                with self.assertRaisesRegex(JobError,'original activation abort'):self.fixture.run_activation()
                with self.assertRaises(DeployError):assert_quiescent(self.root,'controller')
            self.assertIsNone(self.jobs.access.user('bob'))
        self.assertTrue(self.abort.apply(plan)['reused'])

    def test_completed_abort_replay_cannot_delete_new_active_credentials(self):
        old=self.pending();plan=self.plan(old);self.abort.apply(plan)
        new=self.fixture.run_activation();new_bytes=self.client_path.read_bytes();self.assertNotEqual(new['id'],old['id'])
        self.assertTrue(self.abort.apply(plan)['reused']);self.assertEqual(self.client_path.read_bytes(),new_bytes)
        self.assertIsNotNone(self.jobs.access.user('bob'));self.assertIsNone(self.jobs.access.authenticate('Bearer '+old['client']['token']))
        basic='Basic '+base64.b64encode(('bob:'+old['client']['password']).encode()).decode();self.assertIsNone(self.jobs.access.authenticate(basic))

    def test_spent_intent_cannot_be_restored_and_activated(self):
        old=self.pending();plan=self.plan(old);self.abort.apply(plan);_save(self.intent_path,old)
        with self.assertRaisesRegex(JobError,'was aborted'):self.fixture.run_activation()
        self.assertFalse(self.client_path.exists());self.assertIsNone(self.jobs.access.user('bob'))

    def test_abort_plan_cannot_remove_credentials_if_activation_committed_in_between(self):
        old=self.pending();plan=self.plan(old);self.fixture.run_activation();original=self.client_path.read_bytes()
        with self.assertRaisesRegex(JobError,'uncommitted'):self.abort.apply(plan)
        self.assertEqual(self.client_path.read_bytes(),original);self.assertIsNotNone(self.jobs.access.user('bob'))

    def test_committed_but_unpublished_activation_is_not_abortable(self):
        intent=self.pending('committed');original=self.client_path.read_bytes()
        with self.assertRaisesRegex(JobError,'uncommitted'):self.plan(intent)
        self.assertEqual(self.client_path.read_bytes(),original)

    def test_changed_resource_context_does_not_prevent_explicit_abort(self):
        intent=self.pending();self.fixture.alter_node(lambda n:n.update(last_seen=0))
        self.abort.apply(self.plan(intent));self.assertFalse(self.intent_path.exists());self.assertIsNone(self.jobs.access.user('bob'))

    def test_foreign_or_recreated_personal_files_refuse_before_journal(self):
        intent=self.pending();plan=self.plan(intent);self.client_path.unlink();atomic_json(self.client_path,intent['client'])
        with self.assertRaisesRegex(JobError,'identity changed'):self.abort.apply(plan)
        self.assertFalse((self.root/DIRECTORY/'bob.pending.json').exists())
        atomic_json(self.client_path,{'private':'replacement'})
        with self.assertRaisesRegex(JobError,'differs'):self.plan(intent)
        self.assertEqual(json.loads(self.client_path.read_text()),{'private':'replacement'})

    def test_symlinks_and_links_never_become_cleanup_authority(self):
        intent=self.pending();self.client_path.unlink();outside=self.root/'sentinel';atomic_json(outside,intent['client']);self.client_path.symlink_to(outside)
        with self.assertRaises(OSError):self.plan(intent)
        self.assertTrue(outside.exists());self.client_path.unlink();__import__('os').link(outside,self.client_path)
        with self.assertRaises(JobError):self.plan(intent)

    def test_created_trust_is_removed_but_preexisting_trust_is_preserved(self):
        self.jobs.access.config['ca_file']=str(self.root/'authority.pem');pem=b'fixture-public-trust-for-lifecycle-only'
        with patch('owner_activation.authority_bytes',return_value=pem):intent=self.pending()
        trust=self.client_path.parent/'controller-ca.pem';self.assertTrue(intent['trust_created_by_activation']);self.abort.apply(self.plan(intent));self.assertFalse(trust.exists())
        trust.write_bytes(pem);trust.chmod(0o600)
        with patch('owner_activation.authority_bytes',return_value=pem):second=self.pending()
        self.assertFalse(second['trust_created_by_activation']);plan=self.plan(second);self.assertTrue(plan['preserve_trust']);self.abort.apply(plan);self.assertEqual(trust.read_bytes(),pem)

    def test_legacy_intent_without_trust_ownership_marker_preserves_trust(self):
        self.jobs.access.config['ca_file']=str(self.root/'authority.pem');pem=b'legacy-public-trust'
        with patch('owner_activation.authority_bytes',return_value=pem):intent=self.pending()
        intent.pop('trust_created_by_activation');_save(self.intent_path,intent);self.abort.apply(self.plan(intent))
        self.assertEqual((self.client_path.parent/'controller-ca.pem').read_bytes(),pem)

    def test_plan_and_status_do_not_export_passwords_or_tokens(self):
        intent=self.pending();status=self.abort.status('bob');plan=self.plan(intent)
        self.assertEqual(status['activation_id'],intent['id']);self.assertEqual(status['state'],'pending')
        for data in [status,plan]:
            text=json.dumps(data);self.assertNotIn(intent['client']['token'],text);self.assertNotIn(intent['client']['password'],text)
        self.abort.apply(plan);self.assertEqual(self.abort.status('bob')['state'],'staged')

    def test_downgrade_rejects_spent_ids_even_after_private_receipt_loss(self):
        intent=self.pending();self.abort.apply(self.plan(intent));(self.root/DIRECTORY/(intent['id']+'.json')).unlink()
        with self.assertRaisesRegex(DeployError,'Spent activation IDs'):require_features(self.root,'controller',{'files':{'owner_activation.py':{}}})

    def test_wrong_owner_id_or_typed_plan_cannot_redirect_cleanup(self):
        intent=self.pending();plan=self.plan(intent)
        for owner,identifier in [('../outside',intent['id']),('bob','0'*32),('alice',intent['id'])]:
            with self.subTest(owner=owner),self.assertRaises(JobError):self.abort.plan(owner,identifier)
        wrong=copy.deepcopy(plan);wrong['directories']['runtime']['inode']=True;wrong['id']=_sha({k:v for k,v in wrong.items() if k!='id'})
        with self.assertRaises(JobError):_plan_check(wrong)
        with self.assertRaises(JobError):self.abort.apply({**plan,'activation_id':'f'*32})

    def test_live_old_controller_capability_is_rejected(self):
        intent=self.pending();atomic_json(self.root/'server-ready.json',{**identity(__import__('os').getpid()),'capabilities':['owner-activation-v1']})
        with self.assertRaisesRegex(JobError,'Reload'):self.plan(intent)

    def test_installed_admin_dispatch_refuses_equal_form_state_overrides(self):
        import runpy,os
        command=runpy.run_path(str(ROOT/'servicectl'))['main']
        meta={'role':'controller','uid':os.geteuid(),'gid':os.getegid()}
        for tool,override in [('accessctl','--state-dir=/other'),('workerctl','--controller-state=/other'),
                              ('accessctl','--state-d=/other'),('workerctl','--controller-st=/other')]:
            with patch('sys.argv',['servicectl','admin','--prefix','/selected','--',tool,override,'user-status','bob']),\
                 patch('deployment.verify',return_value=(self.root,meta,self.root)),patch('subprocess.run') as run,\
                 self.assertRaisesRegex(DeployError,'without overriding'):
                command()
            run.assert_not_called()


if __name__=='__main__':unittest.main()

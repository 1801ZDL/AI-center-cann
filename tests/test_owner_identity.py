"""Staged owners cannot obtain credentials before all resource bindings exist."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from auth import AccessControl,AccessError
from deployment import bundle,unpack_release,DeployError,CORE
from lab_client import atomic_json
from maintenance import require_features,relocate
import owner_identity


class OwnerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-owner-stage-')
        self.root=Path(self.temp.name);self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.config={'username':'stage_admin','password':'stage-test-only','monitor_local':False,
                     'scheduler':{'workspace_root':str(self.state),'local_enabled':False},
                     'identity_workspace_root':str(self.state/'users')}
        (self.state/'users').mkdir(mode=0o700);atomic_json(self.state/'config.json',self.config)
        self.workspace=self.state/'users/new_owner/workspace';self.workspace.mkdir(parents=True)
        self.access=AccessControl(self.state,self.config)

    def tearDown(self):self.access.close();self.temp.cleanup()

    def stage(self,**kwargs):return self.access.stage_user('new_owner',self.workspace,**kwargs)

    def test_stage_is_atomic_credential_free_and_replay_is_exact(self):
        with patch('auth.secrets.token_urlsafe',side_effect=AssertionError('No credential generation during staging')):
            first=self.stage();again=self.stage()
        self.assertFalse(first['reused']);self.assertTrue(again['reused'])
        self.assertEqual(first['stage_id'],again['stage_id']);self.assertFalse(first['credentials_issued'])
        with self.access.connect() as db:
            row=db.execute('SELECT disabled,password_hash,profile FROM users WHERE id=?',('new_owner',)).fetchone()
            self.assertEqual(row[:2],(1,'!'));self.assertEqual(db.execute('SELECT COUNT(*) FROM tokens').fetchone()[0],0)
        self.assertTrue(owner_identity.staged('new_owner',json.loads(row[2])))
        with self.assertRaises(AccessError):self.stage(quota={'cpu':2})
        with self.assertRaises(AccessError):self.access.create_user('new_owner',self.workspace)
        status=self.access.registration('new_owner');self.assertEqual(status['state'],'staged')
        self.assertFalse({'password','password_hash','salt','token','digest'}&set(status))

    def test_enable_tokens_and_profile_mutation_cannot_bypass_stage(self):
        self.stage();self.access.add_member('project','new_owner')
        self.assertTrue(self.access.enrollment_allowed('new_owner'));self.assertIsNone(self.access.user('new_owner'))
        for operation in [lambda:self.access.disable_user('new_owner',False),
                          lambda:self.access.issue_token('new_owner',{'jobs:read'}),
                          lambda:self.access.set_quota('new_owner',{'cpu':2}),
                          lambda:self.access.grant_environment('new_owner','host-python')]:
            with self.subTest(operation=operation),self.assertRaises(AccessError):operation()
        self.access.disable_user('new_owner');self.assertEqual(self.access.registration('new_owner')['state'],'staged')
        basic='Basic '+base64.b64encode(b'new_owner:anything').decode()
        self.assertIsNone(self.access.authenticate(basic))

    def test_parallel_staging_converges_to_one_disabled_definition(self):
        with ThreadPoolExecutor(4) as pool:results=list(pool.map(lambda _:self.stage(),range(8)))
        self.assertEqual(sum(not r['reused'] for r in results),1)
        self.assertEqual(len({r['stage_id'] for r in results}),1)
        with self.access.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users WHERE disabled=1').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tokens').fetchone()[0],0)

    def test_unknown_or_tampered_stage_remains_closed_even_if_disabled_bit_is_cleared(self):
        self.stage()
        for marker in [None,{}, {'format':'future/v9','state':'ready','id':'0'*64},
                       {'format':owner_identity.FORMAT,'state':'staged','id':'0'*64}]:
            with self.subTest(marker=marker):
                with self.access.connect() as db:
                    profile=json.loads(db.execute('SELECT profile FROM users').fetchone()[0])
                    profile['provisioning']=marker
                    db.execute('UPDATE users SET profile=?,disabled=0',(json.dumps(profile),))
                self.assertIsNone(self.access.user('new_owner'));self.assertFalse(self.access.enrollment_allowed('new_owner'))
                self.assertEqual(self.access.registration('new_owner')['state'],'invalid-provisioning')
                with self.assertRaises(AccessError):self.access.issue_token('new_owner',{'jobs:read'})

    def test_invalid_requests_do_not_leave_an_identity(self):
        for environments in [None,[],['host-python'],['worker-python','worker-python'],[{}],{'worker-python':1}]:
            with self.subTest(environments=environments),self.assertRaises(AccessError):self.stage(environments=environments)
        for quota in [[],{'cpu':True},{'cpu':-1},{'unknown':1},{'cpu':1.5}]:
            with self.subTest(quota=quota),self.assertRaises(AccessError):self.stage(quota=quota)
        with self.access.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)

    def test_legacy_disabled_user_is_not_a_staged_enrollment(self):
        password=self.access.create_user('legacy',self.workspace,environments=['worker-python'])
        token=self.access.issue_token('legacy',{'jobs:read'});self.access.disable_user('legacy')
        self.assertFalse(self.access.enrollment_allowed('legacy'))
        self.access.disable_user('legacy',False)
        self.assertTrue(self.access.enrollment_allowed('legacy'))
        self.assertIsNone(self.access.authenticate('Bearer '+token))
        header='Basic '+base64.b64encode(('legacy:'+password).encode()).decode()
        self.assertIsNotNone(self.access.authenticate(header))

    def test_token_issue_serializes_with_disable_and_reenable_never_revives_it(self):
        self.access.create_user('legacy',self.workspace)
        read=threading.Event();resume=threading.Event();write=threading.Event();disabled=threading.Event()
        original_user=self.access.user;original_connect=self.access.connect
        def gated_user(user):
            principal=original_user(user)
            if threading.current_thread().name.startswith('issuer'):
                read.set()
                if not resume.wait(3):raise RuntimeError('Issuer did not resume')
            return principal
        def traced_connect():
            db=original_connect()
            if threading.current_thread().name.startswith('disabler'):
                db.set_trace_callback(lambda sql:write.set() if sql.startswith('UPDATE users') else None)
            return db
        def disable():self.access.disable_user('legacy');disabled.set()
        with patch.object(self.access,'user',side_effect=gated_user),patch.object(self.access,'connect',side_effect=traced_connect):
            with ThreadPoolExecutor(1,thread_name_prefix='issuer') as issuer,ThreadPoolExecutor(1,thread_name_prefix='disabler') as disabler:
                issuing=issuer.submit(self.access.issue_token,'legacy',{'jobs:read'})
                try:
                    self.assertTrue(read.wait(3));disabling=disabler.submit(disable)
                    self.assertTrue(write.wait(3));disabled.wait(.1)
                finally:resume.set()
                token=issuing.result(3);disabling.result(3)
        self.access.disable_user('legacy',False)
        self.assertIsNone(self.access.authenticate('Bearer '+token))
        with self.access.connect() as db:
            self.assertEqual(db.execute('SELECT revoked FROM tokens WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0],1)

    def test_restore_rebases_stage_id_and_downgrade_stays_blocked(self):
        first=self.stage();self.access.close()
        require_features(self.state,'controller',{'files':{'owner_identity.py':{}}})
        with self.assertRaisesRegex(DeployError,'Staged owner'):require_features(self.state,'controller',{'files':{}})
        relocated=self.root/'restored'
        relocate(self.state,self.state,relocated,'controller')
        with sqlite3.connect(self.state/'identities.sqlite3') as db:
            profile,disabled=db.execute('SELECT profile,disabled FROM users').fetchone();profile=json.loads(profile)
        self.assertEqual(disabled,1);self.assertTrue(owner_identity.staged('new_owner',profile))
        self.assertNotEqual(first['stage_id'],profile['provisioning']['id'])
        self.assertEqual(profile['workspace'],str(relocated/'users/new_owner/workspace'))

    def test_restore_refuses_tampered_stage_instead_of_blessing_it(self):
        self.stage();self.access.close()
        with sqlite3.connect(self.state/'identities.sqlite3') as db:
            profile=json.loads(db.execute('SELECT profile FROM users').fetchone()[0])
            profile['quota']['cpu']=99
            db.execute('UPDATE users SET profile=?',(json.dumps(profile),))
        with self.assertRaisesRegex(DeployError,'Invalid staged owner'):
            relocate(self.state,self.state,self.root/'restored','controller')
        with sqlite3.connect(self.state/'identities.sqlite3') as db:
            self.assertEqual(json.loads(db.execute('SELECT profile FROM users').fetchone()[0]),profile)

    def test_packaged_cli_stages_without_export_and_rejects_workspace_symlink(self):
        archive=self.root/'package.tar.gz';bundle(archive);release,manifest=unpack_release(archive,self.root)
        self.assertIn('owner_identity.py',CORE);self.assertIn('owner_identity.py',manifest['files'])
        spec=self.root/'stage-spec.json';atomic_json(spec,{'user':'cli_owner','environments':['worker-python'],'quota':{'cpu':1}})
        def cli(*args):
            return subprocess.run(['/usr/bin/python3','-B',str(release/'accessctl'),'--state-dir',str(self.state),*args],
                                  capture_output=True,text=True,timeout=15)
        result=cli('user-stage','--spec',str(spec));self.assertEqual(result.returncode,0,result.stderr)
        response=json.loads(result.stdout);self.assertFalse(response['credentials_issued'])
        self.assertFalse((self.state/'personas').exists())
        self.assertEqual(json.loads(cli('user-status','cli_owner').stdout)['state'],'staged')
        self.assertNotEqual(cli('user-enable','cli_owner').returncode,0)
        self.assertTrue(json.loads(cli('user-stage','--spec',str(spec)).stdout)['reused'])
        spec.chmod(0o644);self.assertNotEqual(cli('user-stage','--spec',str(spec)).returncode,0)
        spec.chmod(0o600);atomic_json(spec,{'user':'linked','environments':['worker-python'],'quota':{}})
        (self.state/'users/linked').symlink_to(self.workspace)
        self.assertNotEqual(cli('user-stage','--spec',str(spec)).returncode,0)
        with self.assertRaises(AccessError):self.access.registration('linked')


if __name__=='__main__':unittest.main()

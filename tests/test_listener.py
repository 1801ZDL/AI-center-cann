"""Native TLS validation, portable CA trust and nonblocking handshakes."""
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from listener import assess,runtime,TLSHTTPServer,ListenerError
from lab_client import Client,ClientError,atomic_json,private_profile,transport_profile,resolve_ca
from jobs import ROOT
from maintenance import relocate

class ListenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-listener-pki-');cls.pki=Path(cls.temp.name)
        def openssl(*args):subprocess.run(['openssl',*args],capture_output=True,check=True,timeout=15)
        openssl('req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=TestOnlyCA',
          '-addext','basicConstraints=critical,CA:TRUE','-keyout',str(cls.pki/'ca.key'),'-out',str(cls.pki/'ca.pem'))
        openssl('req','-new','-newkey','rsa:2048','-nodes','-subj','/CN=controller.test',
          '-keyout',str(cls.pki/'server.key'),'-out',str(cls.pki/'server.csr'))
        ext=cls.pki/'server.ext';ext.write_text('subjectAltName=DNS:controller.test,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n')
        openssl('x509','-req','-in',str(cls.pki/'server.csr'),'-CA',str(cls.pki/'ca.pem'),'-CAkey',str(cls.pki/'ca.key'),
          '-CAcreateserial','-out',str(cls.pki/'server.pem'),'-days','1','-extfile',str(ext))
        openssl('pkey','-in',str(cls.pki/'server.key'),'-aes-256-cbc','-passout','pass:test-only','-out',str(cls.pki/'encrypted.key'))
        openssl('x509','-req','-in',str(cls.pki/'server.csr'),'-CA',str(cls.pki/'ca.pem'),'-CAkey',str(cls.pki/'ca.key'),
          '-set_serial','2','-out',str(cls.pki/'renewed.pem'),'-days','1','-extfile',str(ext))
        openssl('x509','-req','-in',str(cls.pki/'server.csr'),'-CA',str(cls.pki/'ca.pem'),'-CAkey',str(cls.pki/'ca.key'),
          '-set_serial','3','-out',str(cls.pki/'no-san.pem'),'-days','1')
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.tempdir=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-listener-');self.directory=Path(self.tempdir.name)
        for name in ['server.pem','server.key','ca.pem','ca.key','encrypted.key','renewed.pem','no-san.pem']:shutil.copyfile(self.pki/name,self.directory/name)
        for path in self.directory.iterdir():path.chmod(0o600)
    def tearDown(self):self.tempdir.cleanup()
    def assess(self,**changes):
        params={'bind_address':'0.0.0.0','port':18765,'public_url':'https://controller.test:18765',
                'tls_cert':str(self.directory/'server.pem'),'tls_key':str(self.directory/'server.key'),'tls_ca':str(self.directory/'ca.pem')}
        return assess(**{**params,**changes})
    def test_loopback_plaintext_compatibility_and_remote_plaintext_rejection(self):
        report,context,materials=assess('127.0.0.1',18765)
        self.assertEqual(report['scheme'],'http');self.assertIsNone(context);self.assertFalse(materials)
        for bind in ['0.0.0.0','192.0.2.1','::1','localhost','224.0.0.1']:
            with self.assertRaises(ListenerError):assess(bind,18765)
    def test_certificate_key_chain_and_hostname_verified_without_mutation_or_network(self):
        before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        report,context,materials=self.assess()
        self.assertTrue(report['hostname_and_chain_verified']);self.assertTrue(report['key_pair_verified'])
        self.assertEqual(report['validation_network_connections'],0);self.assertGreaterEqual(context.minimum_version,ssl.TLSVersion.TLSv1_2)
        self.assertNotIn('BEGIN',json.dumps(report));self.assertEqual(set(materials),{'certificate','private_key','authority'})
        self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.directory.iterdir()})
    def test_wrong_hostname_untrusted_chain_wrong_key_and_encrypted_key_rejected(self):
        for change in [{'public_url':'https://wrong.test:18765'},{'tls_ca':None},
                       {'tls_key':str(self.directory/'ca.key')},{'tls_key':str(self.directory/'encrypted.key')}]:
            with self.subTest(change=change),self.assertRaises(ListenerError):self.assess(**change)
    def test_incomplete_or_credential_bearing_listener_options_rejected(self):
        for change in [{'tls_key':None},{'public_url':None},{'public_url':'http://controller.test:18765'},
                       {'public_url':'https://user:private@controller.test:18765'},{'public_url':'https://controller.test/api-prefix'}]:
            with self.subTest(change=change),self.assertRaises(ListenerError):self.assess(**change)
    def test_world_readable_private_key_and_symlink_or_unsafe_parent_rejected(self):
        key=self.directory/'server.key';key.chmod(0o644)
        with self.assertRaises(ListenerError):self.assess()
        key.chmod(0o600);alias=self.directory/'alias.key';alias.symlink_to(key)
        with self.assertRaises(ListenerError):self.assess(tls_key=str(alias))
        self.directory.chmod(0o777)
        try:
            with self.assertRaises(ListenerError):self.assess()
        finally:self.directory.chmod(0o700)
    def test_runtime_rejects_unapproved_certificate_rotation(self):
        report,_,_=self.assess()
        config={'host':'0.0.0.0','port':18765,'controller':'https://controller.test:18765',
          'tls':{'certificate':str(self.directory/'server.pem'),'private_key':str(self.directory/'server.key'),
                 'authority':str(self.directory/'ca.pem'),'material_sha256':report['material_sha256']}}
        self.assertEqual(runtime(config)[0]['material_sha256'],report['material_sha256'])
        config['tls']['material_sha256']='0'*64
        with self.assertRaises(ListenerError):runtime(config)
    def test_relative_ca_profiles_survive_directory_transfer(self):
        original=self.directory/'profile';original.mkdir(mode=0o700)
        transport=transport_profile({'controller':'https://controller.test:18765','ca_file':str(self.directory/'ca.pem')},original)
        self.assertEqual(transport['ca_file'],'controller-ca.pem')
        atomic_json(original/'client.json',{'token':'test-only-token',**transport})
        moved=self.directory/'moved';shutil.move(original,moved)
        config=private_profile(moved/'client.json');self.assertEqual(config['ca_file'],str(moved/'controller-ca.pem'))
        self.assertEqual(Client.from_profile(moved/'client.json').url,'https://controller.test:18765')
        self.assertNotIn('PRIVATE KEY',(moved/'controller-ca.pem').read_text())
        with self.assertRaises(ClientError):resolve_ca({'ca_file':'../outside.pem'},moved/'client.json')
    def test_slow_tls_connection_does_not_block_other_clients_and_untrusted_client_fails(self):
        _,context,_=self.assess();seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'));data=b'{"passed":true}'
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        server=TLSHTTPServer(('127.0.0.1',0),Handler,context,max_connections=4)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();slow=socket.create_connection(server.server_address,timeout=2)
        try:
            url='https://127.0.0.1:'+str(server.server_address[1])
            with self.assertRaises(ClientError):Client({'controller':url,'token':'untrusted-test-only'}).request('/api/me')
            self.assertFalse(seen)
            start=time.monotonic();client=Client({'controller':url,'token':'trusted-test-only','ca_file':str(self.directory/'ca.pem')})
            self.assertTrue(client.request('/api/me')['passed']);self.assertLess(time.monotonic()-start,3)
            self.assertEqual(seen,['Bearer trusted-test-only'])
        finally:slow.close();server.shutdown();server.server_close();thread.join(timeout=3)
    def test_restore_relocates_tls_paths_but_preserves_public_origin(self):
        state=self.directory/'restore';state.mkdir();old='/var/lib/old';new='/var/lib/new'
        atomic_json(state/'config.json',{'controller':'https://controller.test:18765','ca_file':old+'/tls/authority.pem',
          'monitor_local':False,'identity_workspace_root':old+'/users','scheduler':{'local_enabled':False,'workspace_root':old+'/users'},
          'tls':{'certificate':old+'/tls/certificate.pem','private_key':old+'/tls/private_key.pem','authority':old+'/tls/authority.pem','material_sha256':'0'*64}})
        relocate(state,old,new,'controller');config=json.loads((state/'config.json').read_text())
        self.assertEqual(config['controller'],'https://controller.test:18765');self.assertEqual(config['ca_file'],new+'/tls/authority.pem')
        self.assertEqual(config['tls']['private_key'],new+'/tls/private_key.pem')
    def rotation_state(self):
        from unittest.mock import patch
        from contextlib import ExitStack
        state=self.directory/'managed';state.mkdir(mode=0o700);tls=state/'tls';tls.mkdir(mode=0o700)
        for source,target in [('server.pem','certificate.pem'),('server.key','private_key.pem'),('ca.pem','authority.pem')]:
            shutil.copyfile(self.directory/source,tls/target);(tls/target).chmod(0o600)
        report,_,_=self.assess()
        config={'host':'0.0.0.0','port':18765,'controller':'https://controller.test:18765',
          'tls':{'certificate':str(tls/'certificate.pem'),'private_key':str(tls/'private_key.pem'),
                 'authority':str(tls/'authority.pem'),'material_sha256':report['material_sha256']},'ca_file':str(tls/'authority.pem')}
        atomic_json(state/'config.json',config)
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch('deployment.require_root'))
        stack.enter_context(patch('deployment.verify',return_value=(self.directory,{'role':'controller','state_dir':str(state),'uid':os.getuid(),'gid':os.getgid()},self.directory)))
        return state,config
    def test_renewal_preserves_existing_material_and_is_idempotent(self):
        from listener import rotate
        state,config=self.rotation_state();runtime(config)
        result=rotate(str(self.directory),str(self.directory/'renewed.pem'),str(self.directory/'server.key'),str(self.directory/'ca.pem'))
        self.assertTrue(result['rotated']);self.assertFalse(result['authority_changed'])
        changed=json.loads((state/'config.json').read_text());self.assertNotEqual(changed['tls']['certificate'],config['tls']['certificate'])
        self.assertTrue(Path(config['tls']['private_key']).is_file());runtime(changed)
        again=rotate(str(self.directory),str(self.directory/'renewed.pem'),str(self.directory/'server.key'),str(self.directory/'ca.pem'))
        self.assertFalse(again['rotated'])
    def test_renewal_rejects_a_live_state_and_different_trust_without_changing_config(self):
        from listener import rotate
        from state_lock import state_lock
        state,config=self.rotation_state();before=(state/'config.json').read_bytes()
        with state_lock(state):
            with self.assertRaises(RuntimeError):rotate(str(self.directory),str(self.directory/'server.pem'),str(self.directory/'server.key'),str(self.directory/'ca.pem'))
        alternate=self.directory/'alternate-ca.pem';alternate.write_bytes((self.directory/'ca.pem').read_bytes()+b'\n');alternate.chmod(0o600)
        with self.assertRaisesRegex(ListenerError,'Trust authority changes'):
            rotate(str(self.directory),str(self.directory/'server.pem'),str(self.directory/'server.key'),str(alternate))
        self.assertEqual((state/'config.json').read_bytes(),before)
    def test_cn_only_certificate_and_concurrent_material_change_are_rejected(self):
        from unittest.mock import patch
        import listener
        with self.assertRaises(ListenerError):self.assess(tls_cert=str(self.directory/'no-san.pem'))
        original=listener.verify_name
        def interleave(*args):
            value=original(*args);path=self.directory/'server.key';path.write_bytes(path.read_bytes()+b'\n');return value
        with patch('listener.verify_name',side_effect=interleave):
            with self.assertRaises(ListenerError):self.assess()
    def test_invalid_ca_registration_does_not_create_node_or_user(self):
        from jobs import JobStore
        state=self.directory/'controller';state.mkdir(mode=0o700)
        config={'username':'tls-admin','password':'test-only','container':'unused','host':'127.0.0.1','port':18765,
                'controller':'https://controller.test:18765','ca_file':str(self.directory/'missing-ca.pem')}
        atomic_json(state/'config.json',config);jobs=JobStore(state,config)
        home=state/'alice';home.mkdir();jobs.access.create_user('alice',home,environments=['worker-python'])
        done=subprocess.run([str(ROOT/'workerctl'),'--controller-state',str(state),'enroll','--name','bad-ca',
          '--cpus',str(jobs.policy['cpu_ids'][0]),'--image','sha256:'+'a'*64,'--user-map','alice:23631:23631'],capture_output=True)
        self.assertNotEqual(done.returncode,0)
        with jobs.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM worker_nodes').fetchone()[0],0)
        added=subprocess.run([str(ROOT/'accessctl'),'--state-dir',str(state),'user-add','new_tls_user'],capture_output=True)
        self.assertNotEqual(added.returncode,0);self.assertIsNone(jobs.access.user('new_tls_user'))
    def test_bad_relative_ca_preflight_stays_json_and_never_serializes_node_key(self):
        from preflight import plan
        config=self.directory/'node.json';secret='private-node-key-test-only-'+('q'*32)
        atomic_json(config,{'node_id':'worker-'+'a'*12,'node_key':secret,'controller':'https://controller.test:18765',
          'ca_file':'../invalid.pem','physical_id':'0'*64,'policy':{'cpu_ids':[0],'memory_mib':512,'shm_mib':0,'max_running':1},
          'users':{'alice':{'uid':23631,'gid':23631}}})
        report=plan('node',node_config=str(config))
        self.assertFalse(report['install_ready']);self.assertIn('node_controller',report['blockers'])
        self.assertNotIn(secret,json.dumps(report));self.assertTrue(config.exists())

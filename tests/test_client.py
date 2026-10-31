"""Real HTTP/TLS boundaries of the separately distributable client; no task execution."""
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab_client import Client,ClientError,ROOT,atomic_json,controller_url,private_profile
from worker import NodeClient


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='client-test-')
        self.directory=Path(self.temp.name)
        self.servers=[]
        self.seen=[]

    def tearDown(self):
        for server,thread in self.servers:
            server.shutdown();server.server_close();thread.join(timeout=2)
        self.temp.cleanup()

    def server(self,tls=None):
        seen=self.seen
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append((self.path,self.headers.get('Authorization')))
                if self.path in {'/api/redirect','/api/worker/heartbeat'}:
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:'+str(self.server.server_port)+'/api/stolen')
                    self.end_headers();return
                if self.path=='/api/error':
                    self.send_response(401);self.end_headers();self.wfile.write(b'{"error":"do-not-echo-secret"}');return
                self.send_response(200);self.end_headers()
                self.wfile.write(b'x'*1000 if self.path=='/api/large' else b'{"id":"alice"}')
            do_POST=do_GET
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        if tls:server.socket=tls.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.servers.append((server,thread))
        return ('https' if tls else 'http')+'://127.0.0.1:'+str(server.server_port)

    def test_insecure_or_credential_bearing_urls_are_rejected(self):
        for value in ['http://lab.example.org','http://localhost:18765','https://user:pass@lab.test',
                      'https://lab.test/a/../b','https://lab.test?token=secret','https://lab.test/#fragment',
                      'https://lab.test/%2e%2e','https://lab.test\\@other','https://lab.test\n',
                      'ftp://127.0.0.1','http://127.0.0.1:0']:
            with self.subTest(url=value),self.assertRaises(ClientError):controller_url({'controller':value})
        self.assertEqual(controller_url({'controller':'http://[::1]:1234'}),'http://[::1]:1234')
        self.assertEqual(controller_url({'controller':'https://lab.test/prefix/'}),'https://lab.test/prefix')
        self.assertEqual(controller_url({'host':'127.0.0.1','port':18765}),'http://127.0.0.1:18765')

    def test_profile_must_be_private_regular_and_not_a_link(self):
        path=self.directory/'profile.json';atomic_json(path,{'controller':'https://lab.test','token':'test-token'})
        self.assertEqual(private_profile(path)['token'],'test-token')
        path.chmod(0o644)
        with self.assertRaises(ClientError):private_profile(path)
        path.chmod(0o600)
        link=self.directory/'link';link.symlink_to(path)
        with self.assertRaises(ClientError):private_profile(link)
        link.unlink();os.link(path,link)
        with self.assertRaises(ClientError):private_profile(path)

    def test_redirect_never_forwards_credentials_and_errors_never_echo_response(self):
        client=Client({'controller':self.server(),'token':'test-token'})
        self.assertEqual(client.request('/api/me'),{'id':'alice'})
        with self.assertRaisesRegex(ClientError,'redirect refused'):client.request('/api/redirect')
        self.assertNotIn('/api/stolen',[p for p,_ in self.seen])
        with self.assertRaisesRegex(ClientError,'^Controller HTTP 401$'):client.request('/api/error')
        self.assertTrue(all(auth=='Bearer test-token' for _,auth in self.seen))

    def test_response_limit_and_header_injection_are_rejected(self):
        client=Client({'controller':self.server(),'token':'test-token'})
        with self.assertRaisesRegex(ClientError,'exceeds'):client.request('/api/large',max_bytes=100)
        with self.assertRaises(ClientError):Client({'controller':client.url,'token':'bad\r\nheader'})
        for path in ['//evil.test/api/me','http://evil.test/api/me','/api/\nAuthorization:x']:
            with self.assertRaises(ClientError):client.request(path)

    def test_node_transport_uses_same_redirect_boundary_with_distinct_key(self):
        node=NodeClient({'controller':self.server(),'node_key':'node-private-test'})
        with self.assertRaisesRegex(RuntimeError,'redirect refused'):node.call('heartbeat',{})
        self.assertEqual(self.seen,[('/api/worker/heartbeat','Bearer node-private-test')])

    def test_tls_verification_requires_trusted_ca(self):
        key=self.directory/'key.pem';certificate=self.directory/'cert.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1',
                        '-keyout',str(key),'-out',str(certificate)],capture_output=True,check=True)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certificate,key)
        url=self.server(context)
        with self.assertRaises(ClientError):Client({'controller':url,'token':'test-token'}).request('/api/me')
        self.assertFalse(self.seen,'Untrusted TLS must not send an HTTP Authorization header')
        client=Client({'controller':url,'token':'test-token','ca_file':str(certificate)})
        self.assertEqual(client.request('/api/me')['id'],'alice')

    def test_portable_installation_contains_no_controller_or_credentials(self):
        target=self.directory/'client'
        result=subprocess.run([str(ROOT/'install-client'),'--prefix',str(target)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(set(p.name for p in target.iterdir()),
                         {'labctl','lab_client.py','io_policy.py','client_archive.py','diagnostic_case.py','accuracy_data.py','accuracy_diagnostic.py','ACCURACY.md','compiler_diagnostic.py','compiler_compatibility.py','COMPILER_DIAGNOSTICS.md','operator_profile.py','operator_node.py','operator_workflow.py','OPERATOR_ANALYSIS.md','mcp_bridge.py','CLIENT.md','WORKFLOWS.md','WORKSPACE.md','DEPENDENCIES.md','STORAGE.md','EXPERIMENT_BUDGET.md','manifest.json','client-only'})
        profile=self.directory/'outside-profile.json'
        atomic_json(profile,{'controller':self.server(),'token':'test-token'})
        response=subprocess.run([str(target/'labctl'),'--profile',str(profile),'me'],capture_output=True,text=True)
        self.assertEqual(response.returncode,0,response.stderr)
        self.assertEqual(json.loads(response.stdout)['id'],'alice')
        denied=subprocess.run([str(target/'labctl'),'start'],capture_output=True,text=True)
        self.assertNotEqual(denied.returncode,0)
        self.assertFalse((target/'runtime').exists())
        second=subprocess.run([str(ROOT/'install-client'),'--prefix',str(target)],capture_output=True,text=True)
        self.assertNotEqual(second.returncode,0)


if __name__=='__main__':unittest.main()

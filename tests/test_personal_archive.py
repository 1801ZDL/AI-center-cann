"""Real authenticated HTTP archive, interrupted download, ACL and local publication boundaries."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from archive_api import manifest
from client_archive import CHUNK, checked_manifest, download, remember_input, remember_submission
from jobs import JobStore, JobError, ROOT, atomic_json
from lab_client import Client, ClientError
from server import ThreadingHTTPServer, handler_class


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime', prefix='archive-test-')
        self.base=Path(self.temp.name)
        self.config={'username':'archive-admin','password':'private-admin','container':'zdl'}
        self.jobs=JobStore(self.base/'central', self.config)
        self.credentials={}
        for identity in ['alice','bob']:
            workspace=self.base/identity;workspace.mkdir()
            self.credentials[identity]=self.jobs.access.create_user(identity, workspace)
        self.alice=self.jobs.access.user('alice')
        job,_=self.jobs.submit({'template':'cpu_demo','parameters':{'seconds':1}}, self.alice)
        self.identifier=job['id'];self.jobs.cancel(self.identifier, self.alice)
        self.job=self.jobs.get(self.identifier, principal=self.alice);self.central=self.jobs.directory(self.identifier)
        (self.central/'results').mkdir(exist_ok=True)
        self.data=bytes(range(256))*(CHUNK*3//256)+b'final bytes'
        (self.central/'results/payload.bin').write_bytes(self.data)
        (self.central/'stderr.log').write_bytes(b'expected failure detail\n')
        atomic_json(self.central/'artifacts.json', {'files':[{'name':name,'bytes':(self.central/name).stat().st_size}
                    for name in ['result.json','stderr.log','results/payload.bin']]})
        self.server=ThreadingHTTPServer(('127.0.0.1',0), handler_class(Mock(),self.config,self.jobs))
        self.thread=threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        self.profiles={};self.clients={}
        for identity in ['alice','bob']:
            config={'controller':'http://127.0.0.1:'+str(self.server.server_port),
                    'username':identity,'password':self.credentials[identity]}
            path=self.base/(identity+'.json');atomic_json(path, config)
            self.profiles[identity]=path;self.clients[identity]=Client(config)
        self.client=self.clients['alice'];self.profile=self.profiles['alice']

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.temp.cleanup()

    def target(self, root):return root/self.identifier

    def test_sibling_default_and_failed_job_logs_with_idempotent_verified_reuse(self):
        source=self.base/'personal/run.py';source.parent.mkdir();source.write_text('raise RuntimeError()\n')
        remember_input(self.profile,self.client,source,{'id':'input-local'})
        remember_submission(self.profile,self.client,self.job,input_id='input-local',fallback=self.base)
        report=download(self.client,self.identifier,profile=self.profile)
        target=source.parent/'.ascend-lab-results'/self.identifier
        self.assertEqual(report['directory'],str(target));self.assertTrue(report['complete'])
        self.assertEqual(report['job_state'],'cancelled')
        self.assertEqual((target/'results/payload.bin').read_bytes(),self.data)
        self.assertEqual((target/'stderr.log').read_text(),'expected failure detail\n')
        self.assertEqual(target.stat().st_mode&0o777,0o700)
        self.assertEqual((target/'results/payload.bin').stat().st_mode&0o777,0o600)
        repeated=download(self.client,self.identifier,profile=self.profile)
        self.assertEqual(repeated['bytes_downloaded'],0)
        self.assertEqual(repeated['files_verified'],report['files_verified'])

    def test_disconnect_resumes_same_archive_from_persisted_offset(self):
        root=self.base/'resumable';original=self.client.request;seen=[]
        def interrupted(path,*args,**kwargs):
            if 'artifact-chunk?' in path and 'payload.bin' in path:
                seen.append(path)
                if len(seen)==2:raise ClientError('Injected transport interruption')
            return original(path,*args,**kwargs)
        with patch.object(self.client,'request',side_effect=interrupted):
            with self.assertRaisesRegex(ClientError,'interruption'):download(self.client,self.identifier,root=root)
        self.assertFalse((self.target(root)/'.lab-complete.json').exists())
        self.assertFalse((self.target(root)/'results/payload.bin').exists())
        report=download(self.client,self.identifier,root=root)
        self.assertEqual(report['bytes_resumed'],CHUNK)
        self.assertEqual((self.target(root)/'results/payload.bin').read_bytes(),self.data)

    def test_corrupt_response_is_never_published(self):
        original=self.client.request;root=self.base/'corrupt'
        def corrupt(path,*args,**kwargs):
            response=original(path,*args,**kwargs)
            if 'artifact-chunk?' in path:response['sha256']='0'*64
            return response
        with patch.object(self.client,'request',side_effect=corrupt):
            with self.assertRaisesRegex(ClientError,'range'):download(self.client,self.identifier,root=root)
        self.assertFalse((self.target(root)/'.lab-complete.json').exists())
        self.assertFalse((self.target(root)/'result.json').exists())

    def test_existing_different_file_is_never_overwritten(self):
        root=self.base/'conflict';download(self.client,self.identifier,root=root)
        path=self.target(root)/'results/payload.bin';path.write_bytes(b'USER EDIT')
        with self.assertRaisesRegex(ClientError,'no overwrite'):download(self.client,self.identifier,root=root)
        self.assertEqual(path.read_bytes(),b'USER EDIT')

    def test_symlink_ancestors_and_hardlinks_are_refused(self):
        outside=self.base/'outside';outside.mkdir();alias=self.base/'alias';alias.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ClientError):download(self.client,self.identifier,root=alias)
        self.assertFalse((outside/self.identifier).exists())
        root=self.base/'links';download(self.client,self.identifier,root=root)
        payload=self.target(root)/'results/payload.bin';os.link(payload,outside/'hardlink')
        with self.assertRaisesRegex(ClientError,'hard-linked'):download(self.client,self.identifier,root=root)

    def test_concurrent_download_lock_and_publication_crash_recovery(self):
        root=self.base/'lock';download(self.client,self.identifier,root=root);target=self.target(root)
        with (target/'.lab-lock').open('r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ClientError,'already being downloaded'):download(self.client,self.identifier,root=root)
        partial=hashlib.sha256(b'results/payload.bin').hexdigest()+'.part'
        os.link(target/'results/payload.bin',target/'.lab-partial'/partial)
        report=download(self.client,self.identifier,root=root)
        self.assertEqual(report['bytes_downloaded'],0);self.assertFalse((target/'.lab-partial'/partial).exists())
        self.assertEqual((target/'results/payload.bin').stat().st_nlink,1)

    def test_acl_active_unknown_and_out_of_bounds_ranges_are_rejected(self):
        for suffix in ['/archive-manifest','/artifact-chunk?name=result.json']:
            with self.assertRaisesRegex(ClientError,'HTTP 404'):self.clients['bob'].request('/api/jobs/'+self.identifier+suffix)
        for state in ['running','needs_attention']:
            changed={**self.job,'state':state}
            with self.assertRaises(JobError):manifest(self.central,changed)
        for query in ['offset=-1','length=262145','offset=999999999']:
            with self.assertRaises(ClientError):self.client.request('/api/jobs/'+self.identifier+'/artifact-chunk?name=result.json&'+query)

    def test_remote_index_cannot_escape_through_directory_link(self):
        outside=self.base/'outside-remote';outside.mkdir();(outside/'secret').write_text('private')
        (self.central/'escape').symlink_to(outside,target_is_directory=True)
        atomic_json(self.central/'artifacts.json',{'files':[{'name':'escape/secret','bytes':7}]})
        for suffix in ['/archive-manifest','/artifact-chunk?name=escape%2Fsecret']:
            with self.assertRaisesRegex(ClientError,'HTTP 404'):self.client.request('/api/jobs/'+self.identifier+suffix)

    def test_manifest_paths_and_identity_binding_are_checked_before_overwrite(self):
        valid=self.client.request('/api/jobs/'+self.identifier+'/archive-manifest')
        for name in ['../outside','/absolute','results/../escape','.lab-lock','results/x\n','results//x']:
            value=json.loads(json.dumps(valid));value['files'][0]['name']=name
            with self.assertRaises(ClientError):checked_manifest(value,self.identifier)
        root=self.base/'identity';download(self.client,self.identifier,root=root)
        self.jobs.access.add_member('shared','alice','editor');self.jobs.access.add_member('shared','bob','viewer')
        with self.jobs.transaction() as db:
            job=self.jobs.get(self.identifier,db,self.alice);job['project']='shared';self.jobs.write(db,job)
        with self.assertRaisesRegex(ClientError,'another identity'):download(self.clients['bob'],self.identifier,root=root)

    def test_installed_cli_preferences_and_terminal_archive(self):
        target=self.base/'standalone'
        subprocess.run([str(ROOT/'install-client'),'--prefix',str(target)],check=True,capture_output=True)
        chosen=self.base/'preferred'
        command=[str(target/'labctl'),'--profile',str(self.profile)]
        subprocess.run([*command,'archive-config','--root',str(chosen)],check=True,capture_output=True)
        completed=subprocess.run([*command,'archive',self.identifier],check=True,capture_output=True,text=True)
        self.assertEqual(json.loads(completed.stdout)['directory'],str(chosen/self.identifier))
        subprocess.run([*command,'archive-config','--sibling'],check=True,capture_output=True)
        self.assertNotIn('archive_root',json.loads(self.profile.read_text()))
        self.assertFalse((target/'runtime').exists())


if __name__=='__main__':unittest.main()

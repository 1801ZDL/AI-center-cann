"""Storage reservation authority, retained accounting, IO ceilings and real HTTP ranges."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,atomic_json
from server import ThreadingHTTPServer,handler_class
from io_policy import MIB,META,CAPABILITY
from workflow_spec import CAPABILITIES


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='storage-test-');self.root=Path(self.temp.name)
        self.config={'username':'storage-admin','password':'test-storage-admin','container':'zdl',
                     'scheduler':{'io_limits':{'file_mib':256,'input_mib':512,'artifact_mib':512}}}
        self.jobs=JobStore(self.root,self.config)
        for user in ['alice','bob']:
            work=self.root/user;work.mkdir();self.jobs.access.create_user(user,work,environments=['worker-python'])
        self.alice=self.jobs.access.user('alice');self.bob=self.jobs.access.user('bob')

    def tearDown(self):self.temp.cleanup()

    def submit(self,user=None,key='one'):
        return self.jobs.submit({'template':'cpu_demo','parameters':{'seconds':1},'environment':'worker-python','idempotency_key':key},user or self.alice)

    def amount(self,kind='archive',user=None):
        return self.jobs.storage.summary(user or self.alice)['personal_bytes'].get(kind,0)

    def test_queued_idempotent_unknown_and_terminal_credit(self):
        job,_=self.submit();before=self.amount();self.assertEqual(before,128*MIB+META)
        repeated,reused=self.submit();self.assertTrue(reused);self.assertEqual(repeated['id'],job['id']);self.assertEqual(self.amount(),before)
        with self.jobs.transaction() as db:
            value=self.jobs.get(job['id'],db);value['state']='needs_attention';self.jobs.write(db,value)
        self.assertEqual(self.amount(),before)
        with self.jobs.transaction() as db:
            value=self.jobs.get(job['id'],db);value['state']='queued';self.jobs.write(db,value)
        self.jobs.cancel(job['id'],self.alice)
        self.assertLess(self.amount(),before);self.assertGreaterEqual(self.amount(),META)
        restarted=JobStore(self.root,self.config)
        self.assertEqual(restarted.storage.summary(self.alice)['personal_bytes']['archive'],self.amount())

    def test_user_budget_rejects_before_creating_job_files(self):
        self.jobs.access.set_quota('alice',{'archive_storage_mib':128});self.alice=self.jobs.access.user('alice')
        with self.assertRaisesRegex(JobError,'storage budget'):self.submit()
        self.assertFalse(self.jobs.all());self.assertEqual(list(self.jobs.root.iterdir()),[])

    def test_global_budget_is_transactional_across_users(self):
        self.jobs.policy['storage_budget_mib']=200
        barrier=threading.Barrier(2)
        def create(user):
            barrier.wait()
            try:return self.submit(user)[0]['id']
            except JobError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:created=list(pool.map(create,[self.alice,self.bob]))
        self.assertEqual(sum(item is not None for item in created),1)
        self.assertEqual(self.amount(user=self.alice)+self.amount(user=self.bob),128*MIB+META)

    def test_large_inputs_reserve_declared_bytes_without_allocating_payload(self):
        self.jobs.access.set_quota('alice',{'input_storage_mib':200});self.alice=self.jobs.access.user('alice')
        bundle,_=self.jobs.inputs.create(self.alice,{'files':[{'name':'first.whl','size':150*MIB,'sha256':'a'*64}]})
        self.assertEqual(self.amount('input'),150*MIB)
        self.assertFalse((self.jobs.inputs.directory(bundle['id'])/'first.whl').exists())
        with self.assertRaisesRegex(JobError,'storage budget'):
            self.jobs.inputs.create(self.alice,{'files':[{'name':'second.whl','size':150*MIB,'sha256':'b'*64}]})

    def test_large_output_requires_matching_node_budget_and_capability(self):
        bundle,_=self.jobs.inputs.create(self.alice,{'files':[{'name':'empty','size':0,'sha256':hashlib.sha256(b'').hexdigest()}]})
        self.jobs.inputs.seal(bundle['id'],self.alice)
        request={'template':'build_v1','environment':'worker-python','parameters':{'input_id':bundle['id'],
            'argv':['python','build.py'],'lock':{'python':'3.10.12'},'artifacts':{'file_mib':256,'total_mib':256}}}
        job,_=self.jobs.submit(request,self.alice)
        profile={'name':'small-io','physical_id':self.jobs.backend.physical_id,'users':{'alice':{'uid':23001,'gid':23001}},
                 'templates':['build_v1'],'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1}}
        node,_=self.jobs.nodes.enroll(profile)
        def beat(identifier,io=None,caps=()):
            self.jobs.nodes.heartbeat(identifier,{'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
                'capabilities':['start-ticket-v1','cleanup-before-complete-v1',*CAPABILITIES,*caps],
                'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
                **({'io_limits':io} if io else {})})
        beat(node,caps=[CAPABILITY]);self.assertIsNone(self.jobs.nodes.claim(node)['grant'])
        io={'file_mib':256,'input_mib':512,'artifact_mib':512}
        large,_=self.jobs.nodes.enroll({**profile,'name':'large-io','io_limits':io})
        beat(large,io);self.assertIsNone(self.jobs.nodes.claim(large)['grant'])
        beat(large,io,[CAPABILITY]);grant=self.jobs.nodes.claim(large)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['artifact_limits'],{'file_mib':256,'total_mib':256})

    def test_browser_range_stream_uses_same_acl_and_exact_bytes(self):
        job,_=self.submit();self.jobs.cancel(job['id'],self.alice);directory=self.jobs.directory(job['id'])
        payload=bytes(range(256))*32;(directory/'payload.bin').write_bytes(payload)
        atomic_json(directory/'artifacts.json',{'files':[{'name':'payload.bin','bytes':len(payload)}]})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),self.config,self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def call(user,range):
            token=self.jobs.access.issue_token(user,{'jobs:read'})
            request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/jobs/'+job['id']+'/artifact?name=payload.bin',
                headers={'Authorization':'Bearer '+token,'Range':range})
            return opener.open(request,timeout=5)
        try:
            for value,expected in [('bytes=100-199',payload[100:200]),('bytes=-257',payload[-257:]),('bytes=8000-',payload[8000:])]:
                with call('alice',value) as response:self.assertEqual(response.status,206);self.assertEqual(response.read(),expected)
            for user,value,status in [('alice','bytes=-0',416),('alice','bytes=999999-',416),('bob','bytes=0-1',404)]:
                with self.assertRaises(urllib.error.HTTPError) as raised:call(user,value)
                self.assertEqual(raised.exception.code,status);raised.exception.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()

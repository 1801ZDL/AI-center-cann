"""Server-side multi-user and scope checks; no device operations."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from auth import AccessError
from jobs import JobStore, JobError, Scheduler, ROOT, atomic_json, now
from server import ThreadingHTTPServer, handler_class


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-identities-')
        self.runtime=Path(self.temp.name)
        self.config={'username':pwd.getpwuid(os.getuid()).pw_name,'password':'test-local-admin','container':'zdl'}
        self.jobs=JobStore(self.runtime,self.config)
        self.access=self.jobs.access
        self.passwords={}
        for user in ['alice','bob']:
            workspace=self.runtime/user;workspace.mkdir()
            self.passwords[user]=self.access.create_user(user,workspace)
        self.alice=self.access.user('alice');self.bob=self.access.user('bob')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),self.config,self.jobs))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def call(self,user,path,body=None,token=None):
        auth='Bearer '+token if token else 'Basic '+base64.b64encode((user+':'+self.passwords[user]).encode()).decode()
        request=urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':auth,'Content-Type':'application/json'})
        try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5)
        except urllib.error.HTTPError as error:response=error
        with response:return response.status,json.load(response)

    def spec(self,**kwargs):
        return {'template':'cpu_demo','parameters':{'seconds':1},'idempotency_key':'stable',**kwargs}

    def test_identity_scoped_submission_idempotency_and_impersonation(self):
        a,reused=self.jobs.submit(self.spec(),self.alice)
        self.assertFalse(reused)
        self.assertEqual(self.jobs.submit(self.spec(),self.alice)[0]['id'],a['id'])
        b,_=self.jobs.submit(self.spec(),self.bob)
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual((a['owner'],b['owner']),('alice','bob'))
        for changes in [{'owner':'bob'},{'argv':['sh','-c','cat /etc/passwd']},{'npu':1},
                        {'cwd':self.bob.workspace},{'environment':'zdl-python'},{'output':self.bob.workspace}]:
            with self.subTest(changes=changes),self.assertRaises(JobError):
                self.jobs.submit(self.spec(**changes),self.alice)

    def test_other_private_jobs_logs_artifacts_export_and_monitor_are_denied(self):
        a,_=self.jobs.submit(self.spec(name='PRIVATE_ALICE'),self.alice)
        directory=self.jobs.directory(a['id'])
        (directory/'stdout.log').write_text('PRIVATE_LOG')
        atomic_json(directory/'artifacts.json',{'files':[{'name':'stdout.log','bytes':11}]})
        for suffix in ['', '/log', '/artifact?name=stdout.log']:
            self.assertEqual(self.call('bob','/api/jobs/'+a['id']+suffix)[0],404)
        self.assertEqual(self.call('bob','/api/jobs/'+a['id']+'/cancel',{})[0],404)
        for path in ['/api/snapshot','/api/history','/api/events','/api/experiments','/api/audit']:
            self.assertEqual(self.call('bob',path)[0],403)
        for path in ['/api/jobs','/api/export','/api/activity']:
            status,body=self.call('bob',path)
            self.assertEqual(status,200)
            self.assertNotIn('PRIVATE',json.dumps(body))
            self.assertNotIn(self.alice.workspace,json.dumps(body))

    def test_project_viewer_reads_but_cannot_submit_or_cancel(self):
        self.access.add_member('shared','alice','editor')
        self.access.add_member('shared','bob','viewer')
        job,_=self.jobs.submit(self.spec(project='shared'),self.alice)
        status,detail=self.call('bob','/api/jobs/'+job['id'])
        self.assertEqual(status,200)
        self.assertFalse(detail['permissions']['cancel'])
        self.assertEqual(self.call('bob','/api/jobs/'+job['id']+'/cancel',{})[0],403)
        self.assertEqual(self.call('bob','/api/jobs',self.spec(project='shared'))[0],403)
        self.access.add_member('shared','bob','manager')
        self.assertEqual(self.call('bob','/api/jobs/'+job['id']+'/cancel',{})[0],200)
        self.access.remove_member('shared','bob')
        self.assertEqual(self.call('bob','/api/jobs/'+job['id'])[0],404)

    def test_bearer_scope_expiry_revocation_and_disabled_accounts(self):
        token=self.access.issue_token('alice',{'jobs:read'})
        self.assertEqual(self.call('alice','/api/jobs',token=token)[0],200)
        self.assertEqual(self.call('alice','/api/jobs',self.spec(),token=token)[0],403)
        self.access.revoke_token(token)
        self.assertEqual(self.call('alice','/api/jobs',token=token)[0],401)
        token=self.access.issue_token('alice',{'jobs:read'})
        with patch('auth.time.time',return_value=10**12):
            self.assertIsNone(self.access.authenticate('Bearer '+token))
        self.access.disable_user('alice')
        self.assertEqual(self.call('alice','/api/me')[0],401)
        self.assertEqual(self.call('alice','/api/me',token=token)[0],401)

    def test_staged_owner_http_rejects_basic_and_even_a_preexisting_token_record(self):
        path=self.runtime/'staged';path.mkdir()
        self.access.stage_user('staged',path);self.passwords['staged']='stage-http-test-only'
        token='stage-http-injected-record-only'
        import hashlib,time
        with self.access.connect() as db:
            db.execute('INSERT INTO tokens(digest,user,scopes,expires_at) VALUES(?,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(),'staged',json.dumps(['jobs:read','jobs:submit']),time.time()+60))
        for path,body in [('/api/me',None),('/api/jobs',self.spec())]:
            self.assertEqual(self.call('staged',path,body)[0],401)
            self.assertEqual(self.call('staged',path,body,token=token)[0],401)
        with self.access.connect() as db:db.execute('UPDATE users SET disabled=0 WHERE id=?',('staged',))
        self.assertEqual(self.call('staged','/api/me',token=token)[0],401)

    def test_parallel_requests_never_share_mutable_owner_context(self):
        tokens={user:self.access.issue_token(user,{'jobs:read','jobs:submit'}) for user in ['alice','bob']}
        def submit(index):
            user=['alice','bob'][index%2]
            status,body=self.call(user,'/api/jobs',self.spec(idempotency_key='key-'+str(index)),tokens[user])
            self.assertEqual(status,201)
            self.assertEqual(body['job']['owner'],user)
            return body['job']
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs=list(pool.map(submit,range(8)))
        self.assertEqual(len({job['id'] for job in jobs}),8)
        for user in ['alice','bob']:
            rows=self.jobs.summary(self.access.user(user))['jobs']
            self.assertEqual(len(rows),4)
            self.assertTrue(all(job['owner']==user for job in rows))

    def scheduler(self):
        snapshot={'sampled_at':now(),'host':{'memory_available_bytes':8*1024**3,
                  'workspace_disk':{'free_bytes':10*1024**3}},'npu':{'devices':[]}}
        observations=Mock();observations.latest.return_value=snapshot
        return Scheduler(self.jobs,observations,threading.Event())

    def test_per_user_concurrency_and_round_robin_admission(self):
        a,_=self.jobs.submit(self.spec(idempotency_key='a'),self.alice)
        a2,_=self.jobs.submit(self.spec(idempotency_key='a2'),self.alice)
        b,_=self.jobs.submit(self.spec(idempotency_key='b'),self.bob)
        scheduler=self.scheduler()
        with patch('jobs.subprocess.Popen') as launch:
            launch.return_value.poll.return_value=None
            scheduler.tick()
        self.assertEqual(self.jobs.get(a['id'])['state'],'reserved')
        with self.jobs.transaction() as db:
            row=self.jobs.get(a['id'],db);row['state']='succeeded';self.jobs.write(db,row)
        with patch('jobs.subprocess.Popen'):
            scheduler.tick()
        self.assertEqual(self.jobs.get(b['id'])['state'],'reserved')
        self.assertEqual(self.jobs.get(a2['id'])['state'],'queued')

    def test_revoked_project_permission_blocks_already_queued_execution(self):
        self.access.add_member('shared','alice','editor')
        job,_=self.jobs.submit(self.spec(project='shared'),self.alice)
        self.access.remove_member('shared','alice')
        with patch('jobs.subprocess.Popen') as launch:
            self.scheduler().tick()
        self.assertEqual(self.jobs.get(job['id'])['state'],'queued')
        self.assertIn('撤权',self.jobs.get(job['id'])['reason'])
        launch.assert_not_called()

    def test_user_concurrency_quota_is_independent_of_node_slots(self):
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(domain['physical_id'],{**domain['policy'],'max_running':2},domain['revision'])
        a,_=self.jobs.submit(self.spec(idempotency_key='a'),self.alice)
        a2,_=self.jobs.submit(self.spec(idempotency_key='a2'),self.alice)
        b,_=self.jobs.submit(self.spec(idempotency_key='b'),self.bob)
        with patch('jobs.subprocess.Popen'):
            self.scheduler().tick()
        self.assertEqual(self.jobs.get(a['id'])['state'],'reserved')
        self.assertEqual(self.jobs.get(b['id'])['state'],'reserved')
        self.assertEqual(self.jobs.get(a2['id'])['state'],'queued')
        self.assertIn('用户的运行名额',self.jobs.get(a2['id'])['reason'])

    def test_queue_limit_and_changed_quota_apply_on_the_server(self):
        self.access.set_quota('alice',{'max_queued':1})
        actor=self.access.user('alice')
        self.jobs.submit(self.spec(),actor)
        with self.assertRaises(JobError):self.jobs.submit(self.spec(idempotency_key='second'),actor)
        self.access.set_quota('bob',{'memory_mib':128})
        self.assertEqual(self.call('bob','/api/jobs',self.spec())[0],403)

    def test_node_overview_is_redacted_and_contains_no_private_paths(self):
        status,body=self.call('bob','/api/nodes')
        self.assertEqual(status,200)
        self.assertEqual(body['nodes'][0]['id'],self.jobs.backend.node_id)
        self.assertNotIn(self.alice.workspace,json.dumps(body))
        self.assertNotIn('processes',body['nodes'][0])


if __name__=='__main__':unittest.main()

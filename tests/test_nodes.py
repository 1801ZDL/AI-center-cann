"""Controller node authority, shared-host admission, artifact integrity and stale receipts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,Scheduler,now
from server import ThreadingHTTPServer,handler_class
from worker import NodeClient


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-nodes-')
        self.runtime=Path(self.temp.name)
        self.config={'username':pwd.getpwuid(os.getuid()).pw_name,'password':'admin-test-only','container':'zdl'}
        self.jobs=JobStore(self.runtime,self.config)
        for user in ['alice','bob']:
            workspace=self.runtime/user;workspace.mkdir()
            self.jobs.access.create_user(user,workspace,environments=['host-python','worker-python'])
        self.alice=self.jobs.access.user('alice');self.bob=self.jobs.access.user('bob')
        self.nodes=self.jobs.nodes
        self.profile={'name':'test-node','physical_id':self.jobs.backend.physical_id,
            'users':{'alice':{'uid':23001,'gid':23001},'bob':{'uid':23002,'gid':23002}},
            'templates':['cpu_demo'],'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':512,'shm_mib':0,'max_running':1}}
        self.a,self.key=self.nodes.enroll(self.profile)
        self.b,self.key_b=self.nodes.enroll({**self.profile,'name':'other-node'})
        self.beat(self.a);self.beat(self.b)

    def tearDown(self):self.temp.cleanup()

    def beat(self,node):
        self.nodes.heartbeat(node,{'physical_id':self.jobs.backend.physical_id,
            'protocol_version':1,'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})

    def submit(self,user=None):
        return self.jobs.submit({'template':'cpu_demo','parameters':{'seconds':1},'environment':'worker-python'},user or self.alice)[0]

    def auth(self,grant):return {k:grant[k] for k in ['job_id','attempt_id','lease_token']}

    def test_node_and_user_credentials_are_distinct_and_transport_requires_tls(self):
        token=self.jobs.access.issue_token('alice',{'jobs:read'})
        self.assertIsNone(self.nodes.authenticate('Bearer '+token))
        self.assertIsNone(self.jobs.access.authenticate('Bearer '+self.key))
        self.assertEqual(self.nodes.authenticate('Bearer '+self.key),self.a)
        with self.assertRaises(ValueError):NodeClient({'controller':'http://remote.example','node_key':'not-used'})
        with self.assertRaises(ValueError):NodeClient({'controller':'https://user:secret@remote.example','node_key':'not-used'})

    def test_claim_replay_is_one_attempt_and_task_has_no_mount_or_uid_controls(self):
        job=self.submit();first=self.nodes.claim(self.a)['grant'];again=self.nodes.claim(self.a)['grant']
        self.assertEqual(first,again)
        self.assertEqual(first['job_id'],job['id'])
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)
        self.assertFalse({'cwd','argv','uid','mounts','image'}&set(first))
        for extra in [{'uid':0},{'mounts':['/']},{'command':'sh'},{'image':'other'}]:
            with self.assertRaises(JobError):
                self.jobs.submit({'template':'cpu_demo','environment':'worker-python',**extra},self.alice)

    def test_pinned_runtime_capability_is_required_for_new_work_but_recovery_is_preserved(self):
        self.jobs.policy['require_pinned_docker']=True;job=self.submit()
        with self.assertRaisesRegex(JobError,'pinned-docker-runtime'):
            self.nodes.claim(self.a)
        self.assertEqual(self.jobs.get(job['id'])['state'],'queued');self.assertFalse(self.jobs.attempts(job['id']))
        self.nodes.heartbeat(self.a,{'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1','pinned-docker-runtime-v1'],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        grant=self.nodes.claim(self.a)['grant']
        self.beat(self.a) # Old capability receipt must not discard the existing attempt.
        self.assertEqual(self.nodes.claim(self.a)['grant'],grant)
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_wrong_node_attempt_and_token_cannot_write_or_complete(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.auth(grant)
        for node,change in [(self.b,{}),(self.a,{'attempt_id':'wrong'}),(self.a,{'lease_token':'wrong'})]:
            with self.subTest(node=node,change=change),self.assertRaises(JobError):
                self.nodes.update(node,{**auth,**change,'running':True})
        self.assertEqual(self.jobs.get(job['id'])['state'],'reserved')
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_liveness_only_pulse_preserves_last_log_preview_and_other_stream(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.auth(grant)
        self.nodes.update(self.a,{**auth,'running':True,'logs':{'stdout':{'text':'[192/858] Building CXX','bytes':24}}})
        before=self.jobs.get(job['id'])['_log_preview']['stdout']
        self.nodes.update(self.a,{**auth,'running':False})
        self.assertEqual(self.jobs.get(job['id'])['_log_preview']['stdout'],before)
        self.nodes.update(self.a,{**auth,'running':True,'logs':{'stderr':{'text':'warning','bytes':7}}})
        self.assertEqual(self.jobs.get(job['id'])['_log_preview']['stdout'],before)
        self.assertEqual(self.jobs.get(job['id'])['_log_preview']['stderr']['text'],'warning')

    def test_stale_heartbeat_and_wrong_physical_host_cannot_claim(self):
        self.submit()
        with self.assertRaises(JobError):
            self.nodes.heartbeat(self.a,{'physical_id':'0'*64})
        with self.jobs.connect() as db:db.execute('UPDATE worker_nodes SET last_seen=0 WHERE id=?',(self.a,))
        with self.assertRaises(JobError):self.nodes.claim(self.a)
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)

    def test_upload_retries_offsets_and_control_paths(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.auth(grant)
        def upload(name,offset,content):
            return self.nodes.upload(self.a,{**auth,'name':name,'offset':offset,'data':base64.b64encode(content).decode()})
        for name in ['completion.json','dispatch.json','../config.json','results/../config.json','/etc/passwd','results/link/../../x']:
            with self.subTest(name=name),self.assertRaises(JobError):upload(name,0,b'x')
        self.assertEqual(upload('stdout.log',0,b'abc')['size'],3)
        self.assertTrue(upload('stdout.log',0,b'abc')['reused'])
        with self.assertRaises(JobError):upload('stdout.log',0,b'xyz')
        with self.assertRaises(JobError):upload('stdout.log',5,b'x')
        # Simulate fsync succeeding before SQLite commit: discard only the uncommitted tail.
        path=self.jobs.directory(job['id'])/'stdout.log'
        with path.open('ab') as file:file.write(b'uncommitted')
        upload('stdout.log',3,b'def')
        self.assertEqual(path.read_bytes(),b'abcdef')

    def test_termination_and_archive_integrity_are_required_before_release(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.auth(grant)
        result={'status':'succeeded','exit_code':0,'termination_confirmed':False}
        self.assertTrue(self.nodes.authorize_start(self.a,auth)['start_permitted'])
        with self.assertRaises(JobError):self.nodes.complete(self.a,{**auth,'result':result,'artifacts':[]})
        data=b'{"runtime":"managed-docker"}'
        self.nodes.upload(self.a,{**auth,'name':'environment.json','offset':0,'data':base64.b64encode(data).decode()})
        manifest=[{'name':'environment.json','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]
        result['termination_confirmed']=True
        result['execution_cleanup_confirmed']=True
        with self.assertRaises(JobError):self.nodes.complete(self.a,{**auth,'result':result,'artifacts':[]})
        with self.assertRaises(JobError):self.nodes.complete(self.a,{**auth,'result':result,'artifacts':[{**manifest[0],'sha256':'0'*64}]})
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        receipt=self.nodes.complete(self.a,{**auth,'result':result,'artifacts':manifest})
        self.assertTrue(receipt['accepted'])
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)
        self.assertTrue(self.nodes.complete(self.a,{**auth,'result':result,'artifacts':manifest})['reused'])

    def test_local_and_remote_admission_share_the_same_physical_cpu_ledger(self):
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(domain['physical_id'],{**domain['policy'],'max_running':2},domain['revision'])
        local,_=self.jobs.submit({'argv':['python','-c','print(1)']})
        remote=self.submit()
        observations=Mock();observations.latest.return_value={'sampled_at':now(),
            'host':{'memory_available_bytes':8*1024**3,'workspace_disk':{'free_bytes':8*1024**3}}}
        scheduler=Scheduler(self.jobs,observations,threading.Event())
        with patch('jobs.subprocess.Popen'):scheduler.tick()
        self.assertEqual(self.jobs.get(local['id'])['state'],'reserved')
        self.assertIsNone(self.nodes.claim(self.a)['grant'])
        with self.jobs.transaction() as db:
            row=self.jobs.get(local['id'],db);row['state']='succeeded';self.jobs.write(db,row)
        self.assertEqual(self.nodes.claim(self.a)['grant']['job_id'],remote['id'])

    def test_disconnected_attempt_keeps_resources_and_can_recover_same_grant(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant']
        with self.jobs.transaction() as db:
            row=self.jobs.get(job['id'],db);row['_worker_seen']=0;self.jobs.write(db,row)
        Scheduler(self.jobs,Mock(),threading.Event()).reconcile()
        self.assertEqual(self.jobs.get(job['id'])['state'],'needs_attention')
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        self.assertEqual(self.nodes.claim(self.a)['grant']['attempt_id'],grant['attempt_id'])
        self.nodes.update(self.a,{**self.auth(grant),'running':True})
        self.assertEqual(self.jobs.get(job['id'])['state'],'running')

    def test_start_ticket_cannot_be_reissued_after_local_worker_state_loss(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.auth(grant)
        self.assertTrue(self.nodes.authorize_start(self.a,auth)['start_permitted'])
        self.assertFalse(self.nodes.authorize_start(self.a,auth)['start_permitted'])
        self.assertEqual(self.nodes.claim(self.a)['grant'],grant)
        from worker import Worker
        worker=object.__new__(Worker)
        worker.root=self.runtime/'worker-state'
        worker.config={**self.profile,'node_id':self.a,'image':'sha256:'+'0'*64,'python':'/usr/bin/python3'}
        worker.client=Mock()
        worker.client.call.side_effect=lambda action,body:self.nodes.authorize_start(self.a,body) if action=='start' else self.nodes.update(self.a,body)
        with patch('container_engine.DockerAttempt.inspect',return_value=None),patch('container_engine.run') as docker:
            with self.assertRaises(RuntimeError):worker.attempt(grant)
        docker.assert_not_called()
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_cancel_before_start_never_issues_launch_permission(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.auth(grant)
        self.jobs.cancel(job['id'],self.alice)
        permission=self.nodes.authorize_start(self.a,auth)
        self.assertFalse(permission['start_permitted'])
        self.assertFalse(permission['already_authorized'])
        self.assertTrue(permission['cancel'])

    def test_unverifiable_daemon_marks_attention_and_never_releases_the_grant(self):
        from worker import Worker
        from docker_runtime import DockerRuntimeError
        job=self.submit();grant=self.nodes.claim(self.a)['grant']
        worker=object.__new__(Worker);worker.root=self.runtime/'runtime-fault'
        worker.config={'node_id':self.a,'physical_id':self.jobs.backend.physical_id};worker.client=Mock();worker.finish_attempt=Mock()
        worker.client.call.side_effect=lambda action,body:self.nodes.update(self.a,body)
        control=self.runtime/'runtime-fault-control';control.mkdir()
        engine=Mock();engine.control=control;engine.state={'stage':'new'}
        engine.validate.side_effect=DockerRuntimeError('Cannot verify daemon')
        engine.inspect.side_effect=DockerRuntimeError('Cannot verify daemon')
        with patch('worker.DockerAttempt',return_value=engine):
            with self.assertRaises(DockerRuntimeError):worker.attempt(grant)
        self.assertEqual(self.jobs.get(job['id'])['state'],'needs_attention')
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        worker.finish_attempt.assert_not_called();engine.start.assert_not_called()

    def test_lost_completion_ack_does_not_repeat_uploads_or_execution(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant']
        from worker import Worker
        worker=object.__new__(Worker)
        worker.root=self.runtime/'worker-state';worker.config={'node_id':self.a,'physical_id':self.jobs.backend.physical_id}
        worker.client=Mock();worker.client.call.return_value={'terminal':True,'state':'succeeded'}
        with patch('worker.DockerAttempt') as Engine:
            engine=Engine.return_value;engine.state={'stage':'finished'}
            worker.attempt(grant)
            engine.start.assert_not_called();engine.cleanup.assert_called_once()
            self.assertTrue(engine.state['controller_acknowledged'])
        self.assertEqual(worker.client.call.call_count,1)

    def test_node_key_revocation_and_rotation_preserve_the_existing_lease(self):
        job=self.submit();grant=self.nodes.claim(self.a)['grant']
        self.nodes.disable(self.a)
        self.assertIsNone(self.nodes.authenticate('Bearer '+self.key))
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        new_key=self.nodes.rotate_key(self.a)
        self.assertIsNone(self.nodes.authenticate('Bearer '+self.key))
        self.assertEqual(self.nodes.authenticate('Bearer '+new_key),self.a)
        self.beat(self.a)
        self.assertEqual(self.nodes.claim(self.a)['grant']['attempt_id'],grant['attempt_id'])
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)


if __name__=='__main__':unittest.main()

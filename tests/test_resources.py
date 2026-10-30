"""Concurrent, cross-entry admission and conservative recovery of physical-host budgets."""
from concurrent.futures import ThreadPoolExecutor
import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,Scheduler
from resources import MULTI_SLOT
from worker import Worker,STOP
from container_engine import DockerAttempt


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-resources-');self.root=Path(self.temp.name)
        self.config={'username':'operator','password':'offline-only','container':'zdl'}
        self.jobs=JobStore(self.root,self.config);self.resources=self.jobs.resources;self.remote='f'*64
        for user in ['alice','bob','carol','dan']:
            workspace=self.root/user;workspace.mkdir()
            self.jobs.access.create_user(user,workspace,environments=['worker-python'])

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def domain(self,memory=768,slots=4):
        return self.resources.configure(self.remote,{'cpu_ids':[100,101,102,103],
            'memory_mib':memory,'shm_mib':64,'max_running':slots},0)

    def enroll(self,cpus,memory=512,slots=1):
        profile={'name':'resource-node','physical_id':self.remote,'templates':['cpu_demo'],
          'users':{user:{'uid':23001+i,'gid':23001+i} for i,user in enumerate(['alice','bob','carol','dan'])},
          'policy':{'cpu_ids':cpus,'memory_mib':memory,'shm_mib':0,'max_running':slots}}
        node,key=self.jobs.nodes.enroll(profile)
        self.beat(node);return node

    def beat(self,node,concurrent=True):
        self.jobs.nodes.heartbeat(node,{'physical_id':self.remote,'protocol_version':1,
          'capabilities':['start-ticket-v1','cleanup-before-complete-v1',*([MULTI_SLOT] if concurrent else [])],
          'cpu_ids':[100,101,102,103],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})

    def submit(self,user):
        return self.jobs.submit({'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}},self.jobs.access.user(user))[0]

    def test_remote_domain_must_be_explicit_and_nodes_cannot_inflate_it(self):
        with self.assertRaises(JobError):self.enroll([100])
        self.domain()
        for cpus,memory,slots in [([104],512,1),([100],768,1),([100,101],512,3)]:
            with self.subTest(cpus=cpus,memory=memory,slots=slots),self.assertRaises(JobError):self.enroll(cpus,1024 if memory==768 else memory,slots)

    def test_parallel_claims_across_four_entries_share_one_memory_budget(self):
        self.domain();nodes=[self.enroll([100+i]) for i in range(4)]
        for user in ['alice','bob','carol','dan']:self.submit(user)
        with ThreadPoolExecutor(max_workers=4) as pool:
            grants=[reply['grant'] for reply in pool.map(self.jobs.nodes.claim,nodes)]
        accepted=[grant for grant in grants if grant]
        self.assertEqual(len(accepted),2)
        self.assertEqual(len({cpu for grant in accepted for cpu in grant['allocation']['cpu_ids']}),2)
        summary=next(domain for domain in self.resources.summary() if domain['physical_id']==self.remote)
        self.assertEqual(summary['reserved']['memory_mib'],768)
        self.assertEqual(summary['reserved']['workload_memory_mib'],512)
        self.assertEqual(summary['reserved']['execution_overhead_mib'],256)
        self.assertEqual(summary['reserved']['slots'],2)
        # Increasing a node's local envelope never changes the authoritative total.
        self.assertEqual(summary['policy']['memory_mib'],768)

    def test_concurrent_node_can_claim_another_grant_and_replay_each_attempt(self):
        self.domain(memory=1024,slots=2);node=self.enroll([100,101],memory=768,slots=2)
        self.submit('alice');self.submit('bob');self.submit('carol')
        first=self.jobs.nodes.claim(node)['grant']
        self.assertEqual(self.jobs.nodes.claim(node)['grant'],first)
        second=self.jobs.nodes.claim(node,{'exclude_attempts':[first['attempt_id']]})['grant']
        self.assertNotEqual(first['attempt_id'],second['attempt_id'])
        self.assertIsNone(self.jobs.nodes.claim(node,{'exclude_attempts':[first['attempt_id'],second['attempt_id']]})['grant'])
        self.assertEqual(self.jobs.nodes.claim(node)['grant'],first)
        self.assertEqual(self.jobs.summary()['reserved']['memory_mib'],512)
        self.assertEqual(len(self.jobs.attempts(first['job_id'])),1)

    def test_old_node_capabilities_cannot_enable_concurrent_admission(self):
        self.domain(memory=1024,slots=2);node=self.enroll([100,101],memory=512,slots=2)
        with self.assertRaises(JobError):self.beat(node,concurrent=False)
        for body in [{'exclude_attempts':['not-an-attempt']},{'exclude_attempts':False}]:
            with self.assertRaises(JobError):self.jobs.nodes.claim(node,body)

    def test_node_policy_revision_requires_config_update_and_rejects_in_flight_change(self):
        self.domain(memory=1024);node=self.enroll([100]);profile=self.jobs.nodes.node(node)['profile']
        revised=self.jobs.nodes.set_policy(node,{**profile['policy'],'memory_mib':768},1)
        self.assertEqual(revised['policy_revision'],2)
        self.assertEqual([item['revision'] for item in self.jobs.nodes.policy_events(node)],[2,1])
        with self.assertRaises(JobError):self.beat(node)
        self.jobs.nodes.heartbeat(node,{'physical_id':self.remote,'policy_revision':2,'protocol_version':1,
          'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],'cpu_ids':[100],
          'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        self.submit('alice');grant=self.jobs.nodes.claim(node)['grant'];self.assertIsNotNone(grant)
        with self.assertRaises(JobError):self.jobs.nodes.set_policy(node,revised['policy'],2)
        self.assertEqual(self.jobs.nodes.node(node)['profile']['policy_revision'],2)

    def test_node_policy_change_cannot_exceed_domain_or_overwrite_new_revision(self):
        self.domain();node=self.enroll([100]);profile=self.jobs.nodes.node(node)['profile']
        for policy,revision in [({**profile['policy'],'memory_mib':1024},1),(profile['policy'],0)]:
            with self.assertRaises(JobError):self.jobs.nodes.set_policy(node,policy,revision)
        self.assertEqual(self.jobs.nodes.node(node)['profile']['policy_revision'],1)

    def test_unknown_attempt_still_consumes_budget_and_blocks_unsafe_shrink(self):
        domain=self.domain();node=self.enroll([100]);job=self.submit('alice')
        grant=self.jobs.nodes.claim(node)['grant']
        with self.jobs.transaction() as db:
            row=self.jobs.get(job['id'],db);row['state']='needs_attention';self.jobs.write(db,row)
        for change in [{'cpu_ids':[101,102,103],'max_running':3},{'memory_mib':256,'max_running':1,'shm_mib':0}]:
            if 'memory_mib' in change:
                # A second reservation makes a 256 MiB reduction unsafe.
                other=self.enroll([101]);self.submit('bob');self.jobs.nodes.claim(other)
            with self.assertRaises(JobError):self.resources.configure(self.remote,{**domain['policy'],**change},domain['revision'])
        self.assertEqual(self.resources.get(self.remote)['revision'],domain['revision'])
        self.assertEqual(self.jobs.nodes.claim(node)['grant']['attempt_id'],grant['attempt_id'])
        self.assertEqual(next(item for item in self.resources.summary() if item['physical_id']==self.remote)['unknown_attempts'],1)

    def test_drain_preserves_grants_but_blocks_new_remote_and_local_assignments(self):
        domain=self.domain();a=self.enroll([100]);b=self.enroll([101])
        self.submit('alice');self.submit('bob');grant=self.jobs.nodes.claim(a)['grant']
        self.resources.configure(self.remote,domain['policy'],domain['revision'],True)
        self.assertEqual(self.jobs.nodes.claim(a)['grant'],grant)
        self.assertIsNone(self.jobs.nodes.claim(b)['grant'])
        local=self.resources.get(self.jobs.backend.physical_id)
        self.resources.configure(local['physical_id'],local['policy'],local['revision'],True)
        job,_=self.jobs.submit({'argv':['python','-c','pass']})
        scheduler=Scheduler(self.jobs,Mock(),threading.Event())
        try:
            with patch('jobs.subprocess.Popen') as launch:scheduler.tick()
            launch.assert_not_called();self.assertIn('排空',self.jobs.get(job['id'])['reason'])
        finally:
            if scheduler.lock:scheduler.lock.close()

    def test_revision_race_has_one_winner_and_persists_after_restart(self):
        domain=self.domain()
        def change(_):
            try:return self.resources.configure(self.remote,domain['policy'],domain['revision'],True)['revision']
            except JobError as error:return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(change,range(2)))
        self.assertCountEqual(results,[2,409])
        restarted=JobStore(self.root,self.config)
        try:self.assertEqual(restarted.resources.get(self.remote)['revision'],2)
        finally:restarted.access.close()
        self.assertEqual(len(self.resources.events(self.remote)),2)

    def test_worker_enforces_aggregate_cpu_and_memory_before_start(self):
        worker=object.__new__(Worker);worker.config={'policy':{'cpu_ids':[100,101],'memory_mib':768,'shm_mib':0,'max_running':2}}
        first={'attempt_id':'a','allocation':{'cpu_ids':[100]},'resources':{'memory_mib':256,'shm_mib':0}}
        second={'attempt_id':'b','allocation':{'cpu_ids':[101]},'resources':{'memory_mib':256,'shm_mib':0}}
        worker.check_capacity(second,[first])
        for changed in [{**second,'allocation':{'cpu_ids':[100]}},{**second,'resources':{'memory_mib':512,'shm_mib':0}}]:
            with self.assertRaises(RuntimeError):worker.check_capacity(changed,[first])

    def test_cleanup_repeated_after_lost_ack_keeps_artifacts_and_never_recreates_work(self):
        grant={'attempt_id':'attempt-'+'a'*32,'owner':'alice'}
        engine=DockerAttempt(self.root/'engine',grant,{'image':'sha256:'+'0'*64,'users':{'alice':{'uid':23001,'gid':23001}}})
        artifact=engine.artifacts/'kept.txt';artifact.write_text('completed evidence')
        with patch.object(engine,'inspect',return_value=None),patch('container_engine.run') as docker:
            engine.cleanup();engine.cleanup()
        docker.assert_not_called();self.assertEqual(engine.state['stage'],'cleaned')
        self.assertFalse(engine.work.exists());self.assertEqual(artifact.read_text(),'completed evidence')

    def test_cancel_after_saved_authorization_does_not_launch_on_recovery(self):
        worker=object.__new__(Worker);worker.root=self.root/'cancelled-worker'
        worker.config={'node_id':'worker-test','physical_id':self.remote}
        worker.client=Mock();worker.client.call.return_value={'cancel':True,'terminal':False}
        worker.finish_attempt=Mock()
        grant={'version':1,'node_id':'worker-test','physical_id':self.remote,'job_id':'job-test',
               'attempt_id':'attempt-'+'b'*32,'lease_token':'offline-test-only'}
        engine=Mock();engine.state={'stage':'new','start_authorized_locally':True}
        engine.artifacts=self.root/'cancel-artifacts';engine.artifacts.mkdir()
        engine.control=self.root/'cancel-control';engine.control.mkdir();engine.inspect.return_value=None
        with patch('worker.DockerAttempt',return_value=engine):worker.attempt(grant)
        engine.start.assert_not_called();self.assertEqual(worker.finish_attempt.call_args.args[1]['status'],'cancelled')

    def test_worker_runs_two_attempts_and_refills_only_after_a_slot_finishes(self):
        worker=object.__new__(Worker);worker.root=self.root/'worker'
        worker.config={'policy':{'cpu_ids':[100,101],'memory_mib':768,'shm_mib':0,'max_running':2}}
        worker.heartbeat=Mock();worker.recover=Mock(return_value=[])
        grants=[{'attempt_id':'attempt-'+str(i)*32,'job_id':'job-'+str(i),'lease_token':'offline-only','allocation':{'cpu_ids':[100+i%2]},
                 'resources':{'memory_mib':256,'shm_mib':0}} for i in range(3)]
        worker.client=Mock();worker.client.call.side_effect=lambda action,body:({'grant':grants.pop(0) if grants else None} if action=='claim' else {})
        entered=[threading.Event() for _ in range(3)];release=[threading.Event() for _ in range(3)]
        def attempt(grant):
            index=int(grant['attempt_id'][-1]);entered[index].set();release[index].wait(8)
        worker.attempt=attempt;STOP.clear();thread=threading.Thread(target=worker.run);thread.start()
        try:
            self.assertTrue(entered[0].wait(4));self.assertTrue(entered[1].wait(4))
            self.assertFalse(entered[2].is_set());release[0].set()
            self.assertTrue(entered[2].wait(4))
        finally:
            STOP.set()
            for event in release:event.set()
            thread.join(10);STOP.clear()
        self.assertFalse(thread.is_alive())

    def test_attempt_pulse_is_bounded_and_does_not_claim_container_running(self):
        worker=object.__new__(Worker);worker.client=Mock()
        grant={'job_id':'job-pulse','attempt_id':'attempt-'+'d'*32,'lease_token':'offline-only','timeout_seconds':10}
        with patch('worker.time.monotonic',return_value=100):self.assertTrue(worker.pulse_attempt(grant,90))
        self.assertFalse(worker.client.call.call_args.args[1]['running'])
        worker.client.reset_mock()
        with patch('worker.time.monotonic',return_value=300):self.assertFalse(worker.pulse_attempt(grant,90))
        worker.client.call.assert_not_called()


if __name__=='__main__':unittest.main()

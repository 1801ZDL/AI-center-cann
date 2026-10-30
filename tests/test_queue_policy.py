"""Actual controller claims: aging, fragmentation, cross-entry intent and revocation."""
import datetime as dt
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,Scheduler,now
from queue_policy import ordered,protected,age,threshold,WAIT_REASON


class QueuePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-aging-')
        self.root=Path(self.tmp.name)
        self.config={'username':'operator','password':'offline-only','container':'zdl',
                     'scheduler':{'aging_seconds':30,'max_running':2}}
        self.jobs=JobStore(self.root,self.config);self.physical=self.jobs.backend.physical_id
        self.cpus=self.jobs.resources.get(self.physical)['policy']['cpu_ids']
        for user in ['alice','bob','carol','dan']:
            home=self.root/user;home.mkdir()
            self.jobs.access.create_user(user,home,environments=['worker-python'])
            self.jobs.access.set_quota(user,{'cpu':2,'memory_mib':1024,'max_running':2})
        self.node=self.enroll(self.cpus)

    def tearDown(self):self.jobs.access.close();self.tmp.cleanup()

    def enroll(self,cpus):
        profile={'name':'aging-entry','physical_id':self.physical,'templates':['cpu_demo'],
          'users':{name:{'uid':23601+i,'gid':23601+i} for i,name in enumerate(['alice','bob','carol','dan'])},
          'policy':{'cpu_ids':cpus,'memory_mib':1536,'shm_mib':0,'max_running':len(cpus)}}
        node,_=self.jobs.nodes.enroll(profile)
        self.jobs.nodes.heartbeat(node,{'physical_id':self.physical,'protocol_version':1,
          'capabilities':['start-ticket-v1','cleanup-before-complete-v1','concurrent-attempts-v1'],
          'cpu_ids':self.cpus,'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        return node

    def submit(self,user,cpu=1,old=False):
        job=self.jobs.submit({'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}},self.jobs.access.user(user))[0]
        with self.jobs.transaction() as db:
            job=self.jobs.get(job['id'],db);job['resources']['cpu']=cpu
            if old:job['created_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)).isoformat()
            self.jobs.write(db,job)
        return job

    def finish(self,job_id):
        # Offline fixture only: no Docker object or user command was created.
        with self.jobs.transaction() as db:
            job=self.jobs.get(job_id,db);job['state']='succeeded';job['finished_at']=now();self.jobs.write(db,job)

    def guard(self):
        with self.jobs.connect() as db:return protected(self.jobs,self.jobs.all(db),self.physical,db)

    def blocked_pair(self):
        running=self.submit('alice');self.jobs.nodes.claim(self.node)
        older=self.submit('bob',cpu=2,old=True)
        self.assertIsNone(self.jobs.nodes.claim(self.node,{'exclude_attempts':[self.jobs.get(running['id'])['attempt_id']]})['grant'])
        self.assertEqual(self.guard()['id'],older['id'])
        return running,older

    def test_young_owner_rotation_and_aged_fifo_both_preserved(self):
        rows=[{'id':'history','owner':'alice','state':'succeeded','reserved_at':'2026-10-01T00:00:00+00:00'},
          {'id':'a','owner':'alice','state':'queued','created_at':'2026-10-01T00:00:01+00:00'},
          {'id':'b','owner':'bob','state':'queued','created_at':'2026-10-01T00:00:02+00:00'}]
        epoch=dt.datetime.fromisoformat('2026-10-01T00:00:03+00:00').timestamp()
        self.assertEqual([j['id'] for j in ordered(rows,{'aging_seconds':30},epoch)],['b','a'])
        self.assertEqual([j['id'] for j in ordered(rows,{'aging_seconds':1},epoch)],['a','b'])
        self.assertEqual(age({'created_at':'malformed'}),0)
        self.assertEqual(age({'created_at':'2026-10-01T00:00:00'}),0)
        for bad in [0,-1,True,1.5,86401]:
            with self.assertRaises(ValueError):threshold({'aging_seconds':bad})

    def test_fragmented_aged_job_stops_new_single_cpu_claim_then_runs(self):
        running,older=self.blocked_pair();younger=self.submit('carol');other=self.enroll(self.cpus[-1:])
        self.assertIsNone(self.jobs.nodes.claim(other)['grant'])
        self.assertEqual(self.jobs.get(younger['id'])['reason'],WAIT_REASON)
        self.assertEqual(self.jobs.resources.summary()[0]['reserved']['cpu'],1)
        self.finish(running['id'])
        grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],older['id']);self.assertEqual(len(grant['allocation']['cpu_ids']),2)
        self.finish(older['id'])
        self.assertEqual(self.jobs.nodes.claim(other)['grant']['job_id'],younger['id'])

    def test_cancellation_immediately_removes_protection(self):
        running,older=self.blocked_pair();other=self.enroll(self.cpus[-1:]);younger=self.submit('carol')
        self.jobs.cancel(older['id'],self.jobs.access.user('bob'))
        self.assertIsNone(self.guard())
        self.assertEqual(self.jobs.nodes.claim(other)['grant']['job_id'],younger['id'])

    def test_impossible_request_does_not_drain_a_domain(self):
        self.submit('alice');first=self.jobs.nodes.claim(self.node)['grant']
        impossible=self.submit('bob',cpu=3,old=True);young=self.submit('carol')
        grant=self.jobs.nodes.claim(self.node,{'exclude_attempts':[first['attempt_id']]})['grant']
        self.assertEqual(grant['job_id'],young['id']);self.assertIsNone(self.guard())
        self.assertNotIn('_aging_target',self.jobs.get(impossible['id']))

    def test_disabled_owner_or_node_cannot_keep_an_old_guard(self):
        running,older=self.blocked_pair()
        self.jobs.access.disable_user('bob',True);self.assertIsNone(self.guard())
        self.jobs.access.disable_user('bob',False);self.assertEqual(self.guard()['id'],older['id'])
        self.jobs.nodes.disable(self.node);self.assertIsNone(self.guard())
        self.assertEqual(self.jobs.resources.summary()[0]['reserved']['slots'],1)

    def test_stale_proof_expires_without_releasing_running_resources(self):
        running,older=self.blocked_pair()
        with self.jobs.transaction() as db:
            job=self.jobs.get(older['id'],db);job['_aging_target']['checked_epoch']=time.time()-16;self.jobs.write(db,job)
        self.assertIsNone(self.guard());self.assertEqual(self.jobs.resources.summary()[0]['reserved']['slots'],1)

    def test_stale_heartbeat_rejects_guard_and_preserves_unknown_resource_lease(self):
        running,older=self.blocked_pair()
        with self.jobs.transaction() as db:
            db.execute('UPDATE worker_nodes SET last_seen=? WHERE id=?',(time.time()-31,self.node))
            job=self.jobs.get(running['id'],db);job['state']='needs_attention';self.jobs.write(db,job)
        self.assertIsNone(self.guard())
        self.assertEqual(self.jobs.resources.summary()[0]['reserved']['cpu'],1)
        self.assertEqual(self.jobs.resources.summary()[0]['unknown_attempts'],1)

    def test_controller_reopen_keeps_the_same_guard_and_attempt(self):
        running,older=self.blocked_pair();before=self.jobs.get(running['id'])['attempt_id']
        reopened=JobStore(self.root,self.config)
        try:
            with reopened.connect() as db:
                self.assertEqual(protected(reopened,reopened.all(db),self.physical,db)['id'],older['id'])
            self.assertEqual(reopened.nodes.claim(self.node)['grant']['attempt_id'],before)
        finally:reopened.access.close()

    def test_personal_quota_wait_does_not_idle_other_users(self):
        self.jobs.access.set_quota('alice',{'max_running':1})
        self.submit('alice');first=self.jobs.nodes.claim(self.node)['grant']
        old=self.submit('alice',old=True);young=self.submit('carol')
        result=self.jobs.nodes.claim(self.node,{'exclude_attempts':[first['attempt_id']]})['grant']
        self.assertEqual(result['job_id'],young['id']);self.assertIsNone(self.guard())
        self.assertNotIn('_aging_target',self.jobs.get(old['id']))

    def test_unavailable_actual_memory_does_not_form_protection(self):
        self.submit('alice');first=self.jobs.nodes.claim(self.node)['grant']
        old=self.submit('bob',cpu=2,old=True)
        with self.jobs.connect() as db:
            import json
            report=self.jobs.nodes.node(self.node,db)['report'];report['memory_available_bytes']=0
            db.execute('UPDATE worker_nodes SET report=? WHERE id=?',(json.dumps(report),self.node))
        self.assertIsNone(self.jobs.nodes.claim(self.node,{'exclude_attempts':[first['attempt_id']]})['grant'])
        self.assertIsNone(self.guard());self.assertNotIn('_aging_target',self.jobs.get(old['id']))

    def test_domain_revision_invalidates_the_intent_but_keeps_the_running_lease(self):
        running,old=self.blocked_pair();domain=self.jobs.resources.get(self.physical)
        self.jobs.resources.configure(self.physical,domain['policy'],domain['revision'])
        self.assertIsNone(self.guard());self.assertEqual(self.jobs.resources.summary()[0]['reserved']['slots'],1)

    def test_old_queued_job_remains_visible_beyond_recent_completed_history(self):
        old=self.submit('alice',old=True)
        history=[{**old,'id':'job-'+format(i,'012x'),'state':'succeeded'} for i in range(101)]
        with patch.object(self.jobs,'all',return_value=[old,*history]):
            summary=self.jobs.summary(self.jobs.access.user('alice'))
        self.assertIn(old['id'],{row['id'] for row in summary['jobs']})
        self.assertEqual(summary['counts']['queued'],1)

    def test_local_entry_cannot_bypass_remote_protection(self):
        running,older=self.blocked_pair()
        local=self.jobs.submit({'argv':['python','-c','pass']})[0]
        snapshot={'sampled_at':now(),'host':{'memory_available_bytes':8*1024**3,'workspace_disk':{'free_bytes':8*1024**3}}}
        scheduler=Scheduler(self.jobs,Mock(),threading.Event());scheduler.observations.latest.return_value=snapshot
        with patch.object(scheduler,'reconcile'),patch.object(self.jobs.backend,'launch') as launch:scheduler.tick()
        launch.assert_not_called();self.assertEqual(self.jobs.get(local['id'])['reason'],WAIT_REASON)

    def test_local_aged_job_protects_against_a_remote_entry(self):
        first=self.submit('alice');self.jobs.nodes.claim(self.node)
        local=self.jobs.submit({'argv':['python','-c','pass'],'cpu':2})[0]
        with self.jobs.transaction() as db:
            row=self.jobs.get(local['id'],db);row['created_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)).isoformat();self.jobs.write(db,row)
        snapshot={'sampled_at':now(),'host':{'memory_available_bytes':8*1024**3,'workspace_disk':{'free_bytes':8*1024**3}}}
        scheduler=Scheduler(self.jobs,Mock(),threading.Event());scheduler.observations.latest.return_value=snapshot
        with patch.object(scheduler,'reconcile'),patch.object(self.jobs.backend,'launch') as launch:scheduler.tick()
        launch.assert_not_called();self.assertEqual(self.guard()['id'],local['id'])
        young=self.submit('carol');other=self.enroll(self.cpus[-1:])
        self.assertIsNone(self.jobs.nodes.claim(other)['grant'])
        self.assertEqual(self.jobs.get(young['id'])['reason'],WAIT_REASON)

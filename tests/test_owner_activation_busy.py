"""Final identity admission must coexist with known, reserved foreign work."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_activation as builder
import test_owner_device_probe as staged_builder
from jobs import JobError,identity
from lab_client import atomic_json
from owner_activation import CAPABILITY


class BusyActivationTests(unittest.TestCase):
    def setUp(self):
        self.case=builder.OwnerActivationTests();self.case.setUp()
        self.jobs=self.case.jobs;self.node=self.case.fixture.node
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.node,db);profile=node['profile']
            profile['spool_storage']={'path':'/protocol-fixture-spool','filesystem':'ext4','device':1,'root_inode':2,
                'bytes':128*1024**2,'inodes':262144,'storage_id':'f'*32}
            record=self.jobs.nodes.handoff.get(self.case.fixture.plan['id'],db);record['after_profile']=profile
            self.jobs.nodes.handoff.save(db,record)
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.node))

    def tearDown(self):self.case.tearDown()

    def foreign(self,state='running'):
        job,_=self.jobs.submit({'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}},self.jobs.access.user('alice'))
        response=self.jobs.nodes.claim(self.node);grant=response['grant'];self.assertIsNotNone(grant,response)
        self.assertEqual(grant['job_id'],job['id'])
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        if state=='running':
            self.assertTrue(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted'])
            self.jobs.nodes.update(self.node,{**auth,'running':True})
        elif state!='reserved':
            with self.jobs.transaction() as db:
                value=self.jobs.get(job['id'],db);value['state']=state;self.jobs.write(db,value)
        return self.jobs.get(job['id'])

    def finish(self,job):
        auth={k:job[k] for k in ['attempt_id']};auth.update(job_id=job['id'],lease_token=job['_token'])
        raw=b'{}';self.jobs.nodes.upload(self.node,{**auth,'name':'environment.json','offset':0,'data':base64.b64encode(raw).decode()})
        return self.jobs.nodes.complete(self.node,{**auth,'result':{'status':'succeeded','exit_code':0,'termination_confirmed':True,
            'execution_cleanup_confirmed':True,'workspace_release_confirmed':True,'cache_release_confirmed':True},
            'artifacts':[{'name':'environment.json','size':2,'sha256':hashlib.sha256(raw).hexdigest()}]})

    def test_activation_does_not_rewrite_or_release_foreign_running_lease(self):
        old=self.foreign();before=copy.deepcopy(old);reserved=self.jobs.summary()['reserved']
        self.case.run_activation()
        self.assertEqual(self.jobs.get(old['id']),before)
        self.assertEqual(self.jobs.summary()['reserved'],reserved)
        self.assertIsNotNone(self.jobs.access.user('bob'))
        self.assertEqual(len(self.jobs.attempts(old['id'])),1)

    def test_new_owner_queues_behind_full_node_then_gets_one_original_attempt(self):
        old=self.foreign();self.case.run_activation()
        from environment_contract import lock_for
        user=self.jobs.access.user('bob');raw=b"print('hello')\n";digest=hashlib.sha256(raw).hexdigest()
        bundle,_=self.jobs.inputs.create(user,{'files':[{'name':'main.py','size':len(raw),'sha256':digest}]})
        self.jobs.inputs.upload(bundle['id'],user,{'name':'main.py','offset':0,'data':base64.b64encode(raw).decode()});self.jobs.inputs.seal(bundle['id'],user)
        new,_=self.jobs.submit({'environment':'worker-python','template':'ir_v1','parameters':{'input_id':bundle['id'],'argv':['python','main.py'],
            'lock':lock_for(self.jobs.nodes.node(self.node)['profile']['environment_manifest']),
            'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},'artifacts':{'file_mib':1,'total_mib':2},
            'workspace':{'min_mib':1,'min_inodes':16,'durable':True}}},user)
        self.assertIsNone(self.jobs.nodes.claim(self.node,{'exclude_attempts':[old['attempt_id']]})['grant']);self.assertEqual(self.jobs.get(new['id'])['state'],'queued')
        self.assertEqual(self.jobs.attempts(new['id']),[]);self.assertEqual(self.jobs.get(old['id']),old)
        self.assertEqual(self.finish(old)['state'],'succeeded')
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],new['id'])
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        self.assertTrue(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted'])
        self.assertEqual(len(self.jobs.attempts(new['id'])),1)
        self.assertEqual(grant['allocation']['cpu_ids'],old['allocation']['cpu_ids'])

    def test_reserved_and_cancelling_foreign_work_keep_the_original_allocation(self):
        for state in ['reserved','cancelling']:
            with self.subTest(state=state):
                if state=='reserved':old=self.foreign(state)
                else:
                    with self.jobs.transaction() as db:
                        old=self.jobs.get(old['id'],db);old['state']=state;self.jobs.write(db,old)
                with self.jobs.transaction() as db:
                    profile=self.jobs.access.registration('bob')['profile'];self.case.activation.readiness(self.case.spec,db,profile)
                self.assertEqual(self.jobs.get(old['id']),old)

    def test_unknown_foreign_execution_blocks_without_releasing_its_lease(self):
        old=self.foreign('needs_attention');reserved=self.jobs.summary()['reserved']
        with self.assertRaisesRegex(JobError,'unknown physical-domain'):self.case.run_activation()
        self.assertEqual(self.jobs.get(old['id']),old);self.assertEqual(self.jobs.summary()['reserved'],reserved)
        self.assertIsNone(self.jobs.access.user('bob'));self.assertFalse((self.case.root/'personas/bob/client.json').exists())

    def peer_unknown(self,physical):
        from nodes import REQUIRED_CAPABILITIES
        original=self.jobs.nodes.node(self.node)['profile']
        if physical!=original['physical_id']:
            self.jobs.resources.configure(physical,original['policy'],0)
        peer,_=self.jobs.nodes.enroll({'name':'peer_unknown','physical_id':physical,
            **{k:original[k] for k in ['users','image','policy']},'templates':['cpu_demo']})
        self.jobs.nodes.heartbeat(peer,{'physical_id':physical,'protocol_version':1,'policy_revision':1,
            'cpu_ids':original['policy']['cpu_ids'],'capabilities':sorted(REQUIRED_CAPABILITIES),
            'memory_available_bytes':4*1024**3,'disk_free_bytes':4*1024**3})
        job,_=self.jobs.submit({'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}},self.jobs.access.user('alice'))
        grant=self.jobs.nodes.claim(peer)['grant'];self.assertIsNotNone(grant);self.assertEqual(grant['job_id'],job['id'])
        self.jobs.nodes.update(peer,{**{k:grant[k] for k in ['job_id','attempt_id','lease_token']},'attention':True})
        return peer,self.jobs.get(job['id'])

    def test_unknown_same_domain_peer_blocks_even_when_that_node_is_disabled(self):
        peer,job=self.peer_unknown(self.jobs.backend.physical_id);self.jobs.nodes.disable(peer)
        with self.assertRaisesRegex(JobError,'unknown physical-domain'):self.case.run_activation()
        self.assertEqual(self.jobs.get(job['id']),job);self.assertIsNone(self.jobs.access.user('bob'))

    def test_unknown_on_an_independent_domain_does_not_quarantine_this_identity(self):
        peer,job=self.peer_unknown('9'*64)
        self.assertTrue(self.case.run_activation()['id']);self.assertEqual(self.jobs.get(job['id']),job)
        self.assertEqual(self.jobs.get(job['id'])['state'],'needs_attention')

    def test_structural_handoff_still_requires_the_node_idle(self):
        old=self.foreign()
        with self.jobs.transaction() as db,self.assertRaisesRegex(JobError,'Reconcile every Node execution'):
            self.jobs.nodes.handoff._idle(self.node,self.jobs.nodes.node(self.node,db)['profile'],db)
        self.assertEqual(self.jobs.get(old['id']),old)

    def test_pending_binding_and_domain_drain_still_block_identity_activation(self):
        self.foreign()
        with patch.object(self.jobs.nodes.handoff,'pending',return_value={'plan_id':'e'*64}),self.assertRaisesRegex(JobError,'pending Node'):
            self.case.run_activation()
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(domain['physical_id'],domain['policy'],domain['revision'],draining=True)
        with self.assertRaisesRegex(JobError,'draining'):self.case.run_activation()
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_healthy_foreign_work_cannot_hide_an_explicit_inventory_hold(self):
        old=self.foreign()
        with self.jobs.transaction() as db:
            db.execute('INSERT INTO worker_inventory_holds VALUES(?,?,?,?,?,?)',(self.node,self.jobs.backend.physical_id,1,1,'[]',__import__('time').time()))
        with self.assertRaisesRegex(JobError,'inventory'):self.case.run_activation()
        self.assertEqual(self.jobs.get(old['id']),old);self.assertTrue(self.jobs.inventory.holds())

    def test_current_inventory_from_every_enabled_domain_peer_is_required(self):
        self.foreign();self.jobs.policy['require_attempt_inventory']=True;self.jobs.controller_instance='c'*32
        atomic_json(self.case.root/'server-ready.json',{**identity(os.getpid()),'controller_instance':'c'*32,'capabilities':[CAPABILITY]})
        def report(value):self.case.alter_node(lambda node:node['report'].update(attempt_inventory=value))
        report({'reconciled':True,'controller_instance':'previous-fixture'})
        with self.assertRaisesRegex(JobError,'inventory'):self.case.run_activation()
        report({'reconciled':True,'controller_instance':'c'*32})
        with self.jobs.transaction() as db:
            profile=self.jobs.access.registration('bob')['profile'];self.case.activation.readiness(self.case.spec,db,profile)
        original=self.jobs.nodes.node(self.node)['profile']
        peer,_=self.jobs.nodes.enroll({**{k:v for k,v in original.items() if k in {'physical_id','users','image','templates','policy'}},'name':'inventory_peer'})
        with self.assertRaisesRegex(JobError,'inventory'):self.case.run_activation()
        with self.jobs.transaction() as db:
            db.execute('UPDATE worker_nodes SET last_seen=?,report=? WHERE id=?',(__import__('time').time(),json.dumps({'attempt_inventory':{'reconciled':True,'controller_instance':'c'*32}}),peer))
        self.assertTrue(self.case.run_activation()['id'])

    def fresh_inventory(self):
        self.jobs.policy['require_attempt_inventory']=True
        ready={**identity(os.getpid()),'controller_instance':'c'*32,'capabilities':[CAPABILITY]}
        atomic_json(self.case.root/'server-ready.json',ready)
        self.case.alter_node(lambda n:n['report'].update(attempt_inventory={'reconciled':True,'controller_instance':'c'*32}))
        return ready

    def test_local_administrator_uses_the_live_epoch_without_mutating_its_jobstore(self):
        self.foreign();self.fresh_inventory();self.assertFalse(hasattr(self.jobs,'controller_instance'))
        self.assertTrue(self.case.run_activation()['id']);self.assertFalse(hasattr(self.jobs,'controller_instance'))

    def test_unbound_or_conflicting_inventory_epoch_cannot_issue_credentials(self):
        self.foreign();ready=self.fresh_inventory()
        for change in [{'pid':-1},{'uid':os.geteuid()+1},{'controller_instance':None},{'capabilities':[]}]:
            atomic_json(self.case.root/'server-ready.json',{**ready,**change})
            reason='activation support' if change.get('capabilities')==[] else 'inventory controller epoch'
            with self.subTest(change=change),self.assertRaisesRegex(JobError,reason):self.case.run_activation()
        atomic_json(self.case.root/'server-ready.json',ready);self.jobs.controller_instance='d'*32
        with self.assertRaisesRegex(JobError,'inventory controller epoch'):self.case.run_activation()
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_controller_epoch_switch_during_readiness_does_not_create_an_intent(self):
        self.foreign();ready=self.fresh_inventory();reason=self.jobs.inventory.reason
        def switch(*args,**kwargs):
            value=reason(*args,**kwargs);atomic_json(self.case.root/'server-ready.json',{**ready,'controller_instance':'d'*32});return value
        with patch.object(self.jobs.inventory,'reason',side_effect=switch),self.assertRaisesRegex(JobError,'epoch changed'):
            self.case.run_activation()
        self.assertIsNone(self.jobs.access.user('bob'));self.assertFalse((self.case.root/'owner-activations/bob.pending.json').exists())

    def test_target_owner_slot_is_checked_while_foreign_slot_may_be_busy(self):
        self.foreign();node=self.jobs.nodes.node(self.node);uid=node['profile']['users']['alice']['uid']
        old_slots={s['id'] for s in node['profile']['workspace']['slots'] if s['quota_uid']==uid}
        self.case.alter_node(lambda n:[s.update(clean=False) for s in n['report']['workspace']['slots'] if s['id'] in old_slots])
        with self.jobs.transaction() as db:
            profile=self.jobs.access.registration('bob')['profile'];self.case.activation.readiness(self.case.spec,db,profile)
        self.case.alter_node(lambda n:[s.update(clean=False) for s in n['report']['workspace']['slots'] if s['id'] not in old_slots])
        with self.assertRaisesRegex(JobError,'slots'):self.case.run_activation()


class StagedBusyAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=staged_builder.StagedDeviceProbeTests();self.fixture.setUp()
        self.case=self.fixture.fixture;self.jobs=self.fixture.jobs;self.node=self.fixture.node

    def tearDown(self):self.fixture.tearDown()

    def test_fixed_staged_probe_queues_behind_foreign_work_without_issuing_credentials(self):
        old=BusyActivationTests.foreign(self);probe=self.fixture.submit()
        self.assertEqual(probe['job']['state'],'queued');self.assertIsNone(self.jobs.nodes.claim(self.node,{'exclude_attempts':[old['attempt_id']]})['grant'])
        self.assertEqual(self.jobs.get(old['id']),old);self.assertIsNone(self.jobs.access.user('bob'))
        with self.jobs.access.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM tokens WHERE user='bob'").fetchone()[0],0)
        self.assertEqual(BusyActivationTests.finish(self,old)['state'],'succeeded')
        self.assertEqual(self.fixture.complete()['state'],'succeeded')
        self.assertEqual(len(self.jobs.attempts(probe['job']['id'])),1)

    def test_own_reserved_probe_blocks_activation_and_retains_device_reservation(self):
        probe=self.fixture.submit();grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],probe['job']['id'])
        before=self.jobs.get(grant['job_id'])
        with self.jobs.transaction() as db,self.assertRaisesRegex(JobError,'staged owner executions'):
            profile=self.jobs.access.registration('bob')['profile'];self.case.activation.readiness(self.case.spec,db,profile,device_access=False)
        self.assertEqual(self.jobs.get(grant['job_id']),before);self.assertEqual(before['allocation']['npu_ids'],[0])
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_own_queued_probe_blocks_final_activation_without_changing_its_job(self):
        probe=self.fixture.submit();before=self.jobs.get(probe['job']['id'])
        with self.assertRaisesRegex(JobError,'staged owner executions'):self.case.run_activation()
        self.assertEqual(self.jobs.get(before['id']),before);self.assertEqual(self.jobs.attempts(before['id']),[])

    def test_another_explicit_fixed_probe_can_queue_while_original_probe_runs(self):
        first=self.fixture.submit();grant=self.jobs.nodes.claim(self.node)['grant']
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        self.assertTrue(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted']);self.jobs.nodes.update(self.node,{**auth,'running':True})
        before=self.jobs.get(first['job']['id']);second=self.fixture.submit(operation_id='d'*32,device_id=1)
        self.assertNotEqual(first['job']['id'],second['job']['id']);self.assertEqual(second['job']['state'],'queued')
        self.assertEqual(self.jobs.get(before['id']),before);self.assertIsNone(self.jobs.access.user('bob'))

    def test_current_npu_qualification_can_activate_while_foreign_cpu_work_continues(self):
        self.fixture.submit();self.fixture.complete();old=BusyActivationTests.foreign(self)
        self.assertTrue(self.case.run_activation()['id']);self.assertIsNotNone(self.jobs.access.user('bob'))
        self.assertEqual(self.jobs.get(old['id']),old);self.assertEqual(len(self.jobs.attempts(old['id'])),1)


if __name__=='__main__':unittest.main()

"""Static probe envelopes reject impossibility and retain ordinary queueing."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_device_probe as builder
import test_owner_activation_busy as busy_builder
import test_owner_provision as root_builder
import owner_provision as master
from jobs import JobError
from owner_device_probe import TABLE,initialize,advance
from resources import require_managed_capacity
from deployment import DeployError


class ProbeCapacityTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.StagedDeviceProbeTests();self.base.setUp()
        self.jobs=self.base.jobs;self.node=self.base.node
        self.jobs.access.set_quota('alice',{'npu':1});initialize(self.jobs)
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.node,db);p=node['profile'];p['templates'].append('environment_probe')
            record=self.jobs.nodes.handoff.get(self.base.spec['plan_id'],db);record['after_profile']=p;self.jobs.nodes.handoff.save(db,record)
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(p),self.node))

    def tearDown(self):self.base.tearDown()

    def test_impossible_staged_memory_is_rejected_before_authorization_or_job(self):
        # The target Node has512MiB;385MiB payload plus128MiB managed overhead
        # can never fit even when every CPU, card and workspace is free.
        with self.assertRaises(JobError):self.base.submit(memory_mib=385)
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM storage_allocations WHERE kind='archive'").fetchone()[0],0)
        self.assertIsNone(self.jobs.access.user('bob'))

    def ordinary(self,template='npu_access_probe',memory=256,key=None,node=None,device=0):
        parameters={'node_id':node or self.node,'memory_mib':memory}
        if template=='npu_access_probe':parameters['device_id']=device
        value={'environment':'worker-python','template':template,'parameters':parameters}
        if key:value['idempotency_key']=key
        return self.jobs.submit(value,self.jobs.access.user('alice'))

    def domain(self,memory=None,cpus=None):
        d=self.jobs.resources.get(self.jobs.backend.physical_id);policy=copy.deepcopy(d['policy'])
        if memory is not None:policy.update(memory_mib=memory,shm_mib=0)
        if cpus is not None:policy.update(cpu_ids=cpus,max_running=1)
        self.jobs.resources.configure(d['physical_id'],policy,d['revision']);return d

    def no_job(self):
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM storage_allocations WHERE kind='archive'").fetchone()[0],0)

    def onboarding(self,memory=256,npu=True):
        v=root_builder.spec();v['owner']='charlie';v['delivery'].update(owner='charlie',parent='/home/charlie',uid=23803,gid=23803)
        v['quota']['npu']=1 if npu else 0;v['nodes'][0].update(node_id=self.node,uid=23803,gid=23803)
        if npu:v['device_probes']=[{'node_id':self.node,'device_ids':[0],'memory_mib':memory}]
        return master.request(v)

    def test_exact_node_boundary_includes_managed_overhead(self):
        value=self.base.submit(memory_mib=384);grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],value['job']['id']);self.assertEqual(grant['resources']['memory_mib'],384)
        self.assertEqual(grant['allocation']['execution_overhead_mib'],128)

    def test_physical_domain_limit_is_checked_even_when_node_envelope_is_larger(self):
        self.domain(memory=384)
        with self.assertRaisesRegex(JobError,'physical domain memory budget'):self.base.submit(memory_mib=257)
        self.no_job()
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],0)

    def test_fixed_probe_needs_a_cpu_in_the_current_node_domain_intersection(self):
        node_cpu=self.jobs.nodes.node(self.node)['profile']['policy']['cpu_ids'][0]
        other=[i for i in self.jobs.policy['cpu_ids'] if i!=node_cpu];self.assertTrue(other)
        self.domain(cpus=other[:1])
        with self.assertRaisesRegex(JobError,'shared CPU capacity'):self.base.submit()
        self.no_job()

    def test_busy_but_capable_target_still_authorizes_one_queued_probe(self):
        old=busy_builder.BusyActivationTests.foreign(self);before=self.jobs.get(old['id']);probe=self.base.submit()
        self.assertEqual(probe['job']['state'],'queued')
        self.assertIsNone(self.jobs.nodes.claim(self.node,{'exclude_attempts':[old['attempt_id']]})['grant'])
        self.assertEqual(self.jobs.get(old['id']),before);self.assertEqual(self.jobs.attempts(probe['job']['id']),[])

    def test_ordinary_device_probe_rejects_impossible_memory_without_reserving_archive(self):
        with self.assertRaisesRegex(JobError,'Node memory budget'):self.ordinary(memory=385)
        self.no_job()

    def test_environment_probe_uses_the_same_static_managed_envelope(self):
        with self.assertRaisesRegex(JobError,'Node memory budget'):self.ordinary('environment_probe',385)
        self.no_job();value,_=self.ordinary('environment_probe',384)
        self.assertEqual(value['state'],'queued')

    def test_ordinary_probe_cannot_wait_forever_for_an_unknown_node(self):
        with self.assertRaises(JobError):self.ordinary(node='worker-'+'f'*12)
        self.no_job()

    def test_ordinary_probe_requires_an_explicit_target_owner_mapping(self):
        p=self.jobs.nodes.node(self.node)['profile']
        peer,_=self.jobs.nodes.enroll({'name':'bob_only','physical_id':p['physical_id'],'image':p['image'],'templates':['npu_access_probe'],
            'users':{'bob':p['users']['bob']},'policy':p['policy'],'npu':p['npu']})
        with self.assertRaisesRegex(JobError,'does not map this owner'):self.ordinary(node=peer)
        self.no_job()

    def test_target_template_and_device_must_already_be_registered(self):
        with self.assertRaisesRegex(JobError,'outside this Node'):self.ordinary(device=2)
        self.jobs.nodes.set_templates(self.node,['cpu_demo'])
        with self.assertRaisesRegex(JobError,'explicitly authorize'):self.ordinary()
        self.no_job()

    def test_onboarding_checks_selected_memory_before_any_stage_or_journal(self):
        with self.assertRaisesRegex(DeployError,'Node memory budget'):master._nodes(self.jobs.runtime,self.onboarding(385))
        with self.jobs.access.connect() as db:self.assertIsNone(db.execute("SELECT 1 FROM users WHERE id='charlie'").fetchone())
        self.assertEqual(master._nodes(self.jobs.runtime,self.onboarding(384))[self.node]['physical_id'],self.jobs.backend.physical_id)

    def test_cpu_onboarding_minimum_also_includes_managed_overhead(self):
        p=self.jobs.nodes.node(self.node)['profile']['policy'];self.jobs.nodes.set_policy(self.node,{**p,'memory_mib':256},2)
        with self.assertRaisesRegex(DeployError,'Node memory budget'):master._nodes(self.jobs.runtime,self.onboarding(npu=False))

    def test_onboarding_checks_physical_domain_and_cpu_capacity_from_one_snapshot(self):
        self.domain(memory=256)
        with self.assertRaisesRegex(DeployError,'physical domain memory budget'):master._nodes(self.jobs.runtime,self.onboarding())
        self.no_job()

    def test_preflight_does_not_reject_a_static_envelope_just_because_it_is_draining(self):
        d=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(d['physical_id'],d['policy'],d['revision'],draining=True)
        self.assertEqual(set(master._nodes(self.jobs.runtime,self.onboarding())),{self.node})

    def test_ordinary_idempotent_replay_observes_original_job_after_capacity_shrink(self):
        first,_=self.ordinary(key='ordinary-original');self.domain(memory=256)
        again,reused=self.ordinary(key='ordinary-original');self.assertTrue(reused);self.assertEqual(first['id'],again['id'])
        with self.assertRaisesRegex(JobError,'memory budget'):self.ordinary(key='ordinary-new')
        self.assertEqual(len(self.jobs.all()),1);self.assertEqual(self.jobs.get(first['id'])['allocation'],{})

    def test_staged_historical_operation_keeps_its_original_job_after_capacity_shrink(self):
        first=self.base.submit();self.domain(memory=256);again=self.base.submit()
        self.assertEqual(first['job']['id'],again['job']['id']);self.assertTrue(again['historical_receipt_only'])
        state=advance(self.jobs,self.base.spec);self.assertEqual(state['status'],'attention');self.assertEqual(state['job_id'],first['job']['id'])
        self.assertEqual(len(self.jobs.all()),1);self.assertEqual(self.jobs.get(first['job']['id'])['allocation'],{})

    def test_admission_race_creates_no_job_and_retains_only_the_original_authorization(self):
        original=self.jobs.normalize;before=self.jobs.resources.get(self.jobs.backend.physical_id);changed=[]
        def shrink(*args,**kwargs):
            result=original(*args,**kwargs)
            if not changed:self.domain(memory=384);changed.append(True)
            return result
        with patch.object(self.jobs,'normalize',side_effect=shrink),self.assertRaisesRegex(JobError,'physical domain memory budget'):
            self.base.submit(memory_mib=384)
        self.no_job()
        with self.jobs.connect() as db:
            rows=db.execute('SELECT id,record FROM '+TABLE).fetchall();self.assertEqual(len(rows),1);identifier=rows[0][0]
            self.assertIsNone(json.loads(rows[0][1])['job_id'])
        current=self.jobs.resources.get(before['physical_id']);self.jobs.resources.configure(before['physical_id'],before['policy'],current['revision'])
        value=self.base.submit(memory_mib=384);self.assertEqual(value['id'],identifier)
        self.assertEqual(len(self.jobs.all()),1)

    def test_pure_static_check_has_exact_boundary_and_rejects_invalid_memory(self):
        policy={'cpu_ids':[1],'memory_mib':65664,'shm_mib':0,'max_running':1}
        require_managed_capacity({'policy':policy},{'policy':policy},65536)
        for memory in [True,255,65537]:
            with self.subTest(memory=memory),self.assertRaises(JobError):require_managed_capacity({'policy':policy},{'policy':policy},memory)


if __name__=='__main__':unittest.main()

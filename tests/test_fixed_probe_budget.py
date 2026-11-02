"""Small fixed checks retain historical fingerprints and frozen allocations."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_probe_capacity as builder
import test_environment_contract as environment_builder
from io_policy import artifacts,for_job,MIB,META,submission_artifacts
from jobs import JobError,JobStore


class FixedProbeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.ProbeCapacityTests();self.base.setUp()
        self.jobs=self.base.jobs;self.node=self.base.node

    def tearDown(self):self.base.tearDown()

    def request(self,template='environment_probe',key='probe'):
        parameters={'node_id':self.node}
        if template=='npu_access_probe':parameters['device_id']=0
        return {'template':template,'environment':'worker-python',
                'parameters':parameters,'idempotency_key':key}

    def submit(self,body):return self.jobs.submit(body,self.jobs.access.user('alice'))

    def low_io(self,total=2):
        self.jobs.policy['io_limits']={'file_mib':1,'input_mib':1,'artifact_mib':total}

    def allocations(self):
        with self.jobs.connect() as db:
            return [tuple(row) for row in db.execute("SELECT id,bytes,settled FROM storage_allocations WHERE kind='archive' ORDER BY id")]

    def test_both_fixed_templates_fit_small_io_and_reserve_metadata_separately(self):
        self.low_io()
        self.jobs.access.set_quota('alice',{'archive_storage_mib':20})
        for template in ['environment_probe','npu_access_probe']:
            value,reused=self.submit(self.request(template,key=template))
            self.assertFalse(reused);self.assertEqual(value['artifact_limits'],{'file_mib':1,'total_mib':2})
        self.assertEqual(len(self.allocations()),2)
        self.assertTrue(all(size==2*MIB+META and settled==0 for _,size,settled in self.allocations()))

    def test_administrator_artifact_ceiling_is_still_enforced(self):
        self.low_io(total=1)
        for template in ['environment_probe','npu_access_probe']:
            with self.subTest(template=template),self.assertRaisesRegex(JobError,'IO limits'):
                self.submit(self.request(template,key=template))
        self.assertEqual(self.jobs.all(),[]);self.assertEqual(self.allocations(),[])

    def test_ten_mib_boundary_includes_eight_mib_metadata(self):
        self.low_io();self.jobs.access.set_quota('alice',{'archive_storage_mib':9})
        with self.assertRaisesRegex(JobError,'archive storage budget'):self.submit(self.request())
        self.assertEqual(self.jobs.all(),[]);self.assertEqual(self.allocations(),[])
        self.jobs.access.set_quota('alice',{'archive_storage_mib':10})
        value,_=self.submit(self.request());self.assertEqual(self.allocations(),[(value['id'],10*MIB,0)])
        with self.assertRaisesRegex(JobError,'archive storage budget'):self.submit(self.request(key='another'))
        self.assertEqual(len(self.jobs.all()),1)

    def test_legacy_probe_replay_after_reopen_keeps_fingerprint_request_and_allocation(self):
        body=self.request()
        # Reproduce the previous allocator on the same actual normalized request.
        with patch('io_policy.submission_artifacts',return_value=artifacts()):old,_=self.submit(body)
        self.assertEqual(old['artifact_limits'],artifacts());before=self.allocations()
        path=self.jobs.directory(old['id'])/'request.json';raw=path.read_bytes()
        with self.jobs.connect() as db:
            fingerprint=db.execute('SELECT fingerprint FROM jobs WHERE id=?',(old['id'],)).fetchone()[0]
        self.jobs.access.set_quota('alice',{'archive_storage_mib':9})
        config=copy.deepcopy(self.jobs.access.config)
        config.setdefault('scheduler',{})['io_limits']={'file_mib':1,'input_mib':1,'artifact_mib':2}
        self.jobs=JobStore(self.jobs.runtime,config)
        with patch.object(self.jobs.storage,'reserve',side_effect=AssertionError('Replay must not allocate')):
            same,reused=self.submit(body)
        self.assertTrue(reused);self.assertEqual(same,old);self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.allocations(),before)
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT fingerprint FROM jobs WHERE id=?',(old['id'],)).fetchone()[0],fingerprint)
        self.jobs.access.set_quota('alice',{'archive_storage_mib':146})
        new,reused=self.submit(self.request(key='new-small-probe'))
        self.assertFalse(reused);self.assertEqual(for_job(new),{'file_mib':1,'total_mib':2})
        self.assertEqual(sum(size for _,size,_ in self.allocations()),146*MIB)
        with self.assertRaises(JobError):self.submit({**body,'name':'different'})

    def test_new_probe_replay_never_duplicates_storage(self):
        self.low_io();first,_=self.submit(self.request());before=self.allocations()
        same,reused=self.submit(self.request())
        self.assertTrue(reused);self.assertEqual(same,first);self.assertEqual(self.allocations(),before)

    def test_legacy_device_probe_also_returns_its_original_budget(self):
        body=self.request('npu_access_probe')
        with patch('io_policy.submission_artifacts',return_value=artifacts()):old,_=self.submit(body)
        before=self.allocations();self.low_io()
        same,reused=self.submit(body)
        self.assertTrue(reused);self.assertEqual(same,old);self.assertEqual(self.allocations(),before)
        self.assertEqual(for_job(same),artifacts())

    def test_caller_cannot_change_fixed_budget_or_add_workload(self):
        for template in ['environment_probe','npu_access_probe']:
            body=self.request(template)
            for changes in [{'artifacts':{'file_mib':1,'total_mib':1}},{'argv':['sh']},{'output':'/tmp'}]:
                with self.subTest(template=template,changes=changes),self.assertRaises(JobError):
                    self.submit({**body,'parameters':{**body['parameters'],**changes}})
            with self.assertRaises(JobError):self.submit({**body,'artifact_limits':{'file_mib':1,'total_mib':2}})
        self.assertEqual(self.jobs.all(),[])

    def test_workflow_legacy_and_old_missing_limits_keep_their_own_contracts(self):
        self.assertEqual(submission_artifacts({}),artifacts())
        self.assertEqual(for_job({'template':'environment_probe'}),artifacts())
        explicit={'file_mib':3,'total_mib':7}
        self.assertEqual(submission_artifacts({'template':'ir_v1','parameters':{'artifacts':explicit}}),explicit)
        self.assertEqual(submission_artifacts({'parameters':{'artifacts':explicit}}),explicit)
        self.low_io()
        with self.assertRaisesRegex(JobError,'IO limits'):
            self.jobs.normalize({'argv':['python','-c','print(42)']})

    def test_old_node_waits_without_attempt_then_updated_node_claims_same_probe(self):
        from io_policy import FIXED_PROBE_CAPABILITY
        case=environment_builder.AdmissionTests();case.setUp()
        try:
            capabilities=case.jobs.nodes.node(case.node)['report']['capabilities']
            case.beat(capabilities=[c for c in capabilities if c!=FIXED_PROBE_CAPABILITY])
            body={'template':'environment_probe','environment':'worker-python','parameters':{'node_id':case.node}}
            job,_=case.jobs.submit(body,case.alice)
            self.assertIsNone(case.jobs.nodes.claim(case.node)['grant'])
            self.assertEqual(case.jobs.attempts(job['id']),[])
            self.assertIn('固定探针',case.jobs.get(job['id'])['reason'])
            case.beat()
            self.assertIn(FIXED_PROBE_CAPABILITY,case.jobs.nodes.node(case.node)['report']['capabilities'])
            grant=case.jobs.nodes.claim(case.node)['grant']
            self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['artifact_limits'],{'file_mib':1,'total_mib':2})
        finally:case.tearDown()

    def test_old_frozen_probe_can_still_claim_an_old_node(self):
        from io_policy import FIXED_PROBE_CAPABILITY
        case=environment_builder.AdmissionTests();case.setUp()
        try:
            capabilities=case.jobs.nodes.node(case.node)['report']['capabilities']
            case.beat(capabilities=[c for c in capabilities if c!=FIXED_PROBE_CAPABILITY])
            with patch('io_policy.submission_artifacts',return_value=artifacts()):
                job,_=case.jobs.submit({'template':'environment_probe','environment':'worker-python','parameters':{'node_id':case.node}},case.alice)
            grant=case.jobs.nodes.claim(case.node)['grant']
            self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['artifact_limits'],artifacts())
        finally:case.tearDown()

    def test_actual_node_preflight_accepts_new_and_historical_environment_budgets(self):
        from container_engine import DockerAttempt,SCRIPTS
        from workflow_spec import runtime_manifest
        cpu=self.jobs.policy['cpu_ids'][0]
        grant={'owner':'alice','template':'environment_probe','node_id':self.node,
            'parameters':{'node_id':self.node,'tools':[],'files':[],'packages':[]},
            'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},'allocation':{'cpu_ids':[cpu]},
            'timeout_seconds':120,'source_sha256':hashlib.sha256(SCRIPTS['environment_probe'].read_bytes()).hexdigest(),
            'runtime_files':runtime_manifest()}
        engine=DockerAttempt.__new__(DockerAttempt)
        engine.config={'users':{'alice':{'uid':23001,'gid':23001}},'image':'sha256:'+'a'*64,
                       'policy':{'cpu_ids':[cpu],'memory_mib':512}}
        engine.grant=grant
        for limits in [{'file_mib':1,'total_mib':2},artifacts()]:
            grant['artifact_limits']=limits
            self.assertEqual(engine.validate()['uid'],23001)
        for limits in [{'file_mib':1,'total_mib':1},{'file_mib':1,'total_mib':3}]:
            grant['artifact_limits']=limits
            with self.assertRaisesRegex(ValueError,'pinned task request'):engine.validate()
        grant['artifact_limits']={'file_mib':1,'total_mib':2}
        engine.config['io_limits']={'file_mib':1,'input_mib':1,'artifact_mib':1}
        with self.assertRaisesRegex(ValueError,'node ceiling'):engine.validate()


if __name__=='__main__':unittest.main()

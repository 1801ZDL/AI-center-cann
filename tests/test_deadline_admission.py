"""Administrator task ceiling and real node-declared deadline admission."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,defaults
from workflow_spec import CAPABILITIES,DEADLINE_CAPABILITY,MAX_TIMEOUT_SECONDS


class DeadlineAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='deadline-test-');self.root=Path(self.temp.name)
        self.jobs=JobStore(self.root,{'username':'operator','password':'unit-fixture','container':'unused'})
        workspace=self.root/'alice';workspace.mkdir();self.jobs.access.create_user('alice',workspace,environments=['worker-python'])
        self.alice=self.jobs.access.user('alice')
        value,_=self.jobs.inputs.create(self.alice,{'files':[{'name':'empty.py','size':0,'sha256':hashlib.sha256(b'').hexdigest()}]})
        self.jobs.inputs.seal(value['id'],self.alice)
        self.request={'template':'build_v1','environment':'worker-python','parameters':{'input_id':value['id'],
            'argv':['python','empty.py'],'lock':{'python':'3.11.15'},'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':7200}}
        profile={'name':'deadline-node','physical_id':self.jobs.backend.physical_id,'users':{'alice':{'uid':23900,'gid':23900}},
            'templates':['build_v1'],'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':1024,'shm_mib':0,'max_running':1}}
        self.node,_=self.jobs.nodes.enroll(profile)

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def beat(self,maximum=None):
        body={'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',*sorted(CAPABILITIES)],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':16*1024**3,'disk_free_bytes':16*1024**3}
        if maximum is not None:body['capabilities'].append(DEADLINE_CAPABILITY);body['max_timeout_seconds']=maximum
        self.jobs.nodes.heartbeat(self.node,body)

    def test_default_controller_ceiling_still_rejects_long_workflow_before_job_creation(self):
        with self.assertRaises(JobError):self.jobs.submit(self.request,self.alice)
        self.assertFalse(self.jobs.all())

    def test_explicit_admin_ceiling_accepts_long_job_but_unconfirmed_legacy_node_cannot_claim(self):
        self.jobs.policy['max_timeout_seconds']=7200;self.beat()
        job,_=self.jobs.submit(self.request,self.alice)
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)
        self.beat(7200);grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['timeout_seconds'],7200);self.assertEqual(grant['job_id'],job['id'])

    def test_insufficient_reported_deadline_preserves_the_same_waiting_job_and_resources(self):
        self.jobs.policy['max_timeout_seconds']=7200;self.beat(300)
        job,_=self.jobs.submit(self.request,self.alice)
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.assertEqual(self.jobs.summary()['reserved']['memory_mib'],0)
        self.beat(7200);grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],job['id'])
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)

    def test_changed_receipt_does_not_discard_existing_attempt(self):
        self.jobs.policy['max_timeout_seconds']=7200;self.beat(7200)
        job,_=self.jobs.submit(self.request,self.alice);grant=self.jobs.nodes.claim(self.node)['grant']
        self.beat(300);self.assertEqual(self.jobs.nodes.claim(self.node)['grant'],grant)
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)

    def test_invalid_node_bounds_are_rejected_without_overwriting_last_accepted_receipt(self):
        self.beat(300)
        for value in [True,0,-1,MAX_TIMEOUT_SECONDS+1,'7200']:
            with self.subTest(value=value),self.assertRaises(JobError):self.beat(value)
        self.assertEqual(self.jobs.nodes.node(self.node)['report']['max_timeout_seconds'],300)

    def test_admin_bound_is_finite_and_typed(self):
        for value in [True,0,-1,MAX_TIMEOUT_SECONDS+1,'7200']:
            with self.subTest(value=value),self.assertRaises(ValueError):defaults({'container':'unused','scheduler':{'max_timeout_seconds':value}})
        self.assertEqual(defaults({'container':'unused','scheduler':{'max_timeout_seconds':7200}})[0]['max_timeout_seconds'],7200)


if __name__=='__main__':unittest.main()

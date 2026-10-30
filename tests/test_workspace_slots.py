"""Workspace assignment authority, identity, matching and no silent fallback."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError
from workspace_slots import CAPABILITY,allocation,fingerprint,observation,selected,validate
from workflow_spec import CAPABILITIES,runtime_manifest


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='workspace-test-');self.root=Path(self.temp.name)
        config={'username':'quota-admin','password':'test-quota-admin','container':'zdl'}
        self.jobs=JobStore(self.root,config);self.nodes=self.jobs.nodes
        work=self.root/'alice';work.mkdir();self.jobs.access.create_user('alice',work,environments=['worker-python','host-python'])
        self.alice=self.jobs.access.user('alice')
        self.slot={'id':'work0','storage_id':'a'*32,'path':'/srv/lab/work0','filesystem':'tmpfs',
                   'bytes':32*1024**2,'inodes':512,'device':12345,'root_inode':1}
        self.policy={'slots':[self.slot]}
        self.profile={'name':'bounded','physical_id':self.jobs.backend.physical_id,
            'users':{'alice':{'uid':23001,'gid':23001}},'templates':['cpu_demo','ir_v1'],
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1},
            'workspace':self.policy}
        self.node,self.key=self.nodes.enroll(self.profile)
        self.beat()

    def tearDown(self):self.temp.cleanup()

    def beat(self,clean=True,status='ok'):
        self.nodes.heartbeat(self.node,{'physical_id':self.jobs.backend.physical_id,
             'protocol_version':1,'capabilities':['start-ticket-v1','cleanup-before-complete-v1',*CAPABILITIES,CAPABILITY],
             'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
             'workspace_observation':{'policy_sha256':fingerprint(self.policy),'slots':[{'id':'work0','status':status,
                        'clean':clean,'free_bytes':32*1024**2,'free_inodes':512}]}})

    def submit(self,requirement=None):
        if requirement is None:return self.jobs.submit({'template':'cpu_demo','parameters':{'seconds':1},'environment':'worker-python'},self.alice)[0]
        data=b'x';bundle,_=self.jobs.inputs.create(self.alice,{'files':[{'name':'input.mlir','size':1,'sha256':hashlib.sha256(data).hexdigest()}]})
        # Use the same input API as normal clients, without running a task in these checks.
        import base64
        if not bundle['sealed']:
            self.jobs.inputs.upload(bundle['id'],self.alice,{'name':'input.mlir','offset':0,'data':base64.b64encode(data).decode()})
            self.jobs.inputs.seal(bundle['id'],self.alice)
        return self.jobs.submit({'template':'ir_v1','environment':'worker-python','parameters':{
            'input_id':bundle['id'],'argv':['python','ir.py'],'lock':{'python':'3.10.12'},'workspace':requirement}},self.alice)[0]

    def test_allocation_pins_private_contract_without_exposing_host_path(self):
        job=self.submit();grant=self.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['allocation']['workspace'],allocation(self.slot))
        self.assertEqual(grant['workspace_policy_sha256'],fingerprint(self.policy))
        self.assertEqual(selected(self.policy,grant),self.slot)
        self.assertNotIn('/srv/lab',json.dumps(grant))
        self.assertEqual(self.nodes.claim(self.node)['grant'],grant)
        with self.jobs.transaction() as db:
            current=self.jobs.get(job['id'],db,self.alice);current['state']='needs_attention';self.jobs.write(db,current)
        self.assertEqual(self.nodes.claim(self.node)['grant']['attempt_id'],grant['attempt_id'])
        self.assertIsNone(self.nodes.claim(self.node,{'exclude_attempts':[grant['attempt_id']]})['grant'])

    def test_alias_storage_rejected_even_if_old_node_disabled_but_attempt_unknown(self):
        job=self.submit();grant=self.nodes.claim(self.node)['grant'];self.nodes.disable(self.node)
        for changes in [{'storage_id':'b'*32,'device':54321}, {'storage_id':'b'*32,'path':'/other'},
                        {'path':'/other','device':54321}]:
            profile={**self.profile,'name':'alias','workspace':{'slots':[{**self.slot,**changes}]}}
            with self.assertRaises(JobError):self.nodes.enroll(profile)

    def test_dirty_or_unavailable_slot_does_not_receive_new_work(self):
        self.submit()
        for clean,status in [(False,'ok'),(False,'error')]:
            self.beat(clean,status);self.assertIsNone(self.nodes.claim(self.node)['grant'])
        self.beat();self.assertIsNotNone(self.nodes.claim(self.node)['grant'])

    def test_completion_without_explicit_workspace_release_does_not_free_allocation(self):
        job=self.submit();grant=self.nodes.claim(self.node)['grant'];auth={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
        with self.assertRaisesRegex(JobError,'workspace cleanup'):
            self.nodes.complete(self.node,{**auth,'result':{'status':'failed','exit_code':1,
                'termination_confirmed':True,'execution_cleanup_confirmed':True},'artifacts':[]})
        self.assertEqual(self.jobs.get(job['id'])['state'],'reserved')

    def test_administrator_can_require_hard_bounds_even_for_plain_cpu_templates(self):
        self.jobs.policy['require_bounded_workspace']=True
        profile={key:value for key,value in self.profile.items() if key!='workspace'};profile['name']='ordinary'
        ordinary,_=self.nodes.enroll(profile)
        self.nodes.heartbeat(ordinary,{'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        job=self.submit();self.assertIsNone(self.nodes.claim(ordinary)['grant'])
        self.assertEqual(self.nodes.claim(self.node)['grant']['job_id'],job['id'])

    def test_finite_mount_contract_and_forged_observations_are_rejected(self):
        for change in [{'bytes':0},{'inodes':0},{'path':'/'},{'path':'/safe/../unsafe'},{'filesystem':'bind'},{'filesystem':'xfs'}]:
            with self.assertRaises(ValueError):validate({'slots':[{**self.slot,**change}]})
        with self.assertRaises(ValueError):validate({'slots':[self.slot,{**self.slot,'id':'other','storage_id':'b'*32,'path':'/other'}]})
        with self.assertRaises(ValueError):observation({'policy_sha256':'0'*64,'slots':[]},self.policy)
        if os.geteuid()!=0:
            with self.assertRaisesRegex(ValueError,'trusted root'):validate(self.policy,host=True)

    def test_volatility_and_capacity_requirements_cannot_silently_fall_back(self):
        for requirement in [{'min_mib':33,'min_inodes':1,'durable':False},
                            {'min_mib':1,'min_inodes':513,'durable':False},
                            {'min_mib':1,'min_inodes':1,'durable':True}]:
            job=self.submit(requirement);self.assertIsNone(self.nodes.claim(self.node)['grant'])
            self.jobs.cancel(job['id'],self.alice)
        job=self.submit({'min_mib':16,'min_inodes':64,'durable':False})
        unbounded={key:value for key,value in self.profile.items() if key!='workspace'};unbounded['name']='unbounded'
        ordinary,_=self.nodes.enroll(unbounded)
        self.nodes.heartbeat(ordinary,{'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',*CAPABILITIES],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
        self.assertIsNone(self.nodes.claim(ordinary)['grant']);self.assertEqual(self.nodes.claim(self.node)['grant']['job_id'],job['id'])


if __name__=='__main__':unittest.main()

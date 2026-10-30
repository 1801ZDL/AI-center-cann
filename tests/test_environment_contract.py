"""Environment admission: immutable facts, negative placement, protected provenance."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from environment_contract import CAPABILITY,PROBE,differences,fingerprint,from_receipt,lock_for,probe_parameters,validate,verify_config
from jobs import ROOT,JobStore,JobError,atomic_json
from workflow_spec import CAPABILITIES

IMAGE='sha256:'+'a'*64

def contract():
    return {'format':'ascend-lab-node-environment/v1','image_sha256':IMAGE,'environment_setup':None,
      'observed':{'python':'3.10.12','python_executable':'/usr/bin/python3','machine':'x86_64','image_sha256':IMAGE,
        'packages':{'torch':None,'triton':'3.6.0'},'tools':{'cc':{'path':'/usr/bin/cc','realpath':'/usr/bin/gcc','sha256':'b'*64}},'files':{}},
      'source':{'job_id':'job-'+'a'*12,'attempt_id':'attempt-'+'b'*32,'receipt_sha256':'c'*64,'node_revision':1}}

class ContractTests(unittest.TestCase):
    def test_probe_memory_is_explicit_bounded_and_defaults_unchanged(self):
        from templates import expand_template
        request={'template':'environment_probe','environment':'worker-python','parameters':{'node_id':'worker-'+'a'*12}}
        default=expand_template(request);large=expand_template({**request,'parameters':{**request['parameters'],'memory_mib':32768}})
        self.assertEqual(default['memory_mib'],256);self.assertEqual(large['memory_mib'],32768)
        self.assertEqual(large['cpu'],1);self.assertEqual(large['npu'],0);self.assertEqual(default['argv'],large['argv'])
        self.assertEqual(probe_parameters(large['parameters']),large['parameters'])
    def test_probe_memory_cannot_supply_boolean_unbounded_values_or_extra_execution_controls(self):
        node={'node_id':'worker-'+'a'*12}
        for value in [True,False,255,65537,-1,'32768']:
            with self.subTest(value=value),self.assertRaises(ValueError):probe_parameters({**node,'memory_mib':value})
        for key in ['cpu','npu','argv','mounts']:
            with self.subTest(key=key),self.assertRaises(ValueError):probe_parameters({**node,'memory_mib':32768,key:1})
    def test_exact_known_facts_and_roundtrip_lock(self):
        c=contract();self.assertEqual(differences(lock_for(c),c),[])
        for lock in [{'python':'3.11.0'},{'image_sha256':'sha256:'+'d'*64},{'machine':'aarch64'},
                     {'packages':{'triton':'3.5.0'}},{'tools':{'cc':'e'*64}},{'files':{'/opt/new':'f'*64}}]:
            with self.subTest(lock=lock):self.assertTrue(differences(lock,c))
    def test_unknown_is_not_proof_of_absence(self):
        self.assertEqual(differences({'packages':{'torch':'__absent__'}},contract()),[])
        self.assertEqual(differences({'packages':{'unqueried':'__absent__'}},contract()),['packages.unqueried'])
    def test_fingerprint_binds_facts_and_provenance(self):
        c=contract();before=fingerprint(c);c['source']['node_revision']=2
        self.assertNotEqual(before,fingerprint(c))
    def test_probe_cannot_supply_command_resource_or_mutable_path(self):
        for change in [{'argv':['sh']},{'npu':1},{'files':['/work/private']},{'files':['/opt/../etc/shadow']},
                       {'tools':['arbitrary']},{'packages':['bad/name']},{'node_id':'any'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):probe_parameters({'node_id':'worker-'+'a'*12,**change})
    def test_certified_config_binds_image_setup_and_interpreter(self):
        cfg={'image':IMAGE,'python':'/usr/bin/python3','environment_manifest':contract()}
        self.assertEqual(verify_config(cfg),fingerprint(contract()))
        for change in [{'image':'sha256:'+'e'*64},{'python':'/opt/other/python'},{'environment_setup':'/opt/setup.sh'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):verify_config({**cfg,**change})
    def test_only_nonroot_fixed_probe_receipt_can_certify(self):
        receipt={'schema':'ascend-lab-environment/v1','probe_only':True,'locked':True,'workload_started':False,
                 'probe_exit_code':0,'observed':contract()['observed']}
        meta={'runtime':'managed-docker','privileged':False,'read_only_root':True,'network_mode':'none','task_uid':23003,'image':IMAGE}
        job={'id':'job-'+'a'*12,'attempt_id':'attempt-'+'b'*32}
        self.assertEqual(from_receipt(receipt,meta,job,1)['observed'],contract()['observed'])
        for change in [{'probe_only':False},{'workload_started':True},{'probe_exit_code':78},{'locked':False}]:
            with self.subTest(change=change),self.assertRaises(ValueError):from_receipt({**receipt,**change},meta,job,1)
        for change in [{'task_uid':0},{'privileged':True},{'network_mode':'host'},{'read_only_root':False}]:
            with self.subTest(change=change),self.assertRaises(ValueError):from_receipt(receipt,{**meta,**change},job,1)

class AdmissionTests(unittest.TestCase):
    def test_explicit_probe_memory_requires_updated_node_before_any_attempt(self):
        from environment_contract import PROBE_MEMORY_CAPABILITY
        job,_=self.jobs.submit({'template':PROBE,'environment':'worker-python','parameters':{'node_id':self.node,'memory_mib':256}},self.alice)
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.beat(capabilities=['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY,PROBE_MEMORY_CAPABILITY,*CAPABILITIES])
        self.assertEqual(self.jobs.nodes.claim(self.node)['grant']['job_id'],job['id'])
    def test_explicit_probe_memory_still_obeys_normal_resource_quota(self):
        with self.assertRaises(JobError):
            self.jobs.submit({'template':PROBE,'environment':'worker-python','parameters':{'node_id':self.node,'memory_mib':32768}},self.alice)
        self.assertFalse(self.jobs.all())
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-env-');self.runtime=Path(self.temp.name)
        self.jobs=JobStore(self.runtime,{'username':'env-admin','password':'test-only','container':'zdl'})
        work=self.runtime/'alice';work.mkdir();self.jobs.access.create_user('alice',work,environments=['worker-python'])
        self.alice=self.jobs.access.user('alice');self.jobs.policy['require_environment_contract']=True
        profile={'name':'environment-node','physical_id':self.jobs.backend.physical_id,'image':IMAGE,
          'users':{'alice':{'uid':23001,'gid':23001}},'templates':[PROBE,'ir_v1'],
          'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1}}
        self.node,self.key=self.jobs.nodes.enroll(profile);self.beat()
    def tearDown(self):self.temp.cleanup()
    def beat(self,**updates):
        body={'physical_id':self.jobs.backend.physical_id,'policy_revision':1,'protocol_version':1,
          'capabilities':['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY,*CAPABILITIES],
          'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
          'image_sha256':IMAGE,'environment_setup':None,'python_executable':'/usr/bin/python3',**updates}
        self.jobs.nodes.heartbeat(self.node,body)
    def workflow(self,lock):
        data=b'print(42)';item={'name':'main.py','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        bundle,_=self.jobs.inputs.create(self.alice,{'files':[item]})
        if not bundle['sealed']:
            self.jobs.inputs.upload(bundle['id'],self.alice,{'name':'main.py','offset':0,'data':base64.b64encode(data).decode()})
            self.jobs.inputs.seal(bundle['id'],self.alice)
        return self.jobs.submit({'template':'ir_v1','environment':'worker-python','parameters':{
          'input_id':bundle['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12',**lock}}},self.alice)[0]
    def install_manifest(self):
        with self.jobs.connect() as db:
            profile=self.jobs.nodes.node(self.node,db)['profile'];profile['environment_manifest']=contract()
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.node))
        self.beat(environment_sha256=fingerprint(contract()))
    def test_uncertified_workflow_waits_without_reservation(self):
        job=self.workflow({});self.assertIsNone(self.jobs.nodes.claim(self.node).get('grant'))
        self.assertEqual(self.jobs.get(job['id'])['state'],'queued');self.assertEqual(self.jobs.attempts(job['id']),[])
    def test_all_environment_mismatches_wait_without_attempt(self):
        self.install_manifest()
        for lock in [{'image_sha256':'sha256:'+'d'*64},{'python':'3.11.0'},{'machine':'aarch64'},
                     {'packages':{'triton':'wrong'}},{'tools':{'cc':'f'*64}},{'packages':{'unqueried':'__absent__'}}]:
            job=self.workflow(lock);self.assertIsNone(self.jobs.nodes.claim(self.node).get('grant'))
            self.assertEqual(self.jobs.attempts(job['id']),[])
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)
    def test_matching_contract_is_frozen_into_one_attempt(self):
        self.install_manifest();job=self.workflow(lock_for(contract()));grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['environment_sha256'],fingerprint(contract()))
        self.assertEqual(grant,self.jobs.nodes.claim(self.node)['grant'])
    def test_heartbeat_rejects_changed_image_or_contract(self):
        self.install_manifest()
        for change in [{'image_sha256':'sha256:'+'e'*64},{'environment_sha256':'f'*64},
                       {'environment_setup':'/opt/new.sh'},{'python_executable':'/opt/python'}]:
            with self.subTest(change=change),self.assertRaises(JobError):self.beat(environment_sha256=fingerprint(contract()),**change) if 'environment_sha256' not in change else self.beat(**change)
    def test_probe_targets_only_one_node_and_requires_capability(self):
        job,_=self.jobs.submit({'template':PROBE,'environment':'worker-python','parameters':{'node_id':'worker-'+'f'*12}},self.alice)
        self.assertIsNone(self.jobs.nodes.claim(self.node).get('grant'));self.assertFalse(self.jobs.attempts(job['id']))
    def test_certification_revision_and_active_attempt_guards(self):
        job,_=self.jobs.submit({'template':PROBE,'environment':'worker-python','parameters':{'node_id':self.node}},self.alice)
        with self.assertRaises(JobError):self.jobs.nodes.certify_environment(self.node,job['id'],0)
        with self.assertRaises(JobError):self.jobs.nodes.certify_environment(self.node,job['id'],1)
        self.jobs.nodes.claim(self.node)
        with self.assertRaisesRegex(JobError,'Finish or reconcile'):self.jobs.nodes.certify_environment(self.node,job['id'],1)
    def test_certification_installs_revision_and_invalidates_old_heartbeat(self):
        job,_=self.jobs.submit({'template':PROBE,'environment':'worker-python','parameters':{'node_id':self.node}},self.alice)
        grant=self.jobs.nodes.claim(self.node)['grant']
        with self.jobs.transaction() as db:
            j=self.jobs.get(job['id'],db);j['state']='succeeded';self.jobs.write(db,j)
        receipt={'schema':'ascend-lab-environment/v1','probe_only':True,'locked':True,'workload_started':False,
                 'probe_exit_code':0,'observed':contract()['observed']}
        meta={'runtime':'managed-docker','privileged':False,'read_only_root':True,'network_mode':'none','task_uid':23001,'image':IMAGE}
        directory=self.jobs.directory(job['id']);(directory/'results/_lab').mkdir(parents=True)
        atomic_json(directory/'results/_lab/environment.json',receipt);atomic_json(directory/'environment.json',meta)
        profile=self.jobs.nodes.certify_environment(self.node,job['id'],1)
        self.assertEqual(profile['policy_revision'],2)
        with self.assertRaises(JobError):self.beat()
        with self.assertRaises(JobError):self.jobs.nodes.certify_environment(self.node,job['id'],2)
        self.beat(policy_revision=2,environment_sha256=fingerprint(profile['environment_manifest']))

    def test_custom_python_certification_preserves_cache_namespace_identity(self):
        observed=copy.deepcopy(contract()['observed']);observed.update(python='3.11.15',python_executable='/opt/native/bin/python3.11')
        self.beat(python_executable=observed['python_executable'])
        job,_=self.jobs.submit({'template':PROBE,'environment':'worker-python','parameters':{'node_id':self.node}},self.alice)
        self.jobs.nodes.claim(self.node)
        with self.jobs.transaction() as db:
            value=self.jobs.get(job['id'],db);value['state']='succeeded';self.jobs.write(db,value)
        directory=self.jobs.directory(job['id']);(directory/'results/_lab').mkdir(parents=True)
        atomic_json(directory/'results/_lab/environment.json',{'schema':'ascend-lab-environment/v1','probe_only':True,'locked':True,
            'workload_started':False,'probe_exit_code':0,'observed':observed})
        atomic_json(directory/'environment.json',{'runtime':'managed-docker','privileged':False,'read_only_root':True,
            'network_mode':'none','task_uid':23001,'image':IMAGE})
        profile=self.jobs.nodes.certify_environment(self.node,job['id'],1)
        self.assertEqual(profile.get('python'),observed['python_executable'])
        self.assertEqual(verify_config(profile,host=False),fingerprint(profile['environment_manifest']))
        from persistent_cache import namespace
        grant={'owner':'alice','environment_sha256':fingerprint(profile['environment_manifest']),
            'runtime_files':{'bootstrap':'a'*64},'parameters':{'lock':lock_for(profile['environment_manifest'])}}
        self.assertRegex(namespace(profile,grant,host=False),'^[0-9a-f]{64}$')
        self.beat(policy_revision=2,python_executable=observed['python_executable'],environment_sha256=grant['environment_sha256'])

    def test_inconsistent_legacy_interpreter_waits_for_recertification_without_attempt(self):
        manifest=contract();manifest['observed'].update(python='3.11.15',python_executable='/opt/native/bin/python3.11')
        with self.jobs.transaction() as db:
            profile=self.jobs.nodes.node(self.node,db)['profile'];profile['environment_manifest']=manifest
            profile.pop('python',None);db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.node))
        self.beat(python_executable='/opt/native/bin/python3.11',environment_sha256=fingerprint(manifest))
        job=self.workflow(lock_for(manifest));self.assertTrue(self.jobs.nodes.claim(self.node)['grant'] is None)
        self.assertFalse(self.jobs.attempts(job['id']));self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)
        self.assertIn('重新认证',self.jobs.get(job['id'])['reason'])

    def test_old_heartbeat_cannot_overwrite_concurrent_policy_change(self):
        import environment_contract
        original=environment_contract.immutable_path;changed=False
        def interleave(value):
            nonlocal changed
            if not changed:
                changed=True
                self.jobs.nodes.set_policy(self.node,self.jobs.nodes.node(self.node)['profile']['policy'],1)
            return original(value)
        with patch('environment_contract.immutable_path',side_effect=interleave):
            with self.assertRaisesRegex(JobError,'configuration changed'):self.beat()
        self.assertEqual(self.jobs.nodes.node(self.node)['last_seen'],0)

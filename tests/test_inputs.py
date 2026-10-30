"""Immutable input, cross-user and execution-contract boundaries, without device launches."""
import base64
import hashlib
import json
import json
import uuid
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,atomic_json
from inputs import safe_name
from workflow_spec import validate
from workflow_runner import validate_result
from environment_probe import compare
from worker import Worker


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-input-')
        self.runtime=Path(self.temp.name)
        self.jobs=JobStore(self.runtime,{'username':'input-admin','password':'test-only','container':'zdl'})
        for user in ['alice','bob']:
            workspace=self.runtime/user;workspace.mkdir()
            password=self.jobs.access.create_user(user,workspace,environments=['worker-python'])
        self.alice=self.jobs.access.authenticate('Bearer '+self.jobs.access.issue_token('alice',{'jobs:submit','jobs:read'}))
        self.bob=self.jobs.access.authenticate('Bearer '+self.jobs.access.issue_token('bob',{'jobs:submit','jobs:read'}))
        self.data=b'print("frozen")\n';self.item={'name':'main.py','size':len(self.data),'sha256':hashlib.sha256(self.data).hexdigest()}

    def tearDown(self):self.temp.cleanup()

    def create(self):return self.jobs.inputs.create(self.alice,{'files':[self.item]})[0]

    def upload(self,bundle,offset,data):
        return self.jobs.inputs.upload(bundle['id'],self.alice,{'name':'main.py','offset':offset,'data':base64.b64encode(data).decode()})

    def sealed(self):
        bundle=self.create();self.upload(bundle,0,self.data)
        return self.jobs.inputs.seal(bundle['id'],self.alice)

    def test_readonly_executable_manifest_survives_download_and_recovery(self):
        payloads={'run.sh':b'#!/bin/sh\nexit 0\n','data.txt':b'input bytes'}
        manifest={'files':[{'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                           'executable':name=='run.sh'} for name,data in payloads.items()]}
        engine=SimpleNamespace(inputs=self.runtime/'node-inputs',grant={'job_id':'job-'+'a'*12,
                    'attempt_id':'attempt-'+'b'*32,'lease_token':'private-test','input':manifest})
        worker=Worker.__new__(Worker);worker.client=Mock()
        def chunk(action,body):
            self.assertEqual(action,'input');data=payloads[body['name']][body['offset']:]
            return {'name':body['name'],'offset':body['offset'],'size':len(payloads[body['name']]),'data':base64.b64encode(data).decode()}
        worker.client.call.side_effect=chunk;worker.download_input(engine)
        self.assertEqual((engine.inputs/'run.sh').stat().st_mode&0o777,0o555)
        self.assertEqual((engine.inputs/'data.txt').stat().st_mode&0o777,0o444)
        subprocess.run([str(engine.inputs/'run.sh')],check=True)
        before=worker.client.call.call_count;(engine.inputs/'run.sh').chmod(0o444)
        worker.download_input(engine)
        self.assertEqual(worker.client.call.call_count,before)
        subprocess.run([str(engine.inputs/'run.sh')],check=True)

    def test_unregistered_input_file_is_not_silently_mounted_for_task(self):
        directory=self.runtime/'unexpected-input';directory.mkdir()
        unknown=directory/'not-in-manifest';unknown.write_text('private diagnostic')
        engine=SimpleNamespace(inputs=directory,grant={'input':{'files':[]}})
        worker=Worker.__new__(Worker)
        with self.assertRaisesRegex(ValueError,'Unregistered'):worker.download_input(engine)

    def test_manifest_rejects_traversal_collisions_links_and_over_budget(self):
        for name in ['/etc/shadow','../private','a/../b','a//b','./x','a\\b','a\0b','a/'+'x'*201]:
            with self.subTest(name=name):self.assertFalse(safe_name(name))
        for items in [[self.item,self.item],[{**self.item,'name':'dir'},{**self.item,'name':'dir/file'}],
                      [{**self.item,'size':65*1024**2}],[{**self.item,'sha256':'wrong'}]]:
            with self.assertRaises(JobError):self.jobs.inputs.create(self.alice,{'files':items})

    def test_owner_bound_content_identity_and_cross_user_denial(self):
        a=self.create();again,reused=self.jobs.inputs.create(self.alice,{'files':[self.item]})
        self.assertTrue(reused);self.assertEqual(a['id'],again['id'])
        b,_=self.jobs.inputs.create(self.bob,{'files':[self.item]});self.assertNotEqual(a['id'],b['id'])
        for action in [lambda:self.jobs.inputs.get(a['id'],self.bob),
                       lambda:self.jobs.inputs.upload(a['id'],self.bob,{'name':'main.py','offset':0,'data':'eA=='}),
                       lambda:self.jobs.inputs.seal(a['id'],self.bob)]:
            with self.assertRaises(JobError) as error:action()
            self.assertEqual(error.exception.status,404)

    def test_contiguous_retry_and_crash_tail_reconciliation(self):
        bundle=self.create();self.upload(bundle,0,self.data[:5])
        self.assertTrue(self.upload(bundle,0,self.data[:5])['reused'])
        with self.assertRaises(JobError):self.upload(bundle,1,b'bad')
        with self.assertRaises(JobError):self.upload(bundle,8,b'x')
        path=self.jobs.inputs.directory(bundle['id'])/'main.py'
        with path.open('ab') as file:file.write(b'uncommitted')
        self.upload(bundle,5,self.data[5:])
        self.assertEqual(path.read_bytes(),self.data)
        self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.alice)['sealed'])

    def test_wrong_hash_can_be_reset_before_seal_but_sealed_input_is_immutable(self):
        bundle=self.create();self.upload(bundle,0,b'x'*len(self.data))
        with self.assertRaises(JobError):self.jobs.inputs.seal(bundle['id'],self.alice)
        self.jobs.inputs.reset_file(bundle['id'],self.alice,{'name':'main.py'})
        self.upload(bundle,0,self.data);self.jobs.inputs.seal(bundle['id'],self.alice)
        with self.assertRaises(JobError):self.upload(bundle,0,self.data)
        with self.assertRaises(JobError):self.jobs.inputs.reset_file(bundle['id'],self.alice,{'name':'main.py'})

    def test_empty_file_and_personal_budget_are_accounted(self):
        item={'name':'empty','size':0,'sha256':hashlib.sha256(b'').hexdigest()}
        b,_=self.jobs.inputs.create(self.alice,{'files':[item]})
        self.assertTrue(self.jobs.inputs.seal(b['id'],self.alice)['sealed'])
        self.jobs.policy['input_budget_mib']=0
        with self.assertRaises(JobError):self.create()

    def test_only_own_sealed_input_can_be_bound_to_workflow(self):
        bundle=self.create()
        body={'template':'build_v1','environment':'worker-python','parameters':{'input_id':bundle['id'],
               'argv':['python','main.py'],'lock':{'python':'3.10.12'}}}
        body['idempotency_key']='immutable-workflow-retry'
        with self.assertRaises(JobError):self.jobs.normalize(body,self.alice)
        self.upload(bundle,0,self.data);self.jobs.inputs.seal(bundle['id'],self.alice)
        spec=self.jobs.normalize(body,self.alice)
        self.assertEqual(spec['input']['manifest_sha256'],bundle['manifest_sha256'])
        self.assertEqual(spec['kind'],'build');self.assertEqual(spec['resources']['npu'],0)
        job,_=self.jobs.submit(body,self.alice)
        self.assertEqual(job['source']['path'],str(ROOT/'workflow_runner.py'))
        self.assertEqual(set(job['runtime_files']),{'container_entry.py','artifact_export.py','environment_probe.py','environment_contract.py','workflow_spec.py','device_qualification.py'})
        with patch('workflow_spec.runtime_manifest',return_value={'changed-after-response-loss':'0'*64}):
            replay,reused=self.jobs.submit(body,self.alice)
        self.assertTrue(reused);self.assertEqual(replay['id'],job['id'])
        with self.assertRaises(JobError):self.jobs.normalize(body,self.bob)

    def test_user_cannot_override_mounts_image_identity_or_reserved_environment(self):
        bundle=self.sealed()
        parameters={'input_id':bundle['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'}}
        for changes in [{'mounts':['/']},{'uid':0},{'image':'evil'},{'environment':{'LD_PRELOAD':'evil'}},
                        {'resources':{'cpu':True}},{'lock':{}},{'resources':{'npu':1}}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate({**parameters,**changes},'build')
        for kind in ['accuracy','performance','simulation']:
            self.assertEqual(validate(parameters,kind)['resources']['npu'],1)
        from container_engine import DockerAttempt
        from workflow_spec import runtime_manifest
        configured=validate(parameters,'build');cpu=self.jobs.policy['cpu_ids'][0]
        grant={'job_id':'job-'+'a'*12,'attempt_id':'attempt-'+uuid.uuid4().hex,'owner':'alice',
               'template':'build_v1','parameters':configured,'resources':configured['resources'],
               'timeout_seconds':configured['timeout_seconds'],'input':bundle,
               'allocation':{'cpu_ids':[cpu],'npu_ids':[]},'runtime_files':runtime_manifest(),
               'source_sha256':hashlib.sha256((ROOT/'workflow_runner.py').read_bytes()).hexdigest()}
        engine=DockerAttempt(self.runtime/'offline-node',grant,{'users':{'alice':{'uid':23001,'gid':23001}},
                            'image':'sha256:'+'a'*64,'policy':{'cpu_ids':[cpu],'memory_mib':512}})
        self.assertEqual(engine.validate()['uid'],23001)
        with patch('container_engine.runtime_manifest',return_value={'older-bootstrap':'0'*64}):
            with self.assertRaisesRegex(ValueError,'runtime versions differ'):engine.validate()

    def test_environment_mismatch_and_result_contracts_do_not_infer_success_from_exit_zero(self):
        self.assertEqual(compare({'python':'3.10.12','packages':{'triton':'pinned'}},
                                 {'python':'3.10.12','packages':{'triton':'different'}}),['packages.triton'])
        for kind,result in [('accuracy',{'checks':[]}),('performance',{'samples_us':[1,float('nan'),2]}),
                            ('build',{'artifacts':['results/missing.whl']}),('simulation',{'artifacts':['../outside']}),
                            ('ir',{'artifacts':['ir/missing.mlir']})]:
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                validate_result(kind,{'schema':'ascend-lab-result/v1','kind':kind,'passed':True,**result},self.runtime)

    def record_fixture(self,kind='build',project=None,**overrides):
        bundle=self.sealed();body={'template':kind+'_v1','environment':'worker-python',
            'name':'=dangerous spreadsheet formula','parameters':{'input_id':bundle['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'}}}
        if project:body['project']=project
        if kind in {'accuracy','performance','simulation'}:self.jobs.access.set_quota('alice',{'npu':1});self.alice=self.jobs.access.authenticate('Bearer '+self.jobs.access.issue_token('alice',{'jobs:read','jobs:submit'}))
        created,_=self.jobs.submit(body,self.alice);job=self.jobs.get(created['id']);directory=self.jobs.directory(job['id'])
        if kind in {'accuracy','performance','simulation'}:job['allocation']={'npu_ids':[0]}
        image='sha256:'+'a'*64
        atomic_json(directory/'environment.json',{'image':image,'runtime_files':job.get('runtime_files')})
        (directory/'results/_lab').mkdir(parents=True)
        protected={'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,
                   'probe_exit_code':0,'workload_exit_code':0,'observed':{'python':'3.10.12','image_sha256':image},**overrides}
        if kind in {'accuracy','performance','simulation'} and 'device_access' not in overrides:
            protected['device_access']={'schema':'ascend-lab-device-access/v1','status':'passed','passed':True,
                'requested_device_ids':[0],'opened_device_ids':[0],'physical_device_files':[0],'errors':[],
                'uid':23001,'euid':23001,'cap_effective':'0000000000000000','cap_permitted':'0000000000000000','no_new_privileges':'1'}
        atomic_json(directory/'results/_lab/environment.json',protected)
        (directory/'results/output.whl').write_bytes(b'offline contract fixture, not an actual wheel')
        result={'schema':'ascend-lab-result/v1','kind':kind,'passed':True,'artifacts':['results/output.whl']}
        if kind=='performance':result={'schema':'ascend-lab-result/v1','kind':kind,'passed':True,'samples_us':[10,20,30,40,50],'conditions':{'scope':'offline schema fixture'}}
        atomic_json(directory/'results/workload-result.json',result)
        return job,{'status':'succeeded','exit_code':0}

    def test_protected_environment_and_artifacts_are_required_before_result_success(self):
        job,result=self.record_fixture(locked=False)
        with self.jobs.transaction() as db:record=self.jobs.results.record(job,result,db)
        self.assertFalse(record['passed']);self.assertEqual(result['status'],'failed')
        self.assertEqual(result['failure_stage'],'result_contract')

    def test_device_workflow_cannot_claim_success_without_protected_access_proof(self):
        job,result=self.record_fixture('performance',device_access=None)
        with self.jobs.transaction() as db:record=self.jobs.results.record(job,result,db)
        self.assertFalse(record['passed']);self.assertFalse(record['device_access_confirmed'])
        self.assertEqual(result['failure_stage'],'result_contract')

    def test_device_access_failure_remains_environmental_without_running_user_command(self):
        job,result=self.record_fixture('performance',device_access={'passed':False},locked=False,workload_started=False)
        directory=self.jobs.directory(job['id']);protected=json.loads((directory/'results/_lab/environment.json').read_text())
        protected['failure_stage']='npu_device_access';atomic_json(directory/'results/_lab/environment.json',protected)
        result.update(status='failed',failure_stage='npu_device_access',reason='Task-identity NPU access was rejected')
        with self.jobs.transaction() as db:record=self.jobs.results.record(job,result,db)
        self.assertFalse(record['command_executed']);self.assertFalse(record['passed'])
        self.assertEqual(record['failure_stage'],'npu_device_access');self.assertIn('NPU access',record['error'])

    def test_result_rows_csv_and_project_sharing_obey_same_job_acl(self):
        self.jobs.access.add_member('result_share','alice','manager')
        job,result=self.record_fixture(project='result_share')
        with self.jobs.transaction() as db:record=self.jobs.results.record(job,result,db)
        self.assertTrue(record['passed']);self.assertEqual(record['metrics']['artifacts'],1)
        self.assertEqual(self.jobs.results.rows(self.alice)['total'],1)
        self.assertEqual(self.jobs.results.rows(self.bob)['total'],0)
        self.assertIn("'=dangerous",self.jobs.results.csv(self.alice).decode())
        self.assertNotIn(job['id'],self.jobs.results.csv(self.bob).decode())
        self.jobs.access.add_member('result_share','bob','viewer')
        self.assertEqual(self.jobs.results.rows(self.bob)['total'],1)

    def test_performance_summary_is_derived_from_finite_raw_samples(self):
        job,result=self.record_fixture('performance')
        with self.jobs.transaction() as db:record=self.jobs.results.record(job,result,db)
        self.assertTrue(record['passed']);self.assertEqual(record['metrics']['mean_us'],30)
        self.assertEqual(record['metrics']['median_us'],30)
        self.assertEqual(record['metrics']['p95_us'],50)
        self.assertEqual(self.jobs.results.rows(self.alice,kind='build')['total'],0)
        for args in [{'kind':'unknown'},{'limit':0},{'offset':-1}]:
            with self.assertRaises(JobError):self.jobs.results.rows(self.alice,**args)


if __name__=='__main__':unittest.main()

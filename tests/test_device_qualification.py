"""Protocol-only fixtures: no real device or numerical computation is performed."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,now
from npu_device import fingerprint as npu_sha
from device_qualification import CAPABILITY,PROBE,decision,parameters
from workflow_spec import runtime_manifest,CAPABILITIES


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-device-qual-')
        self.root=Path(self.temp.name);self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        for index,owner in enumerate(['alice','bob']):
            path=self.root/owner;path.mkdir();self.jobs.access.create_user(owner,path,environments=['worker-python'],quota={'npu':1})
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.policy={'device_ids':[0,1],'visible_ids':{'0':0,'1':0},'device_gid':1001,
          'management_devices':['/dev/davinci_manager'],'driver_paths':['/usr/local/Ascend/driver'],
          'query_tool':'/usr/local/sbin/npu-smi','management_env':'/data/pri/cann/set_env.sh'}
        profile={'name':'qualification','physical_id':domain['physical_id'],'image':'sha256:'+'a'*64,
          'users':{'alice':{'uid':23001,'gid':23001},'bob':{'uid':23002,'gid':23002}},
          'templates':[PROBE,'accuracy_v1','cpu_demo'],'npu':self.policy,
          'policy':{'cpu_ids':domain['policy']['cpu_ids'][:1],'memory_mib':768,'shm_mib':0,'max_running':1}}
        self.node,_=self.jobs.nodes.enroll(profile);self.beat()

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def beat(self,**changes):
        body={'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
          'capabilities':['start-ticket-v1','cleanup-before-complete-v1','npu-device-whitelist-v1',CAPABILITY,*CAPABILITIES],
          'access_host_sha256':'b'*64,'access_runtime_files':runtime_manifest(),
          'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
          'npu_observation':{'status':'ok','sampled_at':now(),'policy_sha256':npu_sha(self.policy),
                            'devices':[{'id':device,'name':'offline-fixture','health':'OK','utilization_pct':0,'process_count':0} for device in [0,1]]},**changes}
        self.jobs.nodes.heartbeat(self.node,body)

    def submit_probe(self,owner='alice',device=0):
        return self.jobs.submit({'template':PROBE,'environment':'worker-python',
          'parameters':{'node_id':self.node,'device_id':device}},self.jobs.access.user(owner))[0]

    def workload(self,owner='alice'):
        data=b'pass\n';sha=hashlib.sha256(data).hexdigest();user=self.jobs.access.user(owner)
        bundle,_=self.jobs.inputs.create(user,{'files':[{'name':'main.py','size':len(data),'sha256':sha}]})
        if not bundle['sealed']:
            self.jobs.inputs.upload(bundle['id'],user,{'name':'main.py','offset':0,'data':base64.b64encode(data).decode()})
            self.jobs.inputs.seal(bundle['id'],user)
        return self.jobs.submit({'template':'accuracy_v1','environment':'worker-python','parameters':{
            'input_id':bundle['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'},
            'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':1}}},user)[0]

    def complete_probe(self,passed=True,mutate=None,template=PROBE):
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['template'],template)
        auth={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
        self.jobs.nodes.authorize_start(self.node,auth)
        mapping=self.jobs.nodes.node(self.node)['profile']['users'][grant['owner']];ids=grant['allocation']['npu_ids']
        receipt={'schema':'ascend-lab-environment/v1','locked':passed,'workload_started':False,'probe_only':True,
          'probe_exit_code':0 if passed else 78,'device_access':{'schema':'ascend-lab-device-access/v1','status':'passed' if passed else 'failed',
          'passed':passed,'requested_device_ids':ids,'opened_device_ids':ids if passed else [],'physical_device_files':ids,
          'errors':[] if passed else [{'errno':16}], 'uid':mapping['uid'],'euid':mapping['uid'],
          'cap_effective':'0000000000000000','cap_permitted':'0000000000000000','no_new_privileges':'1'}}
        if not passed:receipt['failure_stage']='npu_device_access'
        meta={'runtime':'managed-docker','task_uid':mapping['uid'],'task_gid':mapping['gid'],
            'privileged':False,'read_only_root':True,'network_mode':'none','image':'sha256:'+'a'*64,'runtime_files':grant['runtime_files']}
        if mutate:mutate(receipt,meta)
        files=[]
        for name,value in [('results/_lab/environment.json',receipt),('environment.json',meta)]:
            data=json.dumps(value).encode()
            self.jobs.nodes.upload(self.node,{**auth,'name':name,'offset':0,'data':base64.b64encode(data).decode()})
            files.append({'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        result={'status':'succeeded' if passed else 'failed','exit_code':0 if passed else 78,'termination_confirmed':True,
          'execution_cleanup_confirmed':True,'npu_release_confirmed':True,**({'failure_stage':'npu_device_access'} if not passed else {})}
        self.jobs.nodes.complete(self.node,{**auth,'result':result,'artifacts':files})
        return grant

    def status(self,owner='alice',device=0):return decision(self.jobs.nodes.node(self.node),owner,device)

    def test_unverified_work_does_not_acquire_resources_but_explicit_probe_can(self):
        job=self.workload();self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.assertEqual(self.jobs.get(job['id'])['allocation'],{})
        self.assertEqual(self.jobs.summary()['reserved']['npu_ids'],[])
        self.submit_probe();self.assertEqual(self.jobs.nodes.claim(self.node)['grant']['template'],PROBE)

    def test_success_is_per_owner_and_card_and_allows_only_that_card(self):
        self.submit_probe();self.complete_probe()
        self.assertEqual(self.status()['status'],'verified');self.assertEqual(self.status('bob')['status'],'unverified')
        self.assertEqual(self.status(device=1)['status'],'unverified')
        job=self.workload();grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['allocation']['npu_ids'],[0])

    def test_failure_blocks_repeat_workflow_and_fixed_probe_can_requalify(self):
        self.submit_probe();self.complete_probe(passed=False);self.assertEqual(self.status()['status'],'blocked')
        self.workload();self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.submit_probe();self.complete_probe();self.assertEqual(self.status()['status'],'verified')
        self.assertEqual(self.jobs.nodes.claim(self.node)['grant']['template'],'accuracy_v1')

    def test_host_change_and_expiry_never_implicitly_restore_permission(self):
        self.submit_probe();self.complete_probe()
        self.beat(access_host_sha256='c'*64);self.assertEqual(self.status()['status'],'stale')
        self.beat();node=self.jobs.nodes.node(self.node)
        record=node['device_qualifications'][0];record['expires_at']=time.time()-1
        with self.jobs.connect() as db:db.execute('UPDATE device_qualifications SET record=? WHERE node_id=?',(json.dumps(record),self.node))
        self.assertEqual(self.status()['status'],'expired')
        self.workload();self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])

    def test_image_identity_revision_and_runtime_changes_invalidate_qualification(self):
        self.submit_probe();self.complete_probe();node=self.jobs.nodes.node(self.node)
        for key,value in [('image','sha256:'+'d'*64),('policy_revision',2),('environment_setup','/opt/changed.sh')]:
            changed=copy.deepcopy(node);changed['profile'][key]=value
            self.assertEqual(decision(changed,'alice',0)['status'],'stale')
        changed=copy.deepcopy(node);changed['profile']['users']['alice']['uid']=24001
        self.assertEqual(decision(changed,'alice',0)['status'],'stale')
        changed=copy.deepcopy(node);changed['report']['access_runtime_files']={}
        self.assertEqual(decision(changed,'alice',0)['status'],'needs_upgrade')

    def test_protected_workflow_access_failure_revokes_existing_permission(self):
        self.submit_probe();self.complete_probe();self.workload()
        self.complete_probe(passed=False,template='accuracy_v1')
        self.assertEqual(self.status()['status'],'blocked')
        self.workload();self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])

    def test_missing_host_identity_and_legacy_runtime_block_new_device_work(self):
        self.beat(access_host_sha256=None);self.assertEqual(self.status()['status'],'unavailable')
        self.beat(capabilities=['start-ticket-v1','cleanup-before-complete-v1','npu-device-whitelist-v1',*CAPABILITIES])
        self.assertEqual(self.status()['status'],'needs_upgrade')

    def test_restore_expires_positive_access_without_erasing_a_failure(self):
        from device_qualification import invalidate_restored
        self.submit_probe();self.complete_probe()
        self.submit_probe('bob');self.complete_probe(passed=False)
        invalidate_restored(self.jobs.db_path)
        self.assertEqual(self.status()['status'],'expired');self.assertEqual(self.status('bob')['status'],'blocked')

    def test_mutated_or_untrusted_receipt_does_not_certify(self):
        mutations=[lambda receipt,meta:receipt.update(workload_started=True),
                   lambda receipt,meta:meta.update(task_uid=0),lambda receipt,meta:meta.update(privileged=True),
                   lambda receipt,meta:receipt['device_access'].update(opened_device_ids=[1])]
        for mutate in mutations:
            self.submit_probe();self.complete_probe(mutate=mutate)
            self.assertEqual(self.status()['status'],'unverified')

    def test_restart_keeps_block_and_public_summary_does_not_export_private_evidence(self):
        self.submit_probe('bob');grant=self.complete_probe(passed=False)
        from nodes import NodeRegistry
        self.jobs.nodes=NodeRegistry(self.jobs)
        self.assertEqual(self.status('bob')['status'],'blocked')
        public=json.dumps(self.jobs.nodes.public_nodes());self.assertNotIn(grant['job_id'],public)
        self.assertNotIn('bob',public);self.assertNotIn('23002',public)
        self.assertEqual(self.jobs.nodes.public_nodes()[0]['device_access']['counts']['blocked'],1)

    def test_probe_has_no_user_command_or_mount_parameters(self):
        for value in [{'node_id':self.node,'device_id':True},{'node_id':self.node,'device_id':0,'argv':['sh']},
                      {'node_id':self.node,'device_id':0,'memory_mib':True}]:
            with self.assertRaises(ValueError):parameters(value)


if __name__=='__main__':unittest.main()

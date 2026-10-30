"""Protocol-only staged owner access proofs. Synthetic Node observations, no NPU."""
import base64
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_activation as activation_fixture
import test_device_qualification as probe_fixture
from owner_device_probe import authorize,request,job_request,load,TABLE
from device_qualification import CAPABILITY,decision
from jobs import JobError,now
from workflow_spec import runtime_manifest
from maintenance import require_features
from deployment import DeployError


class StagedDeviceProbeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=activation_fixture.OwnerActivationTests();self.fixture.stage_npu=1;self.fixture.stage_archive_mib=32;self.fixture.setUp()
        self.jobs=self.fixture.jobs;self.node=self.fixture.fixture.node;self.root=self.fixture.root
        policy={'device_ids':[0,1],'visible_ids':{'0':0,'1':0},'device_gid':1001,
            'management_devices':['/dev/davinci_manager'],'driver_paths':['/usr/local/Ascend/driver'],
            'query_tool':'/usr/local/sbin/npu-smi','management_env':'/data/pri/cann/set_env.sh'}
        self.policy=policy
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.node,db);p=node['profile'];p.update(npu=policy,templates=[*p['templates'],'npu_access_probe'],spool_storage={'path':'/protocol-fixture-spool','filesystem':'ext4','device':1,'root_inode':2,'bytes':128*1024**2,'inodes':262144,'storage_id':'f'*32})
            report=node['report'];report['capabilities']+=['npu-device-whitelist-v1',CAPABILITY]
            report.update(access_host_sha256='b'*64,access_runtime_files=runtime_manifest(),
                npu={'status':'ok','sampled_at':now(),'devices':[{'id':i,'health':'OK','name':'protocol-fixture','utilization_pct':0,'process_count':0} for i in [0,1]]})
            handoff=self.jobs.nodes.handoff.get(self.fixture.fixture.plan['id'],db);handoff['after_profile']=p;self.jobs.nodes.handoff.save(db,handoff)
            db.execute('UPDATE worker_nodes SET profile=?,report=? WHERE id=?',(json.dumps(p),json.dumps(report),self.node))
        self.spec={'operation_id':'c'*32,'owner':'bob','stage_id':self.fixture.spec['stage_id'],'node_id':self.node,
            'plan_id':self.fixture.spec['required_nodes'][0]['plan_id'],'device_id':0,'memory_mib':256}
        # This is the original native/group fixture; no device/node guard is mocked.
        self.groups=patch('owner_activation.Activation.controller_groups',return_value={23832});self.groups.start()

    def tearDown(self):self.groups.stop();self.fixture.tearDown()

    def submit(self,**changes):return authorize(self.jobs,{**self.spec,**changes})

    def complete(self,passed=True,after_start=None):
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertIsNotNone(grant)
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        self.assertTrue(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted'])
        if after_start:after_start()
        mapping=self.jobs.nodes.node(self.node)['profile']['users']['bob'];ids=grant['allocation']['npu_ids']
        receipt={'schema':'ascend-lab-environment/v1','locked':passed,'workload_started':False,'probe_only':True,
            'probe_exit_code':0 if passed else 78,'device_access':{'schema':'ascend-lab-device-access/v1','status':'passed' if passed else 'failed',
            'passed':passed,'requested_device_ids':ids,'opened_device_ids':ids if passed else [],'physical_device_files':ids,
            'errors':[] if passed else [{'errno':16}],'uid':mapping['uid'],'euid':mapping['uid'],
            'cap_effective':'0000000000000000','cap_permitted':'0000000000000000','no_new_privileges':'1'}}
        if not passed:receipt['failure_stage']='npu_device_access'
        meta={'runtime':'managed-docker','task_uid':mapping['uid'],'task_gid':mapping['gid'],'privileged':False,'read_only_root':True,
            'network_mode':'none','image':self.jobs.nodes.node(self.node)['profile']['image'],'runtime_files':grant['runtime_files']}
        files=[]
        for name,value in [('results/_lab/environment.json',receipt),('environment.json',meta)]:
            raw=json.dumps(value).encode();self.jobs.nodes.upload(self.node,{**auth,'name':name,'offset':0,'data':base64.b64encode(raw).decode()})
            files.append({'name':name,'size':len(raw),'sha256':__import__('hashlib').sha256(raw).hexdigest()})
        result={'status':'succeeded' if passed else 'failed','exit_code':0 if passed else 78,'termination_confirmed':True,
            'execution_cleanup_confirmed':True,'npu_release_confirmed':True,'workspace_release_confirmed':True}
        return self.jobs.nodes.complete(self.node,{**auth,'result':result,'artifacts':files})

    def changed_then_restored(self):
        with self.jobs.access.connect() as db:
            db.execute("UPDATE users SET disabled=0 WHERE id='bob'");db.execute("UPDATE users SET disabled=1 WHERE id='bob'")

    def test_disabled_owner_can_only_run_one_admin_fixed_probe_without_credentials(self):
        a=self.submit();b=self.submit();self.assertEqual(a['job']['id'],b['job']['id']);self.assertTrue(b['historical_receipt_only'])
        self.assertIsNone(self.jobs.access.user('bob'));job=self.jobs.get(a['job']['id']);self.assertEqual(job['artifact_limits'],{'file_mib':1,'total_mib':2})
        self.assertFalse(any(k.startswith('_') for k in a['job']));self.assertEqual(self.complete()['state'],'succeeded')
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'bob',0)['status'],'verified')
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'bob',1)['status'],'unverified')
        self.assertIsNone(self.jobs.access.user('bob'))
        with self.jobs.access.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM tokens WHERE user='bob'").fetchone()[0],0)
        self.assertTrue(self.fixture.run_activation()['id']);self.assertIsNotNone(self.jobs.access.user('bob'))

    def test_success_does_not_survive_stage_authority_change_before_activation(self):
        self.submit();self.complete();self.changed_then_restored()
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'bob',0)['status'],'verified')
        with self.assertRaisesRegex(JobError,'NPU activation'):self.fixture.run_activation()
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_recovery_before_and_after_job_binding_keeps_one_original_job(self):
        class Crash(Exception):pass
        from owner_device_probe import _checkpoint
        for boundary in ['authorized','submitted']:
            def fault(name):
                if name==boundary:raise Crash()
            with patch('owner_device_probe._checkpoint',side_effect=fault),self.assertRaises(Crash):self.submit()
        result=self.submit();self.assertTrue(result['historical_receipt_only'])
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_explicit_failure_stays_blocked_and_never_activates(self):
        self.submit();self.assertEqual(self.complete(False)['state'],'failed')
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'bob',0)['status'],'blocked')
        self.assertIsNone(self.jobs.access.user('bob'))
        with self.assertRaisesRegex(JobError,'NPU activation'):self.fixture.run_activation()

    def test_revocation_and_restore_blocks_claim_without_attempt(self):
        a=self.submit();self.changed_then_restored();self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])
        self.assertEqual(self.jobs.attempts(a['job']['id']),[])

    def test_change_after_reservation_prevents_start_but_preserves_lease(self):
        a=self.submit();g=self.jobs.nodes.claim(self.node)['grant'];self.changed_then_restored()
        auth={k:g[k] for k in ['job_id','attempt_id','lease_token']};self.assertFalse(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted'])
        self.assertEqual(self.jobs.get(a['job']['id'])['allocation']['npu_ids'],[0])

    def test_change_while_running_prevents_positive_qualification(self):
        a=self.submit();self.assertEqual(self.complete(after_start=self.changed_then_restored)['state'],'failed')
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'bob',0)['status'],'unverified')
        self.assertEqual(self.jobs.get(a['job']['id'])['result']['failure_stage'],'qualification_evidence')

    def test_same_operation_cannot_change_requested_device(self):
        self.submit()
        with self.assertRaisesRegex(JobError,'another request'):self.submit(device_id=1)

    def test_new_explicit_operation_after_revocation_does_not_reuse_old_job(self):
        a=self.submit();self.changed_then_restored();b=self.submit(operation_id='d'*32)
        self.assertNotEqual(a['job']['id'],b['job']['id'])
        self.assertEqual(self.jobs.nodes.claim(self.node)['grant']['job_id'],b['job']['id'])

    def test_private_marker_cannot_be_supplied_by_public_job_request(self):
        self.submit();request=job_request(self.spec);request['_staged_device_probe']='0'*64
        with self.assertRaises(JobError):self.jobs.submit(request,self.jobs.access.user('alice'))
        request=job_request(self.spec);request['parameters']['argv']=['sh']
        with self.assertRaises(JobError):self.jobs.submit(request,self.jobs.access.user('alice'))

    def test_wrong_stage_or_plan_fails_before_job_or_ledger_record(self):
        for change in [{'stage_id':'e'*64},{'plan_id':'f'*64},{'device_id':2}]:
            with self.subTest(change=change),self.assertRaises(JobError):self.submit(**change)
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],0)

    def test_environment_change_invalidates_queued_authorization(self):
        a=self.submit()
        with self.jobs.transaction() as db:
            n=self.jobs.nodes.node(self.node,db);n['report']['access_host_sha256']='d'*64
            db.execute('UPDATE worker_nodes SET report=? WHERE id=?',(json.dumps(n['report']),self.node))
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertEqual(self.jobs.attempts(a['job']['id']),[])

    def test_history_prevents_incompatible_downgrade(self):
        self.submit()
        from deployment import CORE
        with self.assertRaisesRegex(DeployError,'Staged device'):require_features(self.root,'controller',{'files':{k:{} for k in CORE if k!='owner_device_probe.py'}})

    def test_ordinary_installed_probe_cannot_fall_back_to_service_administrator(self):
        import subprocess
        from lab_client import atomic_json
        atomic_json(self.root/'config.json',self.jobs.access.config)
        result=subprocess.run([sys.executable,str(ROOT/'workerctl'),'--controller-state',str(self.root),
            'node-device-probe',self.node,'--user','bob','--device','0'],capture_output=True,text=True,timeout=20)
        self.assertNotEqual(result.returncode,0);self.assertIn('active mapped identity',result.stderr)
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_start_ticket_holds_identity_reservation_through_job_write(self):
        self.submit();g=self.jobs.nodes.claim(self.node)['grant'];original=self.jobs.write;observed=[]
        def write(db,job):
            with sqlite3.connect(self.jobs.access.path,timeout=0) as contender:
                with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                    contender.execute("UPDATE users SET disabled=1 WHERE id='bob'")
            observed.append(True);return original(db,job)
        with patch.object(self.jobs,'write',side_effect=write):
            self.assertTrue(self.jobs.nodes.authorize_start(self.node,{k:g[k] for k in ['job_id','attempt_id','lease_token']})['start_permitted'])
        self.assertEqual(observed,[True]);self.changed_then_restored()

    def test_qualification_commit_holds_identity_reservation(self):
        self.submit();original=self.jobs.nodes.qualifications.observe;observed=[]
        def observe(*args):
            with sqlite3.connect(self.jobs.access.path,timeout=0) as contender:
                with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                    contender.execute("UPDATE users SET disabled=1 WHERE id='bob'")
            observed.append(True);return original(*args)
        with patch.object(self.jobs.nodes.qualifications,'observe',side_effect=observe):self.assertEqual(self.complete()['state'],'succeeded')
        self.assertEqual(observed,[True]);self.changed_then_restored()

    def test_strict_request_rejects_commands_unbounded_ids_and_bool_memory(self):
        for change in [{'argv':['sh']},{'memory_mib':True},{'operation_id':'x'},{'owner':'../x'}]:
            with self.subTest(change=change),self.assertRaises(JobError):request({**self.spec,**change})


if __name__=='__main__':unittest.main()

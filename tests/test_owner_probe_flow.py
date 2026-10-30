"""Root flow bookkeeping + real controller protocol with synthetic device evidence.

Root file IO is adapted for this non-root unit fixture. No real NPU is opened.
"""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch,Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_device_probe as builder
import test_owner_provision as request_fixture
import owner_provision as flow
from owner_device_probe import advance
from deployment import DeployError
from lab_client import atomic_json
from jobs import JobError


class ProbeFlowTests(unittest.TestCase):
    def setUp(self):
        self.data=builder.StagedDeviceProbeTests();self.data.stage_npu=getattr(self,'cards',1);self.data.setUp()
        self.jobs=self.data.jobs;self.root=self.data.root;self.node=self.data.node
        self.directory=self.root/'probe-flow';self.directory.mkdir(mode=0o700);self.prefix=self.root/'root-prefix';self.prefix.mkdir(mode=0o700)
        spec=request_fixture.spec();spec['quota']['npu']=getattr(self,'cards',1);spec['nodes'][0]['node_id']=self.node
        spec['device_probes']=[{'node_id':self.node,'device_ids':list(reversed(range(spec['quota']['npu']))),'memory_mib':256}]
        self.spec=flow.request(spec)
        self.record={'id':'e'*64,'request':self.spec,'stage_id':self.data.spec['stage_id'],
            'proposals':{self.node:self.data.spec['plan_id']},'phase':'nodes'}
        self.read=patch('owner_provision._read',side_effect=lambda path:json.loads(Path(path).read_text()));self.read.start()
        self.write=patch('owner_provision._write',side_effect=lambda path,value,mode=0o600:atomic_json(Path(path),value));self.write.start()
        self.service=patch('owner_provision._service',side_effect=self.participate);self.service_mock=self.service.start()

    def tearDown(self):self.service.stop();self.write.stop();self.read.stop();self.data.tearDown()

    def participate(self,prefix,meta,identifier,action,spec):
        self.assertEqual(identifier,self.record['id'])
        if action=='probe_adoption_status':
            from owner_probe_adoption import adoption_status
            return adoption_status(self.jobs,spec)
        if action=='probe_adopt':
            from owner_probe_adoption import adopt
            return adopt(self.jobs,spec)
        self.assertEqual(action,'probe');return advance(self.jobs,spec)

    def drive(self,adoptions=None):return flow._drive_probes(self.prefix,{},self.record,self.directory,adoptions)

    def jobs_count(self):
        with self.jobs.connect() as db:return db.execute('SELECT count(*) FROM jobs').fetchone()[0]

    def test_one_selected_device_is_submitted_once_and_completed_before_snapshot(self):
        a=self.drive();b=self.drive();self.assertFalse(a['ready']);self.assertEqual(a,b);self.assertEqual(self.jobs_count(),1)
        job=a['probes'][0]['job_id'];self.assertEqual(self.jobs.attempts(job),[]);self.assertIsNone(self.jobs.access.user('bob'))
        self.data.complete();ready=self.drive();self.assertTrue(ready['ready']);self.assertEqual(ready['probes'][0]['job_id'],job)
        self.assertEqual(flow._probe_snapshot(self.record,self.directory)['probes'],ready['probes'])
        self.assertEqual(self.drive(),ready);self.assertEqual(self.jobs_count(),1)
        self.assertFalse(any(k in json.dumps(ready) for k in ['lease_token','password','node_key']))
        self.assertTrue(self.data.fixture.run_activation()['id'])

    def test_two_cards_are_explicit_and_serial_with_one_original_job_each(self):
        self.tearDown();self.cards=2;self.setUp()
        first=self.drive();self.assertEqual(self.jobs_count(),1);self.assertEqual(first['probes'][0]['device_id'],0)
        self.data.complete();second=self.drive();self.assertFalse(second['ready']);self.assertEqual(self.jobs_count(),2)
        self.assertEqual([v['device_id'] for v in second['probes']],[0,1]);self.assertEqual(second['probes'][1]['state'],'queued')
        self.data.complete();done=self.drive();self.assertTrue(done['ready']);self.assertEqual(self.jobs_count(),2)
        self.assertNotEqual(done['probes'][0]['job_id'],done['probes'][1]['job_id']);self.assertTrue(self.data.fixture.run_activation()['id'])

    def test_one_slot_owner_can_qualify_two_candidates_and_use_the_free_card(self):
        self.spec['device_probes'][0]['device_ids']=[1,0];flow.request(self.spec)
        with self.jobs.transaction() as db:
            n=self.jobs.nodes.node(self.node,db);n['profile']['templates'].append('accuracy_v1')
            handoff=self.jobs.nodes.handoff.get(self.data.spec['plan_id'],db);handoff['after_profile']=n['profile'];self.jobs.nodes.handoff.save(db,handoff)
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(n['profile']),self.node))
        self.drive();self.data.complete();self.drive();self.data.complete();self.assertTrue(self.drive()['ready'])
        self.data.fixture.run_activation();user=self.jobs.access.user('bob');self.assertEqual(user.quota['npu'],1)
        import hashlib,base64
        from environment_contract import lock_for
        data=b'pass\n';bundle,_=self.jobs.inputs.create(user,{'files':[{'name':'run.py','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]})
        self.jobs.inputs.upload(bundle['id'],user,{'name':'run.py','offset':0,'data':base64.b64encode(data).decode()});self.jobs.inputs.seal(bundle['id'],user)
        node=self.jobs.nodes.node(self.node);lock=lock_for(node['profile']['environment_manifest'])
        request={'environment':'worker-python','template':'accuracy_v1','parameters':{'input_id':bundle['id'],'argv':['python','run.py'],
            'lock':lock,'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':1},'artifacts':{'file_mib':1,'total_mib':2}}}
        job,_=self.jobs.submit(request,user)
        with self.jobs.transaction() as db:
            n=self.jobs.nodes.node(self.node,db);n['report']['npu']['devices'][0]['process_count']=1
            db.execute('UPDATE worker_nodes SET report=? WHERE id=?',(json.dumps(n['report']),self.node))
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertIsNotNone(grant,self.jobs.get(job['id'])['reason']);self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['allocation']['npu_ids'],[1])

    def test_no_job_before_readiness_can_resume_only_the_same_explicit_request(self):
        import time
        with self.jobs.transaction() as db:db.execute('UPDATE worker_nodes SET last_seen=0 WHERE id=?',(self.node,))
        first=self.drive();self.assertEqual(first['probes'][0]['status'],'attention');self.assertIsNone(first['probes'][0]['job_id']);self.assertEqual(self.jobs_count(),0)
        with self.jobs.transaction() as db:db.execute('UPDATE worker_nodes SET last_seen=? WHERE id=?',(time.time(),self.node))
        second=self.drive();self.assertEqual(second['probes'][0]['status'],'waiting');self.assertEqual(self.jobs_count(),1)
        self.assertEqual(first['probes'][0]['authorization_id'],second['probes'][0]['authorization_id'])

    def test_delivered_probe_receipt_requires_complete_original_success_entries(self):
        self.drive();self.data.complete();entries=self.drive()['probes']
        result={**flow._result(self.record,self.directory,state='delivered'),'activation_id':'a'*32,'finish_id':'b'*64,'device_probes':entries}
        flow._receipt({'id':self.record['id'],'request':self.spec,'result':result},self.record['id'],self.spec)
        for bad in [[],[{**entries[0],'status':'waiting'}],[{**entries[0],'job_id':'not-a-job'}]]:
            invalid={**result,'device_probes':bad}
            with self.assertRaises(DeployError):flow._receipt({'id':self.record['id'],'request':self.spec,'result':invalid},self.record['id'],self.spec)

    def manual_probe(self,device=0,operation='f'*32):
        from owner_device_probe import authorize
        spec={**next(flow._base_probe_specs(self.record)),'operation_id':operation,'device_id':device}
        result=authorize(self.jobs,spec);self.data.complete();return result['job']['id']

    def failed_original_and_replacement(self):
        original=self.drive()['probes'][0]['job_id'];self.data.complete(False)
        return original,self.manual_probe()

    def test_explicit_success_adoption_preserves_original_failure_and_finishes(self):
        old,new=self.failed_original_and_replacement();self.assertEqual(self.drive()['probes'][0]['status'],'attention')
        done=self.drive({(self.node,0):new});self.assertTrue(done['ready']);self.assertEqual(done['probes'][0]['job_id'],new)
        self.assertEqual(self.jobs.get(old)['state'],'failed');self.assertEqual(self.jobs_count(),2)
        self.assertEqual(self.drive({(self.node,0):new}),done);self.assertEqual(self.jobs_count(),2)
        from deployment import CORE
        from maintenance import require_features
        with self.assertRaisesRegex(DeployError,'Probe adoption'):require_features(self.root,'controller',{'files':{k:{} for k in CORE if k!='owner_probe_adoption.py'}})
        self.assertTrue(self.data.fixture.run_activation()['id'])

    def test_adoption_lost_response_recovers_from_central_committed_chain(self):
        old,new=self.failed_original_and_replacement()
        def lost(*args):
            result=self.participate(*args)
            if args[3]=='probe_adopt':raise DeployError('adoption response unknown')
            return result
        with patch('owner_provision._service',side_effect=lost),self.assertRaises(DeployError):self.drive({(self.node,0):new})
        done=self.drive();self.assertTrue(done['ready']);self.assertEqual(done['probes'][0]['job_id'],new);self.assertEqual(self.jobs_count(),2)

    def test_adoption_cached_boundary_replays_without_reapplying(self):
        old,new=self.failed_original_and_replacement()
        class Crash(BaseException):pass
        def fail(stage):
            if stage=='probe_adoption_saved':raise Crash()
        with patch('owner_provision._checkpoint',side_effect=fail),self.assertRaises(Crash):self.drive({(self.node,0):new})
        self.assertEqual(self.drive()['probes'][0]['job_id'],new);self.assertEqual(self.jobs_count(),2)
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_probe_adoptions').fetchone()[0],1)

    def test_adoption_rejects_a_different_card_and_never_changes_binding(self):
        original=self.drive()['probes'][0]['job_id'];self.data.complete(False);new=self.manual_probe(device=1)
        with self.assertRaisesRegex(JobError,'exact owner'):self.drive({(self.node,0):new})
        self.assertEqual(self.drive()['probes'][0]['job_id'],original)
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM owner_probe_adoptions').fetchone()[0],0)

    def test_queued_revoked_probe_must_be_cancelled_before_explicit_adoption(self):
        old=self.drive()['probes'][0]['job_id'];self.data.changed_then_restored();new=self.manual_probe()
        with self.assertRaisesRegex(JobError,'Cancel or reconcile'):self.drive({(self.node,0):new})
        self.assertEqual(self.jobs.get(old)['state'],'queued');self.jobs.cancel(old)
        self.assertTrue(self.drive({(self.node,0):new})['ready']);self.assertEqual(self.jobs_count(),2)

    def test_unknown_old_execution_cannot_be_fenced_by_success_of_another_job(self):
        old,new=self.failed_original_and_replacement()
        with self.jobs.transaction() as db:
            j=self.jobs.get(old,db);j['state']='needs_attention';self.jobs.write(db,j)
        with self.assertRaisesRegex(JobError,'Cancel or reconcile'):self.drive({(self.node,0):new})
        self.assertEqual(self.jobs.get(old)['allocation']['npu_ids'],[0]);self.assertEqual(self.jobs_count(),2)

    def test_orphaned_pre_submit_intent_is_fenced_against_late_submission(self):
        class Crash(BaseException):pass
        def fail(stage):
            if stage=='probe_intent':raise Crash()
        with patch('owner_provision._checkpoint',side_effect=fail),self.assertRaises(Crash):self.drive()
        original=next(flow._base_probe_specs(self.record));new=self.manual_probe()
        self.assertTrue(self.drive({(self.node,0):new})['ready'])
        self.assertEqual(advance(self.jobs,original)['status'],'attention');self.assertEqual(self.jobs_count(),1)
        from owner_device_probe import job_request,principal
        profile=self.jobs.access.registration('bob')['profile']
        with self.assertRaisesRegex(JobError,'superseded'):self.jobs.submit(job_request(original),principal(profile,'bob'),_staged_probe=flow.sha({'request':original}))
        self.assertEqual(self.jobs_count(),1)

    def test_next_explicit_adoption_keeps_prior_completion_snapshot(self):
        old,new=self.failed_original_and_replacement();self.drive({(self.node,0):new});before=flow._snapshot_path(self.record,self.directory);raw=before.read_bytes()
        with self.jobs.transaction() as db:
            row=db.execute('SELECT record FROM device_qualifications WHERE node_id=?',(self.node,)).fetchone();q=json.loads(row[0]);q['expires_at']=0
            db.execute('UPDATE device_qualifications SET record=? WHERE node_id=?',(json.dumps(q),self.node))
        self.assertEqual(self.drive()['probes'][0]['status'],'attention')
        next_job=self.manual_probe(operation='a'*32);done=self.drive({(self.node,0):next_job});self.assertTrue(done['ready'])
        self.assertNotEqual(flow._snapshot_path(self.record,self.directory),before);self.assertEqual(before.read_bytes(),raw)
        self.assertEqual(self.jobs_count(),3)

    def test_failure_never_submits_a_replacement(self):
        first=self.drive();self.data.complete(False)
        for _ in range(3):
            value=self.drive();self.assertFalse(value['ready']);self.assertEqual(value['probes'][0]['status'],'attention')
            self.assertEqual(value['probes'][0]['job_id'],first['probes'][0]['job_id'])
        self.assertEqual(self.jobs_count(),1);self.assertIsNone(self.jobs.access.user('bob'))
        self.assertFalse((self.directory/'probes.json').exists())

    def test_cancelled_probe_remains_the_same_terminal_job(self):
        a=self.drive();self.jobs.cancel(a['probes'][0]['job_id'])
        b=self.drive();self.assertEqual(b['probes'][0]['state'],'cancelled');self.assertEqual(b['probes'][0]['status'],'attention')
        self.assertEqual(self.jobs_count(),1)

    def test_unknown_execution_keeps_original_allocation_and_stops(self):
        a=self.drive();g=self.jobs.nodes.claim(self.node)['grant']
        with self.jobs.transaction() as db:
            j=self.jobs.get(g['job_id'],db);j['state']='needs_attention';self.jobs.write(db,j)
        b=self.drive();self.assertEqual(b['probes'][0]['status'],'attention')
        self.assertEqual(self.jobs.get(g['job_id'])['allocation']['npu_ids'],[0]);self.assertEqual(self.jobs_count(),1)

    def test_authority_change_after_submission_does_not_issue_a_new_operation(self):
        a=self.drive();self.data.changed_then_restored();b=self.drive()
        self.assertEqual(b['probes'][0]['job_id'],a['probes'][0]['job_id']);self.assertEqual(b['probes'][0]['status'],'attention')
        self.assertEqual(self.jobs_count(),1);self.assertEqual(self.jobs.attempts(a['probes'][0]['job_id']),[])

    def test_expiry_requires_explicit_reconciliation_not_automatic_renewal(self):
        self.drive();self.data.complete();done=self.drive();snapshot=(self.directory/'probes.json').read_bytes()
        with self.jobs.transaction() as db:
            row=db.execute('SELECT record FROM device_qualifications WHERE node_id=?',(self.node,)).fetchone();value=json.loads(row[0]);value['expires_at']=0
            db.execute('UPDATE device_qualifications SET record=? WHERE node_id=?',(json.dumps(value),self.node))
        later=self.drive();self.assertFalse(later['ready']);self.assertEqual(later['probes'][0]['status'],'attention')
        self.assertEqual(later['probes'][0]['job_id'],done['probes'][0]['job_id']);self.assertEqual(self.jobs_count(),1)
        self.assertEqual((self.directory/'probes.json').read_bytes(),snapshot)

    def test_lost_service_response_recovers_the_job_before_root_binding(self):
        def lost(*args):
            result=self.participate(*args)
            if args[3]=='probe':raise DeployError('response unknown')
            return result
        with patch('owner_provision._service',side_effect=lost),self.assertRaises(DeployError):self.drive()
        self.assertEqual(self.jobs_count(),1);a=self.drive();self.assertEqual(self.jobs_count(),1)
        self.assertEqual(a['probes'][0]['status'],'waiting')

    def test_successive_bookkeeping_faults_recover_original_request_and_job(self):
        class Crash(BaseException):pass
        for boundary in ['probe_intent','probe_response','probe_bound']:
            def fail(stage):
                if stage==boundary:raise Crash()
            with patch('owner_provision._checkpoint',side_effect=fail),self.assertRaises(Crash):self.drive()
        self.assertEqual(self.jobs_count(),1);original=self.drive()['probes'][0]['job_id'];self.data.complete()
        for boundary in ['probe_verified','probes_ready']:
            def fail(stage):
                if stage==boundary:raise Crash()
            with patch('owner_provision._checkpoint',side_effect=fail),self.assertRaises(Crash):self.drive()
        done=self.drive();self.assertTrue(done['ready']);self.assertEqual(done['probes'][0]['job_id'],original);self.assertEqual(self.jobs_count(),1)

    def test_changed_root_request_is_refused_before_service_operation(self):
        self.drive();intent,_=flow._probe_paths(self.directory,next(flow._probe_specs(self.record)));value=json.loads(intent.read_text());value['device_id']=1;atomic_json(intent,value)
        self.service_mock.reset_mock()
        with self.assertRaisesRegex(DeployError,'request changed'):self.drive()
        self.assertTrue(all(call.args[3]=='probe_adoption_status' for call in self.service_mock.call_args_list));self.assertEqual(self.jobs_count(),1)

    def test_job_id_conflict_never_overwrites_root_binding(self):
        self.drive();_,binding=flow._probe_paths(self.directory,next(flow._probe_specs(self.record)));before=binding.read_bytes()
        def changed(*args):
            value=self.participate(*args)
            return {**value,'job_id':'job-'+'f'*12} if args[3]=='probe' else value
        with patch('owner_provision._service',side_effect=changed),self.assertRaisesRegex(DeployError,'job binding differs'):self.drive()
        self.assertEqual(binding.read_bytes(),before);self.assertEqual(self.jobs_count(),1)

    def test_snapshot_tampering_or_missing_binding_is_not_completion(self):
        self.drive();self.data.complete();self.drive();_,binding=flow._probe_paths(self.directory,next(flow._probe_specs(self.record)))
        before=binding.read_bytes();binding.unlink()
        with self.assertRaises(FileNotFoundError):flow._probe_snapshot(self.record,self.directory)
        binding.write_bytes(before);value=json.loads((self.directory/'probes.json').read_text());value['probes'][0]['job_id']='job-'+'a'*12;value['id']=flow.sha({k:v for k,v in value.items() if k!='id'});atomic_json(self.directory/'probes.json',value)
        with self.assertRaises(DeployError):flow._probe_snapshot(self.record,self.directory)

    def test_invalid_service_progress_cannot_create_a_binding(self):
        response={'authorization_id':'f'*64,'node_id':self.node,'device_id':0,'job_id':'job-'+'a'*12,'state':'running','status':'verified','reason':None}
        with patch('owner_provision._service',return_value=response),self.assertRaises(DeployError):self.drive()
        _,binding=flow._probe_paths(self.directory,next(flow._probe_specs(self.record)));self.assertFalse(binding.exists())

    def test_completed_and_aborted_receipt_shapes_keep_backward_compatibility(self):
        cpu=request_fixture.spec();record={**self.record,'request':cpu,'phase':'held','stage_id':None}
        result=flow._result(record,self.directory,state='aborted');self.assertNotIn('device_probes',result)
        self.assertEqual(flow._receipt({'id':record['id'],'request':cpu,'result':result},record['id'],cpu),result)
        result=flow._result({**record,'request':self.spec},self.directory,state='aborted')
        self.assertEqual(result['device_probes'],[]);flow._receipt({'id':record['id'],'request':self.spec,'result':result},record['id'],self.spec)

    def test_selection_is_explicit_complete_unique_and_bounded(self):
        for plans in [[],self.spec['device_probes']*2,[{**self.spec['device_probes'][0],'node_id':[]}],
            [{**self.spec['device_probes'][0],'device_ids':[True]}],[{**self.spec['device_probes'][0],'device_ids':[0,0]}],
            [{**self.spec['device_probes'][0],'memory_mib':True}],[{**self.spec['device_probes'][0],'memory_mib':65536}]]:
            with self.subTest(plans=plans),self.assertRaises(DeployError):flow.request({**self.spec,'device_probes':plans})
        with self.assertRaises(DeployError):flow.request({**self.spec,'quota':{**self.spec['quota'],'npu':0}})
        with self.assertRaisesRegex(DeployError,'Archive budget'):flow.request({**self.spec,'quota':{**self.spec['quota'],'archive_storage_mib':10}})

    def test_unregistered_selection_is_rejected_before_staging(self):
        spec=copy.deepcopy(self.spec);spec['device_probes'][0]['device_ids']=[2]
        with self.assertRaisesRegex(DeployError,'outside the registered'):flow._nodes(self.root,spec)


class ProbeWatchTests(unittest.TestCase):
    def test_same_operation_is_observed_until_terminal(self):
        operation=Mock(side_effect=[{'state':'nodes','device_probes':[]},{'state':'delivered','device_probes':[]}])
        with patch('time.monotonic',side_effect=[0,0]),patch('time.sleep') as sleep:result=flow.watch(operation,10)
        self.assertEqual(operation.call_count,2);sleep.assert_called_once_with(2);self.assertFalse(result['wait']['expired'])

    def test_attention_stops_observation_without_retry(self):
        operation=Mock(return_value={'state':'nodes','device_probes':[{'status':'attention'}]})
        with patch('time.monotonic',return_value=0),patch('time.sleep') as sleep:result=flow.watch(operation,10)
        operation.assert_called_once();sleep.assert_not_called();self.assertFalse(result['wait']['expired'])

    def test_observation_timeout_does_not_cancel_or_replace_the_task(self):
        operation=Mock(return_value={'state':'nodes','device_probes':[{'status':'waiting','job_id':'job-'+'a'*12}]})
        with patch('time.monotonic',side_effect=[0,0,3]),patch('time.sleep'):result=flow.watch(operation,3)
        self.assertTrue(result['wait']['expired']);self.assertEqual(operation.call_count,2);self.assertEqual(result['device_probes'][0]['job_id'],'job-'+'a'*12)

    def test_invalid_wait_has_no_operation_side_effect(self):
        for value in [0,-1,901,True,1.5]:
            operation=Mock()
            with self.assertRaises(DeployError):flow.watch(operation,value)
            operation.assert_not_called()


if __name__=='__main__':unittest.main()

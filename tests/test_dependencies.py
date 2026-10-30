"""Dependency admission, durable bounded transfer, failure and retention guards."""
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
from jobs import ROOT,JobStore,JobError,atomic_json
from dependencies import COPY_BYTES_PER_TICK
from workflow_spec import CAPABILITIES
from environment_contract import CAPABILITY,fingerprint
from test_environment_contract import IMAGE,contract
from deployment import DeployError
from maintenance import require_features


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='dependencies-test-');self.root=Path(self.temp.name)
        self.config={'username':'operator','password':'offline-test','container':'zdl'}
        self.jobs=JobStore(self.root,self.config)
        for name in ['alice','bob']:
            p=self.root/name;p.mkdir();self.jobs.access.create_user(name,p,environments=['worker-python'],quota={'npu':1,'memory_mib':1024})
        self.alice=self.jobs.access.user('alice');self.bob=self.jobs.access.user('bob')
        profile={'name':'stage-node','physical_id':self.jobs.backend.physical_id,'image':IMAGE,'python':'/usr/bin/python3',
            'environment_manifest':contract(),'users':{'alice':{'uid':24001,'gid':24001},'bob':{'uid':24002,'gid':24002}},
            'templates':['build_v1','ir_v1','accuracy_v1'],'policy':{'cpu_ids':self.jobs.policy['cpu_ids'],'memory_mib':2048,'shm_mib':0,'max_running':1}}
        self.node,_=self.jobs.nodes.enroll(profile)
        self.jobs.nodes.heartbeat(self.node,{'physical_id':profile['physical_id'],'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY,*CAPABILITIES],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
            'image_sha256':IMAGE,'environment_setup':None,'python_executable':'/usr/bin/python3','environment_sha256':fingerprint(contract())})
        self.base=self.input(self.alice)

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def input(self,owner):
        data=b'print(42)';bundle,_=self.jobs.inputs.create(owner,{'files':[{'name':'main.py','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]})
        self.jobs.inputs.upload(bundle['id'],owner,{'name':'main.py','offset':0,'data':base64.b64encode(data).decode()});return self.jobs.inputs.seal(bundle['id'],owner)

    def request(self,**changes):
        return {'template':'ir_v1','environment':'worker-python','parameters':{'input_id':self.base['id'],'argv':['python','main.py'],
            'lock':{'python':'3.10.12'},'artifacts':{'file_mib':8,'total_mib':16}},**changes}

    def parent(self,owner=None):
        return self.jobs.submit(self.request(template='build_v1'),owner or self.alice)[0]

    def child(self,parent,**changes):
        return self.jobs.submit(self.request(depends_on=[parent['id']],artifact_inputs=[{'job_id':parent['id'],'name':'results/package.bin','target':'deps/package.bin'}],**changes),self.alice)[0]

    def complete_parent(self,parent,data=b'compiled bytes',status='succeeded'):
        directory=self.jobs.directory(parent['id']);(directory/'results').mkdir(mode=0o700)
        (directory/'results/package.bin').write_bytes(data)
        item={'name':'results/package.bin','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        atomic_json(directory/'artifact-hashes.json',{'version':1,'files':[item]});atomic_json(directory/'artifacts.json',{'files':[{'name':item['name'],'bytes':len(data)}]})
        with self.jobs.transaction() as db:
            p=self.jobs.get(parent['id'],db);p.update(state=status,result={'status':status,'termination_confirmed':True,'exit_code':0 if status=='succeeded' else 1},attempt_id='attempt-'+'a'*32)
            self.jobs.write(db,p)
        return item

    def ready(self,job):
        for _ in range(20):
            self.jobs.dependencies.tick();actual=self.jobs.get(job['id'])
            if actual.get('dependency_state',{}).get('status') in {'ready','blocked','upstream_failed'}:return actual
        raise AssertionError('Handoff did not finish')

    def test_waiting_stage_has_no_attempt_and_no_compute_reservation(self):
        parent=self.parent();child=self.child(parent)
        with self.jobs.transaction() as db:
            p=self.jobs.get(parent['id'],db);p['state']='running';p['allocation']={'cpu_ids':self.jobs.policy['cpu_ids'][:1],'npu_ids':[]};p['_remote']=True;p['node_id']=self.node;self.jobs.write(db,p)
        self.jobs.dependencies.tick();actual=self.jobs.get(child['id'])
        self.assertEqual(actual['allocation'],{});self.assertFalse(self.jobs.attempts(child['id']))
        self.assertEqual(actual['dependency_state']['status'],'waiting');self.assertEqual(self.jobs.summary(self.alice)['reserved']['npu_ids'],[])

    def test_artifact_handoff_is_sealed_and_frozen_before_worker_grant(self):
        parent=self.parent();child=self.child(parent);item=self.complete_parent(parent);actual=self.ready(child)
        self.assertEqual(actual['dependency_state']['status'],'ready');self.assertNotEqual(actual['input']['id'],self.base['id'])
        self.assertEqual(self.jobs.inputs.get(actual['input']['id'],self.alice)['sealed'],True)
        self.assertEqual(actual['input_provenance']['artifacts'][0]['sha256'],item['sha256'])
        self.assertEqual((self.jobs.inputs.directory(actual['input']['id'])/'deps/package.bin').read_bytes(),b'compiled bytes')
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],child['id'])
        self.assertEqual(grant['input']['manifest_sha256'],actual['input']['manifest_sha256'])
        self.assertEqual(grant['parameters']['input_id'],actual['input']['id'])

    def test_upstream_failure_cancels_unstarted_child_without_device_claim(self):
        parent=self.parent();request=self.request(template='accuracy_v1',depends_on=[parent['id']])
        request['parameters']['resources']={'cpu':1,'memory_mib':512,'shm_mib':0,'npu':1};request['parameters']['lock']['npu_name']='NPU-NOT-ENROLLED'
        child=self.jobs.submit(request,self.alice)[0];self.complete_parent(parent,status='failed');actual=self.ready(child)
        self.assertEqual(actual['state'],'cancelled');self.assertEqual(actual['result']['failure_stage'],'dependency')
        self.assertIs(actual['result']['execution_started'],False);self.assertFalse(self.jobs.attempts(child['id']));self.assertEqual(actual['allocation'],{})

    def test_existing_parent_ids_prevent_forward_and_cyclic_graph_submission(self):
        before=len(self.jobs.all())
        with self.assertRaises(JobError):self.jobs.submit(self.request(depends_on=['job-'+'f'*12]),self.alice)
        self.assertEqual(len(self.jobs.all()),before)
        parent=self.parent();child=self.child(parent)
        with self.assertRaises(JobError):self.jobs.submit(self.request(depends_on=[child['id']],idempotency_key=parent['idempotency_key']),self.alice)
        self.assertFalse(self.jobs.get(parent['id']).get('depends_on'))

    def test_cross_owner_is_hidden_even_with_shared_project_read_access(self):
        self.jobs.access.add_member('shared',self.bob.id,'editor');self.jobs.access.add_member('shared',self.alice.id,'viewer')
        foreign=self.jobs.submit({**self.request(),'project':'shared','parameters':{**self.request()['parameters'],'input_id':self.input(self.bob)['id']}},self.bob)[0]
        self.assertTrue(self.jobs.access.can_read(self.alice,self.jobs.get(foreign['id'])))
        with self.assertRaises(JobError) as failure:self.child(foreign)
        self.assertEqual(failure.exception.status,404)

    def test_collisions_paths_metadata_sources_and_unknown_fields_are_rejected(self):
        parent=self.parent()
        for change in [{'target':'main.py'},{'target':'../private'},{'target':'main.py/child'},
            {'name':'request.json'},{'sha256':'bad'},{'executable':1},{'path':'/root'}]:
            item={'job_id':parent['id'],'name':'results/package.bin','target':'deps/package.bin',**change}
            with self.subTest(change=change),self.assertRaises(JobError):self.jobs.submit(self.request(depends_on=[parent['id']],artifact_inputs=[item]),self.alice)

    def test_explicit_digest_conflict_blocks_without_an_execution(self):
        parent=self.parent();item={'job_id':parent['id'],'name':'results/package.bin','target':'deps/pkg','sha256':'0'*64}
        child=self.jobs.submit(self.request(depends_on=[parent['id']],artifact_inputs=[item]),self.alice)[0]
        self.complete_parent(parent);actual=self.ready(child)
        self.assertEqual(actual['dependency_state']['status'],'blocked');self.assertFalse(self.jobs.attempts(child['id']))
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant'])

    def test_chunked_handoff_resumes_from_durable_offsets_in_new_store(self):
        parent=self.parent();child=self.child(parent);data=b'x'*(COPY_BYTES_PER_TICK*2+17);self.complete_parent(parent,data)
        self.jobs.dependencies.tick();actual=self.jobs.get(child['id']);self.assertEqual(actual['dependency_state']['status'],'copying')
        progress=self.jobs.inputs.get(actual['dependency_state']['input_id'],self.alice)
        self.assertLessEqual(sum(progress['uploaded'].values()),COPY_BYTES_PER_TICK)
        other=JobStore(self.root,self.config)
        try:
            for _ in range(5):other.dependencies.tick()
            final=other.get(child['id']);self.assertEqual(final['dependency_state']['status'],'ready')
            self.assertEqual((other.inputs.directory(final['input']['id'])/'deps/package.bin').read_bytes(),data)
        finally:other.access.close()

    def test_changed_source_cannot_publish_a_sealed_dependency_input(self):
        parent=self.parent();child=self.child(parent);self.complete_parent(parent,b'x'*(COPY_BYTES_PER_TICK+17))
        self.jobs.dependencies.tick();(self.jobs.directory(parent['id'])/'results/package.bin').write_bytes(b'y'*(COPY_BYTES_PER_TICK+17))
        actual=self.ready(child);self.assertEqual(actual['dependency_state']['status'],'blocked')
        bundle=self.jobs.inputs.get(actual['dependency_state']['input_id'],self.alice);self.assertFalse(bundle['sealed'])
        self.assertFalse(self.jobs.attempts(child['id']))

    def test_parent_archive_and_both_inputs_are_pinned_until_child_ends(self):
        parent=self.parent();child=self.child(parent);self.complete_parent(parent,b'x'*(COPY_BYTES_PER_TICK+17));self.jobs.dependencies.tick()
        actual=self.jobs.get(child['id'])
        for kind,identifier in [('archive',parent['id']),('input',self.base['id']),('input',actual['dependency_state']['input_id'])]:
            with self.subTest(kind=kind),self.jobs.connect() as db,self.assertRaises(JobError):self.jobs.lifecycle.guard(kind,identifier,self.alice,db)
        self.jobs.cancel(child['id'],self.alice)
        with self.jobs.connect() as db:self.jobs.lifecycle.guard('archive',parent['id'],self.alice,db)

    def test_derived_input_is_pinned_before_input_creation_returns(self):
        parent=self.parent();child=self.child(parent);self.complete_parent(parent)
        create=self.jobs.inputs.create
        def interleave(*args,**kwargs):
            result=create(*args,**kwargs)
            with self.jobs.connect() as db,self.assertRaises(JobError):
                self.jobs.lifecycle.guard('input',result[0]['id'],self.alice,db)
            return result
        with patch.object(self.jobs.inputs,'create',side_effect=interleave):self.jobs.dependencies.prepare(child['id'])

    def test_preexisting_derived_trash_stays_restorable_while_child_waits(self):
        parent=self.parent();first=self.child(parent);self.complete_parent(parent);ready=self.ready(first)
        derived=ready['input']['id'];self.jobs.cancel(first['id'],self.alice)
        plan=self.jobs.lifecycle.plan(self.alice,{'kind':'input','id':derived,'action':'trash'})['plan']
        self.jobs.lifecycle.apply(self.alice,plan)
        second=self.child(parent);self.jobs.dependencies.tick();waiting=self.jobs.get(second['id'])
        self.assertEqual(waiting['dependency_state']['status'],'waiting_storage');self.assertNotIn('input_id',waiting['dependency_state'])
        plan=self.jobs.lifecycle.plan(self.alice,{'kind':'input','id':derived,'action':'restore'})['plan']
        self.jobs.lifecycle.apply(self.alice,plan);self.assertEqual(self.ready(second)['input']['id'],derived)

    def test_waiting_front_items_do_not_starve_a_later_ready_handoff(self):
        pairs=[(self.parent(),None) for _ in range(5)]
        children=[self.jobs.submit(self.request(depends_on=[p['id']]),self.alice)[0] for p,_ in pairs]
        self.complete_parent(pairs[-1][0]);self.jobs.dependencies.tick();self.jobs.dependencies.tick()
        self.assertEqual(self.jobs.get(children[-1]['id'])['dependency_state']['status'],'ready')
        self.assertTrue(all(self.jobs.get(c['id'])['dependency_state']['status']=='waiting' for c in children[:-1]))

    def test_storage_budget_waits_and_resumes_without_compute_or_new_parent_run(self):
        parent=self.parent();child=self.child(parent);self.complete_parent(parent,b'x'*100)
        self.jobs.access.set_quota('alice',{'input_storage_mib':0});self.jobs.dependencies.tick()
        actual=self.jobs.get(child['id']);self.assertEqual(actual['dependency_state']['status'],'waiting_storage');self.assertFalse(self.jobs.attempts(child['id']))
        self.jobs.access.set_quota('alice',{'input_storage_mib':512});self.assertEqual(self.ready(child)['dependency_state']['status'],'ready')

    def test_revoked_owner_never_materializes_or_starts_a_child(self):
        parent=self.parent();child=self.child(parent);self.complete_parent(parent)
        self.jobs.access.disable_user('alice');self.jobs.dependencies.tick()
        self.assertEqual(self.jobs.get(child['id'])['dependency_state']['status'],'waiting');self.assertFalse(self.jobs.attempts(child['id']))

    def test_idempotent_retry_returns_original_job_after_input_materialization(self):
        parent=self.parent();request=self.request(depends_on=[parent['id']],artifact_inputs=[{'job_id':parent['id'],'name':'results/package.bin','target':'dep.bin'}],idempotency_key='stable-child')
        child=self.jobs.submit(request,self.alice)[0];self.complete_parent(parent);actual=self.ready(child)
        replay,reused=self.jobs.submit(request,self.alice);self.assertTrue(reused);self.assertEqual(replay['id'],actual['id']);self.assertEqual(replay['input'],actual['input'])

    def test_retry_is_owner_only_unstarted_and_never_accepts_path_traversal(self):
        parent=self.parent();child=self.child(parent)
        for identifier,principal in [(child['id'],self.bob),('../../elsewhere',self.alice)]:
            with self.assertRaises(JobError):self.jobs.dependencies.retry(identifier,principal)
        result=self.jobs.dependencies.retry(child['id'],self.alice);self.assertIs(result['source_binding_changed'],False)

    def test_barrier_fan_in_and_dependency_downgrade_guard(self):
        first=self.parent();second=self.parent();child=self.jobs.submit(self.request(depends_on=[first['id'],second['id']]),self.alice)[0]
        self.complete_parent(first);self.jobs.dependencies.tick();self.assertEqual(self.jobs.get(child['id'])['dependency_state']['status'],'waiting')
        self.complete_parent(second);self.assertEqual(self.ready(child)['dependency_state']['status'],'ready')
        with self.assertRaisesRegex(DeployError,'Dependency jobs'):require_features(self.root,'controller',{'files':{}})


if __name__=='__main__':unittest.main()

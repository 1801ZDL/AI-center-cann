"""Explicit failed-artifact edges retain original attempts and cleanup gates."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_dependencies as base
from dependencies import COPY_BYTES_PER_TICK
from jobs import JobStore,JobError,atomic_json
from maintenance import require_features
from deployment import DeployError
from node_storage import receipt


class TerminalDependencyTests(unittest.TestCase):
    def setUp(self):
        self.base=base.DependencyTests();self.base.setUp();self.jobs=self.base.jobs;self.owner=self.base.alice

    def tearDown(self):self.base.tearDown()

    def producer(self,status='failed',data=b'actual failed-task output'):
        parent=self.base.parent();item=self.base.complete_parent(parent,data,status)
        with self.jobs.transaction() as db:
            parent=self.jobs.get(parent['id'],db);parent.update(execution_backend='managed-docker',node_id=self.base.node)
            parent['result'].update(execution_cleanup_confirmed=True,workspace_release_confirmed=True)
            parent['_artifact_receipt']=receipt(parent,[item]);self.jobs.write(db,parent)
        return parent,item

    def consumer(self,parent,**changes):return self.jobs.submit(self.base.request(depends_on=[parent['id']],artifact_inputs=[{
        'job_id':parent['id'],'name':'results/package.bin','target':'deps/package.bin','when':'terminal'}],**changes),self.owner)[0]

    def update(self,parent,mutate):
        with self.jobs.transaction() as db:
            actual=self.jobs.get(parent['id'],db);mutate(actual);self.jobs.write(db,actual)

    def test_failed_timed_out_cancelled_and_successful_attempts_can_export_when_explicit(self):
        for state in ['failed','timed_out','cancelled','succeeded']:
            parent,item=self.producer(state);child=self.consumer(parent);job=self.base.ready(child)
            self.assertEqual(job['state'],'queued');self.assertEqual(job['dependency_state']['status'],'ready')
            self.assertEqual(job['input_provenance']['terminal_sources'][parent['id']]['state'],state)
            self.assertEqual(job['input_provenance']['artifacts'][0]['when'],'terminal')
            self.assertEqual((self.jobs.inputs.directory(job['input']['id'])/'deps/package.bin').read_bytes(),b'actual failed-task output')
            self.assertFalse(self.jobs.attempts(parent['id']));self.assertEqual(self.jobs.get(parent['id'])['state'],state)
            self.jobs.cancel(child['id'],self.owner)

    def test_default_success_edge_still_cancels_without_claiming_failure_was_executed(self):
        parent,_=self.producer();ordinary=self.base.child(parent);diagnostic=self.consumer(parent)
        self.assertEqual(self.base.ready(ordinary)['state'],'cancelled')
        self.assertEqual(self.base.ready(diagnostic)['dependency_state']['status'],'ready')
        self.assertFalse(self.jobs.attempts(ordinary['id']))

    def test_live_unknown_and_pending_output_are_not_terminal_evidence(self):
        for state in ['running','needs_attention','cancelling']:
            parent,_=self.producer();self.update(parent,lambda p:p.update(state=state,output_pending=True))
            child=self.consumer(parent);self.jobs.dependencies.tick();job=self.jobs.get(child['id'])
            self.assertEqual(job['dependency_state']['status'],'waiting');self.assertEqual(job['allocation'],{})
            self.assertFalse(self.jobs.attempts(child['id']));self.assertEqual(self.jobs.get(parent['id'])['state'],state)
            self.jobs.cancel(child['id'],self.owner);self.update(parent,lambda p:p.update(state='failed'))

    def test_every_required_cleanup_confirmation_waits_without_a_child_attempt(self):
        for key in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed','cache_release_confirmed','npu_release_confirmed']:
            parent,_=self.producer()
            def modify(p):
                p['allocation']={'workspace':{'id':'test-only'},'cache':{'id':'test-only'}};p['resources']['npu']=1
                p['result'].update(termination_confirmed=True,execution_cleanup_confirmed=True,workspace_release_confirmed=True,cache_release_confirmed=True,npu_release_confirmed=True)
                p['result'][key]=False
            self.update(parent,modify);child=self.consumer(parent);self.jobs.dependencies.tick()
            self.assertEqual(self.jobs.get(child['id'])['dependency_state']['status'],'waiting');self.assertFalse(self.jobs.attempts(child['id']))
            self.jobs.cancel(child['id'],self.owner)

    def test_cancelled_without_execution_is_blocked_not_invented_as_an_artifact_attempt(self):
        parent=self.base.parent();self.jobs.cancel(parent['id'],self.owner);child=self.consumer(parent)
        self.assertEqual(self.base.ready(child)['dependency_state']['status'],'blocked');self.assertFalse(self.jobs.attempts(child['id']))

    def test_receipt_and_complete_manifest_binding_are_required(self):
        parent,_=self.producer();self.update(parent,lambda p:p.pop('_artifact_receipt'))
        child=self.consumer(parent);self.jobs.dependencies.tick();self.assertEqual(self.jobs.get(child['id'])['dependency_state']['status'],'waiting')
        self.jobs.cancel(child['id'],self.owner)
        parent,_=self.producer();path=self.jobs.directory(parent['id'])/'artifact-hashes.json';hashes=json.loads(path.read_text());hashes['files'][0]['sha256']='0'*64;atomic_json(path,hashes)
        child=self.consumer(parent);self.assertEqual(self.base.ready(child)['dependency_state']['status'],'blocked')

    def test_mixed_or_unknown_conditions_and_implicit_override_are_rejected(self):
        parent,_=self.producer()
        for mode in ['always',None,True,[],{}]:
            imports=[{'job_id':parent['id'],'name':'results/package.bin','target':'a.bin','when':mode}]
            with self.subTest(mode=mode),self.assertRaises(JobError):self.jobs.submit(self.base.request(depends_on=[parent['id']],artifact_inputs=imports),self.owner)
        imports=[{'job_id':parent['id'],'name':'results/package.bin','target':'a.bin','when':'terminal'},
                 {'job_id':parent['id'],'name':'results/package.bin','target':'b.bin'}]
        with self.assertRaises(JobError):self.jobs.submit(self.base.request(depends_on=[parent['id']],artifact_inputs=imports),self.owner)

    def test_success_condition_normalizes_without_changing_legacy_idempotency(self):
        parent,_=self.producer('succeeded');request=self.base.request(idempotency_key='same-success',depends_on=[parent['id']],artifact_inputs=[{
            'job_id':parent['id'],'name':'results/package.bin','target':'dep.bin'}])
        first=self.jobs.submit(request,self.owner)[0];request['artifact_inputs'][0]['when']='success'
        replay,reused=self.jobs.submit(request,self.owner);self.assertTrue(reused);self.assertEqual(replay['id'],first['id'])
        request['artifact_inputs'][0]['when']='terminal'
        with self.assertRaises(JobError):self.jobs.submit(request,self.owner)

    def test_foreign_owner_and_control_files_remain_inaccessible(self):
        parent,_=self.producer()
        foreign=self.base.request(parameters={**self.base.request()['parameters'],'input_id':self.base.input(self.base.bob)['id']},depends_on=[parent['id']],artifact_inputs=[{
            'job_id':parent['id'],'name':'results/package.bin','target':'dep.bin','when':'terminal'}])
        with self.assertRaises(JobError) as error:self.jobs.submit(foreign,self.base.bob)
        self.assertEqual(error.exception.status,404)
        for name in ['request.json','stdout.log','../results/package.bin']:
            with self.assertRaises(JobError):self.jobs.submit(self.base.request(depends_on=[parent['id']],artifact_inputs=[{
                'job_id':parent['id'],'name':name,'target':'dep.bin','when':'terminal'}]),self.owner)

    def test_terminal_handoff_resumes_from_original_offsets_in_a_fresh_store(self):
        parent,_=self.producer(data=b'd'*(2*COPY_BYTES_PER_TICK+1));child=self.consumer(parent)
        self.jobs.dependencies.tick();partial=self.jobs.get(child['id']);self.assertEqual(partial['dependency_state']['status'],'copying')
        before=partial['dependency_state']['input_id'];other=JobStore(self.base.root,self.base.config)
        try:
            for _ in range(5):other.dependencies.tick()
            final=other.get(child['id']);self.assertEqual(final['dependency_state']['status'],'ready');self.assertEqual(final['input']['id'],before)
            self.assertEqual(final['input_provenance']['terminal_sources'][parent['id']]['attempt_id'],parent['attempt_id'])
        finally:other.access.close()

    def test_evidence_change_after_copy_is_blocked_and_after_grant_waits_without_replacement(self):
        parent,_=self.producer();child=self.consumer(parent);self.base.ready(child)
        grant=self.jobs.nodes.claim(self.base.node)['grant'];self.assertEqual(grant['job_id'],child['id'])
        self.update(parent,lambda p:p['result'].update(execution_cleanup_confirmed=False))
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        reply=self.jobs.nodes.authorize_start(self.base.node,auth);self.assertTrue(reply['wait']);self.assertFalse(reply['start_permitted'])
        self.assertEqual(len(self.jobs.attempts(child['id'])),1);self.assertEqual(self.jobs.get(child['id'])['attempt_id'],grant['attempt_id'])

    def test_parent_state_change_before_seal_does_not_publish_ready_input(self):
        parent,_=self.producer();child=self.consumer(parent);seal=self.jobs.inputs.seal
        def change(*args,**kwargs):
            result=seal(*args,**kwargs);self.update(parent,lambda p:p['result'].update(execution_cleanup_confirmed=False));return result
        with patch.object(self.jobs.inputs,'seal',side_effect=change):self.jobs.dependencies.tick()
        self.assertEqual(self.jobs.get(child['id'])['dependency_state']['status'],'blocked');self.assertFalse(self.jobs.attempts(child['id']))

    def test_downgrade_refuses_even_finished_terminal_edge_history(self):
        parent,_=self.producer();child=self.consumer(parent);self.base.ready(child);self.jobs.cancel(child['id'],self.owner)
        with self.assertRaisesRegex(DeployError,'Terminal artifact'):
            require_features(self.base.root,'controller',{'files':{'dependencies.py':{}}})


if __name__=='__main__':unittest.main()

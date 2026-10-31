"""File-scoped delegation without expanding budget-bound job visibility."""
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_dependencies as builder
from dependencies import COPY_BYTES_PER_TICK
from deployment import DeployError
from jobs import JobError,JobStore,atomic_json
from lab_client import Client,ClientHTTPError
from maintenance import require_features
from node_storage import receipt
from server import ThreadingHTTPServer,handler_class
from source_grants import CAPABILITY,rebind_restored


class SourceGrantTests(unittest.TestCase):
    def setUp(self):
        self.b=builder.DependencyTests();self.b.setUp();self.jobs=self.b.jobs;self.owner=self.b.alice
        self.grants=self.jobs.source_grants;self.budgets=self.jobs.experiment_budgets
        self.budget_spec={'name':'Source handoff','limits':{'jobs':8,'cpu_seconds':1000,'npu_seconds':0},
            'templates':['build_v1','ir_v1'],'lifetime_seconds':3600,'idempotency_key':'old'}
        self.old=self.budgets.create(self.owner,self.budget_spec)[0]
        self.new=self.budgets.create(self.owner,{**self.budget_spec,'idempotency_key':'new'})[0]
        self.token=self.budgets.delegate(self.owner,self.new['id'])['token']
        self.agent=self.jobs.access.authenticate('Bearer '+self.token)
        self.parent=self.producer()
        self.spec={'source_job_id':self.parent['id'],'destination_experiment_id':self.new['id'],
            'files':[{'name':'results/package.bin'}],'lifetime_seconds':1800,'idempotency_key':'source-one'}
        self.plan=self.grants.plan(self.owner,self.spec)
        self.grant=self.grants.create(self.owner,self.plan)[0]

    def tearDown(self):self.b.tearDown()

    def request(self,**changes):
        params={**self.b.request()['parameters'],'timeout_seconds':30}
        return self.b.request(parameters=params,**changes)

    def producer(self,data=b'actual prior failure',status='failed'):
        parent=self.jobs.submit(self.request(template='build_v1',experiment_id=self.old['id']),self.owner)[0]
        item=self.b.complete_parent(parent,data,status)
        with self.jobs.transaction() as db:
            parent=self.jobs.get(parent['id'],db);parent.update(execution_backend='managed-docker',node_id=self.b.node)
            parent['result'].update(execution_cleanup_confirmed=True,workspace_release_confirmed=True)
            parent['_artifact_receipt']=receipt(parent,[item]);self.jobs.write(db,parent)
        return parent

    def consumer(self,*,grant=None,key='consumer',principal=None,**changes):
        item={'job_id':self.parent['id'],'name':'results/package.bin','target':'deps/source.bin','when':'terminal',
            'source_grant':(grant or self.grant)['id']}
        item.update(changes.pop('item',{}))
        req=self.request(idempotency_key=key,depends_on=[self.parent['id']],artifact_inputs=[item],**changes)
        return self.jobs.submit(req,principal or self.agent)[0],req

    def mutate_grant(self,key,**changes):
        with self.jobs.transaction() as db:
            raw=db.execute('SELECT data FROM source_grants WHERE id=?',(key,)).fetchone()[0]
            value={**json.loads(raw),**changes}
            db.execute('UPDATE source_grants SET data=?,state=?,expires_at=? WHERE id=?',
                (json.dumps(value),value['state'],value['expires_at'],key))

    def test_imports_approved_bytes_but_keeps_source_job_and_other_budget_private(self):
        child,_=self.consumer();job=self.b.ready(child)
        self.assertEqual(job['dependency_state']['status'],'ready')
        self.assertEqual((self.jobs.inputs.directory(job['input']['id'])/'deps/source.bin').read_bytes(),b'actual prior failure')
        self.assertEqual(job['input_provenance']['artifacts'][0]['source_grant'],self.grant['id'])
        self.assertEqual(job['input_provenance']['terminal_sources'][self.parent['id']]['attempt_id'],self.parent['attempt_id'])
        with self.assertRaises(Exception):self.jobs.get(self.parent['id'],principal=self.agent)
        self.assertEqual([v['id'] for v in self.jobs.access.readable_jobs(self.agent,self.jobs.all())],[child['id']])
        with self.jobs.connect() as db:
            self.assertEqual(self.budgets.get(db,self.new['id'],self.owner)['charged']['jobs'],1)
            self.assertEqual(self.budgets.get(db,self.old['id'],self.owner)['charged']['jobs'],1)
        self.assertEqual(self.jobs.get(self.parent['id'])['state'],'failed')

    def test_preview_is_read_only_and_approval_rechecks_complete_source_receipt(self):
        with self.jobs.connect() as db:before=list(db.iterdump())
        plan=self.grants.plan(self.owner,{**self.spec,'idempotency_key':'next'})
        with self.jobs.connect() as db:self.assertEqual(before,list(db.iterdump()))
        with self.jobs.transaction() as db:
            parent=self.jobs.get(self.parent['id'],db);parent['result']['execution_cleanup_confirmed']=False;self.jobs.write(db,parent)
        with self.assertRaises(JobError):self.grants.create(self.owner,plan)
        with self.assertRaises(JobError):self.consumer()

    def test_no_grant_does_not_bypass_direct_parent_acl(self):
        req=self.request(depends_on=[self.parent['id']],artifact_inputs=[{
            'job_id':self.parent['id'],'name':'results/package.bin','target':'a','when':'terminal'}])
        with self.assertRaises(Exception):self.jobs.submit(req,self.agent)
        req['artifact_inputs']=[]
        with self.assertRaises(Exception):self.jobs.submit(req,self.agent)
        self.assertEqual(len(self.jobs.all()),1)

    def test_approval_and_revocation_reject_delegated_admin_legacy_and_peer_credentials(self):
        for principal in [self.agent,self.jobs.access.local]:
            with self.subTest(principal=principal.id):
                for action in [lambda:self.grants.plan(principal,self.spec),lambda:self.grants.create(principal,self.plan),
                    lambda:self.grants.revoke(principal,self.grant['id'])]:
                    with self.assertRaises(JobError) as error:action()
                    self.assertEqual(error.exception.status,403)
        for action in [lambda:self.grants.plan(self.b.bob,self.spec),
            lambda:self.grants.revoke(self.b.bob,self.grant['id'])]:
            with self.assertRaises(JobError) as error:action()
            self.assertEqual(error.exception.status,404)

    def test_grant_is_exactly_bound_to_budget_parent_name_digest_and_execute_permission(self):
        for item in [{'name':'results/other.bin'},{'sha256':'0'*64},{'executable':True}]:
            with self.subTest(item=item),self.assertRaises(JobError):self.consumer(item=item)
        other=self.budgets.create(self.owner,{**self.budget_spec,'idempotency_key':'third'})[0]
        actor=self.jobs.access.authenticate('Bearer '+self.budgets.delegate(self.owner,other['id'])['token'])
        with self.assertRaises(JobError):self.consumer(principal=actor)
        with self.assertRaises(JobError):self.consumer(principal=self.owner)  # No destination budget selected.
        approved=self.grants.create(self.owner,self.grants.plan(self.owner,{**self.spec,
            'idempotency_key':'executable','files':[{'name':'results/package.bin','executable':True}]}))[0]
        child,_=self.consumer(grant=approved,item={'executable':True,'sha256':self.plan['files'][0]['sha256']})
        self.assertTrue(self.b.ready(child)['input']['files'][0]['executable'])

    def test_revoked_or_expired_new_submission_rolls_back_without_job_or_charge(self):
        self.grants.revoke(self.owner,self.grant['id'])
        with self.assertRaises(JobError):self.consumer()
        other=self.grants.create(self.owner,self.grants.plan(self.owner,{**self.spec,'idempotency_key':'expiry'}))[0]
        self.mutate_grant(other['id'],expires_at=0)
        with self.assertRaises(JobError):self.consumer(grant=other)
        self.assertEqual(len(self.jobs.all()),1)
        with self.jobs.connect() as db:self.assertEqual(self.budgets.get(db,self.new['id'],self.owner)['charged']['jobs'],0)

    def test_idempotency_does_not_renew_or_revoke_revocation_and_replay_does_not_charge(self):
        child,req=self.consumer();original_expiry=self.grant['expires_at']
        self.grants.revoke(self.owner,self.grant['id']);self.budgets.close(self.owner,self.new['id'])
        replay,reused=self.grants.create(self.owner,self.plan)
        self.assertTrue(reused);self.assertEqual(replay['state'],'revoked');self.assertEqual(replay['expires_at'],original_expiry)
        job,reused=self.jobs.submit(req,self.agent);self.assertTrue(reused);self.assertEqual(job['id'],child['id'])
        changed=copy.deepcopy(self.plan);changed['lifetime_seconds']-=1
        with self.assertRaises(JobError):self.grants.create(self.owner,changed)
        with self.jobs.connect() as db:self.assertEqual(self.budgets.get(db,self.new['id'],self.owner)['charged']['jobs'],1)

    def test_copy_revocation_preserves_progress_and_restart_cannot_reauthorize(self):
        parent=self.producer(b'x'*(2*COPY_BYTES_PER_TICK+1));self.parent=parent
        grant=self.grants.create(self.owner,self.grants.plan(self.owner,{**self.spec,
            'source_job_id':parent['id'],'idempotency_key':'large'}))[0]
        child,_=self.consumer(grant=grant);upload=self.jobs.inputs.upload;count=0
        def revoke(*args,**kwargs):
            nonlocal count
            value=upload(*args,**kwargs);count+=1
            if count==1:self.grants.revoke(self.owner,grant['id'])
            return value
        with patch.object(self.jobs.inputs,'upload',side_effect=revoke):self.jobs.dependencies.tick()
        partial=self.jobs.get(child['id']);key=partial['dependency_state']['input_id']
        uploaded=self.jobs.inputs.get(key,self.owner)['uploaded'];self.assertEqual(count,1)
        self.assertGreater(sum(uploaded.values()),0);self.assertLess(sum(uploaded.values()),COPY_BYTES_PER_TICK)
        reopened=JobStore(self.b.root,self.b.config)
        try:
            reopened.dependencies.tick();actual=reopened.get(child['id'])
            self.assertEqual(actual['state'],'queued');self.assertFalse(actual.get('attempt_id'))
            self.assertEqual(actual['dependency_state']['input_id'],key)
            self.assertEqual(reopened.inputs.get(key,self.owner)['uploaded'],uploaded)
            self.assertFalse(reopened.nodes.claim(self.b.node)['grant'])
        finally:reopened.access.close()

    def test_expiry_after_ready_prevents_claim_and_preserves_sealed_input(self):
        child,_=self.consumer();ready=self.b.ready(child);self.mutate_grant(self.grant['id'],expires_at=0)
        self.assertFalse(self.jobs.nodes.claim(self.b.node)['grant'])
        self.assertEqual(self.jobs.get(child['id'])['input']['id'],ready['input']['id'])
        self.assertFalse(self.jobs.attempts(child['id']))

    def test_revoke_after_reservation_preserves_attempt_until_explicit_cancel(self):
        child,_=self.consumer();ready=self.b.ready(child);grant=self.jobs.nodes.claim(self.b.node)['grant']
        self.grants.revoke(self.owner,self.grant['id'])
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        reply=self.jobs.nodes.authorize_start(self.b.node,auth)
        self.assertTrue(reply['wait']);self.assertFalse(reply['start_permitted'])
        actual=self.jobs.get(child['id']);self.assertEqual(actual['attempt_id'],grant['attempt_id'])
        self.assertEqual(actual['input']['id'],ready['input']['id']);self.assertEqual(len(self.jobs.attempts(child['id'])),1)

    def test_already_authorized_task_is_not_retroactively_cancelled_by_revocation(self):
        child,_=self.consumer();self.b.ready(child);grant=self.jobs.nodes.claim(self.b.node)['grant']
        auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        self.assertTrue(self.jobs.nodes.authorize_start(self.b.node,auth)['start_permitted'])
        self.grants.revoke(self.owner,self.grant['id']);reply=self.jobs.nodes.authorize_start(self.b.node,auth)
        self.assertTrue(reply['already_authorized']);self.assertFalse(reply['cancel'])

    def test_closed_or_exhausted_destination_keeps_preapproved_task_semantics(self):
        self.budgets.close(self.owner,self.new['id'])
        with self.assertRaises(JobError):self.grants.plan(self.owner,{**self.spec,'idempotency_key':'closed'})
        # A final charged task has remaining.jobs == 0; it must still run.
        budget=self.budgets.create(self.owner,{**self.budget_spec,'idempotency_key':'last',
            'limits':{'jobs':1,'cpu_seconds':30,'npu_seconds':0}})[0]
        grant=self.grants.create(self.owner,self.grants.plan(self.owner,{**self.spec,
            'destination_experiment_id':budget['id'],'idempotency_key':'last'}))[0]
        agent=self.jobs.access.authenticate('Bearer '+self.budgets.delegate(self.owner,budget['id'])['token'])
        child,_=self.consumer(grant=grant,principal=agent);self.budgets.close(self.owner,budget['id'])
        self.assertEqual(self.b.ready(child)['dependency_state']['status'],'ready')
        self.assertEqual(self.jobs.nodes.claim(self.b.node)['grant']['job_id'],child['id'])

    def test_active_grant_pins_source_before_a_consumer_exists_and_revocation_unpins(self):
        with self.assertRaises(JobError):self.jobs.lifecycle.plan(self.owner,{'kind':'archive','id':self.parent['id'],'action':'trash'})
        self.grants.revoke(self.owner,self.grant['id'])
        self.assertEqual(self.jobs.lifecycle.plan(self.owner,{'kind':'archive','id':self.parent['id'],'action':'trash'})['plan']['action'],'trash')

    def test_consumer_with_an_approved_ancestor_can_chain_within_current_budget_without_old_reads(self):
        child,_=self.consumer();self.b.ready(child)
        # The next edge reads this budget's child; no extra access to the old ancestor.
        next_job=self.jobs.submit(self.request(depends_on=[child['id']]),self.agent)[0]
        self.assertEqual(next_job['depends_on'],[child['id']])
        with self.assertRaises(Exception):self.jobs.get(self.parent['id'],principal=self.agent)

    def test_mixed_grant_parent_and_path_or_control_file_overrides_are_rejected(self):
        for change in [{'source_grant':[]},{'source_grant':None},{'name':'stdout.log'},{'name':'../results/package.bin'}]:
            with self.subTest(change=change),self.assertRaises(JobError):self.consumer(item=change)
        for selected in [[{'name':'request.json'}],[{'name':'results/package.bin','executable':1}],
            [{'name':'results/package.bin'},{'name':'results/package.bin'}]]:
            with self.subTest(selected=selected),self.assertRaises(JobError):self.grants.plan(self.owner,{**self.spec,'files':selected})
        imports=[{'job_id':self.parent['id'],'name':'results/package.bin','target':'a','when':'terminal','source_grant':self.grant['id']},
            {'job_id':self.parent['id'],'name':'results/package.bin','target':'b','when':'terminal'}]
        with self.assertRaises(JobError):self.jobs.submit(self.request(depends_on=[self.parent['id']],artifact_inputs=imports),self.agent)

    def test_manifest_change_and_symlink_replacement_do_not_authorize_import(self):
        path=self.jobs.directory(self.parent['id'])/'artifact-hashes.json';original=path.read_bytes()
        value=json.loads(original);value['files'][0]['sha256']='0'*64;atomic_json(path,value)
        with self.assertRaises(JobError):self.consumer()
        path.write_bytes(original);file=self.jobs.directory(self.parent['id'])/'results/package.bin'
        saved=file.with_name('retained.bin');file.rename(saved);file.symlink_to(saved.name)
        with self.assertRaises(JobError):self.consumer()

    def test_default_success_condition_still_cancels_failed_producer_import(self):
        child,_=self.consumer(item={'when':'success'});actual=self.b.ready(child)
        self.assertEqual(actual['state'],'cancelled');self.assertFalse(self.jobs.attempts(child['id']))

    def test_restore_freezes_even_revoked_history_and_requires_new_budget_and_grant(self):
        rebind_restored(self.b.root)
        from experiment_budget import rebind_restored as freeze_budgets
        freeze_budgets(self.b.root)
        with self.jobs.connect() as db:self.assertEqual(self.grants.get(db,self.grant['id'],self.owner)['state'],'restored_frozen')
        self.assertIsNone(self.jobs.access.authenticate('Bearer '+self.token))
        with self.assertRaises(JobError):self.consumer(experiment_id=self.new['id'],principal=self.owner)
        fresh=self.budgets.create(self.owner,{**self.budget_spec,'idempotency_key':'after-restore'})[0]
        grant=self.grants.create(self.owner,self.grants.plan(self.owner,{**self.spec,
            'destination_experiment_id':fresh['id'],'idempotency_key':'fresh-grant'}))[0]
        child,_=self.consumer(grant=grant,experiment_id=fresh['id'],principal=self.owner)
        self.assertEqual(self.b.ready(child)['dependency_state']['status'],'ready')
        with self.assertRaisesRegex(DeployError,'source grants'):
            require_features(self.b.root,'controller',{'files':{'experiment_budget.py':{},'dependencies.py':{},'dependency_conditions.py':{}}})

    def test_concurrent_approval_has_one_identity_and_does_not_extend_expiry(self):
        plan=self.grants.plan(self.owner,{**self.spec,'idempotency_key':'concurrent'})
        with ThreadPoolExecutor(max_workers=6) as pool:
            values=list(pool.map(lambda _:self.grants.create(self.owner,plan),range(12)))
        self.assertEqual(len({x[0]['id'] for x in values}),1)
        self.assertEqual(sum(not x[1] for x in values),1)
        self.assertEqual({x[0]['expires_at'] for x in values},{plan['expires_at']})

    def test_revocation_during_seal_does_not_publish_ready_or_replace_input(self):
        child,_=self.consumer();seal=self.jobs.inputs.seal
        def revoke(*args,**kwargs):
            result=seal(*args,**kwargs);self.grants.revoke(self.owner,self.grant['id']);return result
        with patch.object(self.jobs.inputs,'seal',side_effect=revoke):self.jobs.dependencies.tick()
        job=self.jobs.get(child['id']);self.assertNotEqual(job['dependency_state']['status'],'ready')
        self.assertTrue(self.jobs.inputs.get(job['dependency_state']['input_id'],self.owner)['sealed'])
        self.assertFalse(self.jobs.nodes.claim(self.b.node)['grant']);self.assertFalse(self.jobs.attempts(child['id']))

    def test_unconfirmed_source_and_bounded_approval_limits_fail_closed(self):
        for change in [{'lifetime_seconds':86401},{'lifetime_seconds':True},{'files':[]},
            {'files':[{'name':'results/'+'x'*513}]},{'source_job_id':'job-'+'f'*12}]:
            with self.subTest(change=change),self.assertRaises(JobError):self.grants.plan(self.owner,{**self.spec,**change})
        altered=copy.deepcopy(self.plan);altered['source']['attempt_id']='attempt-'+'b'*32
        with self.assertRaises(JobError):self.grants.create(self.owner,altered)
        altered=copy.deepcopy(self.plan);altered.update(idempotency_key='bad-size');altered['files'][0]['size']=True
        with self.assertRaises(JobError):self.grants.create(self.owner,altered)
        plan=self.grants.plan(self.owner,{**self.spec,'idempotency_key':'limit'})
        with patch('source_grants.MAX_PER_OWNER',1):
            with self.assertRaises(JobError):self.grants.create(self.owner,plan)
            self.assertTrue(self.grants.create(self.owner,self.plan)[1])
        with self.jobs.transaction() as db:
            parent=self.jobs.get(self.parent['id'],db);parent.pop('_artifact_receipt');self.jobs.write(db,parent)
        with self.assertRaises(JobError):self.grants.plan(self.owner,{**self.spec,'idempotency_key':'unconfirmed'})

    def test_successful_source_is_frozen_even_without_terminal_condition(self):
        self.parent=self.producer(status='succeeded')
        grant=self.grants.create(self.owner,self.grants.plan(self.owner,{**self.spec,
            'idempotency_key':'success-source','source_job_id':self.parent['id']}))[0]
        child,_=self.consumer(grant=grant,item={'when':'success'});ready=self.b.ready(child)
        with self.jobs.transaction() as db:
            parent=self.jobs.get(self.parent['id'],db);parent['result']['execution_cleanup_confirmed']=False;self.jobs.write(db,parent)
        self.assertFalse(self.jobs.nodes.claim(self.b.node)['grant'])
        self.assertEqual(self.jobs.get(child['id'])['input']['id'],ready['input']['id'])

    def test_revoked_aged_dependency_cannot_keep_another_entrys_fairness_hold(self):
        from queue_policy import note_wait,protected
        child,_=self.consumer();self.b.ready(child)
        physical=self.jobs.backend.physical_id
        with self.jobs.transaction() as db:
            job=self.jobs.get(child['id'],db);job['created_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=600)).isoformat()
            domain=self.jobs.resources.get(physical,db)
            note_wait(self.jobs,job,self.b.node,domain,1,db,True);self.jobs.write(db,job)
            self.assertEqual(protected(self.jobs,self.jobs.all(db),physical,db)['id'],child['id'])
        self.grants.revoke(self.owner,self.grant['id'])
        with self.jobs.connect() as db:self.assertIsNone(protected(self.jobs,self.jobs.all(db),physical,db))

    def test_http_and_portable_cli_preserve_scopes_and_never_open_source_archive(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(self.b.config,Mock(),self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        token=self.jobs.access.issue_token(self.owner.id,{'jobs:read','jobs:submit'},3600)
        owner=Client({'controller':url,'token':token});agent=Client({'controller':url,'token':self.token})
        peer=Client({'controller':url,'token':self.jobs.access.issue_token(self.b.bob.id,{'jobs:read','jobs:submit'},3600)})
        try:
            self.assertIn(CAPABILITY,agent.request('/api/me')['dependency_capabilities'])
            self.assertEqual(agent.request('/api/source-grants')['grants'][0]['id'],self.grant['id'])
            for path in ['/api/jobs/'+self.parent['id'],'/api/jobs/'+self.parent['id']+'/artifact?name=results%2Fpackage.bin',
                '/api/jobs/'+self.parent['id']+'/artifacts','/api/jobs/'+self.parent['id']+'/logs']:
                with self.assertRaises(ClientHTTPError) as error:agent.request(path)
                self.assertEqual(error.exception.status,404)
            for client in [agent,peer]:
                with self.assertRaises(ClientHTTPError):client.request('/api/source-grants/preview',self.spec)
            with self.assertRaises(ClientHTTPError) as error:peer.request('/api/source-grants/'+self.grant['id'])
            self.assertEqual(error.exception.status,404)
            private=self.b.root/'cli';private.mkdir(mode=0o700)
            profile=private/'client.json';atomic_json(profile,{'controller':url,'token':token})
            spec=private/'spec.json';atomic_json(spec,{**self.spec,'idempotency_key':'cli'});plan=private/'plan.json'
            installed=private/'installed'
            subprocess.run([str(ROOT/'install-client'),'--prefix',str(installed)],capture_output=True,check=True)
            def cli(*args):
                reply=subprocess.run([str(installed/'labctl'),'--profile',str(profile),'source-grant',*args],capture_output=True,text=True)
                self.assertEqual(reply.returncode,0,reply.stderr);return json.loads(reply.stdout)
            self.assertFalse(cli('plan','--spec',str(spec),'--output',str(plan))['approved'])
            self.assertEqual(plan.stat().st_mode&0o777,0o600)
            approved=cli('approve','--plan',str(plan))['grant'];self.assertEqual(approved['state'],'active')
            self.assertEqual(cli('show',approved['id'])['grant']['files'],self.grant['files'])
            self.assertEqual(cli('revoke',approved['id'])['grant']['state'],'revoked')
            self.assertTrue(cli('approve','--plan',str(plan))['reused'])
            self.assertEqual(owner.request('/api/source-grants/'+approved['id'])['grant']['state'],'revoked')
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()

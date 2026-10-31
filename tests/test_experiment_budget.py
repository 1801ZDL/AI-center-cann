"""Conservation and credential boundary, not physical-time or model billing."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_nodes as builder
from experiment_budget import policy,require_compatible
from jobs import JobError,JobStore
from lab_client import Client,ClientHTTPError
from server import ThreadingHTTPServer,handler_class


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.NodeTests();self.base.setUp();self.jobs=self.base.jobs;self.alice=self.base.alice;self.bob=self.base.bob
        self.budgets=self.jobs.experiment_budgets
        self.spec={'name':'Bound investigation','idempotency_key':'budget-one',
                   'limits':{'jobs':2,'cpu_seconds':60,'npu_seconds':0},'templates':['cpu_demo'],'lifetime_seconds':3600}
        self.budget=self.budgets.create(self.alice,self.spec)[0]
        self.token=self.budgets.delegate(self.alice,self.budget['id'])['token']
        self.agent=self.jobs.access.authenticate('Bearer '+self.token)
        self.request={'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'task-one'}

    def tearDown(self):self.base.tearDown()

    def view(self):
        with self.jobs.connect() as db:return self.budgets.get(db,self.budget['id'],self.alice)

    def test_bound_credential_automatically_selects_original_budget(self):
        self.assertEqual(self.agent.experiment_id,self.budget['id'])
        job,_=self.jobs.submit(self.request,self.agent)
        self.assertEqual(job['experiment_id'],self.budget['id']);self.assertEqual(job['experiment_charge']['cpu_seconds'],30)
        self.assertEqual(self.view()['charged'],{'jobs':1,'cpu_seconds':30,'npu_seconds':0})
        other=self.budgets.create(self.alice,{**self.spec,'idempotency_key':'other'})[0]
        with self.assertRaises(JobError):self.jobs.submit({**self.request,'experiment_id':other['id']},self.agent)
        for operation in [lambda:self.budgets.create(self.agent,self.spec),lambda:self.budgets.delegate(self.agent,self.budget['id']),lambda:self.budgets.close(self.agent,self.budget['id'])]:
            with self.assertRaises(JobError):operation()

    def test_creation_retry_preserves_expiry_and_limits_instead_of_renewing(self):
        value,reused=self.budgets.create(self.alice,self.spec);self.assertTrue(reused)
        self.assertEqual(value['id'],self.budget['id']);self.assertEqual(value['expires_at'],self.budget['expires_at'])
        changed=copy.deepcopy(self.spec);changed['limits']['jobs']=3
        with self.assertRaises(JobError):self.budgets.create(self.alice,changed)

    def test_parallel_clients_cannot_overspend_one_budget(self):
        def submit(number):
            try:return self.jobs.submit({**self.request,'idempotency_key':'parallel-'+str(number)},self.agent)[0]['id']
            except JobError as error:self.assertEqual(error.status,409);return None
        with ThreadPoolExecutor(max_workers=8) as pool:accepted=[j for j in pool.map(submit,range(16)) if j]
        self.assertEqual(len(accepted),2);self.assertEqual(self.view()['remaining'],{'jobs':0,'cpu_seconds':0,'npu_seconds':0})

    def test_same_task_replay_is_free_after_close_and_conflicting_key_is_refused(self):
        first,_=self.jobs.submit(self.request,self.agent);self.budgets.close(self.alice,self.budget['id'])
        again,reused=self.jobs.submit(self.request,self.agent);self.assertTrue(reused);self.assertEqual(again['id'],first['id'])
        self.assertEqual(self.view()['charged']['jobs'],1)
        with self.assertRaises(JobError):self.jobs.submit({**self.request,'name':'changed'},self.agent)
        with self.assertRaises(JobError):self.jobs.submit({**self.request,'idempotency_key':'new'},self.agent)

    def test_cancellation_and_unknown_execution_do_not_refund(self):
        first,_=self.jobs.submit(self.request,self.agent);self.jobs.cancel(first['id'],self.agent)
        second,_=self.jobs.submit({**self.request,'idempotency_key':'second'},self.agent)
        grant=self.jobs.nodes.claim(self.base.a)['grant'];self.assertEqual(grant['job_id'],second['id'])
        with self.jobs.transaction() as db:
            job=self.jobs.get(second['id'],db);job['state']='needs_attention';self.jobs.write(db,job)
        self.assertEqual(self.view()['charged']['jobs'],2)
        with self.assertRaises(JobError):self.jobs.submit({**self.request,'idempotency_key':'third'},self.agent)

    def test_closed_or_expired_budget_does_not_cancel_preapproved_queue(self):
        job,_=self.jobs.submit(self.request,self.agent);self.budgets.close(self.alice,self.budget['id'])
        self.assertEqual(self.jobs.nodes.claim(self.base.a)['grant']['job_id'],job['id'])
        with self.jobs.transaction() as db:
            value=json.loads(db.execute('SELECT data FROM experiment_budgets WHERE id=?',(self.budget['id'],)).fetchone()[0]);value['expires_at']=0
            db.execute('UPDATE experiment_budgets SET data=? WHERE id=?',(json.dumps(value),self.budget['id']))
        self.assertEqual(self.view()['charged']['jobs'],1)

    def test_preview_reads_budget_without_charge_or_job(self):
        with self.jobs.connect() as db:before=list(db.iterdump())
        self.jobs.preview(self.request,self.agent)
        with self.jobs.connect() as db:self.assertEqual(before,list(db.iterdump()))
        self.assertEqual(self.view()['charged']['jobs'],0)

    def test_job_creation_failure_rolls_back_charge_with_job_transaction(self):
        with patch('jobs.atomic_json',side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):self.jobs.submit(self.request,self.agent)
        self.assertEqual(self.view()['charged']['jobs'],0);self.assertFalse(self.jobs.all())
        self.assertFalse(self.jobs.submit(self.request,self.agent)[1])

    def test_explicit_owner_budget_cannot_be_borrowed_by_peer(self):
        with self.assertRaises(JobError):self.jobs.submit({**self.request,'experiment_id':self.budget['id']},self.bob)
        with self.jobs.connect() as db:
            with self.assertRaises(JobError):self.budgets.get(db,self.budget['id'],self.bob)

    def test_agent_job_visibility_and_cancel_are_limited_to_its_budget(self):
        ordinary,_=self.jobs.submit({**self.request,'idempotency_key':'ordinary'},self.alice)
        bound,_=self.jobs.submit(self.request,self.agent)
        self.assertEqual([j['id'] for j in self.jobs.access.readable_jobs(self.agent,self.jobs.all())],[bound['id']])
        with self.assertRaises(Exception):self.jobs.get(ordinary['id'],principal=self.agent)
        with self.assertRaises(Exception):self.jobs.cancel(ordinary['id'],self.agent)
        self.assertEqual(self.jobs.get(ordinary['id'])['state'],'queued')

    def test_budget_limits_template_and_declared_cpu_and_npu_cost(self):
        value={**self.spec,'idempotency_key':'cpu-tight','limits':{'jobs':2,'cpu_seconds':29,'npu_seconds':0}}
        tight=self.budgets.create(self.alice,value)[0]
        with self.assertRaises(JobError):self.jobs.submit({**self.request,'experiment_id':tight['id']},self.alice)
        with self.jobs.connect() as db:
            spec={'experiment_id':self.budget['id'],'environment':'worker-python','template':'cpu_demo','resources':{'cpu':1,'npu':1},'timeout_seconds':1}
            with self.assertRaises(JobError):self.budgets.check(db,spec,self.alice)
            spec['resources']['npu']=0;spec['template']='ir_v1'
            with self.assertRaises(JobError):self.budgets.check(db,spec,self.alice)
        self.assertFalse(self.jobs.all())

    def test_policy_validation_and_no_downgrade_with_ledger_or_bound_token(self):
        for value in [True,{'unknown':1},{'max_jobs':True},{'max_npu_seconds':-1}]:
            with self.assertRaises(ValueError):policy(value)
        with self.assertRaisesRegex(ValueError,'refusing downgrade'):require_compatible(self.base.runtime)
        self.budgets.close(self.alice,self.budget['id'])
        self.jobs.access.revoke_token(self.token)
        with self.assertRaises(ValueError):require_compatible(self.base.runtime)

    def test_restart_preserves_credential_binding_and_cumulative_charges(self):
        job,_=self.jobs.submit(self.request,self.agent)
        reopened=JobStore(self.base.runtime,self.base.config)
        try:
            agent=reopened.access.authenticate('Bearer '+self.token);self.assertEqual(agent.experiment_id,self.budget['id'])
            replay,reused=reopened.submit(self.request,agent);self.assertTrue(reused);self.assertEqual(replay['id'],job['id'])
            with reopened.connect() as db:self.assertEqual(reopened.experiment_budgets.get(db,self.budget['id'],agent)['charged']['jobs'],1)
        finally:reopened.access.close()

    def test_delegated_credentials_have_finite_history_even_after_revocation(self):
        from auth import AccessError,EXPERIMENT_TOKEN_LIMIT
        for _ in range(EXPERIMENT_TOKEN_LIMIT-1):
            token=self.budgets.delegate(self.alice,self.budget['id'])['token'];self.jobs.access.revoke_token(token)
        with self.assertRaises(AccessError) as error:self.budgets.delegate(self.alice,self.budget['id'])
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.view()['charged']['jobs'],0)

    def test_delegate_respects_audit_backlog_before_creating_a_token(self):
        from audit_retention import Pressure
        with self.jobs.access.connect() as db:before=db.execute('SELECT count(*) FROM tokens').fetchone()[0]
        with patch.object(self.jobs.access,'audit_status',return_value={'blocks_new_work':True}):
            with self.assertRaises(Pressure):self.budgets.delegate(self.alice,self.budget['id'])
        with self.jobs.access.connect() as db:self.assertEqual(before,db.execute('SELECT count(*) FROM tokens').fetchone()[0])

    def test_two_card_declared_cost_consumes_npu_seconds_without_device_execution(self):
        spec={**self.spec,'idempotency_key':'device-arithmetic','limits':{'jobs':2,'cpu_seconds':120,'npu_seconds':60},'templates':['accuracy_v1']}
        budget=self.budgets.create(self.alice,spec)[0]
        candidate={'experiment_id':budget['id'],'environment':'worker-python','template':'accuracy_v1','resources':{'cpu':1,'npu':2},'timeout_seconds':30}
        with self.jobs.transaction() as db:
            charge=self.budgets.charge(db,candidate,self.alice,'job-000000000001')['experiment_charge']
            self.assertEqual(charge['npu_seconds'],60)
            with self.assertRaises(JobError):self.budgets.check(db,candidate,self.alice)
        with self.jobs.connect() as db:self.assertEqual(self.budgets.get(db,budget['id'],self.alice)['remaining']['npu_seconds'],0)
        self.assertFalse(self.jobs.all())  # Arithmetic-only ledger fixture, no worker/grant/device access.

    def test_stale_checkpoint_freezes_budget_and_revokes_delegation(self):
        import sqlite3
        from experiment_budget import rebind_restored
        snapshot=self.base.runtime/'checkpoint';snapshot.mkdir(mode=0o700)
        for name in ['jobs.sqlite3','identities.sqlite3']:
            with sqlite3.connect(self.base.runtime/name) as source,sqlite3.connect(snapshot/name) as target:source.backup(target)
        self.jobs.submit(self.request,self.agent)
        self.assertEqual(self.view()['charged']['jobs'],1)
        rebind_restored(snapshot)
        restored=JobStore(snapshot,self.base.config)
        try:
            self.assertIsNone(restored.access.authenticate('Bearer '+self.token))
            principal=restored.access.user('alice')
            with restored.connect() as db:value=restored.experiment_budgets.get(db,self.budget['id'],principal)
            self.assertEqual(value['state'],'restored_frozen');self.assertEqual(value['charged']['jobs'],0)
            with self.assertRaises(JobError):restored.submit({**self.request,'experiment_id':self.budget['id']},principal)
            with self.assertRaises(JobError):restored.experiment_budgets.delegate(principal,self.budget['id'])
            read=restored.experiment_budgets.delegate(principal,self.budget['id'],read_only=True)
            actor=restored.access.authenticate('Bearer '+read['token']);self.assertEqual(actor.scopes,frozenset({'jobs:read'}))
            with self.assertRaises(Exception):restored.submit(self.request,actor)
        finally:restored.access.close()

    def test_mcp_session_rejects_credential_binding_loss_or_switch(self):
        from mcp_bridge import Bridge,BridgeError
        from lab_client import atomic_json
        path=self.base.runtime/'session.json'
        atomic_json(path,{'run_id':'budget-session','identity':'alice','experiment_id':self.budget['id'],'client_profile':'unused'})
        bridge=Bridge(path)
        for actor in [{'id':'alice'},{'id':'alice','experiment_id':'experiment-'+'f'*24}]:
            with patch('mcp_bridge.Client.from_profile',return_value=Mock(request=Mock(return_value=actor))):
                with self.assertRaises(BridgeError):bridge.api('/api/jobs')

    def test_http_budget_scope_denies_new_budgets_and_preserves_job_replay(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(latest=Mock(return_value=None)),self.base.config,self.jobs))
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        client=Client({'controller':'http://127.0.0.1:'+str(http.server_port),'token':self.token})
        try:
            self.assertEqual(client.request('/api/me')['experiment_id'],self.budget['id'])
            first=client.request('/api/jobs',self.request)['job'];replay=client.request('/api/jobs',self.request)
            self.assertTrue(replay['reused']);self.assertEqual(replay['job']['id'],first['id'])
            with self.assertRaises(ClientHTTPError) as error:client.request('/api/experiment-budgets',self.spec)
            self.assertEqual(error.exception.status,403)
            self.assertEqual(client.request('/api/experiment-budgets')['budgets'][0]['charged']['jobs'],1)
        finally:http.shutdown();http.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()

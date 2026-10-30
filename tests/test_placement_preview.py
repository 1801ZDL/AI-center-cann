"""Preview shares real placement gates but never acquires execution authority."""
import copy
import json
from pathlib import Path
import sys
import time
import threading
import subprocess
import unittest
from unittest.mock import patch,Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_nodes as builder
import test_agent as agent_builder
import test_environment_contract as environment_builder
from jobs import JobError,read_json
from mcp_bridge import BridgeError,tools
from server import ThreadingHTTPServer,handler_class
from lab_client import Client,atomic_json,ClientError


class PlacementPreviewTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.NodeTests();self.base.setUp()
        self.jobs=self.base.jobs;self.alice=self.base.alice;self.a=self.base.a;self.b=self.base.b
        self.request={'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}}

    def tearDown(self):self.base.tearDown()

    def preview(self):return self.jobs.preview(self.request,self.alice)

    def dump(self):
        with self.jobs.connect() as db:return list(db.iterdump())

    def profile(self,node,change):
        value=self.jobs.nodes.node(node)['profile'];change(value)
        with self.jobs.transaction() as db:db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(value),node))

    def test_ready_preview_and_real_claim_agree_without_writing_or_creating_files(self):
        before=self.dump();files=sorted(str(p.relative_to(self.jobs.root)) for p in self.jobs.root.rglob('*'))
        with patch.object(self.jobs,'write',side_effect=AssertionError('preview wrote job')):
            result=self.preview()
        self.assertEqual(result['eligible_count'],2);self.assertEqual(result['status'],'eligible_snapshot')
        self.assertFalse(result['reservation_created']);self.assertFalse(result['start_guaranteed'])
        self.assertEqual(self.dump(),before)
        self.assertEqual(sorted(str(p.relative_to(self.jobs.root)) for p in self.jobs.root.rglob('*')),files)
        job=self.jobs.submit(self.request,self.alice)[0]
        self.assertEqual(self.jobs.nodes.claim(self.a)['grant']['job_id'],job['id'])

    def test_full_physical_domain_is_waiting_and_preserves_original_attempt(self):
        old=self.base.submit();grant=self.jobs.nodes.claim(self.a)['grant'];before=self.dump()
        result=self.preview()
        self.assertEqual(result['status'],'blocked')
        self.assertTrue(all(item['capacity_fits'] for item in result['candidates']))
        self.assertTrue(all(item['reason'] for item in result['candidates']))
        self.assertEqual(self.dump(),before)
        self.assertEqual(self.jobs.get(old['id'])['attempt_id'],grant['attempt_id'])
        queued=self.jobs.submit(self.request,self.alice)[0]
        self.assertEqual(queued['state'],'queued');self.assertEqual(self.jobs.attempts(queued['id']),[])

    def test_node_memory_includes_managed_overhead(self):
        self.profile(self.a,lambda p:p['policy'].update(memory_mib=256))
        result=self.preview();row=next(item for item in result['candidates'] if item['node_id']==self.a)
        self.assertEqual(row['reason_code'],'capacity_exceeded');self.assertFalse(row['capacity_fits'])
        self.assertEqual(result['eligible_count'],1)

    def test_domain_ceiling_is_checked_without_treating_active_usage_as_capacity(self):
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        policy={**domain['policy'],'memory_mib':256,'shm_mib':0}
        self.jobs.resources.configure(domain['physical_id'],policy,domain['revision'])
        result=self.preview()
        self.assertTrue(all(row['reason_code']=='capacity_exceeded' for row in result['candidates']))

    def test_nonintersecting_cpu_sets_are_not_reported_available(self):
        cpu=max(self.jobs.policy['cpu_ids'])+1
        self.profile(self.a,lambda p:p['policy'].update(cpu_ids=[cpu]))
        row=next(item for item in self.preview()['candidates'] if item['node_id']==self.a)
        self.assertEqual(row['reason_code'],'capacity_exceeded')

    def test_stale_and_future_heartbeats_are_unobserved(self):
        for seen in [0,time.time()+300]:
            with self.jobs.transaction() as db:db.execute('UPDATE worker_nodes SET last_seen=?',(seen,))
            self.assertTrue(all(row['reason_code']=='heartbeat_unavailable' for row in self.preview()['candidates']))

    def test_unmapped_node_does_not_leak_uid_paths_or_other_owner(self):
        self.profile(self.b,lambda p:p['users'].pop('alice'))
        result=self.preview();self.assertEqual(result['candidate_count'],1)
        text=json.dumps(result);self.assertNotIn(self.b,text);self.assertNotIn('bob',text)
        self.assertNotIn('23001',text);self.assertNotIn(str(self.base.runtime),text)

    def test_no_visible_node_does_not_reject_intentional_future_queue(self):
        for node in [self.a,self.b]:self.profile(node,lambda p:p['users'].pop('alice'))
        self.assertEqual(self.preview()['status'],'no_visible_node')
        job=self.jobs.submit(self.request,self.alice)[0]
        self.assertEqual(job['state'],'queued')

    def test_unregistered_template_and_disabled_node_have_explicit_reasons(self):
        self.profile(self.a,lambda p:p.update(templates=['isolation_probe']))
        with self.jobs.transaction() as db:db.execute('UPDATE worker_nodes SET disabled=1 WHERE id=?',(self.b,))
        self.assertEqual({row['reason_code'] for row in self.preview()['candidates']},{'template_unavailable','node_disabled'})

    def test_maintenance_drain_and_inventory_gate_still_apply(self):
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        with self.jobs.transaction() as db:db.execute('UPDATE resource_domains SET draining=1 WHERE physical_id=?',(domain['physical_id'],))
        self.assertEqual(self.preview()['status'],'blocked')
        self.assertTrue(all('排空' in row['reason'] for row in self.preview()['candidates']))
        with patch.object(self.jobs.inventory,'reason',return_value='unknown inventory hold'):
            self.assertTrue(all(row['reason']=='unknown inventory hold' for row in self.preview()['candidates']))

    def test_revoked_user_is_not_reported_ready(self):
        self.jobs.access.disable_user('alice')
        self.assertEqual(self.preview()['eligible_count'],0)

    def test_arbitrary_owner_and_command_overrides_are_rejected_before_preview(self):
        self.request['owner']='bob'
        with self.assertRaises(JobError):self.preview()

    def test_preview_query_connection_cannot_mutate(self):
        def accidentally_write(job,node,domain,active,db,identities=None):
            db.execute('DELETE FROM jobs')
        import sqlite3
        with patch.object(self.jobs.nodes,'placement',side_effect=accidentally_write):
            with self.assertRaises(sqlite3.OperationalError):self.preview()

    def test_authenticated_http_cli_and_session_mcp_preview_without_submission(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(latest=Mock(return_value=None)),self.base.config,self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}'
            token=self.jobs.access.issue_token('alice',{'jobs:read','jobs:submit'})
            profile=self.base.runtime/'client.json'
            atomic_json(profile,{'controller':url,'token':token,'default_environment':'worker-python'})
            client=Client.from_profile(profile);before=self.dump()
            result=client.request('/api/jobs/preview',self.request)
            self.assertEqual(result['eligible_count'],2);self.assertEqual(self.dump(),before)
            read_only=Client({'controller':url,'token':self.jobs.access.issue_token('alice',{'jobs:read'})})
            with self.assertRaises(ClientError):read_only.request('/api/jobs/preview',self.request)
            command=[sys.executable,str(ROOT/'labctl'),'--profile',str(profile),'submit','--preview','--template','cpu_demo']
            completed=subprocess.run(command,text=True,capture_output=True,timeout=15)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual(json.loads(completed.stdout)['eligible_count'],2)
            rejected=subprocess.run(command+['--archive'],text=True,capture_output=True,timeout=15)
            self.assertNotEqual(rejected.returncode,0)
            self.assertEqual(self.jobs.all(),[])
            session=self.base.runtime/'session';session.mkdir()
            atomic_json(session/'session.json',{'run_id':'preview-http','environment':'worker-python','seconds':1,
                'execution_mode':'templates','model':'not-called','identity':'alice','client_profile':str(profile)})
            from mcp_bridge import Bridge
            bridge=Bridge(session/'session.json')
            self.assertEqual(bridge.call('preview_job',{'template':'cpu_demo','request_key':'same'})['eligible_count'],2)
            self.assertFalse((session/'submitted-request.json').exists());self.assertEqual(self.jobs.all(),[])
            # Preview never consumes a job key; ordinary submission/claim still works.
            actual=client.request('/api/jobs',{**self.request,'idempotency_key':'after-preview'})['job']
            self.assertEqual(self.jobs.nodes.claim(self.a)['grant']['job_id'],actual['id'])
        finally:
            server.shutdown();server.server_close();thread.join()


class EnvironmentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.base=environment_builder.AdmissionTests();self.base.setUp();self.jobs=self.base.jobs
        job=self.base.workflow({})
        self.request={'template':job['template'],'parameters':job['parameters'],'environment':job['environment']}

    def tearDown(self):self.base.tearDown()

    def preview(self):return self.jobs.preview(self.request,self.base.alice)

    def test_uncertified_environment_waits_without_new_job_or_reservation(self):
        before=self.jobs.all();result=self.preview()
        self.assertEqual(result['status'],'blocked');self.assertIn('认证',result['candidates'][0]['reason'])
        self.assertEqual(self.jobs.all(),before)

    def test_certified_environment_matches_actual_workflow_placement(self):
        self.base.install_manifest();self.assertEqual(self.preview()['eligible_count'],1)
        self.assertIsNotNone(self.jobs.nodes.claim(self.base.node)['grant'])

    def test_different_lock_is_not_treated_as_busy_matching_environment(self):
        self.base.install_manifest();self.request=copy.deepcopy(self.request)
        self.request['parameters']['lock']['python']='3.11.0'
        row=self.preview()['candidates'][0]
        self.assertTrue(row['capacity_fits']);self.assertIn('锁定',row['reason'])
        self.assertEqual(row['status'],'blocked')

    def test_fixed_unknown_probe_target_fails_without_creating_job(self):
        before=self.jobs.all()
        with self.assertRaises(JobError):
            self.jobs.preview({'template':'environment_probe','environment':'worker-python',
                'parameters':{'node_id':'worker-'+'f'*12}},self.base.alice)
        self.assertEqual(self.jobs.all(),before)


class AgentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.base=agent_builder.AgentBoundaryTests();self.base.setUp();self.bridge=self.base.bridge

    def tearDown(self):self.base.tearDown()

    def test_preview_is_readonly_and_does_not_consume_single_task_intent(self):
        calls=[];original=self.bridge.api.side_effect
        def api(path,data=None,raw=False):
            if path=='/api/jobs/preview':calls.append(copy.deepcopy(data));return {'start_guaranteed':False}
            return original(path,data,raw)
        self.bridge.api.side_effect=api
        args={'template':'cpu_demo','request_key':'stable'}
        self.bridge.call('preview_job',args);self.bridge.call('preview_job',args)
        self.assertFalse((self.base.directory/'submitted-request.json').exists())
        self.assertEqual(read_json(self.bridge.state_path),{'jobs':[]})
        self.bridge.call('submit_job',args)
        self.assertEqual(calls,[self.base.submissions[0],self.base.submissions[0]])
        tool=next(item for item in tools(self.bridge.session) if item['name']=='preview_job')
        self.assertTrue(tool['annotations']['readOnlyHint']);self.assertTrue(tool['annotations']['idempotentHint'])

    def test_preview_cannot_broaden_session_authority(self):
        for changes in [{'argv':['sh']},{'npu':1},{'seconds':True},{'environment':'host-python'},{'template':'shell'}]:
            with self.subTest(changes=changes),self.assertRaises(BridgeError):
                self.bridge.call('preview_job',{'template':'cpu_demo','request_key':'stable',**changes})
        self.bridge.api.assert_not_called()

    def test_bound_workflow_preview_uses_exact_input_lock_and_resources(self):
        fixed={'template':'ir_v1','environment':'worker-python','parameters':{'input_id':'input-'+'a'*24,
            'lock':{'python':'3.10.12'},'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60}}
        self.bridge.session.update(environment='worker-python',workflow_request=copy.deepcopy(fixed))
        self.bridge.api.return_value={};self.bridge.api.side_effect=None
        self.bridge.call('preview_job',{'template':'ir_v1','request_key':'bound'})
        path,body=self.bridge.api.call_args.args
        self.assertEqual(path,'/api/jobs/preview');self.assertEqual(body['parameters'],fixed['parameters'])
        self.assertFalse((self.base.directory/'submitted-request.json').exists())
        with self.assertRaises(BridgeError):self.bridge.call('preview_job',{'template':'ir_v1','request_key':'bound','seconds':1})


if __name__=='__main__':unittest.main()

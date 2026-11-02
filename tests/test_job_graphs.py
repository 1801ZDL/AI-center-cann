"""Whole-graph accounting, no partial execution, frozen retry and ACL boundaries."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_dependencies as builder
from auth import AccessError
from job_graphs import validate,CAPABILITY
from jobs import JobStore,JobError,atomic_json
from maintenance import require_features,assert_quiescent
from deployment import DeployError
from server import ThreadingHTTPServer,handler_class
from lab_client import Client,ClientHTTPError,ClientUnavailable
from graph_client import Bridge,serve


class Crash(BaseException):pass


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DependencyTests();self.base.setUp();self.jobs=self.base.jobs
        self.jobs.access.set_quota('alice',{'max_queued':32});self.alice=self.jobs.access.user('alice')

    def tearDown(self):self.base.tearDown()

    def request(self,key='graph-one',count=3):
        nodes=[]
        for i in range(count):
            body=self.base.request();body['parameters'].update(resources={'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},
                timeout_seconds=30,artifacts={'file_mib':1,'total_mib':2})
            node={'key':'stage'+str(i),'job':body}
            if i:
                node.update(after=['stage'+str(i-1)],artifact_inputs=[{'node':'stage'+str(i-1),'name':'results/package.bin','target':'stage'+str(i-1)+'.bin'}])
            nodes.append(node)
        return {'idempotency_key':key,'nodes':nodes}

    def submit(self,body=None,principal=None):return self.jobs.graphs.submit(body or self.request(),principal or self.alice)

    def counts(self):
        with self.jobs.connect() as db:
            return {name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['jobs','job_graphs','experiment_charges']}|{
                'archives':db.execute("SELECT count(*) FROM storage_allocations WHERE kind='archive'").fetchone()[0]}

    def pending(self,stage='admission_committed'):
        def stop(where):
            if where==stage:raise Crash()
        with patch('job_graphs._checkpoint',side_effect=stop),self.assertRaises(Crash):self.submit()
        with self.jobs.connect() as db:return db.execute('SELECT id FROM job_graphs').fetchone()[0]

    def test_one_transaction_maps_local_edges_and_preserves_ordinary_queue(self):
        graph,reused=self.submit();self.assertFalse(reused);self.assertEqual(graph['state'],'submitted')
        mapping={n['key']:n['job_id'] for n in graph['nodes']};self.assertEqual(len(set(mapping.values())),3)
        child=self.jobs.get(mapping['stage1']);self.assertEqual(child['depends_on'],[mapping['stage0']])
        self.assertEqual(child['artifact_inputs'][0]['job_id'],mapping['stage0'])
        self.assertEqual(child['parameters']['input_id'],self.base.base['id'])
        self.assertEqual(self.counts(),{'jobs':3,'job_graphs':1,'experiment_charges':0,'archives':3})
        for job in self.jobs.all():
            self.assertNotIn('_graph_payload',job);self.assertEqual(job['state'],'queued')
            self.assertTrue((self.jobs.directory(job['id'])/'request.json').is_file())
            self.assertIsNone(self.jobs.graphs.reason(job))
        grant=self.jobs.nodes.claim(self.base.node)['grant'];self.assertEqual(grant['job_id'],mapping['stage0'])
        self.assertEqual(self.jobs.attempts(mapping['stage1']),[])

    def test_bad_last_node_rolls_back_every_job_reservation_and_directory(self):
        body=self.request();body['nodes'][-1]['job']['parameters']['input_id']='input-'+'f'*24
        with self.assertRaises(JobError):self.submit(body)
        self.assertEqual(self.counts(),dict.fromkeys(['jobs','job_graphs','experiment_charges','archives'],0))
        self.assertEqual(list(self.jobs.root.iterdir()),[])

    def test_late_queue_limit_failure_is_atomic(self):
        self.jobs.access.set_quota('alice',{'max_queued':2});self.alice=self.jobs.access.user('alice')
        with self.assertRaises(JobError):self.submit()
        self.assertEqual(self.counts()['jobs'],0);self.assertEqual(self.counts()['archives'],0)
        self.assertEqual(list(self.jobs.root.iterdir()),[])

    def test_late_archive_limit_failure_is_atomic(self):
        self.jobs.access.set_quota('alice',{'archive_storage_mib':29});self.alice=self.jobs.access.user('alice')
        with self.assertRaisesRegex(JobError,'archive storage budget'):self.submit()
        self.assertEqual(self.counts()['jobs'],0);self.assertEqual(self.counts()['archives'],0)
        self.assertEqual(list(self.jobs.root.iterdir()),[])

    def budget(self,jobs=3,key='graph-budget'):
        return self.jobs.experiment_budgets.create(self.alice,{'name':'Graph budget','idempotency_key':key,
            'limits':{'jobs':jobs,'cpu_seconds':jobs*30,'npu_seconds':0},'templates':['ir_v1'],'lifetime_seconds':3600})[0]

    def test_budget_charge_is_all_or_none_and_replay_is_free(self):
        budget=self.budget(2);body=self.request()
        for node in body['nodes']:node['job']['experiment_id']=budget['id']
        with self.assertRaises(JobError):self.submit(body)
        self.assertEqual(self.counts()['experiment_charges'],0);self.assertEqual(self.counts()['jobs'],0)
        body['nodes'].pop();graph,_=self.submit(body);before=self.counts()
        same,reused=self.submit(body);self.assertTrue(reused);self.assertEqual(same,graph);self.assertEqual(self.counts(),before)
        with self.jobs.connect() as db:self.assertEqual(self.jobs.experiment_budgets.get(db,budget['id'],self.alice)['charged'],{'jobs':2,'cpu_seconds':60,'npu_seconds':0})

    def test_mixed_budget_graph_rolls_back_and_bound_token_cannot_read_another(self):
        one=self.budget();two=self.budget(key='budget-two');body=self.request()
        body['nodes'][0]['job']['experiment_id']=one['id'];body['nodes'][1]['job']['experiment_id']=two['id']
        with self.assertRaises(JobError):self.submit(body)
        self.assertEqual(self.counts()['experiment_charges'],0)
        graph,_=self.submit();token=self.jobs.experiment_budgets.delegate(self.alice,one['id'])['token']
        agent=self.jobs.access.authenticate('Bearer '+token)
        with self.assertRaises(JobError) as error:self.jobs.graphs.get(graph['id'],agent)
        self.assertEqual(error.exception.status,404)
        bound,_=self.submit(self.request(key='bound'),agent)
        self.assertEqual(bound['experiment_id'],one['id'])

    def test_concurrent_replay_returns_one_graph_and_original_tasks(self):
        body=self.request()
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.submit(body),range(4)))
        self.assertEqual(len({g['id'] for g,_ in results}),1);self.assertEqual(sum(not reused for _,reused in results),1)
        self.assertEqual(self.counts()['jobs'],3);self.assertEqual(self.counts()['archives'],3)

    def test_reordered_nodes_replay_but_changed_content_conflicts(self):
        body=self.request();graph,_=self.submit(body);body['nodes'].reverse()
        same,reused=self.submit(body);self.assertTrue(reused);self.assertEqual(same['id'],graph['id'])
        body['nodes'][0]['job']['name']='Changed'
        with self.assertRaises(JobError) as error:self.submit(body)
        self.assertEqual(error.exception.status,409);self.assertEqual(self.counts()['jobs'],3)

    def test_failure_before_commit_has_no_files_or_records(self):
        def crash(stage):
            if stage=='admitted:stage1':raise Crash()
        with patch('job_graphs._checkpoint',side_effect=crash),self.assertRaises(Crash):self.submit()
        self.assertEqual(self.counts()['jobs'],0);self.assertEqual(self.counts()['job_graphs'],0)
        self.assertEqual(list(self.jobs.root.iterdir()),[])

    def test_after_commit_reopen_resumes_same_ids_and_never_starts_partial_graph(self):
        ident=self.pending();before=self.jobs.graphs.get(ident,self.alice);ids={n['job_id'] for n in before['nodes']}
        self.assertEqual(before['state'],'preparing');self.assertEqual(list(self.jobs.root.iterdir()),[])
        self.assertIsNone(self.jobs.nodes.claim(self.base.node)['grant'])
        self.assertTrue(all(not self.jobs.attempts(i) for i in ids))
        with self.assertRaises(JobError):self.jobs.cancel(next(iter(ids)),self.alice)
        self.jobs=JobStore(self.jobs.runtime,self.base.config);self.jobs.graphs.recover()
        after=self.jobs.graphs.get(ident,self.alice);self.assertEqual(after['state'],'submitted')
        self.assertEqual({n['job_id'] for n in after['nodes']},ids);self.assertEqual(self.counts()['archives'],3)
        self.assertIsNotNone(self.jobs.nodes.claim(self.base.node)['grant'])

    def test_partial_archive_and_prepublication_crashes_preserve_bytes_and_original_ids(self):
        ident=self.pending('archive:stage0');before=self.jobs.graphs.get(ident,self.alice)
        first=before['nodes'][0]['job_id'];path=self.jobs.directory(first)/'request.json';raw=path.read_bytes()
        self.assertEqual(len([p for p in self.jobs.root.iterdir() if p.is_dir()]),1)
        def crash(stage):
            if stage=='before_publish':raise Crash()
        with patch('job_graphs._checkpoint',side_effect=crash),self.assertRaises(Crash):self.jobs.graphs.materialize(ident)
        self.assertEqual(self.jobs.graphs.get(ident,self.alice)['state'],'preparing')
        self.assertIsNone(self.jobs.nodes.claim(self.base.node)['grant'])
        self.jobs.graphs.resume(ident,self.alice);self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.counts()['jobs'],3)

    def test_lost_response_after_publication_never_replaces_a_started_attempt(self):
        def crash(stage):
            if stage=='published':raise Crash()
        with patch('job_graphs._checkpoint',side_effect=crash),self.assertRaises(Crash):self.submit()
        before=self.jobs.all();grant=self.jobs.nodes.claim(self.base.node)['grant']
        graph,reused=self.submit();self.assertTrue(reused)
        self.assertEqual({v['job_id'] for v in graph['nodes']},{j['id'] for j in before})
        self.assertEqual(self.jobs.nodes.claim(self.base.node)['grant'],grant)
        self.assertEqual(len(self.jobs.attempts(grant['job_id'])),1);self.assertEqual(self.counts()['jobs'],3)

    def test_conflicting_archive_is_never_overwritten_and_recovery_is_bounded(self):
        ident=self.pending('archive:stage0');graph=self.jobs.graphs.get(ident,self.alice)
        path=self.jobs.directory(graph['nodes'][0]['job_id'])/'request.json';path.write_bytes(b'conflicting bytes')
        for _ in range(32):self.jobs.graphs.materialize(ident,force=True)
        after=self.jobs.graphs.get(ident,self.alice);self.assertEqual(after['state'],'needs_attention')
        self.assertEqual(after['preparation_failures'],32);self.assertEqual(path.read_bytes(),b'conflicting bytes')
        self.assertIsNone(self.jobs.nodes.claim(self.base.node)['grant']);self.jobs.graphs.recover()
        self.assertEqual(self.jobs.graphs.get(ident,self.alice)['preparation_failures'],32)

    def test_file_failure_keeps_all_reservations_then_explicit_resume_recovers(self):
        with patch('job_graphs.write_once',side_effect=OSError('fixture')):graph,_=self.submit()
        self.assertEqual(graph['state'],'preparing');self.assertEqual(self.counts()['archives'],3)
        self.assertIsNone(self.jobs.nodes.claim(self.base.node)['grant'])
        graph=self.jobs.graphs.resume(graph['id'],self.alice);self.assertEqual(graph['state'],'submitted')

    def test_private_snapshots_are_not_in_public_job_or_graph_and_foreign_read_is_404(self):
        ident=self.pending();value=self.jobs.graphs.get(ident,self.alice)
        self.assertNotIn('_graph_payload',json.dumps(value));self.assertNotIn('base64',json.dumps(value))
        with self.assertRaises(JobError) as error:self.jobs.graphs.get(ident,self.base.bob)
        self.assertEqual(error.exception.status,404)
        readonly=replace(self.alice,scopes=frozenset({'jobs:read'}))
        self.assertEqual(self.jobs.graphs.get(ident,readonly)['id'],ident)
        with self.assertRaises(AccessError):self.jobs.graphs.resume(ident,readonly)
        with self.assertRaises(AccessError):self.submit(principal=readonly)

    def test_old_code_and_backup_cannot_discard_pending_graph(self):
        self.pending()
        with self.assertRaisesRegex(DeployError,'task graphs'):require_features(self.jobs.runtime,'controller',{'files':{}})
        with self.assertRaises(DeployError):assert_quiescent(self.jobs.runtime,'controller')

    def test_restore_cannot_forget_graph_keys_or_original_stage_records(self):
        from job_graphs import require_restore_history
        self.submit();stage=self.jobs.runtime/'snapshot';stage.mkdir()
        with self.jobs.connect() as source,sqlite3.connect(stage/'jobs.sqlite3') as target:source.backup(target)
        require_restore_history(self.jobs.runtime,stage,'controller')
        with sqlite3.connect(stage/'jobs.sqlite3') as db:db.execute('DELETE FROM job_graphs')
        with self.assertRaisesRegex(DeployError,'graph admission'):require_restore_history(self.jobs.runtime,stage,'controller')
        with self.jobs.connect() as source,sqlite3.connect(stage/'jobs.sqlite3') as target:source.backup(target)
        with sqlite3.connect(stage/'jobs.sqlite3') as db:db.execute("UPDATE jobs SET fingerprint='changed'")
        with self.assertRaisesRegex(DeployError,'original attempts'):require_restore_history(self.jobs.runtime,stage,'controller')

    def test_schema_cycles_missing_parents_duplicate_keys_resources_and_overrides(self):
        body=self.request();bad=[]
        value=copy.deepcopy(body);value['nodes'][0]['after']=['stage2'];bad.append(value)
        value=copy.deepcopy(body);value['nodes'][0]['after']=['missing'];bad.append(value)
        value=copy.deepcopy(body);value['nodes'][1]['key']='stage0';bad.append(value)
        value=copy.deepcopy(body);value['nodes'][0]['job']['idempotency_key']='chosen';bad.append(value)
        value=copy.deepcopy(body);value['nodes'][0]['job']={'argv':['sh'],'environment':'host-python'};bad.append(value)
        value=copy.deepcopy(body);value['nodes'][1]['artifact_inputs'][0]['node']='missing';bad.append(value)
        value=copy.deepcopy(body);value['nodes'][0]['job']['parameters']['resources']['npu']=True;bad.append(value)
        value=copy.deepcopy(body);value['nodes'][0]['job']['depends_on']=None;bad.append(value)
        for value in bad:
            with self.subTest(value=value),self.assertRaises(JobError):self.submit(value)
        self.assertEqual(self.counts()['jobs'],0)

    def test_size_and_count_are_bounded(self):
        with self.assertRaises(JobError):validate(self.request(count=17))
        body=self.request();body['nodes'][0]['job']['name']='x'*32768
        with self.assertRaises(JobError):validate(body)

    def test_http_submit_replay_owner_acl_and_resume_overrides(self):
        token=self.jobs.access.issue_token('alice',['jobs:read','jobs:submit'])
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),self.base.config,self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=Client({'controller':'http://127.0.0.1:'+str(server.server_port),'token':token})
            first=client.request('/api/job-graphs',self.request());ident=first['graph']['id']
            same=client.request('/api/job-graphs',self.request());self.assertTrue(same['reused']);self.assertEqual(same['graph']['id'],ident)
            self.assertEqual(client.request('/api/job-graphs/'+ident)['graph']['id'],ident)
            with self.assertRaises(ClientHTTPError):client.request('/api/job-graphs/'+ident+'/resume',{'nodes':[]})
            other=Client({'controller':client.url,'token':self.jobs.access.issue_token('bob',['jobs:read'])})
            with self.assertRaises(ClientHTTPError) as error:other.request('/api/job-graphs/'+ident)
            self.assertEqual(error.exception.status,404)
            self.assertIn(CAPABILITY,client.request('/api/me')['job_graph_capabilities'])
        finally:server.shutdown();server.server_close();thread.join(timeout=3)


class GraphClientTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='graph-client-');self.root=Path(self.temp.name)
        self.root.chmod(0o700);self.request=self.root/'approved.json';atomic_json(self.request,{'idempotency_key':'approved','nodes':[]})
        self.state=self.root/'state.json';self.client=Mock();self.client.url='http://127.0.0.1:18765'
        self.identifier='graph-'+'a'*24;self.client.request.return_value={'graph':{'id':self.identifier},'reused':False}

    def tearDown(self):self.temp.cleanup()

    def bridge(self):return Bridge(self.client,self.request,self.state)

    def test_post_intent_is_saved_and_unknown_response_reuses_exact_request(self):
        self.client.request.side_effect=ClientUnavailable('fixture')
        with self.assertRaises(ClientUnavailable):self.bridge().call('submit_graph',{})
        value=json.loads(self.state.read_text());self.assertTrue(value['submission_intent']);self.assertIsNone(value['graph_id'])
        self.assertEqual(self.bridge().call('graph_status',{})['status'],'submission_unknown')
        self.client.request.side_effect=None;result=self.bridge().call('submit_graph',{})
        self.assertEqual(result['graph']['id'],self.identifier)
        requests=[call.args[1] for call in self.client.request.call_args_list if call.args[0]=='/api/job-graphs']
        self.assertEqual(requests[0],requests[1]);self.assertEqual(json.loads(self.state.read_text())['graph_id'],self.identifier)

    def test_changed_approval_controller_state_or_tool_arguments_are_refused(self):
        bridge=self.bridge();bridge.call('submit_graph',{})
        with self.assertRaises(ValueError):bridge.call('submit_graph',{'argv':['sh']})
        self.client.url='http://127.0.0.1:9999'
        with self.assertRaises(ValueError):self.bridge().call('graph_status',{})
        self.client.url='http://127.0.0.1:18765';atomic_json(self.request,{'idempotency_key':'different'})
        with self.assertRaises(ValueError):bridge.call('submit_graph',{})
        with self.assertRaises(ValueError):self.bridge().call('graph_status',{})

    def test_permission_and_symlink_guard(self):
        self.request.chmod(0o644)
        with self.assertRaises(ValueError):self.bridge()
        self.request.chmod(0o600);link=self.root/'link';link.symlink_to(self.request)
        with self.assertRaises(OSError):Bridge(self.client,link,self.state)

    def test_mcp_tools_are_bound_and_have_no_argument_schema(self):
        incoming=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_graph','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_graph','arguments':{'job':'other'}}}]
        out=io.StringIO();serve(self.bridge(),io.StringIO(''.join(json.dumps(v)+'\n' for v in incoming)),out)
        values=[json.loads(line) for line in out.getvalue().splitlines()]
        tools=values[0]['result']['tools'];self.assertEqual({t['name'] for t in tools},{'submit_graph','graph_status','resume_graph'})
        self.assertTrue(all(t['inputSchema']['properties']=={} for t in tools))
        self.assertFalse(values[1]['result']['isError']);self.assertTrue(values[2]['result']['isError'])


if __name__=='__main__':unittest.main()

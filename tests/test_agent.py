"""Offline checks for the Agent boundary; no model calls or worker execution."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runner import effective_permissions, parse_jsonc
from jobs import ROOT, atomic_json, read_json
from mcp_bridge import Bridge, BridgeError, serve,tools


class AgentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'runtime', prefix='test-agent-')
        self.directory = Path(self.temp.name)
        atomic_json(self.directory/'session.json', {'run_id':'agent-test', 'environment':'zdl-python',
                    'seconds':2, 'model':'test-provider/test-model'})
        self.bridge = Bridge(self.directory/'session.json')
        self.job_id = 'job-012345abcdef'
        self.submissions = []
        def api(path, data=None, raw=False):
            if path == '/api/jobs' and data is not None:
                self.submissions.append(data)
                return {'job':{'id':self.job_id}, 'reused':len(self.submissions)>1}
            if path == '/api/jobs':
                return {'policy':{}, 'reserved':{}, 'environments':[], 'jobs':[{'private':'unrelated'}]}
            if path == '/api/jobs/'+self.job_id:
                return {'job':{'id':self.job_id,'state':'succeeded','result':{'exit_code':0}}}
            if path.endswith('/artifact?name=results%2Fdemo-result.json'):
                return b'{"passed":true,"npu_used":false}'
            raise AssertionError('Unexpected API path')
        self.bridge.api = Mock(side_effect=api)

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_status_exposes_control_admission_without_other_users_jobs(self):
        state={'enabled':True,'status':'pressure','blocks_new_work':True,'reason_code':'control_storage_bytes'}
        self.bridge.api=Mock(return_value={'policy':{},'reserved':{},'environments':[],
                                          'control_storage':state,'jobs':[{'private':'unrelated'}]})
        result=self.bridge.call('queue_status',{})
        self.assertEqual(result['control_storage'],state)
        self.assertNotIn('unrelated',json.dumps(result));self.assertNotIn('jobs',result)
        self.bridge.api.assert_called_once_with('/api/jobs')

    def submit(self, **kwargs):
        return self.bridge.call('submit_job', {'template':'cpu_demo','request_key':'stable', **kwargs})

    def test_unknown_execution_is_not_terminal_and_does_not_allow_duplicate_submission(self):
        self.submit();unknown={'id':self.job_id,'state':'needs_attention'}
        with patch.object(self.bridge,'state',return_value=unknown),patch('mcp_bridge.time.monotonic',side_effect=[0,2]):
            result=self.bridge.call('wait_job',{'job_id':self.job_id,'timeout_seconds':1})
        self.assertFalse(result['terminal']);self.assertTrue(result['needs_attention'])
        self.assertIn('remain reserved',result['note']);self.assertEqual(len(self.submissions),1)

    def test_workflow_attention_does_not_fetch_a_final_result_artifact(self):
        self.submit();self.bridge.session['workflow_request']={'template':'ir_v1','parameters':{'result_file':'results/result.json'}}
        self.bridge.api.reset_mock()
        with patch.object(self.bridge,'state',return_value={'id':self.job_id,'state':'needs_attention'}):
            result=self.bridge.call('get_result',{'job_id':self.job_id})
        self.assertIsNone(result['artifact']);self.bridge.api.assert_not_called()

    def test_personal_archive_path_is_fixed_by_session_not_model_arguments(self):
        self.submit();self.bridge.session['personal_archive']=str(self.directory/'approved')
        for arguments in [{'job_id':self.job_id,'archive_root':'/unapproved'},
                          {'job_id':self.job_id,'local_source':'/unapproved'}]:
            with self.assertRaisesRegex(BridgeError,'Unexpected or missing tool arguments'):
                self.bridge.call('get_result',arguments)

    def test_failed_job_keeps_failure_status_and_can_archive_logs(self):
        self.submit();self.bridge.session['personal_archive']=str(self.directory/'approved')
        failed={'id':self.job_id,'state':'failed','result':{'exit_code':1}}
        with patch.object(self.bridge,'state',return_value=failed),patch.object(self.bridge,'archive_result',side_effect=lambda value,identifier:{**value,'download_requested':identifier}):
            result=self.bridge.call('get_result',{'job_id':self.job_id})
        self.assertEqual(result['job']['state'],'failed');self.assertIsNone(result['artifact'])
        self.assertEqual(result['download_requested'],self.job_id)
        definition=next(item for item in tools(self.bridge.session) if item['name']=='get_result')
        self.assertFalse(definition['annotations']['readOnlyHint'])

    def test_only_bounded_template_can_reach_the_scheduler(self):
        for args in [{'argv':['sh']}, {'npu':1}, {'template':'shell'}, {'environment':'host-python'},
                     {'seconds':True}, {'seconds':11}, {'request_key':'../bad'}]:
            with self.subTest(args=args), self.assertRaises(BridgeError):
                self.submit(**args)
        self.bridge.api.assert_not_called()
        job = self.submit()['job']
        body = self.submissions[0]
        self.assertEqual(body['argv'], ['python','-u','examples/cpu_task.py','--seconds','2'])
        self.assertEqual(body['npu'], 0)
        self.assertEqual(body['origin']['agent_run_id'], 'agent-test')
        self.assertEqual(job['id'], self.job_id)

    def test_retries_preserve_request_and_different_second_job_is_rejected(self):
        self.submit()
        self.assertTrue(self.submit()['reused'])
        self.assertEqual(self.submissions[0], self.submissions[1])
        for changes in [{'request_key':'new'}, {'seconds':3}]:
            with self.assertRaises(BridgeError):self.submit(**changes)
        self.assertEqual(len(self.submissions), 2)
        self.assertEqual(read_json(self.bridge.state_path)['jobs'], [self.job_id])

    def test_logical_user_uses_a_server_enforced_template_request(self):
        self.bridge.session['execution_mode']='templates'
        self.submit()
        body=self.submissions[0]
        self.assertEqual(body['template'],'cpu_demo')
        self.assertEqual(body['parameters'],{'seconds':2})
        self.assertNotIn('argv',body)
        self.assertNotIn('cwd',body)

    def test_lost_submission_response_retries_the_same_idempotency_key(self):
        original = self.bridge.api.side_effect
        self.bridge.api.side_effect = BridgeError('response lost')
        with self.assertRaises(BridgeError):self.submit()
        saved = read_json(self.directory/'submitted-request.json')
        self.bridge.api.side_effect = original
        self.submit()
        self.assertEqual(self.submissions[0], saved)

    def test_bound_workflow_cannot_be_changed_into_another_command_or_resource_request(self):
        fixed={'template':'ir_v1','environment':'worker-python','parameters':{
            'input_id':'input-'+'a'*24,'argv':['python','ir.py'],'lock':{'python':'3.10.12'},
            'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60,'result_file':'results/workload-result.json'}}
        self.bridge.session.update(workflow_request=fixed,environment='worker-python')
        for changes in [{'argv':['sh']},{'npu':1},{'seconds':2},{'template':'build_v1'},{'environment':'host-python'}]:
            with self.subTest(changes=changes),self.assertRaises(BridgeError):
                self.bridge.call('submit_job',{'template':'ir_v1','request_key':'bound',**changes})
        self.bridge.api.assert_not_called()
        self.bridge.call('submit_job',{'template':'ir_v1','request_key':'bound'})
        self.assertEqual(self.submissions[0]['parameters'],fixed['parameters'])
        self.assertNotIn('argv',self.submissions[0])
        schema=next(tool for tool in tools(self.bridge.session) if tool['name']=='submit_job')['inputSchema']
        self.assertEqual(schema['properties']['template']['enum'],['ir_v1'])
        self.assertNotIn('seconds',schema['properties'])

    def test_workflow_result_uses_controller_record_and_bounded_workload_evidence(self):
        self.bridge.session.update(environment='worker-python',workflow_request={
            'template':'ir_v1','environment':'worker-python','parameters':{'input_id':'input-'+'a'*24,'result_file':'results/workload-result.json'}})
        original=self.bridge.api.side_effect
        def api(path,data=None,raw=False):
            if path.endswith('artifact?name=validated-result.json'):
                return b'{"passed":true,"environment_locked":true,"kind":"ir"}'
            if path.endswith('artifact?name=results%2Fworkload-result.json'):
                return b'{"schema":"ascend-lab-result/v1","passed":true,"constant_folded_value":42}'
            return original(path,data,raw)
        self.bridge.api.side_effect=api
        self.bridge.call('submit_job',{'template':'ir_v1','request_key':'bound'})
        value=self.bridge.call('get_result',{'job_id':self.job_id})
        self.assertTrue(value['artifact']['environment_locked'])
        self.assertEqual(value['workload_result']['constant_folded_value'],42)

    def test_changing_profile_to_another_identity_cannot_rebind_an_active_agent(self):
        self.bridge.session['identity']='alice'
        client=Mock();client.request.return_value={'id':'bob'}
        with patch('mcp_bridge.Client.from_profile',return_value=client):
            with self.assertRaisesRegex(BridgeError,'identity changed'):Bridge.api(self.bridge,'/api/jobs')
        client.request.assert_called_once_with('/api/me')

    def test_other_jobs_and_arbitrary_paths_are_never_exposed(self):
        for name in ['get_job','get_logs','get_result','cancel_job','wait_job']:
            with self.subTest(name=name), self.assertRaises(BridgeError):
                self.bridge.call(name, {'job_id':'job-ffffffffffff'})
        self.bridge.api.assert_not_called()
        self.submit()
        with self.assertRaises(BridgeError):
            self.bridge.call('get_result', {'job_id':self.job_id,'path':'/etc/passwd'})
        result = self.bridge.call('get_result', {'job_id':self.job_id})
        self.assertTrue(result['artifact']['passed'])
        self.assertFalse(result['artifact']['npu_used'])
        self.assertNotIn('jobs', self.bridge.call('queue_status', {}))

    def test_stdio_mcp_negotiation_and_tool_errors_remain_valid_json(self):
        messages = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'cancel_job','arguments':{'job_id':'foreign'}}},
        ]
        output=io.StringIO()
        serve(self.bridge,io.StringIO('\n'.join(json.dumps(m) for m in messages)+'\n'),output)
        replies=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([m['id'] for m in replies], [1,2,3])
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        registered=replies[1]['result']['tools']
        self.assertEqual(len(registered), 8)
        preview=next(item for item in registered if item['name']=='preview_job')
        self.assertTrue(preview['annotations']['readOnlyHint'])
        self.assertTrue(replies[2]['result']['isError'])
        self.bridge.api.assert_not_called()

    def test_jsonc_preserves_urls_and_escaped_quotes(self):
        source='''// header
        {"url":"https://example.test/a//b", /* comment */
         "quote":"say \\"hi\\"", "array":[1,2,], "string":",}",}
        '''
        result=parse_jsonc(source)
        self.assertEqual(result, {'url':'https://example.test/a//b','quote':'say "hi"','array':[1,2],'string':',}'})
        with self.assertRaises(ValueError):parse_jsonc('{ /* unfinished')

    def test_final_permission_rules_override_earlier_default_allow(self):
        rules=[{'permission':'*','pattern':'*','action':'allow'},
               {'permission':'*','pattern':'*','action':'deny'},
               {'permission':'ascend_lab_*','pattern':'*','action':'allow'}]
        effective=effective_permissions(rules)
        self.assertTrue(all(action==('allow' if name.startswith('ascend_lab_') else 'deny')
                            for name,action in effective.items()))
        rules.append({'permission':'bash','pattern':'*','action':'allow'})
        self.assertEqual(effective_permissions(rules)['bash'], 'allow')


if __name__ == '__main__':unittest.main()

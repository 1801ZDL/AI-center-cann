import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mocked

import compiler_experiment as ce
from lab_client import ClientError,ClientUnavailable


SOURCE='module { func.func @value() -> i64 { %c = arith.constant 42 : i32 return %c : i32 } }\n'
SOURCE_JOB='job-000000000001'


class FakeClient:
    def __init__(self):
        self.url='http://127.0.0.1:1234';self.config={'token':'unit-only'}
        self.me={'id':'alice','experiment_id':'experiment-small'};self.calls=[];self.jobs={}
        self.inputs={};self.charges=1;self.lose_submit=False;self.lose_upload=False
        self.source={'id':SOURCE_JOB,'owner':'alice','experiment_id':'experiment-small','state':'failed',
                     'parameters':{'lock':{'python':'3.11','image_sha256':'sha256:'+'1'*64,'files':{'/opt/ascend-lab/tool':'2'*64}}}}
    def request(self,path,body=None):
        self.calls.append((path,body))
        if path=='/api/me':return copy.deepcopy(self.me)
        if path=='/api/experiment-budgets/experiment-small':return {'budget':{'accepts_new_jobs':self.charges<3,'charged':{'jobs':self.charges}}}
        if path=='/api/jobs/'+SOURCE_JOB:return {'job':copy.deepcopy(self.source)}
        if path=='/api/jobs':
            for ident,item in self.jobs.items():
                if item['request']['idempotency_key']==body['idempotency_key']:
                    if item['request']!=body:raise ClientError('Conflict')
                    return {'job':{'id':ident,'state':item['state']},'reused':True}
            if self.charges>=3:raise ClientError('Exhausted')
            ident='job-'+str(len(self.jobs)+2).zfill(12);self.jobs[ident]={'request':copy.deepcopy(body),'state':'queued'};self.charges+=1
            if self.lose_submit:self.lose_submit=False;raise ClientUnavailable('Lost reply')
            return {'job':{'id':ident,'state':'queued'},'reused':False}
        ident=path.removeprefix('/api/jobs/').split('/')[0]
        if ident in self.jobs:
            if path.endswith('/cancel'):self.jobs[ident]['state']='cancelled'
            return {'job':{'id':ident,'state':self.jobs[ident]['state']}}
        raise AssertionError(path)
    def upload_input(self,path):
        files={p.name:p.read_bytes() for p in Path(path).iterdir()};digest=ce.sha(b''.join(k.encode()+v for k,v in sorted(files.items())))
        self.inputs[digest]=files
        if self.lose_upload:self.lose_upload=False;raise ClientUnavailable('Lost upload reply')
        return {'id':'input-'+digest[:12]}


def case(source=SOURCE):
    return {'schema':'ascend-lab-compiler-diagnostic/v1','terminal':True,'job':{'state':'failed'},
        'source_complete':True,'source_redacted':False,'source_ir':source,'source_ir_sha256':ce.sha(source.encode()),
        'compiler_stderr_archived':True,'compiler_stderr':'return type mismatch',
        'compiler_report':{'outcome':{'exit_code':1,'timed_out':False,'stderr_truncated':False},
            'tool':{'name':'bishengir-opt','path':'/opt/ascend-lab/tool','sha256':'2'*64,'version':'fixed'}},
        'workflow_binding':{'source_bound_to_seal':True,'driver_bound_to_seal':True,'driver_known_to_client':True,
            'protected_tool_lock_matched':True,'driver_sha256':ce.sha(ce.driver_bytes())},
        'evidence':[{'id':'execution_result','text':json.dumps({'termination_confirmed':True,'execution_cleanup_confirmed':True,'workspace_release_confirmed':True})}],
        'scope':'Parser evidence only'}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.client=FakeClient();self.current_case=case()
        self.patches=[mocked('compiler_experiment.Client.from_profile',return_value=self.client),
                      mocked('compiler_experiment.read_case',side_effect=lambda client,ident:copy.deepcopy(self.current_case))]
        for patch in self.patches:patch.start()
        self.directory=self.root/'session';ce.initialize(self.root/'profile.json',SOURCE_JOB,self.directory)
        self.bridge=ce.Experiment(self.directory)
    def tearDown(self):
        for patch in reversed(self.patches):patch.stop()
        self.tmp.cleanup()
    def prepare(self,key='first',**changes):
        args={'candidate_key':key,'source_sha256':ce.sha(SOURCE.encode()),'old':'-> i64','new':'-> i32',**changes}
        return self.bridge.call('prepare_candidate',args)
    def submit(self,key='first'):return self.bridge.call('submit_candidate',{'candidate_key':key})

    def test_fixed_cpu_contract_and_input_copy_preserve_original(self):
        value=self.prepare();self.assertFalse(value['job_submitted']);self.assertEqual(self.client.charges,1)
        self.assertEqual(len(self.client.inputs),1)
        files=next(iter(self.client.inputs.values()));self.assertEqual(set(files),{'input.mlir',ce.DRIVER,'compiler-case.json'})
        self.assertEqual(files['input.mlir'].decode(),SOURCE.replace('-> i64','-> i32'))
        response=self.submit();body=self.client.jobs[response['job_id']]['request'];self.assertEqual(body['parameters']['resources'],{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0})
        self.assertEqual(body['parameters']['argv'],['python','-I','-B',ce.DRIVER]);self.assertEqual(body['parameters']['timeout_seconds'],60)
        self.assertEqual(self.current_case['source_ir'],SOURCE)

    def test_submit_replay_is_same_job_and_one_charge(self):
        self.prepare();first=self.submit();second=self.submit()
        self.assertEqual(first['job_id'],second['job_id']);self.assertTrue(second['reused']);self.assertEqual(self.client.charges,2)

    def test_upload_reply_loss_resumes_same_sealed_bytes(self):
        self.client.lose_upload=True
        with self.assertRaises(ClientUnavailable):self.prepare()
        self.prepare();self.assertEqual(len(self.client.inputs),1);self.assertEqual(self.client.charges,1)

    def test_lost_submit_cannot_be_replaced_by_another_key(self):
        self.prepare();self.client.lose_submit=True
        with self.assertRaises(ClientUnavailable):self.submit()
        with self.assertRaises(ClientError):self.prepare('replacement',new='-> f32')
        restored=ce.Experiment(self.directory).call('submit_candidate',{'candidate_key':'first'})
        self.assertTrue(restored['reused']);self.assertEqual(self.client.charges,2)

    def test_existing_key_cannot_change_proposal(self):
        self.prepare()
        with self.assertRaises(ClientError):self.prepare(new='-> f32')

    def test_unknown_or_running_candidate_blocks_additional_experiments(self):
        self.prepare();ident=self.submit()['job_id']
        for state in ['queued','reserved','running','needs_attention','cancelling']:
            self.client.jobs[ident]['state']=state
            with self.assertRaises(ClientError):self.prepare('second',new='-> f32')
        self.client.jobs[ident]['state']='failed';self.prepare('second',new='-> f32')

    def test_per_session_count_and_central_budget_remain_distinct(self):
        self.prepare();ident=self.submit()['job_id'];self.client.jobs[ident]['state']='failed'
        self.prepare('second',new='-> f32');self.submit('second')
        self.assertEqual(self.client.charges,3)
        with self.assertRaises(ClientError):self.prepare('third',new='-> f64')

    def test_exhausted_budget_prevents_new_input(self):
        self.client.charges=3
        with self.assertRaises(ClientError):self.prepare()
        self.assertFalse(self.client.inputs)

    def test_owner_budget_and_endpoint_changes_are_rejected(self):
        for field,value in [('id','bob'),('experiment_id','other')]:
            original=self.client.me[field];self.client.me[field]=value
            with self.assertRaises(ClientError):self.bridge.call('get_case',{})
            self.client.me[field]=original
        self.client.url='http://another:1234'
        with self.assertRaises(ClientError):self.bridge.call('experiment_status',{})

    def test_broader_unbound_profile_is_not_a_fallback(self):
        self.client.me.pop('experiment_id')
        with self.assertRaises(ClientError):self.bridge.call('get_case',{})

    def test_command_path_resource_and_job_overrides_are_refused(self):
        for name,args in [('prepare_candidate',{'candidate_key':'x','source_sha256':'x','old':'x','new':'y','argv':['sh']}),
                          ('submit_candidate',{'candidate_key':'x','npu':1}),('get_case',{'job_id':SOURCE_JOB}),
                          ('get_candidate_result',{'candidate_key':'x','artifact':'/etc/passwd'}),('shell',{})]:
            with self.assertRaises(ClientError):self.bridge.call(name,args)
        self.assertFalse(self.client.inputs)

    def test_unsafe_or_ambiguous_edit_and_wrong_source_do_not_create_intent(self):
        for changed in [{'source_sha256':'0'*64},{'old':'missing'},{'old':'i32'},{'new':'line\nmore'},
                        {'new':''},{'old':True},{'new':'x'*257},{'candidate_key':'../escape'}]:
            with self.assertRaises(ClientError):self.prepare(**changed)
        self.assertEqual(json.loads((self.directory/'state.json').read_text())['candidates'],{})

    def test_missing_redacted_large_or_unknown_source_is_not_used_for_patch(self):
        for field,value in [('source_redacted',True),('source_complete',False),('terminal',False),('source_ir','different')]:
            original=self.current_case[field];self.current_case[field]=value
            with self.assertRaises(ClientError):self.prepare()
            self.current_case[field]=original

    def test_unconfirmed_cleanup_or_protected_lock_is_rejected(self):
        self.current_case['workflow_binding']['protected_tool_lock_matched']=False
        with self.assertRaises(ClientError):self.prepare()
        self.current_case=case();self.current_case['evidence'][0]['text']='{}'
        with self.assertRaises(ClientError):self.prepare()

    def test_timeout_or_truncated_error_is_not_a_repair_case(self):
        for field,value in [('timed_out',True),('stderr_truncated',True),('exit_code',0)]:
            previous=self.current_case['compiler_report']['outcome'][field];self.current_case['compiler_report']['outcome'][field]=value
            with self.assertRaises(ClientError):self.prepare()
            self.current_case['compiler_report']['outcome'][field]=previous

    def test_new_session_cannot_overwrite_original_reconnect_state(self):
        with self.assertRaises(ClientError):ce.initialize(self.root/'profile.json',SOURCE_JOB,self.directory)
        self.prepare();self.assertEqual(ce.Experiment(self.directory).call('experiment_status',{})['candidates']['first']['candidate_sha256'],ce.sha(SOURCE.replace('-> i64','-> i32').encode()))

    def test_same_candidate_digest_across_keys_replays_globally(self):
        self.prepare();ident=self.submit()['job_id'];self.client.jobs[ident]['state']='succeeded'
        self.prepare('other');other=self.submit('other')
        self.assertEqual(other['job_id'],ident);self.assertTrue(other['reused']);self.assertEqual(self.client.charges,2)

    def test_mcp_capabilities_and_errors_do_not_echo_untrusted_material(self):
        calls=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
               {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'shell','arguments':{'token':'DO_NOT_ECHO_PRIVATE'}}}]
        output=io.StringIO();ce.serve(self.bridge,io.StringIO(''.join(json.dumps(v)+'\n' for v in calls)),output)
        values=[json.loads(v) for v in output.getvalue().splitlines()]
        self.assertEqual(len(values[0]['result']['tools']),7);self.assertTrue(values[1]['result']['isError'])
        self.assertNotIn('DO_NOT_ECHO_PRIVATE',output.getvalue())


if __name__=='__main__':unittest.main()

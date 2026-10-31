"""Real HTTP compiler evidence ACL, byte binding and bounded candidate contract."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_diagnostic_case as builder
from compiler_diagnostic import REPORT,SOURCE,STDERR,CompilerBridge,read_case,review_minimal_rename,sha
from diagnostic_case import serve
from lab_client import Client,ClientError,atomic_json

EDIT={'old':'"bufferization.to_memref"','new':'"bufferization.to_buffer"'}


def proposal(case):
    return {'schema':'ascend-lab-compiler-proposal/v1','category':'ir_dialect_version_mismatch','summary':'Known paired parser mismatch; candidate needs a separate queued verification.',
        'target_tool':'triton-mlir-opt','expected_source_sha256':case['source_ir_sha256'],'replace':{**EDIT,'count':1},
        'npu_fault_proven':False,'numeric_correctness_proven':False,'full_pipeline_proven':False,'primary_review_required':True}


class CompilerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DiagnosticTests();self.base.setUp();self.client=self.base.clients['alice'];self.identifier=self.base.identifier
        self.directory=self.base.jobs.directory(self.identifier);(self.directory/'ir').mkdir(exist_ok=True)
        self.source=b'module { func.func @view(%a: tensor<4xf32>) -> memref<4xf32> { %0 = "bufferization.to_memref"(%a) : (tensor<4xf32>) -> memref<4xf32>\n return %0 : memref<4xf32> } }\n'
        self.driver_sha=sha((ROOT/'examples/workflows/compiler_compatibility.py').read_bytes());self.tool='/opt/tools/triton-mlir-opt'
        self.report={'schema':'ascend-lab-compiler-report/v1','purpose':'mlir_parse_and_verify','source':{'artifact':SOURCE,'bytes':len(self.source),'sha256':sha(self.source)},
            'driver_sha256':self.driver_sha,'tool':{'name':'triton-mlir-opt','path':self.tool,'sha256':'a'*64,'version':'LLVM22 fixture metadata'},
            'outcome':{'exit_code':1,'timed_out':False,'stderr_bytes':len(b'unregistered operation'),'stderr_truncated':False}}
        (self.directory/SOURCE).write_bytes(self.source);(self.directory/STDERR).write_bytes(b'unregistered operation')
        atomic_json(self.directory/REPORT,self.report)
        (self.directory/'results/_lab').mkdir();self.receipt=self.directory/'results/_lab/environment.json'
        atomic_json(self.receipt,{'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'observed':{'files':{self.tool:{'sha256':'a'*64}}}})
        index=json.loads((self.directory/'artifacts.json').read_text());index['files'] += [{'name':n,'bytes':(self.directory/n).stat().st_size} for n in [REPORT,SOURCE,STDERR,'results/_lab/environment.json']];atomic_json(self.directory/'artifacts.json',index)
        job=self.base.jobs.get(self.identifier);job.update(template='ir_v1',kind='ir',environment='worker-python',execution_backend='managed-docker',
            parameters={'input_id':'input-'+'a'*24,'argv':['python','-I','-B','compiler_compatibility.py'],'lock':{'files':{self.tool:'a'*64}}},
            input={'id':'input-'+'a'*24,'files':[{'name':'input.mlir','sha256':sha(self.source)},{'name':'compiler_compatibility.py','sha256':self.driver_sha}]})
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        self.set_state('failed')

    def set_state(self,state,**result):
        job=self.base.jobs.get(self.identifier);job['state']=state;job['result']={'status':'failed','termination_confirmed':True,'execution_cleanup_confirmed':True,'workspace_release_confirmed':True,**result}
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)

    def tearDown(self):self.base.tearDown()

    def test_complete_ir_and_report_are_bound_without_adding_execution_authority(self):
        before=len(self.base.jobs.all());case=read_case(self.client,self.identifier)
        self.assertEqual(case['source_ir'].encode(),self.source);self.assertEqual(case['source_ir_sha256'],sha(self.source));self.assertFalse(case['source_redacted'])
        self.assertEqual(case['compiler_report_sha256'],sha((self.directory/REPORT).read_bytes()));self.assertIn('No patch or task',case['candidate_policy'])
        self.assertEqual(len(self.base.jobs.all()),before)

    def test_foreign_identity_cannot_reach_compiler_artifact_requests(self):
        calls=[];client=self.base.clients['bob'];original=client.request
        def request(path,*a,**kw):calls.append(path);return original(path,*a,**kw)
        client.request=request
        with self.assertRaisesRegex(ClientError,'404'):read_case(client,self.identifier)
        self.assertFalse(any('artifact' in p for p in calls))

    def test_read_only_token_and_one_bound_stdio_tool(self):
        token=self.base.jobs.access.issue_token('alice',{'jobs:read'});client=Client({'controller':self.client.url,'token':token})
        self.assertEqual(read_case(client,self.identifier)['job']['id'],self.identifier)
        bridge=CompilerBridge(client,self.identifier)
        for name,args in [('submit_job',{}),('get_diagnostic_case',{'job_id':'job-'+'f'*12}),('get_diagnostic_case',{'path':'/etc/passwd'})]:
            with self.assertRaises(ClientError):bridge.call(name,args)
        output=io.StringIO();messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},{'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}}]
        serve(bridge,io.StringIO(''.join(json.dumps(v)+'\n' for v in messages)),output)
        values=[json.loads(v)['result'] for v in output.getvalue().splitlines()]
        self.assertEqual(len(values[0]['tools']),1);self.assertTrue(values[0]['tools'][0]['annotations']['readOnlyHint']);self.assertFalse(values[1]['isError'])

    def test_report_hash_source_binding_and_complete_response_are_required(self):
        original=self.client.request
        for field,value in [('name','other'),('offset',1),('size',999999),('sha256','0'*64)]:
            def request(path,*a,**kw):
                result=original(path,*a,**kw)
                if 'artifact-chunk?' in path and 'compiler-case.json' in path:result={**result,field:value}
                return result
            self.client.request=request
            with self.subTest(field=field),self.assertRaises(ClientError):read_case(self.client,self.identifier)
        self.client.request=original;self.report['source']['sha256']='0'*64;atomic_json(self.directory/REPORT,self.report)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_missing_oversized_and_invalid_utf8_ir_fail_closed(self):
        path=self.directory/SOURCE
        for raw in [b'x'*8193,b'\xff']:
            path.write_bytes(raw);self.report['source'].update(bytes=len(raw),sha256=sha(raw));atomic_json(self.directory/REPORT,self.report)
            with self.assertRaises(ClientError):read_case(self.client,self.identifier)
        path.unlink()
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def large_source(self,raw=None):
        raw=raw or self.source+b'// archived real IR extent\n'*24000
        (self.directory/SOURCE).write_bytes(raw);self.report['schema']='ascend-lab-compiler-report/v2'
        self.report['source'].update(bytes=len(raw),sha256=sha(raw));atomic_json(self.directory/REPORT,self.report)
        job=self.base.jobs.get(self.identifier);job['input']['files'][0]['sha256']=sha(raw)
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        return raw

    def test_large_ir_is_fully_checked_but_only_bounded_excerpts_reach_the_harness(self):
        raw=self.large_source();calls=[];original=self.client.request
        def request(path,*a,**kw):
            if 'artifact-chunk?' in path and 'diagnostic-input.mlir' in path:calls.append(path)
            return original(path,*a,**kw)
        self.client.request=request;case=read_case(self.client,self.identifier)
        self.assertGreater(len(calls),2);self.assertEqual(case['source_ir_sha256'],sha(raw));self.assertEqual(case['source_verified_bytes'],len(raw))
        self.assertIsNone(case['source_ir']);self.assertFalse(case['source_complete']);self.assertLess(len(json.dumps(case).encode()),40000)
        self.assertEqual(case['schema'],'ascend-lab-compiler-diagnostic/v2')
        with self.assertRaises(ClientError):review_minimal_rename(case,proposal(case),EDIT)

    def test_changed_middle_chunk_size_offset_checksum_and_short_read_are_rejected(self):
        self.large_source();original=self.client.request
        for field,value in [('size',123),('offset',0),('sha256','0'*64),('data','')]:
            def request(path,*a,**kw):
                response=original(path,*a,**kw)
                if 'diagnostic-input.mlir' in path and 'offset=262144' in path:response={**response,field:value}
                return response
            self.client.request=request
            with self.subTest(field=field),self.assertRaises(ClientError):read_case(self.client,self.identifier)
        self.client.request=original

    def test_large_source_redaction_and_unicode_are_not_split_at_transfer_boundaries(self):
        secret=self.base.passwords['alice'].encode();raw=b'// '+b'a'*(262144-5)+secret+b'\n// '+('中'*10000).encode()+b'\n'
        self.large_source(raw);case=read_case(self.client,self.identifier)
        self.assertTrue(case['source_redacted']);self.assertNotIn(secret.decode(),json.dumps(case));self.assertFalse(case['source_complete'])

    def test_large_ir_diagnostic_location_has_bounded_line_context(self):
        raw=self.large_source();error=b'/work/ir/diagnostic-input.mlir:12000:7: error: example\n'
        (self.directory/STDERR).write_bytes(error);self.report['outcome']['stderr_bytes']=len(error);atomic_json(self.directory/REPORT,self.report)
        case=read_case(self.client,self.identifier);context=case['source_preview']['diagnostic_context']
        self.assertEqual(context[0]['line'],12000);self.assertEqual([v['number'] for v in context[0]['lines']],[11999,12000,12001])
        self.assertEqual(case['source_ir_sha256'],sha(raw))

    def test_large_source_aggregate_hash_and_versioned_limit_are_still_required(self):
        raw=self.large_source();changed=bytearray(raw);changed[300000]=ord('x');(self.directory/SOURCE).write_bytes(changed)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)
        self.report['source']['bytes']=8*1024**2+1;atomic_json(self.directory/REPORT,self.report)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def omit_stderr(self):
        (self.directory/STDERR).unlink();index=json.loads((self.directory/'artifacts.json').read_text())
        index['files']=[v for v in index['files'] if v['name']!=STDERR];atomic_json(self.directory/'artifacts.json',index)

    def test_successful_large_case_with_omitted_zero_byte_stderr_is_explicit(self):
        self.large_source();self.omit_stderr();self.report['outcome'].update(exit_code=0,stderr_bytes=0)
        atomic_json(self.directory/REPORT,self.report);self.set_state('succeeded')
        case=read_case(self.client,self.identifier)
        self.assertEqual(case['compiler_stderr'],'');self.assertFalse(case['compiler_stderr_archived'])
        self.assertIsNone(case['compiler_stderr_sha256']);self.assertIn('zero bytes',case['compiler_stderr_source'])

    def test_missing_nonempty_stderr_is_rejected_and_absent_empty_stderr_cannot_authorize_rename(self):
        self.omit_stderr()
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)
        self.report['outcome']['stderr_bytes']=0;atomic_json(self.directory/REPORT,self.report)
        case=read_case(self.client,self.identifier)
        with self.assertRaises(ClientError):review_minimal_rename(case,proposal(case),EDIT)

    def test_redacted_source_is_explicit_and_cannot_be_used_as_an_exact_patch(self):
        raw=self.source+b'// password='+self.base.passwords['alice'].encode()+b'\n';(self.directory/SOURCE).write_bytes(raw)
        self.report['source'].update(bytes=len(raw),sha256=sha(raw));atomic_json(self.directory/REPORT,self.report)
        job=self.base.jobs.get(self.identifier);job['input']['files'][0]['sha256']=sha(raw)
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        case=read_case(self.client,self.identifier);self.assertTrue(case['source_redacted']);self.assertNotIn(self.base.passwords['alice'],json.dumps(case))
        with self.assertRaises(ClientError):review_minimal_rename(case,proposal(case),EDIT)

    def test_unknown_or_unconfirmed_cleanup_never_authorizes_candidate(self):
        original=read_case(self.client,self.identifier)
        for state,result in [('needs_attention',{}),('failed',{'workspace_release_confirmed':False}),('failed',{'execution_cleanup_confirmed':False}),('failed',{'termination_confirmed':False})]:
            self.set_state(state,**result)
            if state=='needs_attention':
                with self.assertRaises(ClientError):read_case(self.client,self.identifier)
                case=copy.deepcopy(original);case['job']['state']=state;case['terminal']=False
            else:case=read_case(self.client,self.identifier)
            with self.subTest(state=state,result=result),self.assertRaises(ClientError):review_minimal_rename(case,proposal(case),EDIT)
            self.assertEqual(self.base.jobs.get(self.identifier)['state'],state)

    def test_unknown_driver_and_mismatched_protected_tool_never_authorize_candidate(self):
        atomic_json(self.receipt,{'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'observed':{'files':{self.tool:{'sha256':'b'*64}}}})
        case=read_case(self.client,self.identifier);self.assertFalse(case['workflow_binding']['protected_tool_lock_matched'])
        with self.assertRaises(ClientError):review_minimal_rename(case,proposal(case),EDIT)
        atomic_json(self.receipt,{'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'observed':{'files':{self.tool:{'sha256':'a'*64}}}})
        self.report['driver_sha256']='c'*64;atomic_json(self.directory/REPORT,self.report);job=self.base.jobs.get(self.identifier);job['input']['files'][1]['sha256']='c'*64
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        case=read_case(self.client,self.identifier);self.assertFalse(case['workflow_binding']['driver_known_to_client'])
        with self.assertRaises(ClientError):review_minimal_rename(case,proposal(case),EDIT)

    def test_matching_report_and_archive_cannot_replace_the_sealed_input(self):
        raw=self.source+b'// changed after seal\n';(self.directory/SOURCE).write_bytes(raw)
        self.report['source'].update(bytes=len(raw),sha256=sha(raw));atomic_json(self.directory/REPORT,self.report)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_job_metadata_is_redacted_before_harness_delivery(self):
        job=self.base.jobs.get(self.identifier);job['name']='password='+self.base.passwords['alice']
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        case=read_case(self.client,self.identifier);self.assertNotIn(self.base.passwords['alice'],json.dumps(case));self.assertIn('REDACTED',case['job']['name'])

    def test_primary_approved_single_edit_returns_only_a_candidate(self):
        case=read_case(self.client,self.identifier);draft=proposal(case);before=len(self.base.jobs.all());result=review_minimal_rename(case,draft,EDIT)
        self.assertEqual(result['candidate_ir'],case['source_ir'].replace(EDIT['old'],EDIT['new'],1));self.assertFalse(result['submission_performed'])
        self.assertTrue(result['experiment_still_required']);self.assertFalse(result['full_pipeline_proven']);self.assertEqual(len(self.base.jobs.all()),before)

    def test_timeout_truncated_or_successful_compiler_outcome_cannot_authorize_this_fix(self):
        case=read_case(self.client,self.identifier)
        for patch in [{'exit_code':0},{'exit_code':-6},{'exit_code':-25},{'timed_out':True},{'stderr_truncated':True}]:
            value=copy.deepcopy(case);value['compiler_report']['outcome'].update(patch)
            with self.assertRaises(ClientError):review_minimal_rename(value,proposal(value),EDIT)

    def test_commands_wrong_source_target_count_and_numeric_claims_are_rejected(self):
        case=read_case(self.client,self.identifier)
        mutations=[lambda v:v.update(command='reset device'),lambda v:v.update(expected_source_sha256='0'*64),lambda v:v.update(target_tool='bishengir-opt'),
            lambda v:v['replace'].update(count=True),lambda v:v['replace'].update(count=2),lambda v:v['replace'].update(new='arbitrary IR'),
            lambda v:v.update(npu_fault_proven=True),lambda v:v.update(numeric_correctness_proven=True),lambda v:v.update(full_pipeline_proven=True)]
        for mutate in mutations:
            value=proposal(case);mutate(value)
            with self.assertRaises(ClientError):review_minimal_rename(case,value,EDIT)


if __name__=='__main__':unittest.main()

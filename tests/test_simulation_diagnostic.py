import copy
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_diagnostic_case as builder
from test_simulation_data import fixture
from simulation_data import INPUT,DRIVER,REPORT,PREFIX,analyze,sha
from simulation_diagnostic import SimulationBridge,read_case
from diagnostic_case import serve
from lab_client import Client,ClientError,atomic_json


class SimulationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DiagnosticTests();self.base.setUp();self.client=self.base.clients['alice'];self.identifier=self.base.identifier
        self.directory=self.base.jobs.directory(self.identifier);(self.directory/PREFIX).mkdir(parents=True)
        self.value,self.blobs=fixture();self.raw=json.dumps(self.value,sort_keys=True).encode();self.driver_sha=sha((ROOT/DRIVER).read_bytes())
        self.report={'schema':'ascend-lab-simulation-report/v1','purpose':'offline_simulation_analysis','driver_sha256':self.driver_sha,
            'input_sha256':sha(self.raw),'input':self.value,'analysis':analyze(self.value,lambda name,limit:self.blobs[name])}
        atomic_json(self.directory/REPORT,self.report);(self.directory/(PREFIX+INPUT)).write_bytes(self.raw)
        for name,raw in self.blobs.items():(self.directory/(PREFIX+name)).write_bytes(raw)
        (self.directory/'results/_lab').mkdir();self.receipt=self.directory/'results/_lab/environment.json'
        atomic_json(self.receipt,{'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'observed':{'image_sha256':'sha256:'+'a'*64}})
        index=json.loads((self.directory/'artifacts.json').read_text());names=[REPORT,PREFIX+INPUT,'results/_lab/environment.json',*[PREFIX+n for n in self.blobs]]
        index['files'] += [{'name':n,'bytes':(self.directory/n).stat().st_size} for n in names];atomic_json(self.directory/'artifacts.json',index)
        job=self.base.jobs.get(self.identifier);job.update(template='build_v1',kind='build',environment='worker-python',execution_backend='managed-docker',
            parameters={'input_id':'input-'+'a'*24,'argv':['python','-I','-B',DRIVER],'lock':{'image_sha256':'sha256:'+'a'*64}},
            input={'id':'input-'+'a'*24,'files':[{'name':INPUT,'sha256':sha(self.raw)},{'name':DRIVER,'sha256':self.driver_sha},
                *[{'name':name,'sha256':sha(raw)} for name,raw in self.blobs.items()]]})
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)

    def tearDown(self):self.base.tearDown()

    def test_archived_bytes_are_independently_recomputed_with_bounded_summary_only(self):
        before=len(self.base.jobs.all());case=read_case(self.client,self.identifier)
        self.assertEqual(case['analysis']['capture']['capture_state'],'abnormal');self.assertTrue(case['all_source_bytes_verified'])
        self.assertFalse(case['raw_trace_delivered_to_model']);self.assertTrue(case['workflow_binding']['driver_known_to_client'])
        self.assertTrue(case['workflow_binding']['protected_environment_matched']);self.assertEqual(len(self.base.jobs.all()),before)

    def test_forged_report_cannot_turn_abnormal_capture_or_busy_union_into_success(self):
        for modify in [lambda v:v['analysis']['capture'].update(capture_state='normal',capture_completeness_certified=True),
            lambda v:v['analysis']['cores'][0]['analysis']['pipes']['RVECLD'].update(union_us=100.)]:
            report=copy.deepcopy(self.report);modify(report);atomic_json(self.directory/REPORT,report)
            with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_changed_trace_and_matching_report_do_not_replace_the_sealed_source(self):
        trace=json.loads(self.blobs['core0.trace.json']);trace['traceEvents'][2]['dur']=1
        changed=json.dumps(trace).encode();self.blobs['core0.trace.json']=changed;self.value['cores'][0]['trace']['sha256']=sha(changed)
        raw=json.dumps(self.value,sort_keys=True).encode();self.report.update(input_sha256=sha(raw),analysis=analyze(self.value,lambda name,limit:self.blobs[name]))
        (self.directory/(PREFIX+INPUT)).write_bytes(raw);(self.directory/(PREFIX+'core0.trace.json')).write_bytes(changed);atomic_json(self.directory/REPORT,self.report)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_source_short_chunk_wrong_sha_and_missing_archive_rejected(self):
        original=self.client.request
        for mode in ['short','hash','missing']:
            def request(path,*a,**kw):
                value=original(path,*a,**kw)
                if 'artifact-chunk?' in path and 'core0.trace.json' in path:
                    value=copy.deepcopy(value)
                    if mode=='short':value['data']=value['data'][:-4]
                    elif mode=='hash':value['sha256']='0'*64
                    else:raise ClientError('missing')
                return value
            self.client.request=request
            with self.subTest(mode=mode),self.assertRaises(ClientError):read_case(self.client,self.identifier)
        self.client.request=original

    def test_foreign_identity_is_rejected_before_any_evidence_read(self):
        client=self.base.clients['bob'];calls=[];original=client.request
        def request(path,*a,**kw):calls.append(path);return original(path,*a,**kw)
        client.request=request
        with self.assertRaisesRegex(ClientError,'404'):read_case(client,self.identifier)
        self.assertFalse(any('artifact' in path for path in calls))

    def test_bound_readonly_stdio_rejects_job_path_and_task_override(self):
        bridge=SimulationBridge(self.client,self.identifier)
        calls=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'job_id':'job-'+'a'*12}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'path':'/etc/passwd'}}},
            {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'submit_job','arguments':{}}}]
        output=io.StringIO();serve(bridge,io.StringIO(''.join(json.dumps(v)+'\n' for v in calls)),output)
        values=[json.loads(v)['result'] for v in output.getvalue().splitlines()]
        self.assertEqual(len(values[0]['tools']),1);self.assertFalse(values[1]['isError']);self.assertTrue(all(v['isError'] for v in values[2:]))

    def test_malformed_receipt_is_a_tool_error(self):
        atomic_json(self.receipt,{'observed':[]});output=io.StringIO()
        call={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}}
        serve(SimulationBridge(self.client,self.identifier),io.StringIO(json.dumps(call)+'\n'),output)
        self.assertTrue(json.loads(output.getvalue())['result']['isError'])

    def test_caller_metadata_is_redacted_after_full_hash_verification(self):
        self.value['origin']['scope']='password='+self.base.passwords['alice'];raw=json.dumps(self.value,sort_keys=True).encode()
        self.report.update(input_sha256=sha(raw));(self.directory/(PREFIX+INPUT)).write_bytes(raw);atomic_json(self.directory/REPORT,self.report)
        job=self.base.jobs.get(self.identifier);job['input']['files'][0]['sha256']=sha(raw)
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        case=read_case(self.client,self.identifier);self.assertNotIn(self.base.passwords['alice'],json.dumps(case));self.assertIn('REDACTED',case['input']['origin']['scope'])


if __name__=='__main__':unittest.main()

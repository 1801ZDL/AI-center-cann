import copy
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_diagnostic_case as builder
from test_accuracy_data import fixture
from accuracy_data import INPUT,DRIVER,REPORT,PREFIX,compare,sha
from accuracy_diagnostic import AccuracyBridge,read_case
from diagnostic_case import serve
from lab_client import Client,ClientError,atomic_json


class AccuracyDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DiagnosticTests();self.base.setUp();self.client=self.base.clients['alice'];self.identifier=self.base.identifier
        self.directory=self.base.jobs.directory(self.identifier);(self.directory/PREFIX).mkdir(parents=True)
        self.value,self.blobs=fixture([1.,2.],[1.,3.]);self.config_raw=json.dumps(self.value,sort_keys=True).encode()
        self.driver_sha=sha((ROOT/DRIVER).read_bytes());self.report={'schema':'ascend-lab-accuracy-report/v1','purpose':'offline_accuracy_replay',
            'driver_sha256':self.driver_sha,'input_sha256':sha(self.config_raw),'input':self.value,'comparison':compare(self.value,self.blobs),'new_npu_launches':0}
        atomic_json(self.directory/REPORT,self.report);(self.directory/(PREFIX+INPUT)).write_bytes(self.config_raw)
        for role,raw in self.blobs.items():(self.directory/(PREFIX+self.value[role]['file'])).write_bytes(raw)
        (self.directory/'results/_lab').mkdir();self.receipt=self.directory/'results/_lab/environment.json'
        atomic_json(self.receipt,{'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,'observed':{'image_sha256':'sha256:'+'a'*64}})
        index=json.loads((self.directory/'artifacts.json').read_text());names=[REPORT,PREFIX+INPUT,'results/_lab/environment.json',*[PREFIX+v['file'] for k,v in self.value.items() if k in self.blobs]]
        index['files'] += [{'name':n,'bytes':(self.directory/n).stat().st_size} for n in names];atomic_json(self.directory/'artifacts.json',index)
        job=self.base.jobs.get(self.identifier);job.update(template='build_v1',kind='build',environment='worker-python',execution_backend='managed-docker',state='failed',
            parameters={'input_id':'input-'+'a'*24,'argv':['python','-I','-B',DRIVER],'lock':{'image_sha256':'sha256:'+'a'*64}},
            input={'id':'input-'+'a'*24,'files':[{'name':INPUT,'sha256':sha(self.config_raw)},{'name':DRIVER,'sha256':self.driver_sha},
                *[{'name':self.value[role]['file'],'sha256':sha(raw)} for role,raw in self.blobs.items()]]})
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)

    def tearDown(self):self.base.tearDown()

    def test_actual_bytes_are_recomputed_without_tensor_payload_or_repair_authority(self):
        before=len(self.base.jobs.all());case=read_case(self.client,self.identifier)
        self.assertEqual(case['comparison']['mismatches'],1);self.assertFalse(case['comparison']['passed'])
        self.assertTrue(case['all_tensor_bytes_verified']);self.assertFalse(case['tensor_payload_delivered_to_model'])
        self.assertTrue(case['workflow_binding']['driver_known_to_client']);self.assertTrue(case['workflow_binding']['protected_environment_matched'])
        self.assertFalse(case['workflow_binding']['device_capture_independently_certified']);self.assertEqual(len(self.base.jobs.all()),before)

    def test_report_cannot_claim_pass_against_the_archived_data(self):
        self.report['comparison']['passed']=True;self.report['comparison']['mismatches']=0;atomic_json(self.directory/REPORT,self.report)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_malformed_protected_receipt_returns_an_error_without_crashing_stdio(self):
        for observed in [None,[],False,'invalid']:
            atomic_json(self.receipt,{'schema':'ascend-lab-environment/v1','observed':observed})
            bridge=AccuracyBridge(self.client,self.identifier);out=io.StringIO()
            request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}}
            serve(bridge,io.StringIO(json.dumps(request)+'\n'),out)
            self.assertTrue(json.loads(out.getvalue())['result']['isError'])

    def test_changed_archive_and_report_cannot_replace_sealed_tensor(self):
        raw=b'\0'*len(self.blobs['actual']);(self.directory/(PREFIX+'actual.bin')).write_bytes(raw)
        self.value['actual']['sha256']=sha(raw);self.report['comparison']=compare(self.value,{**self.blobs,'actual':raw})
        config=json.dumps(self.value,sort_keys=True).encode();(self.directory/(PREFIX+INPUT)).write_bytes(config);self.report['input_sha256']=sha(config);atomic_json(self.directory/REPORT,self.report)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_foreign_identity_fails_before_domain_artifact_reads(self):
        calls=[];client=self.base.clients['bob'];original=client.request
        def request(path,*a,**kw):calls.append(path);return original(path,*a,**kw)
        client.request=request
        with self.assertRaisesRegex(ClientError,'404'):read_case(client,self.identifier)
        self.assertFalse(any('artifact' in v for v in calls))

    def test_actual_read_only_stdio_tool_refuses_job_or_path_override(self):
        token=self.base.jobs.access.issue_token('alice',{'jobs:read'});client=Client({'controller':self.client.url,'token':token});bridge=AccuracyBridge(client,self.identifier)
        calls=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'path':'/etc/passwd'}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'submit_job','arguments':{}}}]
        out=io.StringIO();serve(bridge,io.StringIO(''.join(json.dumps(v)+'\n' for v in calls)),out);values=[json.loads(v)['result'] for v in out.getvalue().splitlines()]
        self.assertEqual(len(values[0]['tools']),1);self.assertFalse(values[1]['isError']);self.assertTrue(values[2]['isError']);self.assertTrue(values[3]['isError'])

    def test_metadata_and_examples_are_redacted_before_harness_delivery(self):
        self.value['actual']['origin']['scope']='password='+self.base.passwords['alice'];config=json.dumps(self.value,sort_keys=True).encode()
        self.report['input_sha256']=sha(config);(self.directory/(PREFIX+INPUT)).write_bytes(config);atomic_json(self.directory/REPORT,self.report)
        job=self.base.jobs.get(self.identifier);job['input']['files'][0]['sha256']=sha(config)
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        case=read_case(self.client,self.identifier);self.assertNotIn(self.base.passwords['alice'],json.dumps(case));self.assertIn('REDACTED',case['tensor_sources']['actual']['origin']['scope'])


if __name__=='__main__':unittest.main()

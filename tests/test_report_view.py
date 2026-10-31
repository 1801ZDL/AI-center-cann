import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_diagnostic_case as builder
from test_accuracy_data import fixture as accuracy_fixture
from test_simulation_data import fixture as simulation_fixture
from accuracy_data import compare
from lab_client import Client,ClientHTTPError,atomic_json
from node_storage import manifest_sha256
import report_view
from simulation_data import analyze,sha


def reports():
    av,ab=accuracy_fixture([1.,2.],[1.,3.]);sv,sb=simulation_fixture()
    return {
        'accuracy':{'schema':'ascend-lab-accuracy-report/v1','purpose':'offline_accuracy_replay','input':av,'comparison':compare(av,ab)},
        'simulation':{'schema':'ascend-lab-simulation-report/v1','purpose':'offline_simulation_analysis','input':sv,'analysis':analyze(sv,lambda n,limit:sb[n])},
        'compiler':{'schema':'ascend-lab-compiler-report/v1','purpose':'mlir_parse_and_verify',
            'driver_sha256':'d'*64,'source':{'artifact':'ir/diagnostic-input.mlir','bytes':10,'sha256':'a'*64},
            'tool':{'name':'bishengir-opt','path':'/fixed/bishengir-opt','version':'test-version','sha256':'b'*64},
            'outcome':{'exit_code':1,'timed_out':False,'stderr_bytes':0,'stderr_truncated':False}},
        'operator':{'schema':'ascend-lab-operator-analysis/v1','purpose':'historical_profile_comparison','operator':'unit-only',
            'provider':{'name':'msprof-mcp','version':'0.1.8'},'sources':[{'id':str(i),'samples':30,'mean_us':mean,'min_us':mean,'max_us':mean,'csv_sha256':'a'*64,'context_sha256':'b'*64,'physical_device':i} for i,mean in enumerate([20.,10.])],
            'comparison':{'observed_duration_ratio':2.,'optimization_speedup':None,'blockers':['different_physical_device']}}}


class ReportViewTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DiagnosticTests();self.base.setUp();self.client=self.base.clients['alice'];self.identifier=self.base.identifier
        self.directory=self.base.jobs.directory(self.identifier);self.values=reports()

    def tearDown(self):self.base.tearDown()

    def publish(self,kind,value=None,raw=None):
        data=raw if raw is not None else json.dumps(value if value is not None else self.values[kind],allow_nan=True).encode()
        name=report_view.KINDS[kind][0];(self.directory/name).write_bytes(data)
        names=[n for n,limit in report_view.KINDS.values() if (self.directory/n).exists()]
        names+=['results/compiler-stderr.txt'] if (self.directory/'results/compiler-stderr.txt').exists() else []
        files=[{'name':n,'size':(self.directory/n).stat().st_size,'sha256':sha((self.directory/n).read_bytes())} for n in names]
        atomic_json(self.directory/'artifact-hashes.json',{'version':1,'files':files})
        index=json.loads((self.directory/'artifacts.json').read_text());index['files']=[r for r in index['files'] if r['name'] not in names]+[{'name':v['name'],'bytes':v['size']} for v in files];atomic_json(self.directory/'artifacts.json',index)
        job=self.base.jobs.get(self.identifier);job.update(state='failed',template='build_v1',execution_backend='managed-docker',node_id='worker-'+'b'*12,attempt_id='attempt-'+'a'*32,
            result={'status':'failed','termination_confirmed':True,'execution_cleanup_confirmed':True},
            _artifact_receipt={'job_id':self.identifier,'attempt_id':'attempt-'+'a'*32,'state':'failed','manifest_sha256':manifest_sha256(files)})
        with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
        return data

    def view(self,kind,client=None):return (client or self.client).request('/api/jobs/'+self.identifier+'/diagnostic-report?kind='+kind)

    def test_four_report_types_have_archive_identity_and_explicit_claim_scope(self):
        before=len(self.base.jobs.all())
        for kind in self.values:
            raw=self.publish(kind);v=self.view(kind)
            self.assertEqual(v['report']['sha256'],sha(raw));self.assertEqual(v['job']['state'],'failed')
            self.assertTrue(v['verification']['archive_manifest_verified']);self.assertTrue(v['verification']['report_bytes_verified'])
            self.assertFalse(v['verification']['source_bytes_recomputed']);self.assertFalse(v['verification']['numerical_correctness_certified'])
            self.assertEqual(v['report']['kind'],kind)
        self.assertEqual(len(self.base.jobs.all()),before)

    def test_unknown_or_unclean_execution_never_reads_report_files(self):
        self.publish('simulation')
        for mode in ['unknown','cleanup','attempt','receipt']:
            job=self.base.jobs.get(self.identifier);job['state']='failed';job['result']['execution_cleanup_confirmed']=True;job['attempt_id']='attempt-'+'a'*32
            if mode=='unknown':job['state']='needs_attention'
            elif mode=='cleanup':job['result']['execution_cleanup_confirmed']=False
            elif mode=='attempt':job['attempt_id']='attempt-'+'c'*32
            else:job['_artifact_receipt']['state']='succeeded'
            with self.base.jobs.transaction() as db:self.base.jobs.write(db,job)
            with patch('report_view.raw_file',side_effect=AssertionError('No archive read before confirmation')):
                with self.assertRaises(ClientHTTPError) as caught:self.view('simulation')
                self.assertEqual(caught.exception.status,409)

    def test_foreign_identity_and_unknown_kinds_fail_without_exposing_report(self):
        self.publish('accuracy')
        with patch('report_view.read',side_effect=AssertionError('Foreign caller reached reader')):
            with self.assertRaises(ClientHTTPError) as c:self.view('accuracy',self.base.clients['bob'])
            self.assertEqual(c.exception.status,404)
        for query in ['', '?kind=accuracy&kind=compiler','?kind=unknown','?kind=accuracy&path=/etc/passwd']:
            with self.assertRaises(ClientHTTPError) as c:self.client.request('/api/jobs/'+self.identifier+'/diagnostic-report'+query)
            self.assertEqual(c.exception.status,400)

    def test_readonly_scope_suffices_and_no_job_or_resource_state_changes(self):
        self.publish('accuracy');token=self.base.jobs.access.issue_token('alice',{'jobs:read'});client=Client({'controller':self.client.url,'token':token})
        before=copy.deepcopy(self.base.jobs.get(self.identifier));self.assertIn(report_view.CAPABILITY,client.request('/api/me')['report_view_capabilities'])
        self.assertTrue(self.view('accuracy',client)['verification']['report_bytes_verified']);self.assertEqual(before,self.base.jobs.get(self.identifier))

    def test_byte_tampering_and_rewritten_manifest_are_both_refused(self):
        self.publish('accuracy');path=self.directory/report_view.KINDS['accuracy'][0];path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(ClientHTTPError) as c:self.view('accuracy')
        self.assertEqual(c.exception.status,409)
        manifest=json.loads((self.directory/'artifact-hashes.json').read_text());manifest['files'][0]['sha256']=sha(path.read_bytes());manifest['files'][0]['size']=path.stat().st_size;atomic_json(self.directory/'artifact-hashes.json',manifest)
        with self.assertRaises(ClientHTTPError):self.view('accuracy')

    def test_links_oversized_and_invalid_json_do_not_escape_budgets(self):
        self.publish('compiler');path=self.directory/report_view.KINDS['compiler'][0];path.unlink();path.symlink_to('/etc/passwd')
        with self.assertRaises(ClientHTTPError):self.view('compiler')
        path.unlink()
        for raw in [b'{"a":1,"a":2}',b'{"n":NaN}',b'{"n":'+b'9'*100+b'}',b'{"x":'+b'['*25+b'0'+b']'*25+b'}',b' '*16385]:
            self.publish('compiler',raw=raw)
            with self.subTest(raw=raw[:50]),self.assertRaises(ClientHTTPError) as c:self.view('compiler')
            self.assertEqual(c.exception.status,409)

    def test_impossible_measurements_and_forged_capture_completeness_are_rejected(self):
        for mutate in [lambda v:v['analysis']['cores'][0]['analysis']['pipes']['RVECLD'].update(union_us=100.),
            lambda v:v['analysis']['capture'].update(capture_completeness_certified=True),
            lambda v:v['analysis']['cores'][0]['analysis'].update(events=999)]:
            value=copy.deepcopy(self.values['simulation']);mutate(value);self.publish('simulation',value)
            with self.assertRaises(ClientHTTPError):self.view('simulation')

    def test_summary_never_reads_full_trace_tensor_or_ir(self):
        self.publish('accuracy');opened=[];original=report_view.raw_file
        def read(directory,name,limit):opened.append(name);return original(directory,name,limit)
        with patch('report_view.raw_file',side_effect=read):self.view('accuracy')
        self.assertEqual(opened,['artifact-hashes.json','results/accuracy-analysis.json'])

    def test_concurrent_read_bound_is_explicit_and_does_not_create_work(self):
        self.publish('simulation');before=len(self.base.jobs.all());report_view._SLOTS.acquire();report_view._SLOTS.acquire()
        try:
            with self.assertRaises(ClientHTTPError) as c:self.view('simulation')
            self.assertEqual(c.exception.status,503)
        finally:report_view._SLOTS.release();report_view._SLOTS.release()
        self.assertEqual(len(self.base.jobs.all()),before);self.assertEqual(self.view('simulation')['summary']['capture_state'],'abnormal')

    def test_compiler_error_is_bounded_and_credentials_are_redacted(self):
        error=self.directory/'results/compiler-stderr.txt';error.write_text('password=diagnostic-admin-secret\n<img src=x onerror=alert(1)>')
        self.values['compiler']['outcome']['stderr_bytes']=error.stat().st_size
        self.publish('compiler');view=self.view('compiler');s=view['summary']['stderr_excerpt']
        self.assertNotIn('diagnostic-admin-secret',s);self.assertIn('REDACTED',s);self.assertIn('<img',s)

    def test_quarantined_archive_is_unavailable_before_any_report_file_read(self):
        self.publish('accuracy');plan=self.client.request('/api/storage/plan',{'kind':'archive','id':self.identifier,'action':'trash'})
        self.client.request('/api/storage/apply',plan['plan'])
        with patch('report_view.raw_file',side_effect=AssertionError('Quarantined report must not be opened')):
            with self.assertRaises(ClientHTTPError) as c:self.view('accuracy')
            self.assertEqual(c.exception.status,410)

    def test_nonempty_missing_compiler_error_is_not_a_complete_view(self):
        value=copy.deepcopy(self.values['compiler']);value['outcome']['stderr_bytes']=17;self.publish('compiler',value)
        with self.assertRaises(ClientHTTPError) as c:self.view('compiler')
        self.assertEqual(c.exception.status,409)


if __name__=='__main__':unittest.main()

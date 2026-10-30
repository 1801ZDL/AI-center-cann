"""Offline analysis semantics and bounded per-job diagnostic authority."""
import base64
import copy
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_diagnostic_case as builder
from lab_client import ClientError,atomic_json
from operator_profile import ARTIFACT,SCHEMA,OperatorBridge,check_provider,clock_mhz,comparison,context_summary,csv_summary,read_case,read_input,sha,validate_report
from diagnostic_case import serve


def profile(identity='slow',duration='2',device=0,clock=100):
    raw=('Name,Duration(us),Device_id,Input Shapes,Input Data Types\n_kernel,'+duration+',0,2x3,FLOAT\n').encode()
    return {**csv_summary(raw,'_kernel'),'id':identity,'physical_device':device,'clock_mhz_before':clock,'clock_mhz_after':clock,
        'correctness_claim':True,'elf_sha256_claim':'e'*64,'csv_sha256':sha(raw),'context_sha256':'c'*64}


def report():
    sources=[profile(),profile('fast','1',1,1650)]
    return {'schema':SCHEMA,'purpose':'historical_profile_comparison','sources':sources,'comparison':comparison(*sources),
        'provider':{'name':'msprof-mcp','version':'0.1.8'}}


class AnalysisSemanticsTests(unittest.TestCase):
    def test_launches_are_not_hardware_blocks_and_operator_filter_is_exact(self):
        data=b'Name,Duration(us),Device_id,Input Shapes,Input Data Types\n_kernel,1,0,2,FLOAT\nother,999,0,2,FLOAT\n_kernel,3,0,2,FLOAT\n'
        value=csv_summary(data,'_kernel');self.assertEqual(value['samples'],2);self.assertEqual(value['mean_us'],2)
        self.assertEqual(value['csv_device_ids'],[0]);self.assertIn('not per-block',value['row_semantics'])

    def test_wrong_pipe_schema_and_duplicate_or_missing_columns_reject(self):
        for data in [b'aiv_vec_ratio\n0.5\n',b'Name,Duration(us),Duration(us),Device_id\n_kernel,1,2,0\n',b'Name,Duration(us)\n_kernel,1\n']:
            with self.subTest(data=data),self.assertRaises(ValueError):csv_summary(data,'_kernel')

    def test_nonfinite_negative_empty_and_malformed_rows_reject(self):
        for value in ['nan','inf','-inf','-1','0','not-a-number']:
            with self.subTest(value=value),self.assertRaises(ValueError):profile(duration=value)
        with self.assertRaises(ValueError):csv_summary(b'Name,Duration(us),Device_id\nother,1,0\n','_kernel')
        with self.assertRaises(ValueError):csv_summary(b'Name,Duration(us),Device_id\n_kernel,1\n','_kernel')

    def test_csv_budgets_apply_before_permissive_provider_coercion(self):
        for raw in [b'x'*(2*1024**2+1),b'Name,Duration(us),Device_id\n'+b'_kernel,1,0\n'*10001]:
            with self.assertRaises(ValueError):csv_summary(raw,'_kernel')

    def test_rated_or_profiler_timer_frequency_is_not_current_core_clock(self):
        self.assertEqual(clock_mhz('Aicore Freq(MHZ): 1650\nAicore curFreq(MHZ): 100\n'),100)
        for value in ['Aicore Freq(MHZ): 1650','freq:100','Aicore curFreq(MHZ): 0','Aicore curFreq(MHZ): 100\nAicore curFreq(MHZ): 1650']:
            with self.subTest(value=value),self.assertRaises(ValueError):clock_mhz(value)

    def test_csv_device_zero_does_not_erase_physical_mapping(self):
        left=profile();right=profile('fast','1',1,1650)
        self.assertEqual(left['csv_device_ids'],right['csv_device_ids'])
        value=comparison(left,right)
        self.assertIn('different_physical_device',value['blockers']);self.assertIn('different_or_unstable_core_frequency',value['blockers'])
        self.assertIsNone(value['optimization_speedup']);self.assertEqual(value['observed_duration_ratio'],2)

    def test_same_binary_or_failed_precision_does_not_prove_optimization(self):
        left=profile();right=profile('fast','1');right['correctness_claim']=False
        value=comparison(left,right)
        self.assertIn('same_binary_does_not_establish_a_compiler_optimization',value['blockers'])
        self.assertIn('correctness_not_claimed_passed',value['blockers'])

    def test_matching_metadata_still_requires_controlled_verification(self):
        left=profile();right=profile('fast','1');right['elf_sha256_claim']='f'*64
        value=comparison(left,right);self.assertEqual(value['blockers'],[])
        self.assertEqual(value['status'],'requires_controlled_verification');self.assertIsNone(value['optimization_speedup'])

    def test_context_requires_explicit_physical_mapping_and_frequency(self):
        value={'physical_device':1,'visible_devices':'1','elf_sha256':'a'*64,'correctness':True,
            'frequency_before':'Aicore curFreq(MHZ): 1650','frequency_after':'Aicore curFreq(MHZ): 1650'}
        self.assertEqual(context_summary(json.dumps(value).encode())['physical_device'],1)
        for changes in [{'physical_device':True},{'visible_devices':'0'},{'correctness':'pass'},{'elf_sha256':'unknown'},{'frequency_after':'Aicore Freq(MHZ):1650'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):context_summary(json.dumps({**value,**changes}).encode())

    def test_input_paths_cannot_escape_or_follow_links(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as directory:
            root=Path(directory);(root/'ok').write_bytes(b'abc');(root/'link').symlink_to(root/'ok')
            self.assertEqual(read_input(root,'ok',3),b'abc')
            for name in ['/etc/passwd','../ok','a/../ok','link','bad\\name']:
                with self.subTest(name=name),self.assertRaises(ValueError):read_input(root,name,100)
            with self.assertRaises(ValueError):read_input(root,'ok',2)

    def test_mcp2_is_rejected_before_starting_the_upstream_server(self):
        with patch('importlib.metadata.version',side_effect=lambda name:'0.1.8' if name=='msprof-mcp' else '2.3.0'):
            with self.assertRaisesRegex(ValueError,'MCP1.30.0'):check_provider()

    def test_missing_optional_provider_does_not_trigger_installation(self):
        with patch('importlib.metadata.version',side_effect=importlib.metadata.PackageNotFoundError('msprof-mcp')):
            with self.assertRaisesRegex(ValueError,'isolated pinned'):check_provider()

    def test_analysis_cli_requires_explicit_central_task_context(self):
        env=dict(os.environ);env.pop('LAB_JOB_DIR',None)
        result=subprocess.run([sys.executable,str(ROOT/'operator_profile.py'),'analyze','--inputs','/not-read'],env=env,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertIn('central CPU queue',result.stderr)

    def test_report_rejects_false_speedups_ranges_ratios_and_unbounded_numbers(self):
        self.assertEqual(validate_report(report())['schema'],SCHEMA)
        for mutate in [lambda r:r['comparison'].update(optimization_speedup=2),lambda r:r['comparison'].update(observed_duration_ratio=3),
                       lambda r:r['sources'][0].update(mean_us=999),lambda r:r['sources'][0].update(samples=True),lambda r:r.update(extra=float('nan'))]:
            value=report();mutate(value)
            with self.assertRaises(ValueError):validate_report(value)


class OperatorCaseTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.DiagnosticTests();self.base.setUp();self.client=self.base.clients['alice'];self.identifier=self.base.identifier
        directory=self.base.jobs.directory(self.identifier);atomic_json(directory/ARTIFACT,report())
        index=json.loads((directory/'artifacts.json').read_text());index['files'].append({'name':ARTIFACT,'bytes':(directory/ARTIFACT).stat().st_size});atomic_json(directory/'artifacts.json',index)

    def tearDown(self):self.base.tearDown()

    def test_authenticated_report_has_hashes_and_no_experiment_authority(self):
        calls=[];original=self.client.request
        def api(path,*args,**kwargs):
            self.assertFalse(args);calls.append(path);return original(path,*args,**kwargs)
        self.client.request=api;value=read_case(self.client,self.identifier)
        self.assertEqual(value['job']['id'],self.identifier);self.assertEqual(len(calls),2)
        self.assertTrue(all(path.startswith('/api/jobs/'+self.identifier) for path in calls))
        self.assertIn('untrusted evidence',value['material_policy'])
        expected=value.pop('case_sha256');self.assertEqual(expected,sha(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()))

    def test_foreign_identity_cannot_fetch_even_the_fixed_artifact(self):
        with self.assertRaises(ClientError):read_case(self.base.clients['bob'],self.identifier)

    def test_read_only_token_can_read_without_experiment_permissions(self):
        from lab_client import Client
        token=self.base.jobs.access.issue_token('alice',{'jobs:read'})
        client=Client({'controller':self.client.url,'token':token})
        self.assertEqual(read_case(client,self.identifier)['job']['id'],self.identifier)

    def test_invalid_chunk_identity_digest_and_truncation_are_rejected(self):
        original=self.client.request
        for change in [{'name':'other.json'},{'offset':1},{'sha256':'0'*64},{'size':999999}]:
            def request(path,change=change):
                value=original(path)
                return {**value,**change} if '/artifact-chunk?' in path else value
            self.client.request=request
            with self.subTest(change=change),self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_only_fixed_archive_name_is_ever_read(self):
        directory=self.base.jobs.directory(self.identifier);index=json.loads((directory/'artifacts.json').read_text());index['files']=[v for v in index['files'] if v['name']!=ARTIFACT];atomic_json(directory/'artifacts.json',index)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)

    def test_bound_mcp_rejects_job_command_and_submission_overrides(self):
        bridge=OperatorBridge(self.client,self.identifier)
        for name,args in [('submit_job',{}),('get_diagnostic_case',{'job_id':'job-'+'f'*12}),('get_diagnostic_case',{'command':'sh'}),('get_diagnostic_case',[])]:
            with self.subTest(name=name,args=args),self.assertRaises(ClientError):bridge.call(name,args)
        messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},{'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}}]
        output=io.StringIO();serve(bridge,io.StringIO(''.join(json.dumps(v)+'\n' for v in messages)),output)
        replies=[json.loads(line)['result'] for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies[0]['tools']),1);self.assertTrue(replies[0]['tools'][0]['annotations']['readOnlyHint'])
        self.assertFalse(replies[1]['isError']);self.assertEqual(json.loads(replies[1]['content'][0]['text'])['schema'],'ascend-lab-operator-case/v1')

    def test_profile_credentials_are_redacted_from_workload_report(self):
        directory=self.base.jobs.directory(self.identifier);value=report();value['note']='password='+self.base.passwords['alice'];atomic_json(directory/ARTIFACT,value)
        result=read_case(self.client,self.identifier);self.assertNotIn(self.base.passwords['alice'],json.dumps(result));self.assertIn('REDACTED',result['report']['note'])

    def test_excessive_report_nesting_fails_without_crashing_the_mcp(self):
        value=report();nested='bounded'
        for _ in range(40):nested={'next':nested}
        value['extra']=nested;directory=self.base.jobs.directory(self.identifier);atomic_json(directory/ARTIFACT,value)
        with self.assertRaises(ClientError):read_case(self.client,self.identifier)


if __name__=='__main__':unittest.main()

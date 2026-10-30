"""Legacy command declarations must bind reservations, retries and collected output."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from archive_api import manifest
from executor import finish,run_command
from io_policy import MIB,META
from jobs import ROOT,JobStore,JobError,read_json


class LegacyArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='legacy-artifact-test-')
        self.root=Path(self.temp.name)
        self.config={'username':'legacy-test-admin','container':'zdl',
            'scheduler':{'workspace_root':str(self.root),
                         'io_limits':{'file_mib':256,'input_mib':512,'artifact_mib':512}}}
        self.jobs=JobStore(self.root,self.config)

    def tearDown(self):self.temp.cleanup()

    def request(self,key='one',budget=None,**extra):
        return {'argv':[sys.executable,'-c','print(42)'],'cwd':str(self.root),'idempotency_key':key,
                **({'parameters':{'artifacts':budget}} if budget is not None else {}),**extra}

    def command(self,budget,output=None,key='one'):
        public,_=self.jobs.submit(self.request(key=key,budget=budget,**({'output':str(output)} if output else {})))
        job=self.jobs.get(public['id']);job['allocation']={'cpu_ids':[min(os.sched_getaffinity(0))],'npu_ids':[]}
        # Keep executor coverage independent of submission's normalization bug.
        job['artifact_limits']=budget
        folder=self.jobs.directory(job['id']);(folder/'results').mkdir()
        (folder/'stdout.log').write_text('command finished\n')
        return job,folder

    def test_explicit_budget_is_persisted_and_reserved_once(self):
        budget={'file_mib':8,'total_mib':16};request=self.request(budget=budget)
        job,reused=self.jobs.submit(request);self.assertFalse(reused)
        self.assertEqual(job['artifact_limits'],budget)
        self.assertEqual(job['parameters'],{'artifacts':budget})
        again,reused=self.jobs.submit(request);self.assertTrue(reused);self.assertEqual(again['id'],job['id'])
        self.assertEqual(self.jobs.storage.summary(self.jobs.access.local)['personal_bytes']['archive'],16*MIB+META)
        restarted=JobStore(self.root,self.config)
        self.assertEqual(restarted.get(job['id'])['artifact_limits'],budget)

    def test_same_key_with_different_file_or_total_budget_is_rejected(self):
        self.jobs.submit(self.request(budget={'file_mib':8,'total_mib':16}))
        for budget in [{'file_mib':4,'total_mib':16},{'file_mib':8,'total_mib':24}]:
            with self.subTest(budget=budget),self.assertRaises(JobError) as caught:
                self.jobs.submit(self.request(budget=budget))
            self.assertEqual(caught.exception.status,409)
        self.assertEqual(len(self.jobs.all()),1)

    def test_omitted_empty_and_explicit_default_keep_same_retry(self):
        job,_=self.jobs.submit(self.request())
        for parameters in [{},{'artifacts':{'file_mib':64,'total_mib':128}}]:
            again,reused=self.jobs.submit(self.request(parameters=parameters))
            self.assertTrue(reused);self.assertEqual(again['id'],job['id'])
        self.assertEqual(job['artifact_limits'],{'file_mib':64,'total_mib':128})

    def test_invalid_or_unknown_legacy_parameters_are_rejected_before_storage(self):
        invalid=[None,[],True,{'unexpected':1},{'artifacts':{'file_mib':8,'total_mib':4}},
                 {'artifacts':{'file_mib':True,'total_mib':8}}]
        for parameters in invalid:
            with self.subTest(parameters=parameters),self.assertRaises(JobError) as caught:
                self.jobs.submit(self.request(parameters=parameters))
            self.assertEqual(caught.exception.status,400)
        self.assertEqual(self.jobs.all(),[])
        self.assertEqual(list(self.jobs.root.iterdir()),[])

    def test_oversized_output_fails_but_original_and_downloadable_logs_remain(self):
        job,folder=self.command({'file_mib':1,'total_mib':2})
        oversized=folder/'results/oversized.bin'
        with oversized.open('wb') as f:f.truncate(MIB+1)
        finish(folder,job,{'status':'succeeded','exit_code':0})
        result=read_json(folder/'result.json');self.assertEqual(result['status'],'failed')
        self.assertEqual(result['failure_stage'],'artifact_budget');self.assertEqual(result['execution_status'],'succeeded')
        self.assertEqual(oversized.stat().st_size,MIB+1)
        archived=manifest(folder,{**job,'state':'failed'})
        names={f['name'] for f in archived['files']}
        self.assertIn('stdout.log',names);self.assertIn('result.json',names)
        self.assertNotIn('results/oversized.bin',names)

    def test_combined_outputs_cannot_exceed_total_budget(self):
        job,folder=self.command({'file_mib':1,'total_mib':1})
        for name in ['a.bin','b.bin']:
            with (folder/'results'/name).open('wb') as f:f.truncate(3*MIB//4)
        finish(folder,job,{'status':'succeeded','exit_code':0})
        self.assertEqual(read_json(folder/'result.json')['failure_stage'],'artifact_budget')
        self.assertTrue(all((folder/'results'/n).exists() for n in ['a.bin','b.bin']))

    def test_file_count_limit_counts_real_files_including_existing_metadata(self):
        for count,state in [(1997,'succeeded'),(1998,'failed')]:
            with self.subTest(output_files=count):
                job,folder=self.command({'file_mib':1,'total_mib':1},key='count-'+str(count))
                for number in range(count):(folder/'results'/str(number)).touch()
                # request.json, stdout.log and result.json are the three actual
                # metadata files. Absent optional receipt names consume no slots.
                finish(folder,job,{'status':'succeeded','exit_code':0})
                self.assertEqual(read_json(folder/'result.json')['status'],state)
                entries=read_json(folder/'artifacts.json')['files']
                self.assertEqual(len(entries),2000 if state=='succeeded' else 3)
                self.assertEqual(len(list((folder/'results').iterdir())),count)

    def test_valid_larger_declared_archive_can_exceed_old_128mib_copy_ceiling(self):
        output=self.root/'personal';output.mkdir()
        job,folder=self.command({'file_mib':256,'total_mib':256},output)
        payload=folder/'results/large.bin'
        with payload.open('wb') as f:f.truncate(129*MIB)
        finish(folder,job,{'status':'succeeded','exit_code':0})
        result=read_json(folder/'result.json');self.assertEqual(result['status'],'succeeded')
        self.assertEqual((output/job['id']/'results/large.bin').stat().st_size,129*MIB)

    def test_source_snapshot_must_fit_the_declared_single_file_budget(self):
        source=self.root/'large-source.py';source.write_bytes(b'#'+b'x'*MIB)
        with self.assertRaises(JobError):
            self.jobs.submit(self.request(budget={'file_mib':1,'total_mib':2},argv=[sys.executable,str(source)]))
        self.assertEqual(self.jobs.all(),[]);self.assertEqual(list(self.jobs.root.iterdir()),[])

    def test_log_capture_fits_single_file_cap_and_marks_truncation(self):
        job,folder=self.command({'file_mib':1,'total_mib':2})
        job['argv']=[sys.executable,'-c','import os;os.write(1,b"x"*(2*1024**2))']
        with patch('executor.fingerprint',return_value={'scope':'CPU test'}):run_command(folder,job)
        self.assertEqual(read_json(folder/'result.json')['status'],'succeeded')
        log=(folder/'stdout.log').read_bytes();self.assertLessEqual(len(log),MIB)
        self.assertIn(b'log limit reached',log)


if __name__=='__main__':unittest.main()

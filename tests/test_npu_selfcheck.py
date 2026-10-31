"""Exact card constraints and deterministic reference contracts; no hardware IO."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import npu_selfcheck as ut
import placement
import test_npu_device as devices
from jobs import JobError,JobStore,atomic_json
from lab_client import Client,ClientError
from server import ThreadingHTTPServer,handler_class
from maintenance import require_features
from deployment import CORE,DeployError


def lock():
    return {'python':'3.10.12','image_sha256':'sha256:'+'a'*64,'npu_name':'Ascend-test',
            'packages':{'torch':'2.6.0','torch_npu':'2.6.0'}}


def report():
    return {'schema':ut.SCHEMA,'driver_sha256':ut.source_sha(),'reference_sha256':ut.reference_sha(),
        'runtime':{'backend':'npu','device_index':0,'device_count':1},
        'checks':[{'name':c['name'],'dtype':c['dtype'],'values':list(c['expected'])} for c in ut.reference()],
        'timing':{'method':'npu-events','workload':'mm512_float16','warmup':ut.WARMUP,'status':'collected',
                  'rounds_us':[[20+i for i in range(10)] for _ in range(3)]}}


class ReferenceTests(unittest.TestCase):
    def test_reference_uses_independent_exact_integer_arithmetic(self):
        cases=ut.reference();self.assertEqual(len(cases),6);self.assertEqual(sum(len(c['expected']) for c in cases),2832)
        for case in cases[3:]:
            for i in range(16):
                for j in range(16):
                    expected=0
                    for k in range(16):expected+=case['a'][i*16+k]*case['b'][k*16+j]
                    self.assertEqual(case['expected'][i*16+j],expected)
            self.assertTrue(all(abs(x)<=64 for x in case['expected']))
        self.assertEqual(cases[1]['expected'][123],3*cases[1]['a'][123]+2*cases[1]['b'][123])
    def test_all_values_recomputed_with_performance_still_unverified(self):
        result=ut.validate_report(report());self.assertTrue(result['precision_passed'])
        self.assertEqual(result['performance']['samples'],30)
        self.assertEqual(result['performance']['status'],'unverified')
    def test_one_error_nan_inf_are_preserved_as_failures_not_loosened_tolerance(self):
        for value in [1e10,'nan','inf','-inf']:
            data=report();data['checks'][1]['values'][5]=value;result=ut.validate_report(data)
            self.assertFalse(result['precision_passed']);self.assertEqual(result['checks'][1]['mismatches'],1)
            self.assertEqual(result['checks'][1]['examples'][0]['actual'],value)
    def test_changed_reference_driver_shape_dtype_order_and_cpu_fallback_rejected(self):
        candidates=[]
        for field in ['driver_sha256','reference_sha256']:
            data=report();data[field]='0'*64;candidates.append(data)
        data=report();data['runtime']['backend']='cpu';candidates.append(data)
        data=report();data['checks'].reverse();candidates.append(data)
        data=report();data['checks'][0]['values'].pop();candidates.append(data)
        data=report();data['checks'][0]['dtype']='int32';candidates.append(data)
        data=report();data['checks'][0]['values'][0]=True;candidates.append(data)
        for data in candidates:
            with self.assertRaises(ValueError):ut.validate_report(data)
    def test_bad_or_missing_timing_never_claims_performance_pass(self):
        for value in [float('nan'),0,-1,True,'20']:
            data=report();data['timing']['rounds_us'][0][0]=value
            with self.assertRaises(ValueError):ut.validate_report(data)
        data=report();data['timing'].update(status='unavailable',rounds_us=[])
        self.assertEqual(ut.validate_report(data)['performance']['status'],'unverified')
    def test_extreme_finite_timings_do_not_publish_infinite_statistics(self):
        data=report();data['timing']['rounds_us']=[[5e-324]*10,[1.7e308]*10,[20]*10]
        value=ut.validate_report(data)
        self.assertIsNone(value['performance']['round_median_ratio'])
        self.assertIn('statistic_warning',value['performance']);self.assertEqual(value['performance']['status'],'unverified')
        json.dumps(value,allow_nan=False)
        data=report();data['timing']['rounds_us'][0][0]=10**1000
        with self.assertRaises(ValueError):ut.validate_report(data)
    def test_daily_key_stable_but_card_environment_and_day_are_distinct(self):
        args=('input-'+'1'*24,lock(),'worker-'+'1'*12,0,'2026-10-31')
        original=ut.request_for(*args);self.assertEqual(original,ut.request_for(*args))
        for change in [(3,1),(4,'2026-11-01'),(2,'worker-'+'2'*12)]:
            modified=list(args);modified[change[0]]=change[1]
            self.assertNotEqual(original['idempotency_key'],ut.request_for(*modified)['idempotency_key'])
        self.assertEqual(original['placement']['device_ids'],[0]);self.assertEqual(original['parameters']['input_mode'],'readonly')
    def test_invalid_plan_does_not_start_a_workload(self):
        for bad in [{}, {'packages':None}, {**lock(),'npu_name':'UNKNOWN'}, {**lock(),'image_sha256':'image:latest'}]:
            with self.assertRaises(ValueError):ut.request_for('input-'+'1'*24,bad,'worker-'+'1'*12,0,'2026-10-31')
        with self.assertRaises(ValueError):ut.run()
    def test_installed_personal_entry_has_no_controller_or_torch_dependency(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
            target=Path(temp)/'client'
            subprocess.run([sys.executable,str(ROOT/'install-client'),'--prefix',str(target)],check=True,capture_output=True)
            self.assertFalse((target/'server.py').exists());self.assertFalse((target/'environment_probe.py').exists())
            self.assertEqual((target/ut.DRIVER).read_bytes(),(ROOT/ut.DRIVER).read_bytes())
            result=subprocess.run([sys.executable,str(target/ut.DRIVER),'--help'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0);self.assertIn('submit',result.stdout)
            result=subprocess.run([sys.executable,str(target/ut.DRIVER),'--run'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('managed non-root',result.stderr)


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.fixture=devices.NpuDeviceTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.jobs=self.fixture.jobs;self.root=self.fixture.root
        self.fixture.npu={**self.fixture.npu,'device_ids':[0,1],'visible_ids':{'0':0,'1':1}}
        self.nodes=[]
        for old in self.fixture.nodes:
            profile=copy.deepcopy(self.jobs.nodes.node(old)['profile']);profile['name']+='-two-cards';profile['npu']=self.fixture.npu
            profile['templates']+=['npu_access_probe','accuracy_v1']
            node,key=self.jobs.nodes.enroll(profile);self.nodes.append(node);self.beat(node)
    def beat(self,node,busy=None):
        reading=self.fixture.reading();reading['devices']=[{'id':i,'health':'OK','utilization_pct':0,'process_count':int(i==busy)} for i in [0,1]]
        self.fixture.beat(node,reading)
    def request(self,device=1,node=None,template='npu_probe',key='exact-target'):
        return {'template':template,'environment':'worker-python','idempotency_key':key,
                'placement':{'node_id':node or self.nodes[1],**({'device_ids':[device]} if template=='npu_probe' else {})}}
    def test_exact_card_does_not_fall_back_to_idle_peer_or_other_node(self):
        job,_=self.jobs.submit(self.request(),self.jobs.access.user('alice'))
        self.assertIsNone(self.jobs.nodes.claim(self.nodes[0])['grant'])
        self.beat(self.nodes[1],busy=1);self.assertIsNone(self.jobs.nodes.claim(self.nodes[1])['grant'])
        self.assertFalse(self.jobs.attempts(job['id']));self.assertEqual(self.jobs.get(job['id'])['state'],'queued')
        self.beat(self.nodes[1]);grant=self.jobs.nodes.claim(self.nodes[1])['grant']
        self.assertEqual(grant['allocation']['npu_ids'],[1]);self.assertEqual(grant['node_id'],self.nodes[1])
    def test_two_entries_still_cannot_reserve_same_physical_card(self):
        first,_=self.jobs.submit(self.request(node=self.nodes[0]),self.jobs.access.user('alice'))
        second,_=self.jobs.submit(self.request(node=self.nodes[1],key='other-owner'),self.jobs.access.user('bob'))
        self.assertIsNotNone(self.jobs.nodes.claim(self.nodes[0])['grant'])
        self.assertIsNone(self.jobs.nodes.claim(self.nodes[1])['grant']);self.assertFalse(self.jobs.attempts(second['id']))
    def test_replay_reopen_and_unknown_node_keep_constraints_and_attempt(self):
        request=self.request();job,_=self.jobs.submit(request,self.jobs.access.user('alice'));grant=self.jobs.nodes.claim(self.nodes[1])['grant']
        self.assertTrue(self.jobs.submit(request,self.jobs.access.user('alice'))[1])
        with self.assertRaises(JobError):self.jobs.submit(self.request(device=0),self.jobs.access.user('alice'))
        self.jobs.access.close();self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        self.addCleanup(self.jobs.access.close)
        self.assertEqual(self.jobs.nodes.claim(self.nodes[1])['grant'],grant)
        self.assertEqual(self.jobs.get(job['id'])['placement'],request['placement'])
        future,_=self.jobs.submit(self.request(node='worker-'+'f'*12,key='future-node'),self.jobs.access.user('bob'))
        self.assertIsNone(self.jobs.nodes.claim(self.nodes[0])['grant']);self.assertFalse(self.jobs.attempts(future['id']))
    def test_bad_constraints_and_local_command_are_rejected(self):
        for target in [{},{'node_id':'../../tmp'},{'node_id':self.nodes[0],'device_ids':[True]},
                       {'node_id':self.nodes[0],'device_ids':[0,1]},{'node_id':self.nodes[0],'uid':0}]:
            with self.assertRaises(JobError):self.jobs.submit({**self.request(),'placement':target},self.jobs.access.user('alice'))
        with self.assertRaises(JobError):self.jobs.submit({'argv':['python','-c','pass'],'placement':{'node_id':self.nodes[0]}})
    def test_older_controller_cannot_drop_known_constraints_on_activation(self):
        self.jobs.submit(self.request(),self.jobs.access.user('alice'))
        with self.assertRaisesRegex(DeployError,'placement'):
            require_features(self.root,'controller',{'files':{name:{} for name in CORE if name!='placement.py'}})
    def test_personal_daily_submit_uses_real_http_input_and_original_ids_on_retry(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),{'username':'operator','password':'offline-only'},self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        client=Client({'controller':'http://127.0.0.1:'+str(server.server_port),'username':'alice','password':self.fixture.passwords['alice']})
        a=ut.submit(client,lock(),self.nodes[1],[0,1],'2026-10-31',512)
        b=ut.submit(client,lock(),self.nodes[1],[0,1],'2026-10-31',512)
        self.assertEqual([x['job_id'] for x in a['entries']],[x['job_id'] for x in b['entries']])
        self.assertTrue(all(x['reused'] for x in b['entries']))
        self.assertEqual(len(self.jobs.all()),4)
        for entry in a['entries']:
            job=self.jobs.get(entry['job_id']);self.assertEqual(job['placement']['device_ids'],[entry['device_id']])
            self.assertEqual(job['parameters']['lock'],lock());self.assertEqual(job['resources']['npu'],1)
            self.assertEqual(job['input']['files'][0]['sha256'],ut.source_sha());self.assertFalse(self.jobs.attempts(job['id']))
        path=self.root/'bound.json';atomic_json(path,ut.request_for(self.jobs.get(a['entries'][0]['job_id'])['input']['id'],lock(),self.nodes[1],0,'2026-10-31',512))
        frozen=placement.bound_workflow(client,path,'worker-python');self.assertEqual(frozen['placement']['device_ids'],[0])

    def archived_case(self):
        """Synthetic capture in a real personal HTTP archive; never a device pass."""
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),{'username':'operator','password':'offline-only'},self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        url='http://127.0.0.1:'+str(server.server_port)
        clients={owner:Client({'controller':url,'username':owner,'password':self.fixture.passwords[owner]}) for owner in ['alice','bob']}
        submitted=ut.submit(clients['alice'],lock(),self.nodes[1],[1],'2026-10-31',512)
        job=self.jobs.get(submitted['entries'][0]['job_id']);directory=self.jobs.directory(job['id'])
        (directory/'results/_lab').mkdir(parents=True)
        job.update(state='failed',execution_backend='managed-docker',node_id=self.nodes[1],attempt_id='attempt-'+'a'*32,
            allocation={'npu_ids':[1]},result={'status':'failed','termination_confirmed':True,'execution_cleanup_confirmed':True})
        receipt={'schema':'ascend-lab-environment/v1','locked':True,'workload_started':True,'probe_exit_code':0,
            'observed':{'image_sha256':lock()['image_sha256']},'device_access':{'schema':'ascend-lab-device-access/v1','status':'passed','passed':True,
                'requested_device_ids':[1],'opened_device_ids':[1],'physical_device_files':[1],'errors':[],
                'uid':23001,'euid':23001,'cap_effective':'0000000000000000','cap_permitted':'0000000000000000','no_new_privileges':'1'}}
        atomic_json(directory/ut.REPORT,report());atomic_json(directory/'results/_lab/environment.json',receipt)
        atomic_json(directory/'artifacts.json',{'files':[{'name':n,'bytes':(directory/n).stat().st_size} for n in [ut.REPORT,'results/_lab/environment.json']]})
        with self.jobs.transaction() as db:self.jobs.write(db,job)
        return clients,job,directory
    def test_personal_inspection_recomputes_every_value_and_does_not_certify_hardware(self):
        clients,job,directory=self.archived_case();case=ut.inspect_case(clients['alice'],job['id'])
        self.assertTrue(case['comparison']['precision_passed']);self.assertTrue(case['protected_environment_matched'])
        self.assertFalse(case['hardware_health_certified']);self.assertFalse(case['performance_qualified'])
        self.assertEqual(case['state'],'failed')  # Original task outcome remains authoritative.
        with self.assertRaises(ClientError):ut.inspect_case(clients['bob'],job['id'])
    def test_inspection_preserves_nonfinite_error_and_refuses_unknown_reference(self):
        clients,job,directory=self.archived_case();data=report();data['checks'][0]['values'][0]='nan'
        atomic_json(directory/ut.REPORT,data);case=ut.inspect_case(clients['alice'],job['id'])
        self.assertFalse(case['comparison']['precision_passed']);self.assertEqual(case['comparison']['checks'][0]['nonfinite'],1)
        data['reference_sha256']='f'*64;atomic_json(directory/ut.REPORT,data)
        with self.assertRaises(ClientError):ut.inspect_case(clients['alice'],job['id'])
    def test_inspection_cannot_transfer_capture_to_another_card_or_cpu_task(self):
        clients,job,directory=self.archived_case();job['placement']['device_ids']=[0]
        with self.jobs.transaction() as db:self.jobs.write(db,job)
        with self.assertRaises(ClientError):ut.inspect_case(clients['alice'],job['id'])


if __name__=='__main__':unittest.main()

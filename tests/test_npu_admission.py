"""Management identity, freshness, attempt binding and fail-closed launch gates."""
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,read_json
from npu_admission import HostBroker,request_receipt,validate_receipt,selected_reading,RECEIPT_TTL,SOCKET_NAME
from executor import run_command


class NpuAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-npu-');self.root=Path(self.temp.name)
        self.job={'id':'job-npu-test','attempt_id':'attempt-'+'a'*32,'node_id':'node-test','_token':secrets.token_hex(24),
                  '_environment':{'uid':os.getuid(),'type':'docker'},'allocation':{'npu_ids':[0]}}
        self.devices=[{'id':0,'health':'OK','utilization_pct':0},{'id':1,'health':'OK','utilization_pct':100}]
        self.probe=Mock(return_value=(self.devices,[{'device_id':1,'pid':123,'command':'another-user-private-command'}]))

    def tearDown(self):self.temp.cleanup()

    def test_real_socket_receipt_binds_execution_and_hides_other_processes_and_token(self):
        with HostBroker(self.root,self.job,self.probe):
            receipt=request_receipt(self.root,self.job)
            self.assertEqual(receipt['devices'],[{'id':0,'health':'OK','utilization_pct':0,'process_count':0}])
            self.assertEqual(receipt['query_uid'],os.getuid())
            validate_receipt(receipt,self.job)
            for private in [self.job['_token'],'another-user-private-command','"pid"']:
                self.assertNotIn(private,json.dumps(receipt))
        self.probe.assert_called_once();self.assertFalse((self.root/SOCKET_NAME).exists())

    def test_wrong_token_attempt_or_peer_identity_never_triggers_query(self):
        with HostBroker(self.root,self.job,self.probe):
            for change in [{'_token':'wrong-token'},{'attempt_id':'attempt-'+'b'*32}]:
                with self.assertRaises(RuntimeError):request_receipt(self.root,{**self.job,**change})
            self.probe.assert_not_called()
            self.assertTrue(request_receipt(self.root,self.job)['ok'])
        other={**self.job,'_environment':{'uid':os.getuid()+1,'type':'docker'}}
        probe=Mock(return_value=(self.devices,[]))
        with HostBroker(self.root,other,probe):
            with self.assertRaises(RuntimeError):request_receipt(self.root,other)
        probe.assert_not_called()

    def test_one_management_check_is_not_replayed(self):
        with HostBroker(self.root,self.job,self.probe):
            request_receipt(self.root,self.job)
            with self.assertRaises(RuntimeError):request_receipt(self.root,self.job)
        self.probe.assert_called_once()

    def test_busy_unhealthy_unknown_and_unreadable_states_refuse_admission(self):
        cases=[([],[]),([{'id':0,'health':'Warning','utilization_pct':0}],[]),
               ([{'id':0,'health':'OK','utilization_pct':1}],[]),
               ([{'id':0,'health':'OK','utilization_pct':0}],[{'device_id':0}])]
        for observation in cases:
            with self.subTest(observation=observation),HostBroker(self.root,self.job,Mock(return_value=observation)):
                with self.assertRaises(RuntimeError):request_receipt(self.root,self.job)
        with HostBroker(self.root,self.job,Mock(side_effect=RuntimeError('query failed'))):
            with self.assertRaises(RuntimeError):request_receipt(self.root,self.job)

    def test_receipt_expiry_wrong_execution_and_malformed_device_are_rejected(self):
        with HostBroker(self.root,self.job,self.probe):receipt=request_receipt(self.root,self.job)
        changes=[{'issued_monotonic':time.monotonic()-RECEIPT_TTL-1},{'issued_monotonic':time.monotonic()+10},
                 {'job_id':'job-other'},{'attempt_id':'attempt-'+'b'*32},{'node_id':'node-other'},
                 {'devices':[{'id':1,'health':'OK','utilization_pct':0}]},{'devices':['bad']},
                 {'devices':[{'id':0,'health':'OK','utilization_pct':0,'process_count':1}]}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(RuntimeError):validate_receipt({**receipt,**change},self.job)
        with self.assertRaises(RuntimeError):validate_receipt({**receipt,'nonce':'wrong'},self.job,'expected')

    def test_preexisting_endpoint_is_preserved_and_missing_broker_has_no_fallback(self):
        path=self.root/SOCKET_NAME;path.write_text('must not replace')
        with self.assertRaises(OSError):
            with HostBroker(self.root,self.job,self.probe):pass
        self.assertEqual(path.read_text(),'must not replace');path.unlink()
        with self.assertRaises(RuntimeError):request_receipt(self.root,self.job)
        self.probe.assert_not_called()

    def test_failed_management_gate_never_creates_user_process(self):
        cpu=min(os.sched_getaffinity(0))
        job={**self.job,'environment':'zdl-mxh-dev','resources':{'cpu':1},'argv':['python','never-run.py'],
             'allocation':{'npu_ids':[0],'cpu_ids':[cpu]}}
        with patch('executor.fingerprint',return_value={}),patch('executor.os.access',return_value=True),\
             patch('npu_admission.request_receipt',side_effect=RuntimeError('admission refused')),\
             patch('executor.subprocess.Popen') as process:
            run_command(self.root,job)
        process.assert_not_called()
        result=read_json(self.root/'result.json');self.assertEqual(result['status'],'failed')
        self.assertIn('admission refused',result['reason'])

    def test_supervisor_does_not_report_workload_stopped_after_ambiguous_docker_failure(self):
        from executor import supervisor
        job={**self.job,'timeout_seconds':120}
        with patch('npu_admission.HostBroker') as broker,patch('executor.supervise_docker',side_effect=RuntimeError('channel lost')),\
             patch('executor.finish') as finish:
            with self.assertRaises(RuntimeError):supervisor(self.root,job)
        finish.assert_not_called();broker.return_value.__exit__.assert_called_once()


if __name__=='__main__':unittest.main()

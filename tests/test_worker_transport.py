"""A lost controller response must not abandon or replace a known attempt."""
import io
from pathlib import Path
import sys
import urllib.error
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_nodes as builder
from lab_client import Client,ClientError,ClientUnavailable
from worker import Worker,NodeClient,ControllerUnavailable


class ClientTransportTests(unittest.TestCase):
    def test_network_response_failure_is_typed_without_exposing_its_url(self):
        client=Client({'controller':'http://127.0.0.1:18765','token':'fixture-node-token'})
        client.opener.open=Mock(side_effect=urllib.error.URLError('private diagnostic text'))
        with self.assertRaises(ClientUnavailable) as result:client.request('/api/worker/update',{})
        self.assertIsInstance(result.exception,ClientError);self.assertNotIn('private diagnostic',str(result.exception))

    def test_node_transport_and_http_authorization_failure_are_distinct(self):
        client=NodeClient({'controller':'http://127.0.0.1:18765','node_key':'fixture-node-token'})
        client.client.request=Mock(side_effect=ClientUnavailable('missing response'))
        with self.assertRaisesRegex(ControllerUnavailable,'original attempt retained'):client.call('update',{})
        client.client.request.side_effect=ClientError('Controller HTTP 403')
        with self.assertRaises(RuntimeError) as result:client.call('update',{})
        self.assertNotIsInstance(result.exception,ControllerUnavailable)


class WorkerTransportTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.NodeTests();self.base.setUp();self.jobs=self.base.jobs
        self.job=self.base.submit();self.grant=self.jobs.nodes.claim(self.base.a)['grant']
        self.worker=object.__new__(Worker);self.worker.root=self.base.runtime/'worker';self.worker.config={'node_id':self.base.a,'physical_id':self.jobs.backend.physical_id}
        self.worker.client=Mock();self.worker.finish_attempt=Mock()
        self.engine=Mock();self.engine.state={'stage':'running'}
        self.engine.control=self.base.runtime/'engine-control';self.engine.control.mkdir()
        self.engine.preview.return_value={}
        self.engine.inspect.side_effect=AssertionError('Transport loss must not enter local-fault handling')
        self.jobs.nodes.authorize_start(self.base.a,self.base.auth(self.grant));self.jobs.nodes.update(self.base.a,{**self.base.auth(self.grant),'running':True})

    def tearDown(self):self.base.tearDown()

    def run_worker(self):
        with patch('worker.DockerAttempt',return_value=self.engine),patch('worker.STOP.wait',return_value=False):self.worker.attempt(self.grant)

    def test_running_update_loss_keeps_supervision_and_never_marks_attention(self):
        updates=[]
        def call(action,body):
            self.assertEqual(action,'update');updates.append(body)
            if body.get('running') and len([v for v in updates if v.get('running')])==1:raise ControllerUnavailable('restart window')
            return self.jobs.nodes.update(self.base.a,body)
        self.worker.client.call.side_effect=call;result={'status':'succeeded'};self.engine.tick.side_effect=[None,None,result]
        before=self.jobs.get(self.job['id']);self.run_worker();after=self.jobs.get(self.job['id'])
        self.assertEqual(after['state'],'running');self.assertEqual(after['attempt_id'],before['attempt_id']);self.assertEqual(after['allocation'],before['allocation'])
        self.assertFalse(any(v.get('attention') for v in updates));self.engine.inspect.assert_not_called()
        self.worker.finish_attempt.assert_called_once_with(self.engine,result);self.assertEqual(self.engine.tick.call_count,3)

    def test_local_timeout_is_still_observed_while_control_channel_is_down(self):
        def call(action,body):
            if body.get('running'):raise ControllerUnavailable('controller offline')
            return self.jobs.nodes.update(self.base.a,body)
        self.worker.client.call.side_effect=call;result={'status':'timed_out'};self.engine.tick.side_effect=[None,result]
        self.run_worker();self.worker.finish_attempt.assert_called_once_with(self.engine,result)
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_prestart_response_loss_does_not_infer_preflight_failure(self):
        self.engine.state={'stage':'new'};self.worker.client.call.side_effect=ControllerUnavailable('no initial response')
        with self.assertRaises(ControllerUnavailable):self.run_worker()
        self.worker.finish_attempt.assert_not_called();self.engine.start.assert_not_called();self.engine.inspect.assert_not_called();self.engine.save.assert_not_called()
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_result_transfer_loss_retains_original_attempt_without_a_fault_update(self):
        self.worker.client.call.side_effect=lambda action,body:self.jobs.nodes.update(self.base.a,body)
        self.engine.tick.return_value={'status':'succeeded'};self.worker.finish_attempt.side_effect=ControllerUnavailable('lost completion response')
        with self.assertRaises(ControllerUnavailable):self.run_worker()
        self.engine.inspect.assert_not_called();self.assertEqual(self.jobs.get(self.job['id'])['state'],'running')
        self.assertEqual(len(self.jobs.attempts(self.job['id'])),1)


if __name__=='__main__':unittest.main()

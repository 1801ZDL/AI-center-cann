"""Sanitized, finite read advice crosses the personal client/MCP boundary.

Pure mocked transport: no controller launch, network, model or task execution.
"""
import io,json,sys,tempfile,unittest,urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,ClientHTTPError,atomic_json
from mcp_bridge import Bridge,BridgeError,BridgeReadBusy,serve


class ReadRetryTests(unittest.TestCase):
    def client_error(self,values,status=503,body=None):
        client=Client({'controller':'http://127.0.0.1:18765','token':'explicit-test-token'})
        headers=Message()
        for value in values:headers['Retry-After']=value
        remote=urllib.error.HTTPError('http://untrusted.test/private?secret=never-return',status,'never-return',headers,io.BytesIO(b'never-return'))
        client.opener=Mock();client.opener.open.side_effect=remote
        with self.assertRaises(ClientHTTPError) as found:client.request('/api/jobs/job-'+'a'*12,body)
        self.assertEqual(client.opener.open.call_count,1);self.assertEqual(str(found.exception),'Controller HTTP '+str(status))
        self.assertNotIn('never-return',repr(found.exception));self.assertTrue(remote.fp is None or remote.fp.closed)
        return found.exception
    def test_get_retains_only_finite_numeric_retry_advice_without_replaying_request(self):
        error=self.client_error(['1']);self.assertEqual(error.status,503);self.assertEqual(error.retry_after_seconds,1)
        self.assertEqual(self.client_error(['60']).retry_after_seconds,60)
    def test_ambiguous_dates_invalid_and_large_retry_values_are_discarded(self):
        for values in [[],['1','2'],['0'],['61'],['-1'],['1.5'],['Wed, 21 Oct 2015 07:28:00 GMT'],['secret'],['9'*100]]:
            with self.subTest(values=values):self.assertIsNone(self.client_error(values).retry_after_seconds)
    def test_post_and_other_http_statuses_do_not_get_read_retry_metadata(self):
        self.assertIsNone(self.client_error(['1'],body={'idempotency_key':'original'}).retry_after_seconds)
        self.assertIsNone(self.client_error(['1'],status=401).retry_after_seconds)
        self.assertIsNone(ClientHTTPError(503,retry_after_seconds=True).retry_after_seconds)
    def test_mcp_read_busy_keeps_original_state_then_resumes_same_job_without_submission(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);ident='job-'+'a'*12
            atomic_json(path/'session.json',{'run_id':'explicit-retry-fixture','environment':'host-python','identity':'alice'})
            bridge=Bridge(path/'session.json');atomic_json(bridge.state_path,{'jobs':[ident]});before=bridge.state_path.read_bytes()
            client=Mock();client.request.side_effect=[{'id':'alice'},ClientHTTPError(503,retry_after_seconds=1),
                                                       {'id':'alice'},{'job':{'id':ident,'state':'running'}}]
            request=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_job','arguments':{'job_id':ident}}})+'\n'
            with patch('mcp_bridge.Client.from_profile',return_value=client):
                output=io.StringIO();serve(bridge,io.StringIO(request),output);result=json.loads(output.getvalue())['result']
                value=json.loads(result['content'][0]['text']);self.assertTrue(result['isError'])
                self.assertEqual((value['http_status'],value['retry_after_seconds'],value['retry_scope']),(503,1,'same_read'))
                self.assertEqual(bridge.state_path.read_bytes(),before)
                output=io.StringIO();serve(bridge,io.StringIO(request),output);result=json.loads(output.getvalue())['result']
                self.assertFalse(result['isError']);value=json.loads(result['content'][0]['text']);self.assertEqual((value['id'],value['state']),(ident,'running'))
            self.assertEqual(bridge.state_path.read_bytes(),before);self.assertEqual(client.request.call_count,4)
            self.assertTrue(all(len(call.args)<2 or call.args[1] is None for call in client.request.call_args_list))
    def test_failed_workflow_missing_result_is_distinct_from_busy_or_missing_success_evidence(self):
        for state,status in [('failed',404),('succeeded',404),('failed',503)]:
            with self.subTest(state=state,status=status),tempfile.TemporaryDirectory() as folder:
                path=Path(folder);ident='job-'+'b'*12
                atomic_json(path/'session.json',{'run_id':'explicit-result-fixture','environment':'worker-python',
                    'workflow_request':{'template':'build_v1','parameters':{'result_file':'results/workload-result.json'}}})
                bridge=Bridge(path/'session.json');atomic_json(bridge.state_path,{'jobs':[ident]});before=bridge.state_path.read_bytes()
                client=Mock();client.request.side_effect=[{'job':{'id':ident,'state':state}},
                    ClientHTTPError(status,retry_after_seconds=1 if status==503 else None)]
                with patch('mcp_bridge.Client.from_profile',return_value=client):
                    if state=='failed' and status==404:
                        value=bridge.call('get_result',{'job_id':ident})
                        self.assertEqual(value,{'job_id':ident,'status':'failed','artifact_name':None,'artifact':None})
                    else:
                        with self.assertRaises(BridgeReadBusy if status==503 else BridgeError):bridge.call('get_result',{'job_id':ident})
                self.assertEqual(bridge.state_path.read_bytes(),before);self.assertEqual(client.request.call_count,2)


if __name__=='__main__':unittest.main()

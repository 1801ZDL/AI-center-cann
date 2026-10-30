"""Real HTTP ACL, bounded evidence integrity, portable CLI and read-only MCP."""
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostic_case import ARTIFACTS, DiagnosticBridge, build_case, redact, serve
from jobs import JobStore, ROOT, atomic_json
from lab_client import Client, ClientError
from server import ThreadingHTTPServer, handler_class


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'runtime', prefix='diagnostic-test-')
        self.base = Path(self.temp.name)
        config = {'username': 'diagnostic-admin', 'password': 'diagnostic-admin-secret', 'container': 'zdl'}
        self.jobs = JobStore(self.base/'central', config)
        self.passwords = {}
        for name in ['alice', 'bob']:
            workspace = self.base/name
            workspace.mkdir()
            self.passwords[name] = self.jobs.access.create_user(name, workspace)
        job, _ = self.jobs.submit({'template': 'cpu_demo', 'parameters': {'seconds': 1}}, self.jobs.access.user('alice'))
        self.identifier = job['id']
        self.jobs.cancel(self.identifier, self.jobs.access.user('alice'))
        directory = self.jobs.directory(self.identifier)
        (directory/'stdout.log').write_text('x'*10000+'\n/usr/bin/ld: cannot find -lz: No such file or directory\n')
        (directory/'stderr.log').write_text('Authorization: Bearer PRIVATE-LOG-TOKEN-12345\npassword='+self.passwords['alice']+'\n')
        (directory/'results').mkdir(exist_ok=True)
        atomic_json(directory/'results/full-build-provenance.json', {'passed': False, 'npu_kernel_launches': 0})
        atomic_json(directory/'results/workload-result.json', {'schema': 'ascend-lab-result/v1', 'kind': 'accuracy',
                    'passed': False, 'checks': [{'shape': [16, 2048], 'dtype': 'float32', 'max_abs_error': 0.25}]})
        (directory/'environment.json').write_text('x'*20000)
        atomic_json(directory/'artifacts.json', {'files': [{'name': name, 'bytes': (directory/name).stat().st_size}
                    for name in ['stdout.log', 'stderr.log', 'environment.json', 'results/full-build-provenance.json', 'results/workload-result.json']]})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_class(Mock(), config, self.jobs))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = {}
        self.profiles = {}
        for name in ['alice', 'bob']:
            value = {'controller': 'http://127.0.0.1:'+str(self.server.server_port),
                     'username': name, 'password': self.passwords[name]}
            self.clients[name] = Client(value)
            self.profiles[name] = self.base/(name+'.json')
            atomic_json(self.profiles[name], value)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_http_evidence_has_identity_hashes_and_explicit_incompleteness(self):
        case = build_case(self.clients['alice'], self.identifier)
        self.assertEqual(case['schema'], 'ascend-lab-diagnostic-case/v1')
        self.assertEqual(case['job']['id'], self.identifier)
        self.assertTrue(case['terminal'])
        by_id = {item['id']: item for item in case['evidence']}
        self.assertIn('cannot find -lz', by_id['stdout_tail']['text'])
        self.assertTrue(by_id['stdout_tail']['truncated'])
        self.assertLessEqual(len(by_id['stdout_tail']['text']), 8192)
        self.assertTrue(by_id['runtime_environment']['truncated'])
        self.assertEqual(by_id['runtime_environment']['artifact_bytes'], 20000)
        self.assertEqual(by_id['runtime_environment']['chunk_sha256'], hashlib.sha256(b'x'*16384).hexdigest())
        self.assertFalse(by_id['build_provenance']['truncated'])
        self.assertFalse({'diagnosis', 'root_cause', 'repair', 'next_command'} & set(case))
        fingerprint = case.pop('case_sha256')
        self.assertEqual(fingerprint, hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        for item in case['evidence']:
            self.assertEqual(item['preview_sha256'], hashlib.sha256(item['text'].encode()).hexdigest())

    def test_other_identity_fails_before_reading_any_job_logs(self):
        with self.assertRaisesRegex(ClientError, 'Controller HTTP 404'):
            build_case(self.clients['bob'], self.identifier)

    def test_read_only_token_suffices_and_revocation_immediately_blocks_evidence(self):
        token = self.jobs.access.issue_token('alice', {'jobs:read'})
        client = Client({'controller': self.clients['alice'].url, 'token': token})
        self.assertEqual(build_case(client, self.identifier)['job']['id'], self.identifier)
        with self.assertRaisesRegex(ClientError, 'Controller HTTP 403'):
            client.request('/api/jobs', {'template': 'cpu_demo', 'parameters': {'seconds': 1}})
        self.jobs.access.revoke_token(token)
        with self.assertRaisesRegex(ClientError, 'Controller HTTP 401'):
            build_case(client, self.identifier)

    def test_raw_workflow_checks_remain_evidence_without_overriding_controller_status(self):
        case = build_case(self.clients['alice'], self.identifier)
        report = next(item for item in case['evidence'] if item['id'] == 'workload_result')
        raw = json.loads(report['text'])
        self.assertFalse(raw['passed'])
        self.assertEqual(raw['checks'][0], {'shape': [16, 2048], 'dtype': 'float32', 'max_abs_error': 0.25})
        self.assertFalse(report['truncated'])
        self.assertEqual(case['job']['state'], 'cancelled')
        self.assertIn('not independent numerical certification', case['material_policy'])

    def test_active_or_unknown_job_never_becomes_terminal_or_submits_replacement(self):
        job = self.jobs.get(self.identifier)
        job['state'] = 'needs_attention'
        with self.jobs.transaction() as db:
            self.jobs.write(db, job)
        before = len(self.jobs.all())
        case = build_case(self.clients['alice'], self.identifier)
        self.assertFalse(case['terminal'])
        self.assertEqual(case['job']['state'], 'needs_attention')
        self.assertIn('retains its resources', case['execution_policy'])
        self.assertEqual(len(self.jobs.all()), before)
        self.assertEqual(self.jobs.get(self.identifier)['state'], 'needs_attention')

    def test_credentials_and_terminal_controls_are_not_exposed(self):
        case = build_case(self.clients['alice'], self.identifier)
        body = json.dumps(case)
        self.assertNotIn(self.passwords['alice'], body)
        self.assertNotIn('PRIVATE-LOG-TOKEN-12345', body)
        self.assertIn('[REDACTED]', body)
        self.assertEqual(redact('line\x00\x1b\x7f\n\tend', {}), 'line\n\tend')

    def test_only_allowlisted_artifacts_are_fetched_and_no_manifest_hashes_a_large_wheel(self):
        client = self.clients['alice']
        actual = client.request
        paths = []
        def request(path, *args, **kwargs):
            self.assertFalse(args, 'Diagnostic collector must only GET')
            paths.append(path)
            return actual(path, *args, **kwargs)
        client.request = request
        build_case(client, self.identifier)
        self.assertFalse(any('archive-manifest' in path or 'whl' in path for path in paths))
        self.assertTrue(any('artifact-chunk' in path for path in paths))
        self.assertLessEqual(len(paths), 3+len(ARTIFACTS))

    def test_bad_chunk_checksum_is_unavailable_evidence_not_a_valid_preview(self):
        client = self.clients['alice']
        actual = client.request
        def request(path, *args, **kwargs):
            value = actual(path, *args, **kwargs)
            if 'artifact-chunk' in path:
                value['data'] = base64.b64encode(b'forged').decode()
            return value
        client.request = request
        case = build_case(client, self.identifier)
        item = next(item for item in case['evidence'] if item['id'] == 'runtime_environment')
        self.assertIn('checksum', item['unavailable'])
        self.assertEqual(item['text'], '')

    def test_invalid_identifier_makes_no_request(self):
        client = Mock()
        for value in ['../other', 'job-123', 'https://other/job-123456789abc']:
            with self.subTest(value=value), self.assertRaises(ClientError):
                build_case(client, value)
        client.request.assert_not_called()

    def test_bridge_is_bound_and_exposes_no_execution_or_cancellation(self):
        bridge = DiagnosticBridge(self.clients['alice'], self.identifier)
        for name, args in [('submit_job', {}), ('cancel_job', {}), ('read_file', {}),
                           ('get_diagnostic_case', {'job_id': 'job-000000000000'}),
                           ('get_diagnostic_case', {'path': '/root/private'})]:
            with self.subTest(name=name), self.assertRaises(ClientError):
                bridge.call(name, args)
        self.assertEqual(bridge.call('get_diagnostic_case', {})['job']['id'], self.identifier)

    def test_stdio_tools_schema_actual_acl_and_bad_arguments(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_diagnostic_case', 'arguments': {}}},
                    {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'cancel_job', 'arguments': {}}}]
        out = io.StringIO()
        serve(DiagnosticBridge(self.clients['alice'], self.identifier), io.StringIO('\n'.join(map(json.dumps, requests))+'\n'), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(replies), 4)
        tools = replies[1]['result']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['get_diagnostic_case'])
        self.assertTrue(tools[0]['annotations']['readOnlyHint'])
        self.assertFalse(replies[2]['result']['isError'])
        self.assertTrue(replies[3]['result']['isError'])
        out = io.StringIO()
        serve(DiagnosticBridge(self.clients['bob'], self.identifier), io.StringIO(json.dumps(requests[3])+'\n'), out)
        self.assertTrue(json.loads(out.getvalue())['result']['isError'])

    def test_bad_json_and_oversized_messages_do_not_invoke_tools(self):
        bridge = Mock()
        out = io.StringIO()
        serve(bridge, io.StringIO('['+'x'*40000+'\n{}\n'), out)
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        bridge.call.assert_not_called()

    def test_independently_installed_cli_and_stdio_use_http_without_central_files(self):
        prefix = self.base/'client'
        subprocess.run([str(ROOT/'install-client'), '--prefix', str(prefix)], check=True, capture_output=True)
        argv = [str(prefix/'labctl'), '--profile', str(self.profiles['alice']), 'diagnose', self.identifier]
        result = subprocess.run(argv, cwd=self.base, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['job']['id'], self.identifier)
        self.assertFalse((prefix/'server.py').exists())
        self.assertFalse((prefix/'runtime').exists())
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'get_diagnostic_case', 'arguments': {}}}
        result = subprocess.run([sys.executable, str(prefix/'diagnostic_case.py'), '--profile', str(self.profiles['alice']),
                                 '--job-id', self.identifier], input=json.dumps(message)+'\n',
                                cwd=self.base, check=True, capture_output=True, text=True)
        reply = json.loads(result.stdout)['result']
        self.assertFalse(reply['isError'])
        self.assertEqual(json.loads(reply['content'][0]['text'])['job']['id'], self.identifier)


if __name__ == '__main__':
    unittest.main()

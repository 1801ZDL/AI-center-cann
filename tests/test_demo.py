import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import Collector, command, parse_npu
from server import ThreadingHTTPServer, handler_class

TABLE = '''
| NPU ID | Name | Health | Power(W) Temp(C) Hugepages |
| 0 | Ascend950PR | OK | 205.9 62 0 / 0 |
| | | 0000:D7:00.0 | 0 0 / 0 5721 / 131072 |
| 1 | Ascend950PR | Alarm | 356.0 79 0 / 0 |
| | | 0000:55:00.0 | 100 0 / 0 15516 / 131072 |
| NPU ID | Process id | Process name | Process memory(MB) |
| 0 | 123 | python3.11 | 79 |
| 1 | 456 | python3 | 299 |
'''


class CollectorTests(unittest.TestCase):
    def test_ascend950_device_metrics_and_memory_columns(self):
        devices, processes = parse_npu(TABLE)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]['utilization_pct'], 0)
        self.assertEqual(devices[1]['utilization_pct'], 100)
        self.assertEqual(devices[0]['hbm_used_bytes'], 5721 * 1024**2)
        self.assertEqual(devices[1]['hbm_total_bytes'], 131072 * 1024**2)
        self.assertEqual(devices[1]['health'], 'Alarm')
        self.assertEqual([p['pid'] for p in processes], [123, 456])

    def test_missing_npu_does_not_become_zero_devices(self):
        with self.assertRaises(ValueError):
            parse_npu('Access denied')

    def test_external_npu_pids_are_omitted(self):
        collector = Collector.__new__(Collector)
        collector.username, collector.uid, collector.container_name = 'demo', 1104, 'zdl'
        devices, processes = parse_npu(TABLE)
        collector.npu_cache = {'status': 'ok', 'sampled_at': '2026-10-29T00:00:00+00:00',
                               'devices': devices, 'processes': processes}
        selected = [{'pid': 123, 'start_ticks': 1, 'name': 'python', 'scopes': ['user'],
                     'rss_bytes': 10, 'cpu_pct': None}]
        with patch.object(collector, 'host', return_value={'uptime_seconds': 10}), \
             patch.object(collector, 'container', return_value=({'name': 'zdl'}, set())), \
             patch.object(collector, 'processes', return_value=selected):
            snapshot = collector.snapshot()
        self.assertEqual([p['pid'] for p in snapshot['npu']['processes']], [123])
        self.assertEqual(snapshot['npu']['devices'][1]['external_process_count'], 1)
        self.assertNotIn('456', json.dumps(snapshot['npu']['processes']))
        self.assertIsNone(snapshot['user_summary']['cpu_cores'])

    def test_process_scope_excludes_other_users(self):
        def process(pid, uid, name):
            fields = ['0'] * 22
            fields[0], fields[1], fields[11], fields[12], fields[19], fields[21] = 'S', '1', '2', '3', '4', '5'
            class Entry:
                def __init__(self):
                    self.name = str(pid)
                def stat(self):
                    return SimpleNamespace(st_uid=uid)
                def __truediv__(self, part):
                    if part == 'stat':
                        return SimpleNamespace(read_text=lambda: f'{pid} ({name}) '+ ' '.join(fields))
                    if uid == 2200 and pid != 303:
                        raise AssertionError('Foreign process contents must not be read')
                    return SimpleNamespace(read_bytes=lambda: b'python\0--api-key\0secret\0/private/test_demo.py\0')
            return Entry()
        collector = Collector.__new__(Collector)
        collector.uid, collector.previous_time, collector.previous_processes = 1104, None, {}
        proc = SimpleNamespace(iterdir=lambda: [process(101, 1104, 'python'), process(202, 2200, 'python'), process(303, 2200, 'python')])
        with patch('collector.PROC', proc):
            rows = collector.processes({303}, 100)
        self.assertEqual({p['pid'] for p in rows}, {101, 303})
        self.assertEqual(next(p for p in rows if p['pid'] == 303)['scopes'], ['container'])
        self.assertNotIn('secret', json.dumps(rows))
        self.assertNotIn('/private/', json.dumps(rows))

    def test_query_timeout_is_bounded(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            command([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=.1)
        self.assertLess(time.monotonic() - started, 2)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Store:
            def latest(self): return {'scope': {'username': 'demo'}}
            def history(self): return []
            def events(self): return []
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_class(Store(), {'username': 'demo', 'password': 'test-only-secret', 'interval': 15}))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, auth=False, method='GET'):
        headers = {}
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(b'demo:test-only-secret').decode()
        request = urllib.request.Request(self.url + path, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error

    def test_health_has_no_private_state(self):
        with self.request('/healthz') as response:
            self.assertEqual(json.load(response), {'status': 'ok', 'service': 'ascend-lab-demo'})

    def test_page_and_data_require_authentication(self):
        for path in ['/', '/app.js', '/api/snapshot', '/api/export']:
            with self.subTest(path=path), self.request(path) as response:
                self.assertEqual(response.status, 401)
                self.assertIn('Basic', response.headers['WWW-Authenticate'])
                self.assertNotIn('test-only-secret', response.read().decode())

    def test_authorized_snapshot_has_scope(self):
        with self.request('/api/snapshot', auth=True) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)['snapshot']['scope']['username'], 'demo')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_cannot_read_arbitrary_files(self):
        for path in ['/runtime/config.json', '/../../etc/passwd', '/api/files?path=/etc/passwd']:
            with self.subTest(path=path), self.request(path, auth=True) as response:
                self.assertEqual(response.status, 404)

    def test_no_mutation_routes(self):
        for method in ['POST', 'DELETE', 'PUT', 'PATCH']:
            with self.subTest(method=method), self.request('/api/exec', auth=True, method=method) as response:
                self.assertEqual(response.status, 405)


if __name__ == '__main__':
    unittest.main()

"""Container observation must never create tenant exec processes.

All transport and cgroup reads are mocked; no Docker, filesystem probe or server.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import Collector


class ContainerObservationSafetyTests(unittest.TestCase):
    def collector(self):
        observer = Collector.__new__(Collector)
        observer.container_name = 'fixture-container'
        return observer

    def test_repeated_running_observations_never_exec_or_probe_filesystem(self):
        observer = self.collector()
        info = {'id': 'fixture-id', 'name': '/fixture-container',
                'status': 'running', 'pid': 123, 'shm_limit_bytes': 8192}
        calls = []

        def transport(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ['docker', 'inspect']:
                return json.dumps(info)
            if argv == ['docker', 'top', 'fixture-container', '-eo', 'pid']:
                return 'PID\n123\n456\n'
            self.fail('Monitoring attempted an unexpected command: ' + repr(argv))

        with patch('collector.command', side_effect=transport), \
             patch('collector.cgroup_memory', return_value=None), \
             patch('collector.disk', side_effect=AssertionError('No filesystem probe')), \
             patch('collector.shutil.disk_usage', side_effect=AssertionError('No filesystem probe')):
            for _ in range(8):
                result, pids = observer.container()
                self.assertEqual(pids, {123, 456})
                self.assertIsNone(result['shm'])
                self.assertEqual(result['shm_limit_bytes'], 8192)
                self.assertEqual(result['shm_status'], 'unavailable_no_container_exec')
        self.assertEqual(len(calls), 16)

    def test_stopped_container_does_not_execute_top_or_exec(self):
        info = {'name': '/fixture-container', 'status': 'exited'}
        with patch('collector.command', return_value=json.dumps(info)) as transport:
            result, pids = self.collector().container()
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(pids, set())
        self.assertIsNone(result['shm'])
        self.assertEqual(result['process_count'], 0)


if __name__ == '__main__':
    unittest.main()

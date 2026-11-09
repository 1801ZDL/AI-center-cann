#!/usr/bin/env python3
"""Finite, CPU-only repository checks; no tasks, Docker, network or services."""
import argparse
import io
import json
import os
from pathlib import Path
import resource
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module',action='append',choices=['test_core_operations','test_collector_safety','test_read_retry','test_repository_layout','test_maintenance_policy'])
    args=parser.parse_args()
    os.umask(0o077);os.nice(10);os.sched_setaffinity(0,{min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU,(5,5));signal.alarm(8)
    sys.dont_write_bytecode=True
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'src/ascend_lab'))
    from source_layout import snapshot
    os.environ['ASCEND_LAB_REPOSITORY_ROOT']=str(root)
    output=io.StringIO();started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='ascend-lab-source-check-') as folder:
        info=snapshot(root,folder)
        sys.path[:0]=[folder,str(Path(folder)/'tests')]
        modules=args.module or ['test_core_operations','test_collector_safety','test_read_retry','test_repository_layout','test_maintenance_policy']
        with patch('socket.socket',side_effect=AssertionError('Network forbidden in focused checks')), \
             patch('subprocess.Popen',side_effect=AssertionError('Subprocess forbidden in focused checks')), \
             patch('threading.Thread.start',side_effect=AssertionError('Background threads forbidden in focused checks')):
            suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in modules)
            result=unittest.TextTestRunner(stream=output,verbosity=2).run(suite)
    usage=resource.getrusage(resource.RUSAGE_SELF)
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    record={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'wall_seconds':time.monotonic()-started,'cpu_seconds':usage.ru_utime+usage.ru_stime,
            'peak_rss_kib':int(status['VmHWM'].split()[0]),'address_space_limit_mib':256,
            'single_cpu':True,'snapshot':info,
            'real_subprocess_forbidden':True,'network_forbidden':True,'background_threads_forbidden':True,
            'workloads_started':0,'full_regression':False}
    print(output.getvalue() if not result.wasSuccessful() else '\n'.join(output.getvalue().splitlines()[-5:]))
    print(json.dumps(record))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())

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
from contextlib import nullcontext
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module',action='append',choices=['test_core_operations','test_collector_safety','test_read_retry','test_repository_layout','test_maintenance_policy','test_maintenance_execution','test_result_history','test_grant_history','test_budget_restore','test_graph_history','test_execution_pressure'])
    parser.add_argument('--direct-source',action='store_true',help='Explicit supported focused modules only; skip legacy source snapshot IO')
    args=parser.parse_args()
    direct={'test_core_operations','test_collector_safety','test_read_retry','test_repository_layout',
            'test_maintenance_policy','test_maintenance_execution','test_result_history','test_grant_history','test_budget_restore','test_graph_history','test_execution_pressure'}
    if args.direct_source and (not args.module or set(args.module)-direct):
        parser.error('--direct-source needs explicit supported source modules')
    os.umask(0o077);os.nice(10);os.sched_setaffinity(0,{min(os.sched_getaffinity(0))})
    resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU,(5,5));signal.alarm(8)
    sys.dont_write_bytecode=True
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'src/ascend_lab'))
    from source_layout import snapshot
    os.environ['ASCEND_LAB_REPOSITORY_ROOT']=str(root)
    output=io.StringIO();started=time.monotonic()
    print(json.dumps({'phase':'source_setup','direct_source':args.direct_source}),flush=True)
    with (nullcontext(None) if args.direct_source else tempfile.TemporaryDirectory(prefix='ascend-lab-source-check-')) as folder:
        info={'files':0,'bytes':0,'mode':'direct_source'} if args.direct_source else snapshot(root,folder)
        sys.path[:0]=[str(root/'src/ascend_lab'),str(root/'tests')] if args.direct_source else [folder,str(Path(folder)/'tests')]
        print(json.dumps({'phase':'source_ready','snapshot':info}),flush=True)
        modules=args.module or ['test_core_operations','test_collector_safety','test_read_retry','test_repository_layout','test_maintenance_policy','test_maintenance_execution','test_result_history','test_grant_history','test_budget_restore','test_graph_history','test_execution_pressure']
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

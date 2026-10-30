"""Small CPU-only demo task: emits progress and writes a structured result."""
import argparse
import json
import os
from pathlib import Path
import time

parser = argparse.ArgumentParser()
parser.add_argument('--seconds', type=int, default=6)
args = parser.parse_args()
if not 0 <= args.seconds <= 120:
    parser.error('seconds must be between 0 and 120')
print('CPU-only demonstration; no NPU libraries are imported.', flush=True)
print('Job:', os.environ.get('LAB_JOB_ID', 'standalone'), flush=True)
print('CPU affinity:', sorted(os.sched_getaffinity(0)), flush=True)
for i in range(args.seconds):
    print(f'progress {i+1}/{args.seconds}', flush=True)
    time.sleep(1)
directory = Path(os.environ.get('LAB_JOB_DIR', '.')) / 'results'
directory.mkdir(exist_ok=True)
(directory/'demo-result.json').write_text(json.dumps({'passed': True, 'seconds': args.seconds, 'npu_used': False}, indent=2)+'\n')
print('Completed; results/demo-result.json is ready.', flush=True)

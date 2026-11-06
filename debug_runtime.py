"""Finite, non-root PTY runtime inside a normal managed CPU task container."""
import base64
import fcntl
import json
import os
from pathlib import Path
import pty
import selectors
import signal
import subprocess
import sys
import termios
import time


def decode(value, previous):
    if not isinstance(value, dict) or set(value) != {'seq', 'data'} or type(value['seq']) is not int or not previous < value['seq'] <= 64:
        raise ValueError('Debug input sequence is invalid or repeated')
    if value['seq'] != previous + 1 or not isinstance(value['data'], str) or len(value['data']) > 5500:
        raise ValueError('Debug input is not continuous')
    raw = base64.b64decode(value['data'], validate=True)
    if not 1 <= len(raw) <= 4096:
        raise ValueError('Debug frame size is invalid')
    return raw


def main():
    config = json.loads(Path('/payload/run.json').read_text())
    if os.getuid() < 1000 or config['template'] != 'debug_v1' or config['parameters']['argv'] != ['/bin/bash']:
        raise SystemExit('Debug runtime requires its assigned non-root identity and fixed shell')
    if config.get('npu_ids') or not 1 <= config['timeout_seconds'] <= 300:
        raise SystemExit('Debug runtime requires a finite CPU-only lease')
    master, slave = pty.openpty()

    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-i'],
                               stdin=slave, stdout=slave, stderr=slave, preexec_fn=terminal)
    os.close(slave)
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ, 'output')
    selector.register(0, selectors.EVENT_READ, 'input')
    os.set_blocking(master, False)
    started = last_input = time.monotonic()
    deadline = started + config['timeout_seconds']
    buffer = b''
    seq = total_input = total_output = 0
    reason = 'shell_exit'
    try:
        while process.poll() is None:
            current = time.monotonic()
            if current >= deadline or current - last_input >= 30:
                reason = 'lease_expired' if current >= deadline else 'idle_timeout'
                break
            for key, _ in selector.select(.2):
                try:
                    data = os.read(key.fd, 4096)
                except (BlockingIOError, OSError):
                    continue
                if not data:
                    reason = 'transport_closed'
                    return
                if key.data == 'output':
                    total_output += len(data)
                    if total_output > 1024 * 1024:
                        reason = 'output_budget'
                        return
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                    continue
                buffer += data
                if len(buffer) > 8192:
                    raise ValueError('Debug input framing exceeds its budget')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    value = json.loads(line)
                    raw = decode(value, seq)
                    total_input += len(raw)
                    if total_input > 65536:
                        raise ValueError('Debug input budget exhausted')
                    # The Node journals each delivery intent before forwarding;
                    # this runtime also refuses repeated/out-of-order frames.
                    os.write(master, raw)
                    seq = value['seq']
                    last_input = time.monotonic()
    finally:
        selector.close()
        os.close(master)
        if process.poll() is None:
            try:os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=.2)
            except subprocess.TimeoutExpired:pass
        record = {'schema': 'ascend-lab-debug-session/v1', 'reason': reason,
                  'input_frames': seq, 'input_bytes': total_input, 'output_bytes': total_output}
        Path('/work/results/debug-session.json').write_text(json.dumps(record))
        result = {'schema': 'ascend-lab-result/v1', 'kind': 'command', 'passed': True,
                  'artifacts': ['results/debug-session.json']}
        Path('/work/results/workload-result.json').write_text(json.dumps(result))
        summary = {'schema': 'ascend-lab-workflow/v1', 'kind': 'command', 'passed': True,
                   'command_executed': True, 'environment_lock': 'checked-by-protected-bootstrap',
                   'input': config['input'], 'result': result}
        Path('/work/results/workflow-summary.json').write_text(json.dumps(summary))


if __name__ == '__main__':
    main()

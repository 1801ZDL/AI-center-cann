"""One persisted, fixed read-only probe at a time, including after restart."""
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time

ACTIVE = set()
LOCK = threading.Lock()


def management_command(argv):
    if not isinstance(argv, list) or not argv:
        return False
    if Path(argv[0]).name == 'npu-smi' and argv[1:] == ['info']:
        return True
    scripts = {
        'source "$1" >/dev/null 2>&1 || exit 126; exec "$2" info': ('ascend-lab-host-admission', 8),
        'source "$1" >/dev/null 2>&1 || exit 126; exec "$2" info -t common -i "$3"': ('ascend-lab-frequency', 9),
    }
    if len(argv) < 6 or argv[:4] != ['bash', '--noprofile', '--norc', '-c'] or argv[4] not in scripts:
        return False
    label, length = scripts[argv[4]]
    return len(argv) == length and argv[5] == label and Path(argv[7]).name == 'npu-smi'


def management_guard():
    # Shared by admission, frequency and measurement probes under this UID.
    # Do not put the journal in a release directory or a per-attempt directory.
    return ProbeGuard(Path('/tmp') / ('ascend-lab-npu-probe-' + str(os.getuid())))


def identity(pid):
    try:
        path = Path('/proc') / str(pid)
        raw = (path / 'stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        return {'pid': pid, 'uid': path.stat().st_uid, 'start': int(fields[19]),
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'state': fields[0]}
    except (OSError, ValueError):
        return None


def alive(saved):
    actual = identity(saved.get('pid', -1))
    return actual is not None and actual['state'] != 'Z' and all(
        actual[key] == saved.get(key) for key in ['pid', 'uid', 'start', 'boot'])


def cancel():
    with LOCK:
        for process in ACTIVE:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class ProbeGuard:
    def __init__(self, root):
        self.root = Path(root)

    def run(self, argv, timeout=8):
        # Do not grow a remote exec process merely to obtain monitoring data.
        name = Path(argv[0]).name
        if not ((name == 'docker' and argv[1:2] in [['inspect'], ['top']]) or management_command(argv)):
            raise RuntimeError('Probe command is not in the fixed read-only allowlist')
        if type(timeout) not in {int, float} or not 0 < timeout <= 15:
            raise ValueError('Probe timeout must be finite and at most 15 seconds')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Probe directory must be private')
        descriptor = os.open(self.root / 'probe.json', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise RuntimeError('Unsafe probe journal')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another monitoring probe is in flight') from None
            if info.st_size > 4096:
                raise RuntimeError('Invalid probe journal')
            raw = os.read(descriptor, 4097)
            saved = json.loads(raw) if raw else None
            if saved and saved.get('phase')=='cooldown' and time.time()<saved.get('until_epoch',0):
                raise RuntimeError('Monitoring probe circuit is cooling down after failure')
            if saved and (saved.get('phase') == 'launching' or alive(saved)):
                raise RuntimeError('Previous probe is still alive or its launch is unconfirmed')
            failures=saved.get('failures',0) if saved else 0
            if type(failures) is not int or not 0<=failures<=100:raise RuntimeError('Invalid probe failure counter')

            def write(value):
                encoded = json.dumps(value, sort_keys=True).encode()
                os.lseek(descriptor, 0, os.SEEK_SET)
                written=0
                while written<len(encoded):
                    size=os.write(descriptor,encoded[written:])
                    if size<=0:raise OSError('Probe journal write made no progress')
                    written+=size
                os.ftruncate(descriptor, len(encoded))
                os.fsync(descriptor)

            def failed():
                count=min(100,failures+1)
                write({'phase':'cooldown','failures':count,
                       'until_epoch':time.time()+min(900,60*2**min(count-1,4))})

            write({'phase': 'launching','failures':failures})
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
            except BaseException:
                # Popen failure can be ambiguous around fork/exec; retain intent.
                raise
            with LOCK:
                ACTIVE.add(process)
            killed=False
            try:
                saved = identity(process.pid)
                if saved is None:
                    if process.poll() is None:
                        raise RuntimeError('Cannot identify monitoring probe')
                    saved = {'pid': process.pid, 'phase': 'reaped'}
                else:
                    saved['phase'] = 'running'
                write(saved)
                try:
                    output, _ = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        killed=True
                    except ProcessLookupError:
                        pass
                    # Never wait forever for an uninterruptible kernel wait.
                    try:
                        process.communicate(timeout=.2)
                    except subprocess.TimeoutExpired:
                        write({**saved, 'phase': 'blocked_after_kill'})
                        raise RuntimeError('Probe did not exit after timeout; further probes are blocked') from None
                    failed()
                    raise
                if process.returncode:
                    failed()
                    raise RuntimeError('Read-only probe failed')
                write(None)
                return output
            except BaseException:
                if not killed and process.poll() is None:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    try:process.communicate(timeout=.2)
                    except subprocess.TimeoutExpired:pass
                raise
            finally:
                with LOCK:
                    ACTIVE.discard(process)
        finally:
            os.close(descriptor)

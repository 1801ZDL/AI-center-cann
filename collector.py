"""Read-only collectors. No process control, arbitrary commands, or environment reads."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parent
PROC = Path('/proc')
HZ = os.sysconf('SC_CLK_TCK')
PAGE = os.sysconf('SC_PAGE_SIZE')
COMMAND_LOCK = threading.Lock()
ACTIVE_COMMANDS = set()
COMMANDS_STOPPED = False


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command(argv, timeout=8):
    # These arguments originate exclusively from fixed collectors, never HTTP input.
    with COMMAND_LOCK:
        if COMMANDS_STOPPED:
            raise RuntimeError('Collector is stopping')
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        ACTIVE_COMMANDS.add(p)
    try:
        try:
            output, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Reap only the subprocess group created by this collector.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            raise
    finally:
        with COMMAND_LOCK:
            ACTIVE_COMMANDS.discard(p)
    if p.returncode:
        raise RuntimeError(f'{Path(argv[0]).name}: exit {p.returncode}')
    return output


def cancel_commands():
    global COMMANDS_STOPPED
    with COMMAND_LOCK:
        COMMANDS_STOPPED = True
        for process in ACTIVE_COMMANDS:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def parse_npu(output):
    devices, processes = [], []
    active = None
    in_processes = False
    for line in output.splitlines():
        if 'Process id' in line:
            in_processes = True
        if not line.startswith('|'):
            continue
        cells = [s.strip() for s in line.strip().strip('|').split('|')]
        if len(cells) < 4:
            continue
        if in_processes:
            if cells[0].isdigit() and cells[1].isdigit():
                mem = re.search(r'\d+', cells[3])
                processes.append({'device_id': int(cells[0]), 'pid': int(cells[1]),
                                  'memory_bytes': int(mem.group()) * 1024**2 if mem else None})
            continue
        if cells[0].isdigit() and 'Ascend' in cells[1]:
            nums = re.findall(r'[\d.]+', cells[3])
            active = {'id': int(cells[0]), 'name': cells[1], 'health': cells[2],
                      'power_w': float(nums[0]) if nums else None,
                      'temperature_c': float(nums[1]) if len(nums) > 1 else None,
                      'utilization_pct': None, 'hbm_used_bytes': None,
                      'hbm_total_bytes': None}
            devices.append(active)
        elif active is not None and not cells[0] and re.match(r'[0-9a-fA-F]{4}:', cells[2]):
            # Ascend950 table: two empty cells, then bus, then combined metrics.
            active['bus_id'] = cells[2]
            utilization = cells[3].split()[0] if cells[3].split() else ''
            active['utilization_pct'] = float(utilization) if utilization.isdigit() else None
            pairs = re.findall(r'(\d+)\s*/\s*(\d+)', cells[3])
            if pairs:
                active['hbm_used_bytes'], active['hbm_total_bytes'] = [int(x) * 1024**2 for x in pairs[-1]]
    if not devices:
        raise ValueError('Unsupported or empty npu-smi output')
    return devices, processes


def classify(name, script=''):
    s = (name + ' ' + script).lower()
    if any(x in s for x in ('opencode', 'codex', 'msagent', 'claude')):
        return 'agent', 'Agent'
    if any(x in s for x in ('msprof', 'msopprof', 'simulator')):
        return 'profile', '采集 / 仿真（推断）'
    if name.lower() in ('ninja', 'make', 'cmake', 'cc1', 'cc1plus', 'ccec', 'ld', 'ld.lld') or any(x in name for x in ('clang', 'g++', 'bisheng', 'hivmc')):
        return 'build', '编译（推断）'
    if 'pytest' in s or script.startswith('test_'):
        return 'test', '测试（推断）'
    if name.startswith('python'):
        return 'python', 'Python'
    if name in ('bash', 'sh', 'zsh', 'sshd', 'tmux: server'):
        return 'session', '会话'
    return 'other', '其他'


def cgroup_memory(pid):
    try:
        line = next(x for x in (PROC / str(pid) / 'cgroup').read_text().splitlines() if x.startswith('0::'))
        relative = line.split('::', 1)[1].lstrip('/')
        base = Path('/sys/fs/cgroup').resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base):
            return None
        current = int((path / 'memory.current').read_text())
        limits = []
        for parent in (path, *path.parents):
            if not parent.is_relative_to(base):
                break
            f = parent / 'memory.max'
            if f.exists():
                value = f.read_text().strip()
                if value != 'max':
                    limits.append(int(value))
        return {'current_bytes': current, 'effective_limit_bytes': min(limits) if limits else None}
    except (OSError, ValueError, StopIteration):
        return None


def disk(path):
    u = shutil.disk_usage(path)
    return {'path': str(path), 'total_bytes': u.total, 'used_bytes': u.used, 'free_bytes': u.free}


class Collector:
    def __init__(self, username, container):
        self.username = username
        self.uid = pwd.getpwnam(username).pw_uid
        self.container_name = container
        self.previous_cpu = None
        self.previous_processes = {}
        self.previous_time = None
        self.npu_cache = {'status': 'pending', 'sampled_at': None, 'devices': [], 'processes': []}

    def host(self):
        memory = {}
        for line in (PROC / 'meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        cpu = [int(x) for x in (PROC / 'stat').read_text().splitlines()[0].split()[1:9]]
        # guest times are already included in user/nice; don't count them twice.
        total, idle = sum(cpu), cpu[3] + cpu[4]
        utilization = None
        if self.previous_cpu:
            delta = total - self.previous_cpu[0]
            if delta > 0:
                utilization = max(0, min(100, 100 * (1 - (idle - self.previous_cpu[1]) / delta)))
        self.previous_cpu = total, idle
        return {'hostname': os.uname().nodename, 'cpu_count': os.cpu_count(),
                'cpu_utilization_pct': utilization, 'load_average': list(os.getloadavg()),
                'memory_total_bytes': memory['MemTotal'], 'memory_available_bytes': memory['MemAvailable'],
                'memory_used_bytes': memory['MemTotal'] - memory['MemAvailable'],
                'swap_used_bytes': memory['SwapTotal'] - memory['SwapFree'],
                'uptime_seconds': float((PROC / 'uptime').read_text().split()[0]),
                'shm': disk('/dev/shm'), 'workspace_disk': disk(ROOT),
                'user_cgroup': cgroup_memory(os.getpid())}

    def container(self):
        fields = {
            'id': '.Id', 'name': '.Name', 'status': '.State.Status', 'pid': '.State.Pid',
            'started_at': '.State.StartedAt', 'image': '.Config.Image',
            'memory_limit_bytes': '.HostConfig.Memory', 'shm_limit_bytes': '.HostConfig.ShmSize',
            'ipc_mode': '.HostConfig.IpcMode', 'privileged': '.HostConfig.Privileged',
        }
        template = '{' + ','.join('"' + k + '":{{json ' + v + '}}' for k, v in fields.items()) + '}'
        result = json.loads(command(['docker', 'inspect', '--format', template, self.container_name]))
        result['name'] = result['name'].lstrip('/')
        result['sampled_at'] = now()
        pids = set()
        if result['status'] == 'running':
            lines = command(['docker', 'top', self.container_name, '-eo', 'pid']).splitlines()[1:]
            pids = {int(x.strip()) for x in lines if x.strip().isdigit()}
            result['process_count'] = len(pids)
            result['cgroup_memory'] = cgroup_memory(result['pid'])
            # Fixed read-only command; no shell and no user-submitted arguments.
            try:
                output = command(['docker', 'exec', self.container_name, 'df', '-B1', '--output=size,used,avail', '/dev/shm'], timeout=4)
                values = output.splitlines()[-1].split()
                result['shm'] = dict(zip(('total_bytes', 'used_bytes', 'free_bytes'), map(int, values)))
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                result['shm'] = None
        else:
            result.update(process_count=0, cgroup_memory=None, shm=None)
        # CPU comes from process deltas below, not a blocking docker stats stream.
        return result, pids

    def processes(self, container_pids, uptime):
        rows, previous = [], {}
        monotonic = time.monotonic()
        elapsed = monotonic - self.previous_time if self.previous_time else None
        for path in PROC.iterdir():
            if not path.name.isdigit():
                continue
            try:
                pid = int(path.name)
                uid = path.stat().st_uid
                if uid != self.uid and pid not in container_pids:
                    continue
                raw = (path / 'stat').read_text()
                end = raw.rfind(')')
                name = raw[raw.find('(') + 1:end][:80]
                fields = raw[end + 2:].split()
                ticks = int(fields[11]) + int(fields[12])
                start = int(fields[19])
                key = pid, start
                previous[key] = ticks
                pct = None
                if elapsed and key in self.previous_processes:
                    pct = max(0, (ticks - self.previous_processes[key]) / HZ / elapsed * 100)
                rss = max(0, int(fields[21])) * PAGE
                # Only expose a conservative script basename, never argv, cwd or env.
                script = ''
                if name.startswith('python') or name == 'pytest':
                    try:
                        args = (path / 'cmdline').read_bytes()[:8192].split(b'\0')
                        for arg in args[1:]:
                            candidate = arg.decode(errors='replace').rsplit('/', 1)[-1]
                            if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.py', candidate):
                                script = candidate
                                break
                    except OSError:
                        pass
                category, label = classify(name, script)
                scopes = []
                if uid == self.uid:
                    scopes.append('user')
                if pid in container_pids:
                    scopes.append('container')
                rows.append({'pid': pid, 'ppid': int(fields[1]), 'uid': uid, 'name': name,
                             'script': script, 'state': fields[0], 'rss_bytes': rss,
                             'cpu_pct': round(pct, 2) if pct is not None else None,
                             'elapsed_seconds': max(0, uptime - start / HZ),
                             'category': category, 'category_label': label, 'scopes': scopes,
                             'start_ticks': start})
            except (OSError, ValueError, IndexError):
                continue
        self.previous_processes, self.previous_time = previous, monotonic
        rows.sort(key=lambda r: (r['cpu_pct'] or 0, r['rss_bytes']), reverse=True)
        return rows

    def collect_npu(self):
        # Initialize CANN only inside this bounded read-only subprocess.
        tool = shutil.which('npu-smi') or '/usr/local/sbin/npu-smi'
        argv = ['bash', '--noprofile', '--norc', '-c',
                'source "$1" >/dev/null 2>&1; exec "$2" info',
                'ascend-lab-monitor', '/data/pri/cann/set_env.sh', tool]
        try:
            devices, processes = parse_npu(command(argv, timeout=12))
            self.npu_cache = {'status': 'ok', 'sampled_at': now(), 'devices': devices, 'processes': processes}
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.npu_cache = {**self.npu_cache, 'status': 'error', 'error': type(exc).__name__}

    def snapshot(self):
        errors = []
        host = self.host()
        try:
            container, pids = self.container()
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            container, pids = {'name': self.container_name, 'status': 'unavailable', 'error': type(exc).__name__}, set()
            errors.append('容器状态暂不可读；不会将缺失数据记为 0。')
        processes = self.processes(pids, host['uptime_seconds'])
        index = {row['pid']: row for row in processes}
        npu = {**self.npu_cache, 'devices': [dict(x) for x in self.npu_cache['devices']], 'processes': []}
        for item in self.npu_cache['processes']:
            row = index.get(item['pid'])
            if row:
                npu['processes'].append({**item, 'name': row['name'], 'scopes': row['scopes']})
        for device in npu['devices']:
            all_for_device = [x for x in self.npu_cache['processes'] if x['device_id'] == device['id']]
            mine = [x for x in npu['processes'] if x['device_id'] == device['id']]
            device['scoped_process_count'] = len(mine)
            device['external_process_count'] = len(all_for_device) - len(mine)
            device['scoped_hbm_bytes'] = sum(x['memory_bytes'] or 0 for x in mine)
            device['allocation_status'] = '观察到设备进程' if all_for_device else '未观察到设备进程；尚未预留'
        if npu['status'] == 'error':
            errors.append('NPU 查询失败；如存在上次读数，会保留时间戳并标记过期。')
        selected = [r for r in processes if 'user' in r['scopes']]
        in_container = [r for r in processes if 'container' in r['scopes']]
        def summary(rows):
            return {'process_count': len(rows), 'rss_sum_bytes': sum(r['rss_bytes'] for r in rows),
                    'cpu_cores': sum(r['cpu_pct'] or 0 for r in rows) / 100 if any(r['cpu_pct'] is not None for r in rows) else None}
        return {'schema_version': 1, 'sampled_at': now(),
                'scope': {'username': self.username, 'uid': self.uid, 'container': self.container_name},
                'mode': 'read_only_observer', 'host': host, 'container': container,
                'processes': processes, 'user_summary': summary(selected),
                'container_summary': summary(in_container), 'npu': npu, 'warnings': errors,
                'notes': ['进程分类由名称推断，不能替代用户提交的任务类型。',
                          'RSS 求和可能重复计算共享页；容器内存优先参考 cgroup。',
                          '全机指标包含其他用户；本页面没有预留或分配设备。']}


EXPERIMENTS = [
    ('merge-topk', 'MergeTopK · 设备频率对照', 'performance',
     '/home/m00951283/Q4_kernel/work/merge_topk_perf_diagnosis_20261026/summary.json'),
    ('causal-conv', 'Causal conv · 单次执行验收', 'validation',
     '/home/m00951283/Q4_kernel/work/causal_conv1d_single_launch_20261027/validation.json'),
    ('simt-lineinfo', 'SIMT · 安装补丁追踪', 'environment',
     '/home/m00951283/Q4_kernel/work/causal_conv1d_lineinfo_20261027/installed_patch.json'),
]


def experiments():
    """Small explicit allowlist. Never recursively scan or change experiment folders."""
    records = []
    for key, title, kind, filename in EXPERIMENTS:
        path = Path(filename)
        try:
            data = json.loads(path.read_text())
            facts = []
            if key == 'merge-topk':
                for row in data.get('replays', []):
                    facts.append(f"设备 {row['physical_device']}：{row['mean_us']:.3f} μs / {row['n']} 次")
                facts.append('同 ELF 对照；历史频率状态不代表当前状态')
            elif key == 'causal-conv':
                facts = [f"Shape：{' × '.join(map(str, data.get('shape', [])))}",
                         f"Smoke pytest：{data.get('smoke_pytest', 'unknown')}",
                         '完整仿真：' + ('已启动' if data.get('full_simulation_started') else '未启动')]
            else:
                facts = [f"补丁状态：{data.get('status', 'unknown')}",
                         f"修改后 SHA256：{data.get('after_sha256', '')[:16]}…"]
            records.append({'id': key, 'title': title, 'kind': kind, 'source': filename,
                            'modified_at': dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat(),
                            'facts': facts, 'data': data})
        except (OSError, ValueError, KeyError, TypeError):
            records.append({'id': key, 'title': title, 'kind': kind, 'source': filename,
                            'facts': ['原记录暂不可读'], 'data': None})
    return records

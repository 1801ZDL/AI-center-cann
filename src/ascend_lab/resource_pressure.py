"""Fixed proc/cgroup reads for admission; no subprocesses or device queries."""
import math
from pathlib import Path
import time

CAPABILITY = 'cgroup-pressure-admission-v1'
DEFAULT = {'enabled': False, 'max_age_seconds': 20,
           'memory_full_avg10': 10, 'io_full_avg10': 25,
           'reserve_mib': 128}


def policy(value=None):
    if value is None:
        return dict(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Invalid resource pressure policy')
    result = {**DEFAULT, **value}
    if type(result['enabled']) is not bool:
        raise ValueError('Pressure enabled must be boolean')
    for key, low, high in [('max_age_seconds', 1, 60),
                           ('memory_full_avg10', 1, 100),
                           ('io_full_avg10', 1, 100), ('reserve_mib', 0, 4096)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid pressure threshold: ' + key)
    return result


def text(path):
    with path.open() as stream:
        result = stream.read(16385)
    if len(result) > 16384:
        raise ValueError('Resource observation exceeds its bound')
    return result


def full_pressure(raw):
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] == 'full':
            value = float(dict(field.split('=', 1) for field in fields[1:])['avg10'])
            if math.isfinite(value) and 0 <= value <= 100:
                return value
    raise ValueError('Missing finite full pressure')


def integer(raw, *, unlimited=False):
    if unlimited and raw.strip() == 'max':
        return None
    value = int(raw.strip())
    if value < 0:
        raise ValueError('Negative resource counter')
    return value


def sample(proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup'), *, epoch=None):
    """Account for every visible ancestor, including memory.high backpressure.

    Paths never appear in the public report. A namespace hiding its real cgroup
    hierarchy is explicitly unknown rather than unlimited. Budgets still need
    to describe the task's execution cgroup, not just this agent's cgroup.
    """
    result = {'schema': CAPABILITY, 'sampled_epoch': time.time() if epoch is None else epoch,
              'status': 'unknown', 'available_bytes': None,
              'swap_free_bytes': None, 'memory_full_avg10': None,
              'io_full_avg10': None, 'ancestor_count': 0}
    try:
        memory = {key: int(value.split()[0]) * 1024
                  for key, value in (line.split(':', 1) for line in text(proc / 'meminfo').splitlines())}
        available = [memory['MemAvailable']]
        swap = [memory['SwapFree']]
        memory_psi = [full_pressure(text(proc / 'pressure/memory'))]
        io_psi = [full_pressure(text(proc / 'pressure/io'))]
        lines = [line[3:] for line in text(proc / 'self/cgroup').splitlines() if line.startswith('0::')]
        if len(lines) != 1 or not lines[0].startswith('/') or '..' in Path(lines[0]).parts:
            raise ValueError('Cannot identify cgroup v2 ancestry')
        relative = Path(lines[0].lstrip('/'))
        parts = relative.parts
        if len(parts) > 31:
            raise ValueError('Too many cgroup ancestors')
        for size in range(len(parts), -1, -1):
            path = cgroup.joinpath(*parts[:size])
            current = integer(text(path / 'memory.current'))
            for key in ['memory.high', 'memory.max']:
                try:
                    limit = integer(text(path / key), unlimited=True)
                except FileNotFoundError:
                    if size == 0:
                        continue  # Root cgroup has counters but no limits.
                    raise
                if limit is not None:
                    available.append(max(0, limit - current))
            try:
                swap_max = integer(text(path / 'memory.swap.max'), unlimited=True)
            except FileNotFoundError:
                if size != 0:
                    raise
                swap_max = None
            if swap_max is not None:
                swap.append(max(0, swap_max - integer(text(path / 'memory.swap.current'))))
            memory_psi.append(full_pressure(text(path / 'memory.pressure')))
            io_psi.append(full_pressure(text(path / 'io.pressure')))
            result['ancestor_count'] += 1
        result.update(status='ready', available_bytes=min(available), swap_free_bytes=min(swap),
                      memory_full_avg10=max(memory_psi), io_full_avg10=max(io_psi))
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        # No partial sample is used for new admission.
        result['status'] = 'unknown'
    return result


def validate(value):
    if not isinstance(value, dict) or set(value) != {
        'schema', 'sampled_epoch', 'status', 'available_bytes', 'swap_free_bytes',
        'memory_full_avg10', 'io_full_avg10', 'ancestor_count'}:
        raise ValueError('Invalid resource pressure observation')
    if value['schema'] != CAPABILITY or value['status'] not in {'ready', 'unknown'}:
        raise ValueError('Invalid resource pressure status')
    if type(value['sampled_epoch']) not in {int, float} or not math.isfinite(value['sampled_epoch']):
        raise ValueError('Invalid pressure timestamp')
    if type(value['ancestor_count']) is not int or not 0 <= value['ancestor_count'] <= 32:
        raise ValueError('Invalid pressure ancestry count')
    for key in ['available_bytes', 'swap_free_bytes']:
        item = value[key]
        if item is not None and (type(item) is not int or not 0 <= item <= 2**63 - 1):
            raise ValueError('Invalid pressure capacity')
    for key in ['memory_full_avg10', 'io_full_avg10']:
        item = value[key]
        if item is not None and (type(item) not in {int, float} or not math.isfinite(item) or not 0 <= item <= 100):
            raise ValueError('Invalid pressure metric')
    if value['status'] == 'ready' and (not value['ancestor_count'] or any(value[k] is None for k in [
        'available_bytes', 'swap_free_bytes', 'memory_full_avg10', 'io_full_avg10'])):
        raise ValueError('Incomplete pressure sample')
    return dict(value)


def reason(value, requested_bytes, config=None, *, epoch=None):
    settings = policy(config)
    if not settings['enabled']:
        return None
    try:
        value = validate(value)
        age = (time.time() if epoch is None else epoch) - value['sampled_epoch']
        if value['status'] != 'ready' or not -5 <= age <= settings['max_age_seconds']:
            return '资源压力观测缺失或过期，暂停新任务'
        if value['memory_full_avg10'] >= settings['memory_full_avg10']:
            return '内存回收或换页压力过高，暂停新任务'
        if value['io_full_avg10'] >= settings['io_full_avg10']:
            return 'IO等待压力过高，暂停新任务'
        if value['available_bytes'] < requested_bytes + settings['reserve_mib'] * 1024**2:
            return '任务所在cgroup或其父级内存余量不足，暂停新任务'
    except (ValueError, TypeError):
        return '资源压力观测无效，暂停新任务'
    return None

"""Fixed proc/cgroup reads for admission; no subprocesses or device queries."""
import math
from pathlib import Path
import time

CAPABILITY = 'cgroup-pressure-admission-v1'
EXECUTION_CAPABILITY = 'cgroup-cpu-pids-pressure-v1'
DEFAULT = {'enabled': False, 'max_age_seconds': 20,
           'memory_full_avg10': 10, 'io_full_avg10': 25,
           'reserve_mib': 128, 'cpu_some_avg10': None, 'reserve_pids': None}


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
    for key, low, high in [('cpu_some_avg10',1,100),('reserve_pids',0,65536)]:
        if result[key] is not None and (type(result[key]) is not int or not low<=result[key]<=high):
            raise ValueError('Invalid execution pressure threshold: '+key)
    return result


def text(path):
    with path.open() as stream:
        result = stream.read(16385)
    if len(result) > 16384:
        raise ValueError('Resource observation exceeds its bound')
    return result


def psi(raw,kind):
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] == kind:
            value = float(dict(field.split('=', 1) for field in fields[1:])['avg10'])
            if math.isfinite(value) and 0 <= value <= 100:
                return value
    raise ValueError('Missing finite pressure')


def full_pressure(raw):return psi(raw,'full')


def finite_number(value,maximum):
    # Range-check integers before float conversion: untrusted JSON may contain
    # very large integers for which math.isfinite itself raises OverflowError.
    return type(value) in {int,float} and 0<=value<=maximum and math.isfinite(value)


def ancestry(proc,cgroup):
    lines=[line[3:] for line in text(proc/'self/cgroup').splitlines() if line.startswith('0::')]
    if len(lines)!=1 or not lines[0].startswith('/') or '..' in Path(lines[0]).parts:
        raise ValueError('Cannot identify cgroup v2 ancestry')
    parts=Path(lines[0].lstrip('/')).parts
    if len(parts)>31:raise ValueError('Too many cgroup ancestors')
    return [cgroup.joinpath(*parts[:size]) for size in range(len(parts),-1,-1)]


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
        for path in ancestry(proc,cgroup):
            current = integer(text(path / 'memory.current'))
            for key in ['memory.high', 'memory.max']:
                try:
                    limit = integer(text(path / key), unlimited=True)
                except FileNotFoundError:
                    if path == cgroup:
                        continue  # Root cgroup has counters but no limits.
                    raise
                if limit is not None:
                    available.append(max(0, limit - current))
            try:
                swap_max = integer(text(path / 'memory.swap.max'), unlimited=True)
            except FileNotFoundError:
                if path != cgroup:
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


def sample_execution(proc=Path('/proc'),cgroup=Path('/sys/fs/cgroup'),*,epoch=None):
    """Visible CPU PSI and PID headroom, independent of legacy memory/IO.

    Missing controllers and hidden parents are never inferred as spare capacity.
    Root need not have pids.*; every visible non-root ancestor must provide it.
    This does not observe the execution container or reserve kernel PID slots.
    """
    result={'schema':EXECUTION_CAPABILITY,'sampled_epoch':time.time() if epoch is None else epoch,
            'ancestor_count':0,'cpu_some_avg10':None,'pids_status':'unknown','pids_available':None}
    try:paths=ancestry(proc,cgroup)
    except (OSError,ValueError,TypeError):return result
    result['ancestor_count']=len(paths)
    try:
        readings=[psi(text(proc/'pressure/cpu'),'some')]
        for path in paths:
            try:raw=text(path/'cpu.pressure')
            except FileNotFoundError:
                if path==cgroup:continue  # Host /proc already covers the real root.
                raise
            readings.append(psi(raw,'some'))
        result['cpu_some_avg10']=max(readings)
    except (OSError,ValueError,KeyError,TypeError,OverflowError):pass
    try:
        available=[]
        for path in paths:
            try:limit=integer(text(path/'pids.max'),unlimited=True)
            except FileNotFoundError:
                if path==cgroup and not (path/'pids.current').exists():continue
                raise
            current=integer(text(path/'pids.current'))
            later=integer(text(path/'pids.max'),unlimited=True)
            if current>2**63-1 or any(v is not None and v>2**63-1 for v in (limit,later)):raise ValueError('Oversized PID counter')
            # A changing limit must not make the observation more optimistic.
            limits=[v for v in (limit,later) if v is not None]
            if limits:available.append(max(0,min(limits)-current))
        result.update(pids_status='limited' if available else 'no_visible_limit',
                      pids_available=min(available) if available else None)
    except (OSError,ValueError,TypeError,OverflowError):pass
    return result


def validate_execution(value):
    if not isinstance(value,dict) or set(value)!={'schema','sampled_epoch','ancestor_count','cpu_some_avg10','pids_status','pids_available'}:
        raise ValueError('Invalid execution pressure observation')
    if value['schema']!=EXECUTION_CAPABILITY or not finite_number(value['sampled_epoch'],2**63-1):
        raise ValueError('Invalid execution pressure identity or timestamp')
    count=value['ancestor_count'];cpu=value['cpu_some_avg10'];pids=value['pids_available'];state=value['pids_status']
    if type(count) is not int or not 0<=count<=32:raise ValueError('Invalid execution pressure ancestry')
    if cpu is not None and (not finite_number(cpu,100) or not count):
        raise ValueError('Invalid CPU pressure metric')
    if not isinstance(state,str) or state not in {'limited','no_visible_limit','unknown'}:raise ValueError('Invalid PID pressure state')
    if state=='limited':
        if not count or type(pids) is not int or not 0<=pids<=2**63-1:raise ValueError('Invalid PID headroom')
    elif pids is not None or state=='no_visible_limit' and not count:raise ValueError('Inconsistent PID pressure observation')
    return dict(value)


def validate(value):
    if not isinstance(value, dict) or set(value) != {
        'schema', 'sampled_epoch', 'status', 'available_bytes', 'swap_free_bytes',
        'memory_full_avg10', 'io_full_avg10', 'ancestor_count'}:
        raise ValueError('Invalid resource pressure observation')
    if value['schema'] != CAPABILITY or not isinstance(value['status'],str) or value['status'] not in {'ready', 'unknown'}:
        raise ValueError('Invalid resource pressure status')
    if not finite_number(value['sampled_epoch'],2**63-1):
        raise ValueError('Invalid pressure timestamp')
    if type(value['ancestor_count']) is not int or not 0 <= value['ancestor_count'] <= 32:
        raise ValueError('Invalid pressure ancestry count')
    for key in ['available_bytes', 'swap_free_bytes']:
        item = value[key]
        if item is not None and (type(item) is not int or not 0 <= item <= 2**63 - 1):
            raise ValueError('Invalid pressure capacity')
    for key in ['memory_full_avg10', 'io_full_avg10']:
        item = value[key]
        if item is not None and not finite_number(item,100):
            raise ValueError('Invalid pressure metric')
    if value['status'] == 'ready' and (not value['ancestor_count'] or any(value[k] is None for k in [
        'available_bytes', 'swap_free_bytes', 'memory_full_avg10', 'io_full_avg10'])):
        raise ValueError('Incomplete pressure sample')
    return dict(value)


def reason(value, requested_bytes, config=None, *, epoch=None, execution=None, requested_pids=0):
    settings = policy(config)
    if not settings['enabled']:
        return None
    try:
        if type(requested_bytes) is not int or not 0<=requested_bytes<=2**63-1:raise ValueError('Invalid requested memory')
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
        if settings['cpu_some_avg10'] is not None or settings['reserve_pids'] is not None:
            return execution_reason(execution,settings,requested_pids,epoch=epoch)
    except (ValueError, TypeError):
        return '资源压力观测无效，暂停新任务'
    return None


def execution_reason(value,settings,requested_pids,*,epoch=None):
    try:
        value=validate_execution(value)
        age=(time.time() if epoch is None else epoch)-value['sampled_epoch']
        if not -5<=age<=settings['max_age_seconds']:return 'CPU/PID压力观测过期或时钟不符，暂停新任务'
        if settings['cpu_some_avg10'] is not None:
            if value['cpu_some_avg10'] is None:return 'CPU等待压力观测未知，暂停新任务'
            if value['cpu_some_avg10']>=settings['cpu_some_avg10']:return 'CPU等待压力过高，暂停新任务'
        if settings['reserve_pids'] is not None:
            if type(requested_pids) is not int or not 0<=requested_pids<=65536:raise ValueError('Invalid requested PID budget')
            if value['pids_status']!='limited':return 'PID有限上限或余量未知，暂停新任务'
            if value['pids_available']<requested_pids+settings['reserve_pids']:return '可见cgroup或其父级PID余量不足，暂停新任务'
    except (ValueError,TypeError,KeyError):return 'CPU/PID压力观测缺失或无效，暂停新任务'
    return None

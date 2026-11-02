"""Optional read-only AI-core frequency snapshots; never a health/performance pass.

Fixed common queries have a shared time/count budget. Missing, malformed or
unsupported output stays unknown, without falling back to the rated frequency.
"""
import datetime as dt
import math
import re
import subprocess
import time

CAPABILITY='npu-frequency-samples-v1'
SCHEMA='ascend-lab-npu-frequency/v1'
SOURCE='npu-smi info -t common'
QUERY_BUDGET=3.0
QUERY_TIMEOUT=1.0
MAX_QUERIES=8
MAX_TEXT=16384


def instant(value):
    if not isinstance(value,str) or len(value)>64:raise ValueError('Invalid frequency sample time')
    parsed=dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:raise ValueError('Frequency time needs a timezone')
    epoch=parsed.timestamp()
    if not math.isfinite(epoch):raise ValueError('Invalid frequency time')
    return epoch


def now():return dt.datetime.now(dt.timezone.utc).isoformat()


def unknown(status='not_collected',reason='no_frequency_observation'):
    return {'schema':SCHEMA,'status':status,'source':SOURCE,'reason':reason,
            'sampled_at':None,'current_mhz':None,'rated_mhz':None}


def parse(text,physical):
    if not isinstance(text,str) or len(text)>MAX_TEXT:raise ValueError('Unsupported frequency output')
    fields={}
    for line in text.splitlines():
        if ':' not in line:continue
        key,value=line.split(':',1);fields.setdefault(key.strip(),[]).append(value.strip())
    def integer(key):
        values=fields.get(key,[])
        if len(values)!=1 or not re.fullmatch(r'[0-9]{1,6}',values[0]):raise ValueError('Ambiguous/missing frequency field')
        return int(values[0])
    if integer('NPU ID')!=physical:raise ValueError('Frequency target differs')
    if 'Chip Count' in fields and integer('Chip Count')!=1:raise ValueError('Multi-chip frequency is not supported')
    if 'Chip ID' in fields and integer('Chip ID')!=0:raise ValueError('Unsupported chip selector')
    current=integer('Aicore curFreq(MHZ)');rated=integer('Aicore Freq(MHZ)')
    if not 1<=current<=100000 or not 1<=rated<=100000:raise ValueError('Frequency is unavailable or out of range')
    return {'current_mhz':current,'rated_mhz':rated}


def collect(device_ids,management_env,tool):
    from collector import command
    deadline=time.monotonic()+QUERY_BUDGET;result={}
    for index,physical in enumerate(device_ids):
        if index>=MAX_QUERIES or time.monotonic()>=deadline:
            result[physical]=unknown('not_sampled_budget','query_budget_exhausted');continue
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:result[physical]=unknown('not_sampled_budget','query_budget_exhausted');continue
            text=command(['bash','--noprofile','--norc','-c',
                'source "$1" >/dev/null 2>&1 || exit 126; exec "$2" info -t common -i "$3"',
                'ascend-lab-frequency',management_env,tool,str(physical)],timeout=min(QUERY_TIMEOUT,remaining))
            values=parse(text,physical)
            result[physical]={'schema':SCHEMA,'status':'sampled','source':SOURCE,'device_id':physical,
                              'sampled_at':now(),**values}
        except subprocess.TimeoutExpired:result[physical]=unknown('unavailable','query_timeout')
        except (OSError,RuntimeError):result[physical]=unknown('unavailable','query_failed')
        except (ValueError,OverflowError):result[physical]=unknown('unavailable','unsupported_output')
    return result


def normalize(value,physical,management_time):
    if value is None:return unknown()
    try:
        if not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('source')!=SOURCE:raise ValueError()
        status=value['status']
        if status!='sampled':
            allowed={'not_collected':{'no_frequency_observation'},'not_sampled_budget':{'query_budget_exhausted'},
                     'unavailable':{'query_timeout','query_failed','unsupported_output','invalid_observation'}}
            if value.get('reason') not in allowed.get(status,set()):raise ValueError()
            return unknown(status,value['reason'])
        if type(value.get('device_id')) is not int or value['device_id']!=physical:raise ValueError()
        if any(type(value.get(k)) is not int or not 1<=value[k]<=100000 for k in ['current_mhz','rated_mhz']):raise ValueError()
        # The common query follows the separate management snapshot. They are
        # not atomic readings, and upload/reconciliation may happen much later.
        delta=instant(value['sampled_at'])-instant(management_time)
        if not -1<=delta<=QUERY_BUDGET+1:raise ValueError()
        return {'schema':SCHEMA,'status':status,'source':SOURCE,'device_id':physical,
                **{k:value[k] for k in ['sampled_at','current_mhz','rated_mhz']}}
    except (ValueError,KeyError,TypeError,OverflowError):return unknown('unavailable','invalid_observation')


def empty_stats():
    return {'sample_count':0,'missing_count':0,'min_mhz':None,'max_mhz':None,
            'rated_min_mhz':None,'rated_max_mhz':None,'uncertain_order_count':0,
            'boundary_count':0,'boundary_missing_count':0}


def accumulate(stats,reading,count=True,boundary=False):
    """Preserve extrema when the bounded raw snapshot ring drops older values."""
    for row in reading['devices']:
        item=stats.setdefault(str(row['id']),empty_stats())
        frequency=row.get('frequency') or unknown()
        if not count and not boundary:item['uncertain_order_count']+=1
        if frequency['status']!='sampled':
            if boundary:item['boundary_missing_count']+=1
            elif count:item['missing_count']+=1
            continue
        if boundary:item['boundary_count']+=1
        elif count:item['sample_count']+=1
        for lower,upper,key in [('min_mhz','max_mhz','current_mhz'),('rated_min_mhz','rated_max_mhz','rated_mhz')]:
            value=frequency[key]
            item[lower]=value if item[lower] is None else min(item[lower],value)
            item[upper]=value if item[upper] is None else max(item[upper],value)


def initialize(trace,device_ids):
    if 'frequency_stats' not in trace:
        stats={}
        for reading in trace.get('samples',[]):accumulate(stats,reading)
        # An old bounded trace cannot provide frequency observations for its
        # dropped snapshots. Do not promote a new suffix to complete coverage.
        dropped=max(0,trace.get('sample_count',0)-len(trace.get('samples',[])))
        for physical in device_ids:
            item=stats.setdefault(str(physical),empty_stats())
            item['missing_count']+=dropped+trace.get('management_error_count',0)
        trace['frequency_stats']=stats
    return trace['frequency_stats']


def summary(trace,boundaries,device_ids):
    import copy
    stats=initialize(copy.deepcopy(trace),device_ids)
    # Boundary receipts can reuse a cached heartbeat observation. Keep their
    # references separate, rather than claiming extra independent sensor reads.
    for name in ['admission','release']:
        reading=boundaries.get(name) or {'devices':[{'id':physical} for physical in device_ids]}
        accumulate(stats,reading,boundary=True)
    cards=[]
    for physical in device_ids:
        item=stats.get(str(physical),{})
        sampled=item.get('sample_count',0)+item.get('boundary_count',0)
        missing=item.get('missing_count',0)+item.get('boundary_missing_count',0)
        state='not_collected' if not sampled else 'partial' if missing or item.get('uncertain_order_count') else 'sampled'
        cards.append({'device_id':physical,'status':state,**item})
    state='not_collected' if not any(c.get('sample_count',0)+c.get('boundary_count',0) for c in cards) else 'partial' if any(c['status']!='sampled' for c in cards) else 'sampled'
    return {'status':state,'source':SOURCE,'cards':cards,'performance_qualified':False,
            'note':'Asynchronous AI-core snapshots and retained extrema; not continuous clock coverage, Cube/Vector clock certification, or a performance baseline.'}

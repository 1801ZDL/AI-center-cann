"""Bounded controller evidence of management snapshots, never numerical certification.

Only authenticated Node telemetry and its private admission receipt are inputs.
Workload reports cannot supply this evidence. Missing telemetry does not release
resources, change the workload assertion, or authorize another execution.
"""
import datetime as dt
import hashlib
import math

CAPABILITY='npu-condition-evidence-v1'
SCHEMA='ascend-lab-npu-conditions/v1'
FILE='npu-conditions.json'
MAX_SAMPLES=64
MAX_GAP_SECONDS=30
DISCLAIMER='Discrete management snapshots only; no daily numerical or performance UT is linked. Running occupancy may be this task itself. Frequency was not collected.'


def epoch(value):
    if not isinstance(value,str) or len(value)>64:raise ValueError('Invalid sample time')
    instant=dt.datetime.fromisoformat(value)
    if instant.tzinfo is None:raise ValueError('Sample time requires timezone')
    return instant.timestamp()


def sample(record,job):
    if not isinstance(record,dict) or record.get('status','ok')!='ok':raise ValueError('Management observation unavailable')
    if record.get('policy_sha256')!=job.get('_npu_policy_sha256'):raise ValueError('Device policy differs')
    epoch(record.get('sampled_at'))
    ids=job['allocation']['npu_ids'];rows=record.get('devices')
    if not isinstance(rows,list) or len(rows)>256:raise ValueError('Invalid inventory')
    selected=[]
    for physical in ids:
        found=[row for row in rows if isinstance(row,dict) and type(row.get('id')) is int and row['id']==physical]
        if len(found)!=1:raise ValueError('Allocated device missing or duplicated')
        item=found[0];util=item.get('utilization_pct');count=item.get('process_count')
        if (item.get('health') not in {'OK','Warning','Alarm','UNKNOWN'} or type(count) is not int or not 0<=count<2**31
            or type(util) not in {int,float} or not math.isfinite(util) or not 0<=util<=100):raise ValueError('Invalid device sample')
        row={key:item[key] for key in ['id','health','utilization_pct','process_count']}
        for key,lower,upper in [('temperature_c',-100,250),('power_w',0,100000)]:
            value=item.get(key)
            row[key]=value if type(value) in {int,float} and math.isfinite(value) and lower<=value<=upper else None
        selected.append(row)
    return {'sampled_at':record['sampled_at'],'devices':selected}


def capture(job,record):
    """Called inside the controller transaction; bounded even for long attempts."""
    trace=job.setdefault('_npu_conditions',{'samples':[],'sample_count':0,'dropped_samples':0,
        'management_error_count':0,'out_of_order_count':0,'conflicting_sample_count':0,'alert_sample_count':0,'unknown_health_count':0,
        'max_gap_seconds':0,'first_sampled_at':None,'last_sampled_at':None})
    try:reading=sample(record,job)
    except (ValueError,TypeError,KeyError,OverflowError):
        trace['management_error_count']+=1
        return
    timestamp=reading['sampled_at'];previous=trace['last_sampled_at']
    if previous:
        delta=epoch(timestamp)-epoch(previous)
        if delta==0:
            if reading!=trace['samples'][-1]:
                trace['conflicting_sample_count']=trace.get('conflicting_sample_count',0)+1
                trace['alert_sample_count']+=int(any(row['health'] in {'Warning','Alarm'} for row in reading['devices']))
                trace['unknown_health_count']+=int(any(row['health']=='UNKNOWN' for row in reading['devices']))
            return  # Cached Node observations are not new samples.
        if delta<0:
            trace['out_of_order_count']+=1
            trace['alert_sample_count']+=int(any(row['health'] in {'Warning','Alarm'} for row in reading['devices']))
            trace['unknown_health_count']+=int(any(row['health']=='UNKNOWN' for row in reading['devices']))
            return
        trace['max_gap_seconds']=max(trace['max_gap_seconds'],delta)
    else:trace['first_sampled_at']=timestamp
    trace['last_sampled_at']=timestamp;trace['sample_count']+=1
    trace['alert_sample_count']+=int(any(row['health'] in {'Warning','Alarm'} for row in reading['devices']))
    trace['unknown_health_count']+=int(any(row['health']=='UNKNOWN' for row in reading['devices']))
    trace['samples'].append(reading)
    if len(trace['samples'])>MAX_SAMPLES:
        # Keep first and recent snapshots; counters retain dropped adverse evidence.
        del trace['samples'][1];trace['dropped_samples']+=1


def fallback(job):
    return {'status':'unverified' if job.get('resources',{}).get('npu') else 'not_applicable',
        'numerical_qualification':'unverified','performance_qualification':'unverified',
        'daily_ut':'not_linked','note':DISCLAIMER}


def finalize(jobs,job,result):
    """Central metadata is outside the Node upload manifest, with its own SHA."""
    from jobs import atomic_json,now
    summary=fallback(job)
    if summary['status']=='not_applicable':
        job['device_conditions']=summary
        return summary
    trace=job.get('_npu_conditions',{})
    reasons=[];boundaries={}
    for key,field in [('admission','npu_admission_observation'),('release','npu_release_observation')]:
        try:
            reading=sample(result.get(field),job)
            instant=epoch(reading['sampled_at'])
            # Complete may arrive much later after an interrupted artifact upload.
            # Validate historical boundaries against this attempt, not HTTP age.
            if instant<epoch(job['reserved_at'])-15 or instant>epoch(now())+1:raise ValueError('Boundary outside attempt')
            boundaries[key]=reading
        except (ValueError,TypeError,KeyError,OverflowError):reasons.append(key+'_missing_or_invalid')
    if not job.get('_start_authorized'):reasons.append('no_start_authorization')
    if result.get('npu_release_basis')=='no-container-start-intent':reasons.append('no_container_start_intent')
    alerts=trace.get('alert_sample_count',0)
    for name,reading in boundaries.items():
        if any(row['health'] in {'Warning','Alarm'} for row in reading['devices']):alerts+=1
        if any(row['health']=='UNKNOWN' for row in reading['devices']):reasons.append(name+'_unknown_health')
        if any(row['utilization_pct']!=0 or row['process_count']!=0 for row in reading['devices']):reasons.append(name+'_not_idle')
    times=[epoch(item['sampled_at']) for item in boundaries.values()]
    for key in ['first_sampled_at','last_sampled_at']:
        if trace.get(key):times.append(epoch(trace[key]))
    gap=trace.get('max_gap_seconds',0)
    if trace.get('first_sampled_at'):
        first=epoch(trace['first_sampled_at']);last=epoch(trace['last_sampled_at'])
        # Samples dropped for size have already contributed to the online max.
        # Never turn the retention gap into a fictitious observation outage.
        gap=max([gap]+[first-t for t in times if t<first]+[t-last for t in times if t>last])
    elif len(times)==2:gap=abs(times[1]-times[0])
    if gap>MAX_GAP_SECONDS:reasons.append('sampling_gap')
    for field in ['management_error_count','out_of_order_count','conflicting_sample_count','unknown_health_count']:
        if trace.get(field):reasons.append(field)
    if 'admission' in boundaries and 'release' in boundaries and epoch(boundaries['admission']['sampled_at'])>epoch(boundaries['release']['sampled_at']):
        reasons.append('boundary_order_invalid')
    summary.update(status='adverse_observed' if alerts else 'incomplete' if reasons else 'sampled_no_alerts',
        alert_sample_count=alerts,reasons=sorted(set(reasons)),max_gap_seconds=round(gap,3),
        snapshot_count=trace.get('sample_count',0)+len(boundaries),frequency_mhz=None)
    document={'schema':SCHEMA,'source':'controller-normalized authenticated Node management evidence',
        'scope':'reservation through terminal completion; includes preparation and artifact transfer, not only execution',
        'job_id':job['id'],'attempt_id':job['attempt_id'],'node_id':job['node_id'],
        'physical_id':job.get('_physical_id'),'device_ids':job['allocation']['npu_ids'],
        'policy_sha256':job.get('_npu_policy_sha256'),'environment_sha256':job.get('environment_sha256'),
        'recorded_at':now(),'summary':summary,'boundaries':boundaries,'telemetry':trace}
    path=jobs.directory(job['id'])/FILE
    atomic_json(path,document)
    summary={**summary,'evidence':FILE,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'origin':'controller; not part of the Node artifact receipt'}
    job['device_conditions']=summary
    return summary

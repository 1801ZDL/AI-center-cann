"""Bounded archive report presentation, never a source replay or device action."""
import hashlib
import json
import math
import os
import re
import threading

from archive_api import open_artifact
from dependency_conditions import confirmation
from diagnostic_case import redact
from jobs import JobError
from node_storage import manifest_sha256

CAPABILITY='archive-report-view-v1'
KINDS={'accuracy':('results/accuracy-analysis.json',32768),
       'compiler':('results/compiler-case.json',16384),
       'simulation':('results/simulation-analysis.json',128*1024),
       'operator':('results/operator-analysis.json',64*1024)}
_SLOTS=threading.BoundedSemaphore(2)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def parse(raw,maximum_nodes=20000):
    def integer(value):
        if len(value)>32:raise ValueError('Integer exceeds presentation budget')
        return int(value)
    def real(value):
        result=float(value)
        if not math.isfinite(result):raise ValueError('Finite report numbers required')
        return result
    def invalid(_):raise ValueError('Finite report JSON required')
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out:raise ValueError('Duplicate report key')
            out[k]=v
        return out
    value=json.loads(raw,parse_int=integer,parse_float=real,parse_constant=invalid,object_pairs_hook=unique)
    remaining=maximum_nodes
    def walk(v,depth=0):
        nonlocal remaining
        remaining-=1
        if remaining<0 or depth>16:raise ValueError('Report structure budget exceeded')
        if isinstance(v,dict):
            if any(not isinstance(k,str) or len(k)>4096 for k in v):raise ValueError('Report key exceeds budget')
            for x in v.values():walk(x,depth+1)
        elif isinstance(v,list):
            for x in v:walk(x,depth+1)
        elif isinstance(v,str) and len(v)>8192:raise ValueError('Report string exceeds budget')
    walk(value);return value


def raw_file(directory,name,limit):
    with open_artifact(directory,name,limit) as f:
        before=os.fstat(f.fileno());raw=f.read(limit+1);after=os.fstat(f.fileno())
    if (len(raw)>limit or len(raw)!=before.st_size
        or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
        raise ValueError('Report changed while reading')
    return raw


def number(value,maximum=1e12,positive=False):
    if type(value) not in {int,float} or not math.isfinite(value) or not 0<=value<=maximum or positive and value==0:
        raise ValueError('Invalid report measurement')
    return value


def count(value,maximum=1048576):
    if type(value) is not int or not 0<=value<=maximum:raise ValueError('Invalid report count')
    return value


def string(value,limit=1024):
    if not isinstance(value,str) or not value or len(value)>limit:raise ValueError('Invalid report label')
    return value


def boolean(value):
    if type(value) is not bool:raise ValueError('Invalid report boolean')
    return value


def accuracy(value):
    from accuracy_data import config_schema
    if value.get('schema')!='ascend-lab-accuracy-report/v1' or value.get('purpose')!='offline_accuracy_replay':raise ValueError('Unsupported accuracy report')
    config=config_schema(value['input']);result=value['comparison'];elements=count(result['elements']);mismatches=count(result['mismatches'],elements)
    passed=boolean(result['passed'])
    if elements!=math.prod(config['shape']) or elements==0 or passed!=(mismatches==0):raise ValueError('Inconsistent accuracy summary')
    examples=result.get('examples',[])
    if not isinstance(examples,list) or len(examples)>16:raise ValueError('Accuracy sample budget exceeded')
    metrics=result.get('numeric_metrics')
    if metrics is not None:
        if not isinstance(metrics,dict):raise ValueError('Invalid accuracy metrics')
        for name in ['max_abs_error','max_relative_error']:
            if metrics.get(name) is not None:number(metrics[name],1.7976931348623157e308)
    return {'name':config['name'],'shape':config['shape'],'policy':config['policy'],'reference_id':config['reference']['origin']['id'],
        'elements':elements,'mismatches':mismatches,'passed_against_supplied_reference':passed,'numeric_metrics':metrics,'examples':examples}


def compiler(value):
    from compiler_diagnostic import report_schema
    report_schema(value);tool=value['tool'];outcome=value['outcome'];source=value['source']
    return {'tool':{k:tool[k] for k in ['name','version','sha256']},'source':{k:source[k] for k in ['artifact','bytes','sha256']},
        'exit_code':outcome['exit_code'],'timed_out':outcome['timed_out'],'stderr_bytes':outcome['stderr_bytes'],
        'stderr_truncated':outcome['stderr_truncated']}


def simulation(value):
    from simulation_data import config_schema
    if value.get('schema')!='ascend-lab-simulation-report/v1' or value.get('purpose')!='offline_simulation_analysis':raise ValueError('Unsupported simulator report')
    config=config_schema(value['input']);result=value['analysis'];capture=result['capture'];cores=result['cores']
    if (capture.get('capture_state') not in {'abnormal','unverified'} or capture.get('capture_completeness_certified') is not False
        or not isinstance(capture.get('logged_child_signals'),list) or len(capture['logged_child_signals'])>128
        or any(type(v) is not int or not 0<=v<=999 for v in capture['logged_child_signals'])
        or not isinstance(cores,list) or len(cores)!=len(config['cores'])):raise ValueError('Invalid simulator collection summary')
    out=[]
    for original,core in zip(config['cores'],cores):
        if core.get('id')!=original['id']:raise ValueError('Simulator core identity differs')
        facts=core['analysis'];events=count(facts['events'],100000);span=number(facts['span_us'],positive=True);pipes=facts['pipes']
        if not isinstance(pipes,dict) or not 1<=len(pipes)<=33 or facts.get('pid_used_for_pipe_classification') is not False:
            raise ValueError('Invalid simulator pipe presentation')
        def intervals(record):
            n=count(record['events'],100000);covered=number(record['union_us'])
            if covered>span+1e-9:raise ValueError('Reported interval exceeds its core span')
            return {'events':n,'union_us':covered}
        chosen={string(name,32):intervals(record) for name,record in pipes.items()}
        if sum(v['events'] for v in chosen.values())!=events:raise ValueError('Reported pipe event counts differ')
        groups=facts['instruction_groups']
        if not isinstance(groups,dict) or len(groups)>16:raise ValueError('Invalid simulator instruction groups')
        groups={string(k,32):intervals(v) for k,v in groups.items()}
        locations=facts.get('top_source_locations',[])
        if not isinstance(locations,list) or len(locations)>8:raise ValueError('Source hotspot budget exceeded')
        out.append({'id':core['id'],'events':events,'span_us':span,'pipes':chosen,'instruction_groups':groups,
            'csv_trace_counts_match':boolean(facts['csv_trace_counts_match']),'count_discrepancies':count(facts['count_discrepancies'],200000),
            'unmapped_events':count(facts['unmapped_events'],events),
            'source_locations':[{'name':string(v['name']),**intervals(v)} for v in locations]})
    return {'name':config['name'],'origin_id':config['origin']['id'],
        'capture_state':capture['capture_state'],'capture_completeness_certified':False,
        'logged_child_signals':capture['logged_child_signals'],'logged_parse_finished':boolean(capture['logged_parse_finished']),
        'exit_claim_conflicts_with_log':boolean(capture['exit_claim_conflicts_with_log']),'cores':out,'timestamp_unit':'us',
        'optimization_speedup':None,'pipe_mapping':'instruction_and_pc_from_csv'}


def operator(value):
    from operator_profile import validate_report
    validate_report(value)
    blockers=value['comparison']['blockers']
    if len(blockers)>32 or any(not isinstance(v,str) or len(v)>512 for v in blockers):raise ValueError('Operator blocker budget exceeded')
    return {'operator':string(value['operator']),
        'sources':[{k:v.get(k) for k in ['id','samples','mean_us','min_us','max_us','physical_device','clock_mhz_before','clock_mhz_after']} for v in value['sources']],
        'observed_duration_ratio':value['comparison']['observed_duration_ratio'],'optimization_speedup':None,
        'blockers':value['comparison']['blockers'],'interpretation':'历史耗时比例，不等于已验证优化收益。'}


def read(jobs,job,kind,config):
    if kind not in KINDS:raise JobError('Unknown diagnostic report kind',400)
    if not _SLOTS.acquire(blocking=False):raise JobError('Diagnostic report readers busy; retry this view',503)
    try:
        with jobs.lifecycle.reader('archive',job['id']):
            projection,reason=confirmation(job)
            if projection is None:raise JobError('Confirmed terminal archive is required for report viewing',409)
            directory=jobs.directory(job['id']);hashes=parse(raw_file(directory,'artifact-hashes.json',2*1024**2))
            if hashes.get('version')!=1 or manifest_sha256(hashes['files'])!=projection['artifact_manifest_sha256']:
                raise ValueError('Archive manifest differs from confirmed receipt')
            entries={v['name']:v for v in hashes['files']};name,limit=KINDS[kind]
            if name not in entries:raise JobError('Diagnostic report is not present in the confirmed archive',404)
            if entries[name]['size']>limit:raise ValueError('Diagnostic report exceeds presentation budget')
            raw=raw_file(directory,name,limit)
            if len(raw)!=entries[name]['size'] or sha(raw)!=entries[name]['sha256']:raise ValueError('Report differs from confirmed archive')
            value=parse(raw);summary={'accuracy':accuracy,'compiler':compiler,'simulation':simulation,'operator':operator}[kind](value)
            if kind=='compiler':
                error='results/compiler-stderr.txt';summary['stderr_archived']=error in entries;summary['stderr_excerpt']=''
                if error in entries:
                    blob=raw_file(directory,error,8192)
                    if sha(blob)!=entries[error]['sha256'] or len(blob)!=entries[error]['size'] or len(blob)!=min(summary['stderr_bytes'],8192):raise ValueError('Compiler error differs from confirmed archive')
                    summary['stderr_excerpt']=blob.decode('utf-8',errors='replace')[:4096]
                elif summary['stderr_bytes']!=0:raise ValueError('Declared compiler error is absent')
            def clean(v):
                if isinstance(v,str):return redact(v,config)
                if isinstance(v,list):return [clean(x) for x in v]
                if isinstance(v,dict):return {redact(k,config):clean(x) for k,x in v.items()}
                return v
            summary=clean(summary)
            result={'schema':'ascend-lab-report-view/v1','job':{k:job.get(k) for k in ['id','state','template','attempt_id','node_id']},
                'report':{'kind':kind,'name':name,'bytes':len(raw),'sha256':sha(raw),'schema':value['schema']},
                'verification':{'archive_manifest_verified':True,'report_bytes_verified':True,'source_bytes_recomputed':False,
                    'numerical_correctness_certified':False,'historical_capture_certified':False,
                    'scope':'已核报告字节与Node确认的归档摘要。内容由工作负载提供，完整源文件未在本网页重算；可使用个人CLI/MCP核验。'},
                'summary':summary}
            if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>192*1024:raise ValueError('Report response exceeds budget')
            return result
    except JobError:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        raise JobError('Diagnostic report is invalid, incomplete or differs from its confirmed archive',409) from None
    finally:_SLOTS.release()

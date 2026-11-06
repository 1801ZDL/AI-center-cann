"""Bounded offline simulator evidence; no device, compiler or source-file access.

Chrome trace timestamps are microseconds; displayTimeUnit is a viewer setting.
Pipe identity comes from (instruction, PC) CSV records, never reused trace PIDs.
"""
import collections
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re

INPUT='simulation-input.json'
DRIVER='simulation_data.py'
REPORT='results/simulation-analysis.json'
PREFIX='results/simulation-input/'
TRACE_LIMIT=32*1024**2
CSV_LIMIT=1024**2
TOTAL_LIMIT=120*1024**2
MAX_EVENTS=100000
MAX_REPORT=128*1024
PHASES={'X','M','s','t','f','i','I'}


def sha(raw):return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def invalid(_):raise ValueError('Finite JSON required')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    return json.loads(raw,parse_constant=invalid,object_pairs_hook=unique)


def digest(value):return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None


def finite(value,maximum=1e12):
    try:return type(value) in {int,float} and math.isfinite(value) and 0<=value<=maximum
    except OverflowError:return False


def text(value,maximum):
    return isinstance(value,str) and 1<=len(value)<=maximum and not any(ord(c)<32 for c in value)


def config_schema(value):
    if (not isinstance(value,dict) or set(value)!={'schema','name','timestamp_unit','origin','log','cores'}
        or value.get('schema')!='ascend-lab-simulation-input/v1' or not text(value.get('name'),160)
        or value.get('timestamp_unit')!='us' or not isinstance(value.get('cores'),list) or not 1<=len(value['cores'])<=4):
        raise ValueError('Explicit bounded per-core simulator input and microsecond timestamps required')
    origin=value['origin']
    if (not isinstance(origin,dict) or set(origin)!={'id','kind','elf_sha256','claimed_exit_code','scope'}
        or not text(origin.get('id'),128) or origin.get('kind') not in {'historical_simulator_output','user_supplied'}
        or not digest(origin.get('elf_sha256')) or not text(origin.get('scope'),2048)
        or origin.get('claimed_exit_code') is not None and (type(origin['claimed_exit_code']) is not int or not -255<=origin['claimed_exit_code']<=255)):
        raise ValueError('Explicit caller-supplied simulator origin required')
    def file(item,name):
        if not isinstance(item,dict) or set(item)!={'file','sha256'} or item.get('file')!=name or not digest(item.get('sha256')):
            raise ValueError('Use fixed evidence names and full hashes')
    file(value['log'],'capture.log');seen=set()
    for i,core in enumerate(value['cores']):
        if (not isinstance(core,dict) or set(core)!={'id','trace','instructions'} or not isinstance(core.get('id'),str)
            or not re.fullmatch(r'core[0-9]{1,3}\.(?:vec|cube)core[0-9]{1,2}',core['id']) or core['id'] in seen):
            raise ValueError('Unique bounded core identities required')
        seen.add(core['id']);file(core['trace'],f'core{i}.trace.json');file(core['instructions'],f'core{i}.instructions.csv')
    return value


def input_files(value):
    value=config_schema(value)
    return [(value['log'],65536),*[(core[role],TRACE_LIMIT if role=='trace' else CSV_LIMIT)
        for core in value['cores'] for role in ['trace','instructions']]]


def checked(raw,item,limit):
    if not isinstance(raw,bytes) or not 0<len(raw)<=limit or sha(raw)!=item['sha256']:
        raise ValueError('Evidence bytes differ from extent or declared hash')
    return raw


def union(intervals):
    end=0.;total=0.
    for a,b in sorted(intervals):
        total+=max(0.,b-max(a,end));end=max(end,b)
    return total


def instruction_table(raw):
    csv.field_size_limit(8192)
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    if reader.fieldnames!=['instr','addr','pipe','call_count','cycles','running_time(us)','detail']:
        raise ValueError('Unsupported instruction CSV columns')
    table={};row_count=0
    for row in reader:
        row_count+=1
        if row_count>10000 or None in row or any(not isinstance(v,str) or len(v)>4096 for v in row.values()):
            raise ValueError('Instruction CSV budget exceeded or malformed row')
        if (not re.fullmatch('[A-Za-z0-9_.$-]{1,80}',row['instr']) or not re.fullmatch('[A-Za-z0-9_]{1,32}',row['pipe'])
            or any(not re.fullmatch('[0-9]{1,16}',row[k]) for k in ['addr','call_count','cycles'])
            or int(row['addr'])>=2**64 or not 0<int(row['call_count'])<=MAX_EVENTS
            or not finite(float(row['running_time(us)']))):raise ValueError('Invalid instruction CSV facts')
        key=(row['instr'],int(row['addr']));record=table.setdefault(key,{'pipe':row['pipe'],'count':0})
        if record['pipe']!=row['pipe']:raise ValueError('Ambiguous instruction/PC pipe mapping')
        record['count']+=int(row['call_count'])
    if not row_count or len({v['pipe'] for v in table.values()})>32:raise ValueError('Empty or excessive instruction pipe table')
    return table,row_count


def core_summary(trace_raw,csv_raw):
    data=strict_json(trace_raw)
    if (not isinstance(data,dict) or type(data.get('schemaVersion')) is not int or data['schemaVersion']!=1
        or data.get('profilingType')!='op' or data.get('displayTimeUnit') not in {'ns','us','ms',None}
        or not isinstance(data.get('traceEvents'),list) or not 1<=len(data['traceEvents'])<=MAX_EVENTS):
        raise ValueError('Unsupported bounded simulator Chrome trace')
    table,csv_rows=instruction_table(csv_raw);phases=collections.Counter();observed=collections.Counter();metadata=collections.defaultdict(set)
    pipes=collections.defaultdict(list);instructions=collections.defaultdict(list);locations=collections.defaultdict(list)
    groups={name:[] for name in ['copy_template','simt_load','reduce_max','reduce_min','sync']};all_intervals=[]
    zero=0;start=None;end=0.;unmapped=0;debug_events=0
    for event in data['traceEvents']:
        if not isinstance(event,dict) or not isinstance(event.get('ph'),str) or event['ph'] not in PHASES:raise ValueError('Unsupported trace phase')
        phases[event['ph']]+=1
        if event['ph']=='M':
            if event.get('name')=='process_name':
                pid=event.get('pid');args=event.get('args',{})
                if type(pid) is not int or not isinstance(args,dict) or not text(args.get('name'),80):raise ValueError('Invalid trace process metadata')
                metadata[pid].add(args['name'])
            continue
        if event['ph']!='X':continue
        if (not isinstance(event.get('name'),str) or not re.fullmatch('[A-Za-z0-9_.$-]{1,80}',event['name']) or not finite(event.get('ts'))
            or not finite(event.get('dur')) or not finite(event['ts']+event['dur'])
            or type(event.get('pid')) is not int or type(event.get('tid')) is not int
            or not isinstance(event.get('args'),dict)):
            raise ValueError('Invalid complete instruction event')
        args=event['args'];pc=args.get('pc_addr');code=args.get('code','')
        if not isinstance(pc,str) or not re.fullmatch('0x[a-fA-F0-9]{1,16}',pc) or not isinstance(code,str) or len(code)>8192:
            raise ValueError('Bounded instruction PC and debug locations required')
        name=event['name'];key=(name,int(pc,16));observed[key]+=1
        interval=(event['ts'],event['ts']+event['dur']);all_intervals.append(interval)
        start=interval[0] if start is None else min(start,interval[0]);end=max(end,interval[1]);zero+=event['dur']==0
        pipe=table[key]['pipe'] if key in table else 'UNMAPPED';unmapped+=key not in table
        pipes[pipe].append(interval);instructions[name].append(interval)
        if len(instructions)>512:raise ValueError('Instruction-name budget exceeded')
        selected=[]
        for line in code.splitlines():
            if not line:continue
            if not text(line,1024):raise ValueError('Invalid debug location')
            selected.append(line)
        if len(selected)>16:raise ValueError('Inline debug depth exceeded')
        debug_events+=bool(selected)
        for line in sorted(set(selected)):locations[line].append(interval)
        if len(locations)>2048:raise ValueError('Debug-location budget exceeded')
        category={'SIMT_LDG':'simt_load','RV_VCMAX':'reduce_max','RV_VCMIN':'reduce_min','SET_FLAG':'sync','WAIT_FLAG':'sync'}.get(name)
        if category:groups[category].append(interval)
        if any('Copy1D.cpp:' in line for line in selected):groups['copy_template'].append(interval)
    if start is None or end<=start:raise ValueError('No positive recorded instruction span')
    differences=[{'instruction':k[0],'pc':hex(k[1]),'trace_count':observed[k],'csv_count':table.get(k,{}).get('count',0)}
        for k in sorted(set(table)|set(observed)) if observed[k]!=table.get(k,{}).get('count',0)]
    def summary(intervals):return {'events':len(intervals),'union_us':union(intervals)}
    def top(items,limit):
        records=[{'name':name,**summary(intervals)} for name,intervals in items.items()]
        return sorted(records,key=lambda r:(-r['union_us'],-r['events'],r['name']))[:limit]
    return {'events':sum(observed.values()),'zero_duration_events':zero,'phase_counts':dict(sorted(phases.items())),
        'timestamp_unit':'us','display_time_unit':data.get('displayTimeUnit'),'start_us':start,'end_us':end,'span_us':end-start,
        'recorded_union_us':union(all_intervals),'csv_rows':csv_rows,'pipe_mapping':'instruction_and_pc_from_csv',
        'pid_used_for_pipe_classification':False,'ambiguous_process_ids':sorted(k for k,v in metadata.items() if len(v)>1)[:32],
        'unmapped_events':unmapped,'csv_trace_counts_match':not differences,'count_discrepancies':len(differences),'count_discrepancy_examples':differences[:16],
        'pipes':{k:summary(v) for k,v in sorted(pipes.items())},'instruction_groups':{k:summary(v) for k,v in groups.items()},
        'top_instructions':top(instructions,12),'events_with_debug_locations':debug_events,'top_source_locations':top(locations,8),
        'interpretation':'Per-core recorded interval unions; categories and pipes overlap. Source locations include inlining. Neither sums nor unions establish removable critical-path time, achieved hardware utilization, deadlock or optimization speedup.'}


def capture_summary(raw,claimed_exit_code):
    log=raw.decode('utf-8')
    signals=sorted({int(v) for v in re.findall(r'Child process killed by signal ([0-9]{1,3})\b',log)})
    failed=bool(signals) or 'Running task failed' in log or claimed_exit_code not in {None,0}
    return {'claimed_exit_code':claimed_exit_code,'logged_child_signals':signals,'logged_running_task_failed':'Running task failed' in log,
        'logged_parse_finished':'Profiling data parse finished.' in log,'capture_state':'abnormal' if failed else 'unverified',
        'capture_completeness_certified':False,'exit_claim_conflicts_with_log':claimed_exit_code==0 and bool(signals or 'Running task failed' in log),
        'scope':'Caller-supplied historical log and exit claim. Parsing success does not establish normal collection exit or complete instruction coverage.'}


def analyze(value,read):
    value=config_schema(value);total=0
    def get(item,limit):
        nonlocal total
        raw=checked(read(item['file'],limit),item,limit);total+=len(raw)
        if total>TOTAL_LIMIT:raise ValueError('Total simulator input budget exceeded')
        return raw
    capture=capture_summary(get(value['log'],65536),value['origin']['claimed_exit_code']);cores=[]
    for core in value['cores']:
        trace=get(core['trace'],TRACE_LIMIT);instructions=get(core['instructions'],CSV_LIMIT)
        cores.append({'id':core['id'],'trace':core['trace'],'instructions':core['instructions'],'analysis':core_summary(trace,instructions)})
    return {'capture':capture,'cores':cores,'source_bytes':total,'all_csv_trace_counts_match':all(c['analysis']['csv_trace_counts_match'] for c in cores),
        'origin_independently_certified':False,'optimization_speedup':None,'new_npu_launches':0,
        'scope':'Offline analysis of supplied per-core simulator traces and instruction CSVs. No new simulation, device timing, correctness, root cause or repair is established.'}


def bounded_read(path,limit):
    import os
    import stat
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or not 0<st.st_size<=limit:raise ValueError('Regular bounded simulator file required')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(limit+1)
        if len(raw)!=st.st_size:raise ValueError('Simulator evidence changed while reading')
        return raw
    finally:os.close(fd)


def workload():
    import os
    root=Path(os.environ['LAB_JOB_DIR']);config=bounded_read(INPUT,16384);value=config_schema(strict_json(config))
    result=analyze(value,lambda name,limit:bounded_read(name,limit))
    report={'schema':'ascend-lab-simulation-report/v1','purpose':'offline_simulation_analysis','driver_sha256':sha(Path(__file__).read_bytes()),
        'input_sha256':sha(config),'input':value,'analysis':result}
    raw=(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode()
    if len(raw)>MAX_REPORT:raise ValueError('Simulation report exceeds budget')
    archive=root/PREFIX;archive.mkdir(mode=0o700);(archive/INPUT).write_bytes(config)
    for item,limit in input_files(value):(archive/item['file']).write_bytes(checked(bounded_read(item['file'],limit),item,limit))
    (root/REPORT).write_bytes(raw)
    with (root/'results/simulation-analysis.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['core','pipe','events','recorded_union_us','span_us','capture_state','capture_completeness_certified'])
        for core in result['cores']:
            for pipe,record in core['analysis']['pipes'].items():writer.writerow([core['id'],pipe,record['events'],record['union_us'],core['analysis']['span_us'],result['capture']['capture_state'],False])
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,
        'purpose':'offline_simulation_analysis','artifacts':[REPORT,'results/simulation-analysis.csv'],
        'capture_state':result['capture']['capture_state'],'capture_completeness_certified':False,'new_npu_launches':0})+'\n')
    print(json.dumps({'purpose':'offline_simulation_analysis','cores':len(result['cores']),'capture_state':result['capture']['capture_state'],
        'capture_completeness_certified':False,'new_npu_launches':0}),flush=True)


if __name__=='__main__':workload()

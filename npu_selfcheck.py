"""Fixed, bounded NPU microchecks and an owner-authenticated daily submission CLI.

The host client uses only stdlib. Torch is imported only inside the managed,
non-root NPU runner. No CPU fallback, driver reset or administrator operation.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys

DRIVER='npu_selfcheck.py'
REPORT='results/device-microcheck.json'
SCHEMA='ascend-lab-device-microcheck/v1'
REFERENCE='bounded-exact-integer-microchecks/v1'
WARMUP=10
ROUNDS=3
SAMPLES=10


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def source_sha():return sha(Path(__file__).read_bytes())
def finite(value):
    try:return type(value) in {int,float} and math.isfinite(value)
    except OverflowError:return False


def reference():
    """Exact small integers: no GPU reference or floating tolerance to loosen."""
    copy=[i*17%257-128 for i in range(1024)]
    a=[i*13%31-15 for i in range(1024)];b=[i*7%23-11 for i in range(1024)]
    reduce=[i*7%9-4 for i in range(512)]
    left=[(i*3+j*2)%5-2 for i in range(16) for j in range(16)]
    right=[(i*7+j*11)%5-2 for i in range(16) for j in range(16)]
    product=[sum(left[i*16+k]*right[k*16+j] for k in range(16)) for i in range(16) for j in range(16)]
    return [
        {'name':'copy_f32','dtype':'float32','shape':[1024],'a':copy,'expected':copy},
        {'name':'axpy_f32','dtype':'float32','shape':[1024],'a':a,'b':b,'expected':[3*x+2*y for x,y in zip(a,b)]},
        {'name':'sum_f32','dtype':'float32','shape':[16,32],'a':reduce,'expected':[sum(reduce[i:i+32]) for i in range(0,512,32)]},
        *[{'name':'mm_'+dtype,'dtype':dtype,'shape':[16,16],'a':left,'b':right,'expected':product}
          for dtype in ['float32','float16','bfloat16']]]


def reference_sha():return sha(canonical({'id':REFERENCE,'cases':reference()}).encode())


def validate_report(value):
    """Independently recompute every reported value on CPU; no input execution."""
    if (not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('reference_sha256')!=reference_sha()
        or value.get('driver_sha256')!=source_sha()):raise ValueError('Unknown self-check driver or reference')
    runtime=value.get('runtime',{})
    if (runtime.get('backend')!='npu' or type(runtime.get('device_count')) is not int or runtime['device_count']!=1
        or type(runtime.get('device_index')) is not int or runtime['device_index']!=0):raise ValueError('No single-NPU execution assertion')
    actual=value.get('checks');cases=reference()
    if not isinstance(actual,list) or len(actual)!=len(cases):raise ValueError('Incomplete microcheck set')
    comparisons=[]
    for expected,found in zip(cases,actual):
        if (not isinstance(found,dict) or found.get('name')!=expected['name'] or found.get('dtype')!=expected['dtype']
            or not isinstance(found.get('values'),list) or len(found['values'])!=len(expected['expected'])):
            raise ValueError('Microcheck shape/order/dtype differs')
        differences=[];nonfinite=0;maximum=0
        for i,(want,got) in enumerate(zip(expected['expected'],found['values'])):
            if isinstance(got,str) and got in {'nan','inf','-inf'}:nonfinite+=1;different=True;delta=None
            elif finite(got):
                delta=abs(got-want);maximum=max(maximum,delta);different=got!=want
            else:raise ValueError('Invalid observation encoding')
            if different:differences.append({'index':i,'expected':want,'actual':got,'absolute_error':delta})
        comparisons.append({'name':expected['name'],'passed':not differences,'values':len(found['values']),
            'mismatches':len(differences),'nonfinite':nonfinite,'max_abs_error':maximum,'examples':differences[:8]})
    timing=value.get('timing',{})
    if (not isinstance(timing,dict) or timing.get('method')!='npu-events' or timing.get('workload')!='mm512_float16'
        or timing.get('warmup')!=WARMUP):raise ValueError('Unknown measurement contract')
    performance={'status':'unverified','reason':'No approved same-device/environment performance baseline or frequency evidence',
                 'method':'npu-events','workload':'mm512_float16'}
    if timing.get('status')=='collected':
        rounds=timing.get('rounds_us')
        if (not isinstance(rounds,list) or len(rounds)!=ROUNDS or any(not isinstance(r,list) or len(r)!=SAMPLES for r in rounds)
            or any(not finite(x) or x<=0 for r in rounds for x in r)):
            raise ValueError('Invalid finite raw timing rounds')
        ordered=[sorted(r) for r in rounds]
        medians=[r[SAMPLES//2-1]+(r[SAMPLES//2]-r[SAMPLES//2-1])/2 for r in ordered]
        ratio=max(medians)/min(medians)
        performance.update(samples=ROUNDS*SAMPLES,round_medians_us=medians,mean_us=statistics.mean(x for r in rounds for x in r),
                           round_median_ratio=ratio if math.isfinite(ratio) else None)
        if not math.isfinite(ratio):performance['statistic_warning']='Round ratio overflow; raw finite samples retained, performance unverified'
    elif timing.get('status')!='unavailable':raise ValueError('Unknown timing observation state')
    return {'precision_passed':all(row['passed'] for row in comparisons),'checks':comparisons,'performance':performance,
        'reference_id':REFERENCE,'reference_sha256':reference_sha(),
        'scope':'Six bounded microchecks only; not exhaustive device health, operator correctness, hardware fault attribution or performance qualification.'}


def run():
    if (os.geteuid()==0 or not Path('/payload/run.json').is_file() or not Path('/inputs/'+DRIVER).is_file()
        or not os.environ.get('ASCEND_RT_VISIBLE_DEVICES')):raise ValueError('Run only as a managed non-root NPU task')
    import torch
    import torch_npu
    npu=torch_npu.npu
    if not npu.is_available() or npu.device_count()!=1:raise ValueError('Exactly one accessible NPU is required; no CPU fallback')
    npu.set_device(0)
    report={'schema':SCHEMA,'driver_sha256':source_sha(),'reference_sha256':reference_sha(),
        'runtime':{'backend':'npu','device_index':0,'device_count':npu.device_count(),'device_name':npu.get_device_name(0),
                   'torch':torch.__version__,'torch_npu':getattr(torch_npu,'__version__','UNKNOWN')},'checks':[]}
    with torch.inference_mode():
        for case in reference():
            dtype=getattr(torch,case['dtype']);a=torch.tensor(case['a'],dtype=dtype).reshape(case['shape']).to('npu:0')
            if case['name']=='copy_f32':out=a.clone()
            elif case['name']=='axpy_f32':
                b=torch.tensor(case['b'],dtype=dtype).to('npu:0');out=a*3+b*2
            elif case['name']=='sum_f32':out=a.sum(dim=1)
            else:
                b=torch.tensor(case['b'],dtype=dtype).reshape(case['shape']).to('npu:0');out=torch.mm(a,b)
            if out.device.type!='npu':raise ValueError('Unexpected CPU execution output')
            values=out.cpu().to(torch.float64).reshape(-1).tolist()
            report['checks'].append({'name':case['name'],'dtype':case['dtype'],
                'values':[x if math.isfinite(x) else 'nan' if math.isnan(x) else 'inf' if x>0 else '-inf' for x in values]})
        timing={'method':'npu-events','workload':'mm512_float16','warmup':WARMUP,'status':'unavailable','rounds_us':[]}
        try:
            a=torch.ones((512,512),device='npu:0',dtype=torch.float16);b=a.clone();out=torch.empty_like(a)
            for _ in range(WARMUP):torch.mm(a,b,out=out)
            npu.synchronize()
            for _ in range(ROUNDS):
                samples=[]
                for _ in range(SAMPLES):
                    start=npu.Event(enable_timing=True);end=npu.Event(enable_timing=True)
                    start.record();torch.mm(a,b,out=out);end.record();end.synchronize()
                    sample=float(start.elapsed_time(end))*1000
                    if not math.isfinite(sample) or sample<=0:raise ValueError('NPU event resolution/measurement was not positive')
                    samples.append(sample)
                timing['rounds_us'].append(samples)
            timing['status']='collected'
        except Exception as error:timing['error']=type(error).__name__+': '+str(error)[:300]
    report['timing']=timing;validation=validate_report(report)
    target=Path('/work')/REPORT;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    target.write_text(json.dumps(report,allow_nan=False)+'\n')
    checks=[{'name':c['name'],'passed':c['passed'],'atol':0,'rtol':0,'max_abs_error':c['max_abs_error']} for c in validation['checks']]
    result={'schema':'ascend-lab-result/v1','kind':'accuracy','passed':validation['precision_passed'],'checks':checks,
            'microcheck_report':REPORT,'performance_qualified':False}
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(result,allow_nan=False)+'\n')
    print(json.dumps({'precision_passed':validation['precision_passed'],'performance_status':'unverified','report':REPORT}))
    return 0 if validation['precision_passed'] else 1


def request_for(input_id,lock,node,device,day,memory_mib=2048):
    from placement import validate
    target=validate({'node_id':node,'device_ids':[device]},1)
    if (not isinstance(lock,dict) or set(lock)-{'image_sha256','python','npu_name','machine','packages','tools','files'}
        or not re.fullmatch('sha256:[0-9a-f]{64}',str(lock.get('image_sha256')))
        or not re.fullmatch(r'\d+\.\d+\.\d+',str(lock.get('python')))
        or not re.fullmatch('[A-Za-z0-9_. -]{1,80}',str(lock.get('npu_name')))
        or str(lock.get('npu_name','')).upper()=='UNKNOWN'):
        raise ValueError('Self-check needs pinned image, exact Python and management NPU name')
    for key in ['packages','tools','files']:
        mapping=lock.get(key,{})
        if (not isinstance(mapping,dict) or len(mapping)>32
            or any(not isinstance(k,str) or not k or len(k)>500 or not isinstance(v,str) or not v or len(v)>500 for k,v in mapping.items())):
            raise ValueError('Invalid bounded environment lock mapping')
        if key in {'tools','files'} and any(not re.fullmatch('[0-9a-f]{64}',v) for v in mapping.values()):raise ValueError('Tool/file lock requires SHA256')
    if any(not isinstance(lock.get('packages',{}).get(name),str) or lock['packages'][name]=='__absent__' for name in ['torch','torch_npu']):
        raise ValueError('Self-check needs exact torch and torch_npu package versions')
    if type(memory_mib) is not int or not 512<=memory_mib<=65536:raise ValueError('Memory must be 512..65536 MiB')
    if not isinstance(day,str) or dt.date.fromisoformat(day).isoformat()!=day:raise ValueError('Day must be an ISO UTC date')
    identity=sha(canonical({'day':day,'target':target,'lock':lock,'driver':source_sha(),'reference':reference_sha(),
                            'memory_mib':memory_mib}).encode())
    return {'template':'accuracy_v1','environment':'worker-python','name':'NPU microcheck '+day+' card '+str(device),
        'placement':target,'idempotency_key':'device-microcheck-'+identity,
        'parameters':{'input_id':input_id,'argv':['python','-I','-B',DRIVER,'--run'],
            'input_mode':'readonly','lock':lock,'resources':{'cpu':1,'memory_mib':memory_mib,'shm_mib':0,'npu':1},
            'timeout_seconds':180,'artifacts':{'file_mib':1,'total_mib':2}}}


def submit(client,lock,node,devices,day,memory_mib=2048):
    from placement import CAPABILITY
    from lab_client import ClientError
    if CAPABILITY not in client.request('/api/me').get('placement_capabilities',[]):raise ClientError('Controller needs exact Node/device placement support')
    if (not isinstance(devices,list) or not 1<=len(devices)<=256 or len(set(devices))!=len(devices)):
        raise ValueError('Choose distinct registered physical cards')
    # Validate every target before creating inputs or jobs.
    for device in devices:request_for('input-'+'0'*24,lock,node,device,day,memory_mib)
    raw=Path(__file__).read_bytes();files=[{'name':DRIVER,'size':len(raw),'sha256':sha(raw),'executable':False}]
    bundle=client.request('/api/inputs',{'files':files})['input'];identifier=bundle['id']
    if not bundle['sealed']:
        offset=bundle.get('uploaded',{}).get(DRIVER,0)
        while offset<len(raw):
            block=raw[offset:offset+256*1024]
            client.request('/api/inputs/'+identifier+'/blob',{'name':DRIVER,'offset':offset,'data':base64.b64encode(block).decode()});offset+=len(block)
        client.request('/api/inputs/'+identifier+'/seal',{})
    entries=[]
    for device in devices:
        request=request_for(identifier,lock,node,device,day,memory_mib)
        # Probe uses the same ordinary owner quota/queue. A failed/expired probe
        # never grants numerical qualification or bypasses the queued UT gate.
        probe={'template':'npu_access_probe','environment':'worker-python','parameters':{'node_id':node,'device_id':device},
               'idempotency_key':request['idempotency_key']+'-access'}
        access=client.request('/api/jobs',probe)
        task=client.request('/api/jobs',request)
        entries.append({'device_id':device,'access_probe':access['job']['id'],'job_id':task['job']['id'],'state':task['job']['state'],
                        'reused':task['reused'],'precision_qualified':False,'performance_qualified':False})
    return {'day_utc':day,'node_id':node,'entries':entries,'active_ut_submitted':True,'timer_installed':False,
            'note':'Submission is not completion. Missing/expired access waits; inspect original jobs, never infer a device pass.'}


def inspect_case(client,identifier):
    from lab_client import job_id,ClientError
    from compiler_diagnostic import fixed_artifact
    job=client.request('/api/jobs/'+job_id(identifier))['job']
    if (job.get('template')!='accuracy_v1' or job.get('execution_backend')!='managed-docker'
        or job.get('state') not in {'succeeded','failed','timed_out','cancelled'}):raise ClientError('No confirmed terminal managed microcheck')
    parameters=job.get('parameters',{});bundle=job.get('input',{});target=job.get('placement',{})
    if (parameters.get('argv')!=['python','-I','-B',DRIVER,'--run'] or parameters.get('input_mode')!='readonly'
        or job.get('resources',{}).get('npu')!=1 or target.get('node_id')!=job.get('node_id')
        or target.get('device_ids')!=job.get('allocation',{}).get('npu_ids') or bundle.get('id')!=parameters.get('input_id')
        or bundle.get('files')!=[{'name':DRIVER,'size':Path(__file__).stat().st_size,'sha256':source_sha(),'executable':False}]):
        raise ClientError('Microcheck does not match the fixed sealed recipe/target')
    detail=client.request('/api/jobs/'+identifier);indexed={row['name'] for row in detail['artifacts']}
    manifest=client.request('/api/jobs/'+identifier+'/archive-manifest')
    files={row['name']:row for row in manifest['files']}
    report_raw=fixed_artifact(client,identifier,REPORT,128*1024,indexed)
    try:
        if files[REPORT]['sha256']!=sha(report_raw) or files[REPORT]['size']!=len(report_raw):raise ValueError('Report differs from archive manifest')
        report=json.loads(report_raw);comparison=validate_report(report)
        raw=fixed_artifact(client,identifier,'results/_lab/environment.json',128*1024,indexed)
        if files['results/_lab/environment.json']['sha256']!=sha(raw):raise ValueError('Receipt differs from archive manifest')
        receipt=json.loads(raw);access=receipt.get('device_access',{})
        access_ok=(isinstance(access,dict) and access.get('schema')=='ascend-lab-device-access/v1'
            and access.get('status')=='passed' and access.get('passed') is True
            and all(access.get(key)==target['device_ids'] for key in ['requested_device_ids','opened_device_ids','physical_device_files'])
            and access.get('errors')==[] and type(access.get('uid')) is int and access['uid']>=1000 and access.get('euid')==access['uid']
            and access.get('cap_effective')==access.get('cap_permitted')=='0000000000000000' and access.get('no_new_privileges')=='1')
        environment_ok=(receipt.get('schema')=='ascend-lab-environment/v1' and receipt.get('locked') is True and receipt.get('workload_started') is True
            and receipt.get('probe_exit_code')==0 and receipt.get('observed',{}).get('image_sha256')==parameters['lock']['image_sha256']
            and access_ok)
    except (KeyError,ValueError,TypeError,RecursionError):raise ClientError('Invalid archived microcheck evidence') from None
    return {'job_id':identifier,'state':job['state'],'comparison':comparison,'report_sha256':sha(report_raw),
        'protected_environment_matched':environment_ok,'device_conditions':job.get('device_conditions'),
        'assertion_source':'Fixed task-produced observations, independently compared on CPU; not independent hardware attestation',
        'hardware_health_certified':False,'performance_qualified':False,'automatic_admission_policy_changed':False}


def main():
    if sys.argv[1:]==['--run']:return run()
    from lab_client import Client,private_profile
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--profile',required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    command=commands.add_parser('submit');command.add_argument('--lock',required=True);command.add_argument('--node',required=True)
    command.add_argument('--devices',required=True,type=int,nargs='+');command.add_argument('--day',default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    command.add_argument('--memory-mib',type=int,default=2048)
    command=commands.add_parser('inspect');command.add_argument('job_id')
    args=parser.parse_args();client=Client(private_profile(args.profile))
    try:
        value=(submit(client,json.loads(Path(args.lock).read_text()),args.node,args.devices,args.day,args.memory_mib)
               if args.action=='submit' else inspect_case(client,args.job_id))
    except (OSError,ValueError) as error:parser.error(str(error))
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':raise SystemExit(main())

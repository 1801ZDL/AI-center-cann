"""Finite stdlib tensor comparisons and an ordinary sealed CPU replay workload.

Comparison against caller-supplied reference bytes is not device certification.
This module never imports tensor frameworks, loads pickle, or accesses devices.
"""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct

MAX_ELEMENTS=1048576
MAX_BYTES=8*1024**2
DTYPES={'uint8':('B',1),'float32':('f',4),'float64':('d',8)}
INPUT='accuracy-input.json'
DRIVER='accuracy_data.py'
REPORT='results/accuracy-analysis.json'
PREFIX='results/accuracy-input/'


def sha(raw):return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def invalid(_):raise ValueError('Finite JSON required')
    def unique(pairs):
        out={}
        for key,value in pairs:
            if key in out:raise ValueError('Duplicate JSON key')
            out[key]=value
        return out
    return json.loads(raw,parse_constant=invalid,object_pairs_hook=unique)


def digest(value):return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None


def nonnegative(value):
    try:return type(value) in {int,float} and math.isfinite(value) and value>=0
    except OverflowError:return False


def config_schema(value):
    required={'schema','name','shape','actual','reference','policy'}
    if (not isinstance(value,dict) or set(value)-{'source_context'}!=required
        or value.get('schema')!='ascend-lab-accuracy-input/v1' or not isinstance(value.get('name'),str)
        or not 1<=len(value['name'])<=160 or any(ord(c)<32 for c in value['name'])
        or not isinstance(value.get('shape'),list) or not 1<=len(value['shape'])<=8
        or any(type(n) is not int or not 1<=n<=MAX_ELEMENTS for n in value['shape'])):
        raise ValueError('Invalid finite tensor comparison input')
    count=math.prod(value['shape'])
    if count>MAX_ELEMENTS:raise ValueError('Tensor element budget exceeded')
    for role in ['actual','reference',*(['source_context'] if 'source_context' in value else [])]:
        item=value[role]
        if (not isinstance(item,dict) or set(item)!={'file','dtype','sha256','origin'}
            or item.get('file')!=role+'.bin' or not isinstance(item.get('dtype'),str) or item['dtype'] not in DTYPES
            or count*DTYPES[item['dtype']][1]>MAX_BYTES or not digest(item.get('sha256'))):
            raise ValueError('Use fixed regular tensor files, explicit dtypes and full hashes')
        origin=item['origin']
        if (not isinstance(origin,dict) or set(origin)!={'id','kind','source_sha256','scope'}
            or not isinstance(origin.get('id'),str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',origin['id'])
            or origin.get('kind') not in {'historical_device_capture','cpu_reference','user_supplied'}
            or not digest(origin.get('source_sha256')) or not isinstance(origin.get('scope'),str)
            or not 1<=len(origin['scope'])<=2048):raise ValueError('Explicit bounded reference and capture provenance required')
    policy=value['policy']
    if not isinstance(policy,dict):raise ValueError('Explicit comparison policy required')
    if policy=={'mode':'exact_encoding'}:
        if any(value[k]['dtype']!='uint8' for k in ['actual','reference']):raise ValueError('Encoding comparison requires uint8 data')
    elif (set(policy)=={'mode','atol','rtol','equal_nan','signed_zero_equal'} and policy.get('mode')=='numeric'
        and all(nonnegative(policy[k]) for k in ['atol','rtol'])
        and all(type(policy[k]) is bool for k in ['equal_nan','signed_zero_equal'])
        and all(value[k]['dtype'] in {'float32','float64'} for k in ['actual','reference'])):pass
    else:raise ValueError('Unsupported comparison or tolerance policy')
    if 'source_context' in value and value['source_context']['dtype'] not in {'float32','float64'}:
        raise ValueError('Source context must be numeric')
    return value


def scalar(value):
    if isinstance(value,float) and not math.isfinite(value):return 'nan' if math.isnan(value) else '+inf' if value>0 else '-inf'
    return value


def coordinates(index,shape):
    result=[]
    for dim in reversed(shape):result.append(index%dim);index//=dim
    return list(reversed(result))


def compare(value,blobs):
    value=config_schema(value);count=math.prod(value['shape']);arrays={}
    for role in ['actual','reference',*(['source_context'] if 'source_context' in value else [])]:
        raw=blobs[role];fmt,width=DTYPES[value[role]['dtype']]
        if not isinstance(raw,bytes) or len(raw)!=count*width or sha(raw)!=value[role]['sha256']:
            raise ValueError('Tensor bytes differ from declared extent or hash')
        arrays[role]=struct.iter_unpack('<'+fmt,raw)
    policy=value['policy'];numeric=policy['mode']=='numeric';mismatches=0;examples=[];categories={}
    finite_pairs=0;max_abs=0.;max_rel=None;abs_overflow=False;rel_overflow=False;zero_reference=0
    for index,((actual,),(reference,)) in enumerate(zip(arrays['actual'],arrays['reference'])):
        context=next(arrays['source_context'])[0] if 'source_context' in arrays else None
        reason=None
        if not numeric:
            if actual!=reference:reason='encoding_mismatch'
        elif math.isnan(actual) or math.isnan(reference):
            if not (policy['equal_nan'] and math.isnan(actual) and math.isnan(reference)):reason='nan_mismatch'
        elif math.isinf(actual) or math.isinf(reference):
            if actual!=reference:reason='infinity_mismatch'
        else:
            finite_pairs+=1;error=abs(actual-reference);threshold=policy['atol']+policy['rtol']*abs(reference)
            exact_error=None
            if not math.isfinite(error) or not math.isfinite(threshold):
                # Overflow must not make inf<=inf pass accidentally. Fractions
                # are used only for these exceptional finite arithmetic cases.
                from fractions import Fraction
                exact_error=abs(Fraction(actual)-Fraction(reference))
                passed=exact_error<=Fraction(policy['atol'])+Fraction(policy['rtol'])*abs(Fraction(reference))
            else:passed=error<=threshold
            if actual==reference==0 and not policy['signed_zero_equal'] and math.copysign(1.,actual)!=math.copysign(1.,reference):
                reason='signed_zero_mismatch'
            elif not passed:reason='tolerance_mismatch'
            if math.isfinite(error):max_abs=max(max_abs,error)
            else:abs_overflow=True
            if reference:
                try:relative=float(exact_error/abs(Fraction(reference))) if exact_error is not None else error/abs(reference)
                except OverflowError:relative=float('inf')
                if math.isfinite(relative):max_rel=relative if max_rel is None else max(max_rel,relative)
                else:rel_overflow=True
            elif actual!=0:zero_reference+=1
        if reason:
            mismatches+=1;categories[reason]=categories.get(reason,0)+1
            if len(examples)<16:
                example={'index':index,'coordinates':coordinates(index,value['shape']),'actual':scalar(actual),'reference':scalar(reference),'reason':reason}
                if 'source_context' in arrays:example['source_context']=scalar(context)
                examples.append(example)
    return {'mode':policy['mode'],'elements':count,'mismatches':mismatches,'passed':mismatches==0,
        'categories':categories,'examples':examples,'examples_truncated':mismatches>len(examples),
        'numeric_metrics':{'finite_pairs':finite_pairs,'max_abs_error':None if abs_overflow or not finite_pairs else max_abs,
            'max_relative_error':None if rel_overflow else max_rel,'absolute_error_overflow':abs_overflow,
            'relative_error_overflow':rel_overflow,'nonzero_actual_zero_reference':zero_reference} if numeric else None,
        'policy':policy,'scope':'Comparison of these supplied bytes against this explicit reference only. No independent reference truth, NPU fault, repair or new device execution is certified.'}


def csv_report(value,result):
    out=io.StringIO(newline='');writer=csv.writer(out)
    def safe(x):return "'"+x if isinstance(x,str) and x.lstrip().startswith(('=','+','-','@')) else x
    writer.writerow(['record','name','reference_id','mode','passed','elements','mismatches','index','actual','reference','reason'])
    writer.writerow([safe(x) for x in ['summary',value['name'],value['reference']['origin']['id'],result['mode'],result['passed'],result['elements'],result['mismatches'],'','','','']])
    for row in result['examples']:
        writer.writerow([safe(x) for x in ['example',value['name'],value['reference']['origin']['id'],result['mode'],False,'','',row['index'],row['actual'],row['reference'],row['reason']]])
    return out.getvalue().encode('utf-8')


def workload():
    import os
    root=Path(os.environ['LAB_JOB_DIR']);config_raw=Path(INPUT).read_bytes()
    if not 0<len(config_raw)<=16384:raise ValueError('Comparison manifest exceeds its budget')
    value=config_schema(strict_json(config_raw));blobs={}
    for role in ['actual','reference',*(['source_context'] if 'source_context' in value else [])]:
        path=Path(value[role]['file'])
        if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('Regular bounded tensor file required')
        blobs[role]=path.read_bytes()
    result=compare(value,blobs)
    archived=root/PREFIX;archived.mkdir(mode=0o700)
    (archived/INPUT).write_bytes(config_raw)
    for role,raw in blobs.items():(archived/value[role]['file']).write_bytes(raw)
    report={'schema':'ascend-lab-accuracy-report/v1','purpose':'offline_accuracy_replay','driver_sha256':sha(Path(__file__).read_bytes()),
        'input_sha256':sha(config_raw),'comparison':result,'input':value,
        'origin_trust':'Caller-supplied provenance; sealed hashes establish byte identity, not historical device authenticity.',
        'new_npu_launches':0}
    (root/REPORT).write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
    (root/'results/accuracy-analysis.csv').write_bytes(csv_report(value,result))
    workload_result={'schema':'ascend-lab-result/v1','kind':'build','passed':result['passed'],
        'artifacts':[REPORT,'results/accuracy-analysis.csv'],'purpose':'offline_accuracy_replay',
        'comparison_passed':result['passed'],'mismatches':result['mismatches'],'new_npu_launches':0}
    Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps(workload_result,allow_nan=False)+'\n')
    print(json.dumps({'purpose':'offline_accuracy_replay','comparison_passed':result['passed'],'elements':result['elements'],'mismatches':result['mismatches'],'new_npu_launches':0}),flush=True)
    raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':workload()

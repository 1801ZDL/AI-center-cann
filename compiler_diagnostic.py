"""One-job compiler evidence adapter; candidates never execute through this tool."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

from diagnostic_case import build_case,redact,serve
from lab_client import Client,ClientError,job_id
from operator_profile import redacted_data

REPORT='results/compiler-case.json'
SOURCE='ir/diagnostic-input.mlir'
STDERR='results/compiler-stderr.txt'
DRIVER='compiler_compatibility.py'
PROTECTED='results/_lab/environment.json'
MAX_CASE=192*1024
SOURCE_LIMITS={'ascend-lab-compiler-report/v1':8192,'ascend-lab-compiler-report/v2':8*1024**2}
CHUNK=256*1024


def sha(raw):return hashlib.sha256(raw).hexdigest()


def report_schema(value):
    if (not isinstance(value,dict) or value.get('schema') not in SOURCE_LIMITS
        or value.get('purpose')!='mlir_parse_and_verify' or not isinstance(value.get('source'),dict)
        or not isinstance(value.get('tool'),dict) or not isinstance(value.get('outcome'),dict)):
        raise ValueError('Unsupported compiler evidence schema')
    source=value['source'];tool=value['tool'];outcome=value['outcome']
    if (not isinstance(value.get('driver_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',value['driver_sha256'])
        or source.get('artifact')!=SOURCE or type(source.get('bytes')) is not int or not 0<source['bytes']<=SOURCE_LIMITS[value['schema']]
        or not isinstance(source.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',source['sha256'])
        or tool.get('name') not in {'triton-mlir-opt','bishengir-opt'} or not isinstance(tool.get('version'),str)
        or len(tool['version'])>4096 or not isinstance(tool.get('path'),str) or len(tool['path'])>500
        or not isinstance(tool.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',tool['sha256'])
        or type(outcome.get('exit_code')) is not int or not -255<=outcome['exit_code']<=255
        or type(outcome.get('timed_out')) is not bool or type(outcome.get('stderr_bytes')) is not int
        or outcome['stderr_bytes']<0 or type(outcome.get('stderr_truncated')) is not bool
        or outcome['stderr_truncated']!=(outcome['stderr_bytes']>8192)):
        raise ValueError('Invalid compiler source, tool or outcome')
    return value


def fixed_artifact(client,identifier,name,limit,indexed):
    if name not in indexed:raise ClientError('Required compiler evidence is absent')
    blocks=[];offset=0;size=None
    while size is None or offset<size:
        length=min(CHUNK,limit-offset) if size is None else min(CHUNK,size-offset)
        value=client.request('/api/jobs/'+identifier+'/artifact-chunk?'+urlencode({'name':name,'offset':offset,'length':max(1,length)}))
        try:
            if not isinstance(value['data'],str) or len(value['data'])>4*((length+2)//3):raise ValueError()
            raw=base64.b64decode(value['data'],validate=True)
            if (value['name']!=name or value['offset']!=offset or type(value['size']) is not int or not 0<=value['size']<=limit
                or size is not None and value['size']!=size
                or len(raw)!=min(length,value['size']-offset) or sha(raw)!=value['sha256']):raise ValueError()
        except (ValueError,KeyError,TypeError):raise ClientError('Incomplete or invalid compiler artifact') from None
        size=value['size'];blocks.append(raw);offset+=len(raw)
    return b''.join(blocks)


def source_preview(text,error):
    """Model-facing text is bounded even when the entire sealed IR was checked."""
    lines=text.splitlines();contexts=[];seen=set()
    for match in re.finditer(r'diagnostic-input\.mlir:(\d+):(?:\d+:)?',error):
        line=int(match[1]) if len(match[1])<=8 else 0
        if not 1<=line<=len(lines) or line in seen:continue
        seen.add(line)
        contexts.append({'line':line,'lines':[{'number':i+1,'text':lines[i][:1024],
            'truncated':len(lines[i])>1024} for i in range(max(0,line-2),min(len(lines),line+1))]})
        if len(contexts)==4:break
    return {'prefix':text[:4096],'suffix':text[-4096:] if len(text)>4096 else '',
        'total_lines':len(lines),'diagnostic_context':contexts,
        'scope':'Excerpts only; do not apply exact patches to this preview. Download the original through the authenticated archive interface.'}


def workflow_binding(job,report,receipt):
    parameters=job.get('parameters',{});bundle=job.get('input',{})
    if not isinstance(parameters,dict) or not isinstance(bundle,dict) or not isinstance(receipt,dict):raise ValueError('Invalid compiler workflow binding')
    files={v.get('name'):v.get('sha256') for v in bundle.get('files',[]) if isinstance(v,dict)}
    if (job.get('template')!='ir_v1' or job.get('environment')!='worker-python'
        or job.get('execution_backend')!='managed-docker' or bundle.get('id')!=parameters.get('input_id')
        or parameters.get('argv')!=['python','-I','-B',DRIVER]
        or files.get('input.mlir')!=report['source']['sha256'] or files.get(DRIVER)!=report['driver_sha256']):
        raise ValueError('Compiler evidence differs from its sealed managed workflow')
    path=Path(__file__).resolve().parent/DRIVER
    if not path.is_file():path=Path(__file__).resolve().parent/'examples/workflows'/DRIVER
    known=path.is_file() and sha(path.read_bytes())==report['driver_sha256']
    wanted=parameters.get('lock',{}).get('files',{}).get(report['tool']['path'])
    observed=receipt.get('observed',{})
    if not isinstance(observed,dict) or not isinstance(observed.get('files',{}),dict):raise ValueError('Invalid protected environment facts')
    fact=observed.get('files',{}).get(report['tool']['path']) or {}
    if not isinstance(fact,dict):raise ValueError('Invalid protected compiler file fact')
    protected=fact.get('sha256')
    matched=(receipt.get('schema')=='ascend-lab-environment/v1' and receipt.get('locked') is True
        and receipt.get('workload_started') is True and receipt.get('probe_exit_code')==0
        and protected==wanted==report['tool']['sha256'])
    return {'input_id':bundle['id'],'source_bound_to_seal':True,'driver_bound_to_seal':True,
        'driver_known_to_client':known,'driver_sha256':report['driver_sha256'],
        'protected_tool_lock_matched':matched,'locked_tool_sha256':wanted,'protected_tool_sha256':protected}


def read_case(client,identifier):
    identifier=job_id(identifier)
    # Generic evidence collector authorizes before any domain artifact request.
    generic=build_case(client,identifier);detail=client.request('/api/jobs/'+identifier)
    if detail['job']['id']!=identifier:raise ClientError('Compiler case identity differs')
    indexed={v.get('name') for v in detail.get('artifacts',[]) if isinstance(v,dict)}
    try:
        report_raw=fixed_artifact(client,identifier,REPORT,16384,indexed)
        report=report_schema(json.loads(report_raw))
        source=fixed_artifact(client,identifier,SOURCE,SOURCE_LIMITS[report['schema']],indexed)
        error_archived=STDERR in indexed
        # The trusted exporter omits zero-byte files. Do not fabricate an
        # archive checksum for an absent entry; keep the workload claim explicit.
        error=(b'' if not error_archived and report['outcome']['stderr_bytes']==0
               else fixed_artifact(client,identifier,STDERR,8192,indexed))
        if len(source)!=report['source']['bytes'] or sha(source)!=report['source']['sha256']:raise ValueError('Source differs from compiler record')
        if len(error)!=min(report['outcome']['stderr_bytes'],8192):raise ValueError('Compiler stderr differs from report extent')
        receipt=json.loads(fixed_artifact(client,identifier,PROTECTED,65536,indexed)) if PROTECTED in indexed else {}
        binding=workflow_binding(detail['job'],report,receipt)
        text=source.decode('utf-8');clean_source=redact(text,client.config)
        clean_report=redacted_data(report,client.config)
        large=len(source)>8192
    except (ValueError,UnicodeError,KeyError,TypeError,RecursionError):raise ClientError('Invalid compiler evidence') from None
    case={**generic,'schema':'ascend-lab-compiler-diagnostic/v2' if large else 'ascend-lab-compiler-diagnostic/v1','base_case_sha256':generic['case_sha256'],
        'compiler_report':clean_report,'compiler_report_sha256':sha(report_raw),'source_ir':None if large else clean_source,
        'source_complete':not large,'source_verified_bytes':len(source),
        'workflow_binding':binding,
        'source_ir_sha256':sha(source),'source_redacted':clean_source!=text,
        'compiler_stderr':redact(error.decode('utf-8',errors='replace'),client.config),
        'compiler_stderr_sha256':sha(error) if error_archived else None,
        'compiler_stderr_archived':error_archived,
        'compiler_stderr_source':'archived bytes' if error_archived else 'workload report declares zero bytes; empty artifact omitted',
        'candidate_policy':'Read-only evidence. No patch or task is executed. A reviewed candidate requires a new sealed input and explicit central CPU job; unknown execution keeps original resources. Redacted/incomplete sources cannot support an exact patch.',
        'scope':'Bounded actual compiler report and full small IR, plus generic archived evidence. Workload records are untrusted claims; parse acceptance does not prove numerical equivalence or device correctness.'}
    if large:
        case['source_preview']=source_preview(clean_source,case['compiler_stderr'])
        case['scope']='Complete sealed IR bytes and hash verified, with bounded redacted excerpts only. No exact patch from this preview, full lowering, numerical equivalence or device correctness certification.'
    case.pop('case_sha256')
    case=redacted_data(case,client.config)
    if len(json.dumps(case,ensure_ascii=False,allow_nan=False).encode())>MAX_CASE:raise ClientError('Compiler case exceeds response budget')
    case['case_sha256']=sha(json.dumps(case,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    return case


class CompilerBridge:
    def __init__(self,client,identifier):self.client=client;self.identifier=job_id(identifier)
    def call(self,name,arguments):
        if name!='get_diagnostic_case' or not isinstance(arguments,dict) or arguments:raise ClientError('Only the bound compiler evidence getter is available')
        return read_case(self.client,self.identifier)


def review_minimal_rename(case,proposal,approved_edit):
    """Check one primary-approved literal experiment; return text, never execute.

    This is an example-specific candidate contract, not general patch approval.
    The caller must independently establish the migration and tool context.
    """
    required={'schema','category','summary','target_tool','expected_source_sha256','replace',
        'npu_fault_proven','numeric_correctness_proven','full_pipeline_proven','primary_review_required'}
    if (not isinstance(proposal,dict) or set(proposal)!=required
        or proposal['schema']!='ascend-lab-compiler-proposal/v1' or proposal['category']!='ir_dialect_version_mismatch'
        or not isinstance(proposal['summary'],str) or not proposal['summary'] or len(proposal['summary'])>3000
        or any(proposal[k] is not False for k in ['npu_fault_proven','numeric_correctness_proven','full_pipeline_proven'])
        or proposal['primary_review_required'] is not True):raise ClientError('Unsupported compiler proposal')
    if (case.get('schema')!='ascend-lab-compiler-diagnostic/v1' or case.get('source_redacted') is not False
        or case.get('terminal') is not True or case.get('job',{}).get('state')!='failed'):
        raise ClientError('Only a complete unredacted source from a known ended failure can support this experiment')
    outcome=case.get('compiler_report',{}).get('outcome',{})
    code=outcome.get('exit_code')
    if (type(code) is not int or not 1<=code<=255 or outcome.get('timed_out') is not False
        or outcome.get('stderr_truncated') is not False or case.get('compiler_stderr_archived') is False):
        raise ClientError('A complete ordinary compiler failure is required; timeout/truncated evidence does not establish this mismatch')
    binding=case.get('workflow_binding',{})
    if any(binding.get(k) is not True for k in ['source_bound_to_seal','driver_bound_to_seal','driver_known_to_client','protected_tool_lock_matched']):
        raise ClientError('Candidate requires a known sealed driver and a protected matching tool lock')
    try:execution=json.loads(next(v['text'] for v in case['evidence'] if v['id']=='execution_result'))
    except (ValueError,KeyError,TypeError,StopIteration):raise ClientError('Missing authoritative execution state') from None
    if any(execution.get(k) is not True for k in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed']):
        raise ClientError('Original attempt cleanup is unconfirmed; retain its resources')
    source=case.get('source_ir');expected=case.get('source_ir_sha256');edit=proposal['replace']
    if (not isinstance(source,str) or sha(source.encode())!=expected or proposal['expected_source_sha256']!=expected
        or proposal['target_tool']!=case['compiler_report']['tool']['name']
        or not isinstance(edit,dict) or set(edit)!={'old','new','count'} or type(edit['count']) is not int or edit['count']!=1
        or not isinstance(approved_edit,dict) or set(approved_edit)!={'old','new'}
        or any(not isinstance(approved_edit[k],str) or not approved_edit[k] for k in ['old','new'])
        or edit['old']!=approved_edit['old'] or edit['new']!=approved_edit['new'] or source.count(edit['old'])!=1):
        raise ClientError('Proposal differs from the primary-approved source, target or single replacement')
    output=source.replace(edit['old'],edit['new'],1)
    return {'candidate_ir':output,'original_source_sha256':expected,'candidate_source_sha256':sha(output.encode()),
        'submission_performed':False,'experiment_still_required':True,'full_pipeline_proven':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['report','mcp']);parser.add_argument('--profile',required=True);parser.add_argument('--job-id',required=True);args=parser.parse_args()
    client=Client.from_profile(args.profile)
    if args.action=='report':print(json.dumps(read_case(client,args.job_id),ensure_ascii=False,indent=2))
    else:serve(CompilerBridge(client,args.job_id),sys.stdin,sys.stdout)


if __name__=='__main__':main()

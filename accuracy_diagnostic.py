"""One authorized archived comparison; no model, submission, patch or device IO."""
import argparse
import json
from pathlib import Path
import sys

from accuracy_data import INPUT,DRIVER,REPORT,PREFIX,MAX_BYTES,compare,config_schema,sha,strict_json
from compiler_diagnostic import fixed_artifact
from diagnostic_case import build_case,serve
from lab_client import Client,ClientError,job_id
from operator_profile import redacted_data


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def binding(job,report,receipt):
    parameters=job.get('parameters',{});bundle=job.get('input',{})
    if not isinstance(parameters,dict) or not isinstance(bundle,dict):raise ValueError('Missing sealed comparison workflow')
    files={v.get('name'):v for v in bundle.get('files',[]) if isinstance(v,dict)}
    expected={INPUT:report['input_sha256'],DRIVER:report['driver_sha256'],
        **{v['file']:v['sha256'] for k,v in report['input'].items() if k in {'actual','reference','source_context'}}}
    if (job.get('template')!='build_v1' or job.get('environment')!='worker-python' or job.get('execution_backend')!='managed-docker'
        or job.get('resources',{}).get('npu')!=0 or bundle.get('id')!=parameters.get('input_id')
        or parameters.get('argv')!=['python','-I','-B',DRIVER]
        or any(files.get(name,{}).get('sha256')!=wanted for name,wanted in expected.items())):
        raise ValueError('Comparison archive differs from its sealed CPU workflow')
    if not isinstance(receipt,dict):raise ValueError('Invalid comparison environment receipt')
    observed=receipt.get('observed',{})
    if not isinstance(observed,dict):raise ValueError('Invalid protected environment facts')
    protected=(receipt.get('schema')=='ascend-lab-environment/v1' and receipt.get('locked') is True
        and receipt.get('workload_started') is True and receipt.get('probe_exit_code')==0
        and observed.get('image_sha256')==parameters.get('lock',{}).get('image_sha256'))
    path=Path(__file__).resolve().parent/DRIVER
    return {'input_id':bundle['id'],'source_bound_to_seal':True,'driver_bound_to_seal':True,
        'driver_known_to_client':path.is_file() and sha(path.read_bytes())==report['driver_sha256'],
        'protected_environment_matched':protected,'device_capture_independently_certified':False,
        'scope':'Protected environment is for this CPU replay, not the historical device capture.'}


def read_case(client,identifier):
    identifier=job_id(identifier);generic=build_case(client,identifier);detail=client.request('/api/jobs/'+identifier)
    if detail['job']['id']!=identifier:raise ClientError('Accuracy case identity differs')
    indexed={v.get('name') for v in detail.get('artifacts',[]) if isinstance(v,dict)}
    try:
        raw=fixed_artifact(client,identifier,REPORT,32768,indexed);report=strict_json(raw)
        if (not isinstance(report,dict) or report.get('schema')!='ascend-lab-accuracy-report/v1'
            or report.get('purpose')!='offline_accuracy_replay' or type(report.get('new_npu_launches')) is not int or report['new_npu_launches']!=0):
            raise ValueError('Unsupported accuracy evidence schema')
        value=config_schema(report['input'])
        config_raw=fixed_artifact(client,identifier,PREFIX+INPUT,16384,indexed)
        if canonical(strict_json(config_raw))!=canonical(value) or sha(config_raw)!=report['input_sha256']:raise ValueError('Archived comparison manifest differs')
        blobs={role:fixed_artifact(client,identifier,PREFIX+value[role]['file'],MAX_BYTES,indexed)
            for role in ['actual','reference',*(['source_context'] if 'source_context' in value else [])]}
        comparison=compare(value,blobs)
        if canonical(report.get('comparison'))!=canonical(comparison):raise ValueError('Archived comparison differs from independent byte recomputation')
        receipt=strict_json(fixed_artifact(client,identifier,'results/_lab/environment.json',65536,indexed))
        workflow=binding(detail['job'],report,receipt)
    except (OSError,ValueError,KeyError,TypeError,RecursionError):raise ClientError('Invalid or incomplete accuracy evidence') from None
    case={**generic,'schema':'ascend-lab-accuracy-diagnostic/v1','base_case_sha256':generic['case_sha256'],
        'accuracy_report_sha256':sha(raw),'comparison':comparison,'tensor_shape':value['shape'],
        'tensor_sources':{k:v for k,v in value.items() if k in {'actual','reference','source_context'}},
        'workflow_binding':workflow,'all_tensor_bytes_verified':True,'tensor_payload_delivered_to_model':False,
        'origin_trust':'Caller-supplied historical/reference provenance; hashes and CPU recomputation do not certify a device capture or correctness of the reference.',
        'repair_policy':'Read-only evidence. Changing tolerance/reference is a new experiment, not a verified operator fix. Preserve original failed comparisons. Do not release or resubmit unknown execution.',
        'scope':'Offline comparison against explicitly supplied reference bytes; no new device launch, hardware fault, root cause or repair is established.'}
    case.pop('case_sha256');case=redacted_data(case,client.config)
    if len(json.dumps(case,ensure_ascii=False,allow_nan=False).encode())>192*1024:raise ClientError('Accuracy case exceeds response budget')
    case['case_sha256']=sha(json.dumps(case,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode())
    return case


class AccuracyBridge:
    def __init__(self,client,identifier):self.client=client;self.identifier=job_id(identifier)
    def call(self,name,arguments):
        if name!='get_diagnostic_case' or not isinstance(arguments,dict) or arguments:
            raise ClientError('Only the bound read-only accuracy getter is available')
        return read_case(self.client,self.identifier)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['report','mcp']);p.add_argument('--profile',required=True);p.add_argument('--job-id',required=True);args=p.parse_args()
    client=Client.from_profile(args.profile)
    if args.action=='report':print(json.dumps(read_case(client,args.job_id),ensure_ascii=False,indent=2,allow_nan=False))
    else:serve(AccuracyBridge(client,args.job_id),sys.stdin,sys.stdout)


if __name__=='__main__':main()

"""One authorized simulator report; complete bytes verified, bounded summary only."""
import argparse
import json
from pathlib import Path
import sys

from compiler_diagnostic import fixed_artifact
from diagnostic_case import build_case,serve
from lab_client import Client,ClientError,job_id
from operator_profile import redacted_data
from simulation_data import INPUT,DRIVER,REPORT,PREFIX,MAX_REPORT,analyze,config_schema,sha,strict_json


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def binding(job,report,receipt):
    p=job.get('parameters',{});bundle=job.get('input',{})
    if not isinstance(p,dict) or not isinstance(bundle,dict):raise ValueError('Missing sealed simulation evidence workflow')
    value=report['input'];sources=[value['log'],*[core[role] for core in value['cores'] for role in ['trace','instructions']]]
    wanted={INPUT:report['input_sha256'],DRIVER:report['driver_sha256'],**{v['file']:v['sha256'] for v in sources}}
    files={v.get('name'):v.get('sha256') for v in bundle.get('files',[]) if isinstance(v,dict)}
    if (job.get('template')!='build_v1' or job.get('environment')!='worker-python' or job.get('execution_backend')!='managed-docker'
        or job.get('resources',{}).get('npu')!=0 or bundle.get('id')!=p.get('input_id')
        or p.get('argv')!=['python','-I','-B',DRIVER] or any(files.get(k)!=v for k,v in wanted.items())):
        raise ValueError('Simulation evidence differs from its sealed CPU workflow')
    if not isinstance(receipt,dict) or not isinstance(receipt.get('observed'),dict):raise ValueError('Invalid protected simulation environment receipt')
    protected=(receipt.get('schema')=='ascend-lab-environment/v1' and receipt.get('locked') is True
        and receipt.get('workload_started') is True and receipt.get('probe_exit_code')==0
        and receipt['observed'].get('image_sha256')==p.get('lock',{}).get('image_sha256'))
    path=Path(__file__).resolve().parent/DRIVER
    return {'input_id':bundle['id'],'source_bound_to_seal':True,'driver_bound_to_seal':True,
        'driver_known_to_client':path.is_file() and sha(path.read_bytes())==report['driver_sha256'],
        'protected_environment_matched':protected,'historical_capture_independently_certified':False}


def read_case(client,identifier):
    identifier=job_id(identifier);generic=build_case(client,identifier);detail=client.request('/api/jobs/'+identifier)
    if detail['job']['id']!=identifier:raise ClientError('Simulation case identity differs')
    indexed={v.get('name') for v in detail.get('artifacts',[]) if isinstance(v,dict)}
    try:
        raw=fixed_artifact(client,identifier,REPORT,MAX_REPORT,indexed);report=strict_json(raw)
        if (not isinstance(report,dict) or report.get('schema')!='ascend-lab-simulation-report/v1' or report.get('purpose')!='offline_simulation_analysis'
            or not isinstance(report.get('driver_sha256'),str)):
            raise ValueError('Unsupported simulation evidence report')
        value=config_schema(report['input']);config=fixed_artifact(client,identifier,PREFIX+INPUT,16384,indexed)
        if canonical(strict_json(config))!=canonical(value) or sha(config)!=report['input_sha256']:raise ValueError('Archived simulation input manifest differs')
        result=analyze(value,lambda name,limit:fixed_artifact(client,identifier,PREFIX+name,limit,indexed))
        if canonical(result)!=canonical(report.get('analysis')):raise ValueError('Simulation report differs from independent recomputation')
        receipt=strict_json(fixed_artifact(client,identifier,'results/_lab/environment.json',65536,indexed))
        workflow=binding(detail['job'],report,receipt)
    except (OSError,ValueError,KeyError,TypeError,RecursionError):raise ClientError('Invalid or incomplete simulator evidence') from None
    case={**generic,'schema':'ascend-lab-simulation-diagnostic/v1','base_case_sha256':generic['case_sha256'],
        'simulation_report_sha256':sha(raw),'input':value,'analysis':result,'workflow_binding':workflow,
        'all_source_bytes_verified':True,'raw_trace_delivered_to_model':False,
        'origin_trust':'Caller-supplied origin and ELF identity. Verified hashes and CPU recomputation do not certify the historical collector, device, complete trace or claimed exit status.',
        'repair_policy':'Read-only evidence. Reported source paths are data, never files to open or commands to execute. Additional collection or experiments require a separately approved central task; preserve unknown execution.',
        'scope':'Recorded per-core interval unions and instruction counts. Not an optimization speedup, root cause, complete simulation or device correctness acceptance.'}
    case.pop('case_sha256');case=redacted_data(case,client.config)
    if len(canonical(case).encode())>192*1024:raise ClientError('Simulator case exceeds response budget')
    case['case_sha256']=sha(canonical(case).encode());return case


class SimulationBridge:
    def __init__(self,client,identifier):self.client=client;self.identifier=job_id(identifier)
    def call(self,name,arguments):
        if name!='get_diagnostic_case' or not isinstance(arguments,dict) or arguments:raise ClientError('Only the bound read-only simulator getter is available')
        return read_case(self.client,self.identifier)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['report','mcp']);p.add_argument('--profile',required=True);p.add_argument('--job-id',required=True);args=p.parse_args()
    client=Client.from_profile(args.profile)
    if args.action=='report':print(json.dumps(read_case(client,args.job_id),ensure_ascii=False,indent=2,allow_nan=False))
    else:serve(SimulationBridge(client,args.job_id),sys.stdin,sys.stdout)


if __name__=='__main__':main()

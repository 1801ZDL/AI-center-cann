"""Read an actual CPU analysis archive through a separate stdlib CLI and MCP.

No dataset import, model call, experiment, identity creation or task submission.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--foreign-profile',required=True)
    p.add_argument('--job-id',required=True);p.add_argument('--output-dir',required=True);args=p.parse_args()
    output=Path(args.output_dir).resolve();output.mkdir(mode=0o700,parents=True,exist_ok=True)
    digest=hashlib.sha256((ROOT/'operator_profile.py').read_bytes()).hexdigest()
    bundle_digest=hashlib.sha256(b''.join((ROOT/name).read_bytes() for name in ['operator_profile.py','lab_client.py','labctl','install-client'])).hexdigest()
    installed=output/('client-'+bundle_digest[:12])
    if not installed.exists():subprocess.run([sys.executable,str(ROOT/'install-client'),'--prefix',str(installed)],check=True,capture_output=True)
    assert not (installed/'server.py').exists() and not (installed/'runtime').exists()
    metadata=json.loads((installed/'manifest.json').read_text());assert 'operator-case-v1' in metadata['capabilities']
    assert metadata['sha256']['operator_profile.py']==digest
    aliases={'compiler_compatibility.py':'examples/workflows/compiler_compatibility.py'}
    assert all(hashlib.sha256((ROOT/aliases.get(name,name)).read_bytes()).hexdigest()==value for name,value in metadata['sha256'].items())
    client=Client.from_profile(args.profile);before=client.request('/api/jobs')['jobs']
    detail=client.request('/api/jobs/'+args.job_id);assert detail['job']['state']=='succeeded' and len(detail['attempts'])==1
    cli=subprocess.run([sys.executable,str(installed/'labctl'),'--profile',args.profile,'operator-case',args.job_id],text=True,capture_output=True,timeout=30)
    assert cli.returncode==0,cli.stderr;case=json.loads(cli.stdout);assert case['job']['id']==args.job_id
    atomic_json(output/'operator-case.json',case)
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'submit_job','arguments':{}}},
        {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'job_id':'job-'+'f'*12}}}]
    def mcp(profile):
        result=subprocess.run([sys.executable,str(installed/'operator_profile.py'),'mcp','--profile',profile,'--job-id',args.job_id],
            input=''.join(json.dumps(item)+'\n' for item in messages),text=True,capture_output=True,timeout=30)
        assert result.returncode==0,result.stderr
        return [json.loads(line)['result'] for line in result.stdout.splitlines()]
    own=mcp(args.profile);foreign=mcp(args.foreign_profile)
    assert len(own)==5 and len(own[1]['tools'])==1 and own[1]['tools'][0]['annotations']['readOnlyHint']
    assert not own[2]['isError'] and own[3]['isError'] and own[4]['isError'] and foreign[2]['isError']
    other_case=json.loads(own[2]['content'][0]['text']);assert other_case['artifact_sha256']==case['artifact_sha256']
    assert other_case['report']['comparison']==case['report']['comparison']
    assert {item['id'] for item in before}=={item['id'] for item in client.request('/api/jobs')['jobs']}
    record={'passed':True,'at':now(),'job_id':args.job_id,'case_sha256':case['case_sha256'],
        'installed_operator_module_sha256':digest,'cli_and_actual_stdio_mcp':True,'bound_read_only_tools':1,
        'foreign_identity_rejected':True,'submission_and_job_override_rejected':True,'no_added_jobs':True,
        'producer_attempts':1,'analysis_provider_imported_by_reader':False,'model_invocations':0,'npu_kernel_launches':0}
    atomic_json(output/'client-validation.json',record);print(json.dumps(record,ensure_ascii=False))


if __name__=='__main__':main()

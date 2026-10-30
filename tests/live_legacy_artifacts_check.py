"""Two-phase CLI/API acceptance across the legacy artifact-contract upgrade.

Run before/after loading the new controller. Own Main only; no model or NPU.
Submission intents and handles survive observation interruption. Never reloads
services, removes archives or retries an ambiguous submission automatically.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,ClientError,read_json,atomic_json,now

TERMINAL={'succeeded','failed','cancelled','timed_out'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['before','after']);args=parser.parse_args()
    base=ROOT/'runtime/legacy-artifact-20261030';base.mkdir(mode=0o700,exist_ok=True)
    profile=ROOT/'runtime/config.json';client=Client.from_profile(profile)
    binary=base/'independent-client/labctl'
    if not binary.exists():
        subprocess.run([str(ROOT/'install-client'),'--prefix',str(binary.parent)],check=True,capture_output=True,text=True,timeout=30)
    assert hashlib.sha256(binary.read_bytes()).digest()==hashlib.sha256((ROOT/'labctl').read_bytes()).digest()

    def wait(identifier):
        until=time.monotonic()+120
        while time.monotonic()<until:
            try:detail=client.request('/api/jobs/'+identifier)
            except ClientError:time.sleep(1);continue
            if detail['job']['state'] in TERMINAL:return detail
            time.sleep(1)
        raise RuntimeError('Observe the original saved handle; no replacement task was submitted')

    def submit(label,size,cap):
        intent=base/(label+'-intent.json');handle=base/(label+'-handle.json')
        key='legacy-artifact-live-20261030-'+label
        code='from pathlib import Path;import os;p=Path(os.environ["LAB_JOB_DIR"])/"results/output.bin";p.write_bytes(b"x"*'+str(size)+');print("CPU artifact fixture finished")'
        request={'name':'Legacy artifact '+label,'environment':'host-python','kind':'command','cwd':str(ROOT),
            'argv':['/usr/bin/python3','-c',code],'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0,'timeout_seconds':30,
            'idempotency_key':key,'parameters':{'artifacts':cap}}
        if handle.exists():return wait(read_json(handle)['job']['id']),request
        if intent.exists():
            # Resolve an interrupted CLI response by the published key. Absence
            # from a bounded page is not evidence that submission never happened.
            matches=[j for j in client.request('/api/jobs')['jobs'] if j.get('idempotency_key')==key]
            if len(matches)!=1:raise RuntimeError('Ambiguous original submission; preserve intent and reconcile before any retry')
            atomic_json(handle,{'job':matches[0]});return wait(matches[0]['id']),request
        atomic_json(intent,request)
        command=[str(binary),'--profile',str(profile),'submit','--env','host-python','--name',request['name'],
            '--cwd',str(ROOT),'--cpu','1','--memory-mib','256','--shm-mib','0','--npu','0','--timeout','30','--key',key,
            '--artifact-file-mib',str(cap['file_mib']),'--artifact-total-mib',str(cap['total_mib']),'--',*request['argv']]
        result=subprocess.run(command,capture_output=True,text=True,timeout=40)
        try:response=json.loads(result.stdout);assert response['job']['id']
        except (ValueError,KeyError):raise RuntimeError('CLI submission response unconfirmed; preserve intent, do not resubmit') from None
        atomic_json(handle,response)
        return wait(response['job']['id']),request

    if args.phase=='before':
        detail,request=submit('old-ignored',1024,{'file_mib':8,'total_mib':16})
        assert detail['job']['state']=='succeeded' and len(detail['attempts'])==1
        assert detail['job']['artifact_limits']=={'file_mib':64,'total_mib':128},'This phase requires the older, still-loaded controller'
        atomic_json(base/'pre-upgrade-live.json',{'at':now(),'job_id':detail['job']['id'],'request':request,
            'saved_limits':detail['job']['artifact_limits'],'single_attempt':True})
        print(json.dumps({'pre_upgrade_ignored_declaration_reproduced':True,'job_id':detail['job']['id']}));return

    prior=read_json(base/'pre-upgrade-live.json');assert prior
    try:client.request('/api/jobs',prior['request'])
    except ClientError as error:assert str(error)=='Controller HTTP 409'
    else:raise AssertionError('Do not reinterpret an old saved default budget as a newly honored declaration')
    old=client.request('/api/jobs/'+prior['job_id']);assert len(old['attempts'])==1 and old['job']['artifact_limits']==prior['saved_limits']
    default_request=read_json(ROOT/'runtime/large-mixed-20261030/unit-440-request.json')
    default_handle=read_json(ROOT/'runtime/large-mixed-20261030/unit-440-handle.json')
    replay=client.request('/api/jobs',default_request);assert replay['job']['id']==default_handle['job']['id']
    proofs=[]
    for label,size,state in [('within-limit',512*1024,'succeeded'),('over-limit',2*1024**2,'failed')]:
        detail,_=submit(label,size,{'file_mib':1,'total_mib':2});job=detail['job']
        assert job['state']==state and len(detail['attempts'])==1 and job['artifact_limits']=={'file_mib':1,'total_mib':2}
        if state=='failed':assert job['result']['failure_stage']=='artifact_budget' and job['result']['execution_status']=='succeeded'
        destination=base/'personal'
        result=subprocess.run([str(binary),'--profile',str(profile),'archive',job['id'],'--archive-root',str(destination)],
            check=True,capture_output=True,text=True,timeout=60)
        downloaded=json.loads(result.stdout);assert downloaded['complete'];folder=Path(downloaded['directory'])
        assert (folder/'stdout.log').is_file() and (folder/'result.json').is_file()
        if state=='succeeded':assert hashlib.sha256((folder/'results/output.bin').read_bytes()).hexdigest()==hashlib.sha256(b'x'*size).hexdigest()
        else:
            assert not (folder/'results/output.bin').exists()
            assert (ROOT/'runtime/jobs'/job['id']/'results/output.bin').stat().st_size==size
        proofs.append({'job_id':job['id'],'state':state,'single_attempt':True,'download_complete':True,'artifact_limits':job['artifact_limits']})
    for invalid in [None,[],{'unexpected':1}]:
        request={**prior['request'],'idempotency_key':'legacy-invalid-schema','parameters':invalid}
        try:client.request('/api/jobs',request)
        except ClientError as error:assert str(error)=='Controller HTTP 400'
        else:raise AssertionError('Invalid legacy parameters were accepted')
    result={'passed':True,'at':now(),'independently_installed_cli':True,'cases':proofs,
        'pre_upgrade_ignored_budget_not_reinterpreted':True,'pre_upgrade_default_retry_same_job':True,
        'invalid_parameter_schema_http400':True,'failed_logs_downloadable':True,'oversized_original_retained':True,
        'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Real own-user cooperative host CLI/API/archive acceptance; not root quota or NPU/multi-host acceptance'}
    atomic_json(base/'live-validation.json',result);print(json.dumps(result))


if __name__=='__main__':main()

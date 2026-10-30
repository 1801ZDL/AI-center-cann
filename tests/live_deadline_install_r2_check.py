"""Manage only our idle Demo's temporary acceptance budget and saved job handle."""
import argparse
import json
import re
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,read_json,atomic_json


def command(name,*args):
    return subprocess.run([str(ROOT/name),*map(str,args)],check=True,capture_output=True,text=True,timeout=25).stdout


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['prepare','submit','restore']);parser.add_argument('--attempt',type=int,default=1);args=parser.parse_args()
    assert 1<=args.attempt<=26
    base=ROOT/'runtime/deadline-install-r2-20261030';base.mkdir(mode=0o700,exist_ok=True);state_path=base/'control.json'
    client=Client.from_profile(ROOT/'runtime/config.json')
    def idle():
        value=client.request('/api/jobs');assert all(value['counts'][s]==0 for s in ['queued','reserved','running','cancelling','needs_attention'])
        assert not any(d['admission']['blocked'] for d in value['resource_domains']);return value
    def domain_set(domain,memory):
        p=domain['policy']
        command('workerctl','domain-set',domain['physical_id'],'--cpus',','.join(map(str,p['cpu_ids'])),
            '--memory-mib',memory,'--shm-mib',p['shm_mib'],'--max-running',p['max_running'],'--expected-revision',domain['revision'])
    if args.phase=='prepare':
        assert not state_path.exists();summary=idle();domain=summary['resource_domains'][0]
        assert len(summary['resource_domains'])==1 and domain['policy']['memory_mib']==2048
        available=int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024
        assert available>8*1024**3
        original=read_json(ROOT/'runtime/config.json');atomic_json(base/'original-main-config.json',original)
        prepared={**original,'scheduler':{**original['scheduler'],'memory_mib':4096}}
        atomic_json(base/'prepared-main-config.json',prepared);atomic_json(state_path,{'baseline_domain':domain,'prepared':False,'restored':False})
        command('labctl','stop');atomic_json(ROOT/'runtime/config.json',prepared);command('labctl','start')
        domain_set(domain,4096)
        atomic_json(state_path,{'baseline_domain':domain,'prepared':True,'restored':False})
        print(json.dumps({'prepared':True,'temporary_demo_memory_mib':4096,'original_demo_memory_mib':2048,'host_global_settings_modified':False}))
    elif args.phase=='submit':
        state=read_json(state_path);assert state['prepared'] and not state['restored'];idle()
        suffix='' if args.attempt==1 else '-r'+str(args.attempt)
        name='handle'+suffix+'.json';assert not (base/name).exists()
        if args.attempt>1:
            previous=read_json(base/state.get('latest_handle','handle.json'))['job']['id']
            assert client.request('/api/jobs/'+previous)['job']['state'] in {'failed','cancelled','timed_out'},'Do not duplicate an active or already accepted parent'
        assert re.search(r'Ran 432 tests in [0-9.]+s\n\nOK\s*$',(ROOT/'runtime/unit-v036-final.log').read_text())
        request=read_json(ROOT/'runtime/server-install-v033-request.json')
        request.update(name='v0.36 declared long deadline admission node confirmation and full regression',memory_mib=4096,
            timeout_seconds=1500,idempotency_key='root-installed-deadline-v036-r2-20261030-'+chr(96+args.attempt))
        request['argv'].extend(['--native-toolchain','--payload-quota-resize','--deadline-limit']);atomic_json(base/('request'+suffix+'.json'),request)
        result=client.request('/api/jobs',request);atomic_json(base/name,result);state['latest_handle']=name;atomic_json(state_path,state)
        print(json.dumps({'job_id':result['job']['id'],'state':result['job']['state'],'cpu':2,'memory_mib':4096,'npu':0}))
    else:
        state=read_json(state_path);assert state['prepared'] and not state['restored'];summary=idle()
        identifier=read_json(base/state.get('latest_handle','handle.json'))['job']['id'];job=client.request('/api/jobs/'+identifier)['job']
        assert job['state'] in {'succeeded','failed','cancelled','timed_out'},'Observe the original live job; never restore during unknown execution'
        current=read_json(ROOT/'runtime/config.json');assert current==read_json(base/'prepared-main-config.json'),'Demo configuration changed; preserve the user edits'
        domain=next(d for d in summary['resource_domains'] if d['physical_id']==state['baseline_domain']['physical_id'])
        domain_set(domain,state['baseline_domain']['policy']['memory_mib'])
        command('labctl','stop');atomic_json(ROOT/'runtime/config.json',read_json(base/'original-main-config.json'));command('labctl','start')
        state.update(restored=True,parent_job=identifier,parent_state=job['state']);atomic_json(state_path,state)
        print(json.dumps({'restored':True,'demo_memory_mib':2048,'parent_job':identifier,'parent_state':job['state']}))


if __name__=='__main__':main()

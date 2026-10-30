"""Own Demo budget/handle lifecycle for the bounded mixed compiler acceptance."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,read_json


def command(name,*args):
    return subprocess.run([str(ROOT/name),*map(str,args)],check=True,capture_output=True,text=True,timeout=30).stdout


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['prepare','submit','restore']);parser.add_argument('--attempt',type=int,default=1)
    args=parser.parse_args();assert 1<=args.attempt<=26
    base=ROOT/'runtime/large-mixed-20261030';base.mkdir(mode=0o700,exist_ok=True);state_path=base/'control.json'
    client=Client.from_profile(ROOT/'runtime/config.json')
    def idle():
        s=client.request('/api/jobs')
        assert all(s['counts'][k]==0 for k in ['queued','reserved','running','cancelling','needs_attention'])
        assert not any(d['admission']['blocked'] for d in s['resource_domains']);return s
    def domain_set(d,memory):
        p=d['policy'];command('workerctl','domain-set',d['physical_id'],'--cpus',','.join(map(str,p['cpu_ids'])),
            '--memory-mib',memory,'--shm-mib',p['shm_mib'],'--max-running',p['max_running'],'--expected-revision',d['revision'])
    if args.phase=='prepare':
        previous=read_json(state_path)
        if previous:
            assert previous['restored'] and previous['parent_state'] in {'failed','timed_out','cancelled'}
            history=base/('control-before-attempt-'+str(args.attempt)+'.json');assert not history.exists()
            atomic_json(history,previous)
        s=idle();assert len(s['resource_domains'])==1;d=s['resource_domains'][0]
        assert d['policy']=={'cpu_ids':[254,255],'memory_mib':2048,'shm_mib':1024,'max_running':1}
        available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
        assert available>32*1024**3
        original=read_json(ROOT/'runtime/config.json');atomic_json(base/'original-main-config.json',original)
        prepared={**original,'scheduler':{**original['scheduler'],'memory_mib':12288,'max_timeout_seconds':10800}}
        atomic_json(base/'prepared-main-config.json',prepared)
        atomic_json(state_path,{'baseline_domain':d,'prepared':False,'restored':False})
        command('labctl','stop');atomic_json(ROOT/'runtime/config.json',prepared);command('labctl','start')
        domain_set(d,12288);atomic_json(state_path,{'baseline_domain':d,'prepared':True,'restored':False,
            **({'latest_handle':previous['latest_handle']} if previous else {})})
        print(json.dumps({'prepared':True,'demo_memory_mib':12288,'demo_cpus':[254,255],'max_timeout_seconds':10800,'host_global_settings_modified':False}))
    elif args.phase=='submit':
        state=read_json(state_path);assert state['prepared'] and not state['restored'];idle()
        name='handle'+('' if args.attempt==1 else '-r'+str(args.attempt))+'.json';assert not (base/name).exists()
        if args.attempt>1:
            previous=read_json(base/state['latest_handle'])['job']['id']
            assert client.request('/api/jobs/'+previous)['job']['state'] in {'failed','timed_out','cancelled'},'Never duplicate an active, unknown or already accepted parent'
        request={'name':'Quota-backed full compiler cold/warm and twenty-owner mixed workload',
            'kind':'command','environment':'host-python','cwd':str(ROOT),
            'argv':['/usr/bin/python3',str(ROOT/'tests/large_mixed_compile_check.py')],
            'cpu':2,'memory_mib':12288,'shm_mib':0,'npu':0,'timeout_seconds':10800,
            'idempotency_key':'quota-full-compiler-mixed-20261030-'+chr(96+args.attempt)}
        atomic_json(base/('request-'+str(args.attempt)+'.json'),request)
        result=client.request('/api/jobs',request);atomic_json(base/name,result)
        state['latest_handle']=name;atomic_json(state_path,state)
        print(json.dumps({'job_id':result['job']['id'],'state':result['job']['state'],'cpu':2,'memory_mib':12288,'npu':0}))
    else:
        state=read_json(state_path);assert state['prepared'] and not state['restored'];s=idle()
        identifier=read_json(base/state['latest_handle'])['job']['id'];job=client.request('/api/jobs/'+identifier)['job']
        assert job['state'] in {'succeeded','failed','cancelled','timed_out'},'Observe the original handle until actual terminal state'
        fixture=read_json(ROOT/'runtime/jobs'/identifier/'tmp/large-mixed/control.json')
        assert fixture is None or (fixture.get('cleaned') and not fixture.get('cleanup_errors')),'Reconcile recorded owned roles/volumes/loops before restoring the envelope'
        assert read_json(ROOT/'runtime/config.json')==read_json(base/'prepared-main-config.json'),'Preserve concurrent user configuration edits'
        domain=next(d for d in s['resource_domains'] if d['physical_id']==state['baseline_domain']['physical_id'])
        domain_set(domain,state['baseline_domain']['policy']['memory_mib'])
        command('labctl','stop');atomic_json(ROOT/'runtime/config.json',read_json(base/'original-main-config.json'));command('labctl','start')
        state.update(restored=True,parent_job=identifier,parent_state=job['state']);atomic_json(state_path,state)
        print(json.dumps({'restored':True,'demo_memory_mib':2048,'parent_job':identifier,'parent_state':job['state']}))


if __name__=='__main__':main()

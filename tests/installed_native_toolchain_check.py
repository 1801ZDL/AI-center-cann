"""Real managed-node native compilation, protected locks, cache and client proof."""
import base64
import copy
import hashlib
import json
import time


def upload(ctx,headers=None):
    data=(ctx['ROOT']/'examples/workflows/native_toolchain.py').read_bytes()
    status,value=ctx['api']('/api/inputs',{'files':[{'name':'native_toolchain.py','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]},extra_headers=headers)
    assert status in {200,201},value
    identity=value['input']['id']
    assert ctx['api']('/api/inputs/'+identity+'/blob',{'name':'native_toolchain.py','offset':0,'data':base64.b64encode(data).decode()},extra_headers=headers)[0]==200
    assert ctx['api']('/api/inputs/'+identity+'/seal',{},extra_headers=headers)[1]['input']['sealed']
    return identity


def submit(ctx,request,key,headers=None,variant=0,hold=False,environment=None):
    value=copy.deepcopy(request);value['idempotency_key']=key
    value['parameters']['argv']=['python','native_toolchain.py','--source-variant',str(variant),*(['--hold'] if hold else [])]
    if environment is not None:value['parameters']['environment']=environment
    status,response=ctx['api']('/api/jobs',value,extra_headers=headers);assert status==201,response
    return response['job']['id']


def result(ctx,identifier,headers=None):
    job=ctx['wait'](identifier,seconds=480 if ctx['options'].deadline_limit else 120,headers=headers);assert job['state']=='succeeded',job
    api=ctx['api'];details=api('/api/jobs/'+identifier,extra_headers=headers)[1];assert len(details['attempts'])==1
    def artifact(name,raw=False):return api('/api/jobs/'+identifier+'/artifact?name='+name,raw=raw,extra_headers=headers)[1]
    report=artifact('results%2Fnative-cache-report.json');runtime=artifact('environment.json');receipt=artifact('results%2F_lab%2Fenvironment.json')
    assert report['passed'] and report['uid']==runtime['task_uid'] and report['cap_effective']=='0000000000000000'
    assert report['no_new_privileges'] and report['kernel_launches']==report['kernel_loads']==report['device_queries']==0
    assert receipt['locked'] and receipt['workload_started'] and receipt['probe_exit_code']==receipt['workload_exit_code']==0
    assert not job['allocation']['npu_ids'] and job['resources']['npu']==0
    assert runtime['persistent_cache']['enforced'] and runtime['persistent_cache']['key']==job['allocation']['cache']['key']
    assert runtime['persistent_cache']['verified_identity']['uid']==report['uid']
    assert job['result']['cache_release_confirmed'] and job['result']['workspace_release_confirmed'] and job['result']['execution_cleanup_confirmed']
    binary=artifact('results%2Fnative-kernel.o',raw=True);assert binary[:4]==b'\x7fELF' and len(binary)==report['binary_bytes']
    assert hashlib.sha256(binary).hexdigest()==report['binary_sha256']
    manifest=artifact('results%2Fkernel-manifest.json');assert manifest['binary_sha256']==report['binary_sha256']
    assert b'tt.func' in artifact('ir%2Fkernel.ttir',raw=True)
    row=artifact('validated-result.json');assert row['passed'] and row['kind']=='build' and row['environment_locked']
    assert row['metrics']['artifacts']==len(report['artifacts'])
    return job,report,runtime


def assert_cold(report):
    assert report['triton_stages_called']==['ttir','ttadapter','mlirbc','bcmlir','npubin']
    assert not report['triton_cache_hit'] and report['cmake']['cache_misses']==2 and report['cmake']['cache_hits']==0


def assert_warm(report):
    assert not report['triton_stages_called'] and report['triton_cache_hit'] and not report['actual_external_tools']
    assert report['warm_triton_files_unchanged'] and report['cmake']['cache_misses']==0 and report['cmake']['cache_hits']==2


def run(ctx):
    api=ctx['api'];ctl=ctx['ctl'];node=ctx['node'];docker=ctx['docker']
    facts=ctx['configuration']['environment_manifest']['observed']
    assert facts['python']=='3.11.15' and facts['packages']['triton-ascend'].startswith('3.6.0')
    files={n:v['sha256'] for n,v in facts['files'].items() if v};assert len(files)>=6
    audit='/opt/ascend-lab-offline-compat.json';assert audit in files
    request=copy.deepcopy(ctx['request']);request.update(template='build_v1',name='Native offline compiler cache')
    request['parameters'].update(input_id=upload(ctx),argv=['python','native_toolchain.py','--source-variant','0'],
        lock={'python':facts['python'],'image_sha256':ctx['task_image'],'packages':{n:v if v is not None else '__absent__' for n,v in facts['packages'].items()},
              'tools':{n:v['sha256'] for n,v in facts['tools'].items() if v},'files':files},
        resources={'cpu':1,'memory_mib':2048,'shm_mib':0,'npu':0},timeout_seconds=300 if ctx['options'].deadline_limit else 90,
        workspace={'min_mib':12,'min_inodes':256,'durable':True},
        cache={'mode':'persistent','min_free_mib':8,'min_free_inodes':700},artifacts={'file_mib':8,'total_mib':16})
    # Reject a wrong actual backend lock and an impossible cache requirement
    # before creating an attempt, even though package version labels match.
    blocked=[]
    for kind in ['file-lock','cache-inodes']:
        value=copy.deepcopy(request);value['idempotency_key']='native-blocked-'+kind
        if kind=='file-lock':
            name=next(n for n in files if n.endswith('/compiler.py'));value['parameters']['lock']['files'][name]='0'*64
        else:value['parameters']['cache']['min_free_inodes']=10000
        status,response=api('/api/jobs',value);assert status==201,response
        identifier=response['job']['id'];time.sleep(.7);waiting=api('/api/jobs/'+identifier)[1]
        assert waiting['job']['state']=='queued' and not waiting['attempts'] and waiting['job']['allocation']=={}
        assert api('/api/jobs/'+identifier+'/cancel',{})[1]['job']['state']=='cancelled';blocked.append(identifier)
    first=submit(ctx,request,'native-managed-cold');cold,cold_report,metadata=result(ctx,first);assert_cold(cold_report)
    second=submit(ctx,request,'native-managed-warm');warm,warm_report,_=result(ctx,second);assert_warm(warm_report)
    assert warm_report['binary_sha256']==cold_report['binary_sha256'] and warm['allocation']['cache']==cold['allocation']['cache']
    holding=submit(ctx,request,'native-managed-restart',hold=True);deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        active=api('/api/jobs/'+holding)[1]['job']
        if active['state']=='running':break
        time.sleep(.2)
    assert active['state']=='running';name='ascend-lab-'+active['attempt_id'][8:]
    before=json.loads(docker(['inspect',name]).stdout)[0];assert before['State']['Running']
    ctl(node,'node','stop');assert ctl(node,'node','start')['ready']
    after=json.loads(docker(['inspect',name]).stdout)[0];assert after['State']['Running'] and after['State']['StartedAt']==before['State']['StartedAt']
    held,held_report,_=result(ctx,holding);assert_warm(held_report);assert held['attempt_id']==active['attempt_id']
    changed_id=submit(ctx,request,'native-managed-source-variant',variant=1)
    changed,changed_report,_=result(ctx,changed_id);assert_cold(changed_report)
    assert changed_report['source_variant']==1 and changed_report['cmake']['answer']==43
    assert changed['allocation']['cache']==cold['allocation']['cache'] and changed_report['binary_sha256']!=cold_report['binary_sha256']
    original_id=submit(ctx,request,'native-managed-original-source')
    original,original_report,_=result(ctx,original_id);assert_warm(original_report)
    assert original_report['binary_sha256']==cold_report['binary_sha256']
    environment_id=submit(ctx,request,'native-managed-environment-variant',environment={'TRITON_PRINT_AUTOTUNING':'1'})
    environment,environment_report,_=result(ctx,environment_id);assert_cold(environment_report)
    assert environment['allocation']['cache']['key']!=cold['allocation']['cache']['key']
    peer_request=copy.deepcopy(request);peer_request['parameters']['input_id']=upload(ctx,ctx['peer_headers'])
    peer_id=submit(ctx,peer_request,'native-managed-peer',headers=ctx['peer_headers'])
    peer,peer_report,_=result(ctx,peer_id,ctx['peer_headers']);assert_cold(peer_report);assert peer_report['uid']==23004
    assert peer['allocation']['cache']['key']!=cold['allocation']['cache']['key'] and api('/api/jobs/'+peer_id)[0]==404
    assert api('/api/jobs/'+first,extra_headers=ctx['peer_headers'])[0]==404
    rows=api('/api/results?limit=200')[1]['rows'];assert all(next(r for r in rows if r['job_id']==j)['passed'] for j in [first,second,holding,changed_id,original_id,environment_id])
    csv=api('/api/results.csv',raw=True)[1];assert all(j.encode() in csv for j in [first,second,holding,changed_id])
    proof={'passed':True,'request':request,'cold_job':first,'warm_job':second,'restart_job':holding,'source_variant_job':changed_id,
        'original_again_job':original_id,'environment_variant_job':environment_id,'peer_job':peer_id,
        'cache_key':cold['allocation']['cache']['key'],'binary_sha256':cold_report['binary_sha256'],'binary_bytes':cold_report['binary_bytes'],
        'source_variant_binary_sha256':changed_report['binary_sha256'],'certified_files':files,'certified_packages':facts['packages'],
        'task_image':ctx['task_image'],'task_uid':metadata['task_uid'],'peer_task_uid':peer_report['uid'],
        'blocked_zero_attempt_jobs':blocked,'cold_stages':cold_report['triton_stages_called'],'warm_stages':warm_report['triton_stages_called'],
        'warm_ccache_hits':warm_report['cmake']['cache_hits'],'warm_ccache_misses':warm_report['cmake']['cache_misses'],
        'restart_same_attempt':True,'same_container_started_at':before['State']['StartedAt'],
        'source_change_same_namespace':True,'environment_change_separate_namespace':True,'cross_owner_private':True,'results_and_csv':True,
        'kernel_launches':0,'scope':'Actual installed non-root managed CPU build and certified private offline compatibility image; not numerical/performance/device or full compiler source-build cache acceptance'}
    from lab_client import atomic_json
    from pathlib import Path
    import os
    atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/native-toolchain-ui.json',{'source_parent':Path(os.environ['LAB_JOB_DIR']).name,
        'scope':'Archived actual managed native compiler acceptance, not Main live jobs','jobs':[cold,warm,changed,original,environment],
        'rows':[r for r in rows if r['job_id'] in {first,second,changed_id,original_id,environment_id}]})
    return proof


def mcp(ctx,proof):
    home='/home/native-client';docker=ctx['docker'];controller=ctx['controller']
    handoff={'client.json':json.dumps({'controller':ctx['controller_url'],'token':ctx['token'],'ca_file':'controller-ca.pem'}),
        'controller-ca.pem':ctx['authority_text'],'workflow.json':json.dumps(proof['request'])}
    setup='import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642);v=json.loads(sys.stdin.read())\nfor n,b in v.items():\n f=p/n;f.write_text(b);f.chmod(0o600);os.chown(f,23642,23642)'
    docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',setup,home],input=json.dumps(handoff))
    helper=(ctx['ROOT']/'tests/native_https_mcp_helper.py').read_text()
    r=json.loads(docker(['exec','--user','23642:23642',controller,'/usr/bin/python3','-I','-c',helper,
        ctx['independent'],home,str(ctx['controller_state'])],timeout=150).stdout)
    assert r['passed'] and r['client_uid']==23642 and r['private_state_unreadable'] and r['reconnect_reused_job']
    job,report,_=result(ctx,r['job_id']);assert_warm(report);assert job['allocation']['cache']['key']==proof['cache_key']
    assert report['binary_sha256']==proof['binary_sha256']
    return r


def after_restore(ctx,proof):
    identifier=submit(ctx,proof['request'],'native-managed-after-control-restore')
    job,report,_=result(ctx,identifier);assert_warm(report)
    assert job['allocation']['cache']['key']==proof['cache_key'] and report['binary_sha256']==proof['binary_sha256']
    return {'passed':True,'job_id':identifier,'same_cache_key':True,'warm_stages':report['triton_stages_called'],
        'warm_ccache_hits':report['cmake']['cache_hits'],'warm_ccache_misses':report['cmake']['cache_misses'],'attempts':1}

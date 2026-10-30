"""Real fixed-image offline reports in the installed quota Node acceptance."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
from urllib.parse import quote

from environment_contract import lock_for
from operator_profile import csv_summary
from operator_workflow import prepare,source_locks

PERSONAL_CLIENT='/opt/ascend-lab-operator-client'

def upload(ctx,source,headers=None):
    files={str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert files and sum(map(len,files.values()))<8*1024**2
    status,response=ctx['api']('/api/inputs',{'files':[{'name':n,'size':len(v),'sha256':hashlib.sha256(v).hexdigest()} for n,v in sorted(files.items())]},extra_headers=headers)
    assert status in {200,201};identifier=response['input']['id']
    for name,raw in sorted(files.items()):
        for offset in range(0,len(raw),65536):
            assert ctx['api']('/api/inputs/'+identifier+'/blob',{'name':name,'offset':offset,'data':base64.b64encode(raw[offset:offset+65536]).decode()},extra_headers=headers)[0]==200
    sealed=ctx['api']('/api/inputs/'+identifier+'/seal',{},extra_headers=headers)[1]['input'];assert sealed['sealed']
    return identifier


def submit(ctx,request,key,headers=None):
    value=copy.deepcopy(request);value['idempotency_key']=key
    status,response=ctx['api']('/api/jobs',value,extra_headers=headers);assert status==201,response
    return response['job']['id']


def checked(ctx,identifier,uid,headers=None):
    job=ctx['wait'](identifier,seconds=90,headers=headers);assert job['state']=='succeeded',(identifier,job['state'],job.get('result'))
    detail=ctx['api']('/api/jobs/'+identifier,extra_headers=headers)[1];assert len(detail['attempts'])==1
    def artifact(name):return ctx['api']('/api/jobs/'+identifier+'/artifact?name='+quote(name,safe=''),extra_headers=headers)[1]
    report=artifact('results/operator-analysis.json');result=artifact('results/workload-result.json')
    receipt=artifact('results/_lab/environment.json');runtime=artifact('environment.json');validated=artifact('validated-result.json')
    assert report['generator_sha256']==source_locks()['/opt/ascend-lab-operator/app/operator_profile.py']
    assert report['comparison']['optimization_speedup'] is None and report['upstream_delta_check']['exit_code'] in {0,2}
    assert result['uid']==runtime['task_uid']==uid and result['cap_effective']=='0000000000000000' and result['no_new_privileges']
    assert runtime['network_mode']=='none' and runtime['read_only_root'] and not runtime['privileged'] and runtime['node_spool']['enforced']
    assert receipt['locked'] and receipt['workload_started'] and receipt['probe_exit_code']==receipt['workload_exit_code']==0
    assert validated['passed'] and validated['environment_locked'] and validated['kind']=='build' and validated['metrics']['artifacts']==2
    assert job['resources']['npu']==0 and not job['allocation']['npu_ids']
    assert job['result']['execution_cleanup_confirmed'] and job['result']['workspace_release_confirmed']
    assert runtime['workspace']['allocation']['provider']=='uid-quota-v1' and runtime['workspace']['user_quota']
    assert runtime['device_nodes']==[] and runtime['memory_limit']==768*1024**2 and runtime['ipc_mode']=='none'
    names={x['name'] for x in detail['artifacts']};assert 'results/shadow-module-executed' not in names
    assert 'results/operator-analysis.csv' in names
    return job,report


def read_personally(ctx,identifier,home):
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'submit_job','arguments':{}}},
        {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'job_id':'job-'+'f'*12}}}]
    client=PERSONAL_CLIENT;docker=ctx['docker'];container=ctx['controller']
    result=docker(['exec','-i','--user','23642:23642',container,'/usr/bin/python3','-B',client+'/operator_profile.py',
        'mcp','--profile',home+'/client.json','--job-id',identifier],input=''.join(json.dumps(v)+'\n' for v in messages))
    values=[json.loads(v)['result'] for v in result.stdout.splitlines()]
    assert len(values)==5 and len(values[1]['tools'])==1 and values[1]['tools'][0]['annotations']['readOnlyHint']
    assert not values[2]['isError'] and values[3]['isError'] and values[4]['isError']
    case=json.loads(values[2]['content'][0]['text']);assert case['job']['id']==identifier
    return case


def run(ctx):
    root=ctx['ROOT'];source=ctx['base']/'operator-source';shutil.copytree(Path(ctx['options'].operator_analysis),source)
    request_data=json.loads((source/'case.json').read_text())
    expected=[csv_summary((source/v['csv']).read_bytes(),request_data['operator']) for v in request_data['profiles']]
    # A data-only recipe must not import workload-controlled modules as tools.
    (source/'msprof_mcp').mkdir();(source/'msprof_mcp/__init__.py').write_text("from pathlib import Path;Path('/work/results/shadow-module-executed').write_text('BAD');raise RuntimeError('Untrusted module shadowed pinned tools')\n")
    (source/'csv.py').write_text("raise RuntimeError('Untrusted CSV module imported')\n")
    input_id=upload(ctx,source);lock=lock_for(ctx['configuration']['environment_manifest'])
    fixed=prepare(input_id,lock,idempotency_key='operator-fixed-original')
    home='/home/operator-client';files={'client.json':json.dumps({'controller':ctx['controller_url'],'token':ctx['token']}),
        'lock.json':json.dumps(lock)}
    setup="import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642);v=json.loads(sys.stdin.read());[(lambda f,b:(f.write_text(b),f.chmod(0o600),os.chown(f,23642,23642)))(p/n,b) for n,b in v.items()]"
    ctx['docker'](['exec','-i',ctx['controller'],'/usr/bin/python3','-I','-c',setup,home],input=json.dumps(files))
    ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-B','/opt/ascend-lab-controller/current/install-client','--prefix',PERSONAL_CLIENT])
    ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',PERSONAL_CLIENT+'/labctl',
        '--profile',home+'/client.json','environment-lock',ctx['probe_job']['id'],'--output',home+'/exported-lock.json'])
    exported=json.loads(ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-I','-c',
        'from pathlib import Path;import sys;print(Path(sys.argv[1]).read_text())',home+'/exported-lock.json']).stdout)
    assert exported==lock
    before={v['id'] for v in ctx['api']('/api/jobs')[1]['jobs']}
    result=ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',PERSONAL_CLIENT+'/operator_workflow.py',
        '--input-id',input_id,'--lock',home+'/exported-lock.json','--output',home+'/request.json','--idempotency-key','operator-fixed-original'])
    assert json.loads(result.stdout)['submitted'] is False
    assert before=={v['id'] for v in ctx['api']('/api/jobs')[1]['jobs']}
    saved=json.loads(ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-I','-c',
        "import json,os,sys;from pathlib import Path;assert os.getuid()==23642;print(Path(sys.argv[1]).read_text())",home+'/request.json']).stdout)
    assert saved==fixed
    # This local edit is confined to the copied fixture, after immutable sealing.
    (source/request_data['profiles'][0]['csv']).write_text('local source changed after sealing\n')
    first=submit(ctx,saved,'operator-fixed-original');job,report=checked(ctx,first,23003)
    assert [v['samples'] for v in report['sources']]==[v['samples'] for v in expected]
    assert [v['mean_us'] for v in report['sources']]==[v['mean_us'] for v in expected]
    case=read_personally(ctx,first,home);assert case['report']['comparison']==report['comparison']
    assert ctx['api']('/api/jobs/'+first,extra_headers=ctx['peer_headers'])[0]==404
    assert ctx['api']('/api/inputs/'+input_id,extra_headers=ctx['peer_headers'])[0]==404
    wrong=copy.deepcopy(saved);key=next(v for v in lock['files'] if v.endswith('msprof_mcp/server.py'));wrong['parameters']['lock']['files'][key]='0'*64
    blocked=submit(ctx,wrong,'operator-invalid-tool-lock');time.sleep(1)
    value=ctx['api']('/api/jobs/'+blocked)[1];assert value['job']['state']=='queued' and not value['attempts']
    assert ctx['api']('/api/jobs/'+blocked+'/cancel',{})[1]['job']['state']=='cancelled'
    other=ctx['base']/'operator-peer-source';shutil.copytree(Path(ctx['options'].operator_analysis),other)
    peer_input=upload(ctx,other,ctx['peer_headers']);peer_request=prepare(peer_input,lock)
    peer=submit(ctx,peer_request,'operator-fixed-peer',ctx['peer_headers']);_,peer_report=checked(ctx,peer,23004,ctx['peer_headers'])
    assert peer_report['comparison']==report['comparison'] and ctx['api']('/api/jobs/'+peer)[0]==404
    # Validly sealed bad data must fail as its own task, not silently get a report.
    bad_source=ctx['base']/'operator-invalid-source';shutil.copytree(other,bad_source)
    target=bad_source/request_data['profiles'][0]['csv'];target.write_text('Name,Duration(us),Device_id\n'+request_data['operator']+',NaN,0\n')
    bad_input=upload(ctx,bad_source);bad=submit(ctx,prepare(bad_input,lock),'operator-invalid-csv')
    failed=ctx['wait'](bad,seconds=90);assert failed['state']=='failed'
    detail=ctx['api']('/api/jobs/'+bad)[1];assert len(detail['attempts'])==1 and 'results/operator-analysis.json' not in {v['name'] for v in detail['artifacts']}
    receipt=ctx['api']('/api/jobs/'+bad+'/artifact?name=results%2F_lab%2Fenvironment.json')[1]
    assert receipt['locked'] and receipt['workload_started'] and receipt['workload_exit_code']!=0
    return {'passed':True,'first_job':first,'peer_job':peer,'bad_data_job':bad,'invalid_lock_zero_attempt_job':blocked,
        'request':saved,'personal_home':home,'personal_uid':23642,'task_uids':[23003,23004],'input_id':input_id,
        'data_only_shadow_modules_not_imported':True,'sealed_input_survives_local_edit':True,'fixed_recipe_preparation_did_not_submit':True,
        'actual_stdio_read_only_report':True,'cross_owner_input_and_report_denied':True,'bad_data_failed_without_analysis_report':True,
        'source_generator_sha256':report['generator_sha256'],'means_us':[v['mean_us'] for v in report['sources']],
        'optimization_speedup':None,'image':ctx['task_image'],'packages':lock['packages'],'certified_files':lock['files'],
        'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Same-host installed UID-separated services and actual fixed-image CPU report tasks on native UID quota storage. Existing build_v1 contract; not a compiler build, NPU measurement, actual PID1 or two physical hosts.'}


def after_restore(ctx,proof):
    case=read_personally(ctx,proof['first_job'],proof['personal_home'])
    assert case['report']['generator_sha256']==proof['source_generator_sha256']
    identifier=submit(ctx,proof['request'],'operator-fixed-after-restore');_,report=checked(ctx,identifier,23003)
    assert [v['mean_us'] for v in report['sources']]==proof['means_us']
    return {'passed':True,'new_job':identifier,'attempts':1,'original_archive_readable':True,'original_sealed_input_reused':True}

"""Historical real tensor bytes through installed CPU budgets and personal MCP."""
import copy
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess

from accuracy_data import INPUT,DRIVER,compare,strict_json
from accuracy_diagnostic import read_case
from environment_contract import lock_for
from installed_operator_check import upload
from lab_client import Client,ClientHTTPError,atomic_json


def run(ctx):
    base=ctx['base'];reference=json.loads(Path(ctx['options'].accuracy_case).read_text());lock=lock_for(ctx['configuration']['environment_manifest'])
    spec={'name':'Historical precision reference investigation','idempotency_key':'accuracy-budget',
        'limits':{'jobs':3,'cpu_seconds':180,'npu_seconds':0},'templates':['build_v1'],'lifetime_seconds':900}
    status,value=ctx['api']('/api/experiment-budgets',spec);assert status==201,value
    budget=value['budget']['id'];status,value=ctx['api']('/api/experiment-budgets/'+budget+'/token',{});assert status==200,value
    profile=base/'accuracy-budget-client.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':value['token']});client=Client.from_profile(profile)
    installed=base/'accuracy-client';subprocess.run([str(ctx['ROOT']/'install-client'),'--prefix',str(installed)],check=True,capture_output=True,text=True,timeout=30)
    public='/opt/ascend-lab-accuracy-client';home='/home/accuracy-client'
    ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-B','/opt/ascend-lab-controller/current/install-client','--prefix',public])
    code="import os,sys;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642);f=p/'client.json';f.write_bytes(sys.stdin.buffer.read());f.chmod(0o600);os.chown(f,23642,23642)"
    ctx['docker'](['exec','-i',ctx['controller'],'/usr/bin/python3','-I','-c',code,home],input=json.dumps({'controller':ctx['controller_url'],'token':ctx['token']}))
    denied=ctx['docker'](['exec','--user','23004:23004',ctx['controller'],'/usr/bin/python3','-I','-c',
        'import sys;from pathlib import Path;Path(sys.argv[1]).read_bytes()',home+'/client.json'],check=False);assert denied.returncode!=0
    tasks=[]
    for dataset in reference['datasets']:
        label=dataset['id'];source=Path(dataset['path']);assert source.is_relative_to(ctx['ROOT']/'runtime')
        value=strict_json((source/INPUT).read_bytes());blobs={k:(source/v['file']).read_bytes() for k,v in value.items() if k in {'actual','reference','source_context'}}
        baseline=compare(value,blobs);assert baseline['mismatches']==dataset['mismatches']
        sealed=base/('accuracy-source-'+label);shutil.copytree(source,sealed)
        (sealed/DRIVER).write_bytes((ctx['ROOT']/DRIVER).read_bytes());input_id=upload(ctx,sealed)
        request={'name':'Historical accuracy replay '+label,'template':'build_v1','environment':'worker-python',
            'parameters':{'input_id':input_id,'argv':['python','-I','-B',DRIVER],'lock':copy.deepcopy(lock),
            'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60,'input_mode':'readonly',
            'workspace':{'min_mib':8,'min_inodes':128,'durable':True},'artifacts':{'file_mib':8,'total_mib':32}}}
        request_file=base/('accuracy-'+label+'-request.json');atomic_json(request_file,request)
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',str(profile),'--session-dir',str(base/('accuracy-session-'+label)),
            '--environment','worker-python','--workflow',str(request_file)]
        calls=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':'build_v1','request_key':'original'}}}]
        def conversation():
            result=subprocess.run(command,input=''.join(json.dumps(v)+'\n' for v in calls),capture_output=True,text=True,timeout=60)
            assert result.returncode==0,'Inspect the original private MCP handle; never replace an uncertain task'
            response=json.loads(result.stdout.splitlines()[-1])['result'];assert not response.get('isError'),response
            return json.loads(response['content'][0]['text'])
        started=conversation();identifier=started['job']['id'];task={'job_id':identifier,'dataset':label,'input_id':input_id};tasks.append(task)
        atomic_json(base/'accuracy-tasks-in-progress.json',{'budget_id':budget,'tasks':tasks})
        job=ctx['wait'](identifier,seconds=90);assert job['state']==dataset['expected_state'],job
        detail=client.request('/api/jobs/'+identifier);assert len(detail['attempts'])==1
        assert all(job['result'][key] is True for key in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed'])
        case=read_case(client,identifier);assert case['comparison']==baseline and case['all_tensor_bytes_verified']
        assert case['workflow_binding']['driver_known_to_client'] and case['workflow_binding']['protected_environment_matched']
        assert not case['workflow_binding']['device_capture_independently_certified'] and not case['tensor_payload_delivered_to_model']
        atomic_json(base/('accuracy-'+label+'-case.json'),case)
        replay=conversation();assert replay['reused'] and replay['job']['id']==identifier
        assert ctx['api']('/api/jobs/'+identifier,extra_headers=ctx['peer_headers'])[0]==404
        metadata=ctx['api']('/api/jobs/'+identifier+'/artifact?name=environment.json')[1]
        assert metadata['task_uid']==23003 and metadata['node_daemon_uid']==0 and metadata['device_nodes']==[] and metadata['network_mode']=='none'
        assert metadata['workspace']['user_quota'] and metadata['workspace']['allocation']['provider']=='uid-quota-v1'
        data=ctx['api']('/api/jobs/'+identifier+'/artifact?name=results%2Faccuracy-analysis.csv',raw=True)[1]
        row=next(csv.DictReader(io.StringIO(data.decode())));assert int(row['mismatches'])==baseline['mismatches'] and row['passed']==str(baseline['passed'])
        messages=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_job','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'path':'/etc/passwd'}}}]
        result=ctx['docker'](['exec','-i','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',public+'/accuracy_diagnostic.py',
            'mcp','--profile',home+'/client.json','--job-id',identifier],input=''.join(json.dumps(v)+'\n' for v in messages),timeout=90)
        values=[json.loads(v)['result'] for v in result.stdout.splitlines()];assert not values[0]['isError'] and values[1]['isError'] and values[2]['isError']
        personal_case=json.loads(values[0]['content'][0]['text']);assert personal_case['comparison']==baseline
        cli=ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',public+'/labctl',
            '--profile',home+'/client.json','accuracy-case',identifier]);assert json.loads(cli.stdout)['comparison']==baseline
        task.update(attempt_id=job['attempt_id'],state=job['state'],mismatches=baseline['mismatches'],elements=baseline['elements'],
            actual_sha256=value['actual']['sha256'],reference_sha256=value['reference']['sha256'],reference_id=value['reference']['origin']['id'],
            numeric_metrics=baseline['numeric_metrics'])
    assert tasks[0]['actual_sha256']==tasks[1]['actual_sha256'] and tasks[0]['reference_sha256']!=tasks[1]['reference_sha256']
    assert [v['mismatches'] for v in tasks]==[11,0,0] and tasks[2]['numeric_metrics']['max_abs_error']==0
    rows={r['job_id']:r for r in ctx['api']('/api/results?kind=build')[1]['rows']}
    assert [rows[v['job_id']]['passed'] for v in tasks]==[False,True,True]
    assert not any(v['job_id'] in {r['job_id'] for r in ctx['api']('/api/results',extra_headers=ctx['peer_headers'])[1]['rows']} for v in tasks)
    charged=client.request('/api/experiment-budgets/'+budget)['budget']['charged'];assert charged=={'jobs':3,'cpu_seconds':180,'npu_seconds':0}
    return {'passed':True,'tasks':tasks,'budget_id':budget,'charged':charged,'private_client':str(profile),
        'same_actual_bytes_different_reference_contracts':True,'original_failure_preserved':True,'result_rows_and_csv_preserve_failure':True,
        'actual_personal_uid':23642,'actual_task_uid':23003,'personal_profile_peer_unreadable':True,
        'independent_tensor_recomputation':True,'actual_stdio_and_cli':True,'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Actual installed CPU replay of historical HiF8 capture bytes and supplied CPU reference bytes. No new NPU execution, independently certified capture origin, operator repair, formal ISA ruling, PID1 or two physical hosts.'}


def after_restore(ctx,proof):
    old=Client.from_profile(proof['private_client'])
    try:old.request('/api/me')
    except ClientHTTPError as error:assert error.status==401
    else:raise AssertionError('Old accuracy spending credential survived restore')
    budget=ctx['api']('/api/experiment-budgets/'+proof['budget_id'])[1]['budget'];assert budget['state']=='restored_frozen' and budget['charged']==proof['charged']
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    for task in proof['tasks']:
        detail=client.request('/api/jobs/'+task['job_id']);assert detail['job']['state']==task['state'] and detail['job']['attempt_id']==task['attempt_id'] and len(detail['attempts'])==1
        case=read_case(client,task['job_id']);assert case['comparison']['mismatches']==task['mismatches'] and case['tensor_sources']['actual']['sha256']==task['actual_sha256']
    return {'passed':True,'attempts_and_original_failure_preserved':True,'charged_cpu_seconds':180,'old_spending_credential_revoked':True,'new_replays':0}

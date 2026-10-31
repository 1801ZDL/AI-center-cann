"""Actual failed precision/compiler and simulator reports, read through installed UI."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

from environment_contract import lock_for
from lab_client import Client,ClientHTTPError,atomic_json
from report_view import KINDS
from simulation_data import sha


def run(ctx):
    base=ctx['base'];reference=json.loads(Path(ctx['options'].report_view).read_text());assert reference['image']==ctx['task_image']
    owner=Client({'controller':ctx['controller_url'],'token':ctx['token']});lock=lock_for(ctx['configuration']['environment_manifest'])
    budget=owner.request('/api/experiment-budgets',{'name':'Installed archive report presentation','idempotency_key':'report-view-budget',
        'limits':{'jobs':3,'cpu_seconds':240,'npu_seconds':0},'templates':['ir_v1','build_v1'],'lifetime_seconds':900})['budget']['id']
    issued=owner.request('/api/experiment-budgets/'+budget+'/token',{});profile=base/'report-view-budget-client.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':issued['token']})
    installed=base/'report-view-client';subprocess.run([str(ctx['ROOT']/'install-client'),'--prefix',str(installed)],capture_output=True,text=True,check=True,timeout=30)
    tasks=[];cases=[]
    for kind,timeout in [('accuracy',60),('simulation',120),('compiler',60)]:
        source=base/('report-view-source-'+kind)
        if kind in {'accuracy','simulation'}:
            original=Path(reference[kind+'_source']);assert original.is_relative_to(ctx['ROOT']/'runtime');shutil.copytree(original,source)
            driver=kind+'_data.py';(source/driver).write_bytes((ctx['ROOT']/driver).read_bytes());template='build_v1'
        else:
            source.mkdir(mode=0o700);original=Path(reference['compiler_source']);assert original.is_relative_to(ctx['ROOT']/'runtime')
            raw=original.read_bytes();assert sha(raw)==reference['compiler_sha256'];(source/'input.mlir').write_bytes(raw)
            driver='compiler_compatibility.py';(source/driver).write_bytes((ctx['ROOT']/'examples/workflows'/driver).read_bytes());template='ir_v1'
            tool=reference['tools']['triton'];assert lock['files'][tool['path']]==tool['sha256']
            atomic_json(source/'compiler-case.json',{'schema':'ascend-lab-compiler-input/v2','tool':'triton-mlir-opt','path':tool['path'],'sha256':tool['sha256']})
        input_id=owner.upload_input(source)['id']
        request={'name':'Archive report '+kind,'template':template,'environment':'worker-python','parameters':{
            'input_id':input_id,'argv':['python','-I','-B',driver],'lock':copy.deepcopy(lock),'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},
            'timeout_seconds':timeout,'input_mode':'readonly','workspace':{'min_mib':256,'min_inodes':128,'durable':True},'artifacts':{'file_mib':32,'total_mib':128}}}
        request_file=base/('report-view-'+kind+'-request.json');atomic_json(request_file,request)
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',str(profile),'--session-dir',str(base/('report-view-session-'+kind)),
            '--environment','worker-python','--workflow',str(request_file)]
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':template,'request_key':'original'}}}]
        def conversation():
            reply=subprocess.run(command,input=''.join(json.dumps(v)+'\n' for v in messages),capture_output=True,text=True,timeout=60)
            assert reply.returncode==0,'Inspect original MCP handle; never substitute an uncertain task'
            value=json.loads(reply.stdout.splitlines()[-1])['result'];assert not value.get('isError'),value
            return json.loads(value['content'][0]['text'])
        started=conversation();identifier=started['job']['id'];item={'kind':kind,'job_id':identifier,'input_id':input_id};tasks.append(item)
        atomic_json(base/'report-view-tasks-in-progress.json',{'budget_id':budget,'tasks':tasks})
        job=ctx['wait'](identifier,seconds=180);assert job['state']==('succeeded' if kind=='simulation' else 'failed'),(identifier,job['state'],job.get('result'))
        assert len(owner.request('/api/jobs/'+identifier)['attempts'])==1
        assert all(job['result'][k] is True for k in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed'])
        env=owner.request('/api/jobs/'+identifier+'/artifact?name=environment.json');assert env['task_uid']==23003 and env['device_nodes']==[] and env['network_mode']=='none' and env['workspace']['user_quota']
        result=owner.request('/api/jobs/'+identifier+'/diagnostic-report?kind='+kind)
        assert result['verification']['report_bytes_verified'] and not result['verification']['source_bytes_recomputed']
        if kind=='accuracy':assert result['summary']['mismatches']==11
        elif kind=='simulation':assert result['summary']['capture_state']=='abnormal' and len(result['summary']['cores'])==4
        else:assert result['summary']['source']['sha256']==reference['compiler_sha256'] and result['summary']['exit_code']==1
        replay=conversation();assert replay['reused'] and replay['job']['id']==identifier
        item.update(attempt_id=job['attempt_id'],state=job['state'],report_sha256=result['report']['sha256'])
        cases.append({'kind':kind,'job_id':identifier,'attempt_id':job['attempt_id'],'state':job['state'],'report_sha256':result['report']['sha256'],'summary':result['summary']})
        assert ctx['api']('/api/jobs/'+identifier+'/diagnostic-report?kind='+kind,extra_headers=ctx['peer_headers'])[0]==404
    identifier=ctx['operator_proof']['first_job'];job=owner.request('/api/jobs/'+identifier)['job'];result=owner.request('/api/jobs/'+identifier+'/diagnostic-report?kind=operator')
    assert result['summary']['optimization_speedup'] is None
    cases.append({'kind':'operator','job_id':identifier,'attempt_id':job['attempt_id'],'state':job['state'],'report_sha256':result['report']['sha256'],'summary':result['summary']})
    browser_profile=base/'report-view-browser-client.json';atomic_json(browser_profile,{'controller':ctx['outer_url'],'username':ctx['personal']['username'],'password':ctx['personal']['password']})
    case_file=base/'report-view-browser-cases.json';atomic_json(case_file,cases);output=base/'report-view-browser.json'
    browser=subprocess.run(['/bin/bash',str(ctx['ROOT']/'check-browser.sh'),'tests/browser_installed_report_check.py','--profile',str(browser_profile),'--cases',str(case_file),'--output',str(output)],
        cwd=ctx['ROOT'],capture_output=True,text=True,timeout=240)
    (base/'report-view-browser.log').write_text(browser.stdout+browser.stderr)
    assert browser.returncode==0,'Inspect private actual browser log and original report jobs; no replacement submission'
    browser_proof=json.loads(output.read_text());assert browser_proof['passed'] and browser_proof['no_new_jobs_or_attempts']
    charged=owner.request('/api/experiment-budgets/'+budget)['budget']['charged'];assert charged=={'jobs':3,'cpu_seconds':240,'npu_seconds':0}
    return {'passed':True,'tasks':tasks,'cases':cases,'budget_id':budget,'charged':charged,'private_budget_profile':str(profile),
        'browser':browser_proof,'actual_task_uid':23003,'read_only_view_did_not_submit':True,'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Actual installed bounded report presentation for four archived CPU report types, including historical precision failure, full IR parse error, abnormal simulator capture and pinned performance comparison. GUI checks confirmed report bytes, not independent full-source recomputation.'}


def after_restore(ctx,proof):
    try:Client.from_profile(proof['private_budget_profile']).request('/api/me')
    except ClientHTTPError as error:assert error.status==401
    else:raise AssertionError('Old report experiment spending credential survived restore')
    owner=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    budget=owner.request('/api/experiment-budgets/'+proof['budget_id'])['budget'];assert budget['state']=='restored_frozen' and budget['charged']==proof['charged']
    for case in proof['cases']:
        detail=owner.request('/api/jobs/'+case['job_id']);assert detail['job']['state']==case['state'] and detail['job']['attempt_id']==case['attempt_id'] and len(detail['attempts'])==1
        view=owner.request('/api/jobs/'+case['job_id']+'/diagnostic-report?kind='+case['kind'])
        assert view['report']['sha256']==case['report_sha256'] and view['summary']==case['summary'] and not view['verification']['source_bytes_recomputed']
    return {'passed':True,'same_report_hashes_states_and_attempts':True,'old_spending_token_revoked':True,'source_replay_claimed':False,'new_report_tasks':0}

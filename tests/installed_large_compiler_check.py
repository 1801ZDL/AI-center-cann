"""Full historical IR, actual fixed compilers and budget-bound stdio sessions."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

from compiler_diagnostic import read_case
from environment_contract import lock_for
from installed_operator_check import upload
from lab_client import Client,ClientHTTPError,atomic_json


def run(ctx):
    base=ctx['base'];reference=json.loads(Path(ctx['options'].large_compiler_case).read_text())
    source=Path(reference['source_file']);raw=source.read_bytes()
    assert source.is_relative_to(ctx['ROOT']/'runtime') and not source.is_symlink()
    assert 8192<len(raw)<=8*1024**2 and hashlib.sha256(raw).hexdigest()==reference['source_sha256']
    assert reference['image']==ctx['task_image'];lock=lock_for(ctx['configuration']['environment_manifest'])
    spec={'name':'Historical full IR paired compiler investigation','idempotency_key':'large-ir-budget',
        'limits':{'jobs':2,'cpu_seconds':120,'npu_seconds':0},'templates':['ir_v1'],'lifetime_seconds':900}
    status,value=ctx['api']('/api/experiment-budgets',spec);assert status==201,value
    budget=value['budget']['id'];status,value=ctx['api']('/api/experiment-budgets/'+budget+'/token',{})
    assert status==200,value
    profile=base/'large-ir-budget-client.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':value['token']})
    client=Client.from_profile(profile);installed=base/'large-ir-client'
    subprocess.run([str(ctx['ROOT']/'install-client'),'--prefix',str(installed)],capture_output=True,text=True,check=True,timeout=30)
    tasks=[]
    def conversation(request,path,session):
        atomic_json(path,request)
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',str(profile),
            '--session-dir',str(session),'--environment','worker-python','--workflow',str(path)]
        calls=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':'ir_v1','request_key':'original'}}}]
        result=subprocess.run(command,input=''.join(json.dumps(v)+'\n' for v in calls),capture_output=True,text=True,timeout=60)
        assert result.returncode==0,'Inspect original MCP stderr/handle; never replace an uncertain task'
        reply=json.loads(result.stdout.splitlines()[-1])['result']
        assert not reply.get('isError'),reply
        return json.loads(reply['content'][0]['text'])
    for label,tool in reference['tools'].items():
        assert lock['files'][tool['path']]==tool['sha256']
        directory=base/('large-ir-source-'+label);directory.mkdir(mode=0o700)
        (directory/'input.mlir').write_bytes(raw)
        (directory/'compiler_compatibility.py').write_bytes((ctx['ROOT']/'examples/workflows/compiler_compatibility.py').read_bytes())
        name='triton-mlir-opt' if label=='triton' else 'bishengir-opt'
        atomic_json(directory/'compiler-case.json',{'schema':'ascend-lab-compiler-input/v2','tool':name,'path':tool['path'],'sha256':tool['sha256']})
        input_id=upload(ctx,directory)
        request={'name':'Historical full operator IR '+label,'template':'ir_v1','environment':'worker-python',
            'parameters':{'input_id':input_id,'argv':['python','-I','-B','compiler_compatibility.py'],'lock':copy.deepcopy(lock),
            'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60,'input_mode':'readonly',
            'workspace':{'min_mib':8,'min_inodes':128,'durable':True},'artifacts':{'file_mib':8,'total_mib':20}}}
        path=base/('large-ir-'+label+'-request.json');session=base/('large-ir-'+label+'-session')
        started=conversation(request,path,session);identifier=started['job']['id']
        task={'job_id':identifier,'label':label,'input_id':input_id};tasks.append(task)
        atomic_json(base/'large-ir-tasks-in-progress.json',{'budget':budget,'tasks':tasks})
        job=ctx['wait'](identifier,seconds=90);assert job['state'] in {'succeeded','failed'},job
        detail=client.request('/api/jobs/'+identifier);assert len(detail['attempts'])==1
        assert job['result']['termination_confirmed'] and job['result']['execution_cleanup_confirmed'] and job['result']['workspace_release_confirmed']
        case=read_case(client,identifier)
        assert job['state']==('failed' if label=='triton' else 'succeeded')
        assert case['compiler_stderr_archived']==(label=='triton')
        if label=='bisheng':assert case['compiler_stderr_sha256'] is None and case['compiler_report']['outcome']['stderr_bytes']==0
        assert case['source_ir_sha256']==reference['source_sha256'] and case['source_verified_bytes']==len(raw)
        assert not case['source_complete'] and case['source_ir'] is None
        assert case['workflow_binding']['driver_known_to_client'] and case['workflow_binding']['protected_tool_lock_matched']
        assert case['compiler_report']['outcome']['timed_out'] is False
        assert case['compiler_report']['tool']['sha256']==tool['sha256']
        assert len(json.dumps(case).encode())<192*1024
        replay=conversation(request,path,session);assert replay['reused'] and replay['job']['id']==identifier
        assert ctx['api']('/api/jobs/'+identifier,extra_headers=ctx['peer_headers'])[0]==404
        metadata=ctx['api']('/api/jobs/'+identifier+'/artifact?name=environment.json')[1]
        assert metadata['task_uid']==23003 and metadata['device_nodes']==[] and metadata['network_mode']=='none'
        atomic_json(base/('large-ir-'+label+'-case.json'),case)
        task.update(attempt_id=job['attempt_id'],state=job['state'],case_sha256=case['case_sha256'],
            compiler_exit_code=case['compiler_report']['outcome']['exit_code'],tool_sha256=tool['sha256'])
        # The separately installed personal UID gets the same bounded case via
        # the ordinary read-only compiler MCP, not central database access.
        home=ctx['operator_proof']['personal_home'];messages=[{'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'get_diagnostic_case','arguments':{}}},{'jsonrpc':'2.0','id':2,'method':'tools/call',
            'params':{'name':'get_diagnostic_case','arguments':{'path':'/etc/passwd'}}}]
        reply=ctx['docker'](['exec','-i','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',
            '/opt/ascend-lab-operator-client/compiler_diagnostic.py','mcp','--profile',home+'/client.json','--job-id',identifier],
            input=''.join(json.dumps(v)+'\n' for v in messages),timeout=90)
        values=[json.loads(v)['result'] for v in reply.stdout.splitlines()]
        assert not values[0]['isError'] and values[1]['isError']
        personal_case=json.loads(values[0]['content'][0]['text']);assert personal_case['source_ir_sha256']==reference['source_sha256']
    assert any(v['state']=='failed' for v in tasks),'Expected an actual incompatible consumer; inspect observations without inventing a failure'
    charged=client.request('/api/experiment-budgets/'+budget)['budget']['charged'];assert charged=={'jobs':2,'cpu_seconds':120,'npu_seconds':0}
    return {'passed':True,'tasks':tasks,'budget_id':budget,'charged':charged,'private_client':str(profile),
        'source_sha256':reference['source_sha256'],'source_bytes':len(raw),'source_origin':'Read-only copy of historical MergeTopK baseline HIVM dump',
        'complete_archive_and_seal_verified':True,'bounded_personal_stdio_mcp':True,'mcp_reconnect_same_attempt':True,
        'preview_patch_authority':False,'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Actual full historical operator IR parse/verify with two pinned consumer tools. Not full lowering, a compiler repair, numerical/device validation or independent proof of the original dump producer.'}


def after_restore(ctx,proof):
    old=Client.from_profile(proof['private_client'])
    try:old.request('/api/me')
    except ClientHTTPError as error:assert error.status==401
    else:raise AssertionError('Old budget token survived restore')
    value=ctx['api']('/api/experiment-budgets/'+proof['budget_id'])[1]['budget']
    assert value['state']=='restored_frozen' and value['charged']==proof['charged']
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    for task in proof['tasks']:
        detail=client.request('/api/jobs/'+task['job_id'])
        assert detail['job']['state']==task['state'] and detail['job']['attempt_id']==task['attempt_id'] and len(detail['attempts'])==1
        case=read_case(client,task['job_id']);assert case['source_ir_sha256']==proof['source_sha256'] and case['source_verified_bytes']==proof['source_bytes']
    return {'passed':True,'original_attempts_and_source_preserved':True,'charges_preserved':True,'old_spending_token_revoked':True,'new_compiler_tasks':0}

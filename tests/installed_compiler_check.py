"""Paired actual IR parser failure, bounded agent draft and queued candidate."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from compiler_diagnostic import read_case,review_minimal_rename
from installed_operator_check import upload
from lab_client import Client,atomic_json


def submit(ctx,source,tool,key,lock):
    source.mkdir(mode=0o700)
    (source/'compiler_compatibility.py').write_bytes((ctx['ROOT']/'examples/workflows/compiler_compatibility.py').read_bytes())
    (source/'input.mlir').write_text(tool['input'])
    atomic_json(source/'compiler-case.json',{'schema':'ascend-lab-compiler-input/v1','tool':tool['name'],'path':tool['path'],'sha256':tool['sha256']})
    identifier=upload(ctx,source)
    request={'name':'Compiler IR compatibility '+key,'template':'ir_v1','environment':'worker-python','idempotency_key':key,
        'parameters':{'input_id':identifier,'argv':['python','-I','-B','compiler_compatibility.py'],'lock':copy.deepcopy(lock),
        'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60,'input_mode':'readonly',
        'workspace':{'min_mib':8,'min_inodes':128,'durable':True},'artifacts':{'file_mib':1,'total_mib':2}}}
    atomic_json(ctx['base']/(key+'-request.json'),request)
    status,response=ctx['api']('/api/jobs',request);assert status==201,response
    job=ctx['wait'](response['job']['id'],seconds=90);assert job['state'] in {'failed','succeeded'}
    assert len(ctx['api']('/api/jobs/'+job['id'])[1]['attempts'])==1
    return job,request


def artifact(ctx,identifier,name):return ctx['api']('/api/jobs/'+identifier+'/artifact?name='+name.replace('/','%2F'))[1]


def run(ctx):
    from environment_contract import lock_for
    reference_path=Path(ctx['options'].compiler_case);reference=json.loads(reference_path.read_text())
    assert reference['image']==ctx['task_image']
    lock=lock_for(ctx['configuration']['environment_manifest'])
    newer=reference['tools']['triton'];older=reference['tools']['bisheng']
    for tool in [newer,older]:assert lock['files'][tool['path']]==tool['sha256']
    original=reference['original_ir'];approved=reference['approved_edit']
    modern={'name':'triton-mlir-opt','path':newer['path'],'sha256':newer['sha256'],'input':original}
    legacy={'name':'bishengir-opt','path':older['path'],'sha256':older['sha256'],'input':original}
    old_job,_=submit(ctx,ctx['base']/'compiler-older',legacy,'compiler-older-original',lock)
    assert old_job['state']=='succeeded'
    failed,request=submit(ctx,ctx['base']/'compiler-newer-original',modern,'compiler-newer-original',lock)
    assert failed['state']=='failed' and failed['result']['termination_confirmed'] and failed['result']['execution_cleanup_confirmed']
    report=artifact(ctx,failed['id'],'results/compiler-case.json');assert report['outcome']['exit_code']==1
    assert report['tool']['sha256']==newer['sha256'] and '22.' in report['tool']['version']
    assert 'unregistered operation' in ctx['api']('/api/jobs/'+failed['id']+'/log?stream=stderr')[1]['text']
    assert ctx['api']('/api/jobs/'+failed['id'],extra_headers=ctx['peer_headers'])[0]==404
    profile=ctx['base']/'compiler-model-profile.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':ctx['token']})
    client=Client.from_profile(profile);case=read_case(client,failed['id']);assert case['source_ir']==original and not case['source_redacted']
    case_file=ctx['base']/'compiler-case.json';atomic_json(case_file,case)
    # A separate actual personal UID uses the separately installed stdlib client.
    home=ctx['operator_proof']['personal_home'];public='/opt/ascend-lab-operator-client'
    cli=ctx['docker'](['exec','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',public+'/labctl','--profile',home+'/client.json','compiler-case',failed['id']])
    assert json.loads(cli.stdout)['compiler_report_sha256']==case['compiler_report_sha256']
    messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_job','arguments':{}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'path':'/etc/passwd'}}}]
    result=ctx['docker'](['exec','-i','--user','23642:23642',ctx['controller'],'/usr/bin/python3','-B',public+'/compiler_diagnostic.py',
        'mcp','--profile',home+'/client.json','--job-id',failed['id']],input=''.join(json.dumps(v)+'\n' for v in messages))
    values=[json.loads(line)['result'] for line in result.stdout.splitlines()]
    assert len(values[0]['tools'])==1 and values[0]['tools'][0]['annotations']['readOnlyHint']
    assert not values[1]['isError'] and values[2]['isError'] and values[3]['isError']
    model_directory=reference_path.parent/'agent-draft';model_record=model_directory/'validation.json'
    if not model_record.exists():
        assert not (model_directory/'current.json').exists(),'Observe the existing compiler model handle; no retry'
        result=subprocess.run([sys.executable,str(ctx['ROOT']/'tests/live_compiler_agent_check.py'),'--case',str(case_file),
            '--reference',str(reference_path),'--profile',str(profile),'--output-dir',str(model_directory)],capture_output=True,text=True,timeout=300)
        assert result.returncode==0,'Compiler draft failed; inspect the saved model handle and private logs'
    model=json.loads(model_record.read_text());assert model['passed'] and model['source_ir_sha256']==case['source_ir_sha256']
    assert model['target_tool_sha256']==newer['sha256'] and model['reference_sha256']==hashlib.sha256(reference_path.read_bytes()).hexdigest()
    review=review_minimal_rename(case,model['draft'],approved)
    atomic_json(ctx['base']/'compiler-primary-review.json',{'approved_for_single_cpu_experiment':True,'case_sha256':case['case_sha256'],
        'model_source_job_id':model['source_job_id'],'current_job_id':failed['id'],'source_sha256':case['source_ir_sha256'],
        'review':review,'migration_reference':reference['migration_reference']})
    changed={**modern,'input':review['candidate_ir']}
    corrected,correct_request=submit(ctx,ctx['base']/'compiler-newer-candidate',changed,'compiler-newer-candidate',lock)
    assert corrected['state']=='succeeded'
    output=ctx['api']('/api/jobs/'+corrected['id']+'/artifact?name=ir%2Fverified.mlir',raw=True)[1]
    assert b'bufferization.to_buffer' in output and b'bufferization.to_memref' not in output
    metadata=artifact(ctx,corrected['id'],'environment.json');receipt=artifact(ctx,corrected['id'],'results/_lab/environment.json')
    assert metadata['task_uid']==23003 and metadata['device_nodes']==[] and metadata['network_mode']=='none'
    assert receipt['locked'] and receipt['workload_exit_code']==0
    counter,_=submit(ctx,ctx['base']/'compiler-older-candidate',{**legacy,'input':review['candidate_ir']},'compiler-older-candidate',lock)
    assert counter['state']=='failed'
    assert client.request('/api/jobs/'+failed['id'])['job']['state']=='failed' and len(client.request('/api/jobs/'+failed['id'])['attempts'])==1
    assert read_case(client,failed['id'])['source_ir_sha256']==case['source_ir_sha256']
    return {'passed':True,'older_original_job':old_job['id'],'newer_original_failed_job':failed['id'],
        'newer_candidate_job':corrected['id'],'older_candidate_failed_job':counter['id'],'request':correct_request,
        'original_source_sha256':case['source_ir_sha256'],'candidate_source_sha256':review['candidate_source_sha256'],
        'source_ir':original,'candidate_ir':review['candidate_ir'],'output_ir_sha256':hashlib.sha256(output).hexdigest(),
        'target_tool_sha256':newer['sha256'],'reference_tool_sha256':older['sha256'],
        'model_run_id':model['run_id'],'configured_label':model['configured_label'],'model_invocations_for_phase':1,
        'model_material_source_job_id':model['source_job_id'],'model_tools':len(model['tool_events']),
        'personal_uid':23642,'task_uid':23003,'actual_stdio_read_only':True,'original_failure_and_input_preserved':True,
        'primary_approved_one_literal_experiment':True,'all_four_jobs_single_attempt':True,'npu_kernel_launches':0,
        'numeric_correctness_proven':False,'full_pipeline_proven':False,
        'scope':'Controlled minimal real IR parse/verify incompatibility, one known-evidence model draft and primary-approved queued candidate. Not an original large-kernel repair, general auto-diagnosis, numerical/device or full lowering acceptance.'}


def after_restore(ctx,proof):
    from lab_client import Client
    # The original API token remains scoped to this own fixture user.
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    case=read_case(client,proof['newer_original_failed_job']);assert case['source_ir_sha256']==proof['original_source_sha256']
    request=copy.deepcopy(proof['request']);request['idempotency_key']='compiler-candidate-after-restore'
    status,response=ctx['api']('/api/jobs',request);assert status==201
    job=ctx['wait'](response['job']['id'],seconds=90);assert job['state']=='succeeded'
    assert len(ctx['api']('/api/jobs/'+job['id'])[1]['attempts'])==1
    return {'passed':True,'job_id':job['id'],'original_failure_archive_readable':True,'candidate_input_reused':True,'attempts':1}

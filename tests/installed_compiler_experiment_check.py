"""Actual model/tool/queue loop on an installed UID-separated CPU Node."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from compiler_diagnostic import read_case
from compiler_experiment import Experiment,initialize
from environment_contract import lock_for
from installed_operator_check import PERSONAL_CLIENT
from lab_client import Client,ClientHTTPError,atomic_json

SOURCE='module {\n  func.func @value() -> i64 {\n    %c = arith.constant 42 : i32\n    return %c : i32\n  }\n}\n'


def write_personal(ctx,files):
    home=ctx['operator_proof']['personal_home']
    code='import json,os,sys;from pathlib import Path;root=Path(sys.argv[1]);files=json.loads(sys.stdin.read());[(lambda p,s:(p.write_text(s),p.chmod(0o600),os.chown(p,23642,23642)))(root/n,s) for n,s in files.items()]'
    ctx['docker'](['exec','-i',ctx['controller'],'/usr/bin/python3','-I','-c',code,home],input=json.dumps(files))


def personal(ctx,action,extra=(),messages=None):
    home=ctx['operator_proof']['personal_home'];command=['exec',*(['-i'] if messages else []),'--user','23642:23642',ctx['controller'],
        '/usr/bin/python3','-B',PERSONAL_CLIENT+'/compiler_experiment.py',action,'--session-dir',home+'/experiment-session',*extra]
    result=ctx['docker'](command,input=''.join(json.dumps(v)+'\n' for v in messages) if messages else None,timeout=90)
    return [json.loads(v) for v in result.stdout.splitlines()]


def run(ctx):
    base=ctx['base'];reference=json.loads(Path(ctx['options'].agent_experiment).read_text())
    assert reference['image']==ctx['task_image']
    tool=reference['tools']['triton'];lock=lock_for(ctx['configuration']['environment_manifest'])
    assert lock['files'][tool['path']]==tool['sha256']
    spec={'name':'One finite compiler candidate investigation','idempotency_key':'compiler-experiment-budget',
        'limits':{'jobs':3,'cpu_seconds':180,'npu_seconds':0},'templates':['ir_v1'],'lifetime_seconds':900}
    status,value=ctx['api']('/api/experiment-budgets',spec);assert status==201,value
    budget=value['budget']['id'];status,value=ctx['api']('/api/experiment-budgets/'+budget+'/token',{});assert status==200
    profile=base/'compiler-experiment-profile.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':value['token']})
    client=Client.from_profile(profile)
    source=base/'experiment-original';source.mkdir(mode=0o700)
    (source/'input.mlir').write_text(SOURCE)
    (source/'compiler_compatibility.py').write_bytes((ctx['ROOT']/'examples/workflows/compiler_compatibility.py').read_bytes())
    atomic_json(source/'compiler-case.json',{'schema':'ascend-lab-compiler-input/v1','tool':'triton-mlir-opt','path':tool['path'],'sha256':tool['sha256']})
    identifier=client.upload_input(source)['id']
    request={'name':'Original actual type-mismatch IR','template':'ir_v1','environment':'worker-python','idempotency_key':'experiment-original-failure',
        'parameters':{'input_id':identifier,'argv':['python','-I','-B','compiler_compatibility.py'],'lock':copy.deepcopy(lock),
        'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':60,'input_mode':'readonly',
        'workspace':{'min_mib':8,'min_inodes':128,'durable':True},'artifacts':{'file_mib':1,'total_mib':2}}}
    original=client.request('/api/jobs',request)['job']['id'];atomic_json(base/'experiment-original-job.json',{'id':original,'budget_id':budget})
    failed=ctx['wait'](original,seconds=90);assert failed['state']=='failed'
    case=read_case(client,original);assert case['source_ir']==SOURCE and case['compiler_report']['outcome']['exit_code']==1
    assert 'return' in case['compiler_stderr'] and 'i64' in case['compiler_stderr'] and 'i32' in case['compiler_stderr']
    session=base/'compiler-experiment-session';initialize(profile,original,session)
    # Only this tiny task/error is disclosed to the selected model. Commands,
    # runtime, budget, source tool and protected environment stay fixed.
    model_directory=Path(ctx['options'].agent_experiment).absolute().parent/'agent-loop'
    process=subprocess.run([sys.executable,str(ctx['ROOT']/'tests/live_compiler_experiment_agent_check.py'),
        '--profile',str(profile),'--session-dir',str(session),'--output-dir',str(model_directory)],capture_output=True,text=True,timeout=420)
    (base/'experiment-model-helper.log').write_text(process.stdout+'\n'+process.stderr)
    assert process.returncode==0,'Inspect original model handle/session/job; do not repeat an uncertain invocation'
    model=json.loads((model_directory/'validation.json').read_text());assert model['passed'] and model['model_invocations']==1
    bridge=Experiment(session);saved=json.loads((session/'state.json').read_text());entry=saved['candidates']['first'];candidate=entry['job_id']
    done=ctx['wait'](candidate,seconds=90);assert done['state']=='succeeded'
    result=bridge.call('get_candidate_result',{'candidate_key':'first'});assert result['parse_and_verify_passed']
    # Primary-authored acceptance of this particular fixture, independent of
    # model prose or a prefilled proposed edit. No numerical equivalence claim.
    assert result['candidate_ir']==SOURCE.replace('-> i64','-> i32',1),entry['proposal']
    assert result['semantic_equivalence_proven'] is False and result['numeric_correctness_proven'] is False and result['npu_qualified'] is False
    assert result['compiler_report']['tool']['sha256']==tool['sha256']
    for job,state in [(original,'failed'),(candidate,'succeeded')]:
        detail=client.request('/api/jobs/'+job);assert detail['job']['state']==state and len(detail['attempts'])==1
        assert detail['job']['experiment_id']==budget and detail['job']['result']['workspace_release_confirmed']
        metadata=ctx['api']('/api/jobs/'+job+'/artifact?name=environment.json')[1]
        receipt=ctx['api']('/api/jobs/'+job+'/artifact?name=results%2F_lab%2Fenvironment.json')[1]
        assert metadata['task_uid']==23003 and metadata['device_nodes']==[] and metadata['network_mode']=='none' and metadata['read_only_root']
        assert not metadata['privileged'] and metadata['memory_limit']==512*1024**2 and receipt['locked']
        assert ctx['api']('/api/jobs/'+job,extra_headers=ctx['peer_headers'])[0]==404
    assert read_case(client,original)['source_ir']==SOURCE
    replay=bridge.call('submit_candidate',{'candidate_key':'first'});assert replay['reused'] and replay['job_id']==candidate
    charged=client.request('/api/experiment-budgets/'+budget)['budget']['charged'];assert charged=={'jobs':2,'cpu_seconds':120,'npu_seconds':0}
    for path,body in [('/api/experiment-budgets',spec),('/api/experiment-budgets/'+budget+'/token',{})]:
        try:client.request(path,body)
        except ClientHTTPError as error:assert error.status==403
        else:raise AssertionError('Spending credential obtained approval authority')
    try:client.request('/api/jobs/'+ctx['operator_proof']['first_job'])
    except ClientHTTPError as error:assert error.status in {403,404}
    else:raise AssertionError('Experiment credential read unrelated report')
    # Independently installed portable code runs as another real numeric UID.
    # It reads exactly the same candidate; no second model or compute run.
    write_personal(ctx,{'experiment-profile.json':profile.read_text()})
    home=ctx['operator_proof']['personal_home']
    initialized=personal(ctx,'init',['--profile',home+'/experiment-profile.json','--source-job',original])[0];assert initialized['session_ready']
    args=copy.deepcopy(entry['proposal']);args['candidate_key']='first'
    calls=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'prepare_candidate','arguments':args}},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_candidate','arguments':{'candidate_key':'first'}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'get_candidate_result','arguments':{'candidate_key':'first'}}},
        {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'submit_candidate','arguments':{'candidate_key':'first','argv':['sh'],'npu':1}}}]
    responses=personal(ctx,'mcp',messages=calls);assert len(responses[0]['result']['tools'])==7
    assert not any(responses[i]['result']['isError'] for i in [1,2,3]) and responses[4]['result']['isError']
    reused=json.loads(responses[2]['result']['content'][0]['text']);assert reused['reused'] and reused['job_id']==candidate
    assert client.request('/api/experiment-budgets/'+budget)['budget']['charged']==charged
    ctx['check']('Actual bounded model uses fixed MCP to diagnose one small IR type error, seal a candidate, queue and inspect a real UID-separated CPU compiler task; portable second UID replays the same job without extra charge')
    return {'passed':True,'source_job':original,'candidate_job':candidate,'budget_id':budget,'charged':charged,
        'source_sha256':case['source_ir_sha256'],'candidate_sha256':entry['candidate_sha256'],'tool_sha256':tool['sha256'],
        'proposal':entry['proposal'],'private_client':str(profile),'session_dir':str(session),'model':{
            'run_id':model['run_id'],'configured_label':model['configured_label'],'tools':[e['tool'] for e in model['events']],
            'only_fixed_mcp_allowed':True,'invocations':1},'personal_uid':23642,'task_uid':23003,
        'original_failure_and_single_attempts_preserved':True,'installed_stdio_reuses_same_candidate':True,
        'unknown_execution_resubmission_proven':False,'npu_kernel_launches':0,'numeric_correctness_proven':False,
        'scope':'Actual model/tools/queue/archive loop for one authored small MLIR type error, CPU parse/verify only. Not general repair, a compiler source patch, full lowering, numerical equivalence, NPU or multi-host acceptance.'}


def after_restore(ctx,proof):
    old=Client.from_profile(proof['private_client'])
    try:old.request('/api/me')
    except ClientHTTPError as error:assert error.status==401
    else:raise AssertionError('Restore revived the spending token')
    budget=ctx['api']('/api/experiment-budgets/'+proof['budget_id'])[1]['budget']
    assert budget['state']=='restored_frozen' and budget['charged']==proof['charged']
    owner=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    for job,state,digest in [(proof['source_job'],'failed',proof['source_sha256']),(proof['candidate_job'],'succeeded',proof['candidate_sha256'])]:
        detail=owner.request('/api/jobs/'+job);assert detail['job']['state']==state and len(detail['attempts'])==1
        assert read_case(owner,job)['source_ir_sha256']==digest
    readonly=owner.request('/api/experiment-budgets/'+proof['budget_id']+'/token',{'read_only':True})
    private={'controller':ctx['controller_url'],'token':readonly['token']};atomic_json(proof['private_client'],private)
    reader=Experiment(proof['session_dir']);result=reader.call('get_candidate_result',{'candidate_key':'first'});assert result['parse_and_verify_passed']
    try:reader.call('submit_candidate',{'candidate_key':'first'})
    except ClientHTTPError as error:assert error.status==403
    else:raise AssertionError('Read-only profile gained submit authority')
    write_personal(ctx,{'experiment-profile.json':json.dumps(private)})
    messages=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_candidate_result','arguments':{'candidate_key':'first'}}},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_candidate','arguments':{'candidate_key':'first'}}}]
    values=personal(ctx,'mcp',messages=messages);assert not values[0]['result']['isError'] and values[1]['result']['isError']
    return {'passed':True,'old_spending_token_revoked':True,'budget_frozen_and_charges_preserved':True,
        'original_two_single_attempt_archives_preserved':True,'new_readonly_profile_same_session_reads_result':True,
        'installed_second_uid_readonly_verified':True,'new_compute_jobs':0,'additional_model_invocations':0}

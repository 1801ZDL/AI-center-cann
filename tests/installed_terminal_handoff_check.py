"""Real failed precision evidence, bounded CPU timeout and frozen handoff."""
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import uuid

from accuracy_data import DRIVER,INPUT,sha
from accuracy_diagnostic import read_case
from environment_contract import lock_for
from installed_operator_check import upload
from lab_client import Client,ClientHTTPError,atomic_json


def run(ctx):
    base=ctx['base'];reference=json.loads(Path(ctx['options'].terminal_handoff).read_text())
    source=Path(next(d['path'] for d in reference['datasets'] if d['id']=='legacy-pre'));assert source.is_relative_to(ctx['ROOT']/'runtime')
    lock=lock_for(ctx['configuration']['environment_manifest']);spec={'name':'Failed artifacts and bounded timeout investigation',
        'idempotency_key':'terminal-handoff-budget','limits':{'jobs':5,'cpu_seconds':255,'npu_seconds':0},'templates':['build_v1'],'lifetime_seconds':900}
    status,value=ctx['api']('/api/experiment-budgets',spec);assert status==201,value
    budget=value['budget']['id'];status,value=ctx['api']('/api/experiment-budgets/'+budget+'/token',{});assert status==200,value
    profile=base/'terminal-budget-client.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':value['token']});client=Client.from_profile(profile)
    assert 'terminal-artifact-handoff-v1' in client.request('/api/me')['dependency_capabilities']
    installed=base/'terminal-client';subprocess.run([str(ctx['ROOT']/'install-client'),'--prefix',str(installed)],check=True,capture_output=True,text=True,timeout=30)
    tasks=[];requests={};sessions={};timed_marker='controlled-'+uuid.uuid4().hex
    def submit(label,input_id,argv,*,parents=None,imports=None,timeout=60):
        request={'name':'Terminal artifact '+label,'template':'build_v1','environment':'worker-python','parameters':{
            'input_id':input_id,'argv':argv,'lock':copy.deepcopy(lock),'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},
            'timeout_seconds':timeout,'input_mode':'readonly','workspace':{'min_mib':8,'min_inodes':128,'durable':True},'artifacts':{'file_mib':8,'total_mib':32}}}
        if parents:request.update(depends_on=parents,artifact_inputs=imports)
        path=base/('terminal-'+label+'-request.json');atomic_json(path,request);requests[label]=request
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',str(profile),'--session-dir',str(base/('terminal-session-'+label)),
            '--environment','worker-python','--workflow',str(path)]
        calls=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'queue_status','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':'build_v1','request_key':'original'}}}]
        def conversation():
            reply=subprocess.run(command,input=''.join(json.dumps(v)+'\n' for v in calls),capture_output=True,text=True,timeout=60)
            assert reply.returncode==0,'Inspect the original private MCP handle; never replace an uncertain job'
            values={v['id']:v['result'] for v in map(json.loads,reply.stdout.splitlines())}
            policy=json.loads(values[2]['content'][0]['text'])['session_policy']
            assert policy['artifact_inputs']==request.get('artifact_inputs',[])
            assert not values[3].get('isError'),values[3]
            return json.loads(values[3]['content'][0]['text'])
        first=conversation();identifier=first['job']['id'];sessions[label]=conversation
        tasks.append({'label':label,'job_id':identifier,'input_id':input_id})
        atomic_json(base/'terminal-tasks-in-progress.json',{'budget_id':budget,'tasks':tasks})
        return identifier
    def finish(identifier,expected):
        job=ctx['wait'](identifier,seconds=100);assert job['state']==expected,job
        detail=client.request('/api/jobs/'+identifier)
        item=next(t for t in tasks if t['job_id']==identifier)
        item.update(state=expected,attempts=len(detail['attempts']),attempt_id=job.get('attempt_id'))
        if expected=='cancelled' and job.get('result',{}).get('failure_stage')=='dependency':assert not detail['attempts']
        else:
            assert len(detail['attempts'])==1
            assert all(job['result'][k] is True for k in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed'])
            env=ctx['api']('/api/jobs/'+identifier+'/artifact?name=environment.json')[1]
            assert env['task_uid']==23003 and env['device_nodes']==[] and env['network_mode']=='none' and env['workspace']['user_quota']
        replay=sessions[item['label']]();assert replay['reused'] and replay['job']['id']==identifier
        return job
    producer_source=base/'terminal-producer';shutil.copytree(source,producer_source);(producer_source/DRIVER).write_bytes((ctx['ROOT']/DRIVER).read_bytes())
    producer_input=upload(ctx,producer_source)
    consumer_source=base/'terminal-consumer';consumer_source.mkdir(mode=0o700);(consumer_source/DRIVER).write_bytes((ctx['ROOT']/DRIVER).read_bytes())
    # This explicitly synthetic 6MiB file exercises durable multi-tick transfer;
    # historical numerical tensors and reference bytes remain unchanged.
    transport=b't'*(6*1024**2);(consumer_source/'zz-transfer-check.bin').write_bytes(transport);consumer_input=upload(ctx,consumer_source)
    assert not [j for j in ctx['api']('/api/jobs')[1]['jobs'] if j['state'] not in {'succeeded','failed','cancelled','timed_out'}]
    ctx['ctl'](ctx['node'],'node','stop')
    parent=submit('precision-parent',producer_input,['python','-I','-B',DRIVER])
    names=[INPUT,'actual.bin','reference.bin','source_context.bin']
    imports=[{'job_id':parent,'name':'results/accuracy-input/'+n,'target':n} for n in names]
    ordinary=submit('success-only',consumer_input,['python','-I','-B',DRIVER],parents=[parent],imports=imports)
    terminal=submit('precision-diagnostic',consumer_input,['python','-I','-B',DRIVER],parents=[parent],imports=[{**v,'when':'terminal'} for v in imports])
    for identifier in [ordinary,terminal]:
        detail=client.request('/api/jobs/'+identifier);assert detail['job']['state']=='queued' and not detail['attempts'] and not detail['job']['allocation']
    ctx['ctl'](ctx['node'],'node','start')
    end=time.monotonic()+100;partial=None
    while time.monotonic()<end:
        detail=client.request('/api/jobs/'+terminal);stage=detail['job'].get('dependency_state',{})
        if stage.get('status')=='copying' and 0<stage.get('copied_bytes',0)<stage['total_bytes']:
            partial=stage;assert not detail['attempts'] and not detail['job']['allocation'];break
        assert detail['job']['state']=='queued',detail['job'];time.sleep(.1)
    assert partial,'Inspect original handoff; no replacement submission'
    assert ctx['api']('/api/storage/plan',{'kind':'archive','id':parent,'action':'trash'},extra_headers=ctx['owner_headers'])[0]==409
    atomic_json(base/'terminal-partial.json',partial)
    ctx['ctl'](ctx['controller'],'controller','stop')
    inspect='import json,sqlite3,sys;db=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True);v=json.loads(db.execute("SELECT data FROM jobs WHERE id=?",(sys.argv[2],)).fetchone()[0]);print(json.dumps({k:v.get(k) for k in ["state","attempt_id","allocation","dependency_state"]}))'
    stopped=json.loads(ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-I','-c',inspect,str(ctx['controller_state']/'jobs.sqlite3'),terminal]).stdout)
    assert stopped['state']=='queued' and stopped['attempt_id'] is None and not stopped['allocation']
    partial=stopped['dependency_state'];assert partial['status']=='copying' and 0<partial['copied_bytes']<partial['total_bytes']
    atomic_json(base/'terminal-stopped-partial.json',stopped)
    assert ctx['ctl'](ctx['controller'],'controller','start')['ready']
    parent_job=finish(parent,'failed');assert read_case(client,parent)['comparison']['mismatches']==11
    finish(ordinary,'cancelled');diagnostic=finish(terminal,'failed')
    assert diagnostic['input']['id']==partial['input_id'] and diagnostic['input']['id']!=consumer_input
    lineage=ctx['api']('/api/jobs/'+terminal+'/artifact?name=dependency-inputs.json')[1]
    assert lineage['terminal_sources'][parent]['state']=='failed' and lineage['terminal_sources'][parent]['attempt_id']==parent_job['attempt_id']
    assert lineage['terminal_sources'][parent]['result']['execution_cleanup_confirmed']
    case=read_case(client,terminal);assert case['comparison']['mismatches']==11 and case['tensor_sources']['actual']['sha256']==read_case(client,parent)['tensor_sources']['actual']['sha256']
    atomic_json(base/'terminal-precision-case.json',case)
    # Same app owner is required even when the source failure and artifact name
    # are known; a different user's sealed base cannot import this private data.
    peer_input=upload(ctx,consumer_source,ctx['peer_headers']);foreign=copy.deepcopy(requests['precision-diagnostic']);foreign['parameters']['input_id']=peer_input
    assert ctx['api']('/api/jobs',foreign,extra_headers=ctx['peer_headers'])[0]==404
    wait_source=base/'terminal-wait-source';wait_source.mkdir(mode=0o700)
    marker={'schema':'controlled-cpu-wait/v1','marker':timed_marker,'scope':'Synthetic bounded CPU wait; not NPU deadlock evidence'}
    (wait_source/'controlled_wait.py').write_text("import json,os,time\nfrom pathlib import Path\np=Path(os.environ['LAB_JOB_DIR'])/'results/hang-marker.json';p.write_text("+repr(json.dumps(marker))+ ")\nprint('Controlled CPU wait entered',flush=True)\nwhile True:time.sleep(1)\n")
    wait_input=upload(ctx,wait_source);waiting=submit('controlled-timeout',wait_input,['python','-I','-B','controlled_wait.py'],timeout=15)
    inspect_source=base/'terminal-timeout-consumer';inspect_source.mkdir(mode=0o700)
    script="import json,os\nfrom pathlib import Path\nv=json.loads(Path('marker.json').read_text());assert v=="+repr(marker)+"\np=Path(os.environ['LAB_JOB_DIR'])/'results/timeout-diagnosis.json';p.write_text(json.dumps({'purpose':'archived_timeout_marker_check','marker':v,'npu_fault_proven':False}))\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,'artifacts':['results/timeout-diagnosis.json']}))\n"
    (inspect_source/'inspect_timeout.py').write_text(script);inspect_input=upload(ctx,inspect_source)
    inspector=submit('timeout-diagnostic',inspect_input,['python','-I','-B','inspect_timeout.py'],parents=[waiting],imports=[{
        'job_id':waiting,'name':'results/hang-marker.json','target':'marker.json','when':'terminal'}])
    timeout_job=finish(waiting,'timed_out');finish(inspector,'succeeded')
    evidence=ctx['api']('/api/jobs/'+inspector+'/artifact?name=dependency-inputs.json')[1]
    assert evidence['terminal_sources'][waiting]['state']=='timed_out' and evidence['terminal_sources'][waiting]['attempt_id']==timeout_job['attempt_id']
    saved=ctx['api']('/api/jobs/'+inspector+'/artifact?name=results%2Ftimeout-diagnosis.json')[1];assert saved['marker']==marker and saved['npu_fault_proven'] is False
    assert client.request('/api/experiment-budgets/'+budget)['budget']['charged']=={'jobs':5,'cpu_seconds':255,'npu_seconds':0}
    # A genuine prior release must not reinterpret retained terminal edges.
    old=base/'terminal-old-source';old.mkdir(mode=0o700);packed=subprocess.check_output(['git','archive','21bde7f624717d780a136927f212c050d0d542c0'],cwd=ctx['ROOT'])
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:archive.extractall(old,filter='data')
    package=base/'terminal-old-server.tar.gz';code='import sys;sys.path.insert(0,sys.argv[1]);from deployment import bundle;bundle(sys.argv[2],sys.argv[1])'
    subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(old),str(package)],check=True,capture_output=True,text=True,timeout=45)
    ctx['docker'](['cp',str(package),ctx['controller']+':/provision/terminal-old-server.tar.gz'])
    return {'passed':True,'tasks':tasks,'budget_id':budget,'private_client':str(profile),'charged':{'jobs':5,'cpu_seconds':255,'npu_seconds':0},
        'precision_parent':parent,'precision_diagnostic':terminal,'ordinary_cancelled':ordinary,'timeout_parent':waiting,'timeout_diagnostic':inspector,
        'partial_input_id':partial['input_id'],'partial_copied_bytes':partial['copied_bytes'],'partial_total_bytes':partial['total_bytes'],
        'controller_restart_resumed_original_handoff':True,'failed_source_archive_pinned':True,'same_historical_actual_bytes':True,
        'original_precision_mismatches':11,'synthetic_transport_bytes':len(transport),'synthetic_transport_sha256':sha(transport),
        'controlled_cpu_wait_entered_and_timed_out':True,'timeout_marker':timed_marker,'foreign_owner_import_denied':True,
        'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Installed UID-separated CPU tests with historical precision bytes and an explicitly synthetic 6MiB transfer fixture plus a controlled15-second CPU wait. Actual failed/timed-out artifact handoff, not new NPU accuracy, NPU deadlock, PID1 or physical multi-host acceptance.'}


def after_restore(ctx,proof):
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    for task in proof['tasks']:
        detail=client.request('/api/jobs/'+task['job_id']);assert detail['job']['state']==task['state'] and detail['job'].get('attempt_id')==task['attempt_id'] and len(detail['attempts'])==task['attempts']
    case=read_case(client,proof['precision_diagnostic']);assert case['comparison']['mismatches']==11
    assert client.request('/api/experiment-budgets/'+proof['budget_id'])['budget']['charged']==proof['charged']
    old=Client.from_profile(proof['private_client'])
    try:old.request('/api/me')
    except ClientHTTPError as error:assert error.status==401
    else:raise AssertionError('Old spending credential survived restore')
    ctx['ctl'](ctx['node'],'node','stop');ctx['ctl'](ctx['controller'],'controller','stop')
    rejected=ctx['ctl'](ctx['controller'],'controller','activate','--archive','/provision/terminal-old-server.tar.gz',check=False)
    assert rejected.returncode!=0 and 'Terminal artifact conditions' in rejected.stderr
    assert ctx['ctl'](ctx['controller'],'controller','start')['ready'] and ctx['ctl'](ctx['node'],'node','start')['ready']
    return {'passed':True,'original_attempts_and_failures_preserved':True,'old_spending_token_revoked':True,
        'genuine_old_controller_downgrade_refused':True,'new_comparisons_or_waits':0}

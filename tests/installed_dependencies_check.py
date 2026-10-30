"""Real installed phase handoff/restart/failure proof, without device execution."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import time


def run(ctx):
    api=ctx['api'];ctl=ctx['ctl'];controller=ctx['controller'];wait=ctx['wait']
    ctl(controller,'controller','admin','--','accessctl','quota','proof_user','--npu','1')
    producer=b'''import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);out=root/'results/compiled.mlir'
subprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)
data=root/'results/data.bin'
with data.open('wb') as f:
 for _ in range(128):f.write(b'd'*65536)
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,'artifacts':['results/compiled.mlir','results/data.bin'],'data_sha256':hashlib.sha256(data.read_bytes()).hexdigest()}))
time.sleep(8)
'''
    consumer=b'''import hashlib,json,os,shutil,subprocess
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);data=Path('deps/data.bin').read_bytes();assert data==b'd'*(8*1024**2)
source=Path('deps/compiled.mlir');assert 'arith.constant 42' in source.read_text()
out=root/'ir/canonical.mlir'
subprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse',str(source),'-o',str(out)],check=True)
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],'data_sha256':hashlib.sha256(data).hexdigest(),'inherited_ir_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}))
'''
    source={'produce.py':producer,'consume.py':consumer,'fail.py':b'import time\ntime.sleep(3)\nraise RuntimeError("controlled upstream build failure")\n',
            'input.mlir':(ctx['ROOT']/'examples/workflows/input.mlir').read_bytes()}
    status,value=api('/api/inputs',{'files':[{'name':n,'size':len(v),'sha256':hashlib.sha256(v).hexdigest()} for n,v in source.items()]});assert status==201,value
    base_id=value['input']['id']
    for name,data in source.items():assert api('/api/inputs/'+base_id+'/blob',{'name':name,'offset':0,'data':base64.b64encode(data).decode()})[0]==200
    assert api('/api/inputs/'+base_id+'/seal',{})[1]['input']['sealed']
    def request(template,argv,key,parents=None,imports=None):
        value=copy.deepcopy(ctx['request']);value.update(template=template,name=key,idempotency_key=key)
        value['parameters'].update(input_id=base_id,argv=argv,artifacts={'file_mib':8,'total_mib':16},timeout_seconds=60)
        if template=='accuracy_v1':
            value['parameters']['resources']={'cpu':1,'memory_mib':512,'shm_mib':0,'npu':1}
            value['parameters']['lock']['npu_name']='NPU-NOT-ENROLLED'
        if parents:value['depends_on']=parents
        if imports:value['artifact_inputs']=imports
        return value
    def submit(value):
        status,response=api('/api/jobs',value);assert status==201,response
        return response['job']['id']
    producer_id=submit(request('build_v1',['python','produce.py'],'stage-actual-producer'))
    imports=[{'job_id':producer_id,'name':'results/data.bin','target':'deps/data.bin'},
             {'job_id':producer_id,'name':'results/compiled.mlir','target':'deps/compiled.mlir'}]
    consumer_request=request('ir_v1',['python','consume.py'],'stage-actual-consumer',[producer_id],imports)
    consumer_id=submit(consumer_request)
    device_id=submit(request('accuracy_v1',['python','consume.py'],'stage-device-unallocated',[producer_id],imports))
    deadline=time.monotonic()+20;waiting=None
    while time.monotonic()<deadline:
        parent=api('/api/jobs/'+producer_id)[1]['job'];waiting=api('/api/jobs/'+consumer_id)[1]
        if parent['state']=='running':break
        time.sleep(.1)
    assert parent['state']=='running' and parent['resources']['npu']==0 and parent['allocation']['npu_ids']==[]
    assert waiting['job']['state']=='queued' and not waiting['attempts'] and waiting['job']['allocation']=={}
    assert api('/api/jobs')[1]['reserved']['npu_ids']==[]
    deadline=time.monotonic()+40;partial=None
    while time.monotonic()<deadline:
        details=api('/api/jobs/'+consumer_id)[1];job=details['job'];stage=job.get('dependency_state',{})
        if stage.get('status')=='copying' and 0<stage.get('copied_bytes',0)<stage['total_bytes']:
            partial=stage;break
        assert job['state'] not in {'failed','cancelled','needs_attention'},job
        time.sleep(.1)
    assert partial and not details['attempts'] and job['allocation']=={}
    parent_now=api('/api/jobs/'+producer_id)[1]['job'];assert parent_now['state']=='succeeded'
    from lab_client import atomic_json
    atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/dependency-ui.json',{'source_parent':Path(os.environ['LAB_JOB_DIR']).name,
        'scope':'Archived actual partial phase handoff, not a live Main task','jobs':[parent_now,job]})
    ctl(controller,'controller','stop');assert ctl(controller,'controller','start')['ready']
    consumer_job=wait(consumer_id);assert consumer_job['state']=='succeeded',consumer_job
    assert len(api('/api/jobs/'+consumer_id)[1]['attempts'])==1
    provenance=api('/api/jobs/'+consumer_id+'/artifact?name=dependency-inputs.json')[1]
    actual=api('/api/jobs/'+consumer_id+'/artifact?name=results%2Fworkload-result.json')[1]
    sha=hashlib.sha256(b'd'*(8*1024**2)).hexdigest();assert actual['data_sha256']==sha
    data_receipt=next(v for v in provenance['artifacts'] if v['target']=='deps/data.bin')
    assert data_receipt['sha256']==sha and data_receipt['size']==8*1024**2
    assert data_receipt['job_id']==producer_id and data_receipt['attempt_id']==parent_now['attempt_id']
    assert consumer_job['input']['id']==partial['input_id']==provenance['input_id']
    assert consumer_job['input']['id']!=base_id and api('/api/inputs/'+consumer_job['input']['id'])[1]['sealed']
    status,replay=api('/api/jobs',consumer_request);assert status==200 and replay['reused'] and replay['job']['id']==consumer_id
    device=api('/api/jobs/'+device_id)[1];assert device['job']['state']=='queued' and not device['attempts'] and device['job']['allocation']=={}
    assert api('/api/jobs')[1]['reserved']['npu_ids']==[]
    status,denied=api('/api/storage/plan',{'kind':'archive','id':producer_id,'action':'trash'},extra_headers=ctx['owner_headers'])
    assert status==409 and 'downstream' in denied['error']
    joined_imports=[{'job_id':producer_id,'name':'results/data.bin','target':'deps/data.bin'},
                    {'job_id':consumer_id,'name':'ir/canonical.mlir','target':'deps/compiled.mlir'}]
    joined_id=submit(request('ir_v1',['python','consume.py'],'stage-fan-in',[producer_id,consumer_id],joined_imports))
    joined=wait(joined_id);assert joined['state']=='succeeded',joined
    assert len(joined['input_provenance']['parents'])==2
    api('/api/jobs/'+device_id+'/cancel',{});assert api('/api/jobs/'+device_id)[1]['job']['state']=='cancelled'
    failed_id=submit(request('build_v1',['python','fail.py'],'stage-controlled-failure'))
    descendant_id=submit(request('accuracy_v1',['python','consume.py'],'stage-unstarted-descendant',[failed_id]))
    failed=wait(failed_id);assert failed['state']=='failed'
    descendant=wait(descendant_id);assert descendant['state']=='cancelled' and descendant['result']['failure_stage']=='dependency'
    assert descendant['result']['execution_started'] is False and not api('/api/jobs/'+descendant_id)[1]['attempts']
    rows=api('/api/results?limit=200')[1]['rows'];row=next(r for r in rows if r['job_id']==descendant_id)
    assert row['command_executed'] is False and row['failure_stage']=='dependency' and not row['passed']
    csv=api('/api/results.csv',raw=True)[1];assert b'dependency_provenance_sha256' in csv and descendant_id.encode() in csv
    ctl(controller,'controller','admin','--','accessctl','quota','proof_user','--npu','0')
    return {'passed':True,'producer_job':producer_id,'consumer_job':consumer_id,'join_job':joined_id,
        'partial_input_id':partial['input_id'],'partial_copied_bytes':partial['copied_bytes'],'partial_total_bytes':partial['total_bytes'],
        'controller_restart_partial_handoff_resumed':True,'consumer_attempts':1,'data_bytes':8*1024**2,'data_sha256':sha,
        'producer_cpu_only':True,'device_stage_job':device_id,'device_stage_attempts':0,'device_stage_executed':False,
        'device_stage_no_allocation':True,'producer_archive_pinned':True,'fan_in_parent_count':2,
        'idempotent_child_reused':True,'failed_upstream_job':failed_id,'unstarted_descendant_job':descendant_id,
        'unstarted_result_recorded':True,'result_csv_has_lineage':True,
        'scope':'Actual CPU BishengIR producer/consumer and8MiB sealed transfer under installed quotas; device stage deliberately has no enrolled device and was cancelled without execution, not numerical/NPU acceptance'}

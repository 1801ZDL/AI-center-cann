"""Real frozen-input CPU build/BishengIR workflows and negative result/lock boundaries."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,ClientError,atomic_json,now,private_profile
from jobs import JobStore,read_json

os.umask(0o077)
base=ROOT/'runtime'/('workflow-acceptance-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
source=base/'source';shutil.copytree(ROOT/'examples/workflows',source)
(source/'data.bin').write_bytes(bytes(range(256))*128)
(source/'long_ir.py').write_text('''import json,os,subprocess
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);p=root/'ir'
for n in range(4):p=p/('old_triton_dump_'+str(n)+'_'+'x'*65)
p.mkdir(parents=True);target=p/'kernel.canonical.mlir'
subprocess.run(['bishengir-opt','--canonicalize','input.mlir','-o',str(target)],check=True)
assert 'arith.constant 42' in target.read_text()
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':[str(target.relative_to(root))]}))
''')
(source/'exit_zero.py').write_text('print("Intentional zero exit without a result assertion.",flush=True)\n')
(source/'link.py').write_text('from pathlib import Path\nPath("/work/results/leak").symlink_to("/receipt/environment.json")\nprint("Created a link without reading protected data.",flush=True)\n')
profile=base/'personal.json';original=private_profile(ROOT/'runtime/personas/demo_bob/client.json')
atomic_json(profile,{'controller':'http://127.0.0.1:'+str(original['port']),'token':original['token']})
client=Client.from_profile(profile);alice=Client.from_profile(ROOT/'runtime/personas/demo_alice/client.json')
installed=base/'client';subprocess.run([str(ROOT/'install-client'),'--prefix',str(installed)],capture_output=True,check=True)
cli=[str(installed/'labctl'),'--profile',str(profile)]
created=[];checks=[];quota_changed=False;initial_npu_quota=None
prefix='workflow-'+base.name[-8:]


def check(message):checks.append(message);print('PASS:',message,flush=True)


def wait(job_id,timeout=120):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state'] in {'succeeded','failed','cancelled','timed_out','needs_attention'}:return job
        time.sleep(.25)
    raise AssertionError('Workflow acceptance deadline exceeded: '+job_id)


def submit(label,template='ir_v1',argv=None,lock=None):
    body={'name':label,'template':template,'environment':'worker-python','parameters':{
        'input_id':bundle['id'],'argv':argv or ['python','ir.py'],'lock':lock or {'python':'3.10.12'},'timeout_seconds':90},
        'idempotency_key':prefix+'-'+str(len(created))}
    path=base/('request-'+str(len(created))+'.json');atomic_json(path,body)
    output=subprocess.run([*cli,'submit','--workflow',str(path)],capture_output=True,text=True,check=True)
    job=json.loads(output.stdout)['job'];created.append(job['id']);return job


def artifact(job_id,name):
    return client.request('/api/jobs/'+job_id+'/artifact?name='+urllib.parse.quote(name,safe=''),raw=True,max_bytes=16*1024**2)


try:
    active=client.request('/api/jobs')['counts']
    assert not any(active[k] for k in ['queued','reserved','running','cancelling','needs_attention'])
    added=subprocess.run([*cli,'input-add',str(source)],capture_output=True,text=True,check=True)
    bundle=json.loads(added.stdout)['input'];assert bundle['sealed']
    assert next(x for x in bundle['files'] if x['name']=='data.bin')['size']==32768
    try:alice.request('/api/inputs/'+bundle['id'])
    except ClientError as error:assert '404' in str(error)
    else:raise AssertionError('Cross-user input manifest was visible')
    check('Independent installed client uploads/seals multichunk input; other user receives 404')
    (source/'kernel.c').write_text('#error LOCAL SOURCE CHANGED AFTER SEAL\n')
    build=wait(submit('实际编译 · 固定输入',template='build_v1',argv=['python','build.py'])['id'])
    assert build['state']=='succeeded',build
    actual=json.loads(artifact(build['id'],'results/workload-result.json'))
    assert actual['compiled_symbol_result']==42 and actual['protected_receipt_unwritable']
    protected=json.loads(artifact(build['id'],'results/_lab/environment.json'))
    assert protected['locked'] and protected['workload_started'] and protected['workload_exit_code']==0
    check('Real C compilation and symbol call use frozen source; task cannot overwrite bootstrap receipt')
    lock_path=base/'environment-lock.json'
    subprocess.run([*cli,'environment-lock',build['id'],'--output',str(lock_path)],capture_output=True,check=True)
    full_lock=read_json(lock_path)
    assert full_lock['tools']['bishengir-opt'] and full_lock['packages']['triton']=='__absent__'
    ir=wait(submit('实际 BishengIR IR 处理',lock=full_lock)['id'])
    assert ir['state']=='succeeded',ir
    assert b'arith.constant 42' in artifact(ir['id'],'ir/canonical.mlir')
    check('Exact Python/image/package/tool/setup hashes match; real bishengir-opt folds constant to 42')
    long_job=wait(submit('长 IR 路径归档',argv=['python','long_ir.py'],lock=full_lock)['id'])
    assert long_job['state']=='succeeded',long_job
    mapping=json.loads(artifact(long_job['id'],'results/_lab/path-map.json'))['mappings'][0]
    assert len(mapping['original'])>300 and len(mapping['archived'])<200
    data=artifact(long_job['id'],mapping['archived']);assert b'arith.constant 42' in data
    hashes=json.loads(artifact(long_job['id'],'artifact-hashes.json'))['files']
    assert next(x for x in hashes if x['name']==mapping['archived'])['sha256']==hashlib.sha256(data).hexdigest()
    check('Long actual IR path gets short archive address, original-path mapping and matching content hash')
    wrong=json.loads(json.dumps(full_lock));wrong['tools']['bishengir-opt']='0'*64
    mismatch=wait(submit('环境锁拒绝验证',lock=wrong)['id']);assert mismatch['state']=='failed',mismatch
    rejected=json.loads(artifact(mismatch['id'],'results/_lab/environment.json'))
    assert rejected['locked'] is False and rejected['workload_started'] is False
    assert 'tools.bishengir-opt' in rejected['differences']
    check('Wrong compiler hash rejects before workload command; protected receipt retains exact difference')
    zero=wait(submit('=1+1',argv=['python','exit_zero.py'],lock=full_lock)['id']);assert zero['state']=='failed',zero
    summary=json.loads(artifact(zero['id'],'results/workflow-summary.json'))
    assert summary['command_executed'] and summary['exit_code']==0 and summary['passed'] is False
    check('Exit zero without structured result does not count as workflow success')
    attack=wait(submit('拒绝输出链接',argv=['python','link.py'],lock=full_lock)['id'])
    assert attack['state']=='failed' and attack['result']['failure_stage']=='artifact_collection',attack
    try:artifact(attack['id'],'results/leak')
    except ClientError:pass
    else:raise AssertionError('Protected receipt symlink was exported')
    check('Read-only exporter refuses task-created link to protected receipt')
    rows=client.request('/api/results')['rows'];ids={x['job_id'] for x in rows}
    assert {build['id'],ir['id'],long_job['id'],mismatch['id'],zero['id'],attack['id']}<=ids
    assert not {x['job_id'] for x in alice.request('/api/results')['rows']}&set(created)
    csv=client.request('/api/results.csv',raw=True).decode('utf-8-sig')
    assert "'=1+1" in csv
    assert not any(job_id in alice.request('/api/results.csv',raw=True).decode() for job_id in created)
    check('Result rows/CSV share job ACL; spreadsheet formulas are escaped')
    # NPU templates must wait for a device-capable worker, never run as CPU-only jobs.
    admin=JobStore(ROOT/'runtime',read_json(ROOT/'runtime/config.json'))
    initial_npu_quota=admin.access.user('demo_bob').quota['npu'];admin.access.set_quota('demo_bob',{'npu':1});quota_changed=True
    blocked=[]
    for template in ['accuracy_v1','performance_v1','simulation_v1']:
        job=submit('设备模板等待 · '+template,template=template,lock=full_lock)
        end=time.monotonic()+8
        while time.monotonic()<end:
            state=client.request('/api/jobs/'+job['id'])['job']
            if 'NPU' in state['reason']:break
            time.sleep(.2)
        assert state['state']=='queued' and not state['allocation'] and 'NPU' in state['reason'],state
        client.request('/api/jobs/'+job['id']+'/cancel',{});blocked.append(job['id'])
    check('Accuracy/performance/simulation reserve NPU explicitly and remain unallocated on CPU-only nodes')
    admin.access.set_quota('demo_bob',{'npu':initial_npu_quota});quota_changed=False
    report={'passed':True,'recorded_at':now(),'checks':checks,'job_ids':created,'build_job':build['id'],
            'ir_job':ir['id'],'long_path_job':long_job['id'],'lock_rejection_job':mismatch['id'],
            'exit_zero_failure_job':zero['id'],'link_rejection_job':attack['id'],'npu_blocked_jobs':blocked,
            'input_id':bundle['id'],'input_manifest_sha256':bundle['manifest_sha256'],
            'raw_ir_path_length':len(mapping['original']),'archived_ir_path':mapping['archived'],
            'npu_kernel_launches':0,'scope':'Real small CPU compilation and Ascend NPU-IR IR transform; device workflows are admission-only, not accuracy/performance/simulation acceptance'}
    atomic_json(ROOT/'evidence/workflow-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    if quota_changed:admin.access.set_quota('demo_bob',{'npu':initial_npu_quota})
    for job_id in created:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state'] in {'queued','reserved','running'}:client.request('/api/jobs/'+job_id+'/cancel',{})

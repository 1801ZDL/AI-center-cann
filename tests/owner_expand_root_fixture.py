"""Owned Root role: one existing personal credential, two independent Nodes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid


def run(session,old_config):
    import deployment as deploy
    import quota_workspace as native
    import workspace_quota_admin as slots
    import node_owner_admin as binding
    import node_owner_ack as ack
    from lab_client import read_json,atomic_json,ClientError
    from persistent_cache import Pool
    from worker import Worker
    prefix='/opt/expansion-node';state=Path(old_config['state_dir']).parent/'expansion-node'
    paths=[session.context[k] for k in ['expand_work_path','expand_cache_path','expand_spool_path']]
    for raw in paths:
        root=Path(raw);root.chmod(0o700)
        if (root/'lost+found').exists():(root/'lost+found').rmdir()
    limits=lambda uid:[{'uid':uid,'bytes':16*1024**2,'inodes':128}]
    work=native.prepare(paths[0],limits(23811))['policy'];cache=native.prepare(paths[1],limits(23811))['policy']
    info=Path(paths[2]).stat();space=os.statvfs(paths[2])
    config=copy.deepcopy(old_config)
    for field in ['owner_bindings','node_owner_revision']:config.pop(field,None)
    config.update(users={'old_owner':{'uid':23811,'gid':23811}},workspace=work,persistent_cache=cache,policy_revision=1,
        spool_storage={'path':paths[2],'filesystem':'ext4','device':info.st_dev,'root_inode':info.st_ino,
            'bytes':space.f_blocks*space.f_frsize,'inodes':space.f_files,'storage_id':uuid.uuid4().hex})
    profile={k:v for k,v in config.items() if k not in {'node_id','node_key','controller','docker_runtime','state_dir'}}
    profile['name']='expansion_node';source=session.state/'expansion-profile.json';atomic_json(source,profile);os.chown(source,23820,23820)
    created=session.service_code('''import json,sys
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/'config.json'));node,key=j.nodes.enroll(read_json(s/'expansion-profile.json'))
print(json.dumps({'node_id':node,'node_key':key}))
''')
    config.update(created);config['state_dir']=str(state)
    source=Path('/state/expansion-enroll.json');atomic_json(source,config)
    deploy.install('/payload/server.tar.gz',prefix,str(state),'node',node_config=str(source))
    config=read_json(state/'config.json');Pool(state,config).assert_idle()
    native_results=[slots.apply(prefix,slots.plan(prefix,path,limits(23812))['plan']) for path in paths[:2]]
    additions=[next(s for s in r['policy']['slots'] if s['quota_uid']==23812) for r in native_results]
    request={'owner':'new_owner','stage_id':session.stage_id,'uid':23812,'gid':23812,
        'workspace_slot':additions[0]['id'],'cache_slot':additions[1]['id']}
    private_plan=Path('/state/expansion-bind.json');value=binding.plan(prefix,request)['plan'];atomic_json(private_plan,value)
    central_plan=session.state/'expansion-bind.json';atomic_json(central_plan,value);os.chown(central_plan,23820,23820)
    refused=session.service_run('''import json,sys
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/'config.json'));j.nodes.handoff.approve("'''+created['node_id']+'''",read_json(s/'expansion-bind.json'))
''')
    assert refused.returncode and 'credential-free' in refused.stderr

    def credential_snapshot():
        return session.service_code('''import json,sys,hashlib
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
from node_owner_admin import sha
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/'config.json'))
with j.access.connect() as db:
 rows=db.execute("SELECT digest,scopes,revoked,expires_at FROM tokens WHERE user='new_owner' ORDER BY digest").fetchall()
print(json.dumps({'tokens':len(rows),'credential_rows_sha256':sha(rows),
 'source_client_sha256':hashlib.sha256((s/'personas/new_owner/client.json').read_bytes()).hexdigest(),
 'old_profile_sha256':sha(j.nodes.node("'''+session.node_id+'''")['profile'])}))
''')

    before=credential_snapshot();assert before['tokens']==1
    personal_before=session.personal_code('''import json,sys,hashlib
from pathlib import Path
print(json.dumps({'sha256':hashlib.sha256((Path(sys.argv[1])/'client.json').read_bytes()).hexdigest()}))
''')
    old_cache=Path(old_config['persistent_cache']['slots'][0]['path'])
    def cache_snapshot():
        return {str(p.relative_to(old_cache)):(p.stat().st_ino,hashlib.sha256(p.read_bytes()).hexdigest())
                for p in old_cache.rglob('*') if p.is_file() and not p.is_symlink()}
    cached_before=cache_snapshot();assert cached_before

    def authorize(index,reauthorize=False):
        target=session.state/('expansion-grant-'+str(index)+'.json')
        session.command('workerctl','owner-expand-plan',created['node_id'],'--plan',str(central_plan),'--output',str(target),
                        *(['--reauthorize'] if reauthorize else []))
        first=session.command('workerctl','owner-expand-apply','--plan',str(target))
        again=session.command('workerctl','owner-expand-apply','--plan',str(target))
        assert not first['reused'] and again['reused'] and first['approval']==again['approval']
        return target,first['grant_id']

    original,first_id=authorize(1);faults=[]
    for boundary in ['journal','cache_control','cache_marker','new_control','new_marker','config','metadata','receipt','handoff','published']:
        killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,json,os,signal
sys.path.insert(0,"/payload/code");import node_owner_admin as a
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.apply('/opt/expansion-node',json.load(open('/state/expansion-bind.json')))
''',boundary],capture_output=True,text=True,timeout=30)
        assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-1200:])
        faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_used':True})
    cli=['/usr/bin/python3','-I','-B',prefix+'/current/servicectl']
    subprocess.run([*cli,'node-owner-apply','--prefix',prefix,'--plan',str(private_plan)],capture_output=True,text=True,check=True,timeout=30)
    killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,os,signal
sys.path.insert(0,"/payload/code");import node_owner_ack as a
def crash(stage):
 if stage=='intent':os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.complete('/opt/expansion-node',sys.argv[1])
''',value['id']],capture_output=True,text=True,timeout=30)
    assert killed.returncode==-signal.SIGKILL,killed.stderr[-1200:]
    ack_faults=[{'boundary':'intent','sigkill_observed':True,'same_ack_replayed':True}]

    def change_restore_authority():
        session.service_code('''import json,sys
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/'config.json'));q=j.access.user('new_owner').quota['cpu']
j.access.set_quota('new_owner',{'cpu':q+1});j.access.set_quota('new_owner',{'cpu':q})
print(json.dumps({'restored':True}))
''')

    def confirm_stale_authority():
        verified=session.service_code('''import json,sys
from pathlib import Path
from jobs import JobStore,JobError
from lab_client import read_json
from owner_node_grant import Expansion
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/'config.json'))
with j.nodes.handoff.transaction() as (db,identities):
 r=j.nodes.handoff.get("'''+value['id']+'''",db)
 try:Expansion(j.nodes.handoff).validate(r,db,identities)
 except JobError as error:assert 'reauthorize' in str(error)
 else:raise AssertionError('Changed owner authority accepted')
print(json.dumps({'authority_refused':True}))
''')
        assert verified['authority_refused']

    change_restore_authority()
    confirm_stale_authority()
    failed=subprocess.run([*cli,'node-owner-ack','--prefix',prefix,'--plan-id',value['id']],capture_output=True,text=True,timeout=30)
    (session.state/'expansion-refused-ack.stderr').write_text(failed.stderr)
    assert failed.returncode and 'HTTP 409' in failed.stderr,(failed.returncode,failed.stderr[-1800:])
    assert (state/binding.AWAIT).exists()
    second,second_id=authorize(2,True);assert second_id!=first_id
    for boundary in ['commit_response','committed','config_ready','ready_response','ready_saved','receipt','awaiting_removed','published']:
        killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,os,signal
sys.path.insert(0,"/payload/code");import node_owner_ack as a
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.complete('/opt/expansion-node',sys.argv[2])
''',boundary,value['id']],capture_output=True,text=True,timeout=30)
        assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-1200:])
        ack_faults.append({'boundary':boundary,'sigkill_observed':True,'same_ack_replayed':True})
    subprocess.run([*cli,'node-owner-ack','--prefix',prefix,'--plan-id',value['id']],capture_output=True,text=True,check=True,timeout=30)
    assert not (state/binding.AWAIT).exists() and not (state/ack.PROGRESS).exists()
    config=read_json(state/'config.json');change_restore_authority();confirm_stale_authority()
    worker=Worker(config)
    try:
        try:worker.heartbeat()
        except RuntimeError as error:assert 'HTTP 409' in str(error)
        else:raise AssertionError('Stale authorization admitted first heartbeat')
    finally:worker.lock.close();worker.state_lock.close()
    third,third_id=authorize(3,True);assert third_id not in {first_id,second_id}
    # Both Node inventories belong to the live controller epoch before claim.
    for current in [old_config,config]:
        worker=Worker(current)
        try:worker.heartbeat()
        finally:worker.lock.close();worker.state_lock.close()
    assert session.command('workerctl','owner-status',created['node_id'])[0]['node_online_seen']
    personal=session.personal_code('''import json,sys,os
from pathlib import Path
from lab_client import Client,read_json
from environment_contract import lock_for
s=Path(sys.argv[1]);c=Client.from_profile(s/'client.json');assert c.request('/api/me')['id']=='new_owner'
source=s/'expansion-source';source.mkdir(mode=0o700)
(source/'input.mlir').write_text('module { func.func @add() -> i32 { %a = arith.constant 19 : i32\\n %b = arith.constant 23 : i32\\n %r = arith.addi %a, %b : i32\\n return %r : i32 } }\\n')
(source/'run.py').write_text("import os,json,subprocess,shutil\\nfrom pathlib import Path\\nassert os.getuid()==23812\\ns=Path('/proc/self/status').read_text();assert 'CapEff:\\t0000000000000000' in s and 'NoNewPrivs:\\t1' in s\\nr=Path(os.environ['LAB_JOB_DIR']);out=r/'ir/canonical.mlir'\\nsubprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)\\nassert 'arith.constant 42' in out.read_text()\\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],'uid':os.getuid(),'caps_zero':True}))\\n")
bundle=c.upload_input(source);request={'environment':'worker-python','template':'ir_v1','idempotency_key':'expanded-owner-ir-once',
'parameters':{'input_id':bundle['id'],'argv':['python','run.py'],'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},
'timeout_seconds':30,'lock':lock_for(read_json(s/'environment.json')),'workspace':{'min_mib':1,'min_inodes':16,'durable':True},'artifacts':{'file_mib':1,'total_mib':2}}}
first=c.request('/api/jobs',request);again=c.request('/api/jobs',request);assert first['job']['id']==again['job']['id'] and again['reused']
print(json.dumps({'job_id':first['job']['id'],'client_uid':os.getuid(),'replay_same_job':True}))
''')
    worker=Worker(config)
    try:
        worker.heartbeat();grant=worker.client.call('claim',{})['grant']
        assert grant and grant['job_id']==personal['job_id'] and grant['node_id']==created['node_id']
        from contextlib import redirect_stdout
        with (session.state/'expansion-worker.log').open('x') as output,redirect_stdout(output):worker.attempt(grant)
    finally:worker.lock.close();worker.state_lock.close()
    checked=session.personal_code('''import json,sys,hashlib
from pathlib import Path
from lab_client import Client
c=Client.from_profile(Path(sys.argv[1])/'client.json');identifier="'''+personal['job_id']+'''"
value=c.request('/api/jobs/'+identifier);assert value['job']['state']=='succeeded' and len(value['attempts'])==1,value
artifact=c.request('/api/jobs/'+identifier+'/artifact?name=ir%2Fcanonical.mlir',raw=True);assert b'arith.constant 42' in artifact
print(json.dumps({'job_id':identifier,'state':'succeeded','attempts':1,'ir_sha256':hashlib.sha256(artifact).hexdigest(),
 'personal_profile_sha256':hashlib.sha256((Path(sys.argv[1])/'client.json').read_bytes()).hexdigest(),'task_uid':23812}))
''')
    assert checked['personal_profile_sha256']==personal_before['sha256']
    assert credential_snapshot()==before and cache_snapshot()==cached_before
    historical=session.command('workerctl','owner-expand-apply','--plan',str(third));assert historical['historical_receipt_only']
    return {'passed':True,'existing_credential_count':1,'new_credentials_issued':0,'old_node_profile_and_cache_preserved':True,
        'personal_profile_unchanged':True,'classic_stage_approval_refused_active_owner':True,'native_uid_work_cache_appended':True,
        'authority_changed_then_restored_refused_before_ack':True,'authority_changed_before_first_heartbeat_refused':True,
        'explicit_same_plan_reauthorizations':2,'node_binding_fault_chain':faults,'ack_fault_chain':ack_faults,
        'actual_cpu_ir':checked,'personal_uid':23802,'node2_task_uid':23812,'systemd_pid1_tested':False,
        'scope':'Two installed Nodes inside one owned Root role on one physical host; no NPU, real host accounts, remote HOME or second physical server.'}

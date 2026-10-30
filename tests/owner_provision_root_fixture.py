"""Fresh staged owner: central coordinator plus the installed Node participant."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import uuid


def run(session,node_prefix,config,cold,actor,grant,first):
    import deployment as deploy
    import owner_provision as master
    from lab_client import read_json,atomic_json
    from worker import Worker
    from persistent_cache import Pool,Lease
    parent=Path('/personal/new_owner');parent.mkdir(mode=0o700);os.chown(parent,23802,23802)
    spool=Path('/personal/master-spool');spool.mkdir(mode=0o700)
    session.personal_path=parent/'agent'
    source=Path('/state/master-spec.json')
    spec={'operation_id':uuid.uuid4().hex,'owner':'new_owner','quota':{'input_storage_mib':4,'archive_storage_mib':32},
        'storage':{'gid':23832,'bytes':64*1024**2,'inodes':512},'service':{'mode':'process','unit_dir':None},
        'delivery':{'owner':'new_owner','parent':str(parent),'name':'agent','uid':23802,'gid':23802,'spool':str(spool)},
        'nodes':[{'node_id':session.node_id,'uid':23802,'gid':23802,
            'workspace':{'root':str(Path(config['workspace']['slots'][0]['path']).parent),'bytes':16*1024**2,'inodes':128},
            'cache':{'root':str(Path(config['persistent_cache']['slots'][0]['path']).parent),'bytes':16*1024**2,'inodes':128}}]}
    atomic_json(source,spec)
    cli=['/usr/bin/python3','-I','-B',session.prefix+'/current/servicectl','owner-provision','--prefix',session.prefix,'--spec',str(source)]
    local=['--local-node',session.node_id+'='+node_prefix]
    faults=[]

    def kill(boundary,component='master',local_nodes=False):
        result=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,json,os,signal
sys.path.insert(0,'/payload/code')
import owner_provision as m,payload_owner_admin as s,owner_finish as f,owner_delivery as d
spec=json.load(open('/state/master-spec.json'));component,boundary,local=sys.argv[1:]
def crash(stage):
 if stage==boundary:os.kill(os.getpid(),signal.SIGKILL)
{'master':m,'storage':s,'finish':f,'delivery':d}[component]._checkpoint=crash
m.provision('/opt/handoff-controller',spec,local_nodes={spec['nodes'][0]['node_id']:'/opt/owner-node'} if local=='yes' else {})
''',component,boundary,'yes' if local_nodes else 'no'],capture_output=True,text=True,timeout=45)
        assert result.returncode==-signal.SIGKILL,(component,boundary,result.returncode,result.stderr[-2400:])
        faults.append({'component':component,'boundary':boundary,'sigkill_observed':True})

    for conflict in ['gid','uid']:
        bad=copy.deepcopy(spec)
        if conflict=='gid':bad['storage']['gid']=23831
        else:bad['nodes'][0]['uid']=23801
        atomic_json(source,bad);refused=subprocess.run(cli,capture_output=True,text=True,timeout=30)
        assert refused.returncode and not (Path(session.prefix)/master.JOURNAL).exists()
        with sqlite3.connect(session.state/'identities.sqlite3') as db:assert not db.execute("SELECT 1 FROM users WHERE id='new_owner'").fetchone()

    abandoned=copy.deepcopy(spec);abandoned['operation_id']=uuid.uuid4().hex;abandoned['owner']='abandoned_owner'
    abandoned['delivery'].update(owner='abandoned_owner',name='unused')
    atomic_json(source,abandoned);kill('journal')
    aborted=json.loads(subprocess.run([*cli,'--abort'],capture_output=True,text=True,check=True,timeout=30).stdout)
    assert aborted['state']=='aborted' and not (Path(session.prefix)/master.JOURNAL).exists()
    with sqlite3.connect(session.state/'identities.sqlite3') as db:assert not db.execute("SELECT 1 FROM users WHERE id='abandoned_owner'").fetchone()
    atomic_json(source,{**abandoned,'quota':{'cpu':2,'input_storage_mib':4,'archive_storage_mib':32}})
    rejected=subprocess.run(cli,capture_output=True,text=True,timeout=30);assert rejected.returncode
    atomic_json(source,spec)
    for boundary in ['journal','held','stopped','stage_intent','staged','storage_plan']:kill(boundary)
    for boundary in ['journal','quota','config','metadata','binding','receipt','published']:kill(boundary,'storage')
    for boundary in ['storage_applied','storage_saved','service_allowed','service_response']:kill(boundary)
    pid=read_json(session.state/'service-process.json')['pid']
    maintenance=session.service_code('''import json,sys
from pathlib import Path
from lab_client import Client,ClientError
c=Client.from_profile(Path(sys.argv[1])/'personas/old_owner/client.json')
try:c.request('/api/jobs',{'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}})
except ClientError as error:assert '503' in str(error)
else:raise AssertionError('Central maintenance allowed new jobs')
print(json.dumps({'maintenance_submit_503':True}))
''');assert maintenance['maintenance_submit_503']
    for boundary in ['service_ready','packets']:kill(boundary)
    assert read_json(session.state/'service-process.json')['pid']==pid
    pending=json.loads(subprocess.run(cli,capture_output=True,text=True,check=True,timeout=30).stdout)
    assert pending['state']=='nodes' and len(pending['node_requests'])==1
    deploy.stop(session.prefix)
    recovered=json.loads(subprocess.run(cli,capture_output=True,text=True,check=True,timeout=30).stdout)
    assert recovered['state']=='nodes'
    restart_pid=read_json(session.state/'service-process.json')['pid'];assert restart_pid!=pid;pid=restart_pid
    packet_path=Path(pending['node_requests'][0]['request_file']);packet=read_json(packet_path)
    session.stage_id=pending['stage_id'];assert not (session.state/'personas/new_owner/client.json').exists()
    assert session.command('accessctl','user-status','new_owner')['disabled']
    wrong={**packet,'node_id':'worker-'+'0'*12};wrong['id']=master.sha({k:v for k,v in wrong.items() if k!='id'})
    try:master.node_participate(node_prefix,wrong)
    except deploy.DeployError:pass
    else:raise AssertionError('Wrong Node packet was accepted')
    wrong=copy.deepcopy(packet);wrong['controller']['url']='http://127.0.0.1:1';wrong['id']=master.sha({k:v for k,v in wrong.items() if k!='id'})
    try:master.node_participate(node_prefix,wrong)
    except deploy.DeployError:pass
    else:raise AssertionError('Packet targeted another controller')
    assert not (Path(config['state_dir'])/'node-owner-provision-change.json').exists()

    # Real old-owner work can proceed while the master waits for proposals.
    created=session.service_code('''import json,sys,os
from pathlib import Path
from lab_client import Client,read_json
from environment_contract import lock_for
s=Path(sys.argv[1]);c=Client.from_profile(s/'personas/old_owner/client.json');source=s/'old-owner-during-onboarding';source.mkdir(mode=0o700)
(source/'input.mlir').write_text('module { func.func @add() -> i32 { %a = arith.constant 19 : i32\\n %b = arith.constant 23 : i32\\n %r = arith.addi %a, %b : i32\\n return %r : i32 } }\\n')
(source/'run.py').write_text("import os,json,subprocess,shutil\\nfrom pathlib import Path\\nassert os.getuid()==23801\\nr=Path(os.environ['LAB_JOB_DIR']);out=r/'ir/canonical.mlir'\\nsubprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)\\nassert 'arith.constant 42' in out.read_text()\\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],'uid':os.getuid()}))\\n")
bundle=c.upload_input(source);config=read_json(s/'node-profile.json')
request={'environment':'worker-python','template':'ir_v1','idempotency_key':'old-owner-during-onboarding',
'parameters':{'input_id':bundle['id'],'argv':['python','run.py'],'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},'timeout_seconds':30,
'lock':lock_for(config['environment_manifest']),'workspace':{'min_mib':1,'min_inodes':16,'durable':True},'artifacts':{'file_mib':1,'total_mib':2}}}
value=c.request('/api/jobs',request);print(json.dumps({'job_id':value['job']['id']}))
''')
    worker=Worker(config)
    try:
        worker.heartbeat();job=worker.client.call('claim',{})['grant'];assert job and job['job_id']==created['job_id']
        from contextlib import redirect_stdout
        with (session.state/'old-owner-onboarding.log').open('x') as output,redirect_stdout(output):worker.attempt(job)
    finally:worker.lock.close();worker.state_lock.close()
    old_checked=session.service_code('''import sys,json
from pathlib import Path
from lab_client import Client
c=Client.from_profile(Path(sys.argv[1])/'personas/old_owner/client.json');j=c.request('/api/jobs/'''+created['job_id']+'''')
assert j['job']['state']=='succeeded' and len(j['attempts'])==1,j
print(json.dumps({'old_owner_real_ir_succeeded_while_waiting':True}))
''')
    node_packet=Path('/state/protected-node-request.json');atomic_json(node_packet,packet)
    prepared_cli=subprocess.run(['/usr/bin/python3','-I','-B',node_prefix+'/current/servicectl','owner-provision-node',
        '--prefix',node_prefix,'--request',str(node_packet)],capture_output=True,text=True,check=True,timeout=30)
    prepared=json.loads(prepared_cli.stdout);assert prepared['state']=='awaiting_approval' and prepared['proposal_id']
    for boundary in ['proposal','approval','approved','local_node','finish_intent']:kill(boundary,local_nodes=True)
    assert read_json(session.state/'service-process.json')['pid']==pid
    for boundary in ['journal','activation']:kill(boundary,'finish')
    deploy.stop(session.prefix)
    # Only credential delivery is pending: the same central operation must
    # restore its process without reissuing the committed user credential.
    kill('service_recovered')
    pid=read_json(session.state/'service-process.json')['pid']
    kill('planned','finish')
    for boundary in ['journal','directory','files','renamed','handoff','ownership','receipt','published']:kill(boundary,'delivery')
    for boundary in ['delivered','receipt','published']:kill(boundary,'finish')
    for boundary in ['finished','receipt','gate_removed','published']:kill(boundary)
    done=json.loads(subprocess.run([*cli,*local],capture_output=True,text=True,check=True,timeout=45).stdout)
    assert done['state']=='delivered' and done['historical_receipt_only'] and not (Path(session.prefix)/master.JOURNAL).exists()
    assert not (Path(session.prefix)/master.GATE).exists() and done['credentials_in_output'] is False
    current=read_json(Path(config['state_dir'])/'config.json');deploy.stop(node_prefix)
    new=Pool(Path(config['state_dir']),current);lease=Lease(new,grant(current,'old_owner',95));lease.prepare();warm=actor(lease);lease.release(termination_confirmed=True)
    assert not warm['compiled'] and warm['inode']==cold['inode'] and warm['sha256']==cold['sha256'] and lease.key==first.key
    assert current['users']['old_owner']==config['users']['old_owner'] and current['environment_manifest']==config['environment_manifest']
    # A second fresh owner completes in one ordinary installed CLI invocation,
    # without fault injection or manually staging/approving/delivering it.
    second=copy.deepcopy(spec);second['operation_id']=uuid.uuid4().hex;second['owner']='second_owner';second['storage']['gid']=23833
    second['nodes'][0].update(uid=23803,gid=23803)
    second_parent=Path('/personal/second_owner');second_parent.mkdir(mode=0o700);os.chown(second_parent,23803,23803)
    second['delivery'].update(owner='second_owner',parent=str(second_parent),uid=23803,gid=23803)
    second_source=Path('/state/second-master-spec.json');atomic_json(second_source,second)
    second_cli=['/usr/bin/python3','-I','-B',session.prefix+'/current/servicectl','owner-provision','--prefix',session.prefix,
        '--spec',str(second_source),*local]
    second_done=json.loads(subprocess.run(second_cli,capture_output=True,text=True,check=True,timeout=60).stdout)
    assert second_done['state']=='delivered' and not second_done['historical_receipt_only'] and not second_done['credentials_in_output']
    original_personal=session.personal_path;session.personal_path=second_parent/'agent'
    second_me=session.personal_code('''import json,sys,os
from pathlib import Path
from lab_client import Client
c=Client.from_profile(Path(sys.argv[1])/'client.json');assert c.request('/api/me')['id']=='second_owner'
assert os.getuid()==23803
print(json.dumps({'second_owner_personal_auth_verified':True}))
''',uid=23803)
    peer=session.personal_code('''import json,sys
from pathlib import Path
try:(Path(sys.argv[1])/'client.json').read_bytes()
except PermissionError:print(json.dumps({'peer_private_profile_denied':True}))
else:raise AssertionError('First owner read second owner profile')
''')
    assert peer['peer_private_profile_denied'];session.personal_path=original_personal
    deploy.stop(node_prefix);current=read_json(Path(config['state_dir'])/'config.json')
    assert current['users']['old_owner']==config['users']['old_owner'] and current['users']['new_owner']=={'uid':23802,'gid':23802}
    atomic_json(session.personal_path/'environment.json',current['environment_manifest']);os.chown(session.personal_path/'environment.json',23802,23802)
    # Reuse the previously accepted personal-client IR on a second Node. This
    # proves the centrally delivered profile works and credentials stay single.
    from owner_expand_fixture import run as expand
    expansion=expand(session,current)
    session.command('accessctl','user-disable','new_owner',json_output=False)
    replay=json.loads(subprocess.run(cli,capture_output=True,text=True,check=True,timeout=30).stdout)
    assert replay['historical_receipt_only'] and session.command('accessctl','user-status','new_owner')['disabled']
    deploy.stop(session.prefix)
    return {'passed':True,'fault_chain':faults,'fault_count':len(faults),'initial_abort_before_identity_has_no_user_row':True,
        'duplicate_gid_and_mapped_uid_refused_before_stage_or_journal':True,
        'changed_completed_request_refused':True,'central_staged_gid_lifecycle_node_and_delivery_coordinated':True,
        'single_live_controller_pid_preserved_after_start':True,'maintenance_submit_refused':True,**old_checked,
        'controller_restart_while_waiting_nodes_and_credential_delivery':True,
        'wrong_node_and_controller_packets_refused_before_native_effects':True,'source_stage_disabled_until_final_activation':True,
        'installed_node_packet_cli_prepared_original_proposal':True,'second_fresh_owner_single_installed_cli_call_delivered':True,
        **second_me,'peer_private_profile_denied':True,
        'old_mapping_environment_and_warm_cache_inode_sha_preserved':True,'manual_credential_copy_used':False,
        'personal_delivered_profile_actual_second_node_ir':expansion,'later_disabled_historical_replay_does_not_revive':True,
        'service_mode':'process','systemd_pid1_tested':False,'physical_hosts_tested':1,'remote_root_transport':False,
        'scope':'Owned same-host Root controller coordinator plus installed local Node participant; real non-root service, native GID/UID quotas, authenticated ACK/heartbeat and automatic final personal delivery. Separate-host packet transport and actual PID1 remain untested.'}

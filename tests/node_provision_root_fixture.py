"""Real native Node participant, staged central owner and original-plan faults."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid


def run(session,prefix,config,cold,actor,grant,first):
    import deployment as deploy
    import node_owner_provision as participant
    import filesystem_quota as quota
    from lab_client import atomic_json,read_json
    from persistent_cache import Pool,Lease
    from workspace_slots import root_fd
    from worker import Worker
    from state_lock import state_lock
    state=Path(config['state_dir']);source=Path('/state/node-provision-spec.json')
    spec={'operation_id':uuid.uuid4().hex,'owner':'new_owner','stage_id':session.stage_id,'uid':23802,'gid':23802,
        'workspace':{'root':str(Path(config['workspace']['slots'][0]['path']).parent),'bytes':16*1024**2,'inodes':128},
        'cache':{'root':str(Path(config['persistent_cache']['slots'][0]['path']).parent),'bytes':16*1024**2,'inodes':128}}
    atomic_json(source,spec);cli=['/usr/bin/python3','-I','-B',prefix+'/current/servicectl','node-owner-provision','--prefix',prefix,'--spec',str(source)]
    with state_lock(state):
        try:participant.provision(prefix,spec)
        except RuntimeError:pass
        else:raise AssertionError('Participant ignored live Node shared lock')
    assert not (state/participant.JOURNAL).exists()
    pool=Pool(state,config);held=Lease(pool,grant(config,'old_owner',90));held.prepare()
    try:
        try:participant.provision(prefix,spec)
        except Exception:pass
        else:raise AssertionError('Participant prepared while old cache had a lease')
    finally:held.release(termination_confirmed=True)
    assert not (state/participant.JOURNAL).exists()

    def kill(mode,boundary,component='participant',role='workspace'):
        result=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import json,sys,os,signal
sys.path.insert(0,'/payload/code')
import node_owner_provision as p,workspace_quota_admin as n,node_owner_admin as b,node_owner_ack as a
spec=json.load(open(sys.argv[1]));component,role,boundary,action=sys.argv[2:]
def crash(stage):
 if stage==boundary:os.kill(os.getpid(),signal.SIGKILL)
if component=='participant':p._checkpoint=crash
elif component=='binding':b._checkpoint=crash
elif component=='ack':a._checkpoint=crash
else:
 original=n.apply
 def apply(prefix,value):
  n._checkpoint=crash if value['root']==spec[role]['root'] else lambda stage:None
  return original(prefix,value)
 n.apply=apply
p.provision('/opt/owner-node',spec,action)
''',str(source),component,role,boundary,mode],capture_output=True,text=True,timeout=30)
        assert result.returncode==-signal.SIGKILL,(component,role,boundary,result.returncode,result.stderr[-1800:])
        return {'component':component,'role':role if component=='native' else None,'boundary':boundary,'sigkill_observed':True}

    # An abort finishes only the already-started native append. Its unstarted
    # cache plan remains unused, while the allocated work quota stays owned.
    abort_faults=[]
    for uid,active_role in [(23803,'workspace'),(23804,'cache')]:
        abandoned={**spec,'operation_id':uuid.uuid4().hex,'uid':uid,'gid':uid};atomic_json(source,abandoned)
        abort_faults.append(kill('prepare','quota','native',active_role))
        stopped=subprocess.run([*cli,'--abort'],capture_output=True,text=True,check=True,timeout=30);aborted=json.loads(stopped.stdout)
        assert aborted['state']=='aborted' and aborted['resources_retained'] and not (state/participant.JOURNAL).exists()
        replay=json.loads(subprocess.run(cli,capture_output=True,text=True,check=True,timeout=30).stdout)
        assert replay['state']=='aborted' and replay['historical_receipt_only']
        for role in ['workspace','cache']:
            expected=16*1024**2 if role=='workspace' or active_role=='cache' else 0
            slot=config['workspace' if role=='workspace' else 'persistent_cache']['slots'][0]
            with root_fd(slot) as fd:assert quota.read(fd,uid)['byte_hard']==expected
        atomic_json(source,{**abandoned,'uid':23805,'gid':23805})
        changed=subprocess.run(cli,capture_output=True,text=True,timeout=30)
        assert changed.returncode and 'historical receipt differs' in changed.stderr
    assert 'new_owner' not in read_json(state/'config.json')['users']
    atomic_json(source,spec);faults=[kill('prepare','journal')]
    for role in ['workspace','cache']:
        for boundary in ['journal','intent','quota','directory','marker','receipt','published']:
            faults.append(kill('prepare',boundary,'native',role))
        faults.append(kill('prepare',role))
    for boundary in ['proposal','prepared']:faults.append(kill('prepare',boundary))
    ready=json.loads(subprocess.run(cli,capture_output=True,text=True,check=True,timeout=30).stdout)
    assert ready['state']=='awaiting_approval' and ready['credentials_issued'] is False
    proposal_path=Path(ready['proposal_path']);value=read_json(proposal_path);raw=proposal_path.read_bytes()
    assert value['id']==ready['proposal_id'] and value['request']['stage_id']==session.stage_id
    missing=proposal_path.with_name(proposal_path.name+'.retained');proposal_path.rename(missing)
    refused=subprocess.run(cli,capture_output=True,text=True,timeout=30)
    assert refused.returncode and 'Unknown or unsafe' in refused.stderr
    # Keep the original valid filename while testing a missing proposal, so
    # the unknown-history guard does not obscure the actual missing binding.
    missing.rename(proposal_path);outside=state/'retained-node-proposal.json';proposal_path.rename(outside)
    refused=subprocess.run(cli,capture_output=True,text=True,timeout=30)
    assert refused.returncode and 'proposal is missing' in refused.stderr
    outside.rename(proposal_path);assert proposal_path.read_bytes()==raw
    changed={**spec,'operation_id':uuid.uuid4().hex};atomic_json(source,changed)
    rejected=subprocess.run(cli,capture_output=True,text=True,timeout=30);assert rejected.returncode
    atomic_json(source,spec)
    rejected=subprocess.run([*cli,'--abort'],capture_output=True,text=True,timeout=30)
    assert rejected.returncode and 'central reconciliation' in rejected.stderr and proposal_path.read_bytes()==raw
    for action in [lambda:deploy.verify(prefix),lambda:deploy.start(prefix),lambda:Worker(config)]:
        try:action()
        except deploy.DeployError:pass
        else:raise AssertionError('Unapproved participant allowed normal Node use')

    faults.append(kill('complete','binding_intent'))
    for boundary in ['journal','cache_control','cache_marker','new_control','new_marker','config','metadata','receipt','handoff','published']:
        faults.append(kill('complete',boundary,'binding'))
    for boundary in ['binding_applied','ack_intent']:faults.append(kill('complete',boundary))
    faults.append(kill('complete','intent','ack'))
    unapproved=subprocess.run([*cli,'--complete'],capture_output=True,text=True,timeout=30)
    assert unapproved.returncode and 'HTTP 404' in unapproved.stderr
    stage=session.command('accessctl','user-status','new_owner');assert stage['disabled'] and stage['state']=='staged'
    # The trusted transfer copies a credential-free proposal only, through the
    # existing private service-UID channel. It is not a remote Root transport.
    session.approve(proposal_path)
    faults.append(kill('complete','commit_response','ack'))
    rotated=session.service_code('''import json,sys
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/'config.json'))
print(json.dumps({'node_key':j.nodes.rotate_key("'''+config['node_id']+'''")}))
''')
    updated=read_json(state/'config.json');updated['node_key']=rotated['node_key'];atomic_json(state/'config.json',updated)
    from lab_client import Client,ClientError
    try:Client({'controller':config['controller'],'token':config['node_key']}).request('/api/worker/claim',{})
    except ClientError as error:assert '401' in str(error)
    else:raise AssertionError('Revoked old Node credential remained valid')
    for boundary in ['committed','config_ready','ready_response','ready_saved','receipt','awaiting_removed','published']:
        faults.append(kill('complete',boundary,'ack'))
    for boundary in ['acknowledged','node_ready','receipt','published']:faults.append(kill('complete',boundary))
    finished=json.loads(subprocess.run([*cli,'--complete'],capture_output=True,text=True,check=True,timeout=30).stdout)
    assert finished['state']=='bound' and finished['historical_receipt_only']
    assert finished['node_start_and_fresh_heartbeat_required'] and finished['central_owner_activation_still_required']
    assert not (state/participant.JOURNAL).exists() and proposal_path.read_bytes()==raw
    deploy.verify(prefix);current=read_json(state/'config.json');new=Pool(state,current);new.assert_idle()
    old=Lease(new,grant(current,'old_owner',91));old.prepare();warm=actor(old);old.release(termination_confirmed=True)
    assert not warm['compiled'] and warm['inode']==cold['inode'] and warm['sha256']==cold['sha256'] and old.key==first.key
    added=Lease(new,grant(current,'new_owner',92));added.prepare();fresh=actor(added);added.release(termination_confirmed=True)
    assert fresh['compiled'] and fresh['uid']==23802 and added.path!=old.path
    assert current['users']['old_owner']==config['users']['old_owner']
    assert current['environment_manifest']==config['environment_manifest']
    assert current['workspace']['slots'][:-1]==config['workspace']['slots'] and current['persistent_cache']['slots'][:-1]==config['persistent_cache']['slots']
    assert current['users']['new_owner']=={'uid':23802,'gid':23802}
    return value,current,{'passed':True,'installed_cli':True,'fault_chain':faults,'fault_count':len(faults),
        'abort_faults':abort_faults,'abort_work_retained_unstarted_cache_unallocated':True,'abort_cache_inflight_keeps_completed_work':True,
        'operation_id_reuse_with_changed_spec_refused':True,'missing_proposal_never_regenerated':True,
        'node_key_rotated_after_commit_response_old_key_denied_same_ack_resumed':True,
        'shared_service_lock_and_live_cache_rejected_before_journal':True,'one_original_proposal_sha256':hashlib.sha256(raw).hexdigest(),
        'changed_request_and_exported_abort_refused':True,'unapproved_ack_refused':True,
        'startup_and_maintenance_blocked_while_pending':True,'old_uid_mapping_slots_environment_preserved':True,
        'old_cache_inode_ir_sha_and_namespace_preserved':True,'cold_ir':cold,'warm_ir':warm,'new_owner_ir':fresh,
        'credentials_issued_by_node':False,'bound_receipt_is_historical_not_health':True,
        'scope':'Same-host owned Root Node participant with native quota plans, real central stage/approval/ACK and CPU BishengIR. No remote Root transport, real host account creation, service start or NPU.'}

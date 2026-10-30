"""Actual Root/SVC/personal UID final activation + managed credential delivery."""
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid


CHILD='''import sys,os,signal,json,subprocess,resource
sys.path.insert(0,"/opt/handoff-controller/current")
import owner_finish as f,owner_delivery as d
boundary=sys.argv[1]
def crash(stage):
 if boundary=='finish:'+stage:os.kill(os.getpid(),signal.SIGKILL)
def delivery_crash(stage):
 if boundary=='delivery:'+stage:os.kill(os.getpid(),signal.SIGKILL)
f._checkpoint=crash;d._checkpoint=delivery_crash
if boundary.startswith('activation:'):
 def service(release,state,meta,path,aborting=False):
  code="""import sys,os,signal
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from jobs import JobStore
from lab_client import read_json
import owner_activation as a
def crash(stage):
 if stage==sys.argv[4]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;s=Path(sys.argv[2]);a.Activation(JobStore(s,read_json(s/'config.json'))).activate(read_json(sys.argv[3]))
"""
  def drop():
   from payload_groups import gids
   os.setgroups(gids(meta.get('payload_storage')));os.setgid(meta['gid']);os.setuid(meta['uid']);os.umask(0o077)
  r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(release),str(state),str(path),boundary.split(':')[1]],preexec_fn=drop,
   stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30,
   env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
  assert r.returncode==-signal.SIGKILL,(r.returncode,r.stderr[-1000:])
  os.kill(os.getpid(),signal.SIGKILL)
 f._service=service
f.finish('/opt/handoff-controller',json.load(open('/state/finish-spec.json')))
'''


def run(session,spec,config):
    import deployment as deploy
    from lab_client import read_json
    from payload_quota_admin import write
    import owner_delivery as delivery
    import owner_finish as finish
    personal=Path('/personal/new_owner');personal.mkdir(mode=0o700);os.chown(personal,23802,23802)
    spool=Path('/personal/.delivery-spool');spool.mkdir(mode=0o700)
    session.personal_path=personal/'agent'
    request={'operation_id':uuid.uuid4().hex,'activation':spec,'delivery':{'owner':'new_owner','parent':str(personal),'name':'agent','uid':23802,'gid':23802,'spool':str(spool)}}
    path=Path('/state/finish-spec.json');write(path,request);session.finish_spec=request
    preview=session.root_command('owner-finish','--prefix',session.prefix,'--spec',str(path),'--plan');assert preview['read_only']
    assert not (Path(session.prefix)/finish.JOURNAL).exists() and not session.personal_path.exists()
    session.service_code('''import json,sys,sqlite3
from pathlib import Path
p=Path(sys.argv[1])/'identities.sqlite3'
with sqlite3.connect(p) as d:
 d.execute('CREATE TABLE finish_fixture_padding(payload BLOB)');d.execute('INSERT INTO finish_fixture_padding VALUES(zeroblob(300000))')
assert p.stat().st_size>256*1024
print(json.dumps({'large_identity_db':True}))
''')
    def killed(boundary):
        r=subprocess.run(['/usr/bin/python3','-I','-B','-c',CHILD,boundary],capture_output=True,text=True,timeout=40)
        assert r.returncode==-signal.SIGKILL,(boundary,r.returncode,r.stderr[-1600:])
        for flags in [{},{'manager_recovery':True},{'startup_recovery':True},{'quota_recovery':True}]:
            try:deploy.verify(session.prefix,**flags)
            except deploy.DeployError:pass
            else:raise AssertionError('Pending finish coordinator bypassed maintenance guard')
    # The wrapper aborts only its own uncommitted native activation; it keeps
    # identity/storage registrations and issues no personal credential.
    abort_request={**request,'operation_id':uuid.uuid4().hex}
    write(path,abort_request);killed('finish:journal');killed('activation:intent')
    aborted=session.root_command('owner-finish','--prefix',session.prefix,'--spec',str(path),'--abort')
    assert aborted['state']=='aborted' and aborted['prior_state_retained'] and not session.personal_path.exists()
    with __import__('sqlite3').connect(session.state/'identities.sqlite3') as db:
        assert db.execute("SELECT disabled,password_hash FROM users WHERE id='new_owner'").fetchone()==(1,'!')
        assert db.execute("SELECT COUNT(*) FROM tokens WHERE user='new_owner'").fetchone()[0]==0
    write(path,request)
    killed('finish:journal')
    before=deploy.process_status(session.prefix);deploy.stop(session.prefix);started=deploy.start(session.prefix)
    assert before['pid']!=started['pid'] and started['ready']
    from worker import Worker
    worker=Worker(config)
    try:worker.heartbeat()
    finally:worker.lock.close();worker.state_lock.close()
    activation=[];identifier=None
    for boundary in ['intent','trust','client','before_commit','committed','receipt','published']:
        killed('activation:'+boundary)
        pending=read_json(session.state/'owner-activations/new_owner.pending.json')
        if pending:
            if identifier:assert identifier==pending['id']
            identifier=pending['id']
        activation.append({'boundary':boundary,'sigkill_observed':True,'same_activation_id':True})
    killed('finish:activation');killed('finish:planned')
    master=read_json(Path(session.prefix)/finish.JOURNAL);plan=master['delivery_plan'];session.delivery_plan=plan
    encoded=json.dumps(plan);source=read_json(session.state/'personas/new_owner/client.json')
    assert source['token'] not in encoded and source['password'] not in encoded
    faults=[]
    for boundary in ['journal','directory','files','renamed','handoff','ownership','receipt','published']:
        killed('delivery:'+boundary)
        if boundary=='directory':
            stage=spool/('.delivery-'+plan['nonce']);raw=(session.state/'personas/new_owner/client.json').read_bytes()
            fd=os.open(stage/'client.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as file:file.write(raw[:23]);file.flush();os.fsync(file.fileno())
        if boundary=='ownership':
            saved=session.personal_code('''import sys,json
from pathlib import Path
from lab_client import atomic_json
p=Path(sys.argv[1])/'client.json';v=json.loads(p.read_text());v['archive_root']=str(p.parent/'archives');atomic_json(p,v)
print(json.dumps({'personal_preference_saved':True}))
''');assert saved['personal_preference_saved']
        faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_resumed':True})
    for boundary in ['delivered','receipt']:
        killed('finish:'+boundary)
    r=subprocess.run(['/usr/bin/python3','-I','-B','-c',CHILD,'finish:published'],capture_output=True,text=True,timeout=40)
    assert r.returncode==-signal.SIGKILL
    result=session.root_command('owner-finish','--prefix',session.prefix,'--spec',str(path));assert result['historical_receipt_only'] and result['state']=='delivered'
    assert not (Path(session.prefix)/delivery.JOURNAL).exists() and not (Path(session.prefix)/finish.JOURNAL).exists()
    profile=read_json(session.personal_path/'client.json');assert profile['archive_root']==str(session.personal_path/'archives') and profile['token']==source['token']
    for entry in session.personal_path.iterdir():assert entry.stat().st_uid==23802 and entry.stat().st_mode&0o777==0o600
    assert session.personal_path.stat().st_mode&0o777==0o700
    with __import__('sqlite3').connect(session.state/'identities.sqlite3') as db:
        assert db.execute("SELECT COUNT(*) FROM tokens WHERE user='new_owner'").fetchone()[0]==1
    # Source was activated once; a conflicting personal destination is untouched.
    collision={**request['delivery'],'name':'collision'};planned=delivery.plan(session.prefix,collision)['plan']
    collision_path=personal/'collision';collision_path.mkdir(mode=0o700);(collision_path/'sentinel').write_text('untouched')
    try:delivery.apply(session.prefix,planned)
    except deploy.DeployError:pass
    else:raise AssertionError('Pre-existing personal destination was overwritten')
    assert (collision_path/'sentinel').read_text()=='untouched' and not (Path(session.prefix)/delivery.JOURNAL).exists()
    # Recreated personal parents and a different filesystem cannot be adopted.
    stale=Path('/personal/stale-parent');stale.mkdir(mode=0o700);os.chown(stale,23802,23802)
    stale_plan=delivery.plan(session.prefix,{**request['delivery'],'parent':str(stale),'name':'agent'})['plan']
    stale.rename(stale.with_name('stale-parent-original'));stale.mkdir(mode=0o700);os.chown(stale,23802,23802)
    try:delivery.apply(session.prefix,stale_plan)
    except deploy.DeployError:pass
    else:raise AssertionError('Recreated personal parent was accepted')
    cross=Path('/opt/cross-delivery-spool');cross.mkdir(mode=0o700)
    try:delivery.plan(session.prefix,{**request['delivery'],'spool':str(cross),'name':'cross'})
    except deploy.DeployError:pass
    else:raise AssertionError('Cross-filesystem publication was accepted')
    # Advance only this isolated admin process clock at the handoff boundary.
    expiry_plan=delivery.plan(session.prefix,{**request['delivery'],'name':'expiry'})['plan']
    expiry_path=Path('/state/expiry-delivery-plan.json');write(expiry_path,expiry_plan)
    r=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,json
sys.path.insert(0,"/opt/handoff-controller/current")
import owner_delivery as d
v=json.load(open("/state/expiry-delivery-plan.json"))
def advance(stage):
 if stage=='handoff':d.time.time=lambda:v['source']['valid_until_epoch']+1
d._checkpoint=advance
try:d.apply("/opt/handoff-controller",v)
except d.DeployError:sys.exit(42)
'''],capture_output=True,text=True,timeout=30)
    assert r.returncode==42 and (personal/'expiry').stat().st_uid==0
    abandoned=delivery.apply(session.prefix,expiry_plan,abort=True);assert abandoned['state']=='aborted' and abandoned['destination_artifacts_retained']
    session.after_disable_plan=delivery.plan(session.prefix,{**request['delivery'],'name':'after-disable'})['plan']
    proof=Path('/state/owner-finish-proof');proof.mkdir(mode=0o700)
    write(proof/'master.json',master);write(proof/'receipt.json',result)
    write(proof/'delivery-receipt.json',read_json(Path(session.prefix)/delivery.HISTORY/(plan['id']+'.json')))
    return activation,identifier,{'passed':True,'activation_fault_chain':activation,'delivery_fault_chain':faults,
        'finish_fault_boundaries':['journal','activation','planned','delivered','receipt','published'],
        'single_activated_token':True,'root_cli_preview_and_replay':True,'personal_uid':23802,'profile_mode':'0600','directory_mode':'0700',
        'personal_preference_preserved_after_ownership':True,'collision_no_overwrite':True,'pending_maintenance_guard':True,
        'plan_contains_no_credentials':True,'manual_credential_copy_used':False,'full_resource_onboarding_coordinator':False,
        'uncommitted_finish_abort_cleared_only_own_activation':True,
        'fresh_operation_id_retries_same_target_after_abort':True,'controller_restart_while_finish_pending':True,
        'partial_root_file_prefix_resumed':True,'stale_parent_refused':True,'cross_filesystem_refused':True,
        'expiry_before_ownership_refused_and_explicitly_aborted':True,'large_identity_db_over_256k':True,
        'scope':'Installed Root final activation/delivery coordinator, real SVC activation and personal UID ownership. Earlier resource preprovisioning remains separate; no real OS accounts, PID1, physical multi-host or NPU acceptance.'}


def after_disable(session):
    import owner_delivery as delivery
    import deployment as deploy
    from lab_client import read_json
    before=(session.personal_path/'client.json').read_bytes()
    try:delivery.apply(session.prefix,session.after_disable_plan)
    except deploy.DeployError:pass
    else:raise AssertionError('Disabled/revoked credentials were newly delivered')
    result=session.root_command('owner-finish','--prefix',session.prefix,'--spec','/state/finish-spec.json')
    assert result['historical_receipt_only'] and result['state']=='delivered' and (session.personal_path/'client.json').read_bytes()==before
    assert session.command('accessctl','user-status','new_owner')['disabled']
    assert not (Path(session.prefix)/delivery.JOURNAL).exists() and not (session.personal_path.parent/'after-disable').exists()
    session.finish_proof.update(disabled_revoked_source_rejected_before_mutation=True,finished_replay_keeps_disabled_and_personal_edits=True)

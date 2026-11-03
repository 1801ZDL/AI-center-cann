"""Real CPU source grant -> cold payload -> migrated body -> restore.

Extends the existing UID-separated source workflow. No manufactured execution
receipt, NPU capture or completed job is inserted into the installed controller.
"""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from accuracy_diagnostic import read_case
from lab_client import Client,ClientHTTPError,atomic_json

PREFIX='/opt/ascend-lab-controller'


def root(ctx,code,*args):
    reply=ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-I','-B','-c',code,*map(str,args)],timeout=45)
    return json.loads(reply.stdout) if reply.stdout.strip() else None


def command(ctx,action,*args,check=True):
    # Installed current helper, not a copied plan interpreter or Root SSH.
    reply=ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-I','-B',PREFIX+'/current/servicectl',
        action,'--prefix',PREFIX,*map(str,args)],check=check,timeout=45)
    return json.loads(reply.stdout) if check else reply


def save(ctx,path,value):
    code='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);assert not p.exists();p.write_text(sys.argv[2]);p.chmod(0o600)'
    root(ctx,code,path,json.dumps(value))


def assertions(client,source,manifest):
    assert client.request('/api/jobs/'+source)['job']['state']=='failed'
    assert client.request('/api/jobs/'+source+'/archive-manifest')==manifest


def run(ctx):
    proof=ctx['source_grant_proof'];parent=proof['parent'];helpers=ctx['_source_grant_helpers']
    owner=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    original=owner.request('/api/jobs/'+parent);manifest=owner.request('/api/jobs/'+parent+'/archive-manifest')
    assert original['job']['state']=='failed' and len(original['attempts'])==1
    old_case=read_case(owner,parent);assert old_case['comparison']['mismatches']==11
    destination,agent,profile=helpers['budget']('cold-migration',2)
    grant=helpers['approval']('cold-migration',parent,destination,helpers['files'])
    imports=[{'job_id':parent,'name':file,'target':name,'when':'terminal','source_grant':grant['id']}
        for file,name in zip(helpers['files'],helpers['names'])]
    # All original tasks are terminal. Stop the same two owned role handles.
    ctx['ctl'](ctx['node'],'node','stop');ctx['ctl'](ctx['controller'],'controller','stop')
    command(ctx,'history-capacity-plan','--max-control-jobs',200,'--max-online-archives',200,'--output','/root/cold-history-capacity.json')
    command(ctx,'history-capacity-apply','--plan','/root/cold-history-capacity.json')
    storage=root(ctx,'''import json,os,sys,uuid
from pathlib import Path
p=Path('/srv/cold-history');assert sorted(x.name for x in p.iterdir())==['lost+found']
(p/'lost+found').rmdir();p.chmod(0o700);s=p.stat();v=os.statvfs(p)
print(json.dumps({'path':str(p),'filesystem':'ext4','device':s.st_dev,'root_inode':s.st_ino,
 'bytes':v.f_blocks*v.f_frsize,'inodes':v.f_files,'storage_id':uuid.uuid4().hex}))
''')
    spec={'storage':storage,'budget_bytes':64*1024**2,'budget_inodes':1024,
        'owners':{original['job']['owner']:{'bytes':48*1024**2,'inodes':768}},'max_job_bytes':32*1024**2,'max_transfers':8}
    save(ctx,'/root/cold-history-pool.json',spec)
    command(ctx,'cold-register-plan','--spec','/root/cold-history-pool.json','--output','/root/cold-register.json')
    command(ctx,'cold-register-apply','--plan','/root/cold-register.json')
    command(ctx,'cold-copy-plan','--job-id',parent,'--output','/root/cold-copy.json')
    for _ in range(32):
        copied=command(ctx,'cold-copy-step','--plan','/root/cold-copy.json')
        if copied['state']=='verified':break
    else:raise AssertionError('Original bounded cold transfer did not converge')
    command(ctx,'cold-read-plan','--copy-plan','/root/cold-copy.json','--output','/root/cold-read.json')
    command(ctx,'cold-retention-apply','--plan','/root/cold-read.json')
    command(ctx,'cold-retire-plan','--copy-plan','/root/cold-copy.json','--output','/root/cold-retire.json')
    for _ in range(64):
        retired=command(ctx,'cold-retention-apply','--plan','/root/cold-retire.json')
        if not retired.get('pending'):break
    else:raise AssertionError('Original bounded retirement did not converge')
    assert retired['online_payload_removed'] and retired['online_archive_slot_released']
    command(ctx,'control-history-plan','--job-id',parent,'--budget-bytes',16*1024**2,'--max-exports',4,'--output','/root/control-history.json')
    for _ in range(32):
        snapshot=command(ctx,'control-history-step','--plan','/root/control-history.json')
        if snapshot['state']=='verified':break
    else:raise AssertionError('Original bounded control export did not converge')
    command(ctx,'control-rotate-plan','--export-id',snapshot['id'],'--output','/root/control-rotate.json')
    migrated=command(ctx,'control-rotate-apply','--plan','/root/control-rotate.json');assert migrated['control_slots_released']==1
    unchanged=root(ctx,'''import json,sqlite3,sys
from pathlib import Path
db=sqlite3.connect(sys.argv[1]);row=db.execute('SELECT data FROM jobs WHERE id=?',(sys.argv[2],)).fetchone();job=json.loads(row[0])
assert '_control_history_ref' in job and '_token' not in job
assert not Path(job['archive_dir']).exists()
assert db.execute('SELECT online FROM history_capacity_slots WHERE job_id=?',(sys.argv[2],)).fetchone()==(0,)
assert db.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(sys.argv[2],)).fetchone()[0]==1
print(json.dumps({'stub_bytes':len(row[0].encode()),'source_attempts':1,'original_warm_absent':True}))
''',ctx['controller_state']/'jobs.sqlite3',parent)
    ctx['ctl'](ctx['controller'],'controller','start');ctx['ctl'](ctx['node'],'node','start')
    assertions(owner,parent,manifest)
    assert owner.request('/api/jobs/'+parent)['job']==original['job']
    identifier=helpers['submit']('migrated-cold-diagnostic',agent,profile,helpers['consumer_input'],imports,parent)
    task=next(t for t in helpers['tasks'] if t['job_id']==identifier);replay=task['_conversation']
    completed=helpers['finished'](identifier,agent,'failed');case=read_case(agent,identifier)
    assert case['comparison']['mismatches']==11 and case['tensor_sources']['actual']['sha256']==proof['actual_sha256']
    assert owner.request('/api/jobs/'+parent)['attempts']==original['attempts']
    for path in ['/api/jobs/'+parent,'/api/jobs/'+parent+'/archive-manifest']:
        try:agent.request(path)
        except ClientHTTPError as error:assert error.status==404
        else:raise AssertionError('File grant disclosed the original source history')
    owner.request('/api/source-grants/'+grant['id']+'/revoke',{})
    replayed=replay();assert replayed['reused'] and replayed['job']['id']==identifier
    denied=json.loads((ctx['base']/'migrated-cold-diagnostic-source-grant-request.json').read_text())
    denied['idempotency_key']='new-after-migrated-cold-grant-revocation'
    try:agent.request('/api/jobs',denied)
    except ClientHTTPError as error:assert error.status==409
    else:raise AssertionError('Historical control snapshot revived a revoked source grant')
    charged=agent.request('/api/experiment-budgets/'+destination)['budget']['charged']
    assert charged=={'jobs':1,'cpu_seconds':60,'npu_seconds':0}
    # Root hides only our original objects, then restores the same inode names.
    # There are no active consumers here. Never edit or rehash missing bytes.
    paths=root(ctx,'''import json
from pathlib import Path
p=Path('/root/cold-copy.json');copy=json.loads(p.read_text());history=json.loads(Path('/root/control-history.json').read_text())
print(json.dumps({'cold':'/srv/cold-history/objects/'+copy['id'],'metadata':'/opt/ascend-lab-controller/control-history/'+history['id']+'.json'}))
''')
    for kind in ['cold','metadata']:
        path=paths[kind]
        root(ctx,'from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_name(p.name+".hidden"))',path)
        try:
            if kind=='cold':
                detail=owner.request('/api/jobs/'+parent);assert detail['archive_location']['read_unavailable']
                endpoint='/api/jobs/'+parent+'/archive-manifest'
            else:endpoint='/api/jobs/'+parent
            try:owner.request(endpoint)
            except ClientHTTPError as error:assert error.status==409
            else:raise AssertionError('Unavailable original '+kind+' silently returned replacement facts')
            try:agent.request('/api/jobs/'+parent)
            except ClientHTTPError as error:assert error.status==404
            else:raise AssertionError('Unavailable history changed current experiment ACL')
        finally:
            root(ctx,'from pathlib import Path;import sys;p=Path(sys.argv[1]);p.with_name(p.name+".hidden").rename(p)',path)
        assertions(owner,parent,manifest)
    result={'passed':True,'parent':parent,'original_attempt_id':proof['original_attempt_id'],'destination_budget':destination,
        'grant':grant['id'],'diagnostic_job':identifier,'diagnostic_attempt_id':completed['attempt_id'],'mismatches':11,
        'actual_sha256':proof['actual_sha256'],'copy_id':copied['id'],'control_export_id':snapshot['id'],'migration_id':migrated['id'],
        'manifest':manifest,'original_job':original['job'],'original_attempts':original['attempts'],'charged':charged,**unchanged,
        'active_grant_preserved_across_migration':True,'revocation_blocks_new_job_but_original_replay_is_free':True,
        'cold_and_metadata_loss_fail_closed_independently':True,'same_original_bytes_and_one_producer_attempt':True,
        'normal_read_authority':True,'control_slots_released':1,'npu_launches':0,'model_calls':0}
    atomic_json(ctx['base']/'cold-history-original.json',result)
    return result


def after_restore(ctx,proof):
    owner=Client({'controller':ctx['controller_url'],'token':ctx['token']});parent=proof['parent']
    detail=owner.request('/api/jobs/'+parent);expected=dict(proof['original_job']);expected['archive_dir']=detail['job']['archive_dir']
    assert detail['job']==expected and detail['attempts']==proof['original_attempts'] and detail['archive_location']['read_unavailable']
    try:owner.request('/api/jobs/'+parent+'/archive-manifest')
    except ClientHTTPError as error:assert error.status==409
    else:raise AssertionError('Restored cold access silently resumed')
    ctx['ctl'](ctx['node'],'node','stop');ctx['ctl'](ctx['controller'],'controller','stop')
    command(ctx,'cold-read-resume-plan','--copy-plan','/root/cold-copy.json','--output','/root/cold-read-resume.json')
    command(ctx,'cold-retention-apply','--plan','/root/cold-read-resume.json')
    ctx['ctl'](ctx['controller'],'controller','start');ctx['ctl'](ctx['node'],'node','start')
    assertions(owner,parent,proof['manifest'])
    assert owner.request('/api/source-grants/'+proof['grant'])['grant']['state']=='restored_frozen'
    usage=owner.request('/api/jobs')['history_capacity'];assert usage['compact_history']['jobs']==1
    return {'passed':True,'original_metadata_and_attempt_preserved':True,'cold_read_frozen_until_explicit_revalidation':True,
        'same_manifest_after_revalidation':True,'historical_grant_not_reactivated':True,'compact_history_credit_retained':True}


def retain_private_evidence(ctx,proof,partial=False):
    # Independent audit later reads raw SQL and Root files without these readers.
    code='''import json,sqlite3,tarfile
from pathlib import Path
p=Path('/opt/ascend-lab-controller');state=Path(json.loads((p/'installation.json').read_text())['state_dir'])
with tarfile.open('/root/cold-history-evidence.tar.gz','x:gz') as archive:
 archive.add(p,arcname='installation');archive.add(state/'jobs.sqlite3',arcname='control/jobs.sqlite3')
 archive.add(state/'config.json',arcname='control/config.json')
 for name in ['cold-register','cold-copy','cold-read','cold-retire','control-history','control-rotate','cold-read-resume']:
  archive.add('/root/'+name+'.json',arcname='plans/'+name+'.json')
Path('/root/cold-history-evidence.tar.gz').chmod(0o600)
print(json.dumps({'captured':True}))
'''
    target=ctx['base']/'private-controller-evidence.tar.gz'
    if target.exists():return {'retained_privately':True,'archive_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
    if not partial:
        ctx['ctl'](ctx['node'],'node','stop');ctx['ctl'](ctx['controller'],'controller','stop')
    if partial:
        # Diagnostic copy only, never a restore-approved live database backup.
        code=code.replace("archive.add(p,arcname='installation');archive.add(state/'jobs.sqlite3',arcname='control/jobs.sqlite3')", "archive.add(p,arcname='installation');archive.add(state/'jobs.sqlite3',arcname='control/jobs.sqlite3')")
        code=code.replace("  archive.add('/root/'+name+'.json',arcname='plans/'+name+'.json')", "  if Path('/root/'+name+'.json').exists():archive.add('/root/'+name+'.json',arcname='plans/'+name+'.json')")
    root(ctx,code)
    encoded=ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-I','-B','-c',
        'import base64;from pathlib import Path;p=Path("/root/cold-history-evidence.tar.gz");assert p.stat().st_size<=32*1024**2;print(base64.b64encode(p.read_bytes()).decode())'],timeout=45).stdout
    with target.open('xb') as out:out.write(base64.b64decode(encoded,validate=False))
    target.chmod(0o600)
    return {'retained_privately':True,'archive_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'diagnostic_partial':partial}

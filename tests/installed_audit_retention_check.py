"""Owned Root audit backlog, confirmed pruning and real process-crash recovery.

Runs only inside the native install fixture, with its controller/Node initially
stopped. Raw archives remain private and are explicitly copied for verification.
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import time


def run(ctx,native,prefix):
    docker=ctx['docker'];api=ctx['api'];ctl=ctx['ctl'];check=ctx['check'];controller=ctx['controller'];node=ctx['node']
    uid=ctx['installed']['uid'];gid=ctx['installed']['gid'];state='/srv/native1/state'
    def python(code,*args,user=None,expected=0):
        result=docker(['exec',*(['--user',user] if user else []),controller,'/usr/bin/python3','-I','-B','-c',code,*map(str,args)],check=False,timeout=45)
        assert result.returncode==expected,result.stderr
        return result
    setup=r'''import json,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]);f=p/'config.json';c=json.loads(f.read_text())
with sqlite3.connect(p/'identities.sqlite3') as db:n=db.execute('SELECT count(*) FROM audit').fetchone()[0]
limit=max(128,n+64);c['audit_retention']['maximum_unarchived_rows']=limit;f.write_text(json.dumps(c));f.chmod(0o600)
print(json.dumps({'limit':limit,'initial':n}))
'''
    limits=json.loads(python(setup,state,user=str(uid)+':'+str(gid)).stdout)
    assert native('start')['ready'] and ctl(node,'node','start')['ready']
    from lab_client import Client
    from environment_contract import lock_for
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    source=ctx['base']/'audit-ir-source';source.mkdir(mode=0o700)
    (source/'ir.py').write_text('import time\ntime.sleep(45)\n'+(ctx['ROOT']/'examples/workflows/ir.py').read_text())
    (source/'input.mlir').write_bytes((ctx['ROOT']/'examples/workflows/input.mlir').read_bytes())
    bundle=client.upload_input(source)
    request={'template':'ir_v1','environment':'worker-python','idempotency_key':'audit-running-ir',
        'parameters':{'input_id':bundle['id'],'argv':['python','ir.py'],
        'lock':lock_for(ctx['configuration']['environment_manifest']),'timeout_seconds':90}}
    status,result=api('/api/jobs',request);assert status==201,result
    job=result['job']['id']
    follower_request={'template':'cpu_demo','environment':'worker-python','idempotency_key':'audit-existing-follower','parameters':{'seconds':1}}
    status,result=api('/api/jobs',follower_request);assert status==201,result;follower=result['job']['id']
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        detail=api('/api/jobs/'+job)[1]['job']
        if detail['state']=='running':break
        time.sleep(.1)
    assert detail['state']=='running';attempt=detail['attempt_id']
    fill=r'''import json,sys
from pathlib import Path
sys.path.insert(0,'/bootstrap')
from auth import AccessControl
state=Path(sys.argv[1]);a=AccessControl(state,json.loads((state/'config.json').read_text()))
while not a.audit_status()['blocks_new_work']:a.record(a.local,'fixture.'+'x'*1024,'audit-test','retained')
print(json.dumps(a.audit_status()));a.close()
'''
    filled=json.loads(python(fill,state,user=str(uid)+':'+str(gid)).stdout);assert filled['retained_rows']>=limits['limit']
    status,value=api('/api/audit-retention');assert status==200 and value['blocks_new_work']
    status,value=api('/api/jobs',{**follower_request,'idempotency_key':'audit-rejected'});assert status==503 and value['error']=='audit_retention_backlog',value
    status,value=api('/api/inputs',{'files':[{'name':'new.bin','size':1,'sha256':hashlib.sha256(b'x').hexdigest()}]});assert status==503,value
    status,value=api('/api/jobs',request);assert status==200 and value['job']['id']==job
    waiting=api('/api/jobs/'+follower)[1]['job'];assert waiting['state']=='queued' and not waiting.get('attempt_id')
    assert api('/api/audit-retention')[1]['retained_rows']==filled['retained_rows']
    from lab_client import atomic_json
    profile=ctx['base']/'audit-client.json';atomic_json(profile,{'controller':ctx['controller_url'],'token':ctx['token']})
    browser_file=ctx['base']/'audit-browser.json'
    browser=subprocess.run(['/bin/bash',str(ctx['ROOT']/'check-browser.sh'),'tests/browser_audit_retention_check.py',
        '--profile',str(profile),'--output',str(browser_file)],cwd=ctx['ROOT'],capture_output=True,text=True,timeout=45)
    (ctx['base']/'audit-browser.log').write_text(browser.stdout+browser.stderr)
    assert browser.returncode==0,'Inspect the private browser log; no repeated task submission'
    browser_proof=json.loads(browser_file.read_text());assert browser_proof['passed'] and browser_proof['created_jobs']==0
    check('Actual audit backlog preserves every row, rejects new job/input without amplification and retains existing task/grant identities')
    assert not native('stop')['running']  # Node remains alive with the original computation/result.
    python("from pathlib import Path;p=Path('/root/private-audit');p.mkdir(mode=0o700)")
    archive='/root/private-audit/batch.jsonl'
    crash_export=r'''import os,sys
sys.path.insert(0,'/bootstrap');import audit_retention as a
def crash(*args):os._exit(77)
a.append=crash;a.export(sys.argv[1],sys.argv[2])
'''
    python(crash_export,prefix,archive,expected=77)
    exported=native('audit-export','--output',archive);assert exported['exported'] and not exported['reused']
    replay=native('audit-export','--output',archive);assert replay['reused'] and replay['sha256']==exported['sha256']
    # Verify private archive fields independently; no account/token table export.
    verify=r'''import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]);raw=p.read_bytes();lines=raw.splitlines();h=json.loads(lines[0]);rows=[json.loads(x) for x in lines[1:]]
assert len(rows)==h['rows'] and all(set(r)=={'id','at','actor','action','target','outcome'} for r in rows)
assert hashlib.sha256(b'\n'.join(lines[1:])+b'\n').hexdigest()==h['rows_sha256']
assert not any(x in raw for x in [b'password_hash',b'node_key',b'lease_token'])
print(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(),'rows':len(rows),'last_id':h['last_id']}))
'''
    manifest=json.loads(python(verify,archive).stdout);assert manifest['sha256']==exported['sha256']
    refused=native('audit-prune','--archive',archive,'--confirm-sha256','0'*64,check=False);assert refused.returncode!=0
    # Kill after DELETE/receipt update with a tiny SQLite cache, before commit.
    crash_prune=r'''import os,sys
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0,'/bootstrap');import audit_retention as a
old=a.context
@contextmanager
def wrapped(prefix):
 with old(prefix) as value:
  value[0].execute('PRAGMA cache_size=1');yield value
a.context=wrapped
def crash(*args):
 journal=Path('/srv/native1/state/identities.sqlite3-journal');assert journal.is_file();assert journal.stat().st_uid==int(sys.argv[4]);os._exit(78)
a.append=crash;a.prune(sys.argv[1],sys.argv[2],sys.argv[3])
'''
    python(crash_prune,prefix,archive,exported['sha256'],uid,expected=78)
    pruned=native('audit-prune','--archive',archive,'--confirm-sha256',exported['sha256'])
    assert pruned['pruned_rows']==exported['rows'] and not pruned['reused']
    assert native('audit-prune','--archive',archive,'--confirm-sha256',exported['sha256'])['reused']
    check('Private audit export recovers publication-before-receipt crash; confirmed prune recovers a killed service-owned journal and repeats without deleting twice')
    # Preserve an explicit private copy outside the ephemeral Role filesystem.
    copy=r'''import os,sys
from pathlib import Path
source=Path(sys.argv[1]);target=Path(sys.argv[2]);assert not target.exists()
fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'wb') as out:out.write(source.read_bytes());out.flush();os.fsync(out.fileno())
'''
    evidence=ctx['controller_state']/'audit-export-proof.jsonl';python(copy,archive,evidence)
    assert native('start')['ready']
    done=ctx['wait'](job);assert done['state']=='succeeded' and done['attempt_id']==attempt,done
    later=ctx['wait'](follower);assert later['state']=='succeeded'
    assert api('/api/audit-retention')[1]['blocks_new_work'] is False
    assert not ctl(node,'node','stop')['running'] and not native('stop')['running']
    # Restore the fixture's original threshold, preserving archive mode.
    python("import json,sys;from pathlib import Path;p=Path(sys.argv[1])/'config.json';c=json.loads(p.read_text());c['audit_retention']['maximum_unarchived_rows']=10000;p.write_text(json.dumps(c));p.chmod(0o600)",state,user=str(uid)+':'+str(gid))
    # Actual historical package, built with its own matching old packaging code.
    old=ctx['base']/'audit-old-source';old.mkdir(mode=0o700)
    packed=subprocess.check_output(['git','archive','481852974396821813dd078fb0c79661689eb986'],cwd=ctx['ROOT'])
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:tar.extractall(old,filter='data')
    package=ctx['base']/'audit-old-server.tar.gz'
    code="import sys;sys.path.insert(0,sys.argv[1]);from deployment import bundle;bundle(sys.argv[2],sys.argv[1])"
    subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(old),str(package)],check=True,capture_output=True,timeout=45)
    # Provision mounts contain only the two original package files, not base.
    docker(['cp',str(package),controller+':/provision/audit-old-server.tar.gz'])
    rejected=native('activate','--archive','/provision/audit-old-server.tar.gz',check=False)
    assert rejected.returncode!=0 and 'Audit archive policy' in rejected.stderr,rejected.stderr
    check('After audit maintenance the original IR/follower each execute once, and an actual pre-archive release is refused without changing state')
    return {'passed':True,'archive':archive,'sha256':exported['sha256'],'rows':exported['rows'],
        'threshold':limits['limit'],'original_job':job,'original_attempt':attempt,'follower_job':follower,
        'backlog_new_job_input_503':True,'replay_no_audit_amplification':True,'existing_node_not_stopped_during_export':True,
        'export_crash_exit':77,'prune_crash_exit':78,'service_owned_journal_verified':True,
        'wrong_sha_refused':True,'same_export_recovered':True,'same_prune_idempotent':True,
        'browser':browser_proof,
        'private_archive_copy':str(evidence),'old_code_activation_refused':True,'threshold_restored':10000}

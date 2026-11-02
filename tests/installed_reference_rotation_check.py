"""Actual installed Root maintenance and SIGKILL recovery of synthetic history."""
import hashlib
import json
from pathlib import Path


def run(ctx):
    docker=ctx['docker'];ctl=ctx['ctl'];controller=ctx['controller'];state=ctx['restored_state']
    prefix='/opt/ascend-lab-controller';uid=ctx['installed']['uid'];gid=ctx['installed']['gid']
    archive='/root/reference-archives/history.json'
    live=ctl(controller,'controller','reference-export','--output',archive,check=False)
    assert live.returncode and 'State is in use' in live.stderr,live.stderr[-1000:]
    ctl(controller,'controller','stop')
    ctl(controller,'controller','backup','--output','/root/backups/rotation-before.tar.gz')
    docker(['exec',controller,'/usr/bin/python3','-I','-c','from pathlib import Path;p=Path("/root/reference-archives");p.mkdir(mode=0o700)'])
    # Add an unapproved synthetic policy reference while stopped. Even a
    # paused policy must prevent maintenance from silently removing its gate.
    modify='''import json,sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
 row=db.execute('SELECT id,record FROM device_evidence_policies').fetchone();v=json.loads(row[1])
 assert 'performance_references' not in v['spec'];v['spec']['performance_references']={'0':sys.argv[2]}
 db.execute('UPDATE device_evidence_policies SET record=? WHERE id=?',(json.dumps(v),row[0]))
'''
    identifier=ctx['performance_reference_proof']['reference_id']
    docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-I','-c',modify,str(state/'jobs.sqlite3'),identifier])
    try:
        blocked=ctl(controller,'controller','reference-export','--output',archive,check=False)
        assert blocked.returncode and 'Retire every policy' in blocked.stderr,blocked.stderr[-1000:]
    finally:
        revert=modify.replace("assert 'performance_references' not in v['spec'];v['spec']['performance_references']={'0':sys.argv[2]}","v['spec'].pop('performance_references')")
        docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-I','-c',revert,str(state/'jobs.sqlite3'),identifier])
    # Preserve actual pre-rotation SQLite with its first restore freeze.
    copy='import sqlite3,sys;src=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True);dst=sqlite3.connect(sys.argv[2]);src.backup(dst);dst.close();src.close()'
    docker(['exec',controller,'/usr/bin/python3','-I','-c',copy,str(state/'jobs.sqlite3'),str(ctx['controller_state']/'rotation-before-proof.sqlite3')])
    kill='''import os,signal,sys
sys.path.insert(0,'/opt/ascend-lab-controller/current')
import reference_maintenance as h
def stop(*args):os.kill(os.getpid(),signal.SIGKILL)
h.save=stop
if sys.argv[1]=='export':h.export(sys.argv[2],sys.argv[3])
else:h.prune(sys.argv[2],sys.argv[3],sys.argv[4])
'''
    interrupted=docker(['exec',controller,'/usr/bin/python3','-I','-c',kill,'export',prefix,archive],check=False)
    assert interrupted.returncode in {137,-9},interrupted.stderr[-1000:]
    first=ctl(controller,'controller','reference-export','--output',archive)
    assert first['exported'] and first['references']==1 and first['events']==1 and first['generation']==0
    assert ctl(controller,'controller','reference-export','--output',archive)==first
    bad=ctl(controller,'controller','reference-prune','--archive',archive,'--confirm-sha256','0'*64,check=False)
    assert bad.returncode and 'checksum differs' in bad.stderr
    interrupted=docker(['exec',controller,'/usr/bin/python3','-I','-c',kill,'prune',prefix,archive,first['sha256']],check=False)
    assert interrupted.returncode in {137,-9},interrupted.stderr[-1000:]
    # Inspect journal ownership without opening/recovering SQLite as Root.
    journal='import os,sys;from pathlib import Path;p=Path(sys.argv[1]);print("absent" if not p.exists() else str(p.stat().st_uid))'
    owner=docker(['exec',controller,'/usr/bin/python3','-I','-c',journal,str(state/'jobs.sqlite3-journal')]).stdout.strip()
    assert owner=='absent' or owner==str(uid),owner
    done=ctl(controller,'controller','reference-prune','--archive',archive,'--confirm-sha256',first['sha256'])
    assert done['pruned'] and not done['reused'] and done['generation']==1
    assert ctl(controller,'controller','reference-prune','--archive',archive,'--confirm-sha256',first['sha256'])['reused']
    rejected='/var/lib/ascend-lab-rotation-rejected'
    failed=ctl(controller,'controller','restore','--archive','/root/backups/rotation-before.tar.gz','--new-state-dir',rejected,check=False)
    assert failed.returncode and 'rotation checkpoint' in failed.stderr,failed.stderr[-1000:]
    docker(['exec',controller,'/usr/bin/python3','-I','-c','import sys;from pathlib import Path;assert not Path(sys.argv[1]).exists()',rejected])
    ctl(controller,'controller','backup','--output','/root/backups/rotation-latest.tar.gz')
    target=Path('/var/lib/ascend-lab-controller-rotation-restored')
    ctl(controller,'controller','restore','--archive','/root/backups/rotation-latest.tar.gz','--new-state-dir',str(target))
    old=ctl(controller,'controller','reference-prune','--archive',archive,'--confirm-sha256',first['sha256'],check=False)
    assert old.returncode and 'restore epoch' in old.stderr,old.stderr[-1000:]
    ctx['restored_state']=target
    started=ctl(controller,'controller','start');assert started['ready']
    assert ctx['api']('/api/performance-references')[1]['total']==0
    # Existing original CPU results and immutable archives still accessible.
    for ident in ctx['measurement_proof']['job_ids']:
        job=ctx['api']('/api/jobs/'+ident)[1]['job'];assert job['state']=='succeeded' and job['resources']['npu']==0
    # Export archive bytes into this task's private evidence, never public docs.
    raw=docker(['exec',controller,'/usr/bin/python3','-I','-c','import sys;from pathlib import Path;sys.stdout.write(Path(sys.argv[1]).read_text())',archive]).stdout.encode()
    assert hashlib.sha256(raw).hexdigest()==first['sha256']
    path=ctx['base']/'reference-history-private.json';path.write_bytes(raw);path.chmod(0o600)
    return {'passed':True,'synthetic_history_only':True,'archive_sha256':first['sha256'],
        'live_controller_rejected':True,'paused_policy_reference_rejected':True,'wrong_sha_rejected':True,
        'actual_sigkill_export_recovered':True,'actual_sigkill_prune_rolled_back_and_recovered':True,
        'hot_journal_service_uid':owner,'generation':done['generation'],'head_sha256':done['head_sha256'],
        'repeat_prune_idempotent':True,'older_backup_rejected_before_rebinding':True,
        'latest_restore_invalidated_old_file':True,'original_cpu_results_retained':True,
        'references_after_restore':0,'npu_kernel_launches':0,'real_npu_approval':False}

"""Real control-state ext4 exhaustion: local success/deadline, same-attempt recovery.

Only a newly installed, stopped controller's private state is copied and bound
inside its owned role namespace. No host/shared mount or retained data is swept.
"""
import hashlib
import json
from pathlib import Path
import time


def run(ctx):
    api=ctx['api'];ctl=ctx['ctl'];docker=ctx['docker'];secret=ctx['secret_json'];check=ctx['check']
    controller=ctx['controller'];node=ctx['node'];state=ctx['restored_state'];node_state=ctx['node_parent']/'restored'
    assert ctx['node_restored']['previous_state_retained']==str(ctx['node_state'])
    # Both services were stopped after the regular backup/restore acceptance.
    # cp -a retains the installed UID/modes; the original stopped state remains.
    prepare=r'''import json,os,subprocess,sys
from pathlib import Path
root=Path('/central-pressure');state=Path(sys.argv[1])
assert sorted(x.name for x in root.iterdir())==['lost+found']
assert state.is_dir() and not state.is_symlink() and not (state/'jobs.sqlite3').is_symlink()
(root/'lost+found').rmdir()
subprocess.run(['cp','-a',str(state),str(root/'state')],check=True)
assert (root/'state/jobs.sqlite3').stat().st_dev!=state.stat().st_dev
subprocess.run(['mount','--bind',str(root/'state'),str(state)],check=True)
s=os.statvfs(root)
print(json.dumps({'capacity_bytes':s.f_blocks*s.f_frsize,'state_device':state.stat().st_dev,
 'original_control_device':Path(sys.argv[2]).stat().st_dev,'state_inode':state.stat().st_ino}))
'''
    separation=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',prepare,str(state),str(ctx['controller_state'])]).stdout)
    separation['node_device']=int(docker(['exec',node,'/usr/bin/python3','-I','-c','import os,sys;print(os.stat(sys.argv[1]).st_dev)',str(node_state)]).stdout)
    assert separation['state_device']!=separation['node_device'] and separation['capacity_bytes']<=128*1024**2
    assert ctl(controller,'controller','start')['ready'] and ctl(node,'node','start')['ready']
    identity=secret(controller,state/'service-process.json')
    caps=r'''import json,sys
from pathlib import Path
s=dict(x.split(':',1) for x in (Path('/proc')/sys.argv[1]/'status').read_text().splitlines() if ':' in x)
print(json.dumps({'uids':list(map(int,s['Uid'].split())),'effective':int(s['CapEff'].strip(),16),'permitted':int(s['CapPrm'].strip(),16)}))
'''
    principal=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',caps,str(identity['pid'])]).stdout)
    assert principal['uids']==[ctx['installed']['uid']]*4 and principal['effective']==principal['permitted']==0
    check('Actual unprivileged controller runs on a bounded owned ext4 state filesystem, separate from Node control/spool')
    fill=r'''import errno,json,os
from pathlib import Path
p=Path('/central-pressure/filler');fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
try:
 while True:os.write(fd,b'x'*65536)
except OSError as e:assert e.errno==errno.ENOSPC
finally:
 try:os.fsync(fd)
 except OSError as e:assert e.errno==errno.ENOSPC
 os.close(fd)
s=p.stat();v=os.statvfs(p.parent)
print(json.dumps({'device':s.st_dev,'inode':s.st_ino,'bytes':s.st_size,'free_bytes':v.f_bavail*v.f_frsize}))
'''
    release=r'''import os,sys
from pathlib import Path
p=Path('/central-pressure/filler');s=p.stat()
assert [s.st_dev,s.st_ino]==list(map(int,sys.argv[1:]))
p.unlink();fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
'''
    from lab_client import Client
    from environment_contract import lock_for
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    lock=lock_for(ctx['configuration']['environment_manifest']);proof=[]
    for label,expected,timeout,sleep in [('success','succeeded',45,10),('deadline','timed_out',10,60)]:
        source=ctx['base']/('control-pressure-'+label);source.mkdir(mode=0o700)
        script="""import json,os,time,uuid
from pathlib import Path
p=Path(os.environ['LAB_JOB_DIR']);marker=uuid.uuid4().hex
(p/'ir/marker.json').write_text(json.dumps({'marker':marker}))
print('original-run-marker='+marker,flush=True)
time.sleep(SLEEP)
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/marker.json']}))
""".replace('SLEEP',str(sleep))
        (source/'task.py').write_text(script)
        bundle=client.upload_input(source)
        request={'template':'ir_v1','environment':'worker-python','idempotency_key':'control-full-'+label,
                 'parameters':{'input_id':bundle['id'],'argv':['python','task.py'],'lock':lock,'timeout_seconds':timeout}}
        status,value=api('/api/jobs',request);assert status==201,value
        identifier=value['job']['id']
        status,value=api('/api/jobs',{'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'control-full-follower-'+label})
        assert status==201,value
        follower=value['job']['id'];deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            detail=api('/api/jobs/'+identifier)[1]
            if detail['job']['state']=='running':break
            time.sleep(.1)
        assert detail['job']['state']=='running',detail['job']['state']
        attempt=detail['job']['attempt_id'];control=node_state/'attempts'/attempt/'control'
        grant=secret(node,control/'grant.json');auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
        filler=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',fill]).stdout)
        assert filler['free_bytes']==0 and filler['bytes']<128*1024**2
        status,failed=api('/api/worker/update',{**auth,'running':True},extra_headers={'Authorization':'Bearer '+ctx['configuration']['node_key']})
        assert status==507 and failed['error']=='storage_unavailable',failed
        status,new=api('/api/jobs',{'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'control-full-rejected-'+label})
        assert status==507 and new['error']=='storage_unavailable',new
        # Observe local result while central SQLite cannot commit even a journal.
        # A generic worker exception would abandon tick(), including its deadline.
        deadline=time.monotonic()+35;local={};outcome={}
        while time.monotonic()<deadline:
            local=secret(node,control/'state.json')
            if local.get('stage')=='cleaned':
                outcome=secret(node,control/'result.json');break
            time.sleep(.2)
        assert local.get('stage')=='cleaned' and not local.get('controller_acknowledged'),local.get('stage')
        assert outcome['status']==expected and outcome['termination_confirmed'] and outcome['execution_cleanup_confirmed'],outcome
        spool=Path(ctx['spool_policy']['path'])/'attempts'/attempt
        marker=secret(node,spool/'artifacts/ir/marker.json')['marker']
        assert len(marker)==32
        held=api('/api/jobs/'+identifier)[1]
        assert held['job']['attempt_id']==attempt and len(held['attempts'])==1
        assert held['job']['state'] not in {'succeeded','failed','timed_out','cancelled'}
        assert not api('/api/jobs/'+follower)[1]['attempts']
        assert api('/api/jobs')[1]['reserved']['cpu']==1
        check('Control SQLITE_FULL/HTTP507: original '+label+' reaches local '+expected+' and cleanup; original lease and queued follower remain held')
        docker(['exec',controller,'/usr/bin/python3','-I','-c',release,str(filler['device']),str(filler['inode'])])
        done=ctx['wait'](identifier);assert done['state']==expected
        later=ctx['wait'](follower);assert later['state']=='succeeded'
        status,replay=api('/api/jobs',request);assert status==200 and replay['reused'] and replay['job']['id']==identifier
        detail=api('/api/jobs/'+identifier)[1];assert len(detail['attempts'])==1 and detail['job']['attempt_id']==attempt
        assert len(api('/api/jobs/'+follower)[1]['attempts'])==1
        archived=api('/api/jobs/'+identifier+'/artifact?name=ir%2Fmarker.json')[1]
        assert archived['marker']==marker
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            local=secret(node,control/'state.json')
            if local.get('artifact_gc',{}).get('state')=='purged':break
            time.sleep(.1)
        assert local['controller_acknowledged'] and local['artifact_gc']['state']=='purged'
        assert local['artifact_receipt']['attempt_id']==attempt
        assert api('/api/jobs')[1]['reserved']['cpu']==0
        proof.append({'case':label,'job_id':identifier,'attempt_id':attempt,'follower_job_id':follower,
            'local_status_while_full':expected,'worker_update_http':507,'new_submission_http':507,
            'local_cleanup_while_full':True,'lease_held_until_ack':True,'follower_no_attempt_while_full':True,
            'same_attempt':True,'idempotent_replay_reused':True,'same_marker':True,'ack_gc_confirmed':True,
            'filler_identity':filler,'archived_marker_sha256':hashlib.sha256(json.dumps(archived,sort_keys=True).encode()).hexdigest()})
        check('Capacity restored: original '+label+' resumes transfers/ACK/GC, idempotent replay reuses it, follower executes once')
    ctl(node,'node','stop');ctl(controller,'controller','stop')
    # The pressure state lives inside the role namespace. Keep an explicit,
    # stopped read-only SQLite export for primary independent verification.
    export=r'''import sqlite3,sys
from pathlib import Path
source=Path(sys.argv[1]);target=Path(sys.argv[2]);assert not target.exists()
a=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True);b=sqlite3.connect(target)
a.backup(b);assert b.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert b.execute("SELECT count(*) FROM jobs WHERE idem_key LIKE 'control-full-rejected-%'").fetchone()[0]==0
b.close();a.close();target.chmod(0o600)
'''
    target=ctx['controller_state']/'control-pressure-proof.sqlite3'
    docker(['exec',controller,'/usr/bin/python3','-I','-c',export,str(state/'jobs.sqlite3'),str(target)])
    return {'passed':True,'cases':proof,'control_filesystem':separation,'actual_service_identity':principal,
            'new_uncommitted_jobs_absent':True,'stopped_sqlite_export':str(target),
            'scope':'Same-host owned bounded ext4; actual control SQLITE_FULL, CPU success/deadline and recovery. No power-loss, failover, production storage migration or automatic retention.'}

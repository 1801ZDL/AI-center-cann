"""Owned node spool fault injection; verify one compute survives full export disk."""
import base64
import hashlib
import json
import time
from pathlib import Path


def run(ctx):
    api=ctx['api'];docker=ctx['docker'];node=ctx['node'];root=ctx['ROOT'];policy=ctx['spool_policy']
    source={name:(root/'examples/workflows'/name).read_bytes() for name in ['ir.py','input.mlir']}
    source['hold.py']=b'import json,os,runpy,time\nfrom pathlib import Path\nrunpy.run_path("ir.py",run_name="__main__")\ncounter=Path(os.environ["LAB_JOB_DIR"])/"ir/once.json"\nold=json.loads(counter.read_text())["launches"] if counter.exists() else 0\ncounter.write_text(json.dumps({"launches":old+1,"scope":"one real offline IR invocation"}))\ntime.sleep(25)\n'
    status,response=api('/api/inputs',{'files':[{'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in source.items()]});assert status==201,response
    bundle=response['input']['id']
    for name,data in source.items():assert api('/api/inputs/'+bundle+'/blob',{'name':name,'offset':0,'data':base64.b64encode(data).decode()})[0]==200
    assert api('/api/inputs/'+bundle+'/seal',{})[1]['input']['sealed']
    request={**ctx['request'],'parameters':{**ctx['request']['parameters'],'input_id':bundle,'argv':['python','hold.py']},'idempotency_key':'node-spool-full-original-computation'}
    status,response=api('/api/jobs',request);assert status==201,response
    job_id=response['job']['id'];deadline=time.monotonic()+30;before=None
    while time.monotonic()<deadline:
        job=api('/api/jobs/'+job_id)[1]['job']
        if job.get('attempt_id'):
            container='ascend-lab-'+job['attempt_id'][8:]
            ready=docker(['exec','--user','23003:23003',container,'/usr/bin/python3','-I','-c',
                'import json;from pathlib import Path;v=json.loads(Path("/work/ir/once.json").read_text());assert v["launches"]==1'],check=False)
            if ready.returncode==0:
                before=json.loads(docker(['inspect',container]).stdout)[0];break
        time.sleep(.2)
    assert before and before['State']['Running'],'Original IR did not enter its fixed hold window'
    attempt=job['attempt_id'];control=ctx['node_state']/'attempts'/attempt/'control'
    filler=Path(policy['path'])/'attempts'/attempt/'artifacts/.spool-pressure'
    fill='import errno,json,os,sys;from pathlib import Path;p=Path(sys.argv[1]);fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);s=os.fstat(fd);assert s.st_dev==int(sys.argv[2]);length=0;code=None\ntry:\n for chunk in [1024**2,4096]:\n  while length<2*1024**3:\n   try:os.posix_fallocate(fd,length,chunk);length+=chunk\n   except OSError as e:\n    assert e.errno==errno.ENOSPC;code=e.errno;break\n os.fsync(fd);s=os.fstat(fd)\nfinally:os.close(fd)\nassert code==errno.ENOSPC;print(json.dumps({"device":s.st_dev,"inode":s.st_ino,"bytes":s.st_size,"allocated_bytes":s.st_blocks*512,"allocation_method":"posix_fallocate","errno":code}))'

    full=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',fill,str(filler),str(policy['device'])]).stdout)
    deadline=time.monotonic()+45;pending=None
    while time.monotonic()<deadline:
        state=ctx['secret_json'](node,control/'state.json')
        current=api('/api/jobs/'+job_id)[1]
        if state.get('output_pending') and state.get('execution_result') and current['job']['state']=='needs_attention':pending=state;break
        time.sleep(.2)
    assert pending and pending['execution_result']['status']=='succeeded'
    ended=json.loads(docker(['inspect',container]).stdout)[0]
    assert not ended['State']['Running'] and ended['State']['Pid']==0 and ended['State']['ExitCode']==0
    assert ended['State']['StartedAt']==before['State']['StartedAt']
    assert len(current['attempts'])==1 and not current['job']['result']
    read='from pathlib import Path;import hashlib,json;v=Path("/work/ir/canonical.mlir").read_bytes();o=json.loads(Path("/work/ir/once.json").read_text());assert o["launches"]==1;print(json.dumps({"sha256":hashlib.sha256(v).hexdigest(),"bytes":len(v)}))'
    # Stopped containers cannot exec. The trusted node reads only this granted
    # work path selected by the assigned workspace, never a client path.
    workspace=next(s for s in ctx['slots'] if s['id']==current['job']['allocation']['workspace']['slot_id'])
    work=Path(workspace['path'])/attempt
    inspect_work='import hashlib,json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=(p/"ir/canonical.mlir").read_bytes();o=json.loads((p/"ir/once.json").read_text());assert o["launches"]==1;print(json.dumps({"sha256":hashlib.sha256(v).hexdigest(),"bytes":len(v)}))'
    retained=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',inspect_work,str(work)]).stdout)
    unavailable=api('/api/nodes')[1]['nodes'];report=next(n for n in unavailable if n['id']==ctx['enrollment']['node_id'])
    assert report['spool_storage']['free_bytes']<4096
    from lab_client import atomic_json
    import os
    atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/node-spool-ui.json',{'source_job':job_id,'source_parent':Path(os.environ['LAB_JOB_DIR']).name,
        'scope':'Archived actual pending node spool test, not live Main enrollment','node':report,'job':current['job']})
    clean='import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(sys.argv[2]);s=p.lstat();assert (s.st_dev,s.st_ino,s.st_size,s.st_uid,s.st_nlink)==(v["device"],v["inode"],v["bytes"],0,1);p.unlink()'
    docker(['exec',node,'/usr/bin/python3','-I','-c',clean,str(filler),json.dumps(full)])
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        final=api('/api/jobs/'+job_id)[1]['job']
        if final['state'] in {'succeeded','failed','cancelled','timed_out'}:break
        assert final['state'] in {'needs_attention','running','reserved'},final
        time.sleep(.2)
    assert final['state']=='succeeded',final
    details=api('/api/jobs/'+job_id)[1];assert len(details['attempts'])==1 and final['attempt_id']==attempt
    artifact=api('/api/jobs/'+job_id+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    assert hashlib.sha256(artifact).hexdigest()==retained['sha256']
    once=api('/api/jobs/'+job_id+'/artifact?name=ir%2Fonce.json')[1];assert once['launches']==1
    # Controller finished_at is terminal archive acknowledgement; the original
    # container completion time has its own field and must survive recovery.
    assert final['result']['execution_finished_at']==pending['execution_result']['finished_at']
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        state=ctx['secret_json'](node,control/'state.json')
        if state.get('artifact_gc',{}).get('state')=='purged':break
        time.sleep(.2)
    assert state.get('artifact_gc',{}).get('state')=='purged'
    ctx['ctl'](node,'node','stop')
    docker(['exec',node,'umount',policy['path']])
    assert docker(['exec',node,'/usr/bin/python3','-I','-c','from pathlib import Path;import sys;assert not list(Path(sys.argv[1]).iterdir())',policy['path']]).returncode==0
    assert ctx['ctl'](node,'node','start')['ready']
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        nodes=api('/api/nodes')[1]['nodes'];reading=next(n for n in nodes if n['id']==ctx['enrollment']['node_id'])['spool_storage']
        if reading['status']=='error':break
        time.sleep(.2)
    assert reading['status']=='error' and reading['free_bytes']==reading['free_inodes']==0
    missing_request={**ctx['request'],'idempotency_key':'node-spool-missing-original-waiting-job'}
    status,response=api('/api/jobs',missing_request);assert status==201,response
    blocked_id=response['job']['id'];time.sleep(1);blocked=api('/api/jobs/'+blocked_id)[1]
    assert blocked['job']['state']=='queued' and not blocked['attempts']
    ctx['ctl'](node,'node','stop')
    docker(['exec',node,'mount','--bind','/spool-proof-alias',policy['path']])
    assert ctx['ctl'](node,'node','start')['ready']
    resumed=ctx['wait'](blocked_id);assert resumed['state']=='succeeded',resumed
    return {'passed':True,'job_id':job_id,'attempt_id':attempt,'native_enospc':full['errno'],
        'filled_bytes':full['bytes'],'pending_original_status':pending['execution_result']['status'],
        'pending_control_record_written':True,'workspace_retained':True,'same_container_started_at':before['State']['StartedAt'],
        'attempts':1,'launches':1,'original_finished_at_preserved':True,'archived_ir':retained,
        'missing_mount_heartbeat_survived':True,'missing_mount_no_fallback':True,'missing_mount_job_id':blocked_id,
        'missing_mount_zero_attempts_then_same_job_resumed':True,
        'scope':'One actual CPU BishengIR operation; finite node spool full, control/work separate, exact owned filler removed, original export resumed. No NPU or host reboot.'}

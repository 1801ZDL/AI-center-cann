"""Actual payload-disk ENOSPC via installed HTTP/worker protocols, no compute rerun."""
import base64
import hashlib
import json
from pathlib import Path
import time


def run(context):
    api=context['api'];docker=context['docker'];ctl=context['ctl'];secret=context['secret_json'];check=context['check']
    controller=context['controller'];node=context['node'];state=context['restored_state'];node_state=context['node_state']
    user=context['installed']['uid'];group=context['installed']['gid'];configuration=context['configuration']
    assert ctl(controller,'controller','start')['ready']
    process=secret(controller,state/'service-process.json')
    inspect_caps='import json,sys;from pathlib import Path;s=dict(line.split(":",1) for line in (Path("/proc")/sys.argv[1]/"status").read_text().splitlines() if ":" in line);print(json.dumps({"uids":[int(x) for x in s["Uid"].split()],"effective":int(s["CapEff"].strip(),16),"permitted":int(s["CapPrm"].strip(),16)}))'
    service_caps=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',inspect_caps,str(process['pid'])]).stdout)
    assert service_caps['uids']==[user]*4 and service_caps['effective']==service_caps['permitted']==0
    # Previous lifecycle/backup checks have completed. Node remains stopped while
    # these two new, explicitly owned payload directories are prepared.
    data=b'p'*(512*1024);prefix=data[:256*1024]
    status,value=api('/api/inputs',{'files':[{'name':'payload.bin','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]})
    assert status==201,value
    partial=value['input']['id']
    body={'name':'payload.bin','offset':0,'data':base64.b64encode(prefix).decode()}
    assert api('/api/inputs/'+partial+'/blob',body)[0]==200
    script=b'''import json,os,uuid\nfrom pathlib import Path\np=Path(os.environ['LAB_JOB_DIR'])\nwith (p/'ir/pressure.bin').open('wb') as f:\n for _ in range(128):f.write(b'q'*65536)\n(p/'ir/run-marker.json').write_text(json.dumps({'marker':uuid.uuid4().hex}))\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/pressure.bin','ir/run-marker.json']}))\n'''
    status,value=api('/api/inputs',{'files':[{'name':'pressure.py','size':len(script),'sha256':hashlib.sha256(script).hexdigest()}]})
    assert status==201,value
    source=value['input']['id']
    assert api('/api/inputs/'+source+'/blob',{'name':'pressure.py','offset':0,'data':base64.b64encode(script).decode()})[0]==200
    assert api('/api/inputs/'+source+'/seal',{})[0]==200
    request={'template':'ir_v1','environment':'worker-python','idempotency_key':'central-disk-pressure-original',
             'parameters':{'input_id':source,'argv':['python','pressure.py'],'lock':{'python':'3.10.12','image_sha256':context['image']},'timeout_seconds':60}}
    status,value=api('/api/jobs',request);assert status==201,value
    job=value['job']['id']
    status,value=api('/api/jobs',{'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'central-disk-pressure-follower'})
    assert status==201,value
    follower=value['job']['id']
    # Only the new archive and partial-input directories move. All other
    # archives/control databases remain available on their original filesystem.
    prepare=r'''import os,sys,shutil,subprocess,json
from pathlib import Path
root=Path('/central-pressure');assert sorted(x.name for x in root.iterdir())==['lost+found'];(root/'lost+found').rmdir()
uid,gid=map(int,sys.argv[3:5]);originals=[Path(sys.argv[1]),Path(sys.argv[2])]
for i,source in enumerate(originals):
 target=root/('archive' if i==0 else 'input');shutil.copytree(source,target,symlinks=True)
 for p in [target,*target.rglob('*')]:
  assert not p.is_symlink();os.chown(p,uid,gid)
 subprocess.run(['mount','--bind',str(target),str(source)],check=True)
assert root.stat().st_dev!=Path(sys.argv[5]).stat().st_dev
print(json.dumps({'payload_device':root.stat().st_dev,'control_device':Path(sys.argv[5]).stat().st_dev,'capacity':os.statvfs(root).f_blocks*os.statvfs(root).f_frsize}))
'''
    separation=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',prepare,str(state/'jobs'/job),str(state/'inputs'/partial),str(user),str(group),str(state/'jobs.sqlite3')]).stdout)
    assert separation['payload_device']!=separation['control_device']
    fill=r'''import os,errno,json
from pathlib import Path
p=Path('/central-pressure/filler');fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);written=0
try:
 while True:written+=os.write(fd,b'x'*65536)
except OSError as e:assert e.errno==errno.ENOSPC
finally:
 try:os.fsync(fd)
 except OSError as e:assert e.errno==errno.ENOSPC
 os.close(fd)
s=p.stat();print(json.dumps({'device':s.st_dev,'inode':s.st_ino,'bytes':s.st_size,'free_bytes':os.statvfs(p.parent).f_bavail*os.statvfs(p.parent).f_frsize}))
'''
    filler=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',fill]).stdout)
    assert filler['free_bytes']==0 and filler['bytes']<32*1024**2
    body={'name':'payload.bin','offset':len(prefix),'data':base64.b64encode(data[len(prefix):]).decode()}
    status,failed_input=api('/api/inputs/'+partial+'/blob',body)
    assert status==507 and failed_input['error']=='storage_unavailable',failed_input
    assert api('/api/inputs/'+partial)[1]['uploaded']['payload.bin']==len(prefix)
    check('Real dedicated ext4 ENOSPC returns bounded HTTP507 for input; committed prefix/offset stay intact and control database remains available')
    assert ctl(node,'node','start')['ready']
    deadline=time.monotonic()+60;detail=None
    while time.monotonic()<deadline:
        detail=api('/api/jobs/'+job)[1]
        if detail['job']['state']=='needs_attention':break
        time.sleep(.2)
    assert detail['job']['state']=='needs_attention',detail
    attempt=detail['job']['attempt_id'];control=node_state/'attempts'/attempt/'control'
    local=secret(node,control/'state.json');saved=secret(node,control/'result.json');grant=secret(node,control/'grant.json')
    assert local['stage']=='cleaned' and not local.get('controller_acknowledged')
    assert saved['status']=='succeeded' and saved['exit_code']==0
    environment=secret(node,control/'artifacts/environment.json')
    marker=secret(node,control/'artifacts/ir/run-marker.json')['marker']
    assert environment['task_uid']==23003
    assert not api('/api/jobs/'+follower)[1]['attempts']
    auth={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
    status,failed_blob=api('/api/worker/blob',{**auth,'name':'environment.json','offset':0,
             'data':base64.b64encode(json.dumps(environment).encode()).decode()},extra_headers={'Authorization':'Bearer '+configuration['node_key']})
    assert status==507 and failed_blob['error']=='storage_unavailable',failed_blob
    held=api('/api/jobs')[1]['reserved'];assert held['cpu']==1 and held['memory_mib']>0
    check('Actual task finishes once; archive HTTP507 retains node result/artifacts and resource lease while the next task has zero attempts')
    ctl(node,'node','stop');assert ctl(node,'node','start')['ready']
    time.sleep(1)
    assert len(api('/api/jobs/'+job)[1]['attempts'])==1
    assert not api('/api/jobs/'+follower)[1]['attempts']
    # Unlink only the filler whose dev/inode was recorded; never purge user data.
    release='import os,json,sys;from pathlib import Path;p=Path("/central-pressure/filler");s=p.stat();assert [s.st_dev,s.st_ino]==list(map(int,sys.argv[1:]));p.unlink();f=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(f);os.close(f)'
    docker(['exec',controller,'/usr/bin/python3','-I','-c',release,str(filler['device']),str(filler['inode'])])
    assert api('/api/inputs/'+partial+'/blob',body)[0]==200
    assert api('/api/inputs/'+partial+'/seal',{})[1]['input']['sealed']
    def terminal(identifier):
        until=time.monotonic()+60
        while time.monotonic()<until:
            current=api('/api/jobs/'+identifier)[1]
            if current['job']['state']=='succeeded':return current
            time.sleep(.2)
        raise AssertionError('Observe original storage-recovery job '+identifier+'; never resubmit computation')
    done=terminal(job);later=terminal(follower)
    assert len(done['attempts'])==len(later['attempts'])==1 and done['job']['attempt_id']==attempt
    assert api('/api/jobs/'+job+'/artifact?name=ir%2Frun-marker.json')[1]['marker']==marker
    payload=api('/api/jobs/'+job+'/artifact?name=ir%2Fpressure.bin',raw=True)[1]
    expected=hashlib.sha256(b'q'*(8*1024**2)).hexdigest();assert len(payload)==8*1024**2 and hashlib.sha256(payload).hexdigest()==expected
    until=time.monotonic()+20
    while time.monotonic()<until:
        final=secret(node,control/'state.json')
        if final.get('artifact_gc',{}).get('state')=='purged':break
        time.sleep(.2)
    assert final['controller_acknowledged'] and final['artifact_gc']['state']=='purged'
    assert final['artifact_receipt']['attempt_id']==attempt
    assert api('/api/jobs')[1]['reserved']['cpu']==0
    check('Node restart and capacity restoration resume original transfers, preserve marker/SHA, acknowledge and GC copies, then run the queued follower once')
    ctl(node,'node','stop');ctl(controller,'controller','stop')
    return {'passed':True,'job_id':job,'attempt_id':attempt,'follower_job_id':follower,'input_id':partial,
            'input_http_status':507,'node_blob_http_status':507,'committed_input_prefix_bytes':len(prefix),
            'task_execution_succeeded_before_upload_failure':True,'node_restarted_while_full':True,
            'same_attempt_after_recovery':True,'same_run_marker':True,'payload_bytes':len(payload),'payload_sha256':expected,
            'node_ack_gc_verified':True,'follower_waited_without_attempt':True,'control_payload_separation':separation,
            'filler_bytes':filler['bytes'],'service_capabilities':service_caps,'npu_kernel_launches':0,'backup_includes_pressure_mounts':False,
            'scope':'Root fault injection mounts only two new payload directories in a private controller namespace; no production payload-mount installer/backup or project-quota claim'}

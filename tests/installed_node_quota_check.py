"""Actual installed UID CAS, warm cache preservation and repeated SIGKILLs."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tarfile
import time

from installed_cache_check import submit,result
from lab_client import Client,ClientHTTPError,atomic_json
from node_quota_admin import JOURNAL,HISTORY,INTENT


def run(ctx):
    docker=ctx['docker'];ctl=ctx['ctl'];api=ctx['api'];node=ctx['node'];controller=ctx['controller'];base=ctx['base']
    node_prefix='/opt/ascend-lab-node';central_prefix='/opt/ascend-lab-controller';node_id=ctx['enrollment']['node_id']
    state=str(ctx['node_state']);proof=ctx['cache_proof'];request=copy.deepcopy(proof['request']);key=proof['cache_key']
    input_sha=__import__('hashlib').sha256((ctx['ROOT']/'examples/workflows/input.mlir').read_bytes()).hexdigest()
    work_slot=next(s for s in ctx['slots'] if s['quota_uid']==23003)
    cache_slot=next(s for s in ctx['cache_policy']['slots'] if s['quota_uid']==23003)
    namespace=str(Path(cache_slot['path'])/key);filler_dir=namespace+'/triton'
    records=[];crashes=[];jobs=[]
    def node_cli(*args,check=True):return docker(['exec',node,'/usr/bin/python3','-B',node_prefix+'/current/servicectl',*args,'--prefix',node_prefix],check=check)
    def inspect():
        script='''import hashlib,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]+"/current")
from deployment import regular_json
from node_owner_admin import projection
from node_quota_admin import HISTORY,JOURNAL
import filesystem_quota as q
c=regular_json(Path(sys.argv[2])/"config.json",private=True);m=regular_json(Path(sys.argv[1])/"installation.json");out={"projection":projection(c),"revision":m.get("node_quota_revision",0),"journal":(Path(sys.argv[2])/JOURNAL).exists(),"quota":{},"cache_files":{}}
for kind in ["workspace","persistent_cache"]:
 out["quota"][kind]=[]
 for s in c.get(kind,{}).get("slots",[]):
  fd=os.open(s["path"],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:v=q.read(fd,s["quota_uid"])
  finally:os.close(fd)
  out["quota"][kind].append({k:v[k] for k in ["uid","byte_hard","inode_hard","bytes_used","inodes_used"]})
  if kind=="persistent_cache":
   p=Path(s["path"])/sys.argv[3];f=p/"triton"/sys.argv[4]/"canonical.mlir"
   if f.exists():
    st=f.lstat();out["cache_files"][str(s["quota_uid"]) ]={"sha256":hashlib.sha256(f.read_bytes()).hexdigest(),"inode":st.st_ino,"device":st.st_dev,"uid":st.st_uid,"namespace_inode":p.stat().st_ino}
out["history"]=sorted(p.name for p in (Path(sys.argv[2])/HISTORY).glob("*.json"));print(json.dumps(out))
'''
        return json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',script,node_prefix,state,key,input_sha]).stdout)
    def make(label,kind,mib,inodes):
        path='/provision/quota-'+label+'.json'
        reply=node_cli('node-quota-plan','--owner','proof_user','--kind',kind,'--mib',str(mib),'--inodes',str(inodes),'--output',path)
        value=json.loads(reply.stdout);assert value['read_only'];plan=ctx['secret_json'](node,Path(path))
        return path,plan
    def approve(label,path,plan):
        central_file=ctx['controller_state']/('quota-'+label+'.json')
        writer='import sys;from pathlib import Path;p=Path(sys.argv[1]);assert not p.exists();p.write_text(sys.stdin.read());p.chmod(0o600)'
        docker(['exec','-i','--user',str(ctx['installed']['uid'])+':'+str(ctx['installed']['gid']),controller,
            '/usr/bin/python3','-I','-c',writer,str(central_file)],input=json.dumps(plan))
        reply=ctl(controller,'controller','admin','--','workerctl','node-quota-approve',node_id,'--plan',str(central_file))
        assert reply['approval']['plan_id']==plan['id'] and not reply['reused']
        again=ctl(controller,'controller','admin','--','workerctl','node-quota-approve',node_id,'--plan',str(central_file))
        assert again['reused'] and again['approval']==reply['approval']
        return reply['approval']
    def observed(plan):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            value=ctl(controller,'controller','admin','--','workerctl','node-quota-status',node_id,'--plan-id',plan['id'])
            if value['state']=='observed':return value
            time.sleep(.2)
        raise AssertionError('Inspect the original Node policy/heartbeat; no replacement plan')
    def actor(mode,expected=None):
        script='''import errno,json,os,resource,subprocess,sys
p=sys.argv[1];mode=sys.argv[2];expected=json.loads(sys.argv[3])
def drop():
 os.chdir(p);os.setgroups([]);os.setgid(23003);os.setuid(23003);resource.setrlimit(resource.RLIMIT_CORE,(0,0))
code="""import errno,json,os,stat,sys
name='node-quota-pressure.bin';mode=sys.argv[1];expected=json.loads(sys.argv[2])
if mode=='remove':
 s=os.lstat(name);assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_uid==23003 and [s.st_dev,s.st_ino,s.st_size]==[expected[k] for k in ['device','inode','bytes']];os.unlink(name);print(json.dumps({'removed':True,'uid':os.getuid()}))
else:
 flags=os.O_WRONLY|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if mode=='create' else 0);fd=os.open(name,flags,0o600);size=os.fstat(fd).st_size;failure=None
 for chunk in [1024**2,4096]:
  while size<64*1024**2:
   try:os.posix_fallocate(fd,size,chunk);size+=chunk
   except OSError as e:failure=e.errno;break
 os.fsync(fd);s=os.fstat(fd);os.close(fd);assert failure==errno.EDQUOT;print(json.dumps({'device':s.st_dev,'inode':s.st_ino,'bytes':s.st_size,'errno':failure,'uid':s.st_uid}))
"""
r=subprocess.run(['/usr/bin/python3','-I','-c',code,mode,json.dumps(expected)],preexec_fn=drop,capture_output=True,text=True,timeout=30);assert r.returncode==0,r.stderr;print(r.stdout)
'''
        return json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',script,filler_dir,mode,json.dumps(expected)]).stdout)
    def warm(label):
        identifier=submit(ctx,request,'quota-'+label);job,record,_=result(ctx,identifier)
        assert record['cache_hit'] and record['ir_sha256']==proof['ir_sha256'] and job['allocation']['cache']['key']==key
        jobs.append({'label':label,'job_id':identifier,'attempt_id':job['attempt_id'],'cache_hit':True});return job
    def crash_apply(path,stage):
        code='import json,os,signal,sys;sys.path.insert(0,sys.argv[1]+"/current");import node_quota_admin as a;from deployment import regular_json\ndef boundary(name):\n if name==sys.argv[3]:\n  print(json.dumps({"stage":name,"uid":os.getuid(),"pid":os.getpid()}),flush=True);os.kill(os.getpid(),signal.SIGKILL)\na._checkpoint=boundary;a.apply(sys.argv[1],regular_json(sys.argv[2],private=True))'
        reply=docker(['exec',node,'/usr/bin/python3','-I','-c',code,node_prefix,path,stage],check=False)
        assert reply.returncode==137,(stage,reply.returncode,reply.stderr)
        crashes.append(json.loads(reply.stdout))
        if stage!='published':
            assert inspect()['journal']
            denied=node_cli('verify',check=False);assert denied.returncode and 'pending Node quota' in denied.stderr
            denied=node_cli('start',check=False);assert denied.returncode and 'pending Node quota' in denied.stderr
    ctl(node,'node','stop');ctl(controller,'controller','stop')
    ctl(controller,'controller','backup','--output','/root/backups/before-node-quota.tar.gz')
    ctl(node,'node','backup','--output','/root/backups/before-node-quota.tar.gz')
    assert ctl(controller,'controller','start')['ready'] and ctl(node,'node','start')['ready']
    warm('after-pre-quota-backup');ctl(node,'node','stop')
    initial=inspect();old_cache=next(v for v in initial['quota']['persistent_cache'] if v['uid']==23003)
    old_work=next(v for v in initial['quota']['workspace'] if v['uid']==23003);peer_limits={k:next(v for v in initial['quota'][k] if v['uid']==23004) for k in ['workspace','persistent_cache']}
    assert old_cache['byte_hard']==old_work['byte_hard']==16*1024**2
    fill=actor('create');assert fill['errno']==122
    path,grow=make('cache-grow','persistent_cache',24,256);_,stale=make('cache-stale','persistent_cache',32,384)
    after_plan=inspect();assert not after_plan['journal'] and after_plan['revision']==initial['revision']
    assert next(v for v in after_plan['quota']['persistent_cache'] if v['uid']==23003)['byte_hard']==old_cache['byte_hard']
    denied=docker(['exec','--user','23642:23642',node,node_prefix+'/current/servicectl','node-quota-plan','--prefix',node_prefix,
        '--owner','proof_user','--kind','workspace','--mib','24','--inodes','256','--output','/root/forbidden.json'],check=False)
    assert denied.returncode and 'requires root' in denied.stderr
    approve('cache-grow',path,grow)
    stages=['journal','intent','quota','native_marker','cache_control','cache_marker','config','metadata','receipt','ready','intent_removed','published']
    for stage in stages:
        crash_apply(path,stage)
        if stage=='quota':
            # Recovery uses the original plan after explicit credential revocation
            # and rotation; only the key is transferred into the stopped Node.
            old_config=ctx['secret_json'](node,ctx['node_state']/'config.json');old_key=old_config['node_key']
            docker(['exec',controller,'/usr/bin/python3','-B',central_prefix+'/current/servicectl','admin','--prefix',central_prefix,'--','workerctl','node-rotate',node_id])
            rotated=ctx['secret_json'](controller,ctx['controller_state']/'workers'/node_id/'config.json')
            save='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["node_key"]=sys.stdin.read();p.write_text(json.dumps(v));p.chmod(0o600)'
            docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save,str(ctx['node_state']/'config.json')],input=rotated['node_key'])
            try:Client({'controller':ctx['outer_url'],'token':old_key}).request('/api/worker/quota-status',{'plan_id':grow['id']})
            except ClientHTTPError as error:assert error.status==401
            else:raise AssertionError('Old Node key survived rotation')
    applied=json.loads(node_cli('node-quota-apply','--plan',path).stdout);assert applied['already_applied']
    grown=inspect();assert grown['cache_files']==initial['cache_files']
    records.append({'kind':'cache-grow','plan_id':grow['id'],'receipt':applied['receipt']})
    assert ctl(node,'node','start')['ready'];observed(grow);warm('cache-grow-warm');ctl(node,'node','stop')
    filled=actor('append');assert filled['bytes']>fill['bytes']+4*1024**2 and filled['inode']==fill['inode']
    unsafe=node_cli('node-quota-plan','--owner','proof_user','--kind','persistent_cache','--mib','16','--inodes','128',
        '--output','/provision/forbidden-shrink.json',check=False)
    assert unsafe.returncode and 'retained data' in unsafe.stderr
    assert not inspect()['journal']
    assert ctl(node,'node','start')['ready']
    waiting=submit(ctx,request,'quota-full-owner-wait');time.sleep(1)
    detail=api('/api/jobs/'+waiting)[1];assert detail['job']['state']=='queued' and not detail['attempts']
    # Use the original peer's sealed manifest; an ordinary personal request
    # never imports the first owner's inputs or cache paths.
    peer_jobs=api('/api/jobs',extra_headers=ctx['peer_headers'])[1]['jobs']
    previous=next(v for v in peer_jobs if v.get('allocation',{}).get('cache'))
    peer_request={'template':previous['template'],'environment':'worker-python','parameters':copy.deepcopy(previous['parameters'])}
    peer_id=submit(ctx,peer_request,'quota-peer-progress',headers=ctx['peer_headers']);peer,peer_record,_=result(ctx,peer_id,ctx['peer_headers'])
    assert peer_record['cache_hit'] and peer_record['ir_sha256']==proof['ir_sha256'] and api('/api/jobs/'+waiting)[1]['job']['state']=='queued'
    assert actor('remove',filled)['removed'];done,record,_=result(ctx,waiting)
    assert record['cache_hit'] and done['allocation']['cache']['key']==key
    jobs.extend([{'label':'full-owner-wait','job_id':waiting,'attempt_id':done['attempt_id'],'cache_hit':True},
        {'label':'peer-progress','job_id':peer_id,'attempt_id':peer['attempt_id'],'cache_hit':True}])
    ctl(node,'node','stop');path,shrink=make('cache-shrink','persistent_cache',16,128);approve('cache-shrink',path,shrink)
    reply=json.loads(node_cli('node-quota-apply','--plan',path).stdout);assert reply['applied'];records.append({'kind':'cache-shrink','plan_id':shrink['id'],'receipt':reply['receipt']})
    assert ctl(node,'node','start')['ready'];observed(shrink);warm('cache-shrink-warm');ctl(node,'node','stop')
    for label,mib,inodes in [('work-grow',24,256),('work-shrink',16,128)]:
        path,plan=make(label,'workspace',mib,inodes);approve(label,path,plan)
        if label=='work-grow':crash_apply(path,'config')
        reply=json.loads(node_cli('node-quota-apply','--plan',path).stdout);assert reply['applied'];records.append({'kind':label,'plan_id':plan['id'],'receipt':reply['receipt']})
        assert ctl(node,'node','start')['ready'];observed(plan)
        if label=='work-grow':
            test=copy.deepcopy(ctx['request']);test['idempotency_key']='quota-grown-work-kernel-limits'
            test['parameters'].update(argv=['python','workspace_probe.py','--quota-bytes',str(24*1024**2),'--quota-inodes','256'])
            status,value=api('/api/jobs',test);assert status==201,value;job=ctx['wait'](value['job']['id']);assert job['state']=='succeeded'
            result_value=api('/api/jobs/'+job['id']+'/artifact?name=ir%2Fworkspace-probe.json')[1]
            assert result_value['byte_errno']==result_value['inode_errno']==122 and result_value['quota_byte_hard']==24*1024**2 and result_value['quota_inode_hard']==256
            jobs.append({'label':'grown-work-EDQUOT','job_id':job['id'],'attempt_id':job['attempt_id']})
        warm(label+'-warm');ctl(node,'node','stop')
    final=inspect();assert final['revision']==initial['revision']+4 and not final['journal'] and final['cache_files']==initial['cache_files']
    for kind in ['workspace','persistent_cache']:
        peer_now=next(v for v in final['quota'][kind] if v['uid']==23004)
        assert {k:peer_now[k] for k in ['byte_hard','inode_hard']}=={k:peer_limits[kind][k] for k in ['byte_hard','inode_hard']}
    save='import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600)'
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save,'/provision/stale-node-quota.json'],input=json.dumps(stale))
    assert node_cli('node-quota-apply','--plan','/provision/stale-node-quota.json',check=False).returncode
    # The final cache limits equal the original prepared restore contract.
    ctx['configuration']=ctx['secret_json'](node,ctx['node_state']/'config.json');ctx['slots']=ctx['configuration']['workspace']['slots'];ctx['cache_policy']=ctx['configuration']['persistent_cache']
    old=base/'node-quota-old-source';old.mkdir(mode=0o700);packed=subprocess.check_output(['git','archive','765f902b0e17df4db36fae993ffb0b50249fe8f7'],cwd=ctx['ROOT'])
    with tarfile.open(fileobj=io.BytesIO(packed)) as archive:archive.extractall(old,filter='data')
    package=base/'node-quota-old.tar.gz';code='import sys;sys.path.insert(0,sys.argv[1]);from deployment import bundle;bundle(sys.argv[2],sys.argv[1])'
    subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(old),str(package)],capture_output=True,text=True,check=True,timeout=45)
    for container in [controller,node]:docker(['cp',str(package),container+':/provision/node-quota-old.tar.gz'])
    ctl(controller,'controller','stop')
    # Reject checkpoints taken before these native changes, before any spool,
    # cache binding or installation metadata is rebound to the proposed target.
    save_spool='import sys;from pathlib import Path;p=Path("/root/quota-restore-spool.json");p.write_text(sys.stdin.read());p.chmod(0o600)'
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save_spool],input=json.dumps(ctx['spool_policy']))
    before_reject=inspect()
    for container,role,arguments in [(controller,'controller',[]),(node,'node',[
        '--spool-config','/root/quota-restore-spool.json','--cache-config','/provision/cache-quota.json'])]:
        denied=ctl(container,role,'restore','--archive','/root/backups/before-node-quota.tar.gz',
            '--new-state-dir','/var/lib/rejected-pre-quota-'+role,*arguments,check=False)
        assert denied.returncode and 'quota history' in denied.stderr,denied.stderr
    assert inspect()==before_reject
    for container,role in [(controller,'controller'),(node,'node')]:
        denied=ctl(container,role,'activate','--archive','/provision/node-quota-old.tar.gz',check=False)
        assert denied.returncode and 'quota change history' in denied.stderr
    assert ctl(controller,'controller','start')['ready'] and ctl(node,'node','start')['ready'];warm('after-downgrade-denial')
    atomic_json(base/'node-quota-native-proof.json',{'initial':initial,'final':final,'changes':records,'crashes':crashes})
    return {'passed':True,'changes':records,'process_crashes':crashes,'jobs':jobs,'initial':initial,'final':final,
        'plan_readonly':True,'unprivileged_plan_denied':True,'native_cache_old_limit_edquot':fill,'native_cache_new_limit_edquot':filled,
        'unsafe_shrink_rejected':True,'stale_plan_rejected':True,'same_namespace_inode_and_ir_bytes':True,'original_cache_key':key,
        'full_owner_waits_without_attempt_while_peer_progresses':True,'original_waiting_job_resumed':True,'actual_grown_workspace_edquot':True,
        'node_key_rotation_preserves_original_pending_plan':True,'old_node_key_revoked':True,'prior_controller_and_node_downgrade_denied':True,
        'older_controller_and_node_snapshots_rejected_before_native_rebind':True,'stale_restore_leaves_current_state_unchanged':True,
        'peer_native_limits_unchanged':True,'work_and_cache_limits_restored_to_original':True,'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Same-host installed Root UID quota maintenance and preserved BishengIR example cache, 13 explicit administrative SIGKILLs, actual EDQUOT/peer queue and restore contract. Not online/shared-Node resize, Triton JIT/ccache warm qualification, real PID1, NPU or two-physical-host acceptance.'}


def after_restore(ctx,proof):
    for task in proof['jobs']:
        headers=ctx['peer_headers'] if task['label']=='peer-progress' else None
        detail=ctx['api']('/api/jobs/'+task['job_id'],extra_headers=headers)[1]
        assert detail['job']['state']=='succeeded' and detail['job']['attempt_id']==task['attempt_id'] and len(detail['attempts'])==1
    for record in proof['changes']:
        status=ctx['ctl'](ctx['controller'],'controller','admin','--','workerctl','node-quota-status',ctx['enrollment']['node_id'],'--plan-id',record['plan_id'])
        assert status['state']=='observed'
    identifier=submit(ctx,proof['request'] if 'request' in proof else ctx['cache_proof']['request'],'quota-after-restore-warm')
    job,record,_=result(ctx,identifier);assert record['cache_hit'] and job['allocation']['cache']['key']==proof['original_cache_key']
    return {'passed':True,'historical_jobs_and_attempts_preserved':True,'approved_changes_preserved':True,
        'warm_job_id':identifier,'warm_attempt_id':job['attempt_id'],'same_cache_key_and_ir_bytes':True}

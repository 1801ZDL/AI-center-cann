"""Installed native controller + finite ext4 cold copy with actual SIGKILLs.

The job is genuinely submitted then cancelled without a Node attempt. Additional
public/private fixture bytes exercise archive transport, not compiler results.
"""
import hashlib,json,os,signal,sqlite3,subprocess,sys,tarfile,time,uuid
from pathlib import Path
E=Path('/evidence');owner=E.stat().st_uid;group=E.stat().st_gid
assert owner>0 and os.geteuid()==0 and Path('/proc/1/comm').read_text().strip()=='systemd'
assert not Path('/var/run/docker.sock').exists() and not list(Path('/dev').glob('davinci*'))
prefix=Path('/opt/ascend-lab-controller');state=Path('/state/controller');pool_path=Path('/srv/cold')
events=[];stage='bus';report={'passed':False};faults=[]
READ=Path('/provision/cold-reader').exists();reader_evidence={}
RETAINED=Path('/provision/retained-restore').exists();retained_resume=None;initial_before=None
CAPACITY=Path('/provision/history-capacity').exists();capacity_evidence={}
def write(name,value):
 p=E/name
 with p.open('x') as f:json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
 p.chmod(0o600);os.chown(p,owner,group)
def ctl(action,*args,bootstrap=False,check=True):
 tool='/provision/bootstrap/servicectl' if bootstrap else str(prefix/'current/servicectl')
 r=subprocess.run(['/usr/bin/python3','-I','-B',tool,action,*args],capture_output=True,text=True,timeout=35)
 events.append({'action':action,'exit_code':r.returncode})
 if not check:return r
 if r.returncode:
  write('failure-command.json',{'action':action,'stderr':r.stderr[-4096:]});raise RuntimeError('Fixed cold operation refused: '+action)
 return json.loads(r.stdout)
def start(label):
 for action in ['unit-manager','unit-start']:
  path='/state/'+action+'-'+label+'.json'
  ctl(action+'-plan','--prefix',str(prefix),'--output',path)
  until=time.monotonic()+25
  while True:
   result=ctl(action+'-apply','--prefix',str(prefix),'--plan',path)
   if action=='unit-manager' or result['state']=='ready':break
   if time.monotonic()>until:raise RuntimeError('Original native startup not ready')
   time.sleep(.2)
def restart():
 ctl('unit-manager-check','--prefix',str(prefix))
 subprocess.run(['/usr/bin/systemctl','start','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
 until=time.monotonic()+25
 while not ctl('status','--prefix',str(prefix))['ready']:
  if time.monotonic()>until:raise RuntimeError('Original verified unit restart not ready')
  time.sleep(.2)
 ctl('unit-manager-check','--prefix',str(prefix),'--running')
def stop():subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
def drop():os.setgroups([]);os.setgid(installed['gid']);os.setuid(installed['uid'])
def run_owner(code,*args):
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(prefix/'current'),str(state/'config.json'),*args],
  preexec_fn=drop,capture_output=True,text=True,timeout=25,env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8'})
 if r.returncode:
  write('failure-owner.json',{'stderr':r.stderr[-4096:]});raise RuntimeError('Own fixture owner operation refused')
 return json.loads(r.stdout)
def recover():
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c','import sqlite3,sys\nc=sqlite3.connect(sys.argv[1]);c.execute("BEGIN IMMEDIATE");c.rollback();c.close()',str(state/'jobs.sqlite3')],
  preexec_fn=drop,capture_output=True,text=True,timeout=10)
 assert r.returncode==0
def fault(boundary,method,plan,retention=False,maximum_entries=256,capacity=False):
 code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
import importlib
c=importlib.import_module(sys.argv[4])
def kill(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
c._checkpoint=kill
v=json.loads(Path(sys.argv[3]).read_text())
getattr(c,sys.argv[2])("/opt/ascend-lab-controller",v,maximum_entries=int(sys.argv[5])) if sys.argv[4]=="cold_retention" else getattr(c,sys.argv[2])("/opt/ascend-lab-controller",v)
'''
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,boundary,method,plan,'history_capacity' if capacity else 'cold_retention' if retention else 'cold_exports',str(maximum_entries)],capture_output=True,text=True,timeout=25)
 if r.returncode!=-signal.SIGKILL:write('failure-fault.json',{'boundary':boundary,'code':r.returncode,'stderr':r.stderr[-4096:]})
 assert r.returncode==-signal.SIGKILL;recover();faults.append({'boundary':boundary,'exit_code':r.returncode})
def accounting():
 with sqlite3.connect(state/'jobs.sqlite3') as db:
  return {'job':json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(job_id,)).fetchone()[0]),
   'allocation':list(db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind="archive" AND id=?',(job_id,)).fetchone()),
   'jobs':db.execute('SELECT count(*) FROM jobs').fetchone()[0],'attempts':db.execute('SELECT count(*) FROM execution_attempts').fetchone()[0]}
try:
 until=time.monotonic()+20
 while subprocess.run(['/usr/bin/busctl','--system','--json=short','call','org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner','s','org.freedesktop.systemd1'],capture_output=True,timeout=5).returncode:
  if time.monotonic()>until:raise RuntimeError('Original native bus unavailable')
  time.sleep(.2)
 stage='install'
 installed=ctl('install','--archive','/provision/server.tar.gz','--role','controller','--prefix',str(prefix),
  '--state-dir',str(state),'--create-service-user','labsvc','--port','18865',bootstrap=True)
 ctl('unit-install','--prefix',str(prefix));start('initial')
 sys.path.insert(0,str(prefix/'current'));import storage_files as fs
 from unit_manager import Bus
 from jobs import identity,identity_alive
 handle=identity(Bus().unit('ascend-lab-controller.service')['MainPID'])
 assert handle['uid']==installed['uid'] and identity_alive(handle)
 status={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/'+str(handle['pid'])+'/status').read_text().splitlines()) if sep}
 assert int(status['CapEff'],16)==0 and status['NoNewPrivs']=='1'
 if CAPACITY:
  stop();stage='history-capacity-policy'
  policy_plan='/root/history-capacity.json'
  ctl('history-capacity-plan','--prefix',str(prefix),'--max-control-jobs','2','--max-online-archives','1','--output',policy_plan)
  for boundary in ['before_commit','committed']:fault(boundary,'apply',policy_plan,capacity=True)
  result=ctl('history-capacity-apply','--prefix',str(prefix),'--plan',policy_plan);assert result['reused']
  capacity_evidence['policy']=result['policy'];capacity_evidence['actual_policy_sigkills']=2
  restart()
 stage='actual-job'
 submitted=run_owner('''import base64,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);raw=b"cold-source-input"
i=c.request("/api/inputs",{"files":[{"name":"payload.bin","size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}]})["input"]["id"]
c.request("/api/inputs/"+i+"/blob",{"name":"payload.bin","offset":0,"data":base64.b64encode(raw).decode()});c.request("/api/inputs/"+i+"/seal",{})
request={"name":"cold archive transport fixture","cwd":json.loads(Path(sys.argv[2]).read_text())["scheduler"]["workspace_root"],"environment":"worker-python","template":"ir_v1","parameters":{"input_id":i,"argv":["python","-c","print(42)"],"lock":{"python":"3.10.12"},"resources":{"cpu":1,"memory_mib":128,"shm_mib":0,"npu":0},"timeout_seconds":30},"idempotency_key":"cold-archive-fixture"}
Path(sys.argv[2]).with_name("fixture-replay-request.json").write_text(json.dumps(request));Path(sys.argv[2]).with_name("fixture-replay-request.json").chmod(0o600)
j=c.request("/api/jobs",request)["job"]["id"]
c.request("/api/jobs/"+j+"/cancel",{});job=c.request("/api/jobs/"+j)["job"];assert job["state"]=="cancelled"
print(json.dumps({"id":j,"owner":job["owner"]}))
''');job_id=submitted['id'];job_owner=submitted['owner'];stop()
 if CAPACITY:
  restart()
  capacity_evidence['warm_capacity']=run_owner('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);body=json.loads(Path(sys.argv[2]).with_name("fixture-replay-request.json").read_text())
v=c.request("/api/jobs",body);assert v["job"]["id"]==sys.argv[3] and v["reused"]
assert c.request("/api/jobs")["history_capacity"]["usage"]=={"control_jobs":1,"online_archives":1}
body["idempotency_key"]="must-wait-for-online-retirement"
try:c.request("/api/jobs",body)
except ClientHTTPError as e:assert e.status==409
else:raise AssertionError("Warm terminal archive released an online slot")
print(json.dumps({"terminal_warm_blocks_new_job":True,"original_replay_at_full_is_free":True}))
''',job_id);stop()
 archive=state/'jobs'/job_id
 # Explicit test-only data; controller is stopped, no real result is replaced.
 run_owner('''import json,sys
from pathlib import Path
p=Path(sys.argv[3]);(p/"transport-fixture.bin").write_bytes(b"P"*(700*1024));(p/"private-evidence.bin").write_bytes(b"Q"*(900*1024))
index=p/"artifacts.json";v=json.loads(index.read_text());v["files"].append({"name":"transport-fixture.bin","size":700*1024});index.write_text(json.dumps(v));index.chmod(0o600)
for name in ["transport-fixture.bin","private-evidence.bin"]:(p/name).chmod(0o600)
print(json.dumps({"fixture_bytes_added":True}))
''',str(archive))
 before=accounting();assert before['attempts']==0
 original=fs.scan(archive);source_digest=fs.inventory_digest(original)
 Path('/root/backups').mkdir(mode=0o700)
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/before.tar.gz')
 stage='finite-pool'
 lost=pool_path/'lost+found'
 if lost.exists():assert lost.is_dir() and not list(lost.iterdir());lost.rmdir()
 assert not list(pool_path.iterdir());pool_path.chmod(0o700)
 info=pool_path.stat();space=os.statvfs(pool_path)
 spec={'storage':{'path':str(pool_path),'filesystem':'ext4','device':info.st_dev,'root_inode':info.st_ino,
  'bytes':space.f_blocks*space.f_frsize,'inodes':space.f_files,'storage_id':uuid.uuid4().hex},
  'budget_bytes':32*1024**2,'budget_inodes':512,'owners':{job_owner:{'bytes':16*1024**2,'inodes':256}},'max_job_bytes':4*1024**2,'max_transfers':100}
 Path('/root/cold-spec.json').write_text(json.dumps(spec));Path('/root/cold-spec.json').chmod(0o600)
 pool_plan='/root/cold-register.json'
 ctl('cold-register-plan','--prefix',str(prefix),'--spec','/root/cold-spec.json','--output',pool_plan)
 for boundary in ['registration_reserved','registration_marker','registration_committed']:
  fault(boundary,'register_apply',pool_plan)
  rejected=ctl('backup','--prefix',str(prefix),'--output','/root/backups/pending-registration.tar.gz',check=False)
  if boundary!='registration_committed':assert rejected.returncode and 'cold registration' in rejected.stderr
  elif rejected.returncode==0:Path('/root/backups/pending-registration.tar.gz').unlink()
 registered=ctl('cold-register-apply','--prefix',str(prefix),'--plan',pool_plan);assert registered['reused']
 assert not (prefix/'cold-registration-change.json').exists()
 stage='private-plan'
 denied=ctl('cold-copy-plan','--prefix',str(prefix),'--job-id',job_id,'--output','/root/not-approved.json',check=False)
 assert denied.returncode and not Path('/root/not-approved.json').exists()
 copy_plan='/root/cold-copy.json'
 ctl('cold-copy-plan','--prefix',str(prefix),'--job-id',job_id,'--private-file','private-evidence.bin','--output',copy_plan)
 plan=json.loads(Path(copy_plan).read_text());assert 'private-evidence.bin' in plan['manifest']['private'] and 'private-evidence.bin' not in plan['manifest']['indexed']
 assert 'transport-fixture.bin' in plan['manifest']['indexed']
 denied=run_owner('''import json,subprocess,sys
r=subprocess.run(["/usr/bin/python3","-I","-B",sys.argv[1]+"/servicectl","cold-copy-step","--prefix","/opt/ascend-lab-controller","--plan","/root/cold-copy.json"],capture_output=True,text=True)
assert r.returncode;print(json.dumps({"denied":True}))
''');assert denied['denied']
 stage='same-plan-actual-kills'
 for boundary in ['reserved','file_created','data_synced','progress_committed','sealed','copy_published','verified']:
  fault(boundary,'copy_step',copy_plan)
  assert accounting()==before and fs.inventory_digest(fs.scan(archive))==source_digest
  with sqlite3.connect(state/'jobs.sqlite3') as db:
   row=db.execute('SELECT state,record FROM cold_transfers').fetchone();assert row and db.execute('SELECT count(*) FROM cold_transfers').fetchone()[0]==1
  if boundary!='verified':
   rejected=ctl('backup','--prefix',str(prefix),'--output','/root/backups/pending-copy.tar.gz',check=False)
   assert rejected.returncode and 'cold copy' in rejected.stderr
  if boundary=='reserved':
   restart()
   pinned=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2])
try:c.request("/api/storage/plan",{"kind":"archive","id":sys.argv[3],"action":"trash"})
except ClientHTTPError as e:assert e.status==409
else:raise AssertionError("Copying source was allowed to move")
print(json.dumps({"pinned":True}))
''',job_id);assert pinned['pinned'];stop()
 verified=ctl('cold-copy-step','--prefix',str(prefix),'--plan',copy_plan)
 assert verified['state']=='verified' and verified['reused'] and verified['original_retained']
 assert not any(verified[k] for k in ['quota_released','history_capacity_released','read_pointer_changed'])
 assert ctl('cold-copy-verify','--prefix',str(prefix),'--plan',copy_plan)['copy_bytes_verified']
 assert accounting()==before and fs.inventory_digest(fs.scan(archive))==source_digest
 stage='original-api-and-acl'
 restart()
 checked=run_owner('''import base64,json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);j=sys.argv[3];v=c.request("/api/jobs/"+j)
assert v["cold_copies"][0]["state"]=="verified" and not v["cold_copies"][0]["available_for_user_reads"]
assert "private-evidence.bin" not in json.dumps(v["cold_copies"])
m=c.request("/api/jobs/"+j+"/archive-manifest")
assert "private-evidence.bin" not in json.dumps(m) and "transport-fixture.bin" in json.dumps(m)
try:c.request("/api/jobs/"+j+"/artifact-chunk?name=private-evidence.bin&offset=0")
except ClientHTTPError as e:assert e.status==404
else:raise AssertionError("Private controller evidence became a user download")
print(json.dumps({"original_manifest":m,"cold_status_only":True,"private_read_denied":True}))
''',job_id)
 stop();assert accounting()==before
 expected_before_restore=before
 if READ:
  stage='activate-authoritative-reader'
  ctl('backup','--prefix',str(prefix),'--output','/root/backups/copy-only.tar.gz')
  read_plan='/root/cold-read.json';retire_plan='/root/cold-retire.json'
  ctl('cold-read-plan','--prefix',str(prefix),'--copy-plan',copy_plan,'--output',read_plan)
  fault('reader_committed','apply',read_plan,retention=True)
  assert ctl('cold-retention-apply','--prefix',str(prefix),'--plan',read_plan)['reused']
  assert accounting()==before
  if RETAINED:
   stage='restore-retained-original'
   restart()
   replay=run_owner('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);v=c.request("/api/jobs",json.loads(Path(sys.argv[2]).with_name("fixture-replay-request.json").read_text()));assert v["job"]["id"]==sys.argv[3]
print(json.dumps({"same_original_idempotent_job":True}))
''',job_id);stop()
   ctl('backup','--prefix',str(prefix),'--output','/root/backups/retained.tar.gz')
   initial_before=before
   ctl('restore','--prefix',str(prefix),'--archive','/root/backups/retained.tar.gz','--new-state-dir','/state/retained')
   state=Path('/state/retained');archive=state/'jobs'/job_id
   expected=json.loads(json.dumps(before));expected['job']['archive_dir']=str(archive);assert accounting()==expected
   mid='/root/cold-read-retained-resume.json'
   ctl('cold-read-resume-plan','--prefix',str(prefix),'--copy-plan',copy_plan,'--output',mid)
   retained_resume=json.loads(Path(mid).read_text())
   assert retained_resume['source_inventory_sha256']!=json.loads(Path(read_plan).read_text())['source_inventory_sha256']
   ctl('cold-retention-apply','--prefix',str(prefix),'--plan',mid)
   ctl('unit-refresh-plan','--prefix',str(prefix),'--output','/root/retained-unit-refresh.json')
   ctl('unit-refresh-apply','--prefix',str(prefix),'--plan','/root/retained-unit-refresh.json')
   start('retained-resumed');stop();before=accounting()
  ctl('cold-retire-plan','--prefix',str(prefix),'--copy-plan',copy_plan,'--output',retire_plan)
  for boundary in ['retire_inventory_synced','retire_prepared','original_renamed','original_purged_step','original_removed','retirement_credited']:
   fault(boundary,'apply',retire_plan,retention=True,maximum_entries=1 if boundary=='original_purged_step' else 256)
   current=accounting();assert current['job']==before['job'] and current['jobs']==before['jobs'] and current['attempts']==0
   if boundary!='retirement_credited':
    assert current['allocation']==before['allocation']
   if boundary not in {'retire_inventory_synced','retirement_credited'}:
    rejected=ctl('backup','--prefix',str(prefix),'--output','/root/backups/retiring.tar.gz',check=False)
    assert rejected.returncode and 'retirement' in rejected.stderr
  retired=ctl('cold-retention-apply','--prefix',str(prefix),'--plan',retire_plan)
  assert retired['reused'] and retired['quota_released'] and retired['online_payload_removed']
  assert retired['online_archive_slot_released']==CAPACITY
  assert not archive.exists() and not (state/'storage-trash/cold'/plan['id']).exists()
  expected_before_restore=accounting();assert expected_before_restore['allocation'][1]<before['allocation'][1]
  if CAPACITY:
   stage='online-slot-release-and-control-bound'
   restart()
   second=run_owner('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);body=json.loads(Path(sys.argv[2]).with_name("fixture-replay-request.json").read_text())
# This is a new job and must use the current workspace; only exact old requests
# can resolve their original receipt before path validation.
body["cwd"]=json.loads(Path(sys.argv[2]).read_text())["scheduler"]["workspace_root"]
body["idempotency_key"]="second-after-confirmed-retirement"
job=c.request("/api/jobs",body)["job"];c.request("/api/jobs/"+job["id"]+"/cancel",{})
import hashlib
directory=Path(job["archive_dir"])
files={str(p.relative_to(directory)):{"size":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in directory.rglob("*") if p.is_file()}
print(json.dumps({"id":job["id"],"owner":job["owner"],"warm_files":files}))
''');stop()
   # The restored pool intentionally retains frozen write authority. Keep this
   # second archive warm and exercise an explicit policy revision, so the next
   # rejection proves control capacity independently of online capacity.
   expansion='/root/history-online-expand.json'
   ctl('history-capacity-plan','--prefix',str(prefix),'--max-control-jobs','2','--max-online-archives','2','--output',expansion)
   revised=ctl('history-capacity-apply','--prefix',str(prefix),'--plan',expansion)
   assert revised['policy']['revision']==2 and revised['usage']=={'control_jobs':2,'online_archives':1}
   capacity_evidence.update(initial_policy=capacity_evidence['policy'],policy=revised['policy'],second_warm_files=second['warm_files'])
   restart()
   capacity_evidence['confirmed_retirement']=run_owner('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);body=json.loads(Path(sys.argv[2]).with_name("fixture-replay-request.json").read_text())
assert c.request("/api/jobs",body)["job"]["id"]==sys.argv[3]
body["cwd"]=json.loads(Path(sys.argv[2]).read_text())["scheduler"]["workspace_root"];body["idempotency_key"]="control-capacity-must-stop"
try:c.request("/api/jobs",body)
except ClientHTTPError as e:assert e.status==409
else:raise AssertionError("Control history was unlimited after payload retirement")
page=c.request("/api/jobs/history?limit=1&offset=1");assert page["total"]==2 and len(page["jobs"])==1
s=c.request("/api/jobs")["history_capacity"];assert s["usage"]=={"control_jobs":2,"online_archives":1} and s["limits"]=={"max_control_jobs":2,"max_online_archives":2} and s["blocks_new_jobs"]
print(json.dumps({"second_original_job":sys.argv[4],"online_slot_reused_after_confirmed_removal":True,"both_control_jobs_retained":True,"control_full_blocks_new_admission":True,"usage":s["usage"],"paginated_history_http_verified":True,"restored_exact_old_request_replay":True}))
''',job_id,second['id']);stop()
   expected_before_restore=accounting()
  rejected=ctl('activate','--prefix',str(prefix),'--archive','/provision/legacy.tar.gz',check=False)
  assert rejected.returncode and ('capacity/replay' if CAPACITY else 'cold read authority') in rejected.stderr
  rejected=ctl('restore','--prefix',str(prefix),'--archive','/root/backups/copy-only.tar.gz','--new-state-dir','/state/rejected-reader',check=False)
  assert rejected.returncode and 'cold read authority' in rejected.stderr and not Path('/state/rejected-reader').exists()
  restart()
  observed=identity(Bus().unit('ascend-lab-controller.service')['MainPID']);assert observed['uid']==installed['uid'] and identity_alive(observed)
  mount_view=run_owner('''import json,os,sys
v=os.statvfs(sys.argv[3]);print(json.dumps({"readonly":bool(v.f_flag&os.ST_RDONLY),"nosuid":bool(v.f_flag&os.ST_NOSUID),"nodev":bool(v.f_flag&os.ST_NODEV)}))
''','/proc/'+str(observed['pid'])+'/root/srv/cold');assert mount_view['readonly'] and mount_view['nosuid'] and mount_view['nodev']
  write('cold-service-mount-view.json',mount_view)
  verified_reads=run_owner('''import base64,hashlib,json,subprocess,sys,urllib.request
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);j=sys.argv[3]
v=c.request("/api/jobs/"+j);assert v["archive_location"]["online_payload_removed"] and not v["archive_location"]["read_unavailable"]
assert v["cold_copies"][0]["available_for_user_reads"] and not v["permissions"]["storage"]
if sys.argv[4]!="restored":
 replayed=c.request("/api/jobs",json.loads(Path(sys.argv[2]).with_name("fixture-replay-request.json").read_text()));assert replayed["job"]["id"]==j
m=c.request("/api/jobs/"+j+"/archive-manifest")
part=c.request("/api/jobs/"+j+"/artifact-chunk?name=transport-fixture.bin&offset=262140&length=21")
assert base64.b64decode(part["data"])==b"P"*21 and part["sha256"]==hashlib.sha256(b"P"*21).hexdigest()
r=urllib.request.Request(c.url+"/api/jobs/"+j+"/artifact?name=transport-fixture.bin",headers={"Authorization":c.authorization,"Range":"bytes=262140-262160"})
with c.opener.open(r,timeout=10) as response:assert response.status==206 and response.read()==b"P"*21
c.request("/api/jobs/"+j+"/log?stream=stdout")
for path,body,code in [("/artifact-chunk?name=private-evidence.bin",None,404),("storage",{"kind":"archive","id":j,"action":"trash"},409)]:
 try:c.request("/api/storage/plan" if path=="storage" else "/api/jobs/"+j+path,body)
 except ClientHTTPError as e:assert e.status==code
 else:raise AssertionError("Cold retention or private evidence was exposed")
loader='import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")'
r=subprocess.run(["/usr/bin/python3","-I","-B","-c",loader,sys.argv[1],sys.argv[1]+"/accessctl","--state-dir",str(Path(sys.argv[2]).parent),"user-add","cold-peer"],capture_output=True,text=True);assert r.returncode==0
peer=Client.from_profile(Path(sys.argv[2]).parent/"personas/cold-peer/client.json")
assert peer.request("/api/jobs/history?limit=1")["total"]==0
for path in ["","/archive-manifest","/artifact-chunk?name=transport-fixture.bin","/log"]:
 try:peer.request("/api/jobs/"+j+path)
 except ClientHTTPError as e:assert e.status==404
 else:raise AssertionError("Cross-owner cold read was allowed")
print(json.dumps({"manifest":m,"range_and_chunks_verified":True,"idempotent_replay_same_job":sys.argv[4]!="restored","cross_owner_reads_denied":True}))
''',job_id,'restored' if RETAINED and not CAPACITY else 'original')
  assert verified_reads['manifest']==checked['original_manifest'];stop();assert accounting()==expected_before_restore
  reader_evidence={'original_allocation':before['allocation'],'retired_allocation':expected_before_restore['allocation'],'service_mount_view':mount_view,
   'activation_plan':json.loads(Path(read_plan).read_text()),'retirement_plan':json.loads(Path(retire_plan).read_text()),
   'retired_result':retired,'actual_http_checks':verified_reads}
  if RETAINED:reader_evidence.update(retained_original_resume_plan=retained_resume,original_idempotent_replay_before_state_restore=replay)
 stage='backup-restore'
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/after.tar.gz')
 rejected=ctl('restore','--prefix',str(prefix),'--archive','/root/backups/before.tar.gz','--new-state-dir','/state/rejected',check=False)
 assert rejected.returncode and 'cold' in rejected.stderr and not Path('/state/rejected').exists()
 destination=Path('/state/restored')
 ctl('restore','--prefix',str(prefix),'--archive','/root/backups/after.tar.gz','--new-state-dir',str(destination));state=destination
 rejected=ctl('cold-copy-step','--prefix',str(prefix),'--plan',copy_plan,check=False)
 assert rejected.returncode and 'stale' in rejected.stderr
 historical=ctl('cold-copy-verify','--prefix',str(prefix),'--plan',copy_plan)
 expected=json.loads(json.dumps(expected_before_restore));expected['job']['archive_dir']=str(state/'jobs'/job_id)
 assert historical['copy_bytes_verified'] and historical['historical_only'] and accounting()==expected
 with sqlite3.connect(state/'jobs.sqlite3') as db:
  assert db.execute('SELECT state FROM cold_pools').fetchone()[0]=='restore_frozen'
  assert db.execute('SELECT state FROM cold_transfers').fetchone()[0]=='restore_frozen'
 if READ:
  stage='restored-frozen-reader'
  ctl('unit-refresh-plan','--prefix',str(prefix),'--output','/root/restored-unit-refresh.json')
  ctl('unit-refresh-apply','--prefix',str(prefix),'--plan','/root/restored-unit-refresh.json')
  start('restored-frozen')
  frozen=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);v=c.request("/api/jobs/"+sys.argv[3]);assert v["job"]["state"]=="cancelled" and v["archive_location"]["read_unavailable"]
try:c.request("/api/jobs/"+sys.argv[3]+"/archive-manifest")
except ClientHTTPError as e:assert e.status==409
else:raise AssertionError("Frozen reader was silently reactivated")
print(json.dumps({"metadata_available":True,"cold_reads_frozen":True}))
''',job_id);stop()
  resumed_plan='/root/cold-read-resume.json'
  ctl('cold-read-resume-plan','--prefix',str(prefix),'--copy-plan',copy_plan,'--output',resumed_plan)
  ctl('cold-retention-apply','--prefix',str(prefix),'--plan',resumed_plan)
  restart()
  resumed=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);print(json.dumps(c.request("/api/jobs/"+sys.argv[3]+"/archive-manifest")))
''',job_id);assert resumed==checked['original_manifest'];stop()
  reader_evidence.update(restored_metadata_available_but_reader_frozen=frozen,explicit_root_revalidation_resumed_same_manifest=True,
   resume_plan=json.loads(Path(resumed_plan).read_text()))
  write('cold-reader-records.json',reader_evidence)
  if CAPACITY:
   restart()
   capacity_evidence['latest_restore']=run_owner('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);body=json.loads(Path(sys.argv[2]).with_name("fixture-replay-request.json").read_text())
assert c.request("/api/jobs",body)["job"]["id"]==sys.argv[3]
s=c.request("/api/jobs")["history_capacity"];assert s["usage"]=={"control_jobs":2,"online_archives":1}
assert c.request("/api/jobs/history?limit=1")["total"]==2
print(json.dumps({"same_original_job_after_second_restore":True,"capacity_and_history_preserved":True}))
''',job_id);stop();write('history-capacity-records.json',capacity_evidence)
 import cold_exports as cold
 write('cold-records.json',{'plan':plan,'pool_plan':json.loads(Path(pool_plan).read_text()),'before':before,'after':accounting(),'initial_before':initial_before or before,
  'source_inventory':original,'manifest':checked['original_manifest'],'historical':historical})
 report={'passed':True,'stage':stage,'actual_systemd_pid1':True,'controller_uid':installed['uid'],'controller_capabilities_zero':True,
  'source_job_submitted_and_cancelled':True,'execution_attempts':0,'additional_archive_bytes_are_explicit_test_fixture':True,
  'finite_ext4_pool':True,'pool_registration_faults':3,'actual_sigkill_boundaries':faults,'same_original_plan_reused':True,
  'one_transfer_and_reservation':True,'whole_public_and_private_tree_verified':True,'unapproved_private_files_refused':True,
  'nonroot_cold_mutation_refused':True,'pending_copy_blocks_backup':True,'actual_owner_source_retirement_refused_while_copying':True,'original_archive_and_accounting_unchanged':True,
  'original_api_still_authoritative':True,'private_data_not_added_to_user_index':True,'old_backup_missing_cold_history_refused':True,
  'latest_backup_restores_original_and_freezes_cold_authority':True,'old_plan_cannot_write_after_restore':True,
  'historical_cold_bytes_verified_after_restore':True,'read_pointer_changed':False,'original_deleted':False,
  'quota_released':False,'history_capacity_released':False,'npu_launches':0,'physical_hosts':1,'events':events}
 if READ:
  report.update(authoritative_cold_reads_verified=True,original_deleted=True,quota_released=True,read_pointer_changed=True,
   cross_owner_cold_reads_denied=True,same_job_idempotency_and_manifest_preserved=True,actual_range_and_chunk_checks=True,
   reader_and_retirement_actual_sigkills=7,old_code_and_old_reader_snapshot_refused=True,
   restored_metadata_available_while_reader_frozen=True,explicit_root_revalidation_resumes_reads=True,
   original_archive_and_accounting_unchanged_before_retirement=True,original_archive_and_accounting_unchanged=False,
   original_api_still_authoritative=False,original_job_api_and_acl_retained=True,
   latest_backup_restores_original_and_freezes_cold_authority=False,latest_backup_restores_control_history_with_external_cold_bytes=True,
   retained_original_restore_then_retirement_tested=RETAINED)
 if CAPACITY:report.update(history_capacity=capacity_evidence,original_control_jobs=2,actual_policy_sigkills=2,
  finite_online_and_control_limits_verified=True,restored_exact_request_replay_verified=True,
  one_transfer_and_reservation=True,second_archive_remains_warm=True,explicit_nonempty_policy_revision_preserved=True)
except BaseException as error:
 report={'passed':False,'stage':stage,'error_type':type(error).__name__,'error':str(error)[:1024],'actual_sigkill_boundaries':faults,'events':events}
finally:
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,timeout=25)
 with tarfile.open(E/'private-controller-evidence.tar.gz','x:gz') as out:
  if prefix.exists():out.add(prefix,arcname='installation')
  if state.exists():out.add(state,arcname='control')
  for p in [Path('/root/cold-register.json'),Path('/root/cold-copy.json'),Path('/root/cold-read.json'),Path('/root/cold-retire.json'),Path('/root/cold-read-resume.json'),Path('/root/cold-read-retained-resume.json'),Path('/root/history-capacity.json'),Path('/root/history-online-expand.json')]:
   if p.exists():out.add(p,arcname='plans/'+p.name)
 archive=E/'private-controller-evidence.tar.gz';archive.chmod(0o600);os.chown(archive,owner,group)
 limits=Path('/run/native-fixture/cgroup.json');root=Path('/sys/fs/cgroup')
 report['parent_resource_limits_unchanged']=json.loads(limits.read_text())['limits']=={n:(root/n).read_text().strip() for n in ['cpu.max','memory.max','memory.swap.max','pids.max','cpuset.cpus.effective']}
 if not report['parent_resource_limits_unchanged']:report['passed']=False
 write('native-report.json',report)
raise SystemExit(0 if report['passed'] else 1)

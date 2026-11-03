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
def fault(boundary,method,plan):
 code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
import cold_exports as c
def kill(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
c._checkpoint=kill
getattr(c,sys.argv[2])("/opt/ascend-lab-controller",json.loads(Path(sys.argv[3]).read_text()))
'''
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,boundary,method,plan],capture_output=True,text=True,timeout=25)
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
 stage='actual-job'
 submitted=run_owner('''import base64,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);raw=b"cold-source-input"
i=c.request("/api/inputs",{"files":[{"name":"payload.bin","size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}]})["input"]["id"]
c.request("/api/inputs/"+i+"/blob",{"name":"payload.bin","offset":0,"data":base64.b64encode(raw).decode()});c.request("/api/inputs/"+i+"/seal",{})
j=c.request("/api/jobs",{"name":"cold archive transport fixture","cwd":json.loads(Path(sys.argv[2]).read_text())["scheduler"]["workspace_root"],"environment":"worker-python","template":"ir_v1","parameters":{"input_id":i,"argv":["python","-c","print(42)"],"lock":{"python":"3.10.12"},"resources":{"cpu":1,"memory_mib":128,"shm_mib":0,"npu":0},"timeout_seconds":30},"idempotency_key":"cold-archive-fixture"})["job"]["id"]
c.request("/api/jobs/"+j+"/cancel",{});job=c.request("/api/jobs/"+j)["job"];assert job["state"]=="cancelled"
print(json.dumps({"id":j,"owner":job["owner"]}))
''');job_id=submitted['id'];job_owner=submitted['owner'];stop()
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
 stage='backup-restore'
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/after.tar.gz')
 rejected=ctl('restore','--prefix',str(prefix),'--archive','/root/backups/before.tar.gz','--new-state-dir','/state/rejected',check=False)
 assert rejected.returncode and 'cold' in rejected.stderr and not Path('/state/rejected').exists()
 destination=Path('/state/restored')
 ctl('restore','--prefix',str(prefix),'--archive','/root/backups/after.tar.gz','--new-state-dir',str(destination));state=destination
 rejected=ctl('cold-copy-step','--prefix',str(prefix),'--plan',copy_plan,check=False)
 assert rejected.returncode and 'stale' in rejected.stderr
 historical=ctl('cold-copy-verify','--prefix',str(prefix),'--plan',copy_plan)
 expected=json.loads(json.dumps(before));expected['job']['archive_dir']=str(state/'jobs'/job_id)
 assert historical['copy_bytes_verified'] and historical['historical_only'] and accounting()==expected
 with sqlite3.connect(state/'jobs.sqlite3') as db:
  assert db.execute('SELECT state FROM cold_pools').fetchone()[0]=='restore_frozen'
  assert db.execute('SELECT state FROM cold_transfers').fetchone()[0]=='restore_frozen'
 import cold_exports as cold
 write('cold-records.json',{'plan':plan,'pool_plan':json.loads(Path(pool_plan).read_text()),'before':before,'after':accounting(),
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
except BaseException as error:
 report={'passed':False,'stage':stage,'error_type':type(error).__name__,'error':str(error)[:1024],'actual_sigkill_boundaries':faults,'events':events}
finally:
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,timeout=25)
 with tarfile.open(E/'private-controller-evidence.tar.gz','x:gz') as out:
  if prefix.exists():out.add(prefix,arcname='installation')
  if state.exists():out.add(state,arcname='control')
  for p in [Path('/root/cold-register.json'),Path('/root/cold-copy.json')]:
   if p.exists():out.add(p,arcname='plans/'+p.name)
 archive=E/'private-controller-evidence.tar.gz';archive.chmod(0o600);os.chown(archive,owner,group)
 limits=Path('/run/native-fixture/cgroup.json');root=Path('/sys/fs/cgroup')
 report['parent_resource_limits_unchanged']=json.loads(limits.read_text())['limits']=={n:(root/n).read_text().strip() for n in ['cpu.max','memory.max','memory.swap.max','pids.max','cpuset.cpus.effective']}
 if not report['parent_resource_limits_unchanged']:report['passed']=False
 write('native-report.json',report)
raise SystemExit(0 if report['passed'] else 1)

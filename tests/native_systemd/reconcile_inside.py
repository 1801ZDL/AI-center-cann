"""Actual native controller/Root repair, with explicitly simulated legacy intent."""
import hashlib,json,os,signal,sqlite3,subprocess,sys,tarfile,time
from pathlib import Path
E=Path('/evidence');owner=E.stat().st_uid;group=E.stat().st_gid
assert owner>0 and os.geteuid()==0 and Path('/proc/1/comm').read_text().strip()=='systemd'
assert not Path('/var/run/docker.sock').exists() and not list(Path('/dev').glob('davinci*'))
prefix=Path('/opt/ascend-lab-controller');state=Path('/state/controller');stage='bus';events=[];report={'passed':False}
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
  write('failure-command.json',{'action':action,'stderr':r.stderr[-4096:]});raise RuntimeError('Fixed reconciliation operation refused: '+action)
 return json.loads(r.stdout)
def start(label):
 for action,filename in [('unit-manager','manager'),('unit-start','start')]:
  path='/state/'+filename+'-'+label+'.json'
  ctl(action+'-plan','--prefix',str(prefix),'--output',path)
  until=time.monotonic()+25
  while True:
   result=ctl(action+'-apply','--prefix',str(prefix),'--plan',path)
   if action=='unit-manager' or result['state']=='ready':break
   if time.monotonic()>until:raise RuntimeError('Original native startup not ready')
   time.sleep(.2)
def stop():subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
def run_owner(code,*args,check=True):
 def drop():os.setgroups([]);os.setgid(installed['gid']);os.setuid(installed['uid'])
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(prefix/'current'),str(state/'config.json'),*args],
  preexec_fn=drop,capture_output=True,text=True,timeout=25,env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8'})
 if not check:return r
 if r.returncode:
  write('failure-owner.json',{'stderr':r.stderr[-4096:]});raise RuntimeError('Own fixture owner operation refused')
 return json.loads(r.stdout)
def sql():return sqlite3.connect(state/'jobs.sqlite3')
def retained(kind='input',key=None):
 key=key or identifier
 with sql() as db:
  r=json.loads(db.execute('SELECT record FROM storage_lifecycle WHERE kind=? AND id=?',(kind,key)).fetchone()[0])
  allocation=db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind=? AND id=?',(kind,key)).fetchone()
 return r,list(allocation)
try:
 until=time.monotonic()+20
 while subprocess.run(['/usr/bin/busctl','--system','--json=short','call','org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner','s','org.freedesktop.systemd1'],capture_output=True,timeout=5).returncode:
  if time.monotonic()>until:raise RuntimeError('Original native bus unavailable')
  time.sleep(.2)
 stage='install-legacy'
 installed=ctl('install','--archive','/provision/legacy.tar.gz','--role','controller','--prefix',str(prefix),
  '--state-dir',str(state),'--create-service-user','labsvc','--port','18865',bootstrap=True)
 assert not (prefix/'current/storage_reconciliation.py').exists()
 ctl('unit-install','--prefix',str(prefix));start('legacy')
 stage='actual-input-and-service-view'
 uploaded=run_owner('''import base64,hashlib,json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);ids=[]
for raw in [b"legacy-reconciliation-bytes",b"peer-legacy-reconciliation-bytes"]:
 i=c.request("/api/inputs",{"files":[{"name":"payload.bin","size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}]})["input"]["id"]
 c.request("/api/inputs/"+i+"/blob",{"name":"payload.bin","offset":0,"data":base64.b64encode(raw).decode()});c.request("/api/inputs/"+i+"/seal",{})
 p=c.request("/api/storage/plan",{"kind":"input","id":i,"action":"trash"})["plan"];c.request("/api/storage/apply",p);ids.append(i)
print(json.dumps({"id":ids[0],"peer_id":ids[1]}))
''');identifier=uploaded['id'];peer_id=uploaded['peer_id']
 from_path=prefix/'current';sys.path.insert(0,str(from_path))
 from unit_manager import Bus
 from jobs import identity,identity_alive
 unit=Bus().unit('ascend-lab-controller.service');handle=identity(unit['MainPID']);assert handle['uid']==installed['uid'] and identity_alive(handle)
 observed=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);import storage_files as fs
print(json.dumps(fs.scan(sys.argv[3])))
''','/proc/'+str(handle['pid'])+'/root'+str(state/'storage-trash/input'/identifier))
 peer_view=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);import storage_files as fs
print(json.dumps(fs.scan(sys.argv[3])))
''','/proc/'+str(handle['pid'])+'/root'+str(state/'storage-trash/input'/peer_id))
 assert identity_alive(handle);stop()
 Path('/root/backups').mkdir(mode=0o700)
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/before.tar.gz')
 stage='simulate-retained-legacy-intent'
 # Deliberate legacy-state simulation, not an old-code SIGKILL claim. File
 # identities and original service mount IDs come from the actual service.
 old,charged=retained();observed.pop('format')
 import storage_files as fs
 old.update(state='moving_to_trash',operation='trash',inventory=observed,fingerprint=fs.digest(observed),payload_bytes=observed['bytes'])
 old['request_sha256']=fs.digest({'kind':'input','id':identifier,'action':'trash','revision':old['revision']-1,'fingerprint':old['fingerprint']})
 with sql() as db:db.execute('UPDATE storage_lifecycle SET state=?,record=? WHERE kind="input" AND id=?',(old['state'],json.dumps(old,sort_keys=True),identifier))
 peer_old,peer_charge=retained('input',peer_id);peer_view.pop('format')
 peer_old.update(state='moving_to_trash',operation='trash',inventory=peer_view,fingerprint=fs.digest(peer_view),payload_bytes=peer_view['bytes'])
 peer_old['request_sha256']=fs.digest({'kind':'input','id':peer_id,'action':'trash','revision':peer_old['revision']-1,'fingerprint':peer_old['fingerprint']})
 with sql() as db:db.execute('UPDATE storage_lifecycle SET state=?,record=? WHERE kind="input" AND id=?',(peer_old['state'],json.dumps(peer_old,sort_keys=True),peer_id))
 root_view=fs.scan(state/'storage-trash/input'/identifier)
 assert root_view['root']['mount']!=observed['root']['mount']
 write('legacy-raw-inventory.json',{'legacy':observed,'root':root_view,'original_record':old,'allocation':charged,'intent_simulated':True})
 stage='code-only-upgrade'
 normal=ctl('activate','--prefix',str(prefix),'--archive','/provision/server.tar.gz',bootstrap=True,check=False)
 assert normal.returncode and b'pending storage' in normal.stderr.encode()
 repaired_code=ctl('activate','--prefix',str(prefix),'--archive','/provision/server.tar.gz',
  '--repair-storage-kind','input','--repair-storage-id',identifier,bootstrap=True)
 assert (prefix/'current/storage_reconciliation.py').is_file() and retained()==(old,charged)
 # Avoid old modules cached above; actual mutation runs only in fresh installed
 # CLI children with the current protected release on sys.path.
 stage='root-plan'
 plan_path='/root/reconcile.json'
 preview=ctl('storage-reconcile-plan','--prefix',str(prefix),'--kind','input','--id',identifier,'--output',plan_path)
 assert preview['read_only'] and retained()==(old,charged)
 value=json.loads(Path(plan_path).read_text());assert value['original']==old
 denied=run_owner('''import subprocess,sys
r=subprocess.run(["/usr/bin/python3","-I","-B",sys.argv[1]+"/servicectl","storage-reconcile-plan","--prefix","/opt/ascend-lab-controller","--kind","input","--id",sys.argv[3],"--output","/state/not-root.json"],capture_output=True,text=True)
assert r.returncode;print(r.returncode)
''',identifier,check=False);assert denied.returncode==0 and not Path('/state/not-root.json').exists()
 stage='same-plan-real-sigkill-recovery'
 code='''import json,os,signal,sys
sys.path.insert(0,"/opt/ascend-lab-controller/current")
import storage_reconciliation as r
def fault(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
r._checkpoint=fault
r.apply("/opt/ascend-lab-controller",json.loads(Path("/root/reconcile.json").read_text()))
'''.replace('import storage_reconciliation as r','from pathlib import Path\nimport storage_reconciliation as r')
 faults=[]
 for boundary in ['prepared','updated','committed','receipt']:
  child=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,boundary],capture_output=True,text=True,timeout=25)
  if child.returncode!=-signal.SIGKILL:
   write('failure-fault.json',{'boundary':boundary,'exit_code':child.returncode,'stderr':child.stderr[-4096:]})
  assert child.returncode==-signal.SIGKILL
  # Open with the actual service UID so a rollback journal is recovered with
  # that identity; never leave Root-owned SQLite journals behind.
  def drop():os.setgroups([]);os.setgid(installed['gid']);os.setuid(installed['uid'])
  recovered=subprocess.run(['/usr/bin/python3','-I','-B','-c','import sqlite3,sys\nc=sqlite3.connect(sys.argv[1]);c.execute("BEGIN IMMEDIATE");c.rollback();c.close()',str(state/'jobs.sqlite3')],preexec_fn=drop,capture_output=True,text=True,timeout=10)
  assert recovered.returncode==0
  current,billing=retained();assert billing==charged
  assert retained('input',peer_id)==(peer_old,peer_charge)
  if boundary in {'prepared','updated'}:assert current==old
  else:assert current['inventory']['format']=='ascend-lab-storage-inventory/v2' and current['revision']==old['revision']+1
  faults.append({'boundary':boundary,'exit_code':child.returncode,'billing_unchanged':True})
 result=ctl('storage-reconcile-apply','--prefix',str(prefix),'--plan',plan_path)
 assert result['reused'] and not result['payload_mutated'] and not result['quota_released']
 current,billing=retained();assert billing==charged and current['request_sha256']==old['request_sha256']
 with sql() as db:
  assert db.execute('SELECT count(*) FROM storage_reconciliations').fetchone()[0]==1
  assert db.execute('SELECT count(*) FROM storage_lifecycle_events WHERE action="rebind-legacy-pending"').fetchone()[0]==1
  assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 assert (state/'jobs.sqlite3').stat().st_uid==installed['uid']
 write('reconciliation-record.json',{'after':current,'result':result,'billing':billing,'faults':faults})
 peer_plan='/root/peer-reconcile.json'
 ctl('storage-reconcile-plan','--prefix',str(prefix),'--kind','input','--id',peer_id,'--output',peer_plan)
 peer_result=ctl('storage-reconcile-apply','--prefix',str(prefix),'--plan',peer_plan)
 peer_after,unchanged=retained('input',peer_id);assert unchanged==peer_charge
 assert retained()==(current,billing)
 write('peer-reconciliation-record.json',{'before':peer_old,'after':peer_after,'result':peer_result,'billing':unchanged})
 stage='actual-owner-resumes-original-operation'
 # No configuration change: restart the already verified native unit. A new
 # manager/startup configuration plan correctly requires full quiescence,
 # which this intentionally unfinished owner operation does not yet satisfy.
 ctl('unit-manager-check','--prefix',str(prefix))
 subprocess.run(['/usr/bin/systemctl','start','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
 until=time.monotonic()+25
 while not ctl('status','--prefix',str(prefix))['ready']:
  if time.monotonic()>until:raise RuntimeError('Original verified unit restart not ready')
  time.sleep(.2)
 ctl('unit-manager-check','--prefix',str(prefix),'--running')
 resumed=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2])
for i in sys.argv[3:]:
 p=c.request("/api/storage/plan",{"kind":"input","id":i,"action":"resume"})["plan"];v=c.request("/api/storage/apply",p);assert v["object"]["state"]=="trash"
 p=c.request("/api/storage/plan",{"kind":"input","id":i,"action":"restore"})["plan"];v=c.request("/api/storage/apply",p);assert v["object"]["state"]=="available"
print(json.dumps({"owner_resumed":True}))
''',identifier,peer_id)
 assert (state/'inputs'/identifier/'payload.bin').read_bytes()==b'legacy-reconciliation-bytes'
 assert (state/'inputs'/peer_id/'payload.bin').read_bytes()==b'peer-legacy-reconciliation-bytes'
 stage='actual-cancelled-job-archive'
 archive=run_owner('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);j=c.request("/api/jobs",{"name":"cancelled archive reconciliation","cwd":json.loads(Path(sys.argv[2]).read_text())["scheduler"]["workspace_root"],"environment":"worker-python","template":"ir_v1","parameters":{"input_id":sys.argv[3],"argv":["python","-c","print(42)"],"lock":{"python":"3.10.12"},"resources":{"cpu":1,"memory_mib":128,"shm_mib":0,"npu":0},"timeout_seconds":30},"idempotency_key":"archive-reconciliation"})["job"]["id"]
c.request("/api/jobs/"+j+"/cancel",{});v=c.request("/api/jobs/"+j)["job"];assert v["state"]=="cancelled"
m=c.request("/api/jobs/"+j+"/archive-manifest")
p=c.request("/api/storage/plan",{"kind":"archive","id":j,"action":"trash"})["plan"];c.request("/api/storage/apply",p)
print(json.dumps({"id":j,"manifest":m}))
''',identifier);archive_id=archive['id']
 unit=Bus().unit('ascend-lab-controller.service');handle=identity(unit['MainPID']);assert handle['uid']==installed['uid'] and identity_alive(handle)
 archive_view=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);import storage_files as fs
print(json.dumps(fs.scan(sys.argv[3])))
''','/proc/'+str(handle['pid'])+'/root'+str(state/'storage-trash/archive'/archive_id))
 assert identity_alive(handle);stop();archive_view.pop('format')
 previous,archive_charge=retained('archive',archive_id)
 previous.update(state='moving_to_trash',operation='trash',inventory=archive_view,fingerprint=fs.digest(archive_view),payload_bytes=archive_view['bytes'])
 previous['request_sha256']=fs.digest({'kind':'archive','id':archive_id,'action':'trash','revision':previous['revision']-1,'fingerprint':previous['fingerprint']})
 with sql() as db:
  db.execute('UPDATE storage_lifecycle SET state=?,record=? WHERE kind="archive" AND id=?',(previous['state'],json.dumps(previous,sort_keys=True),archive_id))
  job=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(archive_id,)).fetchone()[0])
  job['archive_status']={k:previous[k] for k in ['state','revision','purge_after','metadata_bytes'] if k in previous}
  db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(job),archive_id))
 archive_plan='/root/archive-reconcile.json'
 ctl('storage-reconcile-plan','--prefix',str(prefix),'--kind','archive','--id',archive_id,'--output',archive_plan)
 archive_result=ctl('storage-reconcile-apply','--prefix',str(prefix),'--plan',archive_plan)
 current,unchanged=retained('archive',archive_id);assert unchanged==archive_charge
 with sql() as db:
  job=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(archive_id,)).fetchone()[0])
  assert job['archive_status']['revision']==current['revision']==previous['revision']+1 and job['state']=='cancelled'
 ctl('unit-manager-check','--prefix',str(prefix))
 subprocess.run(['/usr/bin/systemctl','start','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
 until=time.monotonic()+25
 while not ctl('status','--prefix',str(prefix))['ready']:
  if time.monotonic()>until:raise RuntimeError('Original archive-case native unit not ready')
  time.sleep(.2)
 after=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);j=sys.argv[3]
for action in ["resume","restore"]:
 p=c.request("/api/storage/plan",{"kind":"archive","id":j,"action":action})["plan"];c.request("/api/storage/apply",p)
print(json.dumps(c.request("/api/jobs/"+j+"/archive-manifest")))
''',archive_id)
 assert after==archive['manifest'];stop()
 write('archive-reconciliation-record.json',{'before':previous,'after_rebind':current,'result':archive_result,'allocation':unchanged,'manifest_preserved':True})
 stage='backup-lineage-and-relocation'
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/after.tar.gz')
 refused=ctl('restore','--prefix',str(prefix),'--archive','/root/backups/before.tar.gz','--new-state-dir','/state/rejected',check=False)
 assert refused.returncode and 'reconciliation' in refused.stderr and not Path('/state/rejected').exists()
 destination=Path('/state/restored')
 ctl('restore','--prefix',str(prefix),'--archive','/root/backups/after.tar.gz','--new-state-dir',str(destination));state=destination
 stale=ctl('storage-reconcile-apply','--prefix',str(prefix),'--plan',plan_path,check=False)
 assert stale.returncode and 'stale' in stale.stderr
 assert (state/'inputs'/identifier/'payload.bin').read_bytes()==b'legacy-reconciliation-bytes'
 with sql() as db:assert db.execute('SELECT count(*) FROM storage_reconciliations').fetchone()[0]==3
 report={'passed':True,'stage':stage,'actual_systemd_pid1':True,'controller_uid':installed['uid'],'legacy_intent_simulated':True,
  'raw_service_and_root_mount_ids_differ':True,'initial_release_has_no_reconciliation_helper':True,
  'ordinary_activation_refused_pending':True,'explicit_code_only_upgrade_preserved_payload_and_billing':True,
  'actual_sigkill_boundaries':faults,'same_original_plan_reused':True,'one_ledger_receipt_and_event_per_plan':True,'ledger_receipts_at_backup':3,
  'multiple_pending_reconciled_without_mutating_peer':True,'both_original_input_bytes_verified':True,
  'root_plan_and_apply_move_or_delete_no_payload':True,'no_quota_credit_from_rebind':True,'nonroot_reconciliation_refused':True,
  'actual_owner_resumed_original_operation':True,'original_bytes_verified':True,'older_backup_without_receipt_refused':True,
  'actual_cancelled_archive_manifest_preserved':True,'archive_job_status_revision_updated':True,'archive_rebind_no_billing_change':True,
  'latest_backup_restored_receipt_and_bytes':True,'old_plan_refused_after_state_relocation':True,
  'physical_hosts':1,'native_node_tested':False,'npu_launches':0,'events':events}
except BaseException as error:
 report={'passed':False,'stage':stage,'error_type':type(error).__name__,'error':str(error)[:1024],'events':events}
finally:
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,timeout=25)
 with tarfile.open(E/'private-controller-evidence.tar.gz','x:gz') as out:
  if prefix.exists():out.add(prefix,arcname='installation')
  if state.exists():out.add(state,arcname='control')
 archive=E/'private-controller-evidence.tar.gz';archive.chmod(0o600);os.chown(archive,owner,group)
 before=json.loads(Path('/run/native-fixture/cgroup.json').read_text())['limits']
 report['parent_resource_limits_unchanged']=before=={n:Path('/sys/fs/cgroup',n).read_text().strip() for n in before}
 if not report['parent_resource_limits_unchanged']:report['passed']=False
 write('native-report.json',report)
raise SystemExit(0 if report['passed'] else 1)

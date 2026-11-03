"""Installed Root control-snapshot copy, real kills and service/task UID boundary."""
import errno,hashlib,json,os,signal,sqlite3,subprocess,sys,tarfile,time
from pathlib import Path

E=Path('/evidence');owner=E.stat().st_uid;group=E.stat().st_gid
assert os.geteuid()==0 and owner>0 and Path('/proc/1/comm').read_text().strip()=='systemd'
assert not Path('/var/run/docker.sock').exists() and not list(Path('/dev').glob('davinci*'))
prefix=Path('/opt/ascend-lab-controller');state=Path('/state/controller');events=[];faults=[];stage='bus';report={'passed':False}
def write(name,value):
 p=E/name
 with p.open('x') as f:json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
 p.chmod(0o600);os.chown(p,owner,group)
def ctl(action,*args,bootstrap=False,check=True):
 tool='/provision/bootstrap/servicectl' if bootstrap else str(prefix/'current/servicectl')
 r=subprocess.run(['/usr/bin/python3','-I','-B',tool,action,*args],capture_output=True,text=True,timeout=35)
 events.append({'action':action,'exit_code':r.returncode})
 if not check and r.returncode:write('rejection-'+str(len(events))+'.json',{'action':action,'stderr':r.stderr[-4096:],'exit_code':r.returncode})
 if check and r.returncode:
  write('failure-command.json',{'action':action,'stderr':r.stderr[-4096:]});raise RuntimeError('Original control-history operation refused: '+action)
 return json.loads(r.stdout) if check else r
def start(label):
 for action in ['unit-manager','unit-start']:
  path='/state/'+action+'-'+label+'.json';ctl(action+'-plan','--prefix',str(prefix),'--output',path)
  until=time.monotonic()+25
  while True:
   value=ctl(action+'-apply','--prefix',str(prefix),'--plan',path)
   if action=='unit-manager' or value['state']=='ready':break
   if time.monotonic()>until:raise RuntimeError('Original startup not ready')
   time.sleep(.2)
def stop():subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
def restart():
 ctl('unit-manager-check','--prefix',str(prefix));subprocess.run(['/usr/bin/systemctl','start','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
 until=time.monotonic()+25
 while not ctl('status','--prefix',str(prefix))['ready']:
  if time.monotonic()>until:raise RuntimeError('Original verified unit restart not ready')
  time.sleep(.2)
def drop():os.setgroups([]);os.setgid(installed['gid']);os.setuid(installed['uid'])
def actor(code,*args):
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(prefix/'current'),str(state/'config.json'),*args],preexec_fn=drop,
  capture_output=True,text=True,timeout=25,env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8'})
 if r.returncode:write('failure-owner.json',{'stderr':r.stderr[-4096:]});raise RuntimeError('Original fixture service identity operation refused')
 return json.loads(r.stdout)
def counts():
 with sqlite3.connect(state/'jobs.sqlite3') as db:
  return {name:list(map(list,db.execute('SELECT * FROM '+name+' ORDER BY rowid'))) for name in ['jobs','execution_attempts','workflow_results','storage_allocations','job_graphs']}
try:
 until=time.monotonic()+20
 while subprocess.run(['/usr/bin/busctl','--system','--json=short','call','org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner','s','org.freedesktop.systemd1'],capture_output=True,timeout=5).returncode:
  if time.monotonic()>until:raise RuntimeError('Original native bus unavailable')
  time.sleep(.2)
 stage='install'
 installed=ctl('install','--archive','/provision/server.tar.gz','--role','controller','--prefix',str(prefix),'--state-dir',str(state),'--create-service-user','labsvc','--port','18865',bootstrap=True)
 # This isolated controller explicitly chooses a one-second test grace. No Main
 # policy, timestamp, existing payload or other user's service is modified.
 cfg=state/'config.json';config=json.loads(cfg.read_text());config['scheduler']['storage_trash_grace_seconds']=1
 cfg.write_text(json.dumps(config));cfg.chmod(0o600)
 ctl('unit-install','--prefix',str(prefix));start('initial')
 stage='original-graph'
 original=actor('''import base64,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);raw=b"control-history-input"
i=c.request("/api/inputs",{"files":[{"name":"data.bin","size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}]})["input"]["id"]
c.request("/api/inputs/"+i+"/blob",{"name":"data.bin","offset":0,"data":base64.b64encode(raw).decode()});c.request("/api/inputs/"+i+"/seal",{})
body={"cwd":json.loads(Path(sys.argv[2]).read_text())["scheduler"]["workspace_root"],"environment":"worker-python","template":"ir_v1","parameters":{"input_id":i,"argv":["python","-c","print(42)"],"lock":{"python":"3.10.12"},"resources":{"cpu":1,"memory_mib":128,"shm_mib":0,"npu":0},"timeout_seconds":30}}
request={"idempotency_key":"control-history-graph","nodes":[{"key":"first","job":body},{"key":"second","job":body,"after":["first"],"artifact_inputs":[{"node":"first","name":"results/package.bin","target":"previous.bin"}]}]}
import urllib.request,urllib.error
try:
 with c.opener.open(urllib.request.Request(c.url+"/api/job-graphs",data=json.dumps(request).encode(),headers={"Authorization":c.authorization,"Content-Type":"application/json"}),timeout=10) as response:graph=json.loads(response.read())["graph"]
except urllib.error.HTTPError as error:
 detail=json.loads(error.read(4096));raise RuntimeError("Fixed graph request rejected: "+str(detail.get("error","unknown"))) from None
for node in graph["nodes"]:c.request("/api/jobs/"+node["job_id"]+"/cancel",{})
assert all(c.request("/api/jobs/"+node["job_id"])["job"]["state"]=="cancelled" for node in graph["nodes"])
print(json.dumps({"graph_id":graph["id"],"first":graph["nodes"][0]["job_id"],"jobs":[n["job_id"] for n in graph["nodes"]],"request":request,"body":body}))
''')
 stop();assert len(counts()['jobs'])==2 and not counts()['execution_attempts']
 ctl('history-capacity-plan','--prefix',str(prefix),'--max-control-jobs','2','--max-online-archives','2','--output','/root/capacity.json')
 ctl('history-capacity-apply','--prefix',str(prefix),'--plan','/root/capacity.json');restart()
 stage='original-payload-removal'
 purged=actor('''import json,sys,time
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);j=sys.argv[3]
p=c.request("/api/storage/plan",{"kind":"archive","id":j,"action":"trash"})["plan"];c.request("/api/storage/apply",p)
time.sleep(1.2)
p=c.request("/api/storage/plan",{"kind":"archive","id":j,"action":"purge"})["plan"];v=c.request("/api/storage/apply",p);assert v["object"]["state"]=="purged" and not v["pending"]
original=c.request("/api/jobs/"+j)["job"];capacity=c.request("/api/jobs")["history_capacity"]
assert capacity["usage"]=={"control_jobs":2,"online_archives":1}
print(json.dumps({"original":original,"usage":capacity["usage"]}))
''',original['first']);stop();before=counts()
 assert not (state/'jobs'/original['first']).exists()
 Path('/root/backups').mkdir(mode=0o700)
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/before-rotation.tar.gz')
 stage='verified-copy'
 copy_path='/root/control-history.json'
 ctl('control-history-plan','--prefix',str(prefix),'--job-id',original['first'],'--budget-bytes',str(1024**2),'--max-exports','1','--output',copy_path)
 copy_plan=json.loads(Path(copy_path).read_text());copied=ctl('control-history-step','--prefix',str(prefix),'--plan',copy_path)
 assert copied['content_verified'] and counts()==before
 target=prefix/'control-history'/(copy_plan['id']+'.json');raw=target.read_bytes();assert hashlib.sha256(raw).hexdigest()==copy_plan['source']['sha256']
 # Actual numeric task identity cannot read the Root/service-group copy.
 def task_identity():os.setgroups([]);os.setgid(23901);os.setuid(23901)
 denied=subprocess.run(['/usr/bin/python3','-I','-B','-c','import errno,sys\ntry:open(sys.argv[1],"rb")\nexcept OSError as e:assert e.errno==errno.EACCES\nelse:raise AssertionError("Task read control metadata")',str(target)],preexec_fn=task_identity,capture_output=True,timeout=10)
 assert denied.returncode==0
 stage='rotation-plan'
 rotate_path='/root/control-rotate.json'
 ctl('control-rotate-plan','--prefix',str(prefix),'--export-id',copy_plan['id'],'--output',rotate_path)
 rotation=json.loads(Path(rotate_path).read_text());assert counts()==before
 stage='actual-migration-sigkills'
 for boundary in ['before_commit','committed']:
  code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current");import control_rotation as r
def kill(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
r._checkpoint=kill;r.apply("/opt/ascend-lab-controller",json.loads(Path("/root/control-rotate.json").read_text()))
'''
  result=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,boundary],capture_output=True,text=True,timeout=30)
  if result.returncode!=-9:write('failure-fault.json',{'boundary':boundary,'exit_code':result.returncode,'stderr':result.stderr[-4096:]})
  assert result.returncode==-9;faults.append({'boundary':boundary,'exit_code':result.returncode})
  # Recover the original rollback journal as the actual service identity.
  recover=subprocess.run(['/usr/bin/python3','-I','-B','-c','import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute("BEGIN IMMEDIATE");c.rollback()',str(state/'jobs.sqlite3')],preexec_fn=drop,capture_output=True,timeout=10)
  assert recover.returncode==0
  if boundary=='before_commit':assert counts()==before
  else:
   with sqlite3.connect(state/'jobs.sqlite3') as db:
    compact=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(original['first'],)).fetchone()[0]);assert '_control_history_ref' in compact and '_token' not in compact
    assert db.execute('SELECT count(*) FROM control_rotation_jobs').fetchone()[0]==1 and db.execute('SELECT count(*) FROM control_rotation_changes').fetchone()[0]==1
 rotated=ctl('control-rotate-apply','--prefix',str(prefix),'--plan',rotate_path);assert rotated['reused'] and rotated['normal_read_authority'] and rotated['control_slots_released']==1
 rejected=ctl('activate','--prefix',str(prefix),'--archive','/provision/legacy.tar.gz',check=False);assert rejected.returncode and 'Migrated control history' in rejected.stderr
 stage='service-ro-reads-and-new-admission'
 restart()
 checked=actor('''import json,runpy,subprocess,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);source=json.loads(sys.argv[3]);old=json.loads(sys.argv[4]);j=source["first"]
assert c.request("/api/jobs/"+j)["job"]==old
usage=c.request("/api/jobs")["history_capacity"]["usage"];assert usage=={"control_jobs":1,"online_archives":1}
page=c.request("/api/jobs/history?limit=1&offset=1");assert page["total"]==2 and page["jobs"][0]==old
assert c.request("/api/jobs/"+j)["attempts"]==[]
graph=c.request("/api/job-graphs",source["request"])["graph"];assert graph["id"]==source["graph_id"] and {n["job_id"] for n in graph["nodes"]}==set(source["jobs"])
v=c.request("/api/control-history-exports/"+sys.argv[5]);assert v["content_verified"] and v["readonly_mount"]
body={**source["body"],"idempotency_key":"after-control-rotation"}
new=c.request("/api/jobs",body)["job"];c.request("/api/jobs/"+new["id"]+"/cancel",{})
assert c.request("/api/jobs")["history_capacity"]["usage"]=={"control_jobs":2,"online_archives":2}
try:c.request("/api/jobs",{**body,"idempotency_key":"still-finite"})
except ClientHTTPError as e:assert e.status==409
else:raise AssertionError("Finite capacity was bypassed")
loader='import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")'
r=subprocess.run(["/usr/bin/python3","-I","-B","-c",loader,sys.argv[1],sys.argv[1]+"/accessctl","--state-dir",str(Path(sys.argv[2]).parent),"user-add","history-peer"],capture_output=True);assert r.returncode==0
peer=Client.from_profile(Path(sys.argv[2]).parent/"personas/history-peer/client.json")
for path in ["/api/jobs/"+j,"/api/control-history-exports/"+sys.argv[5]]:
 try:peer.request(path)
 except ClientHTTPError as e:assert e.status==404
 else:raise AssertionError("Peer read private original history")
assert c.request("/api/jobs/"+j)["job"]==old
print(json.dumps({"new_job":new["id"],"usage_before":usage,"usage_after":{"control_jobs":2,"online_archives":2},"service_ro_verified":True,"current_peer_acl_denied":True,"original_graph_replay":True,"original_job_detail_and_page_unchanged":True,"finite_limit_still_enforced":True}))
''',json.dumps(original),json.dumps(purged['original']),copy_plan['id']);stop()
 after=counts();assert len(after['jobs'])==3 and not after['execution_attempts']
 for name in ['workflow_results','storage_allocations','job_graphs']:
  for row in before[name]:assert row in after[name]
 stage='backup-restore'
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/after-rotation.tar.gz')
 rejected=ctl('restore','--prefix',str(prefix),'--archive','/root/backups/before-rotation.tar.gz','--new-state-dir','/state/rejected-history',check=False)
 assert rejected.returncode and not Path('/state/rejected-history').exists()
 ctl('restore','--prefix',str(prefix),'--archive','/root/backups/after-rotation.tar.gz','--new-state-dir','/state/restored');state=Path('/state/restored')
 rejected=ctl('control-rotate-apply','--prefix',str(prefix),'--plan',rotate_path,check=False);assert rejected.returncode
 stale=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import json,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current");from jobs import JobError;from control_rotation import apply
try:apply("/opt/ascend-lab-controller",json.loads(Path("/root/control-rotate.json").read_text()))
except JobError as error:assert error.status==409 and "binding" in str(error)
else:raise AssertionError("Old write plan survived restore")
'''],capture_output=True,timeout=20);assert stale.returncode==0
 assert ctl('control-history-verify','--prefix',str(prefix),'--plan',copy_path)['content_verified']
 ctl('unit-refresh-plan','--prefix',str(prefix),'--output','/root/history-unit-refresh.json');ctl('unit-refresh-apply','--prefix',str(prefix),'--plan','/root/history-unit-refresh.json');start('restored')
 restored=actor('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);old=json.loads(sys.argv[3]);job=c.request("/api/jobs/"+old["id"])["job"]
old.pop("archive_dir",None);job.pop("archive_dir",None);assert job==old
assert c.request("/api/jobs")["history_capacity"]["usage"]=={"control_jobs":2,"online_archives":2}
assert c.request("/api/control-history-exports/"+sys.argv[4])["content_verified"]
print(json.dumps({"original_job_semantics_preserved":True,"compact_capacity_credit_retained":True,"historical_file_ro_read":True}))
''',json.dumps(purged['original']),copy_plan['id']);stop()
 write('control-rotation-records.json',{'copy_plan':copy_plan,'rotation_plan':rotation,'before':before,'after':counts(),'original':original,'original_public':purged['original'],'service_checks':checked,'restore_checks':restored,'rotation_response':rotated})
 report={'passed':True,'stage':stage,'actual_systemd_pid1':True,'actual_sigkill_boundaries':faults,'original_jobs':2,'new_jobs':1,'source_execution_attempts':0,
  'actual_original_archive_purge':True,'private_copy_verified_before_body_migration':True,'sql_body_migrated_compact_identity_retained':True,'normal_read_authority':True,'control_slots_released':1,
  'original_graph_and_identity_replay':True,'current_peer_acl_denied':True,'actual_service_readonly_view_verified':True,'old_code_and_snapshot_refused':True,'latest_restore_preserves_read_and_credit':True,
  'other_facts_and_budget_unchanged':True,'numeric_task_uid':23901,'numeric_task_private_file_denied':True,'private_failure_domain_separation_proven':False,'physical_hosts':1,'model_calls':0,'npu_launches':0,'production_ready':False,'events':events}
except BaseException as e:
 report={'passed':False,'stage':stage,'error_type':type(e).__name__,'error':str(e)[:1024],'actual_sigkill_boundaries':faults,'events':events}
finally:
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,timeout=25)
 with tarfile.open(E/'private-controller-evidence.tar.gz','x:gz') as archive:
  if prefix.exists():archive.add(prefix,arcname='installation')
  if state.exists():archive.add(state,arcname='control')
  for name in ['control-history','control-rotate']:
   p=Path('/root/'+name+'.json')
   if p.exists():archive.add(p,arcname='plans/'+name+'.json')
 os.chmod(E/'private-controller-evidence.tar.gz',0o600);os.chown(E/'private-controller-evidence.tar.gz',owner,group)
 limits=['cpu.max','memory.max','memory.swap.max','pids.max','cpuset.cpus.effective']
 report['parent_resource_limits_unchanged']=json.loads(Path('/run/native-fixture/cgroup.json').read_text())['limits']=={n:Path('/sys/fs/cgroup',n).read_text().strip() for n in limits}
 if not report['parent_resource_limits_unchanged']:report['passed']=False
 write('native-report.json',report)

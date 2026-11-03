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
print(json.dumps({"graph_id":graph["id"],"first":graph["nodes"][0]["job_id"],"jobs":[n["job_id"] for n in graph["nodes"]]}))
''');stop();before=counts();assert len(before['jobs'])==2 and not before['execution_attempts']
 Path('/root/backups').mkdir(mode=0o700)
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/before-history.tar.gz')
 plan_path='/root/control-history.json';stage='plan'
 ctl('control-history-plan','--prefix',str(prefix),'--job-id',original['first'],'--budget-bytes',str(1024**2),'--max-exports','1','--output',plan_path)
 plan=json.loads(Path(plan_path).read_text());assert counts()==before
 stage='actual-sigkills'
 for boundary in ['reserved','file_created','data_synced','progress_committed','sealed','published','verified']:
  code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current");import control_history as h
def kill(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
h._checkpoint=kill;h.step("/opt/ascend-lab-controller",json.loads(Path("/root/control-history.json").read_text()))
'''
  r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,boundary],capture_output=True,text=True,timeout=30)
  if r.returncode!=-9:write('failure-fault.json',{'boundary':boundary,'exit_code':r.returncode,'stderr':r.stderr[-4096:]})
  assert r.returncode==-9;faults.append({'boundary':boundary,'exit_code':r.returncode})
  recover=subprocess.run(['/usr/bin/python3','-I','-B','-c','import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute("BEGIN IMMEDIATE");c.rollback()',str(state/'jobs.sqlite3')],preexec_fn=drop,capture_output=True,timeout=10)
  assert recover.returncode==0 and counts()==before
  if boundary!='verified':
   rejected=ctl('backup','--prefix',str(prefix),'--output','/root/backups/pending-history.tar.gz',check=False)
   assert rejected.returncode and 'control history' in rejected.stderr
  with sqlite3.connect(state/'jobs.sqlite3') as db:
   assert db.execute('SELECT count(*) FROM control_history_exports').fetchone()[0]==1
   blob=db.execute('SELECT source FROM control_history_exports').fetchone()[0]
   assert (blob is None)==(boundary=='verified')
 stage='verified-copy'
 copied=ctl('control-history-step','--prefix',str(prefix),'--plan',plan_path);assert copied['reused'] and copied['content_verified']
 assert not any(copied[k] for k in ['normal_read_authority','control_capacity_released','online_capacity_released','authorization_restored'])
 assert ctl('control-history-verify','--prefix',str(prefix),'--plan',plan_path)['content_verified']
 target=prefix/'control-history'/(plan['id']+'.json');info=target.stat()
 assert info.st_uid==0 and info.st_gid==installed['gid'] and info.st_mode&0o777==0o440
 body=json.loads(target.read_text());assert hashlib.sha256(target.read_bytes()).hexdigest()==plan['source']['sha256']
 assert any(row==before['jobs'][0] for row in body['tables']['jobs']['rows'])
 assert any(row[0]==original['graph_id'] for row in body['tables']['job_graphs']['rows'])
 assert counts()==before
 # Numeric task identity has no service group; no host account is created.
 def task_identity():os.setgroups([]);os.setgid(23901);os.setuid(23901)
 denied=subprocess.run(['/usr/bin/python3','-I','-B','-c','import errno,json,sys\ntry:open(sys.argv[1],"rb")\nexcept OSError as e:assert e.errno==errno.EACCES;print(json.dumps({"denied":True}))\nelse:raise AssertionError("Task identity read private control snapshot")',str(target)],preexec_fn=task_identity,capture_output=True,text=True,timeout=10)
 assert denied.returncode==0 and json.loads(denied.stdout)['denied']
 rejected=ctl('activate','--prefix',str(prefix),'--archive','/provision/legacy.tar.gz',check=False)
 assert rejected.returncode and 'control history' in rejected.stderr
 stage='actual-service-read'
 restart()
 checked=actor('''import json,runpy,subprocess,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2]);v=c.request("/api/control-history-exports/"+sys.argv[3]);assert v["content_verified"] and v["readonly_mount"] and not v["control_capacity_released"]
manifest=c.request("/api/jobs/"+sys.argv[4]+"/archive-manifest");assert "control-history" not in json.dumps(manifest)
loader='import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")'
r=subprocess.run(["/usr/bin/python3","-I","-B","-c",loader,sys.argv[1],sys.argv[1]+"/accessctl","--state-dir",str(Path(sys.argv[2]).parent),"user-add","history-peer"],capture_output=True);assert r.returncode==0
peer=Client.from_profile(Path(sys.argv[2]).parent/"personas/history-peer/client.json")
try:peer.request("/api/control-history-exports/"+sys.argv[3])
except ClientHTTPError as e:assert e.status==404
else:raise AssertionError("Peer read administrator export status")
print(json.dumps({"service_readonly_mount":v["readonly_mount"],"service_verified_private_bytes":v["content_verified"],"peer_status_denied":True,"original_manifest_retained":True}))
''',plan['id'],original['first']);stop();assert counts()==before
 stage='backup-restore'
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/after-history.tar.gz')
 rejected=ctl('restore','--prefix',str(prefix),'--archive','/root/backups/before-history.tar.gz','--new-state-dir','/state/rejected-history',check=False)
 assert rejected.returncode and 'control history' in rejected.stderr and not Path('/state/rejected-history').exists()
 ctl('restore','--prefix',str(prefix),'--archive','/root/backups/after-history.tar.gz','--new-state-dir','/state/restored');state=Path('/state/restored')
 with sqlite3.connect(state/'jobs.sqlite3') as db:
  assert db.execute('SELECT state FROM control_history_exports').fetchone()[0]=='restore_frozen'
  assert db.execute('SELECT source FROM control_history_exports').fetchone()[0] is None
 assert hashlib.sha256(target.read_bytes()).hexdigest()==plan['source']['sha256']
 assert ctl('control-history-verify','--prefix',str(prefix),'--plan',plan_path)['content_verified']
 rejected=ctl('control-history-step','--prefix',str(prefix),'--plan',plan_path,check=False);assert rejected.returncode and 'binding' in rejected.stderr
 ctl('unit-refresh-plan','--prefix',str(prefix),'--output','/root/history-unit-refresh.json')
 ctl('unit-refresh-apply','--prefix',str(prefix),'--plan','/root/history-unit-refresh.json');start('restored')
 restored=actor('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);v=c.request("/api/control-history-exports/"+sys.argv[3]);assert v["state"]=="restore_frozen" and v["content_verified"]
assert all(c.request("/api/jobs/"+j)["job"]["state"]=="cancelled" for j in json.loads(sys.argv[4]))
print(json.dumps({"original_jobs_visible":True,"historical_copy_verified":True,"write_authority_frozen":True}))
''',plan['id'],json.dumps(original['jobs']));stop()
 write('control-history-records.json',{'plan':plan,'before':before,'after':counts(),'original':original,'service_checks':checked,'restore_checks':restored,'copy_response':copied})
 report={'passed':True,'stage':stage,'actual_systemd_pid1':True,'actual_sigkill_boundaries':faults,
  'selected_jobs':1,'original_jobs':2,'source_execution_attempts':0,'source_graph_submitted_and_cancelled':True,
  'original_sql_rows_results_attempts_and_billing_retained':True,'frozen_source_cleared_only_after_copy_verified':True,
  'private_root_file_group_readable_by_service_only':True,'numeric_task_uid':23901,'numeric_task_file_read_denied':True,
  'actual_service_readonly_view_verified':True,'peer_admin_status_denied':True,'old_code_and_snapshot_refused':True,
  'latest_restore_keeps_copy_but_old_write_plan_is_stale':True,'normal_read_authority':False,'control_capacity_released':False,
  'metadata_rotation_implemented':False,'physical_failure_domain_separation_proven':False,'physical_hosts':1,'model_calls':0,'npu_launches':0,'events':events}
except BaseException as e:
 report={'passed':False,'stage':stage,'error_type':type(e).__name__,'error':str(e)[:1024],'actual_sigkill_boundaries':faults,'events':events}
finally:
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,timeout=25)
 with tarfile.open(E/'private-controller-evidence.tar.gz','x:gz') as archive:
  if prefix.exists():archive.add(prefix,arcname='installation')
  if state.exists():archive.add(state,arcname='control')
  p=Path('/root/control-history.json')
  if p.exists():archive.add(p,arcname='plans/control-history.json')
 os.chmod(E/'private-controller-evidence.tar.gz',0o600);os.chown(E/'private-controller-evidence.tar.gz',owner,group)
 write('native-report.json',report)

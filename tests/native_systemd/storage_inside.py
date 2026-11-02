"""Actual offline restore plus service mount view, fixed owned controller only."""
import base64,hashlib,json,os,sqlite3,subprocess,sys,tarfile,time
from pathlib import Path
EXPECTED='portable'
E=Path('/evidence');owner=E.stat().st_uid;group=E.stat().st_gid
assert owner>0 and os.geteuid()==0 and Path('/proc/1/comm').read_text().strip()=='systemd'
assert not Path('/var/run/docker.sock').exists() and not list(Path('/dev').glob('davinci*'))
prefix=Path('/opt/ascend-lab-controller');state=Path('/state/controller');new=Path('/state/controller-restored')
report={'passed':False};stage='bus';events=[]
def write(name,data):
 p=E/name;fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())
 os.chown(p,owner,group)
def ctl(action,*args,bootstrap=False):
 tool='/provision/bootstrap/servicectl' if bootstrap else str(prefix/'current/servicectl')
 r=subprocess.run(['/usr/bin/python3','-I','-B',tool,action,*args],capture_output=True,text=True,timeout=35)
 events.append({'action':action,'exit_code':r.returncode})
 if r.returncode:
  write('failure-command.json',{'action':action,'stderr':r.stderr[-4096:]});raise RuntimeError('Fixed operation refused: '+action)
 return json.loads(r.stdout)
def start(label):
 ctl('unit-manager-plan','--prefix',str(prefix),'--output','/state/manager-'+label+'.json')
 ctl('unit-manager-apply','--prefix',str(prefix),'--plan','/state/manager-'+label+'.json')
 ctl('unit-start-plan','--prefix',str(prefix),'--output','/state/start-'+label+'.json')
 until=time.monotonic()+25
 while True:
  v=ctl('unit-start-apply','--prefix',str(prefix),'--plan','/state/start-'+label+'.json')
  if v['state']=='ready':return
  if time.monotonic()>until:raise RuntimeError('Original fixed startup did not become ready')
  time.sleep(.2)
def run_owner(code,*args):
 def drop():os.setgroups([]);os.setgid(installed['gid']);os.setuid(installed['uid'])
 r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(prefix/'current'),str(state/'config.json'),*args],
  preexec_fn=drop,capture_output=True,text=True,timeout=20,env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8'})
 if r.returncode:
  write('failure-owner.json',{'stderr':r.stderr[-4096:]});raise RuntimeError('Own fixture owner operation refused')
 return json.loads(r.stdout)
try:
 until=time.monotonic()+20
 while True:
  r=subprocess.run(['/usr/bin/busctl','--system','--json=short','call','org.freedesktop.DBus',
   '/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner','s','org.freedesktop.systemd1'],capture_output=True,text=True,timeout=5)
  if r.returncode==0:break
  if time.monotonic()>until:raise RuntimeError('Original native system bus unavailable')
  time.sleep(.2)
 stage='install'
 installed=ctl('install','--archive','/provision/server.tar.gz','--role','controller','--prefix',str(prefix),
  '--state-dir',str(state),'--create-service-user','labsvc','--port','18865',bootstrap=True)
 ctl('unit-install','--prefix',str(prefix));start('initial')
 stage='input-trash'
 uploaded=run_owner('''import base64,hashlib,json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);raw=b"namespace-test";v=c.request("/api/inputs",{"files":[{"name":"payload.bin","size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}]})["input"];i=v["id"]
c.request("/api/inputs/"+i+"/blob",{"name":"payload.bin","offset":0,"data":base64.b64encode(raw).decode()});c.request("/api/inputs/"+i+"/seal",{})
p=c.request("/api/storage/plan",{"kind":"input","id":i,"action":"trash"})["plan"];assert c.request("/api/storage/apply",p)["object"]["state"]=="trash"
print(json.dumps({"id":i}))
''');identifier=uploaded['id']
 stage='offline-backup-restore'
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],check=True,capture_output=True,timeout=25)
 Path('/root/backups').mkdir(mode=0o700,exist_ok=True)
 ctl('backup','--prefix',str(prefix),'--output','/root/backups/snapshot.tar.gz')
 ctl('restore','--prefix',str(prefix),'--archive','/root/backups/snapshot.tar.gz','--new-state-dir',str(new))
 sys.path.insert(0,str(prefix/'current'));import storage_files as fs
 with sqlite3.connect((new/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
  record=json.loads(db.execute('SELECT record FROM storage_lifecycle WHERE kind="input" AND id=?',(identifier,)).fetchone()[0])
 root_view=fs.scan(new/'storage-trash/input'/identifier)
 stage='refresh-restored-unit'
 ctl('unit-refresh-plan','--prefix',str(prefix),'--output','/state/refresh.json')
 ctl('unit-refresh-apply','--prefix',str(prefix),'--plan','/state/refresh.json');start('restored')
 from unit_manager import Bus
 from jobs import identity,identity_alive
 unit=Bus().unit('ascend-lab-controller.service');handle=identity(unit['MainPID']);assert handle['uid']==installed['uid'] and identity_alive(handle)
 stage='service-mount-readback'
 scan_code='''import json,sys
sys.path.insert(0,sys.argv[1]);import storage_files as fs
print(json.dumps(fs.scan(sys.argv[3])))
'''
 service_view=run_owner(scan_code,'/proc/'+str(handle['pid'])+'/root'+str(new/'storage-trash/input'/identifier))
 assert identity_alive(handle)
 write('namespace-storage-raw.json',{'saved':record['inventory'],'root':root_view,'service':service_view})
 def portable(v):
  if isinstance(v,dict):return {k:portable(x) for k,x in v.items() if k!='mount'}
  if isinstance(v,list):return [portable(x) for x in v]
  return v
 assert record['inventory']!=service_view and portable(record['inventory'])==portable(service_view)
 stage='owner-restore-plan'
 outcome=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client,ClientHTTPError
c=Client.from_profile(sys.argv[2])
try:p=c.request("/api/storage/plan",{"kind":"input","id":sys.argv[3],"action":"restore"})["plan"]
except ClientHTTPError as e:print(json.dumps({"status":e.status}))
else:print(json.dumps({"status":200,"plan":p}))
''',identifier)
 assert outcome['status']==(409 if EXPECTED=='legacy-rejection' else 200)
 if EXPECTED!='legacy-rejection':
  restored=run_owner('''import json,sys
sys.path.insert(0,sys.argv[1]);from lab_client import Client
c=Client.from_profile(sys.argv[2]);r=c.request("/api/storage/apply",json.loads(sys.argv[3]));assert r["object"]["state"]=="available";print(json.dumps({"restored":True}))
''',json.dumps(outcome['plan']));assert restored['restored']
  assert (new/'inputs'/identifier/'payload.bin').read_bytes()==b'namespace-test'
 report={'passed':True,'stage':stage,'expected':EXPECTED,'actual_systemd_pid1':True,'controller_uid':installed['uid'],
  'only_mount_ids_differ':True,'root_and_service_have_same_inode_metadata':True,'owner_restore_plan_http_status':outcome['status'],
  'input_restored_and_bytes_verified':EXPECTED!='legacy-rejection','actual_native_node_pid1':False,'npu_launches':0,'events':events}
except BaseException as error:
 report={'passed':False,'stage':stage,'error_type':type(error).__name__,'error':str(error)[:1024],'events':events}
finally:
 subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,timeout=25)
 with tarfile.open(E/'private-controller-evidence.tar.gz','x:gz') as out:
  if prefix.exists():out.add(prefix,arcname='installation')
  selected=new if new.exists() else state
  if selected.exists():out.add(selected,arcname='control')
 archive=E/'private-controller-evidence.tar.gz';archive.chmod(0o600);os.chown(archive,owner,group)
 names=['cpu.max','memory.max','memory.swap.max','pids.max','cpuset.cpus.effective']
 before=json.loads(Path('/run/native-fixture/cgroup.json').read_text())['limits']
 report['parent_resource_limits_unchanged']=before=={n:Path('/sys/fs/cgroup',n).read_text().strip() for n in names}
 if not report['parent_resource_limits_unchanged']:report['passed']=False
 write('native-report.json',report)
raise SystemExit(0 if report['passed'] else 1)

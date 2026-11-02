"""Fixed Root acceptance service under the actual namespace PID1; no task devices."""
import hashlib,json,os,signal,subprocess,sys,tarfile,time
from pathlib import Path
assert os.geteuid()==0 and os.getpid()!=1 and Path('/proc/1/comm').read_text().strip()=='systemd'
assert not list(Path('/dev').glob('davinci*')) and not Path('/var/run/docker.sock').exists()
EVIDENCE=Path('/evidence');owner=EVIDENCE.stat().st_uid;group=EVIDENCE.stat().st_gid
assert owner>0 and EVIDENCE.stat().st_mode&0o777==0o700
prefix=Path('/opt/ascend-lab-controller');state=Path('/state/controller');stage='bus';events=[];report={'passed':False}
def write(name,value):
 path=EVIDENCE/name;fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as out:json.dump(value,out,sort_keys=True,indent=2);out.flush();os.fsync(out.fileno())
 os.chown(path,owner,group)
def ctl(action,*args,bootstrap=False,check=True):
 tool='/provision/bootstrap/servicectl' if bootstrap else str(prefix/'current/servicectl')
 observer='''import json,runpy,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
import unit_start as s
from payload_quota_admin import write
counter=Path("/state/start-rpc-counter.json");original=s.Bus.start
def counted(self,name):
 value=json.loads(counter.read_text()) if counter.exists() else {"count":0}
 value["count"]+=1;write(counter,value)
 return original(self,name)
s.Bus.start=counted
sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")
'''
 command=['/usr/bin/python3','-I','-B',*(['-c',observer] if action=='unit-start-apply' else []),tool,action,*args]
 r=subprocess.run(command,capture_output=True,text=True,timeout=35)
 events.append({'action':action,'exit_code':r.returncode})
 if check and r.returncode:
  write('failure-command.json',{'action':action,'returncode':r.returncode,'stderr':r.stderr[-4096:]})
  raise RuntimeError('Installed operation refused: '+action)
 return json.loads(r.stdout) if r.returncode==0 else None
def expired(*_):raise TimeoutError('Owned native acceptance deadline reached')
signal.signal(signal.SIGALRM,expired);signal.alarm(170)
try:
 until=time.monotonic()+20
 while True:
  r=subprocess.run(['/usr/bin/busctl','--system','--json=short','call','org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner','s','org.freedesktop.systemd1'],capture_output=True,text=True,timeout=5)
  if r.returncode==0:break
  if time.monotonic()>until:
   write('private-bus-failure.json',{'exit_code':r.returncode,'stdout':r.stdout[-4096:],'stderr':r.stderr[-4096:]})
   raise RuntimeError('Original native manager did not register on the system bus')
  time.sleep(.2)
 stage='install'
 installed=ctl('install','--archive','/provision/server.tar.gz','--role','controller','--prefix',str(prefix),'--state-dir',str(state),'--create-service-user','labsvc','--port','18865',bootstrap=True)
 stage='doctor';doctor=ctl('systemd-doctor');assert doctor['prerequisites_ready'] and not doctor['service_start_verified']
 stage='unit-install';ctl('unit-install','--prefix',str(prefix))
 stage='manager-plan';ctl('unit-manager-plan','--prefix',str(prefix),'--output','/state/manager-plan.json')
 stage='inactive-unit-retention'
 sys.path.insert(0,str(prefix/'current'))
 from unit_manager import Bus,MissingUnit,BUS,MANAGER,INTERFACE
 from unit_bus_pin import Hold
 raw=Bus();anchor=raw.anchor();selected='ascend-lab-controller.service'
 raw.load(selected);time.sleep(.5)
 try:raw.call(raw.owner,MANAGER,INTERFACE,'GetUnit','s',[selected],'o');collected=False
 except MissingUnit:collected=True
 with Hold(selected,anchor):
  time.sleep(.5)
  held_path=raw.call(raw.owner,MANAGER,INTERFACE,'GetUnit','s',[selected],'o')
  assert held_path.startswith('/org/freedesktop/systemd1/unit/')
  old_property=subprocess.run(['/usr/bin/busctl','--system','--json=short','get-property',raw.owner,held_path,BUS+'.Unit','ControlGroup'],capture_output=True,text=True,timeout=8)
  new_property=subprocess.run(['/usr/bin/busctl','--system','--json=short','get-property',raw.owner,held_path,BUS+'.Service','ControlGroup'],capture_output=True,text=True,timeout=8)
  assert old_property.returncode!=0 and new_property.returncode==0
 time.sleep(.5)
 try:raw.call(raw.owner,MANAGER,INTERFACE,'GetUnit','s',[selected],'o');released=False
 except MissingUnit:released=True
 assert collected and released,'Inactive metadata retention/release did not match the isolated fixture'
 write('native-retention-reproduction.json',{'inactive_collected_without_ref':collected,'retained_with_native_ref':True,
  'collected_after_reference_closed':released,'control_group_absent_from_unit_interface':old_property.returncode!=0,'control_group_read_from_service_interface':new_property.returncode==0})
 stage='manager-apply';loaded=ctl('unit-manager-apply','--prefix',str(prefix),'--plan','/state/manager-plan.json')
 stage='startup-plan';ctl('unit-start-plan','--prefix',str(prefix),'--output','/state/start-plan.json')
 stage='startup-response-sigkill'
 fault_code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]+"/current")
import unit_start as s
from payload_quota_admin import write
from jobs import identity
counter=Path("/state/start-rpc-counter.json")
original=s.Bus.start
def counted(self,name):
 value=json.loads(counter.read_text()) if counter.exists() else {"count":0}
 value["count"]+=1;write(counter,value)
 return original(self,name)
s.Bus.start=counted
def checkpoint(stage):
 if stage=="response":
  pending=json.loads((Path(sys.argv[1])/"unit-start-change.json").read_text())
  write(Path("/state/response-sigkill.json"),{"boundary":stage,"process":identity(os.getpid()),"rpc_count":json.loads(counter.read_text())["count"],
   "plan_id":pending["plan"]["id"],"phase":pending["phase"],"job":pending["job"]})
  os.kill(os.getpid(),signal.SIGKILL)
s._checkpoint=checkpoint
s.apply(sys.argv[1],json.load(open("/state/start-plan.json")))
'''
 killed=subprocess.run(['/usr/bin/python3','-I','-B','-c',fault_code,str(prefix)],capture_output=True,text=True,timeout=35)
 assert killed.returncode==-signal.SIGKILL
 marker=json.loads(Path('/state/response-sigkill.json').read_text());assert marker['boundary']=='response' and marker['rpc_count']==1
 from deployment import process_status
 until=time.monotonic()+25
 while not process_status(prefix)['ready']:
  if time.monotonic()>until:raise RuntimeError('Original service did not become ready after caller SIGKILL')
  time.sleep(.2)
 original_process=process_status(prefix)['pid']
 assert (prefix/'unit-start-change.json').exists() and (prefix/'service-admission.json').exists()
 stage='startup-apply';until=time.monotonic()+55
 while True:
  startup=ctl('unit-start-apply','--prefix',str(prefix),'--plan','/state/start-plan.json')
  if startup['state']=='ready':break
  assert startup['state']=='pending' and not startup['start_sent_again']
  if time.monotonic()>until:raise RuntimeError('Original invocation did not become ready within acceptance window')
  time.sleep(.25)
 assert json.loads(Path('/state/start-rpc-counter.json').read_text())['count']==1
 assert startup['ready']['process']['pid']==original_process
 stage='running-check';checked=ctl('unit-manager-check','--prefix',str(prefix),'--running')
 sys.path.insert(0,str(prefix/'current'))
 from lab_client import Client
 from unit_manager import Bus
 from jobs import identity,identity_alive
 bus=Bus();anchor=bus.anchor();unit=bus.unit('ascend-lab-controller.service')
 assert unit['MainPID']>1 and unit['User']==str(installed['uid']) and len(unit['InvocationID'])==16
 assert unit['CapabilityBoundingSet']==0 and unit['NoNewPrivileges'] and unit['ProtectSystem']=='strict'
 assert anchor['pid1']['pid']==1 and anchor['pid1']['uid']==0
 def owner_identity():
  os.setgroups([]);os.setgid(installed['gid']);os.setuid(installed['uid'])
 api_code='import sys;sys.path.insert(0,sys.argv[1]);from lab_client import Client;assert Client.from_profile(sys.argv[2]).request("/api/me");print("owner-api-ok")'
 def owner_api():
  api=subprocess.run(['/usr/bin/python3','-I','-B','-c',api_code,str(prefix/'current'),str(state/'config.json')],
   preexec_fn=owner_identity,capture_output=True,text=True,timeout=15,env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8'})
  assert api.returncode==0 and api.stdout.strip()=='owner-api-ok','Authenticated API must be read by the actual service/profile owner'
 owner_api()
 pid=unit['MainPID'];process=identity(pid);assert process['uid']==installed['uid'] and identity_alive(process)
 stage='replay';again=ctl('unit-start-apply','--prefix',str(prefix),'--plan','/state/start-plan.json')
 assert again['historical_receipt_only'] and again['ready']==startup['ready'] and bus.unit(unit['Id'])['MainPID']==pid
 stage='automatic-restart'
 fd=os.pidfd_open(pid)
 try:
  assert identity_alive(process) and bus.unit(unit['Id'])['MainPID']==pid
  signal.pidfd_send_signal(fd,signal.SIGKILL)
 finally:os.close(fd)
 until=time.monotonic()+30
 while True:
  status=process_status(prefix)
  if status['ready'] and status['pid']!=pid:break
  if time.monotonic()>until:raise RuntimeError('Original systemd unit did not recover its control service')
  time.sleep(.25)
 restarted=bus.unit(unit['Id']);assert restarted['MainPID']==status['pid'] and restarted['InvocationID']!=unit['InvocationID']
 assert identity(status['pid'])['uid']==installed['uid'] and restarted['CapabilityBoundingSet']==0
 ctl('unit-manager-check','--prefix',str(prefix),'--running')
 historical=ctl('unit-start-apply','--prefix',str(prefix),'--plan','/state/start-plan.json')
 assert historical['historical_receipt_only'] and historical['ready']==startup['ready']
 assert bus.unit(unit['Id'])['MainPID']==status['pid']
 owner_api()
 object_path=bus.call(bus.owner,MANAGER,INTERFACE,'GetUnit','s',[unit['Id']],'o')
 assert bus.properties(object_path,BUS+'.Service',{'NRestarts':'u'})['NRestarts']==1
 assert json.loads(Path('/state/start-rpc-counter.json').read_text())['count']==1
 write('native-start-fault.json',{'response_caller_exit_code':killed.returncode,'response_marker':marker,
  'all_apply_calls_instrumented':True,'final_start_rpc_count':json.loads(Path('/state/start-rpc-counter.json').read_text())['count'],
  'original_service_pid':pid,'recovered_startup_receipt':startup,'automatic_restart_process':identity(status['pid']),
  'automatic_restart_invocation_id':bytes(restarted['InvocationID']).hex(),'automatic_restart_count':1,
  'historical_receipt_after_restart':historical,'owner_api_before_and_after_restart':True})
 report={'passed':True,'native_manager_version':anchor['version'],'manager_pid':1,'controller_uid':installed['uid'],
  'actual_unit_main_pid':pid,'actual_invocation_id':bytes(unit['InvocationID']).hex(),'capabilities_zero':True,
  'private_tmp':unit['PrivateTmp'],'protect_system':unit['ProtectSystem'],'no_new_privileges':unit['NoNewPrivileges'],
  'read_only_doctor_positive':doctor['prerequisites_ready'],'doctor_did_not_claim_service_started':not doctor['service_start_verified'],
  'manager_load_and_exact_properties_verified':True,'startup_ready_and_admission_opened':startup['admission_opened'],
  'same_start_plan_replay_no_second_start':True,'authenticated_api_responded':True,'minimal_fixture_targets':True,
  'api_profile_owner_uid_enforced':True,
  'actual_start_response_sigkill':True,'original_service_ready_before_reconciliation':True,'single_original_start_rpc':True,
  'same_controller_pid_after_start_reconciliation':True,'automatic_control_service_restart_verified':True,
  'automatic_restart_new_pid_and_invocation':True,'historical_receipt_not_rebound_to_new_invocation':True,
  'all_start_apply_calls_observed':True,'after_restart_owner_api_responded':True,'native_automatic_restart_count':1,
  'inactive_unit_collection_reproduced':collected,'native_reference_keeps_unit_loaded':True,'reference_disconnect_allows_collection':released,
  'systemd_pid1_real':True,'npu_launches':0,'real_host_systemd_tested':False,'events':events}
 write('raw-loaded-unit.json',unit)
except BaseException as error:
 report={'passed':False,'stage':stage,'error_type':type(error).__name__,'error':str(error)[:2048],'events':events,'systemd_pid1_real':True}
finally:
 signal.alarm(0)
 try:
  r=subprocess.run(['/usr/bin/systemctl','show','dbus.service','dbus.socket','native-acceptance.service','--no-pager',
   '--property=Id,LoadState,ActiveState,SubState,Result,MainPID,ExecMainCode,ExecMainStatus'],capture_output=True,text=True,timeout=8)
  write('private-native-unit-status.json',{'exit_code':r.returncode,'stdout':r.stdout[-16384:],'stderr':r.stderr[-4096:]})
 except BaseException:pass
 try:
  r=subprocess.run(['/usr/bin/systemctl','stop','ascend-lab-controller.service'],capture_output=True,text=True,timeout=25)
  report['controller_stop_exit_code']=r.returncode
 except BaseException:report['controller_stop_exit_code']=None
 try:
  r=subprocess.run(['/usr/bin/journalctl','--no-pager','-n','100','-u','ascend-lab-controller.service'],capture_output=True,text=True,timeout=8)
  write('private-unit-journal.json',{'exit_code':r.returncode,'stdout':r.stdout[-32768:],'stderr':r.stderr[-4096:]})
 except BaseException:pass
 try:
  with tarfile.open(EVIDENCE/'private-controller-evidence.tar.gz','x:gz') as out:
   if prefix.exists():out.add(prefix,arcname='installation',recursive=True)
   if state.exists():out.add(state,arcname='control',recursive=True)
  archive=EVIDENCE/'private-controller-evidence.tar.gz';archive.chmod(0o600);os.chown(archive,owner,group)
 except BaseException:report['private_evidence_archive_failed']=True
 root=Path('/sys/fs/cgroup');names=['cpu.max','memory.max','memory.swap.max','pids.max','cpuset.cpus.effective']
 before=json.loads(Path('/run/native-fixture/cgroup.json').read_text())
 report['parent_resource_limits_unchanged']=before['limits']=={n:(root/n).read_text().strip() for n in names}
 if not report['parent_resource_limits_unchanged']:report['passed']=False
 write('native-report.json',report)
raise SystemExit(0 if report['passed'] else 1)

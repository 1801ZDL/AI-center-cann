"""Owned native PID1 role integration; personal agents/tasks gain no privileges."""
import hashlib,json,os,re,time,uuid
from pathlib import Path


class Controller:
    def __init__(self,docker,image,boot,role="controller"):
        assert re.fullmatch('sha256:[0-9a-f]{64}',image) and role in {'controller','node'}
        self.role=role;self.prefix='/opt/ascend-lab-'+role;self.unit='ascend-lab-'+role+'.service'
        self.docker=docker;self.image=image;self.boot=boot;self.container=None;self.managed=False;self.events=[]
        self.restore_fault=None;self.process_snapshots=[]
        self.host_mounts=[s for s in Path('/proc/self/mountinfo').read_text().splitlines() if ' - cgroup2 ' in s]

    def prepare(self,name):
        self.container=name
        info=json.loads(self.docker(['inspect',name]).stdout)[0];cid=info['Id'];hc=info['HostConfig']
        assert info['Image']==self.image and hc['CgroupnsMode']=='private' and not hc['Privileged'] and not hc['Devices']
        assert hc['Memory']==hc['MemorySwap']==384*1024**2 and hc['NanoCpus']==250000000 and hc['PidsLimit']==128
        sockets=[m for m in info['Mounts'] if m['Destination']=='/var/run/docker.sock']
        if self.role=='controller':assert not sockets
        else:assert len(sockets)==1 and Path(sockets[0]['Source']).resolve()==Path('/var/run/docker.sock').resolve()
        assert info['State']['Running'] and info['State']['Pid']>1
        leaf=Path('/proc/'+str(info['State']['Pid'])+'/cgroup').read_text().strip();assert leaf.startswith('0::/') and cid in leaf
        ns=self.docker(['exec',name,'/usr/bin/python3','-I','-B','-c','import os;print(os.readlink("/proc/self/ns/cgroup"))']).stdout.strip()
        assert ns!=os.readlink('/proc/self/ns/cgroup')
        value={'expected_cgroup_namespace':ns,'host_leaf_identity_sha256':hashlib.sha256(leaf.encode()).hexdigest()}
        code='import json,sys;from pathlib import Path;p=Path("/run/native-fixture");p.mkdir(mode=0o700,exist_ok=True);f=p/"approved.json";f.write_text(sys.argv[1]);f.chmod(0o600)'
        self.docker(['exec',name,'/usr/bin/python3','-I','-B','-c',code,json.dumps(value)])
        until=time.monotonic()+20
        while True:
            q=self.docker(['exec',name,'/usr/bin/busctl','--system','--json=short','call','org.freedesktop.DBus',
                '/org/freedesktop/DBus','org.freedesktop.DBus','GetNameOwner','s','org.freedesktop.systemd1'],check=False)
            if q.returncode==0:break
            if time.monotonic()>until:raise RuntimeError('Original native controller manager did not become ready')
            time.sleep(.2)

    def cli(self,action,*arguments,check=True):
        r=self.docker(['exec',self.container,'/usr/bin/python3','-I','-B',
            self.prefix+'/current/servicectl',action,'--prefix',self.prefix,*arguments],check=check,timeout=45)
        self.events.append({'action':action,'exit_code':r.returncode})
        return json.loads(r.stdout) if check else r

    def role_code(self,code):
        # Fixed fixture source constants only; role is a closed constructor enum.
        return code.replace('/opt/ascend-lab-controller',self.prefix).replace('ascend-lab-controller.service',self.unit)

    def reconcile(self):
        code='import json,sys;from pathlib import Path;p=Path("/opt/ascend-lab-controller/unit-start-change.json");print(json.dumps(json.loads(p.read_text())["plan"] if p.exists() else None))'
        plan=json.loads(self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',self.role_code(code)]).stdout)
        if plan is None:return None
        path='/root/native-controller-reconcile-'+plan['id']+'.json'
        write='import json,os,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(sys.argv[2]);\nif p.exists():assert json.loads(p.read_text())==v\nelse:\n f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n with os.fdopen(f,"w") as out:json.dump(v,out)'
        self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',write,path,json.dumps(plan)])
        result=self.cli('unit-start-apply','--plan',path)
        if result['state']=='pending':result['required_nodes']=len(plan['required_nodes'])
        return result

    def start(self):
        pending=self.reconcile()
        status=self.cli('status')
        if not status['running'] and pending is None:
            matches='import sys;sys.path.insert(0,"/opt/ascend-lab-controller/current");import deployment as d;from unit_refresh import _file;p,m,_=d.verify("/opt/ascend-lab-controller");t=d.unit_text(p,m);print(int(_file("/etc/systemd/system/ascend-lab-controller.service",0o644)["text"]==t and _file(p/"units/ascend-lab-controller.service",0o444)["text"]==t))'
            same=self.managed and self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',self.role_code(matches)]).stdout.strip()=='1'
            if same:
                # An ordinary restart uses the already verified unit. A new
                # configuration/startup plan would require quiescent state and
                # would be inappropriate while supervising existing tasks.
                self.cli('unit-manager-check')
                self.docker(['exec',self.container,'/usr/bin/systemctl','start',self.unit],timeout=35)
                self.events.append({'action':'normal-native-unit-start','exit_code':0})
            else:self.configure_start()
        until=time.monotonic()+25
        while True:
            receipt=self.reconcile();status=self.cli('status')
            self.events.append({'action':'native-start-readback','ready':status['ready'],
                'startup_state':receipt.get('state') if receipt else None})
            # A Node cannot claim while its own startup journal/gate remains.
            # The controller may still need to return pending so that its Node
            # can start and confirm inventory; preserve that two-role handshake.
            if status['ready'] and (receipt is None or receipt['state']=='ready' or self.role=='controller' and receipt.get('required_nodes',0)>0):break
            if time.monotonic()>until:raise RuntimeError('Original native role did not finish its required startup receipt')
            time.sleep(.2)
        if receipt is None or receipt['state']=='ready':self.cli('unit-manager-check','--running')
        else:status['startup_pending']=True
        return status

    def configure_start(self):
        operation=uuid.uuid4().hex
        present=self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',
            self.role_code('from pathlib import Path;print(int(Path("/etc/systemd/system/ascend-lab-controller.service").exists()))')]).stdout.strip()=='1'
        if not present:
            self.cli('unit-install')
            if self.role=='node':
                seed='''import os,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-node/current")
import deployment as d
p,m,_=d.verify("/opt/ascend-lab-node");desired=d.unit_text(p,m)
current="CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SYS_ADMIN\\n"
assert desired.count(current)==1
old=desired.replace(current,"CapabilityBoundingSet=CAP_SYS_ADMIN\\n")
for path,mode in [(Path("/etc/systemd/system/ascend-lab-node.service"),0o644),(p/"units/ascend-lab-node.service",0o444)]:
 assert path.read_text()==desired and path.stat().st_uid==0
 path.chmod(0o600)
 with path.open("w") as f:f.write(old);f.flush();os.fsync(f.fileno())
 path.chmod(mode)
'''
                self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',seed])
                self.events.append({'action':'exact-legacy-node-unit-seeded','exit_code':0})
        if present or self.role=='node':
            self.cli('unit-refresh-plan','--output','/root/native-controller-refresh-'+operation+'.json')
            self.cli('unit-refresh-apply','--plan','/root/native-controller-refresh-'+operation+'.json')
        self.cli('unit-manager-plan','--output','/root/native-controller-manager-'+operation+'.json')
        self.cli('unit-manager-apply','--plan','/root/native-controller-manager-'+operation+'.json')
        self.cli('unit-start-plan','--output','/root/native-controller-start-'+operation+'.json')
        self.managed=True
        self.cli('unit-start-apply','--plan','/root/native-controller-start-'+operation+'.json')

    def stop(self):
        capture='''import json,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
from unit_manager import Bus
from jobs import identity,identity_alive
u=Bus().unit("ascend-lab-controller.service");p=identity(u["MainPID"])
assert p and p["uid"]==int(sys.argv[1]) and identity_alive(p)
status={k:v.strip() for k,s,v in (line.partition(":") for line in Path("/proc",str(p["pid"]),"status").read_text().splitlines()) if s}
assert int(status["CapEff"],16)==u["CapabilityBoundingSet"] and status["NoNewPrivs"]=="1" and identity_alive(p)
print(json.dumps({"process":p,"effective_capabilities":int(status["CapEff"],16),"no_new_privileges":True,"invocation_id":bytes(u["InvocationID"]).hex()}))
'''
        if self.cli('status')['running']:
            self.process_snapshots.append(json.loads(self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',self.role_code(capture),str(0 if self.role=='node' else 999)]).stdout))
        self.docker(['exec',self.container,'/usr/bin/systemctl','stop',self.unit],timeout=35)
        status=self.cli('status');assert not status['running'];return status

    def restore(self,arguments):
        assert self.role=="controller"
        values=dict(zip(arguments[::2],arguments[1::2]));assert set(values)=={'--archive','--new-state-dir'}
        code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
import deployment as d
import maintenance as m
from jobs import identity
from payload_quota_admin import write
prefix=Path("/opt/ascend-lab-controller");old=Path(d.load_installation(prefix)[1]["state_dir"])
def checkpoint(stage):
 assert stage=="unit_binding"
 meta=d.load_installation(prefix)[1];proof=meta["unit_relocation"]
 write(old/"native-unit-restore-sigkill.json",{"boundary":stage,"process":identity(os.getpid()),
  "relocation_id":proof["id"],"before_sha256":proof["before_sha256"],"desired_sha256":proof["desired_sha256"]})
 os.kill(os.getpid(),signal.SIGKILL)
m._restore_checkpoint=checkpoint
m.restore(prefix,sys.argv[1],sys.argv[2])
'''
        result=self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',code,
            values['--archive'],values['--new-state-dir']],check=False,timeout=45)
        assert result.returncode==137,'Expected the original restore child to be SIGKILLed, not retried'
        observe='''import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
import deployment as d
prefix=Path("/opt/ascend-lab-controller");_,meta,_=d.verify(prefix)
marker=json.loads((Path(meta["previous_state_dir"])/"native-unit-restore-sigkill.json").read_text())
actual=hashlib.sha256((prefix/"units/ascend-lab-controller.service").read_bytes()).hexdigest()
assert marker["boundary"]=="unit_binding" and marker["relocation_id"]==meta["unit_relocation"]["id"]
assert actual==marker["before_sha256"] and marker["desired_sha256"]!=actual
print(json.dumps({"restored_state":meta["state_dir"],"previous_state_retained":meta["previous_state_dir"],
 "response_lost_after_metadata_publish":True,"old_template_preserved_before_refresh":True,"fault":marker}))
'''
        observed=json.loads(self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',observe]).stdout)
        assert observed['restored_state']==values['--new-state-dir']
        self.restore_fault=observed;self.events.append({'action':'restore-unit-binding-SIGKILL','exit_code':137})
        return observed

    def proof(self):
        assert self.host_mounts==[s for s in Path('/proc/self/mountinfo').read_text().splitlines() if ' - cgroup2 ' in s]
        limits='import json;from pathlib import Path;v=json.loads(Path("/run/native-fixture/cgroup.json").read_text())["limits"];print(json.dumps(v=={k:Path("/sys/fs/cgroup",k).read_text().strip() for k in v}))'
        assert json.loads(self.docker(['exec',self.container,'/usr/bin/python3','-I','-B','-c',limits]).stdout)
        if self.role=='node':
            return {'actual_native_node_pid1':True,'trusted_node_docker_socket':True,'personal_task_socket_exposed':False,
                'host_cgroup_mount_inventory_unchanged':True,'parent_resource_limits_unchanged':True,
                'events':self.events,'running_process_snapshots':self.process_snapshots,
                'scope':'Owned native systemd Node with its existing trusted daemon socket; no device or personal-agent privilege grant.'}
        return {'actual_native_controller_pid1':True,'actual_native_node_pid1':False,'controller_docker_socket':False,
            'host_cgroup_mount_inventory_unchanged':True,'parent_resource_limits_unchanged':True,
            'events':self.events,'running_process_snapshots':self.process_snapshots,'restore_binding_sigkill':self.restore_fault,
            'scope':'Actual native controller on one physical host; inspect the separate Node proof when supplied. Not host boot or NPU.'}

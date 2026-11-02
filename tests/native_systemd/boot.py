"""Trusted owned-container PID1 bootstrap, then exec the native system manager."""
import ctypes,json,os,signal,time,uuid
from pathlib import Path
assert os.getpid()==1 and os.geteuid()==0
assert not list(Path('/dev').glob('davinci*')) and not Path('/var/run/docker.sock').exists()
assert Path('/proc/self/cgroup').read_text().strip()=='0::/'
folder=Path('/run/native-fixture');folder.mkdir(mode=0o700)
deadline=time.monotonic()+40
while not (folder/'approved.json').exists():
 if time.monotonic()>deadline:raise RuntimeError('Original owner did not approve this namespace boundary')
 time.sleep(.1)
approval=json.loads((folder/'approved.json').read_text());assert approval['expected_cgroup_namespace']==os.readlink('/proc/self/ns/cgroup')
root=Path('/sys/fs/cgroup');names=['cpu.max','memory.max','memory.swap.max','pids.max','cpuset.cpus.effective']
limits={n:(root/n).read_text().strip() for n in names}
assert limits['cpu.max']=='50000 100000' and limits['memory.max']=='536870912' and limits['memory.swap.max']=='0' and limits['pids.max']=='128'
mounts=[x.split(' - ') for x in Path('/proc/self/mountinfo').read_text().splitlines() if ' - cgroup2 ' in x]
assert len(mounts)==1;pre,post=mounts[0];a=pre.split()
assert a[3:5]==['/','/sys/fs/cgroup'] and not a[6:] and set(a[5].split(','))=={'ro','nosuid','nodev','noexec','relatime'}
libc=ctypes.CDLL(None,use_errno=True);libc.mount.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_void_p]
def mount(target,flags):
 if libc.mount(None,target,None,flags,None):raise OSError(ctypes.get_errno(),'Owned namespace mount operation refused')
mount(b'/',(1<<18)|16384) # detach propagation before creating private shared peers
mount(b'/',(1<<20)|16384)
mount(b'/sys/fs/cgroup',32|4096|2|4|8|(1<<21))
assert limits=={n:(root/n).read_text().strip() for n in names}
(folder/'cgroup.json').write_text(json.dumps({'limits':limits,'private_leaf_sha256':approval['host_leaf_identity_sha256'],'no_host_tree_bind':True}))
Path('/etc/machine-id').write_text(uuid.uuid4().hex+'\n')
machine=Path('/var/lib/dbus/machine-id')
if not machine.is_symlink():machine.write_text(Path('/etc/machine-id').read_text())
Path('/run/dbus').mkdir(mode=0o755,exist_ok=True)
units=Path('/etc/systemd/system');units.mkdir(parents=True,exist_ok=True)
for name in ['sysinit.target','basic.target','network-online.target']:
 (units/name).write_text('[Unit]\nDescription=Owned minimal acceptance target\nDefaultDependencies=no\n')
(units/'dbus.socket').write_text('''[Unit]
Description=Owned native system bus socket
DefaultDependencies=no
[Socket]
ListenStream=/run/dbus/system_bus_socket
SocketMode=0666
''')
(units/'dbus.service').write_text('''[Unit]
Description=Owned native system bus
DefaultDependencies=no
Requires=dbus.socket
After=dbus.socket
[Service]
Type=simple
User=messagebus
Group=messagebus
RuntimeDirectory=dbus
RuntimeDirectoryMode=0755
ExecStart=/usr/bin/dbus-daemon --system --address=systemd: --nofork --nopidfile --systemd-activation
Restart=no
StandardOutput=null
StandardError=null
''')
(units/'native-acceptance.service').write_text('''[Unit]
Description=Owned native manager acceptance
DefaultDependencies=no
Requires=dbus.service
After=dbus.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B /native-inside.py
TimeoutStartSec=200
StandardOutput=null
StandardError=null
''')
(units/'native-lab.target').write_text('''[Unit]
Description=Owned minimal native PID1 acceptance
DefaultDependencies=no
Wants=dbus.socket dbus.service native-acceptance.service
After=dbus.service
AllowIsolate=yes
''')
os.environ['container']='docker';os.environ['SYSTEMD_LOG_TARGET']='console'
os.execve('/lib/systemd/systemd',['/lib/systemd/systemd','--system','--unit=native-lab.target','--log-level=info'],dict(os.environ))

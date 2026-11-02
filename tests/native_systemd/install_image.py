"""Runs only as PID1 of an owned bounded image-preparation container."""
import hashlib,json,os,signal,subprocess,time
from pathlib import Path

assert os.getpid()==1 and os.geteuid()==0
assert not list(Path('/dev').glob('davinci*')) and not Path('/var/run/docker.sock').exists()
def expired(*_):raise TimeoutError('Native image preparation deadline reached')
signal.signal(signal.SIGALRM,expired);signal.alarm(480)
signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(SystemExit(143)))
base=Path('/opt/ascend-lab-native-systemd');base.mkdir(mode=0o755)
assert 'VERSION_ID="22.04"' in Path('/etc/os-release').read_text()
key=Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg');assert key.is_file()
source=base/'sources.list';source.write_text(''.join(
 'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://'+host+'/ubuntu '+suite+' main\n'
 for host,suite in [('archive.ubuntu.com','jammy'),('archive.ubuntu.com','jammy-updates'),('security.ubuntu.com','jammy-security')]))
empty=base/'empty';empty.mkdir();cache=base/'debs';cache.mkdir();(cache/'partial').mkdir()
lists=base/'lists';lists.mkdir();(lists/'partial').mkdir()
policy=Path('/usr/sbin/policy-rc.d');policy.write_text('#!/bin/sh\nexit 101\n');policy.chmod(0o755)
opts=['-o','Dir::Etc::sourcelist='+str(source),'-o','Dir::Etc::sourceparts='+str(empty),
 '-o','Dir::State::lists='+str(lists),'-o','Dir::Cache::archives='+str(cache),
 '-o','Acquire::Retries=0','-o','Acquire::AllowInsecureRepositories=false',
 '-o','APT::Get::AllowUnauthenticated=false']
env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','DEBIAN_FRONTEND':'noninteractive','SYSTEMD_OFFLINE':'1'}
log=base/'install.log'
def run(argv,seconds=180):
 with log.open('ab') as output:
  result=subprocess.run(argv,stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,env=env,timeout=seconds)
 if result.returncode:raise RuntimeError('Native package operation refused; retain the private install log')
 assert log.stat().st_size<=8*1024**2
run(['/usr/bin/apt-get',*opts,'update'])
run(['/usr/bin/apt-get',*opts,'-y','--no-install-recommends','--download-only','install','systemd','dbus'])
packages=[];total=0
for file in sorted(cache.glob('*.deb')):
 total+=file.stat().st_size;assert total<=128*1024**2
 fields=subprocess.check_output(['/usr/bin/dpkg-deb','-f',str(file),'Package','Version','Architecture'],env=env,timeout=10).decode().splitlines()
 packages.append({'file':file.name,'bytes':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'fields':fields})
assert packages and len(packages)<=64
run(['/usr/bin/apt-get',*opts,'-y','--no-install-recommends','--no-download','install','systemd','dbus'])
files={}
for name in ['/lib/systemd/systemd','/usr/bin/systemctl','/usr/bin/busctl','/usr/bin/dbus-daemon']:
 file=Path(name);raw=file.read_bytes();assert raw.startswith(b'\x7fELF')
 files[name]={'path':str(file.resolve()),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
versions={}
for name,args in [('systemd',['/lib/systemd/systemd','--version']),('dbus',['/usr/bin/dbus-daemon','--version'])]:
 versions[name]=subprocess.check_output(args,env=env,timeout=10).decode()[:4096]
installed=subprocess.check_output(['/usr/bin/dpkg-query','-W','-f=${Package}\t${Version}\t${db:Status-Status}\n'],env=env,timeout=10).decode().splitlines()
installed=[v for v in installed if v.split('\t')[0].startswith(('systemd','libsystemd','dbus','libdbus'))]
manifest={'schema':'ascend-lab-native-systemd-image/v1','passed':True,'packages_downloaded':packages,
 'downloaded_bytes':total,'installed_manager_packages':installed,'native_files':files,'versions':versions,
 'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'archive_keyring_sha256':hashlib.sha256(key.read_bytes()).hexdigest(),
 'installer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'apt_signature_verification_disabled':False,
 'npu_devices_present':False,'docker_socket_present':False,'systemd_pid1_started':False}
(base/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
import shutil
shutil.rmtree(cache);shutil.rmtree(lists)
signal.alarm(0);print(json.dumps(manifest),flush=True)

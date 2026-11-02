"""Run fixed installed-controller acceptance under an actual owned systemd PID1."""
import hashlib,json,os,re,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from deployment import bundle,unpack_release
from docker_runtime import discover,command
from lab_client import atomic_json,now
assert os.environ.get('LAB_JOB_DIR')
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--image-metadata',required=True)
parser.add_argument('--scenario',choices=['controller','storage'],default='controller');args=parser.parse_args()
fixtures=ROOT/'tests/native_systemd'
for source in fixtures.glob('*.py'):compile(source.read_bytes(),str(source),'exec')
job=Path(os.environ['LAB_JOB_DIR']);base=job/'tmp/native-systemd';base.mkdir(mode=0o700,parents=True)
provision=base/'provision';provision.mkdir(mode=0o700);evidence=base/'evidence';evidence.mkdir(mode=0o700)
release=bundle(provision/'server.tar.gz');bootstrap,_=unpack_release(provision/'server.tar.gz',provision);bootstrap.rename(provision/'bootstrap')
metadata=Path(args.image_metadata);assert metadata.is_file() and not metadata.is_symlink() and metadata.stat().st_size<=128*1024
native=json.loads(metadata.read_text());image=native['image'];assert native['passed'] and re.fullmatch('sha256:[0-9a-f]{64}',image)
binding,_=discover();name='ascend-lab-nativepid1-run-'+uuid.uuid4().hex[:12];created=None;cleaned=False;report=None;timed_out=False
def docker(args,check=True,timeout=30):
 argv,env=command(binding,['docker',*args]);r=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
 if check and r.returncode:raise RuntimeError('Owned native acceptance operation refused: '+r.stderr[-2048:])
 return r
host_before=[x for x in Path('/proc/self/mountinfo').read_text().splitlines() if ' - cgroup2 ' in x]
atomic_json(job/'results/native-intent.json',{'name':name,'label':name,'image':image,'release_id':release['release_id'],'max_acceptance_seconds':250,'recorded_at':now()})
try:
 caps=['SYS_ADMIN','CHOWN','DAC_OVERRIDE','FOWNER','FSETID','SETUID','SETGID','SYS_CHROOT','KILL','SETFCAP','SETPCAP','MKNOD']
 r=docker(['create','--pull','never','--name',name,'--label','io.ascend-lab.nativepid1='+name,
  '--network','none','--cgroupns','private','--tty','--cap-drop','ALL',*[x for cap in caps for x in ['--cap-add',cap]],
  '--security-opt','no-new-privileges','--security-opt','apparmor=unconfined',
  '--memory','512m','--memory-swap','512m','--pids-limit','128','--cpus','0.5',
  '--cpuset-cpus',','.join(map(str,sorted(os.sched_getaffinity(0)))),
  '--tmpfs','/run:rw,nosuid,nodev,size=64m,mode=755','--tmpfs','/tmp:rw,nosuid,nodev,size=32m,mode=1777',
  '--tmpfs','/state:rw,nosuid,nodev,size=256m,mode=755','--stop-signal','SIGRTMIN+3',
  '--log-opt','max-size=1m','--log-opt','max-file=1',
  '--mount','type=bind,src='+str(provision)+',dst=/provision,readonly',
  '--mount','type=bind,src='+str(evidence)+',dst=/evidence',
  '--mount','type=bind,src='+str(fixtures/'boot.py')+',dst=/boot-native.py,readonly',
  '--mount','type=bind,src='+str(fixtures/('storage_inside.py' if args.scenario=='storage' else 'inside.py'))+',dst=/native-inside.py,readonly',
  '--entrypoint','/usr/bin/python3',image,'-I','-B','/boot-native.py'])
 created=r.stdout.strip();assert re.fullmatch('[0-9a-f]{64}',created)
 docker(['start',created]);info=json.loads(docker(['inspect',created]).stdout)[0]
 assert info['Id']==created and info['Image']==image and info['Config']['Labels']['io.ascend-lab.nativepid1']==name
 hc=info['HostConfig'];assert not hc['Privileged'] and not hc['Devices'] and hc['CgroupnsMode']=='private' and hc['NetworkMode']=='none'
 assert hc['Memory']==512*1024**2 and hc['NanoCpus']==500000000 and hc['PidsLimit']==128
 assert info['State']['Running'] and info['State']['Pid']>1
 host_pid=info['State']['Pid'];host_path=Path('/proc/'+str(host_pid)+'/cgroup').read_text().strip();assert host_path.startswith('0::/') and created in host_path
 ns=docker(['exec',created,'/usr/bin/python3','-I','-B','-c','import os;print(os.readlink("/proc/self/ns/cgroup"))']).stdout.strip();assert ns!=os.readlink('/proc/self/ns/cgroup')
 approval={'expected_cgroup_namespace':ns,'host_leaf_identity_sha256':hashlib.sha256(host_path.encode()).hexdigest()}
 approve='import json,sys;from pathlib import Path;p=Path("/run/native-fixture");p.mkdir(mode=0o700,exist_ok=True);f=p/"approved.json";f.write_text(sys.argv[1]);f.chmod(0o600)'
 docker(['exec',created,'/usr/bin/python3','-I','-B','-c',approve,json.dumps(approval)])
 until=time.monotonic()+250
 while not (evidence/'native-report.json').exists():
  current=json.loads(docker(['inspect',created]).stdout)[0]
  if not current['State']['Running']:break
  if time.monotonic()>until:timed_out=True;break
  time.sleep(.5)
 if (evidence/'native-report.json').exists():report=json.loads((evidence/'native-report.json').read_text())
 current=json.loads(docker(['inspect',created]).stdout)[0]
 same=current['State']['Running'] and current['State']['Pid']==host_pid
 if report is not None:report['original_container_pid1_handle_preserved']=same
 logs=docker(['logs','--tail','150',created],check=False)
 (base/'private-boot.log').write_text(logs.stdout+logs.stderr)
finally:
 r=docker(['inspect',name],check=False)
 if r.returncode==0:
  current=json.loads(r.stdout)[0];assert current['Image']==image and current['Config']['Labels'].get('io.ascend-lab.nativepid1')==name
  if current['State']['Running']:docker(['stop','--time','8',current['Id']],timeout=20)
  stopped=json.loads(docker(['inspect',current['Id']]).stdout)[0];assert not stopped['State']['Running'] and stopped['State']['Pid']==0
  docker(['rm',current['Id']]);cleaned=True
assert cleaned
host_after=[x for x in Path('/proc/self/mountinfo').read_text().splitlines() if ' - cgroup2 ' in x]
assert host_before==host_after
result={'recorded_at':now(),'scenario':args.scenario,'passed':bool(report and report.get('passed') and report.get('original_container_pid1_handle_preserved')),
 'image':image,'release_id':release['release_id'],'report':report,'acceptance_deadline_reached':timed_out,
 'original_container_stopped_and_removed':cleaned,'host_cgroup_mounts_unchanged':True,
 'fixture_apparmor_unconfined':True,'minimal_fixture_targets':True,'host_units_changed':False,'npu_launches':0,
 'scope':'Real native PID1 and project controller in an owned no-device namespace, with private minimal boot targets; not real host accounts/full host boot/two physical machines.'}
atomic_json(job/'results/native-systemd-validation.json',result);print(json.dumps(result))
assert result['passed'],'Original native acceptance failed; preserve private evidence and diagnose, no replacement start'

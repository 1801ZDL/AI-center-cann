"""Queue-owned immutable native-manager image; fixed package installer only."""
import hashlib,json,os,re,subprocess,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from docker_runtime import discover,command
from lab_client import atomic_json,now
assert os.environ.get('LAB_JOB_DIR')
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--base-image',required=True);parser.add_argument('--metadata-output',required=True);args=parser.parse_args()
phase=Path(__file__).resolve().parent;job=Path(os.environ['LAB_JOB_DIR']);BASE=args.base_image
assert re.fullmatch('sha256:[0-9a-f]{64}',BASE)
output=Path(args.metadata_output).absolute();assert not output.exists() and not output.is_symlink()
binding,_=discover();name='ascend-lab-nativepid1-image-'+uuid.uuid4().hex[:12];tag='ascend-lab-native-systemd:'+name
def docker(args,check=True,timeout=30):
 argv,env=command(binding,['docker',*args]);r=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=timeout)
 if check and r.returncode:raise RuntimeError('Owned native image operation refused: '+r.stderr[-1024:])
 return r
installer=phase/'install_image.py';cid=None;image=None;manifest=None;cleaned=False
atomic_json(job/'results/image-intent.json',{'name':name,'label':name,'tag':tag,'base_image':BASE,'installer_sha256':hashlib.sha256(installer.read_bytes()).hexdigest(),'recorded_at':now()})
try:
 r=docker(['create','--pull','never','--name',name,'--label','io.ascend-lab.nativepid1='+name,
  '--network','bridge','--cap-drop','ALL',*[x for cap in ['CHOWN','DAC_OVERRIDE','FOWNER','FSETID','SETUID','SETGID','SETFCAP'] for x in ['--cap-add',cap]],
  '--security-opt','no-new-privileges','--memory','1024m','--memory-swap','1024m','--pids-limit','128',
  '--cpus','1','--cpuset-cpus',','.join(map(str,sorted(os.sched_getaffinity(0)))),
  '--mount','type=bind,src='+str(installer)+',dst=/install-native.py,readonly',
  '--entrypoint','/usr/bin/python3',BASE,'-I','-B','/install-native.py'])
 cid=r.stdout.strip();assert re.fullmatch('[0-9a-f]{64}',cid)
 started=docker(['start','--attach',cid],check=False,timeout=600)
 record=json.loads(docker(['inspect',cid]).stdout)[0]
 assert record['Id']==cid and record['Image']==BASE and record['Config']['Labels'].get('io.ascend-lab.nativepid1')==name
 assert not record['State']['Running'] and record['State']['Pid']==0,'Retain original still-running build; do not restart'
 if started.returncode or record['State']['ExitCode']:
  atomic_json(job/'results/image-failure.json',{'container':cid,'exit_code':record['State']['ExitCode'],'stdout_tail':started.stdout[-4096:],'stderr_tail':started.stderr[-4096:]})
  # Retain the install log within the stopped owned container for diagnosis.
  docker(['cp',cid+':/opt/ascend-lab-native-systemd/install.log',str(job/'native-install.log')],check=False)
  raise RuntimeError('Native image installer failed; original bounded private evidence retained')
 manifest=json.loads(started.stdout);assert manifest['passed'] and manifest['installer_sha256']==hashlib.sha256(installer.read_bytes()).hexdigest()
 r=docker(['commit','--change','ENTRYPOINT ["/bin/false"]','--change','CMD []',cid,tag],timeout=60)
 image=r.stdout.strip();assert re.fullmatch('sha256:[0-9a-f]{64}',image)
 info=json.loads(docker(['image','inspect',image]).stdout)[0];assert info['Id']==image
finally:
 r=docker(['inspect',name],check=False)
 if r.returncode==0:
  current=json.loads(r.stdout)[0]
  assert current['Image']==BASE and current['Config']['Labels'].get('io.ascend-lab.nativepid1')==name
  if current['State']['Running']:
   atomic_json(job/'results/image-still-running.json',{'container':current['Id'],'original_lifetime_seconds':480,'replacement_started':False})
  else:
   docker(['rm',current['Id']]);cleaned=True
assert image and manifest and cleaned
result={'recorded_at':now(),'passed':True,'image':image,'base_image':BASE,'manifest':manifest,'owned_installer_removed':True,
 'host_packages_changed':False,'model_configuration_read':False,'npu_launches':0,'systemd_pid1_started':False}
atomic_json(job/'results/native-image.json',result);atomic_json(output,result)
print(json.dumps({'passed':True,'image':image,'package_count':len(manifest['packages_downloaded']),'native_file_count':len(manifest['native_files']),'owned_installer_removed':True}))

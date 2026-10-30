"""Durable control-plane stages for one actual compiler build through the queue.

Never rerun a task because observation timed out. Source/image preparation and
the build have distinct job IDs; active/unknown work prevents cleanup.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,private_profile,read_json,now

parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['setup','root-renew','root-setup','root-upgrade','configure','build','collect','cleanup']);args=parser.parse_args()
os.umask(0o077)
base=ROOT/'runtime/full-build-20261030';state_path=base/'control.json';state=read_json(state_path)
admin=Client.from_profile(ROOT/'runtime/config.json')
user='demo_full_build_20261030'

def command(*argv):
    result=subprocess.run([str(ROOT/argv[0]),*map(str,argv[1:])],capture_output=True,text=True,check=True,timeout=25)
    return result.stdout

def save():atomic_json(state_path,state)
def docker(*arguments):
    from docker_runtime import discover,command as docker_command
    binding,_=discover();argv,env=docker_command(binding,['docker',*map(str,arguments)])
    return subprocess.run(argv,env=env,capture_output=True,text=True,check=True,timeout=30).stdout
def root_worker(action):
    container=state['root_node_container'];config=Path(state['root_node_state'])/'config.json'
    if action=='start':docker('exec','-d',container,'/opt/lab-build-venv/bin/python','/opt/ascend-lab-node-code/worker.py','--config',config)
    else:docker('exec',container,'/opt/lab-build-venv/bin/python','/opt/ascend-lab-node-code/workerctl','stop','--config',config)
def idle():
    summary=admin.request('/api/jobs')
    assert not any(summary['counts'][key] for key in ['queued','reserved','running','cancelling','needs_attention'])
    assert not any(domain['admission']['blocked'] for domain in summary['resource_domains'])
    return summary
def owner():return Client.from_profile(ROOT/'runtime/personas'/user/'client.json')
def wait(job,seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=owner().request('/api/jobs/'+job)
        if value['job']['state'] in {'succeeded','failed','cancelled','timed_out'}:return value
        if value['job']['state']=='needs_attention':raise RuntimeError('Original build needs reconciliation; preserve its attempt and resources')
        time.sleep(.5)
    raise RuntimeError('Observation deadline only; inspect original job '+job+' without resubmitting')

if args.phase=='setup':
    assert state is None and read_json(base/'image.json')['passed']
    summary=idle();domain=summary['resource_domains'][0]
    memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
    assert memory>20*1024**3
    original=private_profile(ROOT/'runtime/config.json');atomic_json(base/'original-main-config.json',original)
    state={'started_at':now(),'user':user,'baseline_domain':domain,'image':read_json(base/'image.json')['image'],'jobs':[]}
    save()
    command('labctl','stop')
    changed={**original,'scheduler':{**original['scheduler'],'memory_mib':16896,'io_limits':{'file_mib':2048,'input_mib':2048,'artifact_mib':2048}}}
    atomic_json(ROOT/'runtime/config.json',changed);command('labctl','start')
    command('workerctl','domain-set',domain['physical_id'],'--cpus',','.join(map(str,domain['policy']['cpu_ids'])),
            '--memory-mib','16896','--shm-mib',domain['policy']['shm_mib'],'--max-running','1','--expected-revision',domain['revision'])
    command('accessctl','user-add',user)
    command('accessctl','environment-grant',user,'worker-python')
    command('accessctl','quota',user,'--max-running','1','--max-queued','2','--cpu','2','--memory-mib','16384','--shm-mib','0','--npu','0',
            '--input-storage-mib','512','--archive-storage-mib','4096')
    io=base/'node-io.json';atomic_json(io,{'file_mib':2048,'input_mib':2048,'artifact_mib':2048})
    enrolled=json.loads(command('workerctl','enroll','--name','private-full-triton-build','--cpus',','.join(map(str,domain['policy']['cpu_ids'])),
                 '--memory-mib','16512','--shm-mib','0','--max-running','1','--image',state['image'],'--user-map',user+':23701:23701',
                 '--physical-id',domain['physical_id'],'--io-config',io))
    state.update(node_id=enrolled['node_id'],node_config=enrolled['config']);save()
    config=read_json(state['node_config']);config.update(python='/opt/lab-build-venv/bin/python',max_timeout_seconds=3600)
    atomic_json(Path(state['node_config']),config)
    command('workerctl','node-templates',state['node_id'],'--template','environment_probe','--template','build_v1','--template','ir_v1')
    command('workerctl','start','--config',state['node_config'])
    probe=json.loads(command('workerctl','node-probe',state['node_id'],'--user',user,'--tool','cmake','--tool','ninja','--tool','c++',
                             '--tool','ld.lld','--file','/opt/lab-llvm-manifest.json','--file','/opt/lab-bishengir-manifest.json',
                             '--package','setuptools','--package','pybind11'))
    state['probe_job']=probe['job_id'];save();result=wait(probe['job_id'],150);assert result['job']['state']=='succeeded',result['job']['reason']
    command('workerctl','stop','--config',state['node_config'])
    command('workerctl','node-environment',state['node_id'],'--from-job',probe['job_id'],'--expected-revision','1')
    command('workerctl','start','--config',state['node_config']);state['setup_complete']=True;save()
    print(json.dumps({'setup_complete':True,'node_id':state['node_id'],'image':state['image'],'probe_job':probe['job_id'],
                      'temporary_demo_budget_mib':16896,'task_budget_mib':16384,'task_cpu':2,'npu':0},indent=2),flush=True)
elif args.phase=='root-renew':
    assert state and state.get('root_mode') and not state.get('cleaned_up');idle()
    inspection=json.loads(docker('inspect',state['root_node_container']))[0]
    assert inspection['State']['Running'] and inspection['Config']['Labels'].get('io.ascend-lab.fullbuild-node')=='20261030'
    check='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);assert all(v.get("stage")=="cleaned" and v.get("controller_acknowledged") and not v.get("artifact_gc_requested") for f in p.glob("attempts/*/control/state.json") for v in [json.loads(f.read_text())]);assert not list(Path(sys.argv[2]).iterdir())'
    docker('exec',state['root_node_container'],'/opt/lab-build-venv/bin/python','-I','-c',check,state['root_node_state'],state['root_workspace_path'])
    root_worker('stop');command('workerctl','node-disable',state['node_id'])
    old=read_json(base/'root-workspace.json')['slots'][0]
    docker('restart','--time','5',state['root_node_container'])
    probe='import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);assert not list(p.iterdir());s=p.stat();v=os.statvfs(p);print(json.dumps({"device":s.st_dev,"root_inode":s.st_ino,"bytes":v.f_blocks*v.f_frsize,"inodes":v.f_files}))'
    slot=json.loads(docker('exec',state['root_node_container'],'/opt/lab-build-venv/bin/python','-I','-c',probe,state['root_workspace_path']))
    assert slot['bytes']==old['bytes'] and slot['inodes']==old['inodes']
    import uuid
    slot.update(id='fullbuild0',storage_id=uuid.uuid4().hex,path=state['root_workspace_path'],filesystem='tmpfs')
    atomic_json(base/'root-workspace.json',{'slots':[slot]})
    state.setdefault('root_renewals',[]).append({'recorded_at':now(),'previous_started_at':inspection['State']['StartedAt'],
                    'previous_storage_id':old['storage_id'],'new_storage_id':slot['storage_id'],'all_previous_work_confirmed':True})
    save();print('Only the idle owned node container restarted; fresh empty tmpfs identity recorded. Run root-upgrade before any new task.')
elif args.phase in {'root-setup','root-upgrade'}:
    assert state and state.get('root_node_container') and (not state.get('root_mode') or args.phase=='root-upgrade')
    summary=idle();assert int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))>64*1024**2
    if state.get('root_mode'):root_worker('stop')
    else:command('workerctl','stop','--config',state['node_config'])
    command('workerctl','node-disable',state['node_id'])
    state['legacy_node_id']=state['node_id'];state['legacy_node_config']=state['node_config'];save()
    original=private_profile(ROOT/'runtime/config.json');command('labctl','stop')
    atomic_json(ROOT/'runtime/config.json',{**original,'scheduler':{**original['scheduler'],'memory_mib':49792}});command('labctl','start')
    domain=summary['resource_domains'][0]
    command('workerctl','domain-set',domain['physical_id'],'--cpus',','.join(map(str,domain['policy']['cpu_ids'])),
            '--memory-mib','49792','--shm-mib',domain['policy']['shm_mib'],'--max-running','1','--expected-revision',domain['revision'])
    command('accessctl','quota',user,'--memory-mib','49152')
    # The idle owned container has not started a worker; refresh its public code from a verified dev bundle.
    import tarfile
    from deployment import bundle
    package=base/('root-node-source-v022-'+str(time.time_ns())+'.tar.gz');release=bundle(package)
    with tarfile.open(package) as tar:
        for member in tar:
            assert member.isfile() and '..' not in Path(member.name).parts
            target=base/'root-node-code'/member.name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            if target.exists():target.chmod(0o600)
            with tar.extractfile(member) as stream,target.open('wb') as out:shutil.copyfileobj(stream,out)
            target.chmod(0o444)
    state['root_node_source_release']=release['release_id']
    if args.phase=='root-upgrade':state['root_image']=read_json(base/'runtime-image.json')['image']
    enrolled=json.loads(command('workerctl','enroll','--name','root-full-triton-build','--cpus',','.join(map(str,domain['policy']['cpu_ids'])),
                 '--memory-mib','49280','--shm-mib','0','--max-running','1','--image',state['root_image'],'--user-map',user+':23701:23701',
                 '--physical-id',domain['physical_id'],'--io-config',base/'node-io.json','--workspace-config',base/'root-workspace.json',
                 '--node-state-dir',state['root_node_state']))
    state.update(node_id=enrolled['node_id'],node_config=enrolled['config'],image=state['root_image'],root_mode=True);save()
    config=read_json(state['node_config']);config.update(python='/opt/lab-build-venv/bin/python',max_timeout_seconds=3600,
                                                      environment_setup=state['environment_setup'])
    atomic_json(Path(state['node_config']),config)
    command('workerctl','node-templates',state['node_id'],'--template','environment_probe','--template','build_v1','--template','ir_v1')
    config=read_json(state['node_config']);atomic_json(Path(state['root_node_state'])/'config.json',config)
    root_worker('start')
    probe=json.loads(command('workerctl','node-probe',state['node_id'],'--user',user,'--memory-mib','32768',
                             '--tool','cmake','--tool','ninja','--tool','c++','--tool','ld.lld','--tool','git',
                             '--file','/opt/lab-llvm-manifest.json','--file','/opt/lab-bishengir-manifest.json',
                             *(['--file','/opt/lab-toolchain-link-audit.json'] if (base/'link-image-id').is_file() and (base/'link-image-id').read_text().strip()==state['image'] else []),
                             *(['--file','/opt/lab-pybind-import-audit.json','--file','/opt/lab-toolchain-link-audit.json'] if (read_json(base/'runtime-image.json') or {}).get('pybind_import_audit') else []),
                             '--package','setuptools','--package','pybind11'))
    state['probe_job']=probe['job_id'];save();result=wait(probe['job_id'],150);assert result['job']['state']=='succeeded',result['job']['reason']
    root_worker('stop')
    command('workerctl','node-environment',state['node_id'],'--from-job',probe['job_id'],'--expected-revision','1')
    atomic_json(Path(state['root_node_state'])/'config.json',read_json(state['node_config']));root_worker('start')
    state['root_setup_complete']=True;save()
    print(json.dumps({'root_setup_complete':True,'node_id':state['node_id'],'probe_job':probe['job_id'],
                      'finite_workspace_gib':32,'task_memory_mib':49152,'task_cpu':2,'npu':0},indent=2),flush=True)
elif args.phase in {'configure','build'}:
    assert state and state.get('setup_complete') and not state.get('cleaned_up')
    client=owner();folder=base/'submission';folder.mkdir(mode=0o700,exist_ok=True)
    if not (folder/'source.tar.gz').exists():os.link(base/'source.tar.gz',folder/'source.tar.gz')
    shutil.copyfile(base/'source.json',folder/'source.json')
    shutil.copyfile(ROOT/'examples/workflows/full_triton_build.py',folder/'build.py')
    bundle=client.upload_input(folder)
    existing=next((item for item in state['jobs'] if item['phase']==args.phase and item['input_id']==bundle['id'] and item.get('image')==state['image']),None)
    if existing:print(json.dumps({'reused':True,**existing},indent=2));raise SystemExit(0)
    idle()
    deadline=time.monotonic()+30;selected=None
    while time.monotonic()<deadline:
        selected=next(n for n in client.request('/api/nodes')['nodes'] if n['id']==state['node_id'])
        if selected['environment'].get('sha256') and selected['status']=='online':break
        time.sleep(.2)
    lock=selected['environment']['lock']
    assert all(name in lock.get('tools',{}) for name in ['cmake','ninja','c++','ld.lld','git']), 'Certify the explicit build toolchain; absent probe facts cannot satisfy build prerequisites'
    timeout=240 if args.phase=='configure' else 3600
    if state.get('root_mode') and args.phase=='build':
        # This owned demo daemon is time-bounded. Leave time for receipts and GC
        # rather than launching a task that can outlive its node container.
        from datetime import datetime
        container=json.loads(docker('inspect',state['root_node_container']))[0]
        expiry=datetime.fromisoformat(container['State']['StartedAt'].replace('Z','+00:00')).timestamp()+7200
        timeout=min(timeout,int(expiry-time.time()-300))
        assert timeout>=2400,'Renew the idle owned node before another full build; do not launch beyond its lifetime'
    request={'name':'完整Triton编译 · '+args.phase,'environment':'worker-python','template':'build_v1',
      'idempotency_key':'full-triton-20261030-'+args.phase+'-'+bundle['id']+'-'+state['image'][7:19],
      'parameters':{'input_id':bundle['id'],'argv':['python','build.py',*(['--configure-only'] if args.phase=='configure' else [])],
       'lock':lock,'resources':{'cpu':2,'memory_mib':49152 if state.get('root_mode') else 16384,'shm_mib':0,'npu':0},
       'timeout_seconds':timeout,
       'artifacts':{'file_mib':2048,'total_mib':2048},
       **({'workspace':{'min_mib':24576,'min_inodes':50000,'durable':False}} if state.get('root_mode') else {})}}
    atomic_json(base/('request-'+args.phase+'.json'),request)
    response=client.request('/api/jobs',request);job=response['job']
    entry={'phase':args.phase,'job_id':job['id'],'input_id':bundle['id'],'image':state['image']};state['jobs'].append(entry);save()
    print(json.dumps(entry,indent=2),flush=True)
elif args.phase=='collect':
    assert state and state['jobs']
    entry=state['jobs'][-1];detail=owner().request('/api/jobs/'+entry['job_id']);job=detail['job']
    print(json.dumps({'job_id':job['id'],'phase':entry['phase'],'state':job['state'],'attempts':len(detail['attempts'])},indent=2),flush=True)
    if job['state'] not in {'succeeded','failed','cancelled','timed_out'}:raise SystemExit(2)
    for name in ['results/full-build-provenance.json','results/full-build.log','results/workload-result.json']:
        try:data=owner().request('/api/jobs/'+job['id']+'/artifact?name='+name,raw=True,max_bytes=32*1024**2)
        except Exception:continue
        (base/(entry['phase']+'-'+Path(name).name)).write_bytes(data)
    state['latest_terminal']={'job_id':job['id'],'state':job['state']};save()
else:
    assert state and state.get('setup_complete') and not state.get('cleaned_up');summary=idle()
    config=read_json(state['node_config'])
    if state.get('root_mode'):
        check='from pathlib import Path;import json,sys;p=Path(sys.argv[1]);assert all((lambda v:v.get("controller_acknowledged") and not v.get("artifact_gc_requested"))(json.loads(q.read_text())) for q in p.glob("attempts/*/control/state.json"))'
        docker('exec',state['root_node_container'],'/opt/lab-build-venv/bin/python','-I','-c',check,state['root_node_state'])
        root_worker('stop')
        restore='import os,sys;from pathlib import Path;p=Path(sys.argv[1]);uid=int(sys.argv[2]);gid=int(sys.argv[3])\nfor parent,dirs,files in os.walk(p,followlinks=False):\n for name in dirs+files:\n  q=Path(parent)/name;os.lchown(q,uid,gid)\n  if q.is_dir() and not q.is_symlink():q.chmod(0o700)\nos.chown(p,uid,gid);p.chmod(0o700)\n'
        docker('exec',state['root_node_container'],'/opt/lab-build-venv/bin/python','-I','-c',restore,state['root_node_state'],os.getuid(),os.getgid())
    else:
        pending=list((Path(config['state_dir'])/'attempts').glob('*/control/state.json'))
        assert all((read_json(path) or {}).get('controller_acknowledged') and not (read_json(path) or {}).get('artifact_gc_requested') for path in pending)
        command('workerctl','stop','--config',state['node_config'])
    command('workerctl','node-disable',state['node_id'])
    command('accessctl','user-disable',user)
    old=state['baseline_domain'];current=next(d for d in summary['resource_domains'] if d['physical_id']==old['physical_id']);policy=old['policy']
    command('workerctl','domain-set',old['physical_id'],'--cpus',','.join(map(str,policy['cpu_ids'])),'--memory-mib',policy['memory_mib'],
            '--shm-mib',policy['shm_mib'],'--max-running',policy['max_running'],'--expected-revision',current['revision'])
    command('labctl','stop');atomic_json(ROOT/'runtime/config.json',read_json(base/'original-main-config.json'));command('labctl','start')
    if state.get('root_mode'):
        info=json.loads(docker('inspect',state['root_node_container']))[0]
        assert info['Config'].get('Labels',{}).get('io.ascend-lab.fullbuild-node')=='20261030'
        docker('stop','--time','5',state['root_node_container']);docker('rm',state['root_node_container'])
        volume=json.loads(docker('volume','inspect',state['root_workspace_volume']))[0]
        assert volume.get('Labels',{}).get('io.ascend-lab.fullbuild-workspace')=='20261030'
        docker('volume','rm',state['root_workspace_volume'])
    state['cleaned_up']=True;state['finished_at']=now();save();print('Only this temporary account/node disabled; original demo budgets restored.')

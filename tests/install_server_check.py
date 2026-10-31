"""Actual administrator installation/lifecycle/restore in two owned, time-bounded containers."""
import base64
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,now
from deployment import bundle,unpack_release
from backends import physical_identity

arguments=argparse.ArgumentParser();arguments.add_argument('--large-wheel');arguments.add_argument('--https-network',action='store_true');arguments.add_argument('--disk-workspace',action='store_true');arguments.add_argument('--user-quota-workspace',action='store_true');arguments.add_argument('--storage-pressure',action='store_true');arguments.add_argument('--control-storage-pressure',action='store_true');arguments.add_argument('--central-payload',action='store_true');arguments.add_argument('--central-owner-quotas',action='store_true');arguments.add_argument('--node-spool-pressure',action='store_true');arguments.add_argument('--persistent-cache',action='store_true');arguments.add_argument('--dependencies',action='store_true');arguments.add_argument('--input-durability',action='store_true');arguments.add_argument('--native-toolchain',action='store_true');arguments.add_argument('--payload-quota-resize',action='store_true');arguments.add_argument('--node-quota-resize',action='store_true');arguments.add_argument('--deadline-limit',action='store_true');arguments.add_argument('--operator-analysis');arguments.add_argument('--operator-image');arguments.add_argument('--compiler-case');arguments.add_argument('--large-compiler-case');arguments.add_argument('--accuracy-case');arguments.add_argument('--terminal-handoff');arguments.add_argument('--source-grants');arguments.add_argument('--simulation-case');arguments.add_argument('--report-view');arguments.add_argument('--native-control',action='store_true');arguments.add_argument('--audit-retention',action='store_true');arguments.add_argument('--browser-workflow',action='store_true');arguments.add_argument('--experiment-budget',action='store_true');options=arguments.parse_args()
assert not options.native_control or not any([options.control_storage_pressure,options.central_payload,options.operator_analysis,options.https_network,options.large_wheel]), 'Native control uses its independent own disks and CPU fixture'
assert not options.audit_retention or options.native_control, 'Audit crash/restore proof uses the isolated native control fixture'
https=options.https_network
user_quota=options.user_quota_workspace
central_groups=options.central_owner_quotas
if central_groups:
    assert user_quota,'Owner-quota acceptance includes two independent node task UIDs'
    options.central_payload=True
assert not options.payload_quota_resize or central_groups, 'Quota resize acceptance requires owner group storage'
assert not options.node_quota_resize or (user_quota and options.persistent_cache and not any([options.native_toolchain,options.operator_analysis,options.https_network,options.central_payload,options.dependencies,options.source_grants,options.terminal_handoff,options.accuracy_case,options.simulation_case,options.report_view,options.native_control])), 'Node quota resize uses an independent stdlib UID/cache fixture'
assert not (user_quota and options.disk_workspace)
assert not options.persistent_cache or user_quota, 'Cache acceptance requires two mapped users and separate quota workspaces'
assert not options.input_durability or options.central_payload, 'Input durability acceptance requires a dedicated payload filesystem'
assert not options.native_toolchain or (user_quota and options.persistent_cache and https and options.dependencies), 'Native toolchain acceptance includes quota/cache/HTTPS/workflow restore'
assert not options.operator_analysis or (user_quota and options.operator_image and not options.native_toolchain and not options.persistent_cache and not https), 'Operator analysis uses its own quota/image fixture, without unrelated cache or TLS cases'
assert not options.compiler_case or options.operator_analysis, 'Compiler case extends the qualified operator image fixture'
assert not options.large_compiler_case or (options.operator_analysis and not options.compiler_case), 'Large IR has its own immutable-image fixture and no model draft'
assert not options.accuracy_case or (user_quota and not any([options.operator_analysis,options.native_control,options.native_toolchain,options.compiler_case,options.large_compiler_case])), 'Accuracy replay uses its own stdlib CPU quota fixture'
assert not options.terminal_handoff or (user_quota and not any([options.accuracy_case,options.operator_analysis,options.native_control,options.native_toolchain])), 'Terminal handoff uses an independent CPU quota fixture'
assert not options.source_grants or (user_quota and not any([options.accuracy_case,options.terminal_handoff,options.operator_analysis,options.native_control,options.native_toolchain,options.simulation_case,options.report_view])), 'Source grants use an independent stdlib CPU quota fixture'
assert not options.simulation_case or (user_quota and not any([options.accuracy_case,options.terminal_handoff,options.operator_analysis,options.native_control,options.native_toolchain])), 'Simulation analysis uses its own bounded CPU quota fixture'
assert not options.report_view or (options.operator_analysis and user_quota and not any([options.compiler_case,options.large_compiler_case,options.native_control,options.native_toolchain])), 'Report view extends the qualified operator image with independent CPU report cases'
disk=options.disk_workspace or user_quota
assert not (disk and options.large_wheel), 'Keep disk hard-limit and large-wheel fixtures separate'
assert not options.control_storage_pressure or (not options.storage_pressure and not options.central_payload and not options.large_wheel), 'Control capacity fault injection uses a separate bounded state filesystem'
assert not options.control_storage_pressure or (not https and not options.operator_analysis), 'Control headroom browser/fault acceptance uses its explicit local CPU role fixture'
assert not (options.storage_pressure and options.large_wheel), 'Keep the full-disk fault injection bounded'
assert not (options.central_payload and (options.storage_pressure or options.large_wheel)), 'Keep production storage and fault injection/large transfers separately verifiable'
large_wheel=Path(options.large_wheel).absolute() if options.large_wheel else None
if large_wheel:assert large_wheel.is_relative_to(ROOT.parent) and large_wheel.is_file() and not large_wheel.is_symlink()

assert os.environ.get('LAB_JOB_DIR'),'Submit this trusted acceptance through the queue'
assert len(os.sched_getaffinity(0))>=2
base=Path(os.environ['LAB_JOB_DIR'])/'tmp/installation';base.mkdir(mode=0o700)
release1=base/'server-v1.tar.gz';v1=bundle(release1)
bootstrap,manifest=unpack_release(release1,base)
candidate=base/'candidate';shutil.copytree(bootstrap,candidate)
doc=candidate/'SERVER.md';doc.chmod(0o600);doc.write_text(doc.read_text()+'\nCompatible release fixture, same state schema and runtime code.\n')
release2=base/'server-v2.tar.gz';v2=bundle(release2,candidate)
controller_state=base/'controller-state';node_parent=base/'node-control';node_parent.mkdir(mode=0o700);node_state=node_parent/'state'
for path in [controller_state,node_state]:path.mkdir(mode=0o700)
prefix='ascend-lab-install-'+uuid.uuid4().hex[:8];controller=prefix+'-controller';node=prefix+'-node'
cpus=sorted(os.sched_getaffinity(0));image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
task_image=(ROOT/'runtime/toolchain-cache-20261030/offline-image-id').read_text().strip() if options.native_toolchain else image
task_python='/usr/local/python3.11.15/bin/python3.11' if options.native_toolchain else '/usr/bin/python3'
task_version='3.11.15' if options.native_toolchain else '3.10.12'
task_setup='/opt/ascend-lab-native-env.sh' if options.native_toolchain else '/usr/local/Ascend/ascend-toolkit/set_env.sh'
if options.operator_analysis:
    operator_image=json.loads(Path(options.operator_image).read_text());task_image=operator_image['image'];task_python=operator_image['python'];task_version=operator_image['python_version']
    assert re.fullmatch('sha256:[a-f0-9]{64}',task_image) and task_python=='/opt/ascend-lab-operator/venv/bin/python' and re.fullmatch(r'3\.11\.\d+',task_version)
    task_setup='/opt/ascend-lab-native-env.sh'
with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
containers=[];volumes=[];networks=[];stopped_main=False;checks=[];token=None;tls_context=None;authority_text=None;renewal=None;https_mcp=None
retention_proof={};mount_proof={};owned_disk=None;pressure_disk=None;pressure_volume=None;storage_pressure=None;control_storage_pressure=None;spool_disk=None
central_disks=[];central_volumes=[];payload_args=[];payload_contracts=[];central_proof=None
native_control_volumes=[];native_control_proof=None;browser_workflow_proof=None;experiment_budget_proof=None
group_pressure={};quota_resize_proof=None;node_quota_proof=None;deadline_proof=None;operator_proof=None;compiler_proof=None;large_compiler_proof=None;accuracy_proof=None;terminal_handoff_proof=None;source_grant_proof=None;simulation_proof=None;report_view_proof=None
node_spool_pressure=None;cache_disk=None;cache_volume=None;cache_policy=None;cache_proof=None;dependency_proof=None;input_durability=None;native_proof=None
controller_url=('https://controller.lab:' if https else 'http://127.0.0.1:')+str(port)
outer_url=('https' if https else 'http')+'://127.0.0.1:'+str(port)


def docker(args,check=True,input=None,timeout=30):
    result=subprocess.run(['docker',*args],input=input,capture_output=True,text=True,timeout=timeout)
    if check and result.returncode:raise RuntimeError('Owned installation operation failed: '+result.stderr[-1500:])
    return result


def ctl(container,role,action,*arguments,check=True,timeout=35):
    result=docker(['exec',container,'/usr/bin/python3','-B','/bootstrap/servicectl',action,'--prefix','/opt/ascend-lab-'+role,*arguments],check=check,timeout=timeout)
    if check and result.stdout.strip():
        try:return json.loads(result.stdout)
        except ValueError:
            if action=='admin':return {'output':result.stdout}
            raise
    return result


def secret_json(container,path):
    code='import json,sys;from pathlib import Path;sys.stdout.write(Path(sys.argv[1]).read_text())'
    # Captured in parent memory only; never print or include credential JSON in reports.
    return json.loads(docker(['exec',container,'/usr/bin/python3','-I','-c',code,str(path)]).stdout)


def api(path,body=None,raw=False,extra_headers=None):
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
    headers.update(extra_headers or {})
    request=urllib.request.Request(outer_url+path,headers=headers,
                                  data=json.dumps(body).encode() if body is not None else None)
    handlers=[urllib.request.ProxyHandler({})]+([urllib.request.HTTPSHandler(context=tls_context)] if https else [])
    try:response=urllib.request.build_opener(*handlers).open(request,timeout=120 if path.endswith('/seal') else 8)
    except urllib.error.HTTPError as error:response=error
    with response:
        data=response.read();return response.status,data if raw else json.loads(data)


def check(message):checks.append(message);print('PASS:',message,flush=True)


def storage(kind,value,action,headers):
    status,preview=api('/api/storage/plan',{'kind':kind,'id':value,'action':action},extra_headers=headers)
    assert status==200,preview
    status,result=api('/api/storage/apply',preview['plan'],extra_headers=headers)
    assert status==200,result
    for _ in range(40):
        if not result.get('pending'):return preview,result
        status,resume=api('/api/storage/plan',{'kind':kind,'id':value,'action':'resume'},extra_headers=headers)
        assert status==200,resume
        status,result=api('/api/storage/apply',resume['plan'],extra_headers=headers);assert status==200,result
    raise AssertionError('Inspect pending storage operation before continuing fixture')


def wait(job_id,seconds=60,headers=None):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:status,body=api('/api/jobs/'+job_id,extra_headers=headers)
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            # Read-only observation only: retain the same task/attempt after a
            # transient TLS/network failure; no submission is repeated here.
            time.sleep(.2);continue
        assert status==200,body
        if body['job']['state'] in {'succeeded','failed','timed_out','cancelled'}:return body['job']
        time.sleep(.2)
    raise RuntimeError('Observation deadline exceeded; inspect the original Root installation task '+job_id+' without resubmission')


def actual_controller_identity():
    code='import json;from pathlib import Path;m=json.loads(Path("/opt/ascend-lab-controller/installation.json").read_text());p=json.loads((Path(m["state_dir"])/"service-process.json").read_text())["pid"];s={k:v.strip() for k,sep,v in (line.partition(":") for line in Path("/proc/"+str(p)+"/status").read_text().splitlines()) if sep};print(json.dumps({"uid":int(s["Uid"].split()[1]),"groups":sorted(int(x) for x in s["Groups"].split()),"cap_effective":s["CapEff"],"cap_permitted":s["CapPrm"],"no_new_privileges":s["NoNewPrivs"],"expected_groups":sorted(q["gid"] for q in m["payload_storage"]["owner_groups"].values())}))'
    identity=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',code]).stdout)
    assert identity['uid']>0 and identity['groups']==identity['expected_groups']
    assert identity['cap_effective']==identity['cap_permitted']=='0000000000000000' and identity['no_new_privileges']=='1'
    return identity


def restore_host_owner():
    code='import os\np="/data"\nfor parent,dirs,files in os.walk(p,followlinks=False):\n for name in dirs+files:\n  q=os.path.join(parent,name);os.lchown(q,'+str(os.getuid())+','+str(os.getgid())+')\n  if name in dirs and not os.path.islink(q):os.chmod(q,0o700)\nos.chown(p,'+str(os.getuid())+','+str(os.getgid())+')\nos.chmod(p,0o700)\n'
    docker(['run','--rm','--label','io.ascend-lab.installproof='+prefix,'--network','none','--read-only',
            '--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER',
            '--memory','128m','--memory-swap','128m','--pids-limit','32','--cpuset-cpus',str(cpus[0]),
            '--mount','type=bind,src='+str(base)+',dst=/data','--entrypoint','/usr/bin/python3',image,'-I','-c',code])


try:
    if options.operator_analysis:
        from lab_client import Client
        current=Client(json.loads((ROOT/'runtime/config.json').read_text())).request('/api/jobs')
        expected=json.loads((Path(os.environ['LAB_JOB_DIR'])/'dispatch.json').read_text())['id']
        assert {j['id'] for j in current['jobs'] if j['state'] not in {'succeeded','failed','cancelled','timed_out'}}=={expected}
    else:
        subprocess.run([str(ROOT/'labctl'),'stop'],capture_output=True,check=True,timeout=15);stopped_main=True
    if options.operator_analysis:
        readonly_probe="import hashlib,json,os,sys;from pathlib import Path;p=Path('/opt/ascend-lab-operator/app');sys.path.insert(0,str(p));from operator_profile import check_provider;assert os.getuid()==23003;print(json.dumps({'versions':check_provider(),'app_sha256':{n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in ['operator_node.py','operator_profile.py','diagnostic_case.py','lab_client.py']}}))"
        result=docker(['run','--rm','--network','none','--read-only','--user','23003:23003','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--memory','128m','--memory-swap','128m','--pids-limit','16',
            '--cpuset-cpus',str(cpus[-1]),'--entrypoint',task_python,task_image,'-I','-B','-c',readonly_probe])
        observed=json.loads(result.stdout)
        assert observed['app_sha256']=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in observed['app_sha256']}
        check('Fixed tool image is readable by the actual non-root task UID and reviewed provider code is present before installing services')
    # Only new fixture directories are exposed; prove same-device bind mounts are not traversed.
    mount_parent=base/'mount-proof';(mount_parent/'payload/nested').mkdir(parents=True,mode=0o755)
    mount_other=base/'mount-other';mount_other.mkdir(mode=0o755);(mount_other/'sentinel').write_bytes(b'owned mount sentinel')
    for directory in [mount_parent,mount_parent/'payload',mount_parent/'payload/nested',mount_other]:directory.chmod(0o755)
    if https:
        network=prefix+'-isolated'
        docker(['network','create','--internal','--label','io.ascend-lab.installproof='+prefix,network]);networks.append(network)
    if options.storage_pressure or options.control_storage_pressure:
        sys.path.insert(0,str(ROOT/'tests'))
        from disk_workspace_fixture import OwnedDisk
        pressure_disk=OwnedDisk(base,prefix+'-pressure',image,cpus,docker,folder_name='archive-pressure',image_mib=128 if options.control_storage_pressure else 32,inodes=8192 if options.control_storage_pressure else 512)
        pressure_device=pressure_disk.create();pressure_volume=prefix+'-pressure'
        docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
                '--opt','type=ext4','--opt','device='+pressure_device,'--opt','o=nosuid,nodev',pressure_volume])
        volumes.append(pressure_volume)
    if options.central_payload:
        sys.path.insert(0,str(ROOT/'tests'));from disk_workspace_fixture import OwnedDisk
        for label in ['central-payload','central-restore']:
            fixture=OwnedDisk(base,prefix+'-'+label,image,cpus,docker,folder_name=label,image_mib=512,inodes=8192,quota_type='group' if central_groups else None,helper_seconds=1800 if options.deadline_limit else 1500 if options.node_quota_resize else 1200 if options.compiler_case or options.accuracy_case or options.terminal_handoff or options.source_grants or options.simulation_case or options.report_view else 720)
            central_disks.append(fixture);device=fixture.create();mounted=prefix+'-'+label
            docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
                    '--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev'+(',grpquota' if central_groups else ''),mounted])
            volumes.append(mounted);central_volumes.append(mounted)
    if options.native_control:
        sys.path.insert(0,str(ROOT/'tests'));from disk_workspace_fixture import OwnedDisk
        for i in range(6):
            folder=base/('native-disk-'+str(i));folder.mkdir(mode=0o700)
            fixture=OwnedDisk(folder,prefix+'-native'+str(i),image,cpus,docker,image_mib=256 if i in {0,1,4} else 512,inodes=8192,helper_seconds=1200)
            central_disks.append(fixture);device=fixture.create();mounted=prefix+'-native'+str(i)
            docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local','--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev',mounted])
            volumes.append(mounted);native_control_volumes.append(mounted)
    sys.path.insert(0,str(ROOT/'tests'));from disk_workspace_fixture import OwnedDisk
    spool_disk=OwnedDisk(base,prefix+'-spool',image,cpus,docker,folder_name='spool-workspace',image_mib=4096 if large_wheel else 1024,inodes=262144,helper_seconds=1800 if options.deadline_limit else 1500 if options.node_quota_resize else 1200 if options.compiler_case or options.accuracy_case or options.terminal_handoff or options.source_grants or options.simulation_case or options.report_view else 720)
    device=spool_disk.create();spool_volume=prefix+'-spool'
    docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
            '--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev',spool_volume]);volumes.append(spool_volume)
    spool_path=json.loads(docker(['volume','inspect',spool_volume]).stdout)[0]['Mountpoint']
    if options.persistent_cache:
        cache_disk=OwnedDisk(base,prefix+'-cache',image,cpus,docker,folder_name='cache-workspace',image_mib=128,inodes=8192 if options.native_toolchain else 4096,quota_type='user',helper_seconds=1800 if options.deadline_limit else 1500 if options.node_quota_resize else 1200 if options.compiler_case or options.accuracy_case or options.terminal_handoff or options.source_grants or options.simulation_case or options.report_view else 720)
        device=cache_disk.create();cache_volume=prefix+'-cache'
        docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
            '--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev,usrquota',cache_volume]);volumes.append(cache_volume)
        cache_path=json.loads(docker(['volume','inspect',cache_volume]).stdout)[0]['Mountpoint']
    volume=prefix+'-work0'
    if disk:
        sys.path.insert(0,str(ROOT/'tests'))
        from disk_workspace_fixture import OwnedDisk
        owned_disk=OwnedDisk(base,prefix,image,cpus,docker,image_mib=1024 if options.simulation_case or options.report_view else 128 if user_quota else 32,inodes=8192 if options.native_toolchain else 4096 if user_quota else 512,quota_type='user' if user_quota else None,helper_seconds=1800 if options.deadline_limit else 1500 if options.node_quota_resize else 1200 if options.compiler_case or options.accuracy_case or options.terminal_handoff or options.source_grants or options.simulation_case or options.report_view else 720)
        loop_device=owned_disk.create()
        docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
                '--opt','type=ext4','--opt','device='+loop_device,'--opt','o=nosuid,nodev'+(',usrquota' if user_quota else ''),volume])
    else:
        docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
                '--opt','type=tmpfs','--opt','device=tmpfs',
                '--opt','o=size=32m,nr_inodes=512,mode=0700,uid=0,gid=0,nosuid,nodev',volume])
    volumes.append(volume)
    slot_path=json.loads(docker(['volume','inspect',volume]).stdout)[0]['Mountpoint']
    if disk:
        seed='import os;from pathlib import Path;p=Path("/disk/persistence-sentinel");p.write_bytes(b"ascend-lab-owned-ext4-remount-v1");f=os.open(p,os.O_RDONLY);os.fsync(f);os.close(f);f=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(f);os.close(f)'
        docker(['run','--rm','--label','io.ascend-lab.installproof='+prefix,'--network','none','--read-only',
                '--cap-drop','ALL','--memory','128m','--memory-swap','128m','--cpus','0.25','--pids-limit','32',
                '--cpuset-cpus',str(cpus[0]),'--mount','type=volume,src='+volume+',dst=/disk,volume-nocopy',
                '--entrypoint','/usr/bin/python3',image,'-I','-c',seed])
        # No running container references the new volume now: Docker unmounts it.
        refs=json.loads(docker(['volume','inspect',volume]).stdout)[0]
        assert refs.get('Options',{}).get('type')=='ext4'
    work_mounts=[(volume,slot_path)]
    if large_wheel:
        large_volume=prefix+'-work-large'
        docker(['volume','create','--label','io.ascend-lab.installproof='+prefix,'--driver','local',
                '--opt','type=tmpfs','--opt','device=tmpfs',
                '--opt','o=size=1536m,nr_inodes=1024,mode=0700,uid=0,gid=0,nosuid,nodev',large_volume])
        volumes.append(large_volume);work_mounts.append((large_volume,json.loads(docker(['volume','inspect',large_volume]).stdout)[0]['Mountpoint']))
    for name,role,state in [(controller,'controller',controller_state),(node,'node',node_state)]:
        argv=['run','--detach','--name',name,'--label','io.ascend-lab.installproof='+prefix,
              '--network',networks[0] if https else 'host','--security-opt','no-new-privileges:true','--cap-drop','ALL',
              '--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER','--cap-add','SETUID','--cap-add','SETGID',
              '--memory','128m' if large_wheel else '384m','--memory-swap','128m' if large_wheel else '384m','--pids-limit','128','--cpuset-cpus',','.join(map(str,cpus)),'--cpus','0.25',
              '--log-opt','max-size=1m','--log-opt','max-file=1',
              '--mount','type=bind,src='+str(bootstrap)+',dst=/bootstrap,readonly',
              '--mount','type=bind,src='+str(release1)+',dst=/provision/server-v1.tar.gz,readonly',
              '--mount','type=bind,src='+str(release2)+',dst=/provision/server-v2.tar.gz,readonly',
              '--mount','type=bind,src='+str(node_parent if role=='node' else state)+',dst='+str(node_parent if role=='node' else state),
              '--mount','type=bind,src=/etc/machine-id,dst=/etc/machine-id,readonly']
        if https and role=='controller':argv+=['--network-alias','controller.lab']
        if (options.storage_pressure or options.control_storage_pressure) and role=='controller':
            # Trusted test administrator only, for private-namespace fault injection.
            # The actual service runs as UID999 with no effective capabilities/socket.
            argv+=['--cap-add','SYS_ADMIN','--security-opt','apparmor=unconfined',
                   '--mount','type=volume,src='+pressure_volume+',dst=/central-pressure,volume-nocopy']
        if options.central_payload and role=='controller':
            argv+=['--cap-add','SYS_ADMIN','--security-opt','apparmor=unconfined']
            for i,mounted in enumerate(central_volumes):
                argv+=['--mount','type=volume,src='+mounted+',dst=/srv/lab-payload'+('-restore' if i else '')+',volume-nocopy']
            argv+=['--mount','type=volume,src='+central_volumes[0]+',dst=/srv/lab-payload-alias,volume-nocopy']
        if options.native_control and role=='controller':
            argv+=['--cap-add','SYS_ADMIN','--security-opt','seccomp=unconfined','--security-opt','apparmor=unconfined']
            for i,mounted in enumerate(native_control_volumes):
                argv+=['--mount','type=volume,src='+mounted+',dst=/srv/native'+str(i)+',volume-nocopy']
        if role=='controller':argv+=['--mount','type=bind,src='+str(mount_parent)+',dst=/mount-proof,readonly',
                                      '--mount','type=bind,src='+str(mount_other)+',dst=/mount-proof/payload/nested,readonly']
        if role=='node':argv+=['--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
                              '--mount','type=bind,src='+shutil.which('docker')+',dst=/usr/bin/docker,readonly']
        if role=='node' and user_quota:argv+=['--cap-add','SYS_ADMIN','--security-opt','seccomp=unconfined',
            '--security-opt','apparmor=unconfined']
        if role=='controller' and central_groups:argv+=['--security-opt','seccomp=unconfined']
        if role=='node':
            argv+=['--mount','type=volume,src='+spool_volume+',dst='+spool_path+',volume-nocopy']
            argv+=['--mount','type=volume,src='+spool_volume+',dst=/spool-proof-alias,volume-nocopy']
            for mounted,path in work_mounts:argv+=['--mount','type=volume,src='+mounted+',dst='+path+',volume-nocopy']
            if cache_volume:argv+=['--mount','type=volume,src='+cache_volume+',dst='+cache_path+',volume-nocopy','--mount','type=volume,src='+cache_volume+',dst=/cache-proof-alias,volume-nocopy']
        lifetime=1500 if options.deadline_limit else 1200 if options.node_quota_resize else 900 if large_wheel or options.compiler_case or options.large_compiler_case or options.accuracy_case or options.terminal_handoff or options.source_grants or options.simulation_case or options.report_view else 900 if options.native_control else 600 if options.native_toolchain or options.operator_analysis or options.control_storage_pressure else 300
        argv+=['--entrypoint','/usr/bin/timeout',image,'--kill-after=5s',str(lifetime)+'s','/usr/bin/python3','-I','-c','import time;time.sleep('+str(lifetime-5)+')']
        docker(argv);containers.append(name)
        claim='import os,sys;os.chown(sys.argv[1],0,0);os.chmod(sys.argv[1],0o700)'
        docker(['exec',name,'/usr/bin/python3','-I','-c',claim,str(state)])
        if role=='node':docker(['exec',name,'/usr/bin/python3','-I','-c',claim,str(node_parent)])
    prepare_spool='import os,json,sys,uuid;from pathlib import Path;p=Path(sys.argv[1]);assert sorted(x.name for x in p.iterdir())==["lost+found"];(p/"lost+found").rmdir();p.chmod(0o700);s=p.stat();v=os.statvfs(p);print(json.dumps({"path":str(p),"filesystem":"ext4","storage_id":uuid.uuid4().hex,"device":s.st_dev,"root_inode":s.st_ino,"bytes":v.f_blocks*v.f_frsize,"inodes":v.f_files}))'
    spool_policy=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',prepare_spool,spool_path]).stdout)
    if disk:
        # Fresh, exclusively owned mkfs image only; production nodes never remove
        # unknown initial filesystem contents during installation.
        prepare='import sys;from pathlib import Path;p=Path(sys.argv[1]);assert sorted(x.name for x in p.iterdir())==["lost+found","persistence-sentinel"];s=p/"persistence-sentinel";assert s.read_bytes()==b"ascend-lab-owned-ext4-remount-v1";s.unlink();(p/"lost+found").rmdir();p.chmod(0o700)'
        docker(['exec',node,'/usr/bin/python3','-I','-c',prepare,slot_path])
        owned_disk.report['sentinel_survived_separate_container_mounts']=True
        check('File-backed ext4 preserves an fsynced sentinel across separate container mounts without changing host mounts')
    if options.central_payload:
        prepare_payload='import os,json,sys,uuid;from pathlib import Path;p=Path(sys.argv[1]);assert sorted(x.name for x in p.iterdir())==["lost+found"];(p/"lost+found").rmdir();p.chmod(0o700);s=p.stat();v=os.statvfs(p);q={"path":str(p),"filesystem":"ext4","storage_id":uuid.uuid4().hex,"device":s.st_dev,"root_inode":s.st_ino,"bytes":v.f_blocks*v.f_frsize,"inodes":v.f_files};f=Path(sys.argv[2]);f.write_text(json.dumps(q));f.chmod(0o600);print(json.dumps(q))'
        for i in range(2):
            path='/srv/lab-payload'+('-restore' if i else '');file='/provision/payload'+str(i)+'.json'
            payload_contracts.append(json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',prepare_payload,path,file]).stdout))
            if central_groups:
                payload_contracts[-1]['owner_groups']={'proof_user':{'gid':23831+i*100,'bytes':192*1024**2,'inodes':1024},
                                                      'proof_peer':{'gid':23832+i*100,'bytes':256*1024**2,'inodes':2048}}
                save='import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600)'
                docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',save,file],input=json.dumps(payload_contracts[-1]))
                command=['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','payload-groups-prepare','--contract',file]
                plan=json.loads(docker(command+['--plan']).stdout);assert plan['read_only']
                prepared=json.loads(docker(command).stdout);repeated=json.loads(docker(command).stdout)
                assert prepared['prepared'] and repeated['prepared'] and not prepared['filesystem_formatted_or_enabled']
        payload_args=['--payload-config','/provision/payload0.json']
        if central_groups:check('Root prepares explicit owner group quotas on two empty disks; read-only plan and repeated preparation preserve the contract')
    listener_args=[]
    if https:
        controller_ip=json.loads(docker(['inspect',controller]).stdout)[0]['NetworkSettings']['Networks'][networks[0]]['IPAddress']
        outer_url='https://'+controller_ip+':'+str(port)
        namespace_code='import os;print(os.readlink("/proc/self/ns/net"))'
        net_namespaces={role:docker(['exec',name,'/usr/bin/python3','-I','-c',namespace_code]).stdout.strip()
                        for role,name in [('controller',controller),('node',node)]}
        assert len(set(net_namespaces.values()))==2 and os.readlink('/proc/self/ns/net') not in net_namespaces.values()
        docker(['exec',controller,'mkdir','-m','700','/provision/tls'])
        docker(['exec',controller,'openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=OwnedDemoCA',
            '-addext','basicConstraints=critical,CA:TRUE','-keyout','/provision/tls/ca.key','-out','/provision/tls/ca.pem'])
        docker(['exec',controller,'openssl','req','-new','-newkey','rsa:2048','-nodes','-subj','/CN=controller.lab',
            '-keyout','/provision/tls/server.key','-out','/provision/tls/server.csr'])
        extension='subjectAltName=DNS:controller.lab,IP:127.0.0.1,IP:'+controller_ip+'\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n'
        ext='from pathlib import Path;Path("/provision/tls/server.ext").write_text('+repr(extension)+')'
        docker(['exec',controller,'/usr/bin/python3','-I','-c',ext])
        docker(['exec',controller,'openssl','x509','-req','-in','/provision/tls/server.csr','-CA','/provision/tls/ca.pem',
            '-CAkey','/provision/tls/ca.key','-CAcreateserial','-days','1','-extfile','/provision/tls/server.ext','-out','/provision/tls/server.pem'])
        authority_text=docker(['exec',controller,'/usr/bin/python3','-I','-c','import sys;from pathlib import Path;sys.stdout.write(Path("/provision/tls/ca.pem").read_text())']).stdout
        tls_context=ssl.create_default_context(cadata=authority_text)
        listener_args=['--bind-address','0.0.0.0','--public-url',controller_url,'--tls-cert','/provision/tls/server.pem',
                       '--tls-key','/provision/tls/server.key','--tls-ca','/provision/tls/ca.pem']
    plan_probe='import json,sys,pwd;from pathlib import Path;exists=bool([p for p in pwd.getpwall() if p.pw_name=="labsvc"]);print(json.dumps({"prefix_exists":Path(sys.argv[1]).exists(),"state_entries":sorted(p.name for p in Path(sys.argv[2]).iterdir()),"service_account_exists":exists}))'
    controller_before=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',plan_probe,'/opt/ascend-lab-controller',str(controller_state)]).stdout)
    rejected_install=docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','install',
        '--archive','/provision/missing.tar.gz','--role','controller','--prefix','/opt/ascend-lab-controller',
        '--state-dir',str(controller_state),'--create-service-user','labsvc','--port',str(port)],check=False)
    assert rejected_install.returncode==1 and 'preflight blocked' in rejected_install.stderr
    hidden_install=docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','install',
        '--archive','/provision/server-v1.tar.gz','--role','controller','--prefix','/root/ascend-lab-hidden',
        '--state-dir',str(controller_state),'--create-service-user','labsvc','--port',str(port)],check=False)
    assert hidden_install.returncode==1 and 'controller_path_access' in hidden_install.stderr
    assert docker(['exec',controller,'/usr/bin/python3','-I','-c','from pathlib import Path;assert not Path("/root/ascend-lab-hidden").exists()']).returncode==0
    controller_plan=json.loads(docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','doctor',
        '--archive','/provision/server-v1.tar.gz','--role','controller','--prefix','/opt/ascend-lab-controller',
        '--state-dir',str(controller_state),'--create-service-user','labsvc','--port',str(port),*listener_args,*payload_args]).stdout)
    controller_after=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',plan_probe,'/opt/ascend-lab-controller',str(controller_state)]).stdout)
    assert controller_plan['install_ready'] and controller_plan['read_only'] and not controller_plan['production_ready']
    assert controller_before==controller_after=={'prefix_exists':False,'state_entries':[],'service_account_exists':False}
    assert controller_plan['release']['id']==v1['release_id']
    check('Read-only controller doctor validates the exact package without creating a prefix, state files or service account')
    installed=json.loads(docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','install',
        '--archive','/provision/server-v1.tar.gz','--role','controller','--prefix','/opt/ascend-lab-controller',
        '--state-dir',str(controller_state),'--create-service-user','labsvc','--port',str(port),*listener_args,*payload_args,'--start']).stdout)
    assert installed['uid']>0 and not installed['started']
    assert ctl(controller,'controller','verify')['verified']
    assert installed['service']['ready']
    if central_groups:central_initial_identity=actual_controller_identity()
    ctl(controller,'controller','stop')
    # Short grace is confined to this new, isolated acceptance controller.
    edit_grace='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["scheduler"]["storage_trash_grace_seconds"]=1;p.write_text(json.dumps(v));p.chmod(0o600)'
    docker(['exec',controller,'/usr/bin/python3','-I','-c',edit_grace,str(controller_state/'config.json')])
    if options.native_toolchain:
        native_budget='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["scheduler"]["memory_mib"]=4096;p.write_text(json.dumps(v));p.chmod(0o600)'
        docker(['exec',controller,'/usr/bin/python3','-I','-c',native_budget,str(controller_state/'config.json')])
    if options.dependencies:
        # Logical request admission only: no NPU contract/devices are enrolled
        # or mounted. The device branch must remain unallocated and unexecuted.
        enable_requests='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["scheduler"]["npu_enabled"]=True;p.write_text(json.dumps(v));p.chmod(0o600)'
        docker(['exec',controller,'/usr/bin/python3','-I','-c',enable_requests,str(controller_state/'config.json')])
    assert ctl(controller,'controller','start')['ready']
    code='import os,sys,json;sys.path.insert(0,"/opt/ascend-lab-controller/current");import storage_files as fs;from jobs import JobError;a=fs.directory("/mount-proof/payload");b=fs.directory("/mount-proof/payload/nested");same=os.fstat(a).st_dev==os.fstat(b).st_dev;different=fs.mount(a)!=fs.mount(b);os.close(a);os.close(b)\ntry:fs.scan("/mount-proof/payload")\nexcept JobError as e:assert "nested mounts" in str(e) and same and different;print(json.dumps({"same_device":same,"distinct_mount_ids":different,"nested_bind_rejected":True}))\nelse:raise AssertionError("Nested mount was traversed")'
    mount_proof=json.loads(docker(['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,'/usr/bin/python3','-I','-c',code]).stdout)
    assert (mount_other/'sentinel').read_bytes()==b'owned mount sentinel'
    check('Actual same-device nested bind mount is rejected by mount identity without changing its sentinel')
    if large_wheel:
        ctl(controller,'controller','stop')
        edit='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["scheduler"]["io_limits"]={"file_mib":2048,"input_mib":4096,"artifact_mib":4096};p.write_text(json.dumps(v));p.chmod(0o600)'
        docker(['exec',controller,'/usr/bin/python3','-I','-c',edit,str(controller_state/'config.json')])
        assert ctl(controller,'controller','start')['ready']
    check('One-command root installation creates dedicated non-root controller account and read-only versioned code')
    if options.central_payload:
        assert controller_plan['payload_storage']['empty'] and controller_plan['payload_storage']['control_filesystem_separate']
        topology='import os,json,sys;from pathlib import Path;s=Path(sys.argv[1]);p=Path("/srv/lab-payload");assert not any((s/n).exists() for n in ["jobs","inputs","storage-trash"]);print(json.dumps({"control_device":s.stat().st_dev,"payload_device":p.stat().st_dev,"marker":json.loads((p/".ascend-lab-payload.json").read_text())}))'
        layout=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',topology,str(controller_state)]).stdout)
        assert layout['control_device']!=layout['payload_device'] and layout['marker']['state_dir']==str(controller_state)
        central_proof={'separate_filesystems':True,'source_contract':payload_contracts[0],'destination_contract':payload_contracts[1]}
        check('Read-only doctor and one-command install bind an explicit separate finite payload filesystem; control database stays outside')
    if https:
        assert controller_plan['listener']['hostname_and_chain_verified'] and controller_plan['listener']['validation_network_connections']==0
        assert 'PRIVATE KEY' not in json.dumps(controller_plan)
        # The private CA is not installed globally; only explicit trust can connect.
        try:urllib.request.build_opener(urllib.request.ProxyHandler({})).open(outer_url+'/healthz',timeout=5)
        except urllib.error.URLError as error:assert isinstance(error.reason,ssl.SSLCertVerificationError)
        else:raise AssertionError('Private TLS authority unexpectedly trusted globally')
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=tls_context)).open(outer_url+'/healthz',timeout=5) as response:assert response.status==200
        check('Native HTTPS listener validates explicit private CA and hostname; isolated controller does not depend on host-loopback transport')
    client_target=str(controller_state/'published-client')
    docker(['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/opt/ascend-lab-controller/current/install-client','--prefix',client_target])
    published=secret_json(controller,Path(client_target)/'manifest.json')
    assert published['version']=='0.32-client' and set(published['sha256'])=={'labctl','lab_client.py','io_policy.py','client_archive.py','diagnostic_case.py','accuracy_data.py','accuracy_diagnostic.py','ACCURACY.md','simulation_data.py','simulation_diagnostic.py','SIMULATION.md','compiler_diagnostic.py','compiler_compatibility.py','COMPILER_DIAGNOSTICS.md','operator_profile.py','operator_node.py','operator_workflow.py','OPERATOR_ANALYSIS.md','mcp_bridge.py','CLIENT.md','WORKFLOWS.md','WORKSPACE.md','DEPENDENCIES.md','STORAGE.md','EXPERIMENT_BUDGET.md','SOURCE_GRANTS.md'}
    check('Installed public code supplies a standalone personal CLI/MCP installer without controller credentials or model configuration')
    ctl(controller,'controller','admin','--','accessctl','user-add','proof_user')
    ctl(controller,'controller','admin','--','accessctl','environment-grant','proof_user','worker-python')
    personal=secret_json(controller,controller_state/'personas/proof_user/client.json');token=personal['token']
    if options.native_toolchain or options.operator_analysis:ctl(controller,'controller','admin','--','accessctl','quota','proof_user','--memory-mib','2048' if options.native_toolchain else '768')
    if user_quota:
        ctl(controller,'controller','admin','--','accessctl','user-add','proof_peer')
        ctl(controller,'controller','admin','--','accessctl','environment-grant','proof_peer','worker-python')
        peer=secret_json(controller,controller_state/'personas/proof_peer/client.json')
        peer_headers={'Authorization':'Bearer '+peer['token']}
        if options.native_toolchain or options.operator_analysis:ctl(controller,'controller','admin','--','accessctl','quota','proof_peer','--memory-mib','2048' if options.native_toolchain else '768')
    owner_headers={'Authorization':'Basic '+base64.b64encode((personal['username']+':'+personal['password']).encode()).decode()}
    probe='import os,json,sys;p=sys.argv[1];s=os.stat(p);v=os.statvfs(p);print(json.dumps({"device":s.st_dev,"root_inode":s.st_ino,"bytes":v.f_blocks*v.f_frsize,"inodes":v.f_files}))'
    slots=[]
    if user_quota:
        limits={'users':[{'uid':23003,'bytes':(256 if options.simulation_case or options.report_view else 16)*1024**2,'inodes':2048 if options.native_toolchain else 1024 if options.operator_analysis else 128},{'uid':23004,'bytes':(320 if options.simulation_case or options.report_view else 24)*1024**2,'inodes':3072 if options.native_toolchain else 1024 if options.operator_analysis else 192}]}
        save='import sys;from pathlib import Path;p=Path("/provision/quota-limits.json");p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)'
        docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save],input=json.dumps(limits))
        prepare_args=['exec',node,'/usr/bin/python3','-B','/bootstrap/servicectl','workspace-prepare','--root',slot_path,
                      '--limits','/provision/quota-limits.json','--output','/provision/workspace-quota.json']
        preview=json.loads(docker(prepare_args+['--plan']).stdout);assert preview['read_only_plan'] and not preview['already_prepared']
        assert docker(['exec',node,'/usr/bin/python3','-I','-c','import sys;from pathlib import Path;assert not (Path(sys.argv[1])/".ascend-lab-userquota.json").exists()',slot_path]).returncode==0
        created=json.loads(docker(prepare_args).stdout);repeated=json.loads(docker(prepare_args).stdout)
        assert not created['already_prepared'] and repeated['already_prepared']
        slots=secret_json(node,'/provision/workspace-quota.json')['slots']
        check('Root UID-quota preparation is read-only in plan mode, idempotent when complete, and never formats or enables the filesystem')
    else:
        for i,(_,path) in enumerate(work_mounts):
            slot=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',probe,path]).stdout)
            slot.update(id='work'+str(i),storage_id=uuid.uuid4().hex,path=path,filesystem='ext4' if disk else 'tmpfs');slots.append(slot)
    if cache_disk:
        prepare_cache='import sys;from pathlib import Path;p=Path(sys.argv[1]);assert sorted(x.name for x in p.iterdir())==["lost+found"];p.chmod(0o700)'
        docker(['exec',node,'/usr/bin/python3','-I','-c',prepare_cache,cache_path])
        cache_args=['exec',node,'/usr/bin/python3','-B','/bootstrap/servicectl','workspace-prepare','--root',cache_path,
            '--limits','/provision/quota-limits.json','--output','/provision/cache-quota.json']
        if options.native_toolchain:
            native_limits={'users':[{'uid':23003,'bytes':32*1024**2,'inodes':2048},{'uid':23004,'bytes':40*1024**2,'inodes':3072}]}
            docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save,'/provision/quota-limits.json'],input=json.dumps(native_limits))
        assert json.loads(docker(cache_args+['--plan']).stdout)['read_only_plan']
        docker(cache_args);cache_policy=secret_json(node,'/provision/cache-quota.json')
    workspace_contract=controller_state/'workspace.json'
    save_contract='import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)'
    docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_contract,str(workspace_contract)],input=json.dumps({'slots':slots}))
    io_contract=controller_state/'io.json'
    spool_contract=controller_state/'spool.json'
    docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_contract,str(spool_contract)],input=json.dumps(spool_policy))
    docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_contract,str(io_contract)],
            input=json.dumps({'file_mib':2048,'input_mib':4096,'artifact_mib':4096} if large_wheel else {'file_mib':64,'input_mib':128,'artifact_mib':128}))
    cache_contract=controller_state/'cache.json'
    if cache_policy:
        docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_contract,str(cache_contract)],input=json.dumps(cache_policy))
    enrollment=ctl(controller,'controller','admin','--','workerctl','enroll','--name','installed-node',
        '--cpus',str(cpus[-1]),'--memory-mib','2176' if options.native_toolchain else '896' if options.operator_analysis else '1664' if large_wheel else '768','--image',task_image,'--user-map','proof_user:23003:23003',
        *(['--user-map','proof_peer:23004:23004'] if user_quota else []),
        '--io-config',str(io_contract),
        '--workspace-config',str(workspace_contract),
        '--spool-config',str(spool_contract),
        *(['--cache-config',str(cache_contract)] if cache_policy else []),
        '--physical-id',physical_identity(),'--controller-url',controller_url,'--node-state-dir',str(node_state))
    ctl(controller,'controller','admin','--','workerctl','node-templates',enrollment['node_id'],
        '--template','cpu_demo','--template','isolation_probe','--template','ir_v1','--template','environment_probe',
        *(['--template','build_v1'] if large_wheel or options.dependencies or options.native_toolchain or options.operator_analysis or options.browser_workflow or options.accuracy_case or options.terminal_handoff or options.source_grants or options.simulation_case or options.report_view else []),
        *(['--template','accuracy_v1'] if options.dependencies else []))
    configuration=secret_json(controller,Path(enrollment['config']))
    configuration['environment_setup']=task_setup
    configuration['python']=task_python
    if large_wheel:configuration['max_timeout_seconds']=720
    write='import os,sys;from pathlib import Path;p=Path("/provision/node.json");p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)'
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',write],input=json.dumps(configuration))
    if https:
        assert configuration['ca_file']=='controller-ca.pem'
        write_ca='import sys;from pathlib import Path;p=Path("/provision/controller-ca.pem");p.write_text(sys.stdin.read());p.chmod(0o600)'
        docker(['exec','-i',node,'/usr/bin/python3','-I','-c',write_ca],input=authority_text)
    node_before=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',plan_probe,'/opt/ascend-lab-node',str(node_state)]).stdout)
    doctor_args=['/usr/bin/python3','-B','/bootstrap/servicectl','doctor','--archive','/provision/server-v1.tar.gz',
        '--role','node','--prefix','/opt/ascend-lab-node','--state-dir',str(node_state),'--node-config','/provision/node.json']
    remote=docker(['exec','--env','DOCKER_HOST=tcp://127.0.0.1:1','--env','DOCKER_CONTEXT=',node,*doctor_args],check=False)
    assert remote.returncode==2
    denied_plan=json.loads(remote.stdout);assert not denied_plan['install_ready'] and 'docker_transport' in denied_plan['blockers']
    denied_install_args=[*doctor_args];denied_install_args[3]='install'
    denied_install=docker(['exec','--env','DOCKER_HOST=tcp://127.0.0.1:1','--env','DOCKER_CONTEXT=',node,*denied_install_args],check=False)
    assert denied_install.returncode==1 and 'preflight blocked' in denied_install.stderr
    node_plan=json.loads(docker(['exec',node,*doctor_args]).stdout)
    node_after=json.loads(docker(['exec',node,'/usr/bin/python3','-I','-c',plan_probe,'/opt/ascend-lab-node',str(node_state)]).stdout)
    assert node_plan['install_ready'] and node_plan['task_dispatch_prerequisites'] and node_plan['read_only']
    assert node_before==node_after and not node_before['prefix_exists'] and not node_before['state_entries']
    assert configuration['node_key'] not in json.dumps(node_plan)
    assert node_plan['acceptance']['npu_initialization']=='not_run'
    check('Read-only node doctor rejects remote Docker, checks the local image and bounded workspace, and redacts its credential')
    node_installed=json.loads(docker(['exec',node,'/usr/bin/python3','-B','/bootstrap/servicectl','install',
        '--archive','/provision/server-v1.tar.gz','--role','node','--prefix','/opt/ascend-lab-node',
        '--state-dir',str(node_state),'--node-config','/provision/node.json','--start']).stdout)
    assert node_installed['uid']==0 and node_installed['service']['ready']
    check('Independent privileged node installs enrolled config and acknowledges controller heartbeat')
    assert node_plan['spool_storage']['control_separate']
    check('Installed node input/artifact spool is a pinned finite ext4 filesystem separate from control state and task workspaces')
    assert not node_plan['versioned_workflows_ready']
    quota_status,_=api('/api/jobs',{'template':'environment_probe','environment':'worker-python',
        'parameters':{'node_id':enrollment['node_id'],'memory_mib':32768}})
    assert quota_status==400
    check('Explicit fixed-probe memory cannot bypass the ordinary personal resource quota')
    probe_parameters={'node_id':enrollment['node_id'],'memory_mib':256}
    if options.native_toolchain:
        package='/usr/local/python3.11.15/lib/python3.11/site-packages/triton'
        probe_parameters.update(packages=['triton-ascend','torch','torch-npu'],tools=['bishengir-opt','bishengir-compile','cmake','ninja','c++'],
            files=[package+'/backends/ascend/compiler.py',package+'/backends/ascend/driver.py',package+'/_C/triton-mlir-opt',
                package+'/backends/ascend/bishengir/bin/bishengir-opt',package+'/backends/ascend/bishengir/bin/bishengir-compile','/opt/ascend-lab-offline-compat.json'])
    if options.operator_analysis:
        from operator_workflow import probe_queries
        probe_parameters=probe_queries(enrollment['node_id'])
        if options.compiler_case or options.large_compiler_case:
            compiler_reference=json.loads(Path(options.compiler_case or options.large_compiler_case).read_text())
            assert compiler_reference['image']==task_image
            probe_parameters['files'] += [tool['path'] for tool in compiler_reference['tools'].values()]
        if options.report_view:
            report_reference=json.loads(Path(options.report_view).read_text());assert report_reference['image']==task_image
            probe_parameters['files'] += [tool['path'] for tool in report_reference['tools'].values()]
    status,response=api('/api/jobs',{'template':'environment_probe','environment':'worker-python','parameters':probe_parameters})
    assert status==201,response
    probe_job=wait(response['job']['id']);assert probe_job['state']=='succeeded',probe_job
    assert probe_job['resources']=={'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0}
    ctl(node,'node','stop')
    ctl(controller,'controller','admin','--','workerctl','node-environment',enrollment['node_id'],
        '--from-job',probe_job['id'],'--expected-revision','1')
    certified=secret_json(controller,Path(enrollment['config']))
    assert certified['python']==task_python
    configuration.update({key:certified[key] for key in ['image','python','environment_setup','environment_manifest','policy_revision']})
    # Preserve the installed local daemon binding and state directory.
    installed_config=secret_json(node,node_state/'config.json')
    installed_config.update({key:configuration[key] for key in ['image','python','environment_setup','environment_manifest','policy_revision']})
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save_contract,str(node_state/'config.json')],input=json.dumps(installed_config))
    ctl(node,'node','start')
    check('Fixed CPU probe certifies immutable environment; revised node acknowledges before versioned workflows')
    source={name:(ROOT/'examples/workflows'/name).read_bytes() for name in ['ir.py','input.mlir','workspace_probe.py']}
    if options.browser_workflow:source.update({name:(ROOT/'examples/workflows'/name).read_bytes() for name in ['build.py','kernel.c']})
    def upload_source(headers=None):
        status,response=api('/api/inputs',{'files':[{'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in source.items()]},extra_headers=headers)
        assert status in {200,201},response
        identity=response['input']['id']
        for name,data in source.items():assert api('/api/inputs/'+identity+'/blob',{'name':name,'offset':0,'data':base64.b64encode(data).decode()},extra_headers=headers)[0]==200
        assert api('/api/inputs/'+identity+'/seal',{},extra_headers=headers)[1]['input']['sealed']
        return identity
    input_id=upload_source()
    request={'template':'ir_v1','environment':'worker-python','parameters':{'input_id':input_id,'argv':['python','ir.py'],
             'lock':{'python':task_version,'image_sha256':task_image},'timeout_seconds':60},'idempotency_key':'installed-ir-before-backup'}
    if options.native_toolchain:request['parameters']['artifacts']={'file_mib':8,'total_mib':16}
    status,response=api('/api/jobs',request);assert status==201,response
    first=wait(response['job']['id']);assert first['state']=='succeeded',first
    first_control=node_state/'attempts'/first['attempt_id']/'control'
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        spool_state=secret_json(node,first_control/'state.json')
        if spool_state.get('artifact_gc',{}).get('state')=='purged':break
        time.sleep(.2)
    assert spool_state['artifact_gc']['state']=='purged' and spool_state['controller_acknowledged']
    node_spool=secret_json(node,first_control/'artifact-transfer.json')
    from node_storage import manifest_sha256
    assert spool_state['artifact_receipt']['manifest_sha256']==manifest_sha256(node_spool['manifest'])
    absent='from pathlib import Path;import sys;assert not (Path(sys.argv[1])/"artifacts").exists();assert not (Path(sys.argv[1])/"artifacts-retired").exists();assert not (Path(sys.argv[2])/"attempts"/sys.argv[3]).exists()'
    docker(['exec',node,'/usr/bin/python3','-I','-c',absent,str(first_control),spool_path,first['attempt_id']])
    check('Installed node removes only controller-confirmed artifact copies and retains the matching checksum receipt and transfer manifest')
    env=api('/api/jobs/'+first['id']+'/artifact?name=environment.json')[1]
    assert env['node_daemon_uid']==0 and env['task_uid']==23003
    assert env['node_spool']['enforced'] and env['node_spool']['bytes']==spool_policy['bytes']
    assert b'arith.constant 42' in api('/api/jobs/'+first['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    check('Installed services execute actual BishengIR transform as UID23003 and return protected evidence through HTTP')
    if options.experiment_budget:
        from installed_experiment_budget_check import run as budget_check
        experiment_budget_proof=budget_check(globals())
    if options.browser_workflow:
        browser_profile=base/'workflow-browser-client.json'
        atomic_json(browser_profile,{'controller':outer_url,'username':personal['username'],'password':personal['password']})
        browser_requests=[]
        for kind,script in [('ir','ir.py'),('build','build.py')]:
            browser_requests.append({'template':kind+'_v1','environment':'worker-python','name':'网页实际'+kind,
                'parameters':{'input_id':input_id,'argv':['python',script],
                    'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},
                    'lock':{'python':task_version,'image_sha256':task_image},'timeout_seconds':90},
                'idempotency_key':'installed-web-'+kind+'-stable'})
        browser_request_file=base/'workflow-browser-requests.json';atomic_json(browser_request_file,browser_requests)
        browser_output=base/'workflow-browser.json'
        browser=subprocess.run(['/bin/bash',str(ROOT/'check-browser.sh'),'tests/browser_installed_workflow_check.py',
            '--profile',str(browser_profile),'--requests',str(browser_request_file),'--output',str(browser_output)],
            cwd=ROOT,capture_output=True,text=True,timeout=240)
        (base/'workflow-browser.log').write_text(browser.stdout+browser.stderr)
        assert browser.returncode==0,'Inspect private installed workflow browser log and original tasks; no substitute submission'
        browser_workflow_proof=json.loads(browser_output.read_text());assert browser_workflow_proof['passed']
        for item in browser_workflow_proof['tasks']:
            assert len(api('/api/jobs/'+item['job_id'])[1]['attempts'])==1
            environment=api('/api/jobs/'+item['job_id']+'/artifact?name=environment.json')[1]
            assert environment['node_daemon_uid']==0 and environment['task_uid']==23003
        check('Packaged browser submits actual locked CPU IR/build as UID23003, replays each original job and downloads real IR/results CSV')
    if options.deadline_limit:
        sys.path.insert(0,str(ROOT/'tests'));from installed_deadline_check import run as deadline_check
        deadline_proof=deadline_check(globals())
        check('Default controller timeout rejects long work; explicit administrator increase keeps the same job queued without an attempt until a node confirms adequate deadline')
        check('Restarted node reports the higher bound and completes one actual locked IR workflow with the same long-deadline job')
    if cache_policy:
        sys.path.insert(0,str(ROOT/'tests'));from installed_cache_check import run as cache_check
        cache_proof=cache_check(globals())
        check('Installed private cache mounts the assigned owner namespace, reuses real IR across attempts, protects active leases and survives node restart/mount loss')
    if options.node_quota_resize:
        from installed_node_quota_check import run as node_quota_check
        node_quota_proof=node_quota_check(globals())
        check('Existing Node work/cache quotas grow and shrink through central approval, 13 real SIGKILLs, key rotation and fresh healthy heartbeat; cache inode/IR bytes and peer limits remain unchanged')
    if options.native_toolchain:
        sys.path.insert(0,str(ROOT/'tests'));from installed_native_toolchain_check import run as native_check
        native_proof=native_check(globals())
        check('Installed managed CPU workflow certifies actual Ascend backend/tool files and archives real Triton ELF/IR plus fresh CMake/ccache cold and warm results')
        check('Native source/environment/owner cache separation and original warm reuse survive node restart without another attempt')
    if options.operator_analysis:
        sys.path.insert(0,str(ROOT/'tests'));from installed_operator_check import run as operator_check
        operator_proof=operator_check(globals())
        check('Fixed-image offline analysis uses sealed input, actual non-root quota tasks, no network/NPU, authenticated report reader and independent personal UID')
    if options.compiler_case:
        from installed_compiler_check import run as compiler_check
        compiler_proof=compiler_check(globals())
        check('Paired actual compiler failures and a tools-denied known-evidence draft produce only one primary-approved literal candidate, verified through a new sealed CPU job')
    if options.large_compiler_case:
        from installed_large_compiler_check import run as large_compiler_check
        large_compiler_proof=large_compiler_check(globals())
        check('Two budget-scoped MCP conversations compile full sealed historical operator IR with pinned tools; bounded personal diagnosis verifies all source bytes without authorizing a preview patch')
    if options.accuracy_case:
        sys.path.insert(0,str(ROOT/'tests'));from installed_accuracy_check import run as accuracy_check
        accuracy_proof=accuracy_check(globals())
        check('Three budget-scoped MCP CPU replays preserve the actual historical precision failure, distinguish changed reference semantics and compare FP32/FP64 dot data')
    if options.terminal_handoff:
        sys.path.insert(0,str(ROOT/'tests'));from installed_terminal_handoff_check import run as terminal_handoff_check
        terminal_handoff_proof=terminal_handoff_check(globals())
        check('Explicit terminal artifact edges preserve the failed precision producer, cancel the ordinary successor, and resume frozen diagnostic input after controller restart')
        check('A controlled CPU wait reaches its deadline, confirms cleanup and transfers its original marker to one separate diagnostic task')
    if options.source_grants:
        sys.path.insert(0,str(ROOT/'tests'));from installed_source_grant_check import run as source_grant_check
        source_grant_proof=source_grant_check(globals())
        check('Personal UID approves exact files to a new budget; revoked partial handoff survives controller restart without copying or execution, and fresh approval preserves original failure')
    if options.simulation_case:
        sys.path.insert(0,str(ROOT/'tests'));from installed_simulation_check import run as simulation_check
        simulation_proof=simulation_check(globals())
        check('Installed budget-scoped CPU analysis verifies complete four-core historical simulator traces and CSV pipe mappings; independent personal CLI/MCP retains abnormal collection status')
    if options.report_view:
        sys.path.insert(0,str(ROOT/'tests'));from installed_report_view_check import run as report_view_check
        report_view_proof=report_view_check(globals())
        check('Packaged browser presents actual failed precision, full historical compiler error, abnormal simulator report and pinned profiling comparison through the authenticated bounded archive API')
    if options.dependencies:
        sys.path.insert(0,str(ROOT/'tests'));from installed_dependencies_check import run as dependency_check
        dependency_proof=dependency_check(globals())
        check('Installed workflow dependencies pin and seal producer bytes, resume partial handoff after controller restart and leave a device stage unallocated')
        check('Failed upstream cancels an unstarted descendant with explicit non-execution evidence in the result table')
    if options.node_spool_pressure:
        assert not large_wheel
        sys.path.insert(0,str(ROOT/'tests'));from node_spool_pressure import run as pressure_check
        node_spool_pressure=pressure_check(globals())
        check('Finite node spool ENOSPC preserves the original ended container, workspace and result; after exact filler removal the same single IR attempt resumes collection')
    if https:
        assert api('/api/jobs/'+first['id']+'/cancel',{},extra_headers={'Origin':'http://'+urllib.parse.urlsplit(outer_url).netloc})[0]==403
        assert api('/api/jobs/'+first['id']+'/cancel',{},extra_headers={'Origin':outer_url})[0]==200
        assert api('/api/jobs/'+first['id'])[1]['job']['state']=='succeeded'
        docker(['exec',controller,'openssl','req','-new','-newkey','rsa:2048','-nodes','-subj','/CN=controller.lab',
            '-keyout','/provision/tls/renewed.key','-out','/provision/tls/renewed.csr'])
        docker(['exec',controller,'openssl','x509','-req','-in','/provision/tls/renewed.csr','-CA','/provision/tls/ca.pem',
            '-CAkey','/provision/tls/ca.key','-CAcreateserial','-days','1','-extfile','/provision/tls/server.ext','-out','/provision/tls/renewed.pem'])
        rotate_args=['--tls-cert','/provision/tls/renewed.pem','--tls-key','/provision/tls/renewed.key','--tls-ca','/provision/tls/ca.pem']
        live=ctl(controller,'controller','tls-rotate',*rotate_args,check=False)
        assert live.returncode!=0 and ctl(controller,'controller','status')['ready']
        ctl(controller,'controller','stop')
        renewal=ctl(controller,'controller','tls-rotate',*rotate_args)
        assert renewal['rotated'] and not renewal['authority_changed'] and renewal['previous_tls_material_retained']
        assert renewal['certificate_sha256']!=controller_plan['listener']['certificate_sha256']
        assert ctl(controller,'controller','start')['ready']
        assert api('/api/me')[0]==200 and api('/api/jobs/'+first['id'])[1]['job']['state']=='succeeded'
        check('Same-origin HTTPS writes pass, plaintext-origin writes fail, and stopped-controller leaf renewal preserves existing CA/client/node trust and records')
    archive_profile=controller_state/'archive-client.json'
    save_profile='import sys,os;from pathlib import Path;p=Path(sys.argv[1]);p.write_bytes(sys.stdin.buffer.read());p.chmod(0o600)'
    docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_profile,str(archive_profile)],
           input=json.dumps({'controller':controller_url,'token':token,
                            **({'ca_file':'personas/proof_user/controller-ca.pem'} if https else {})}))
    archived=json.loads(docker(['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
           '/usr/bin/python3','-B',client_target+'/labctl','--profile',str(archive_profile),
           'archive',first['id'],'--archive-root',str(controller_state/'personal-results')]).stdout)
    assert archived['complete'] and archived['job_state']=='succeeded' and archived['files_verified']>0
    check('Installed personal client downloads and verifies terminal IR archive as non-root UID through the authenticated API')
    if https:
        personal_home='/home/https-client'
        setup='import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642);v=json.loads(sys.stdin.read());[(lambda f,b:(f.write_text(b),f.chmod(0o600),os.chown(f,23642,23642)))(p/n,b) for n,b in v.items()]'
        handoff={'client.json':json.dumps({'controller':controller_url,'token':token,'ca_file':'controller-ca.pem'}),
                 'controller-ca.pem':authority_text,'workflow.json':json.dumps(request)}
        docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',setup,personal_home],input=json.dumps(handoff))
        independent='/opt/ascend-lab-personal-client'
        docker(['exec',controller,'/opt/ascend-lab-controller/current/install-client','--prefix',independent])
        helper=(ROOT/'tests/https_mcp_helper.py').read_text()
        https_mcp=json.loads(docker(['exec','--user','23642:23642',controller,'/usr/bin/python3','-I','-c',helper,
                                    independent,personal_home,str(controller_state)],timeout=120).stdout)
        assert https_mcp['passed'] and https_mcp['client_uid']==23642 and https_mcp['controller_private_state_unreadable']
        assert len(api('/api/jobs/'+https_mcp['job_id'])[1]['attempts'])==1
        check('Independent UID23642 runs installed MCP over verified HTTPS, cannot read controller state/socket and reconnects one actual IR job')
        assert https_mcp['diagnostic_case_readonly']
        check('Independent UID23642 reads a bound diagnostic case over HTTPS; submission and job-ID overrides are rejected')
        if native_proof:
            from installed_native_toolchain_check import mcp
            native_proof['mcp']=mcp(globals(),native_proof)
            check('Independent UID23642 submits and reconnects the fixed native workflow through installed HTTPS MCP and verifies actual warm compiler caches')
    retired_id=https_mcp['job_id'] if https_mcp else first['id']
    artifact_path='/api/jobs/'+retired_id+'/artifact?name=ir%2Fcanonical.mlir'
    status,original_ir=api(artifact_path,raw=True);assert status==200 and b'arith.constant 42' in original_ir
    status,preview=api('/api/storage/plan',{'kind':'archive','id':retired_id,'action':'trash'});assert status==200
    assert not preview['can_apply'] and api('/api/storage/apply',preview['plan'])[0]==403
    charge_before=api('/api/me')[1]['storage']['personal_bytes']['archive']
    _,trashed=storage('archive',retired_id,'trash',owner_headers)
    assert trashed['object']['state']=='trash' and not trashed['quota_released']
    assert api('/api/me')[1]['storage']['personal_bytes']['archive']>=charge_before and api(artifact_path,raw=True)[0]==410
    status,purge_preview=api('/api/storage/plan',{'kind':'archive','id':retired_id,'action':'purge'},extra_headers=owner_headers)
    assert status==200 and not purge_preview['eligible']
    assert api('/api/storage/apply',purge_preview['plan'],extra_headers=owner_headers)[0]==409
    owner_profile=controller_state/'owner-storage-client.json'
    owner_plan=controller_state/'owner-storage-plan.json'
    docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_profile,str(owner_profile)],
           input=json.dumps({**{key:value for key,value in personal.items() if key!='token'},
                             **({'ca_file':'personas/proof_user/controller-ca.pem'} if https else {})}))
    cli=['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
         '/usr/bin/python3','-B',client_target+'/labctl','--profile',str(owner_profile),'storage']
    cli_preview=json.loads(docker([*cli,'plan','archive',retired_id,'restore']).stdout)
    docker(['exec','-i','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-I','-c',save_profile,str(owner_plan)],input=json.dumps(cli_preview))
    restored_payload=json.loads(docker([*cli,'apply','--plan',str(owner_plan)]).stdout)
    assert restored_payload['object']['state']=='available' and api(artifact_path,raw=True)[1]==original_ir
    _,trashed=storage('archive',retired_id,'trash',owner_headers);time.sleep(1.1)
    _,purged=storage('archive',retired_id,'purge',owner_headers)
    assert purged['object']['state']=='purged' and purged['quota_released'] and api(artifact_path,raw=True)[0]==410
    detail=api('/api/jobs/'+retired_id)[1]
    assert detail['job']['state']=='succeeded' and detail['retention_metadata']['environment']['locked']
    assert detail['retention_metadata']['content_hashes'] and detail['artifacts']
    rows=api('/api/results?limit=200')[1]['rows'];row=next(x for x in rows if x['job_id']==retired_id)
    assert row['archive_state']=='purged' and row['passed']
    check('Owner-only archive trash/restore/purge preserves actual IR hashes, grace, result rows and protected evidence; original task token cannot delete')
    payload=b'owned central retention input';manifest_body={'files':[{'name':'payload.bin','size':1024*1024,'sha256':hashlib.sha256(payload).hexdigest()}]}
    status,response=api('/api/inputs',manifest_body);assert status==201,response
    retained_input=response['input']['id']
    assert api('/api/inputs/'+retained_input+'/blob',{'name':'payload.bin','offset':0,'data':base64.b64encode(payload).decode()})[0]==200
    _,trash_input=storage('input',retained_input,'trash',owner_headers);time.sleep(1.1)
    _,purged_input=storage('input',retained_input,'purge',owner_headers)
    assert purged_input['quota_released'] and purged_input['released_reserved_bytes']>512*1024
    status,response=api('/api/inputs',manifest_body);assert status==200,response
    assert response['input']['id']==retained_input and not response['input']['sealed'] and not response['input']['uploaded']
    assert api('/api/inputs/'+retained_input+'/blob',{'name':'payload.bin','offset':0,'data':base64.b64encode(payload).decode()})[0]==200
    _,trash_input=storage('input',retained_input,'trash',owner_headers)
    input_old_revision=trash_input['object']['revision']
    check('Purged partial input releases its declared reservation; same manifest revives with fresh upload progress and can be quarantined for backup')
    retention_proof={'archive_job':retired_id,'archive_sha256':hashlib.sha256(original_ir).hexdigest(),
                     'archive_purged':True,'result_row_retained':True,'original_task_token_delete_denied':True,
                     'input_id':retained_input,'input_reupload_fresh':True,'fixture_grace_seconds':1}
    too_large_mib=max(64,max(slot['bytes'] for slot in slots)//(1024**2)+1)
    negative_requirements=[('too-large',{'min_mib':too_large_mib,'min_inodes':64,'durable':False})]
    if not disk:negative_requirements.append(('persistent-required',{'min_mib':1,'min_inodes':1,'durable':True}))
    for name,requirements in negative_requirements:
        blocked={**request,'parameters':{**request['parameters'],'workspace':requirements},
                 'idempotency_key':'installed-workspace-'+name}
        status,response=api('/api/jobs',blocked);assert status==201,response
        blocked_id=response['job']['id'];time.sleep(1)
        waiting=api('/api/jobs/'+blocked_id)[1]['job'];assert waiting['state']=='queued' and not waiting['allocation'].get('workspace'),waiting
        assert api('/api/jobs/'+blocked_id+'/cancel',{})[1]['job']['state']=='cancelled'
    check('Oversized workspace remains queued with no ordinary-directory fallback' if disk else 'Actual capacity and persistent-storage requirements remain queued on the small volatile node; no ordinary-directory fallback')
    if disk:
        persistent={**request,'parameters':{**request['parameters'],'workspace':{'min_mib':16,'min_inodes':64,'durable':True}},
                    'idempotency_key':'installed-persistent-ext4-workflow'}
        status,response=api('/api/jobs',persistent);assert status==201,response
        persistent_job=wait(response['job']['id']);assert persistent_job['state']=='succeeded',persistent_job
        persistent_env=api('/api/jobs/'+persistent_job['id']+'/artifact?name=environment.json')[1]
        assert persistent_env['workspace']['allocation']['durable']
        assert persistent_env['workspace']['allocation']['storage_id']==slots[0]['storage_id']
        assert persistent_env['workspace']['verified_identity']['device']==slots[0]['device']
        for key in ['bytes','inodes']:
            assert persistent_env['workspace']['verified_identity'][key]==slots[0]['filesystem_'+key if user_quota else key]
        if user_quota:
            attested=persistent_env['workspace']['user_quota']
            assert attested['uid']==23003 and attested['enforcement']['enforced'] and attested['byte_hard']==slots[0]['bytes']
        mount_type='import sys;from pathlib import Path;rows=[line.split(" - ") for line in Path("/proc/self/mountinfo").read_text().splitlines()];found=[b.split()[0] for a,b in rows if a.split()[4]==sys.argv[1]];assert found==["ext4"];print(found[0])'
        assert docker(['exec',node,'/usr/bin/python3','-I','-c',mount_type,slot_path]).stdout.strip()=='ext4'
        owned_disk.report['actual_mount_type']='ext4'
        owned_disk.report['persistent_task_identity_matches_slot']=True
        check('Persistent-workspace request actually executes on the pinned ext4 filesystem as a non-root task')
    limited={**request,'parameters':{**request['parameters'],'argv':['python','workspace_probe.py',*(['--quota-bytes',str(slots[0]['bytes']),'--quota-inodes',str(slots[0]['inodes'])] if user_quota else [])],
             'workspace':{'min_mib':16,'min_inodes':64,'durable':False}},'idempotency_key':'installed-workspace-byte-inode-limits'}
    status,response=api('/api/jobs',limited);assert status==201,response
    limited_job=wait(response['job']['id']);assert limited_job['state']=='succeeded',limited_job
    quota=api('/api/jobs/'+limited_job['id']+'/artifact?name=ir%2Fworkspace-probe.json')[1]
    assert quota['passed'] and quota['uid']==23003 and quota['byte_errno']==(122 if user_quota else 28) and quota['inode_errno']==(122 if user_quota else 28)
    assert quota['filesystem_byte_limit']==slots[0]['filesystem_bytes' if user_quota else 'bytes'] and quota['filesystem_inode_limit']==slots[0]['filesystem_inodes' if user_quota else 'inodes']
    if user_quota:assert quota['quota_byte_hard']==slots[0]['bytes'] and quota['quota_inode_hard']==slots[0]['inodes']
    assert limited_job['result']['execution_cleanup_confirmed']
    assert limited_job['result']['workspace_release_confirmed']
    check('Root node enforces the assigned UID byte/inode quotas on a shared filesystem; EDQUOT and cleanup are confirmed' if user_quota else 'Root node allocates a dedicated finite filesystem; non-root work/cache hits real byte and inode ENOSPC and releases only after cleanup')
    if user_quota:
        peer_request={**request,'parameters':{**request['parameters'],'input_id':upload_source(peer_headers)},
                      'idempotency_key':'installed-peer-own-uid-quota'}
        status,response=api('/api/jobs',peer_request,extra_headers=peer_headers);assert status==201,response
        peer_job=wait(response['job']['id'],headers=peer_headers);assert peer_job['state']=='succeeded',peer_job
        peer_env=api('/api/jobs/'+peer_job['id']+'/artifact?name=environment.json',extra_headers=peer_headers)[1]
        assert peer_env['task_uid']==23004 and peer_env['workspace']['allocation']['slot_id']==slots[1]['id']
        attested=peer_env['workspace']['user_quota']
        assert attested['uid']==23004 and attested['enforcement']['enforced']
        assert attested['byte_hard']==slots[1]['bytes'] and attested['inode_hard']==slots[1]['inodes']
        assert b'arith.constant 42' in api('/api/jobs/'+peer_job['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True,extra_headers=peer_headers)[1]
        assert api('/api/jobs/'+peer_job['id'])[0]==404
        assert api('/api/jobs/'+first['id'],extra_headers=peer_headers)[0]==404
        assert peer_job['result']['execution_cleanup_confirmed'] and peer_job['result']['workspace_release_confirmed']
        check('Second authenticated owner executes actual IR as UID23004 on its own quota; peer job records remain private in both directions')
    if central_groups:
        # Independent service identities and real HTTP transfer recovery under
        # one owner's native hard limits, with the other owner still executing.
        helper=(ROOT/'tests/central_quota_pressure.py').read_text()
        def inject(operation,inventory=None):
            result=docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',helper,
                '/opt/ascend-lab-controller/current',json.dumps(payload_contracts[0]),str(installed['uid']),
                str(installed['gid']),first['id'],operation],input=json.dumps(inventory) if inventory else None)
            return json.loads(result.stdout)
        for mode in ['bytes','inodes']:
            content=b'bounded-owner-quota-'*(1024**2//20);content=content[:512*1024]
            name=mode+'-payload.bin'
            status,response=api('/api/inputs',{'files':[{'name':name,'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}]});assert status==201,response
            identity=response['input']['id'];offset=256*1024 if mode=='bytes' else 0
            if offset:assert api('/api/inputs/'+identity+'/blob',{'name':name,'offset':0,'data':base64.b64encode(content[:offset]).decode()})[0]==200
            inventory=inject(mode)
            assert inventory['errno']==122 and inventory['filesystem_free_bytes']>64*1024**2 and inventory['filesystem_free_inodes']>1024
            chunk={'name':name,'offset':offset,'data':base64.b64encode(content[offset:offset+256*1024]).decode()}
            status,full=api('/api/inputs/'+identity+'/blob',chunk);assert status==507 and full['retryable'],full
            status,progress=api('/api/inputs/'+identity);assert status==200,progress
            assert progress['uploaded'].get(name,0)==offset,progress
            peer_during={**peer_request,'idempotency_key':'peer-while-owner-'+mode+'-full'}
            status,response=api('/api/jobs',peer_during,extra_headers=peer_headers);assert status==201,response
            continuing=wait(response['job']['id'],headers=peer_headers);assert continuing['state']=='succeeded',continuing
            assert api('/api/jobs/'+continuing['id'])[0]==404
            assert inject('clean',inventory)['cleaned']
            while offset<len(content):
                chunk={'name':name,'offset':offset,'data':base64.b64encode(content[offset:offset+256*1024]).decode()}
                assert api('/api/inputs/'+identity+'/blob',chunk)[0]==200;offset+=min(256*1024,len(content)-offset)
            assert api('/api/inputs/'+identity+'/seal',{})[1]['input']['sealed']
            group_pressure[mode]={key:value for key,value in inventory.items() if key not in {'files','directory_identity'}}
            group_pressure[mode].update(http_status=507,original_upload_resumed=True,peer_job_id=continuing['id'],peer_succeeded_while_owner_full=True)
        me=api('/api/me')[1]['storage']['filesystem_quota'];assert me['enforced'] and 'gid' not in me
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/owner-quota-ui.json',
            {'source_parent':Path(os.environ['LAB_JOB_DIR']).name,'scope':'Archived actual proof_user storage snapshot, not live Main storage',
             'storage':api('/api/jobs')[1]['storage']})
        check('Native owner byte/inode EDQUOT yields HTTP507 with unchanged offsets; peer actual IR runs while full, original uploads resume after exact filler cleanup')
    if options.input_durability:
        sys.path.insert(0,str(ROOT/'tests'));from installed_input_durability_check import run as input_check
        input_durability=input_check(globals())
        check('Acknowledged nested input bytes and offset survive SIGKILL of the installed non-root controller; identical retry reuses the committed prefix')
        check('Acknowledged input reset survives a second SIGKILL without losing other offsets; resumed sealed input executes one actual IR attempt')
    if options.payload_quota_resize:
        sys.path.insert(0,str(ROOT/'tests'));from installed_quota_resize_check import run as quota_resize_check
        quota_resize_proof=quota_resize_check(globals())
        check('Offline existing-owner quota plans preserve data and peer limits, reject unsafe shrink and stale plans, and apply idempotently')
        check('SIGKILL after native quota change and after config publication blocks normal start and backup; the original plans resume to consistent bindings')
        check('Actual owner and peer IR workflows complete after quota expansion and contraction, preserving the original archived IR')
    if options.central_payload:
        ctl(node,'node','stop')
        docker(['exec',controller,'umount','/srv/lab-payload'])
        blocked={"template":"cpu_demo","environment":"worker-python","parameters":{"seconds":1},"idempotency_key":"missing-payload-no-fallback"}
        status,missing=api('/api/jobs',blocked);assert status==503,missing
        assert docker(['exec',controller,'/usr/bin/python3','-I','-c','from pathlib import Path;assert not list(Path("/srv/lab-payload").iterdir())']).returncode==0
        assert not ctl(controller,'controller','stop')['running']
        docker(['exec',controller,'mount','--bind','/srv/lab-payload-alias','/srv/lab-payload'])
        assert ctl(controller,'controller','start')['ready'];assert ctl(node,'node','start')['ready']
        central_proof['mount_loss_503_no_fallback']=True;central_proof['stop_works_without_payload']=True
        check('Lost payload mount rejects new writes without fallback, while service stop remains available; rebind resumes the same data')
    denied=ctl(controller,'controller','backup','--output','/root/backups/online.tar.gz',check=False)
    assert denied.returncode!=0 and ctl(controller,'controller','status')['ready']
    check('Live state lock rejects backup without stopping or duplicating the running service')
    assert not ctl(controller,'controller','stop')['running']
    backup=ctl(controller,'controller','backup','--output','/root/backups/controller.tar.gz')
    assert backup['quiescent']
    if options.central_payload:
        assert backup['includes_dedicated_payload'];central_proof['backup_includes_payload']=True
        captured=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',
            'import tarfile,json;f=tarfile.open("/root/backups/controller.tar.gz");m=json.load(f.extractfile("snapshot.json"));print(json.dumps({"format":m["format"],"payload":bool(m.get("payload_storage")),"archive_files":sum(n.startswith("jobs/") for n in m["files"]),"trash_files":sum(n.startswith("storage-trash/") for n in m["files"])}))']).stdout)
        assert captured['format']=='ascend-lab-snapshot/v2' and captured['payload'] and captured['archive_files'] and captured['trash_files']
        central_proof['snapshot_manifest']=captured
    switched=ctl(controller,'controller','activate','--archive','/provision/server-v2.tar.gz')
    assert switched['release_id']==v2['release_id'] and switched['state_preserved']
    assert ctl(controller,'controller','start')['ready']
    assert api('/api/jobs/'+first['id'])[1]['job']['state']=='succeeded'
    check('Quiescent SQLite/file backup and compatible version activation preserve accounts, tokens, job and IR')
    ctl(controller,'controller','stop')
    ctl(node,'node','stop')
    restored_state=base/'controller-restored'
    restored=ctl(controller,'controller','restore','--archive','/root/backups/controller.tar.gz','--new-state-dir',str(restored_state),
        *(['--payload-config','/provision/payload1.json'] if options.central_payload else []))
    assert restored['previous_state_retained']==str(controller_state)
    assert ctl(controller,'controller','start')['ready']
    assert api('/api/me')[1]['workspace'].startswith(str(restored_state))
    if central_groups:central_restored_identity=actual_controller_identity()
    status,old_job=api('/api/jobs/'+first['id']);assert status==200,old_job
    assert old_job['job']['archive_dir'].startswith(payload_contracts[1]['path'] if options.central_payload else str(restored_state))
    status,rebound=api('/api/storage/plan',{'kind':'input','id':retained_input,'action':'restore'},extra_headers=owner_headers)
    assert status==200 and rebound['object']['revision']>input_old_revision,rebound
    status,result=api('/api/storage/apply',rebound['plan'],extra_headers=owner_headers);assert status==200 and result['object']['state']=='available',result
    restored_bytes=docker(['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,'/usr/bin/python3','-I','-c',
                          'from pathlib import Path;import hashlib,sys;print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())',
                          str((Path(payload_contracts[1]['path']) if options.central_payload else restored_state)/'inputs'/retained_input/'payload.bin')]).stdout.strip()
    assert restored_bytes==hashlib.sha256(payload).hexdigest()
    retention_proof['quarantined_backup_restore_rebound']=True
    check('Quarantined partial input survives offline backup and new-directory restore; fresh identity/revision permits exact owner restore')
    if options.central_payload:
        assert restored['dedicated_payload_restored'] and b'arith.constant 42' in api('/api/jobs/'+first['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
        central_proof['restored_to_new_filesystem']=True;central_proof['quarantined_input_rebound']=True;central_proof['old_archive_preserved']=True
        check('Versioned snapshot restores payload and quarantined data to a new filesystem and relocates archive paths without overwriting the source')
        if central_groups:
            verify_groups='import os,json,sys;from pathlib import Path;p=Path(sys.argv[1]);q=json.loads(sys.argv[2]);items={"proof_user":p/"jobs"/sys.argv[3],"proof_peer":p/"jobs"/sys.argv[4]};result={}\nfor owner,path in items.items():\n s=path.stat();assert s.st_gid==q[owner]["gid"] and s.st_mode&0o7777==0o2700;result[owner]={"gid":s.st_gid,"sgid":True}\nprint(json.dumps(result))'
            rebound_groups=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',verify_groups,
                payload_contracts[1]['path'],json.dumps(payload_contracts[1]['owner_groups']),first['id'],peer_job['id']]).stdout)
            assert api('/api/me')[1]['storage']['filesystem_quota']['enforced']
            check('Restored owner data retains SGID inheritance and hard quotas under newly assigned storage GIDs; original tokens still work')
    request['idempotency_key']='installed-ir-after-restore'
    status,response=api('/api/jobs',request);assert status==201,response
    waiting_id=response['job']['id']
    time.sleep(1)
    waiting=api('/api/jobs/'+waiting_id)[1]
    assert waiting['job']['state']=='queued' and not waiting['attempts']
    domains=api('/api/jobs')[1]['resource_domains']
    assert any(domain['admission']['blocked'] and domain['admission']['inventory_required'] for domain in domains)
    assert ctl(node,'node','start')['ready']
    check('Restored controller blocks new work until its enrolled node confirms inventory to the new instance; original queued job is retained')
    second=wait(response['job']['id']);assert second['state']=='succeeded',second
    check('Restore to a new private directory relocates workspace/archive and accepts a new real workflow')
    ctl(controller,'controller','stop')
    rollback=ctl(controller,'controller','activate','--release-id',v1['release_id'])
    assert rollback['release_id']==v1['release_id'];assert ctl(controller,'controller','start')['ready']
    assert api('/api/jobs/'+second['id'])[1]['job']['state']=='succeeded'
    check('Compatible code rollback retains jobs created after restore')
    proof='import os,json;from pathlib import Path;print(json.dumps({"uid":os.getuid(),"socket":Path("/var/run/docker.sock").exists(),"code_writable":os.access("/opt/ascend-lab-controller/current/server.py",os.W_OK)}))'
    role=json.loads(docker(['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,'/usr/bin/python3','-I','-c',proof]).stdout)
    assert not role['socket'] and not role['code_writable']
    ctl(node,'node','stop');ctl(controller,'controller','stop')
    node_backup=ctl(node,'node','backup','--output','/root/backups/node.tar.gz');assert node_backup['quiescent']
    check('Node stops only its daemon; all attempts acknowledged/cleaned before private node backup')
    save='import sys;from pathlib import Path;p=Path("/provision/spool.json");p.write_text(sys.stdin.read());p.chmod(0o600)'
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save],input=json.dumps(spool_policy))
    node_restored=ctl(node,'node','restore','--archive','/root/backups/node.tar.gz','--new-state-dir',str(node_parent/'restored'),
        '--spool-config','/provision/spool.json',*(['--cache-config','/provision/cache-quota.json'] if cache_policy else []))
    assert node_restored['previous_state_retained']==str(node_state)
    assert ctl(controller,'controller','start')['ready'] and ctl(node,'node','start')['ready']
    restored_request={**request,'idempotency_key':'actual-ir-after-node-state-restore'}
    status,response=api('/api/jobs',restored_request);assert status==201,response
    after_node_restore=wait(response['job']['id']);assert after_node_restore['state']=='succeeded',after_node_restore
    if cache_policy:
        from installed_cache_check import after_restore
        cache_proof['restored_cache_job']=after_restore(globals(),cache_proof)
        check('Node control restore rebinds the same private cache without rolling back its data; actual warm IR and cache cleanup still work')
    if node_quota_proof:
        from installed_node_quota_check import after_restore as node_quota_after_restore
        node_quota_proof['after_restore']=node_quota_after_restore(globals(),node_quota_proof)
        check('Controller/Node restore preserves quota change history, actual native limits and original attempts; the same owner cache remains warm')
    if native_proof:
        from installed_native_toolchain_check import after_restore as native_after_restore
        native_proof['after_restore']=native_after_restore(globals(),native_proof)
        check('Restored controller and node control preserve the certified native environment and original compiler cache; one actual warm native workflow completes')
    if operator_proof:
        from installed_operator_check import after_restore as operator_after_restore
        operator_proof['after_restore']=operator_after_restore(globals(),operator_proof)
        check('Restored controller and node retain the original analysis archive and sealed input; one new fixed-image report attempt succeeds')
    if compiler_proof:
        from installed_compiler_check import after_restore as compiler_after_restore
        compiler_proof['after_restore']=compiler_after_restore(globals(),compiler_proof)
        check('Restored state retains the original compiler failure and verifies the same sealed candidate once without another model call')
    if large_compiler_proof:
        from installed_large_compiler_check import after_restore as large_compiler_after_restore
        large_compiler_proof['after_restore']=large_compiler_after_restore(globals(),large_compiler_proof)
        check('Controller/Node restore preserves the complete historical IR, original failed attempts and budget charges; old delegated spending authority is revoked')
    if accuracy_proof:
        from installed_accuracy_check import after_restore as accuracy_after_restore
        accuracy_proof['after_restore']=accuracy_after_restore(globals(),accuracy_proof)
        check('Restored state retains original numerical comparisons and three single attempts, freezes budget authority and permits read-only history diagnosis')
    if terminal_handoff_proof:
        from installed_terminal_handoff_check import after_restore as terminal_after_restore
        terminal_handoff_proof['after_restore']=terminal_after_restore(globals(),terminal_handoff_proof)
        check('Restore preserves failed and timed-out artifact lineage, revokes old spending credentials and rejects an actual historical controller without terminal edge support')
    if source_grant_proof:
        from installed_source_grant_check import after_restore as source_grant_after_restore
        source_grant_proof['after_restore']=source_grant_after_restore(globals(),source_grant_proof)
        check('Restore freezes old grants and tokens, rejects the actual prior release, and a newly approved budget imports the original source without rerunning it')
    if simulation_proof:
        from installed_simulation_check import after_restore as simulation_after_restore
        simulation_proof['after_restore']=simulation_after_restore(globals(),simulation_proof)
        check('Restore preserves original four-core trace bytes, reports, single attempts and budget charges; old spending credential is revoked')
    if report_view_proof:
        from installed_report_view_check import after_restore as report_view_after_restore
        report_view_proof['after_restore']=report_view_after_restore(globals(),report_view_proof)
        check('Restore preserves original diagnostic report hashes, failed states and attempts; archived report views remain read-only and old budget spending token is revoked')
    if experiment_budget_proof:
        from installed_experiment_budget_check import after_restore as budget_after_restore
        budget_after_restore(globals(),experiment_budget_proof)
    if browser_workflow_proof:
        for item in browser_workflow_proof['tasks']:
            detail=api('/api/jobs/'+item['job_id'])[1]
            assert detail['job']['state']=='succeeded' and detail['job']['attempt_id']==item['attempt_id'] and len(detail['attempts'])==1
        ir_task=next(item for item in browser_workflow_proof['tasks'] if item['template']=='ir_v1')
        restored_ir=api('/api/jobs/'+ir_task['job_id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
        assert restored_ir==browser_output.with_suffix('.mlir').read_bytes()
        browser_workflow_proof['restored_tasks_and_ir_preserved']=True
        check('Controller/Node restore preserves both browser task identities, single attempts and exact downloaded IR bytes')
    ctl(node,'node','stop');ctl(controller,'controller','stop')
    check('Quiescent node state restores to a new control directory, rebinds the same empty spool and executes another actual IR task')
    if options.dependencies or options.browser_workflow or options.experiment_budget or options.large_compiler_case or options.accuracy_case or options.terminal_handoff or options.source_grants or options.node_quota_resize or options.simulation_case or options.report_view:
        # Only the original controller state leaf is host-bound. Preserve an
        # explicit readonly SQLite export of the restored private directory
        # before its owned container is removed; never pretend it was mounted.
        export='import os,sqlite3,sys;from pathlib import Path;p=Path(sys.argv[2]);assert not p.exists();src=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True);dst=sqlite3.connect(p);src.backup(dst);assert dst.execute("PRAGMA integrity_check").fetchone()[0]=="ok";dst.close();src.close();p.chmod(0o600);fd=os.open(p,os.O_RDONLY);os.fsync(fd);os.close(fd)'
        docker(['exec',controller,'/usr/bin/python3','-I','-c',export,str(restored_state/'jobs.sqlite3'),str(controller_state/'restored-proof.sqlite3')])
    if options.native_control:
        from installed_control_filesystem_check import run as native_control_run
        native_control_proof=native_control_run(globals())
    large_result=None
    if options.storage_pressure:
        sys.path.insert(0,str(ROOT/'tests'))
        from central_storage_pressure import run
        storage_pressure=run(globals())
    if options.control_storage_pressure:
        sys.path.insert(0,str(ROOT/'tests'))
        from control_storage_pressure import run
        control_storage_pressure=run(globals())
    if large_wheel:
        # Small-state recovery was already verified. Do not claim these backups include the new large wheel.
        assert ctl(controller,'controller','start')['ready'];assert ctl(node,'node','start')['ready']
        ctl(controller,'controller','admin','--','accessctl','quota','proof_user','--memory-mib','1536',
            '--input-storage-mib','2048','--archive-storage-mib','4096')
        script=(ROOT/'examples/workflows/wheel_archive_probe.py').read_bytes();wheel_hash=hashlib.sha256()
        with large_wheel.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):wheel_hash.update(block)
        wheel_size=large_wheel.stat().st_size;assert 1024**3<wheel_size<1280*1024**2
        files=[{'name':'package.whl','size':wheel_size,'sha256':wheel_hash.hexdigest()},
               {'name':'wheel_archive_probe.py','size':len(script),'sha256':hashlib.sha256(script).hexdigest()}]
        status,response=api('/api/inputs',{'files':files});assert status==201,response
        large_input=response['input']['id']
        print('Uploading existing real wheel through bounded HTTP chunks:',wheel_size,flush=True)
        with large_wheel.open('rb') as stream:
            offset=0;announced=0
            while True:
                block=stream.read(256*1024)
                if not block:break
                status,response=api('/api/inputs/'+large_input+'/blob',{'name':'package.whl','offset':offset,'data':base64.b64encode(block).decode()})
                assert status==200,response;offset+=len(block)
                if offset-announced>=256*1024**2:print('Wheel input bytes:',offset,flush=True);announced=offset
        assert api('/api/inputs/'+large_input+'/blob',{'name':'wheel_archive_probe.py','offset':0,'data':base64.b64encode(script).decode()})[0]==200
        assert api('/api/inputs/'+large_input+'/seal',{})[1]['input']['sealed']
        large_request={'template':'build_v1','environment':'worker-python','parameters':{
            'input_id':large_input,'argv':['python','wheel_archive_probe.py','/inputs/package.whl'],
            'input_mode':'readonly',
            'resources':{'cpu':1,'memory_mib':1536,'shm_mib':0,'npu':0},
            'lock':{'python':'3.10.12','image_sha256':image},'timeout_seconds':600,
            'workspace':{'min_mib':1300,'min_inodes':64,'durable':False},
            'artifacts':{'file_mib':1280,'total_mib':1280}},'idempotency_key':'real-large-wheel-roundtrip'}
        status,response=api('/api/jobs',large_request);assert status==201,response
        large_id=response['job']['id'];print('Large wheel job:',large_id,flush=True)
        large_job=wait(large_id,600);assert large_job['state']=='succeeded',large_job
        assertion=api('/api/jobs/'+large_id+'/artifact?name=results%2Fworkload-result.json')[1]
        assert assertion['bytes']==wheel_size and assertion['sha256']==wheel_hash.hexdigest() and assertion['wheel_entries']>100
        print('Downloading large wheel with the installed non-root personal client',flush=True)
        downloaded=json.loads(docker(['exec','--user',str(installed['uid'])+':'+str(installed['gid']),controller,
            '/usr/bin/python3','-B',client_target+'/labctl','--profile',str(archive_profile),
            'archive',large_id,'--archive-root',str(restored_state/'personal-large')],timeout=360).stdout)
        assert downloaded['complete'] and downloaded['bytes']>=wheel_size
        # The browser endpoint also streams a bounded HTTP range instead of reading the whole wheel into RAM.
        ranged=urllib.request.Request(outer_url+'/api/jobs/'+large_id+'/artifact?name=results%2Fpackage.whl',
                headers={'Authorization':'Bearer '+token,'Range':'bytes='+str(wheel_size-257)+'-'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),*([urllib.request.HTTPSHandler(context=tls_context)] if https else [])).open(ranged,timeout=15) as stream:
            tail=stream.read(258);assert stream.status==206 and len(tail)==257
        with large_wheel.open('rb') as stream:stream.seek(wheel_size-257);assert stream.read()==tail
        retained=api('/api/me')[1]['storage'];assert retained['personal_bytes']['archive']>=wheel_size
        large_result={'passed':True,'job_id':large_id,'source_wheel':large_wheel.name,'bytes':wheel_size,
            'sha256':wheel_hash.hexdigest(),'wheel_entries':assertion['wheel_entries'],
            'download_verified':downloaded['complete'],'browser_range_verified':True,'storage':retained,
            'scope':'Existing real cp311 wheel transferred and inspected under Python3.10, not rebuilt/installed or ABI/numerically tested; pre-existing small-state backup excludes this later large wheel'}
        ctl(node,'node','stop');ctl(controller,'controller','stop')
        check('Existing real >1GiB wheel crosses sealed input, bounded workspace, quota-checked output and installed non-root download; hashes and HTTP range agree')
    report={'passed':True,'recorded_at':now(),'checks':checks,'controller_identity':role,
            'controller_preflight':controller_plan,'node_preflight':node_plan,'remote_docker_preflight_rejected':True,
            'direct_personal_installer_executed':True,
            'controller_install_uid':installed['uid'],'controller_install_gid':installed['gid'],'node_uid':0,'task_uid':23003,
            'initial_release':v1['release_id'],'upgrade_release':v2['release_id'],'job_ids':[first['id'],limited_job['id'],second['id']],
            'workspace_probe':quota,'workspace_provider':'per-UID hard quotas on one dedicated ext4 filesystem' if user_quota else 'dedicated file-backed ext4 on disk; not a physical partition or project quota' if disk else 'dedicated tmpfs filesystem; disk-backed ext4 not tested; XFS quota provider not implemented',
            **({'user_quota_workspace':{'prepared_slots':slots,'peer_job_id':peer_job['id'],'peer_task_uid':23004,'cross_owner_jobs_hidden':True,'peer_after_limit_cleanup':True,'root_query_capability':True,'root_fixture_seccomp_unconfined':True,'user_task_capabilities_unchanged':True,'plan_readonly':True,'completed_prepare_idempotent':True}} if user_quota else {}),
            **({'disk_workspace':owned_disk.report,'persistent_workspace_job':persistent_job['id']} if disk else {}),
            **({'large_wheel':large_result} if large_result else {}),
            **({'storage_pressure':storage_pressure,'pressure_disk':pressure_disk.report} if storage_pressure else {}),
            **({'control_storage_pressure':control_storage_pressure,'pressure_disk':pressure_disk.report} if control_storage_pressure else {}),
            **({'native_control':native_control_proof,'central_disks':[item.report for item in central_disks]} if native_control_proof else {}),
            **({'browser_workflow':browser_workflow_proof} if browser_workflow_proof else {}),
            **({'experiment_budget':experiment_budget_proof} if experiment_budget_proof else {}),
            **({'central_payload':central_proof,'central_disks':[item.report for item in central_disks]} if central_proof else {}),
            **({'central_owner_quotas':{'enforced':True,'controller_extra_capabilities':False,'fixture_seccomp_unconfined':True,
                'host_groups_created':False,'preparation_plan_readonly':True,'pressure':group_pressure,
                'actual_initial_controller_process':central_initial_identity,'actual_restored_controller_process':central_restored_identity,
                'restored_storage_groups':rebound_groups}} if central_groups else {}),
            'backup_quiescent':True,'restore_to_new_state':True,'rollback_preserved_state':True,
            'service_mode':'Actual UID-separated processes; generated systemd syntax verified separately, no systemd PID1 in fixture',
            'host_accounts_modified':False,'global_services_installed':False,'npu_kernel_launches':0,
            'native_https':https,'separate_network_namespaces':https,'isolated_docker_network':https,
            'network_namespaces':net_namespaces if https else None,
            'global_trust_store_modified':False,'public_authority_sha256':hashlib.sha256(authority_text.encode()).hexdigest() if https else None,
            'certificate_renewal':renewal,'https_mcp':https_mcp,
            'retention':retention_proof,'nested_mount_guard':mount_proof,
            'node_spool':{'installed_node_payload_removed':True,'receipt_matches_transfer':True,
                          'manifest_sha256':spool_state['artifact_receipt']['manifest_sha256'],
                          'filesystem_hard_quota':True,'old_node_copies_collected':False,'policy':spool_policy,
                          'control_and_spool_separate':True,'node_control_restore_job':after_node_restore['id']},
            'spool_disk':spool_disk.report,
            **({'dependencies':dependency_proof} if dependency_proof else {}),
            **({'input_durability':input_durability} if input_durability else {}),
            **({'native_toolchain':native_proof} if native_proof else {}),
            **({'payload_quota_resize':quota_resize_proof} if quota_resize_proof else {}),
            **({'node_quota_resize':node_quota_proof} if node_quota_proof else {}),
            **({'deadline_limit':deadline_proof} if deadline_proof else {}),
            **({'operator_analysis':operator_proof} if operator_proof else {}),
            **({'compiler_case':compiler_proof} if compiler_proof else {}),
            **({'large_compiler_case':large_compiler_proof} if large_compiler_proof else {}),
            **({'accuracy_case':accuracy_proof} if accuracy_proof else {}),
            **({'terminal_handoff':terminal_handoff_proof} if terminal_handoff_proof else {}),
            **({'source_grants':source_grant_proof} if source_grant_proof else {}),
            **({'simulation':simulation_proof} if simulation_proof else {}),
            **({'report_view':report_view_proof} if report_view_proof else {}),
            **({'persistent_cache':cache_proof,'cache_disk':cache_disk.report} if cache_proof else {}),
            **({'node_spool_pressure':node_spool_pressure} if node_spool_pressure else {}),
            'fixed_probe_memory':{'explicit_memory_mib':256,'oversized_request_http_status':quota_status,
                                  'ordinary_user_quota_enforced':True,'default_memory_unchanged':True},
            'scope':'Owned containers on one physical server; not a real two-server or NPU deployment acceptance'}
    atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/server-install-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    owned_attempts=[]
    if node in containers:
        info=json.loads(docker(['inspect',node]).stdout)[0]
        assert info['Config']['Labels']['io.ascend-lab.installproof']==prefix
        if info['State']['Running']:
            # A lost/late Docker exit event is an observation failure. Keep the
            # exact role handle and inspect until it is stopped before touching
            # its workspace ownership; do not abort on the stop response alone.
            try:docker(['stop','--time','3',node],check=False)
            except subprocess.TimeoutExpired:pass
            until=time.monotonic()+20
            while True:
                stopped=json.loads(docker(['inspect',node]).stdout)[0]
                assert stopped['Id']==info['Id'] and stopped['Config']['Labels'].get('io.ascend-lab.installproof')==prefix
                if not stopped['State']['Running'] and stopped['State']['Pid']==0:break
                if time.monotonic()>=until:raise RuntimeError('Preserve original fixture: role termination is unconfirmed')
                time.sleep(.2)
        ended=json.loads(docker(['inspect',node]).stdout)[0]['State'];assert not ended['Running'] and ended['Pid']==0
        collect='''import json,re
from pathlib import Path
items=[]
for node in Path('/state').iterdir():
 if not node.is_dir() or not (node/'config.json').is_file():continue
 assert not node.is_symlink() and not (node/'config.json').is_symlink()
 config=json.loads((node/'config.json').read_text())
 for path in node.glob('attempts/*/control/grant.json'):
  assert not path.is_symlink() and path.stat().st_size<=1024**2
  grant=json.loads(path.read_text());attempt=grant['attempt_id']
  assert re.fullmatch('attempt-[a-f0-9]{32}',attempt) and path.parent.parent.name==attempt
  assert grant['node_id']==config['node_id'] and grant['physical_id']==config['physical_id'] and grant['resources']['npu']==0
  items.append(attempt)
assert len(items)<=128
print(json.dumps(sorted(set(items))))
'''
        result=docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','DAC_OVERRIDE',
            '--memory','64m','--cpuset-cpus',str(cpus[0]),'--mount','type=bind,src='+str(node_parent)+',dst=/state,readonly',
            '--entrypoint','/usr/bin/python3',image,'-I','-c',collect])
        owned_attempts=json.loads(result.stdout)
    for name in reversed(containers):
        result=docker(['inspect',name],check=False)
        if result.returncode==0 and json.loads(result.stdout)[0]['Config'].get('Labels',{}).get('io.ascend-lab.installproof')==prefix:
            # Diagnostics remain in this private fixture subtree, never in canonical public artifacts.
            role='controller' if name==controller else 'node';state=controller_state if role=='controller' else node_state
            code='import sys;from pathlib import Path;p=Path(sys.argv[1]);print(p.read_text()[-12000:] if p.exists() else "no bootstrap log")'
            log=docker(['exec',name,'/usr/bin/python3','-I','-c',code,str(state/'bootstrap.log')],check=False)
            (base/(role+'-bootstrap.log')).write_text(log.stdout+log.stderr)
            docker(['rm','-f',name],check=False)
    for attempt in owned_attempts:
        assert re.fullmatch('attempt-[0-9a-f]{32}',attempt)
        result=docker(['ps','-a','--filter','label=io.ascend-lab.attempt='+attempt,'--format','{{.ID}}'],check=False)
        for container_id in result.stdout.splitlines():
            info=json.loads(docker(['inspect',container_id]).stdout)[0]
            if info['Config'].get('Labels',{}).get('io.ascend-lab.attempt')==attempt:
                assert info['Config']['Image']==task_image
                if info['State']['Running']:docker(['stop','--time','2',container_id])
                ended=json.loads(docker(['inspect',container_id]).stdout)[0]['State']
                assert not ended['Running'] and ended['Pid']==0,'Preserve fixture: original task termination is unconfirmed'
                docker(['rm',container_id])
    disk_cleanup_errors=[]
    for name in reversed(volumes):
        result=docker(['volume','inspect',name],check=False)
        if result.returncode==0 and json.loads(result.stdout)[0].get('Labels',{}).get('io.ascend-lab.installproof')==prefix:
            removed=docker(['volume','rm',name],check=False)
            if (owned_disk or pressure_disk or central_disks or spool_disk) and removed.returncode:disk_cleanup_errors.append('Owned volume still referenced; keep the loop helper and inspect before retrying cleanup')
    if owned_disk and not disk_cleanup_errors:
        try:
            owned_disk.cleanup()
        except Exception as error:
            disk_cleanup_errors.append(type(error).__name__+': '+str(error))
    if pressure_disk and not disk_cleanup_errors:
        try:pressure_disk.cleanup()
        except Exception as error:disk_cleanup_errors.append(type(error).__name__+': '+str(error))
    if central_disks and not disk_cleanup_errors:
        for item in central_disks:
            try:item.cleanup()
            except Exception as error:disk_cleanup_errors.append(type(error).__name__+': '+str(error))
    if cache_disk and not disk_cleanup_errors:
        try:cache_disk.cleanup()
        except Exception as error:disk_cleanup_errors.append(type(error).__name__+': '+str(error))
    if cache_disk and 'report' in locals():report['cache_disk']=cache_disk.report
    if spool_disk and not disk_cleanup_errors:
        try:spool_disk.cleanup()
        except Exception as error:disk_cleanup_errors.append(type(error).__name__+': '+str(error))
    if spool_disk and 'report' in locals():
        report['spool_disk']=spool_disk.report;report['cleanup_errors']=disk_cleanup_errors;report['passed']=report['passed'] and not disk_cleanup_errors
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/server-install-validation.json',report)
    if owned_disk and 'report' in locals():
        report['disk_workspace']=owned_disk.report
        report['cleanup_errors']=disk_cleanup_errors
        report['passed']=report['passed'] and not disk_cleanup_errors
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/server-install-validation.json',report)
    if pressure_disk and 'report' in locals():
        report['pressure_disk']=pressure_disk.report
        report['cleanup_errors']=disk_cleanup_errors
        report['passed']=report['passed'] and not disk_cleanup_errors
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/server-install-validation.json',report)
    if central_disks and 'report' in locals():
        report['central_disks']=[item.report for item in central_disks];report['cleanup_errors']=disk_cleanup_errors
        report['passed']=report['passed'] and not disk_cleanup_errors
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/server-install-validation.json',report)
    for name in reversed(networks):
        result=docker(['network','inspect',name],check=False)
        if result.returncode==0 and json.loads(result.stdout)[0].get('Labels',{}).get('io.ascend-lab.installproof')==prefix:
            docker(['network','rm',name],check=False)
    try:
        if disk_cleanup_errors:raise RuntimeError('Preserve fixture ownership while storage cleanup is unconfirmed: '+'; '.join(disk_cleanup_errors))
        restore_host_owner()
    finally:
        if large_wheel:
            # Remove only test copies after all fixture daemons are stopped; the original wheel is untouched.
            for copy in base.rglob('package.whl'):
                if copy.is_file() and not copy.is_symlink() and copy.resolve().is_relative_to(base.resolve()):copy.unlink()
        if stopped_main:subprocess.run([str(ROOT/'labctl'),'start'],capture_output=True,timeout=15)
    if disk_cleanup_errors:raise RuntimeError('Owned disk cleanup requires reconciliation: '+str(disk_cleanup_errors))

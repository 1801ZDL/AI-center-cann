"""Package only explicit source; summarize existing evidence without claiming full readiness."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jobs import read_json,atomic_json,now
from lab_client import Client,private_profile

client=Client.from_profile(ROOT/'runtime/config.json')
summary=client.request('/api/jobs')
assert not [j for j in summary['jobs'] if j['state'] in {'queued','reserved','running','cancelling','needs_attention'}]
assert summary['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
log=(ROOT/'runtime/unit-v013.log').read_text()
assert re.search(r'Ran 156 tests.*\n\nOK\s*$',log,re.S),log[-1000:]
subprocess.run(['node','--check',str(ROOT/'web/app.js')],check=True,capture_output=True)
names=['queue-validation.json','queue-browser-validation.json','worker-live-validation.json',
       'worker-browser-validation.json','root-roles-validation.json','managed-engine-validation.json',
       'client-live-validation.json','agent-browser-validation.json','workflow-live-validation.json',
       'workflow-browser-validation.json','root-workflow-validation.json','workflow-mcp-validation.json',
       'server-install-validation.json','resources-live-validation.json','capacity-live-validation.json',
       'npu-admission-live-validation.json','mcp-resume-validation.json','archive-pulse-validation.json',
       'personal-archive-live-validation.json','personal-archive-edge-validation.json','large-wheel-live-validation.json']
evidence={}
snapshot=ROOT/'evidence/v013';snapshot.mkdir(exist_ok=True)
for name in names:
    data=read_json(ROOT/'evidence'/name);assert data and data['passed'],name
    content=(ROOT/'evidence'/name).read_bytes();(snapshot/name).write_bytes(content)
    evidence[name]={'passed':True,'snapshot':'v013/'+name,'sha256':hashlib.sha256(content).hexdigest()}
latest=read_json(ROOT/'runtime/agents/latest.json')
agent=read_json(Path(latest['directory'])/'summary.json');assert agent['verified']
installed=read_json(ROOT/'evidence/server-install-validation.json')
assert installed['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
assert installed['node_uid']==0 and installed['task_uid']==23003
assert installed['backup_quiescent'] and installed['restore_to_new_state'] and installed['rollback_preserved_state']
assert not installed['host_accounts_modified'] and not installed['global_services_installed']
assert installed['npu_kernel_launches']==0 and len(installed['checks'])==13
large=read_json(ROOT/'evidence/large-wheel-live-validation.json')
assert large['passed'] and large['tested_release']==installed['initial_release']
assert large['large_wheel']['bytes']>1024**3 and large['large_wheel']['download_verified'] and large['large_wheel']['browser_range_verified']
quota=installed['workspace_probe']
assert quota['passed'] and quota['uid']==23003 and quota['byte_errno']==quota['inode_errno']==28
assert quota['filesystem_byte_limit']==32*1024**2 and quota['filesystem_inode_limit']==512
experimental={}
for name in ['managed-npu-live-validation.json','npu-runtime-v010.json',
             'npu-environment-registration-v010.json','npu-live-validation.json']:
    data=read_json(ROOT/'evidence'/name);assert data,name
    content=(ROOT/'evidence'/name).read_bytes();(snapshot/name).write_bytes(content)
    experimental[name]={'snapshot':'v013/'+name,'sha256':hashlib.sha256(content).hexdigest()}
assert not read_json(ROOT/'evidence/managed-npu-live-validation.json')['passed']
assert not read_json(ROOT/'evidence/npu-runtime-v010.json')['runtime_initialization_passed']
archive_pulse=read_json(ROOT/'evidence/archive-pulse-validation.json')
assert archive_pulse['passed']
personal=read_json(ROOT/'evidence/personal-archive-live-validation.json')
assert personal['passed'] and personal['large_artifact_bytes']==60*1024**2 and personal['resumed_bytes']>0
assert personal['model_calls']==0 and personal['npu_kernel_launches']==0
capacity=read_json(ROOT/'evidence/capacity-live-validation.json')
assert capacity['logical_users']==20 and capacity['submission_concurrency']==20 and capacity['queue_peak']==20
assert capacity['maximum_reserved_memory_mib']<=768 and capacity['maximum_observed_slots']==2
assert len({item['id'] for item in capacity['jobs']})==20 and len({item['task_uid'] for item in capacity['jobs']})==20
domains=summary['resource_domains'];assert all(not domain['reserved']['slots'] for domain in domains)
original=read_json(ROOT/'evidence/validation-v02.json')['container_unchanged']
container=json.loads(subprocess.run(['docker','inspect','zdl'],check=True,capture_output=True,text=True).stdout)[0]
current={'started_at':container['State']['StartedAt'],'memory_limit':container['HostConfig']['Memory'],
         'shm_limit':container['HostConfig']['ShmSize'],'status':container['State']['Status']}
assert current==original
ours=subprocess.run(['docker','ps','-a','--filter','label=io.ascend-lab.attempt','--format','{{.ID}}'],
                    check=True,capture_output=True,text=True).stdout.strip()
assert not ours,'Owned test containers remain; inspect only project-labeled objects'
volumes=subprocess.run(['docker','volume','ls','--filter','label=io.ascend-lab.installproof','--format','{{.Name}}'],
                       check=True,capture_output=True,text=True).stdout.strip()
assert not volumes,'Owned installation test volume remains; do not delete unrelated volumes'
workers=[]
for node in ['worker-59f0255aaeda','worker-829166f2505f']:
    config=ROOT/'runtime/workers'/node/'config.json'
    status=json.loads(subprocess.run([str(ROOT/'workerctl'),'status','--config',str(config)],
                                    check=True,capture_output=True,text=True).stdout)
    assert status['running'];workers.append(status)

sources=set()
for pattern in ['*.py','*.md','*.sh']:
    sources.update(ROOT.glob(pattern))
sources.update(ROOT/name for name in ['labctl','agentctl','accessctl','workerctl','install-client','servicectl'])
for folder in ['web','examples','tests']:
    sources.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
assert all(not p.is_symlink() for p in sources)
fingerprints={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)}
archive=ROOT/'evidence/ascend-lab-demo-v013-source.tar.gz'
with tarfile.open(archive,'w:gz') as tar:
    for path in sorted(sources):
        tar.add(path,arcname='ascend-lab-demo/'+str(path.relative_to(ROOT)),recursive=False)
with tarfile.open(archive) as tar:
    assert len(tar.getmembers())==len(sources)
    assert all(m.isfile() and '/runtime/' not in m.name and '/tools/' not in m.name and '/evidence/' not in m.name
               and not m.name.startswith('/') and '..' not in Path(m.name).parts for m in tar.getmembers())

server_archive=ROOT/'evidence/ascend-lab-server-v013.tar.gz'
assert server_archive.is_file()
with tarfile.open(server_archive) as tar:
    manifest=json.load(tar.extractfile('release.json'));assert manifest['id']==installed['initial_release']
    assert all(m.isfile() and not any(part in {'runtime','tools','evidence'} for part in Path(m.name).parts)
               and '..' not in Path(m.name).parts and not m.name.startswith('/') for m in tar.getmembers())
report={'version':'0.13','verified_at':now(),'milestone_passed':True,'overall_goal_complete':False,
        'milestone_scope':'Transactional storage admission and actual existing >1GiB wheel input/output/download under separate non-root roles; not full rebuild, disk quota, ABI or NPU acceptance',
        'unit_tests':156,'javascript_syntax':'passed','evidence':evidence,
        'physical_resource_domains':domains,
        'capacity':{key:capacity[key] for key in ['logical_users','submission_concurrency','queue_peak','execution_slots',
                 'maximum_reserved_memory_mib','maximum_observed_slots','actual_servers','npu_kernel_launches']},
        'agent':{'run_id':agent['run_id'],'verified':True,'identity':agent['identity'],
                 'model':agent['actual_model'],'job_ids':[x['job']['id'] for x in agent['verified_jobs']]},
        'npu':read_json(ROOT/'evidence/npu-live-validation.json'),
        'experimental_npu_evidence':experimental,
        'npu_compute_accepted':False,'npu_management_protocol_accepted':True,
        'archive_liveness':archive_pulse,
        'personal_archive':personal,
        'large_wheel':large,
        'storage':summary.get('storage'),
        'first_large_attempt_failure':'large-wheel-first-failure-v013.json',
        'workspace':{'provider':installed['workspace_provider'],'actual_enospc':quota,
                     'disk_backed_tested':False,'persistent_shared_cache_tested':False,
                     'new_controller_default_requires_bounded_workspace':True,
                     'existing_demo_nodes_bounded':False},
        'server_install':{'checks':len(installed['checks']),'controller_identity':installed['controller_identity'],
                         'node_uid':installed['node_uid'],'task_uid':installed['task_uid'],
                         'systemd_pid1_tested':False,'actual_two_server_tested':False,
                         'archive':server_archive.name,'sha256':hashlib.sha256(server_archive.read_bytes()).hexdigest()},
        'npu_first_admission_failure':{'evidence':'npu-device-permission-failure.json','kernel_launches':0},
        'reserved_after_validation':summary['reserved'],'active_demo_workers':workers,
        'existing_container_unchanged':current,'owned_attempt_containers_remaining':0,
        'source_sha256':fingerprints,'archive':archive.name,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'remaining':['Real NPU runtime/driver and physical-visible-ID calibration, accuracy/performance/simulation and root device isolation','Full Triton/NPU-IR package builds and complete toolchain/target compatibility',
                     'Disk-backed ext4 workspace acceptance and XFS project-quota provider, slot concurrency, persistent cache and input/archive hard quota',
                     'Full source/archive and multi-GiB stress beyond the demonstrated existing wheel, flexible layouts and retention/GC',
                     'GiB-scale backup/restore and readonly executable-input mode permissions',
                     'Real two-server deployment, heterogeneous capabilities, full mixed workload and waiting-age/backfill policy',
                     'Systemd PID1, installed unit refresh, activation interruption repair and general schema migration',
                     'In-flight disaster recovery/fencing and production identity integration',
                     'msagent capability acceptance; PUBLIC-001 remains deferred']}
atomic_json(ROOT/'evidence/validation-v013.json',report)
print(json.dumps({k:report[k] for k in ['version','milestone_passed','overall_goal_complete','unit_tests','agent',
      'reserved_after_validation','owned_attempt_containers_remaining','archive','archive_sha256']},ensure_ascii=False,indent=2))

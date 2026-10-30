"""Freeze only explicit source and current v0.25 acceptance, never private state."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,read_json,now
from deployment import CORE,digest

evidence=ROOT/'evidence';runtime=ROOT/'runtime'
build=read_json(evidence/'full-build-live-validation.json')
assert build['passed'] and build['wheel_record_files_independently_verified']>1000 and build['workspace_empty']
assert all(digest(ROOT/name)==value for name,value in build['runtime_files'].items())
assert read_json(runtime/'full-build-20261030/control.json')['cleaned_up']
proof=read_json(evidence/'server-install-v025-validation.json')
assert proof['passed'] and len(proof['checks'])==31
central=proof['central_payload']
assert central['separate_filesystems'] and central['mount_loss_503_no_fallback'] and central['stop_works_without_payload']
assert central['backup_includes_payload'] and central['restored_to_new_filesystem'] and central['quarantined_input_rebound']
assert central['old_archive_preserved'] and central['snapshot_manifest']['format']=='ascend-lab-snapshot/v2'
assert len(proof['central_disks'])==2 and all(d['loop_association_released'] and d['helper_removed'] for d in proof['central_disks'])
assert central['source_contract']['device']!=central['destination_contract']['device']
initial=read_json(evidence/'central-payload-first-failure.json')
assert initial['state']=='failed' and initial['product_bug_fixed'] and initial['loops_released']
comparison=read_json(evidence/'central-payload-offline-compare-v025.json')
assert comparison['passed'] and comparison['source_preserved_and_matches_restore']
runtime_attempt=read_json(evidence/'npu-runtime-attempt-v025.json')
assert runtime_attempt['state']=='failed' and runtime_attempt['classification']=='environment_initialization_failure'
assert runtime_attempt['failed_before_explicit_launch'] and runtime_attempt['explicit_triton_launches']==0
assert runtime_attempt['reserved_after']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
assert isinstance(read_json(evidence/'npu-readonly-v025.json'),dict)
after_attempt=read_json(evidence/'npu-after-attempt-v025.json')
assert after_attempt['after_job']==runtime_attempt['job_id']
disk=proof['disk_workspace'];quota=proof['workspace_probe']
assert disk['observed_backing_filesystem']=='ext4' and disk['actual_mount_type']=='ext4'
assert disk['persistent_task_identity_matches_slot'] and disk['sentinel_survived_separate_container_mounts']
assert disk['loop_autoclear'] and disk['loop_association_released'] and disk['helper_removed']
assert not disk['host_block_devices_formatted'] and not disk['host_device_nodes_created'] and not disk['host_mount_propagation_changed']
assert quota['passed'] and quota['uid']==23003 and quota['byte_errno']==quota['inode_errno']==28
assert 16*1024**2<=quota['filesystem_byte_limit']<disk['image_bytes']==32*1024**2
assert quota['filesystem_inode_limit']==512 and not proof['cleanup_errors']
failure=read_json(evidence/'ext4-first-validation-failure.json')
assert failure['job_state']=='failed' and failure['loop_released'] and not failure['compute_or_runtime_failure_claimed']
assert proof['fixed_probe_memory']['ordinary_user_quota_enforced'] and proof['fixed_probe_memory']['oversized_request_http_status']==400
assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
assert proof['https_mcp']['client_uid']==23642 and proof['https_mcp']['mcp_reconnect_reused_job']
assert proof['https_mcp']['diagnostic_case_readonly']
assert read_json(evidence/'diagnostic-agent-validation.json')['primary_review']['does_not_claim_completed_wheel_or_npu_test']
assert proof['node_spool']['installed_node_payload_removed'] and proof['node_spool']['receipt_matches_transfer']
assert proof['retention']['quarantined_backup_restore_rebound'] and proof['nested_mount_guard']['nested_bind_rejected']
assert proof['npu_kernel_launches']==0 and not proof['host_accounts_modified'] and not proof['global_trust_store_modified']
log=runtime/'unit-v025-final.log';assert re.search(r'Ran 301 tests.*\n\nOK\s*$',log.read_text(),re.S)
subprocess.run(['node','--check',str(ROOT/'web/app.js')],check=True,capture_output=True)
server=evidence/'ascend-lab-server-v025.tar.gz'
with tarfile.open(server) as tar:
    manifest=json.load(tar.extractfile('release.json'))
    assert manifest['id']==proof['initial_release']
    for name,record in manifest['files'].items():
        expected=record['sha256'] if isinstance(record,dict) else record
        assert digest(ROOT/name)==expected,('Source differs from Root-tested package',name)

client=Client.from_profile(runtime/'config.json');summary=client.request('/api/jobs')
assert not any(j['state'] in {'queued','reserved','running','cancelling','needs_attention'} for j in summary['jobs'])
assert all(d['policy']['memory_mib']==2048 for d in summary['resource_domains'])
assert summary['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
assert all(not d['reserved']['slots'] and not d['admission']['blocked'] for d in summary['resource_domains'])
workers=[]
for node in ['worker-59f0255aaeda','worker-829166f2505f']:
    config=runtime/'workers'/node/'config.json'
    info=json.loads(subprocess.run([str(ROOT/'workerctl'),'status','--config',str(config)],check=True,capture_output=True,text=True).stdout)
    assert info['running'];workers.append({'node_id':node,'running':True})
zdl=json.loads(subprocess.run(['docker','inspect','zdl'],check=True,capture_output=True,text=True).stdout)[0]
unchanged={'started_at':zdl['State']['StartedAt'],'memory_limit':zdl['HostConfig']['Memory'],'shm_limit':zdl['HostConfig']['ShmSize'],'status':zdl['State']['Status']}
assert unchanged==read_json(evidence/'validation-v02.json')['container_unchanged']
for args in [['ps','-a','--filter','label=io.ascend-lab.attempt','--format','{{.ID}}'],
             ['ps','-a','--filter','label=io.ascend-lab.diskproof','--format','{{.ID}}'],
             ['ps','-a','--filter','label=io.ascend-lab.installproof','--format','{{.ID}}'],
             ['volume','ls','--filter','label=io.ascend-lab.installproof','--format','{{.Name}}'],
             ['network','ls','--filter','label=io.ascend-lab.installproof','--format','{{.Name}}']]:
    assert not subprocess.run(['docker',*args],check=True,capture_output=True,text=True).stdout.strip(),args
assert read_json(runtime/'config.json')['scheduler'].get('storage_trash_grace_seconds',604800)==604800
assert read_json(runtime/'config.json')['scheduler']['require_node_spool']
assert all(n.get('spool_admission') and n['observed'].get('artifact_gc_ready') is True for n in client.request('/api/nodes')['nodes'] if n['enforcement']=='container-cgroups')

snap=evidence/'v025';snap.mkdir(exist_ok=True)
snapshots={}
for name in ['server-install-v025-validation.json','full-build-live-validation.json','full-build-browser-validation.json','full-build-restart-validation.json','full-build-first-failure.json','full-build-link-failure.json','full-build-python-import-failure.json','wheel-import-diagnostic-validation.json','pybind-import-preflight-validation.json','large-probe-live-validation.json','diagnostic-case-live-validation.json','diagnostic-agent-validation.json','listener-reload-live-validation.json','read-load-live-validation.json','read-load-after-live-validation.json','read-load-comparison-validation.json','worker-browser-validation.json']:
    p=evidence/name;value=read_json(p);assert value.get('passed') or name in {'full-build-first-failure.json','full-build-link-failure.json','full-build-python-import-failure.json'},name
    shutil.copy2(p,snap/name);snapshots[name]={'snapshot':'v025/'+name,'sha256':digest(p)}

sources=set()
for pattern in ['*.py','*.md','*.sh']:sources.update(ROOT.glob(pattern))
sources.update(ROOT/n for n in ['labctl','agentctl','accessctl','workerctl','install-client','servicectl'])
for folder in ['web','examples','tests']:
    sources.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
assert all(not p.is_symlink() for p in sources)

# Read only this demo's private account/node fixtures; no model keys or foreign homes.
secrets=set()
def collect(value):
    if isinstance(value,dict):
        for key,entry in value.items():
            if key in {'password','token','node_key'} and isinstance(entry,str) and len(entry)>=12:secrets.add(entry.encode())
            elif isinstance(entry,(dict,list)):collect(entry)
    elif isinstance(value,list):
        for item in value:collect(item)
private_files={runtime/'full-build-20261030/personal-profile.json',runtime/'full-build-20261030/root-node-state/config.json',runtime/'config.json',*(runtime/'personas').glob('*/client.json'),*(runtime/'workers').glob('*/config.json'),*runtime.glob('read-load-20261030*/*.json')}
root_parent=read_json(runtime/'server-install-v025-submit.json')['job']['id']
fixture=runtime/'jobs'/root_parent/'tmp/installation'
for folder in ['controller-state','controller-restored','node-state']:
    p=fixture/folder;private_files.add(p/'config.json');private_files.update(p.glob('personas/*/client.json'))
for p in private_files:
    if p.is_file():collect(read_json(p))
# The two earlier owned ext4 fixtures also had private service credentials.
for identifier in ['job-c1e21d5e43b1','job-33a0b5f9a159','job-949db6c85e7a','job-908f40943bb3','job-3f74ede9b46e','job-f882122e16bd']:
    previous_fixture=runtime/'jobs'/identifier/'tmp/installation'
    for folder in ['controller-state','controller-restored','node-state']:
        target=previous_fixture/folder
        for profile in [target/'config.json',*target.glob('personas/*/client.json')]:
            if profile.is_file():collect(read_json(profile))
    for key in previous_fixture.rglob('*.key'):
        if key.is_file() and not key.is_symlink():secrets.add(key.read_bytes().strip())
for p in fixture.rglob('*.key'):
    if p.is_file() and not p.is_symlink():secrets.add(p.read_bytes().strip())
def scan(data):
    assert not any(secret in data for secret in secrets),'Private fixture credential in package'
    assert not re.search(rb'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{32}',data),'Private PEM material in package'
for p in sources:scan(p.read_bytes())
with tarfile.open(server) as tar:
    for member in tar:
        assert member.isfile() and '..' not in Path(member.name).parts and not member.name.startswith('/')
        assert not {'runtime','tools','evidence'}&set(Path(member.name).parts)
        scan(tar.extractfile(member).read())
source_archive=evidence/'ascend-lab-source-v025.tar.gz'
with tarfile.open(source_archive,'w:gz') as tar:
    for p in sorted(sources):tar.add(p,arcname='ascend-lab-demo/'+str(p.relative_to(ROOT)),recursive=False)
report={'version':'0.25','verified_at':now(),'previous_goal_turn_classification':'progress','milestone_passed':True,
        'overall_goal_complete':False,'production_ready':False,
        'scope':'Explicit separate central payload filesystem installation, no-fallback mount loss, versioned backup and restore onto a new filesystem with quarantine rebinding; prior full wheel and old-release storage-pressure evidence are not rerun or relabeled as this release',
        'unit_tests':301,'diagnostic_case':read_json(evidence/'diagnostic-case-live-validation.json'),'diagnostic_agent':read_json(evidence/'diagnostic-agent-validation.json'),'fixed_probe_memory':proof['fixed_probe_memory'],'unit_log_sha256':digest(log),'javascript_syntax':'passed',
        'root_install_parent_job':root_parent,'root_install_checks':len(proof['checks']),'unit_scope':'All 301 tests newly executed after storage write/HTTP/deployment changes',
        'disk_workspace':disk,'disk_workspace_probe':quota,'central_payload':central,'central_disks':proof['central_disks'],'independent_payload_compare':comparison,'npu_runtime_attempt':runtime_attempt,'npu_after_attempt':after_attempt,'first_ext4_acceptance_failure':failure,'central_payload_first_failure':initial,'model_invocations_this_turn':0,
        'server_release':proof['initial_release'],'server_archive':server.name,'server_archive_sha256':digest(server),
        'source_archive':source_archive.name,'source_archive_sha256':digest(source_archive),
        'source_files':{str(p.relative_to(ROOT)):digest(p) for p in sorted(sources)},'evidence':snapshots,
        'retention':proof['retention'],'main_grace_seconds':604800,'central_automatic_gc':False,
        'node_spool':proof['node_spool'],'full_build':build,
        'nested_mount_guard':proof['nested_mount_guard'],'https_mcp':proof['https_mcp'],
        'credential_scan':{'passed':True,'own_private_values_checked':len(secrets),'model_keys_read_by_packager':False},
        'live_storage':summary.get('storage'),'resource_domains':summary['resource_domains'],
        'active_workers':workers,'reserved_after_validation':summary['reserved'],
        'existing_zdl_unchanged':unchanged,'owned_attempt_containers_remaining':0,
        'model_invocations':1,'provider_http_request_count':None,'npu_kernel_launches':0,'npu_compute_accepted':False,
        'npu_readonly':read_json(evidence/'npu-readonly-v025.json'),
        'remaining':['Actual NPU runtime, device mapping/isolation and accuracy/performance/simulation',
                     'Full AscendNPU-IR/external LLVM build, per-user filesystem quotas, large build workspaces and inode/metadata budgets',
                     'Node/personal cache lifecycle, metadata SQL scaling, GiB multi-filesystem backup and interrupted-publication recovery',
                     'Actual two-server mixed workload, systemd PID1 and central fencing/identity integration',
                     'Domain capability acceptance; PUBLIC-001 remains deferred']}
atomic_json(evidence/'validation-v025.json',report)
print(json.dumps({key:report[key] for key in ['version','milestone_passed','overall_goal_complete','production_ready','unit_tests',
                  'root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},indent=2))

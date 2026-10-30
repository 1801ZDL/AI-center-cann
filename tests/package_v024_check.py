"""Freeze only explicit source and current v0.24 acceptance, never private state."""
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
proof=read_json(evidence/'server-install-v024-validation.json')
assert proof['passed'] and len(proof['checks'])==31
pressure=proof['storage_pressure'];pdisk=proof['pressure_disk']
assert pressure['passed'] and pressure['input_http_status']==pressure['node_blob_http_status']==507
assert pressure['task_execution_succeeded_before_upload_failure'] and pressure['node_restarted_while_full']
assert pressure['same_attempt_after_recovery'] and pressure['same_run_marker'] and pressure['node_ack_gc_verified']
assert pressure['follower_waited_without_attempt'] and pressure['payload_bytes']==8*1024**2
assert pressure['control_payload_separation']['control_device']!=pressure['control_payload_separation']['payload_device']
assert not pressure['backup_includes_pressure_mounts']
assert pressure['service_capabilities']=={'uids':[999]*4,'effective':0,'permitted':0}
assert pdisk['observed_backing_filesystem']=='ext4' and pdisk['loop_association_released'] and pdisk['helper_removed']
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
observer=read_json(evidence/'storage-pressure-first-observer-failure.json')
assert observer['state']=='failed' and observer['actual_input_http_507_observed'] and observer['loops_released']
assert not observer['pressure_compute_started']
assert proof['fixed_probe_memory']['ordinary_user_quota_enforced'] and proof['fixed_probe_memory']['oversized_request_http_status']==400
assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
assert proof['https_mcp']['client_uid']==23642 and proof['https_mcp']['mcp_reconnect_reused_job']
assert proof['https_mcp']['diagnostic_case_readonly']
assert read_json(evidence/'diagnostic-agent-validation.json')['primary_review']['does_not_claim_completed_wheel_or_npu_test']
assert proof['node_spool']['installed_node_payload_removed'] and proof['node_spool']['receipt_matches_transfer']
assert proof['retention']['quarantined_backup_restore_rebound'] and proof['nested_mount_guard']['nested_bind_rejected']
assert proof['npu_kernel_launches']==0 and not proof['host_accounts_modified'] and not proof['global_trust_store_modified']
log=runtime/'unit-v024-final.log';assert re.search(r'Ran 293 tests.*\n\nOK\s*$',log.read_text(),re.S)
subprocess.run(['node','--check',str(ROOT/'web/app.js')],check=True,capture_output=True)
server=evidence/'ascend-lab-server-v024.tar.gz'
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

snap=evidence/'v024';snap.mkdir(exist_ok=True)
snapshots={}
for name in ['server-install-v024-validation.json','full-build-live-validation.json','full-build-browser-validation.json','full-build-restart-validation.json','full-build-first-failure.json','full-build-link-failure.json','full-build-python-import-failure.json','wheel-import-diagnostic-validation.json','pybind-import-preflight-validation.json','large-probe-live-validation.json','diagnostic-case-live-validation.json','diagnostic-agent-validation.json','listener-reload-live-validation.json','read-load-live-validation.json','read-load-after-live-validation.json','read-load-comparison-validation.json','worker-browser-validation.json']:
    p=evidence/name;value=read_json(p);assert value.get('passed') or name in {'full-build-first-failure.json','full-build-link-failure.json','full-build-python-import-failure.json'},name
    shutil.copy2(p,snap/name);snapshots[name]={'snapshot':'v024/'+name,'sha256':digest(p)}

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
root_parent=read_json(runtime/'server-install-v024-submit.json')['job']['id']
fixture=runtime/'jobs'/root_parent/'tmp/installation'
for folder in ['controller-state','controller-restored','node-state']:
    p=fixture/folder;private_files.add(p/'config.json');private_files.update(p.glob('personas/*/client.json'))
for p in private_files:
    if p.is_file():collect(read_json(p))
# The two earlier owned ext4 fixtures also had private service credentials.
for identifier in ['job-c1e21d5e43b1','job-33a0b5f9a159','job-949db6c85e7a','job-908f40943bb3']:
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
source_archive=evidence/'ascend-lab-source-v024.tar.gz'
with tarfile.open(source_archive,'w:gz') as tar:
    for p in sorted(sources):tar.add(p,arcname='ascend-lab-demo/'+str(p.relative_to(ROOT)),recursive=False)
report={'version':'0.24','verified_at':now(),'previous_goal_turn_classification':'progress','milestone_passed':True,
        'overall_goal_complete':False,'production_ready':False,
        'scope':'Structured capacity responses, committed-prefix chunk recovery and actual isolated payload-disk ENOSPC across installed HTTPS/Root services; node restart resumes the original completed computation and queue; prior wheel evidence reused, not rebuilt',
        'unit_tests':293,'diagnostic_case':read_json(evidence/'diagnostic-case-live-validation.json'),'diagnostic_agent':read_json(evidence/'diagnostic-agent-validation.json'),'fixed_probe_memory':proof['fixed_probe_memory'],'unit_log_sha256':digest(log),'javascript_syntax':'passed',
        'root_install_parent_job':root_parent,'root_install_checks':len(proof['checks']),'unit_scope':'All 293 tests newly executed after storage write/HTTP/deployment changes',
        'disk_workspace':disk,'disk_workspace_probe':quota,'storage_pressure':pressure,'pressure_disk':proof['pressure_disk'],'first_ext4_acceptance_failure':failure,'storage_pressure_observer_failure':observer,'model_invocations_this_turn':0,
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
        'npu_readonly':read_json(evidence/'npu-readonly-v024.json'),
        'remaining':['Actual NPU runtime, device mapping/isolation and accuracy/performance/simulation',
                     'Full AscendNPU-IR/external LLVM build, large disk workspaces and central input/archive hard quota',
                     'Node/personal cache lifecycle, metadata SQL scaling and GiB backup/restore',
                     'Actual two-server mixed workload, systemd PID1 and central fencing/identity integration',
                     'Domain capability acceptance; PUBLIC-001 remains deferred']}
atomic_json(evidence/'validation-v024.json',report)
print(json.dumps({key:report[key] for key in ['version','milestone_passed','overall_goal_complete','production_ready','unit_tests',
                  'root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},indent=2))

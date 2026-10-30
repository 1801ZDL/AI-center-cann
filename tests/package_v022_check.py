"""Freeze only explicit source and current v0.22 acceptance, never private state."""
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
proof=read_json(evidence/'server-install-v022-validation.json')
assert proof['passed'] and len(proof['checks'])==26
assert proof['fixed_probe_memory']['ordinary_user_quota_enforced'] and proof['fixed_probe_memory']['oversized_request_http_status']==400
assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
assert proof['https_mcp']['client_uid']==23642 and proof['https_mcp']['mcp_reconnect_reused_job']
assert proof['https_mcp']['diagnostic_case_readonly']
assert read_json(evidence/'diagnostic-agent-validation.json')['primary_review']['does_not_claim_completed_wheel_or_npu_test']
assert proof['node_spool']['installed_node_payload_removed'] and proof['node_spool']['receipt_matches_transfer']
assert proof['retention']['quarantined_backup_restore_rebound'] and proof['nested_mount_guard']['nested_bind_rejected']
assert proof['npu_kernel_launches']==0 and not proof['host_accounts_modified'] and not proof['global_trust_store_modified']
log=runtime/'unit-v022-final.log';assert re.search(r'Ran 287 tests.*\n\nOK\s*$',log.read_text(),re.S)
subprocess.run(['node','--check',str(ROOT/'web/app.js')],check=True,capture_output=True)
server=evidence/'ascend-lab-server-v022.tar.gz'
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
             ['ps','-a','--filter','label=io.ascend-lab.installproof','--format','{{.ID}}'],
             ['volume','ls','--filter','label=io.ascend-lab.installproof','--format','{{.Name}}'],
             ['network','ls','--filter','label=io.ascend-lab.installproof','--format','{{.Name}}']]:
    assert not subprocess.run(['docker',*args],check=True,capture_output=True,text=True).stdout.strip(),args
assert read_json(runtime/'config.json')['scheduler'].get('storage_trash_grace_seconds',604800)==604800
assert read_json(runtime/'config.json')['scheduler']['require_node_spool']
assert all(n.get('spool_admission') and n['observed'].get('artifact_gc_ready') is True for n in client.request('/api/nodes')['nodes'] if n['enforcement']=='container-cgroups')

snap=evidence/'v022';snap.mkdir(exist_ok=True)
snapshots={}
for name in ['server-install-v022-validation.json','full-build-live-validation.json','full-build-browser-validation.json','full-build-restart-validation.json','full-build-first-failure.json','full-build-link-failure.json','full-build-python-import-failure.json','wheel-import-diagnostic-validation.json','pybind-import-preflight-validation.json','large-probe-live-validation.json','diagnostic-case-live-validation.json','diagnostic-agent-validation.json','listener-reload-live-validation.json','read-load-live-validation.json','read-load-after-live-validation.json','read-load-comparison-validation.json','worker-browser-validation.json']:
    p=evidence/name;value=read_json(p);assert value.get('passed') or name in {'full-build-first-failure.json','full-build-link-failure.json','full-build-python-import-failure.json'},name
    shutil.copy2(p,snap/name);snapshots[name]={'snapshot':'v022/'+name,'sha256':digest(p)}

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
root_parent=read_json(runtime/'server-install-v022-submit.json')['job']['id']
fixture=runtime/'jobs'/root_parent/'tmp/installation'
for folder in ['controller-state','controller-restored','node-state']:
    p=fixture/folder;private_files.add(p/'config.json');private_files.update(p.glob('personas/*/client.json'))
for p in private_files:
    if p.is_file():collect(read_json(p))
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
source_archive=evidence/'ascend-lab-source-v022.tar.gz'
with tarfile.open(source_archive,'w:gz') as tar:
    for p in sorted(sources):tar.add(p,arcname='ascend-lab-demo/'+str(p.relative_to(ROOT)),recursive=False)
report={'version':'0.22','verified_at':now(),'previous_goal_turn_classification':'progress','milestone_passed':True,
        'overall_goal_complete':False,'production_ready':False,
        'scope':'Real frozen-source Triton wheel build/private install/native import/IR and personal archival; explicit fixed-probe memory, live-log preservation and read-only diagnostic evidence/reviewed known-failure draft; no NPU/full external dependency build or production claim',
        'unit_tests':287,'diagnostic_case':read_json(evidence/'diagnostic-case-live-validation.json'),'diagnostic_agent':read_json(evidence/'diagnostic-agent-validation.json'),'fixed_probe_memory':proof['fixed_probe_memory'],'unit_log_sha256':digest(log),'javascript_syntax':'passed',
        'root_install_parent_job':root_parent,'root_install_checks':len(proof['checks']),
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
        'npu_readonly':read_json(evidence/'npu-readonly-v022.json'),
        'remaining':['Actual NPU runtime, device mapping/isolation and accuracy/performance/simulation',
                     'Full AscendNPU-IR/external LLVM build and disk-backed workspace/input/archive hard quota',
                     'Node/personal cache lifecycle, metadata SQL scaling and GiB backup/restore',
                     'Actual two-server mixed workload, systemd PID1 and central fencing/identity integration',
                     'Domain capability acceptance; PUBLIC-001 remains deferred']}
atomic_json(evidence/'validation-v022.json',report)
print(json.dumps({key:report[key] for key in ['version','milestone_passed','overall_goal_complete','production_ready','unit_tests',
                  'root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},indent=2))

"""Freeze only explicit source and current v0.20 acceptance, never private state."""
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
proof=read_json(evidence/'server-install-v020-validation.json')
assert proof['passed'] and len(proof['checks'])==23
assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
assert proof['https_mcp']['client_uid']==23642 and proof['https_mcp']['mcp_reconnect_reused_job']
assert proof['retention']['quarantined_backup_restore_rebound'] and proof['nested_mount_guard']['nested_bind_rejected']
assert proof['npu_kernel_launches']==0 and not proof['host_accounts_modified'] and not proof['global_trust_store_modified']
log=runtime/'unit-v020-final-r2.log';assert re.search(r'Ran 253 tests.*\n\nOK\s*$',log.read_text(),re.S)
subprocess.run(['node','--check',str(ROOT/'web/app.js')],check=True,capture_output=True)
server=evidence/'ascend-lab-server-v020.tar.gz'
with tarfile.open(server) as tar:
    manifest=json.load(tar.extractfile('release.json'))
    assert manifest['id']==proof['initial_release']
    for name,record in manifest['files'].items():
        expected=record['sha256'] if isinstance(record,dict) else record
        assert digest(ROOT/name)==expected,('Source differs from Root-tested package',name)

client=Client.from_profile(runtime/'config.json');summary=client.request('/api/jobs')
assert not any(j['state'] in {'queued','reserved','running','cancelling','needs_attention'} for j in summary['jobs'])
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

snap=evidence/'v020';snap.mkdir(exist_ok=True)
snapshots={}
for name in ['server-install-v020-validation.json','retention-live-validation.json','retention-browser-validation.json']:
    p=evidence/name;value=read_json(p);assert value['passed'],name
    shutil.copy2(p,snap/name);snapshots[name]={'snapshot':'v020/'+name,'sha256':digest(p)}

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
private_files={runtime/'config.json',*(runtime/'personas').glob('*/client.json'),*(runtime/'workers').glob('*/config.json')}
fixture=runtime/'jobs/job-6804b4bed454/tmp/installation'
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
source_archive=evidence/'ascend-lab-source-v020.tar.gz'
with tarfile.open(source_archive,'w:gz') as tar:
    for p in sorted(sources):tar.add(p,arcname='ascend-lab-demo/'+str(p.relative_to(ROOT)),recursive=False)
report={'version':'0.20','verified_at':now(),'previous_goal_turn_classification':'progress','milestone_passed':True,
        'overall_goal_complete':False,'production_ready':False,
        'scope':'Owner central-payload retention and explicit purge, preserved results/evidence, restored quarantine identity; not disk hard quota or automatic GC',
        'unit_tests':253,'retention_tests':19,'unit_log_sha256':digest(log),'javascript_syntax':'passed',
        'root_install_parent_job':'job-6804b4bed454','root_install_checks':len(proof['checks']),
        'server_release':proof['initial_release'],'server_archive':server.name,'server_archive_sha256':digest(server),
        'source_archive':source_archive.name,'source_archive_sha256':digest(source_archive),
        'source_files':{str(p.relative_to(ROOT)):digest(p) for p in sorted(sources)},'evidence':snapshots,
        'retention':proof['retention'],'main_grace_seconds':604800,'automatic_gc':False,
        'nested_mount_guard':proof['nested_mount_guard'],'https_mcp':proof['https_mcp'],
        'credential_scan':{'passed':True,'own_private_values_checked':len(secrets),'model_keys_read':False},
        'live_storage':summary.get('storage'),'resource_domains':summary['resource_domains'],
        'active_workers':workers,'reserved_after_validation':summary['reserved'],
        'existing_zdl_unchanged':unchanged,'owned_attempt_containers_remaining':0,
        'model_calls':0,'npu_kernel_launches':0,'npu_compute_accepted':False,
        'remaining':['Actual NPU runtime, device mapping/isolation and accuracy/performance/simulation',
                     'Full Triton/NPU-IR build and disk-backed workspace/input/archive hard quota',
                     'Node/personal cache lifecycle, metadata SQL scaling and GiB backup/restore',
                     'Actual two-server mixed workload, systemd PID1 and central fencing/identity integration',
                     'Domain capability acceptance; PUBLIC-001 remains deferred']}
atomic_json(evidence/'validation-v020.json',report)
print(json.dumps({key:report[key] for key in ['version','milestone_passed','overall_goal_complete','production_ready','unit_tests',
                  'root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},indent=2))

"""Bind the NPU preflight release to current Root/live proof and explicit prior-build scope."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from deployment import CORE,digest
from lab_client import Client,read_json,atomic_json,now
from workflow_spec import runtime_manifest


def main():
    evidence=ROOT/'evidence';runtime=ROOT/'runtime'
    parent=read_json(runtime/'server-install-v026-submit.json')['job']['id']
    client=Client.from_profile(runtime/'config.json')
    assert client.request('/api/jobs/'+parent)['job']['state']=='succeeded'
    proof=read_json(evidence/'server-install-v026-validation.json')
    assert proof['passed'] and len(proof['checks'])==31 and not proof['cleanup_errors']
    assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
    assert proof['https_mcp']['mcp_reconnect_reused_job'] and proof['https_mcp']['diagnostic_case_readonly']
    assert proof['node_spool']['installed_node_payload_removed'] and proof['node_spool']['receipt_matches_transfer']
    payload=proof['central_payload'];assert payload['mount_loss_503_no_fallback'] and payload['restored_to_new_filesystem']
    assert payload['backup_includes_payload'] and payload['quarantined_input_rebound'] and payload['old_archive_preserved']
    independent=read_json(evidence/'central-payload-offline-compare-v026.json')
    assert independent['passed'] and independent['parent_job']==parent and independent['source_preserved_and_matches_restore']
    assert all(item['loop_association_released'] and item['helper_removed'] for item in proof['central_disks'])
    assert proof['disk_workspace']['loop_association_released'] and proof['disk_workspace']['helper_removed']
    assert proof['workspace_probe']['byte_errno']==proof['workspace_probe']['inode_errno']==28
    assert not proof['host_accounts_modified'] and not proof['global_trust_store_modified'] and proof['npu_kernel_launches']==0
    log=runtime/'unit-v026-system-final.log'
    assert re.search(r'Ran 314 tests.*\n\nOK\s*$',log.read_text(),re.S)
    gate=read_json(evidence/'managed-preflight-live-validation-v026.json')
    assert gate['passed'] and gate['negative_gate_verified'] and gate['cleanup_confirmed']
    assert gate['runtime_files']==runtime_manifest() and not gate['workload_sentinel_started']
    assert gate['protected_receipt']['workload_started'] is False and gate['execution']['failure_stage']=='npu_device_access'
    assert gate['execution']['exit_code']==78 and gate['npu_kernel_launches']==0 and not gate['acl_initialized']
    assert client.request('/api/jobs/'+gate['parent_job'])['job']['state']=='succeeded'
    namespace=read_json(evidence/'npu-namespace-diagnostic-v026.json')
    assert namespace['diagnostic_completed'] and not namespace['npu_runtime_accepted']
    browser=read_json(evidence/'worker-browser-validation.json');assert browser['passed']

    server=evidence/'ascend-lab-server-v026.tar.gz'
    with tarfile.open(server) as archive:
        manifest=json.load(archive.extractfile('release.json'))
        assert manifest['id']==proof['initial_release']
        for name,record in manifest['files'].items():
            assert digest(ROOT/name)==record['sha256'],('Current source differs from Root-tested package',name)
    with tarfile.open(evidence/'ascend-lab-server-v025.tar.gz') as archive:
        old=json.load(archive.extractfile('release.json'))
    changed_code=sorted(name for name in manifest['files'] if not name.endswith('.md') and manifest['files'][name]!=old['files'][name])
    assert changed_code==['container_engine.py','container_entry.py','environment_probe.py','workflow_results.py']
    build=read_json(evidence/'full-build-live-validation.json')
    assert build['passed'] and build['workspace_empty'] and build['wheel_record_files_independently_verified']==5416
    assert all(old['files'][name]['sha256']==value for name,value in build['runtime_files'].items())
    changed_runtime=sorted(name for name,value in build['runtime_files'].items() if digest(ROOT/name)!=value)
    assert changed_runtime==['container_entry.py','environment_probe.py']
    assert read_json(runtime/'full-build-20261030/control.json')['cleaned_up']

    summary=client.request('/api/jobs')
    assert all(summary['counts'][s]==0 for s in ['queued','reserved','running','cancelling','needs_attention'])
    assert summary['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
    assert all(d['policy']['memory_mib']==2048 and not d['reserved']['slots'] and not d['admission']['blocked'] for d in summary['resource_domains'])
    workers=[]
    for node in ['worker-59f0255aaeda','worker-829166f2505f']:
        result=subprocess.run([str(ROOT/'workerctl'),'status','--config',str(runtime/'workers'/node/'config.json')],capture_output=True,text=True,check=True)
        assert json.loads(result.stdout)['running'];workers.append(node)
    zdl=json.loads(subprocess.run(['docker','inspect','zdl'],capture_output=True,text=True,check=True).stdout)[0]
    unchanged={'started_at':zdl['State']['StartedAt'],'memory_limit':zdl['HostConfig']['Memory'],'shm_limit':zdl['HostConfig']['ShmSize'],'status':zdl['State']['Status']}
    assert unchanged==read_json(evidence/'validation-v02.json')['container_unchanged']
    for label in ['io.ascend-lab.attempt','io.ascend-lab.diskproof','io.ascend-lab.installproof','io.ascend-lab.npu-identity','io.ascend-lab.namespace-probe']:
        assert not subprocess.run(['docker','ps','-aq','--filter','label='+label],capture_output=True,text=True,check=True).stdout.strip()
    for kind in ['volume','network']:
        assert not subprocess.run(['docker',kind,'ls','-q','--filter','label=io.ascend-lab.installproof'],capture_output=True,text=True,check=True).stdout.strip()
    assert read_json(runtime/'config.json')['scheduler'].get('storage_trash_grace_seconds',604800)==604800

    sources=set()
    for pattern in ['*.py','*.md','*.sh']:sources.update(ROOT.glob(pattern))
    sources.update(ROOT/n for n in ['labctl','agentctl','accessctl','workerctl','install-client','servicectl'])
    for folder in ['web','examples','tests']:sources.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    assert all(not p.is_symlink() for p in sources)
    secrets=set()
    def collect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {'password','token','node_key'} and isinstance(item,str) and len(item)>=12:secrets.add(item.encode())
                elif isinstance(item,(dict,list)):collect(item)
        elif isinstance(value,list):
            for item in value:collect(item)
    # Own fixtures only; never inspect a model config/key or a foreign home.
    private_files={runtime/'config.json',*(runtime/'personas').glob('*/client.json'),*(runtime/'workers').glob('*/config.json'),
                   runtime/'full-build-20261030/personal-profile.json',runtime/'full-build-20261030/root-node-state/config.json',
                   *runtime.glob('read-load-20261030*/*.json')}
    for submission in runtime.glob('server-install-v0*-submit.json'):
        handle=read_json(submission)
        if not isinstance(handle,dict) or not isinstance(handle.get('job'),dict):continue
        base=runtime/'jobs'/handle['job']['id']/'tmp/installation'
        for folder in ['controller-state','controller-restored','node-state']:
            private_files.add(base/folder/'config.json');private_files.update((base/folder).glob('personas/*/client.json'))
        for path in base.rglob('*.key'):
            if path.is_file() and not path.is_symlink():secrets.add(path.read_bytes().strip())
    for path in private_files:
        if path.is_file():collect(read_json(path))
    def scan(data):
        assert not any(secret in data for secret in secrets),'Own private fixture material in package'
        assert not re.search(rb'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{32}',data)
    for path in sources:scan(path.read_bytes())
    with tarfile.open(server) as archive:
        for member in archive:
            assert member.isfile() and '..' not in Path(member.name).parts and not member.name.startswith('/')
            assert not {'runtime','tools','evidence'}&set(Path(member.name).parts)
            scan(archive.extractfile(member).read())
    source=evidence/'ascend-lab-source-v026.tar.gz'
    with tarfile.open(source,'w:gz') as archive:
        for path in sorted(sources):archive.add(path,arcname='ascend-lab-demo/'+str(path.relative_to(ROOT)),recursive=False)
    gate['service_release_validation_pending']=False
    report={'version':'0.26','verified_at':now(),'previous_goal_turn_classification':'progress',
        'milestone_passed':True,'overall_goal_complete':False,'production_ready':False,
        'scope':'Protected task-identity NPU access rejection before workload start; current package Root installation/IR/storage/HTTPS/MCP/restore. No numerical/runtime acceptance claimed.',
        'unit_tests':314,'unit_skipped':0,'unit_log_sha256':digest(log),'unit_python':'/usr/bin/python3 3.12.3',
        'root_install_parent_job':parent,'root_install_checks':len(proof['checks']),'root_install':proof,
        'server_release':manifest['id'],'server_archive':server.name,'server_archive_sha256':digest(server),
        'source_archive':source.name,'source_archive_sha256':digest(source),
        'source_files':{str(p.relative_to(ROOT)):digest(p) for p in sorted(sources)},
        'code_changed_from_v025':changed_code,'managed_preflight':gate,'namespace_observation':namespace,
        'independent_payload_compare':independent,
        'browser':browser,'active_workers':workers,'existing_zdl_unchanged':unchanged,
        'reserved_after_validation':summary['reserved'],'resource_domains':summary['resource_domains'],
        'prior_full_build':{'evidence':'full-build-live-validation.json','sha256':digest(evidence/'full-build-live-validation.json'),
             'job_id':build.get('job_id'),'wheel_record_files':5416,'protected_runtime_differences':changed_runtime,
             'current_runtime_full_build_reexecuted':False,'scope':'Previously accepted real full Triton build under v022/v025 protected runtime. External LLVM/BishengIR reused; current protected runtime exercised by Root IR/CPU tests and negative NPU gate, not by another full Triton build.'},
        'credential_scan':{'passed':True,'own_values_checked':len(secrets),'model_keys_read':False},
        'model_invocations_this_turn':0,'acl_initializations_this_turn':0,'npu_kernel_launches_this_turn':0,
        'npu_compute_accepted':False,'remaining':['Non-root ACL/runtime and numerical/performance/simulation acceptance',
          'Per-user filesystem byte/inode quotas, persistent caches, full external NPU-IR/LLVM build',
          'Actual two physical servers, systemd PID1, full fencing and organization identity',
          '20-person mixed workload, staged DAGs and domain-agent acceptance; PUBLIC-001 deferred']}
    atomic_json(evidence/'validation-v026.json',report)
    print(json.dumps({key:report[key] for key in ['version','milestone_passed','production_ready','unit_tests','unit_skipped','root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},indent=2))


if __name__=='__main__':main()

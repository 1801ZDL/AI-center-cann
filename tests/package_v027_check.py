"""Bind the device qualification release to current Root/live proof and explicit prior-build scope."""
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
    parent=read_json(runtime/'server-install-v027-submit.json')['job']['id']
    client=Client.from_profile(runtime/'config.json')
    assert client.request('/api/jobs/'+parent)['job']['state']=='succeeded'
    proof=read_json(evidence/'server-install-v027-validation.json')
    assert proof['passed'] and len(proof['checks'])==31 and not proof['cleanup_errors']
    assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
    assert proof['https_mcp']['mcp_reconnect_reused_job'] and proof['https_mcp']['diagnostic_case_readonly']
    assert proof['node_spool']['installed_node_payload_removed'] and proof['node_spool']['receipt_matches_transfer']
    payload=proof['central_payload'];assert payload['mount_loss_503_no_fallback'] and payload['restored_to_new_filesystem']
    assert payload['backup_includes_payload'] and payload['quarantined_input_rebound'] and payload['old_archive_preserved']
    independent=read_json(evidence/'central-payload-offline-compare-v027.json')
    assert independent['passed'] and independent['parent_job']==parent and independent['source_preserved_and_matches_restore']
    assert all(item['loop_association_released'] and item['helper_removed'] for item in proof['central_disks'])
    assert proof['disk_workspace']['loop_association_released'] and proof['disk_workspace']['helper_removed']
    assert proof['workspace_probe']['byte_errno']==proof['workspace_probe']['inode_errno']==28
    assert not proof['host_accounts_modified'] and not proof['global_trust_store_modified'] and proof['npu_kernel_launches']==0
    log=runtime/'unit-v027-final.log'
    assert re.search(r'Ran 325 tests.*\n\nOK\s*$',log.read_text(),re.S)
    gate=read_json(evidence/'device-qualification-early-validation-v027.json')
    assert gate['passed'] and gate['negative_gate_verified'] and gate['qualification']=='blocked'
    assert gate['ordinary_attempts']==0 and gate['cpu_progress_after_device_result'] and gate['registry_restart_preserved_status']
    assert gate['protected_receipt']['workload_started'] is False and gate['probe_state']=='failed'
    assert gate['npu_kernel_launches']==0 and not gate['acl_initialized']
    assert client.request('/api/jobs/'+gate['parent_job'])['job']['state']=='succeeded'
    scope=read_json(evidence/'device-qualification-source-scope-v027.json')
    assert scope['passed'] and scope['current_runtime_files']==runtime_manifest() and scope['differences']==['device_qualification.py']
    text=(ROOT/'device_qualification.py').read_text()
    start=text.index('\ndef invalidate_restored(path):');end=text.index('\n\ndef parameters(value):',start)
    prior=(text[:start].rstrip('\n')+'\n'*3+text[end:].lstrip('\n')).replace('import sqlite3\n','')
    assert hashlib.sha256(prior.encode()).hexdigest()==scope['prior_runtime_files']['device_qualification.py']
    assert all(digest(ROOT/name)==sha for name,sha in scope['prior_runtime_files'].items() if name!='device_qualification.py')
    waiting=read_json(evidence/'device-qualification-final-wait-v027.json')
    latest=client.request('/api/jobs/'+waiting['job_id'])
    assert waiting['state']=='cancelled' and waiting['attempt_count']==0 and latest['job']['state']=='cancelled' and not latest['attempts']
    render=read_json(evidence/'device-qualification-browser-v027.json');assert render['passed'] and render['new_jobs_submitted']==0
    browser=read_json(evidence/'worker-browser-validation.json');assert browser['passed']

    server=evidence/'ascend-lab-server-v027.tar.gz'
    with tarfile.open(server) as archive:
        manifest=json.load(archive.extractfile('release.json'))
        assert manifest['id']==proof['initial_release']
        for name,record in manifest['files'].items():
            assert digest(ROOT/name)==record['sha256'],('Current source differs from Root-tested package',name)
    with tarfile.open(evidence/'ascend-lab-server-v026.tar.gz') as archive:
        old=json.load(archive.extractfile('release.json'))
    changed_code=sorted(name for name in manifest['files'] if not name.endswith('.md') and manifest['files'][name]!=old['files'].get(name))
    assert changed_code==['container_engine.py','container_entry.py','deployment.py','device_qualification.py','environment_probe.py','jobs.py','maintenance.py','nodes.py','templates.py','web/app.js','worker.py','workerctl','workflow_spec.py']
    with tarfile.open(evidence/'ascend-lab-server-v025.tar.gz') as archive:
        build_release=json.load(archive.extractfile('release.json'))
    build=read_json(evidence/'full-build-live-validation.json')
    assert build['passed'] and build['workspace_empty'] and build['wheel_record_files_independently_verified']==5416
    assert all(build_release['files'][name]['sha256']==value for name,value in build['runtime_files'].items())
    changed_runtime=sorted(name for name,value in build['runtime_files'].items() if digest(ROOT/name)!=value)
    assert changed_runtime==['container_entry.py','environment_probe.py','workflow_spec.py']
    assert set(runtime_manifest())-set(build['runtime_files'])=={'device_qualification.py'}
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
    for identifier in ['job-421f7d2ccd23','job-1742a6330dba','job-2b164b5a6429']:
        fixture=runtime/'jobs'/identifier/'tmp/qualification'
        private_files.update([fixture/'controller/config.json',fixture/'node-config.json'])
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
    source=evidence/'ascend-lab-source-v027.tar.gz'
    with tarfile.open(source,'w:gz') as archive:
        for path in sorted(sources):archive.add(path,arcname='ascend-lab-demo/'+str(path.relative_to(ROOT)),recursive=False)
    report={'version':'0.27','verified_at':now(),'previous_goal_turn_classification':'progress',
        'milestone_passed':True,'overall_goal_complete':False,'production_ready':False,
        'scope':'Persistent per-identity/card device-file qualification, protected explicit probe, scheduling gate and offline-restore expiry. Current source tested by 325 unit checks and Root31; earlier HTTP/NPU negative run is source-scoped; latest-runtime probe waited/cancelled without execution. No ACL/numerical/runtime acceptance.',
        'unit_tests':325,'unit_skipped':0,'unit_log_sha256':digest(log),'unit_python':'/usr/bin/python3 3.12.3',
        'root_install_parent_job':parent,'root_install_checks':len(proof['checks']),'root_install':proof,
        'server_release':manifest['id'],'server_archive':server.name,'server_archive_sha256':digest(server),
        'source_archive':source.name,'source_archive_sha256':digest(source),
        'source_files':{str(p.relative_to(ROOT)):digest(p) for p in sorted(sources)},
        'code_changed_from_v026':changed_code,'device_qualification_early_http':gate,'device_qualification_source_scope':scope,'latest_runtime_probe_wait':waiting,'qualification_render':render,
        'independent_payload_compare':independent,
        'browser':browser,'active_workers':workers,'existing_zdl_unchanged':unchanged,
        'reserved_after_validation':summary['reserved'],'resource_domains':summary['resource_domains'],
        'prior_full_build':{'evidence':'full-build-live-validation.json','sha256':digest(evidence/'full-build-live-validation.json'),
             'job_id':build.get('job_id'),'wheel_record_files':5416,'protected_runtime_differences':changed_runtime,'added_runtime_files':['device_qualification.py'],
             'current_runtime_full_build_reexecuted':False,'scope':'Previously accepted real full Triton build under v022/v025 protected runtime. External LLVM/BishengIR reused; current protected runtime exercised by Root IR/CPU and qualification protocol/unit tests, not another full Triton build or latest-runtime NPU probe.'},
        'credential_scan':{'passed':True,'own_values_checked':len(secrets),'model_keys_read':False},
        'model_invocations_this_turn':0,'acl_initializations_this_turn':0,'npu_kernel_launches_this_turn':0,
        'npu_compute_accepted':False,'remaining':['Non-root ACL/runtime and numerical/performance/simulation acceptance',
          'Per-user filesystem byte/inode quotas, persistent caches, full external NPU-IR/LLVM build',
          'Actual two physical servers, systemd PID1, full fencing and organization identity',
          '20-person mixed workload, staged DAGs and domain-agent acceptance; PUBLIC-001 deferred']}
    atomic_json(evidence/'validation-v027.json',report)
    print(json.dumps({key:report[key] for key in ['version','milestone_passed','production_ready','unit_tests','unit_skipped','root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},indent=2))


if __name__=='__main__':main()

"""Bind the installed phase dependency candidate to actual installation and fault proof."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from deployment import CORE,digest
from lab_client import Client,atomic_json,read_json,now
from workflow_spec import runtime_manifest


def main():
    evidence=ROOT/'evidence';runtime=ROOT/'runtime'
    previous=read_json(evidence/'validation-v031.json')
    parent=read_json(runtime/'server-install-v032-r4-handle.json')['job']['id']
    client=Client.from_profile(runtime/'config.json')
    assert client.request('/api/jobs/'+parent)['job']['state']=='succeeded'
    proof=read_json(evidence/'server-install-v032-validation.json')
    assert proof['passed'] and len(proof['checks'])==43 and not proof['cleanup_errors']
    assert proof['controller_identity']=={'uid':999,'socket':False,'code_writable':False}
    assert proof['https_mcp']['mcp_reconnect_reused_job'] and proof['https_mcp']['diagnostic_case_readonly']
    spool=proof['node_spool'];pressure=proof['node_spool_pressure']
    assert spool['filesystem_hard_quota'] and spool['control_and_spool_separate'] and spool['node_control_restore_job']
    assert spool['installed_node_payload_removed'] and spool['receipt_matches_transfer']
    assert pressure['passed'] and pressure['native_enospc']==28 and pressure['filled_bytes']>512*1024**2
    assert pressure['pending_original_status']=='succeeded' and pressure['pending_control_record_written'] and pressure['workspace_retained']
    assert pressure['attempts']==pressure['launches']==1 and pressure['original_finished_at_preserved']
    assert pressure['missing_mount_heartbeat_survived'] and pressure['missing_mount_no_fallback']
    assert pressure['missing_mount_zero_attempts_then_same_job_resumed']
    assert all(p['loop_association_released'] and p['helper_removed'] for p in [proof['spool_disk'],proof['disk_workspace'],proof['cache_disk'],*proof['central_disks']])
    quotas=proof['central_owner_quotas'];assert quotas['enforced'] and not quotas['controller_extra_capabilities']
    assert proof['central_payload']['restored_to_new_filesystem'] and proof['central_payload']['old_archive_preserved']
    assert not proof['host_accounts_modified'] and not proof['global_trust_store_modified'] and proof['npu_kernel_launches']==0
    cache=proof['persistent_cache']
    assert cache['passed'] and cache['cold_invocations']==cache['warm_invocations']==1
    assert cache['active_gc_rejected'] and cache['stale_gc_rejected'] and cache['active_node_restart_same_attempt']
    assert cache['lost_mount_zero_attempts'] and cache['lost_mount_no_directory_fallback'] and cache['original_mount_cache_reused']
    assert cache['idle_namespace_pruned'] and cache['same_cache_retained_after_node_control_restore'] and cache['restored_cache_job']
    assert {p['kind'] for p in cache['native_quota_pressure']}=={'bytes','inodes'}
    assert all(p['native_errno']==122 and p['waiting_attempts_before_cleanup']==0 and p['same_waiting_job_resumed'] for p in cache['native_quota_pressure'])
    assert proof['node_preflight']['persistent_cache']['native_uid_hard_quotas']
    phases=proof['dependencies']
    assert phases['passed'] and phases['producer_cpu_only'] and phases['controller_restart_partial_handoff_resumed']
    assert 0<phases['partial_copied_bytes']<phases['partial_total_bytes'] and phases['consumer_attempts']==1
    assert phases['data_bytes']==8*1024**2 and phases['device_stage_attempts']==0 and not phases['device_stage_executed']
    assert phases['device_stage_no_allocation'] and phases['producer_archive_pinned'] and phases['fan_in_parent_count']==2
    assert phases['unstarted_result_recorded'] and phases['result_csv_has_lineage'] and phases['idempotent_child_reused']
    regression=read_json(evidence/'dependency-pin-race-regression-v032.json')
    assert regression['before_fix_failed'] and regression['after_focused_passed']
    independent=read_json(evidence/'dependency-offline-compare-v032.json')
    assert independent['passed'] and independent['parent_job']==parent and independent['source_preserved_and_matches_restore'] and independent['graph_and_derived_input_preserved']
    log=runtime/'unit-v032-r3-final.log'
    assert re.search(r'Ran 407 tests in 70\.892s\n\nOK\s*$',log.read_text())
    subprocess.run(['node','--check',str(ROOT/'web/app.js')],check=True,capture_output=True)
    browser=read_json(evidence/'worker-browser-validation.json');assert browser['passed']
    render=read_json(evidence/'dependency-browser-v032.json');assert render['passed'] and render['source_parent']==parent and render['new_tasks_submitted']==0
    server=evidence/'ascend-lab-server-v032.tar.gz'
    with tarfile.open(server) as archive:
        manifest=json.load(archive.extractfile('release.json'))
        expected=set(CORE)
        for folder in ['web','examples']:
            expected.update(str(p.relative_to(ROOT)) for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
        assert manifest['id']==proof['initial_release'] and set(manifest['files'])==expected
        for name,record in manifest['files'].items():assert digest(ROOT/name)==record['sha256'],('Source differs from Root package',name)
    with tarfile.open(evidence/'ascend-lab-server-v031.tar.gz') as archive:
        old=json.load(archive.extractfile('release.json'))
    changed=sorted(n for n in manifest['files'] if not n.endswith('.md') and manifest['files'][n]!=old['files'].get(n))
    prior_runtime=previous['current_protected_runtime']
    runtime_changes=sorted(n for n,v in prior_runtime.items() if runtime_manifest()[n]!=v)
    assert runtime_changes==[]
    for name in ['filesystem_quota.py','group_quota.py']:
        assert manifest['files'][name]['sha256']==old['files'][name]['sha256']
    build=dict(previous['prior_full_build']);assert digest(evidence/build['evidence'])==build['sha256']
    build['current_runtime_full_build_reexecuted']=False
    build['scope']='Prior v022/v025 full Triton build; current cache/bootstrap runtime verified by installed CPU IR, not a new full wheel build or NPU run.'

    summary=client.request('/api/jobs')
    assert all(summary['counts'][s]==0 for s in ['queued','reserved','running','cancelling','needs_attention'])
    assert summary['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
    assert all(d['policy']['memory_mib']==2048 and not d['reserved']['slots'] and not d['admission']['blocked'] for d in summary['resource_domains'])
    workers=[]
    for node in ['worker-59f0255aaeda','worker-829166f2505f']:
        r=subprocess.run([str(ROOT/'workerctl'),'status','--config',str(runtime/'workers'/node/'config.json')],check=True,capture_output=True,text=True)
        assert json.loads(r.stdout)['running'];workers.append(node)
    personal=read_json(runtime/'config.json')
    assert not personal.get('scheduler',{}).get('payload_storage') and personal['scheduler'].get('storage_trash_grace_seconds',604800)==604800
    assert all(not read_json(runtime/'workers'/n/'config.json').get('spool_storage') and not read_json(runtime/'workers'/n/'config.json').get('persistent_cache') for n in workers)
    zdl=json.loads(subprocess.run(['docker','inspect','zdl'],check=True,capture_output=True,text=True).stdout)[0]
    unchanged={'started_at':zdl['State']['StartedAt'],'memory_limit':zdl['HostConfig']['Memory'],'shm_limit':zdl['HostConfig']['ShmSize'],'status':zdl['State']['Status']}
    assert unchanged==read_json(evidence/'validation-v02.json')['container_unchanged']
    objects={}
    for label in ['io.ascend-lab.attempt','io.ascend-lab.installproof','io.ascend-lab.diskproof','io.ascend-lab.groupquota-proof','io.ascend-lab.cache-proof']:
        for kind,args in [('containers',['ps','-aq']),('volumes',['volume','ls','-q']),('networks',['network','ls','-q'])]:
            output=subprocess.run(['docker',*args,'--filter','label='+label],check=True,capture_output=True,text=True).stdout.strip()
            assert not output,(label,kind);objects[label+'/'+kind]=[]

    sources=set()
    for pattern in ['*.py','*.md','*.sh']:sources.update(ROOT.glob(pattern))
    sources.update(ROOT/n for n in ['labctl','agentctl','accessctl','workerctl','install-client','servicectl'])
    for folder in ['web','examples','tests']:sources.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    assert ROOT/'web/favicon.svg' in sources and all(not p.is_symlink() for p in sources)
    secrets=set()
    def collect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in {'password','token','node_key','lease_token','_token'} and isinstance(item,str) and len(item)>=12:secrets.add(item.encode())
                elif isinstance(item,(dict,list)):collect(item)
        elif isinstance(value,list):
            for item in value:collect(item)
    # Read only our demo account/node fixtures. No agent/provider configs.
    private_files={runtime/'config.json',*(runtime/'personas').glob('*/client.json'),*(runtime/'workers').glob('*/config.json'),
        runtime/'full-build-20261030/personal-profile.json',runtime/'full-build-20261030/root-node-state/config.json',*runtime.glob('read-load-20261030*/*.json')}
    handles=set(runtime.glob('server-install-v0*-submit.json'))|set(runtime.glob('server-install-v0*-handle.json'))
    for p in handles:
        handle=read_json(p)
        if not isinstance(handle,dict) or not isinstance(handle.get('job'),dict):continue
        base=runtime/'jobs'/handle['job']['id']/'tmp/installation'
        for folder in ['controller-state','controller-restored','node-state','node-control/state','node-control/restored']:
            private_files.add(base/folder/'config.json');private_files.update((base/folder).glob('personas/*/client.json'))
        for folder in ['node-state','node-control/state','node-control/restored']:
            private_files.update((base/folder).glob('attempts/*/control/grant.json'))
        for p in base.rglob('*.key'):
            if p.is_file() and not p.is_symlink():secrets.add(p.read_bytes().strip())
    for path in private_files:
        if path.is_file():collect(read_json(path))
    def scan(data):
        assert not any(s in data for s in secrets),'Private demo fixture value in package'
        assert not re.search(rb'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{32}',data)
    for path in sources:scan(path.read_bytes())
    with tarfile.open(server) as archive:
        for member in archive:
            assert member.isfile() and '..' not in Path(member.name).parts and not member.name.startswith('/')
            assert not {'runtime','tools','evidence'}&set(Path(member.name).parts)
            scan(archive.extractfile(member).read())
    source=evidence/'ascend-lab-source-v032.tar.gz'
    with tarfile.open(source,'w:gz') as archive:
        for path in sorted(sources):archive.add(path,arcname='ascend-lab-demo/'+str(path.relative_to(ROOT)),recursive=False)
    report={'version':'0.32-candidate','verified_at':now(),'previous_goal_turn_classification':'progress',
        'milestone_passed':True,'overall_goal_complete':False,'production_ready':False,
        'scope':'Owner-bound managed workflow DAG, incremental sealed producer artifact handoff, partial-copy controller restart, no early compute/device allocation, fan-in, upstream failure non-execution records and retention pins. Actual CPU IR and8MiB data under installed quotas; no actual device-stage execution, full package pipeline or real two-host/systemd acceptance.',
        'unit_tests':407,'unit_skipped':0,'unit_seconds':70.892,'unit_python':'3.12.3','unit_log':str(log.relative_to(ROOT)),'unit_log_sha256':digest(log),
        'unit_scope':'Full407 Python checks after the reproduced pin-race fix; JavaScript syntax and desktop/mobile assertions separately checked.',
        'root_install_parent_job':parent,'root_install_checks':len(proof['checks']),'root_install':proof,
        'root_install_evidence':'server-install-v032-validation.json','root_install_evidence_sha256':digest(evidence/'server-install-v032-validation.json'),
        'server_release':manifest['id'],'server_archive':server.name,'server_archive_sha256':digest(server),
        'source_archive':source.name,'source_archive_sha256':digest(source),'source_files':{str(p.relative_to(ROOT)):digest(p) for p in sorted(sources)},
        'code_changed_from_v031':changed,'current_protected_runtime':runtime_manifest(),'protected_runtime_differences_from_v031':runtime_changes,
        'prior_real_failures':['dependency-first-cache-fixture-busy-v032.json','dependency-second-fixture-npu-policy-v032.json','dependency-pin-race-regression-v032.json','dependency-offline-observer-path-v032.json'],
        'independent_payload_compare':independent,'browser':browser,'dependency_render':render,
        'active_workers':workers,'existing_zdl_unchanged':unchanged,'owned_objects_after_validation':objects,
        'reserved_after_validation':summary['reserved'],'active_jobs_after_validation':0,
        'credential_scan':{'passed':True,'own_values_checked':len(secrets),'own_private_source_files':len(private_files),'model_keys_read':False},
        'pin_race_regression':regression,'prior_full_build':build,'model_invocations_this_turn':0,'model_keys_read_this_turn':False,'acl_initializations_this_turn':0,
        'npu_kernel_launches_this_turn':0,'npu_compute_accepted':False,
        'remaining':['Actual Triton JIT/ccache/CMake cache reuse and a large build under current UID-quota provider; automatic cache retention and new-disk migration',
          'Actual full compiler/package to device accuracy/performance/simulation pipeline and20-person mixed workloads; atomic multi-job graph creation and explicitly authorized cross-owner publication',
          'Dynamic owner quota provisioning/resize; control database/audit growth hard boundary; crash-durable input offsets',
          'Actual systemd PID1, two physical hosts, full old-controller fencing and organization identity',
          'Non-root NPU numbering/isolation/ACL/runtime/numerical/performance/simulation; full external LLVM/NPU-IR build and domain-agent acceptance; PUBLIC-001 deferred']}
    atomic_json(evidence/'validation-v032.json',report)
    print(json.dumps({k:report[k] for k in ['version','milestone_passed','overall_goal_complete','production_ready','unit_tests','root_install_checks','server_release','server_archive_sha256','source_archive_sha256','credential_scan','reserved_after_validation']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()

"""Audit the explicit public source set; never walk model/session directories.

Run from the developer checkout with --audit-local-private to compare against
only this project's known account/node test fixtures. No secrets are printed.
This prepares files; it never commits, publishes or changes running services.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_client import atomic_json,read_json,now
from deployment import CORE
from workflow_spec import runtime_manifest


def source_paths():
    paths={p for pattern in ['*.py','*.md'] for p in ROOT.glob(pattern) if p.name!='AGENTS.md'}
    paths.update(ROOT/n for n in ['.gitignore','labctl','agentctl','accessctl','workerctl','servicectl','install-client',
                                  'check-browser.sh','requirements-qa.txt','evidence/README.md'])
    for folder in ['examples','tests','web','scripts']:
        paths.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix in {'.py','.md','.sh','.html','.css','.js','.svg','.mlir','.c','.cpp','.json','.txt'})
    # Reports are a separate, explicit allowlist, never a catch-all JSON export.
    paths.update(ROOT/'reports'/n for n in ['v036-public.json','public-source-check.json','mixed-compiler-stage.json']
                 if (ROOT/'reports'/n).is_file())
    assert set(CORE)<={str(p.relative_to(ROOT)) for p in paths}
    return sorted(paths)


def private_values():
    values=set();files=set();r=ROOT/'runtime'
    def collect(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k in {'password','token','node_key','lease_token','_token'} and isinstance(x,str) and len(x)>=12:values.add(x.encode())
                elif isinstance(x,(dict,list)):collect(x)
            if all(isinstance(v.get(k),str) for k in ['username','password']):
                values.add(base64.b64encode((v['username']+':'+v['password']).encode()))
        elif isinstance(v,list):
            for x in v:collect(x)
    files.add(r/'config.json')
    for pattern in ['personas/*/client.json','workers/*/config.json',
                    'full-build-20261030/personal-profile.json','full-build-20261030/root-node-state/config.json',
                    'jobs/*/tmp/installation/controller-*/config.json',
                    'jobs/*/tmp/installation/controller-*/personas/*/client.json',
                    'jobs/*/tmp/installation/node-control/*/config.json',
                    'jobs/*/tmp/installation/node-control/*/attempts/*/control/grant.json',
                    'jobs/*/tmp/nested-*/nested-*/request.json']:
        files.update(r.glob(pattern))
    for pattern in ['jobs/*/tmp/large-mixed/client-*.json','jobs/*/tmp/large-mixed/*-private-config.json']:
        files.update(r.glob(pattern))
    for pattern in ['jobs/*/tmp/native-uid-append/control/node/config.json','jobs/*/tmp/native-uid-append/control/enroll.json']:
        files.update(r.glob(pattern))
    files.update(r.glob('jobs/*/tmp/payload-owner-append/control/controller/config.json'))
    for pattern in ['jobs/*/tmp/node-owner-binding/control/node/config.json','jobs/*/tmp/node-owner-binding/control/enroll.json']:
        files.update(r.glob(pattern))
    for pattern in ['jobs/*/tmp/node-owner-binding/control/expansion-node/config.json',
                    'jobs/*/tmp/node-owner-binding/control/expansion-enroll.json']:
        files.update(r.glob(pattern))
    for pattern in ['jobs/*/tmp/node-owner-binding/control/controller/config.json','jobs/*/tmp/node-owner-binding/control/controller/personas/*/client.json',
                    'owner-activations/*.pending.json','jobs/*/tmp/node-owner-binding/control/controller/owner-activations/*.pending.json']:
        files.update(r.glob(pattern))
    for p in files:
        if p.is_file() and not p.is_symlink():collect(read_json(p))
    return values,len(files)


def main():
    p=argparse.ArgumentParser();p.add_argument('--audit-local-private',action='store_true');a=p.parse_args()
    old=read_json(ROOT/'evidence/validation-v036.json')
    if old:
        assert old['unit_tests']==432 and old['root_install_checks']==54 and old['milestone_passed']
        assert not old['production_ready'] and not old['overall_goal_complete']
        assert runtime_manifest()==old['current_protected_runtime'],'Protected runtime changed: update the acceptance provenance before publication'
        public={k:old[k] for k in ['version','verified_at','milestone_passed','overall_goal_complete','production_ready',
                'scope','unit_tests','unit_skipped','unit_seconds','unit_python','unit_log_sha256','unit_scope',
                'root_install_checks','server_release','server_archive_sha256','current_protected_runtime',
                'large_compiler_build_executed_this_turn','domain_agent_installed_or_accepted_this_turn',
                'npu_compute_accepted','remaining']}
        source_changes=[n for n in CORE if not n.endswith('.md')
                        and hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=old['source_files'].get(n)]
        public.update(schema='ai-center-cann-public-validation/v1',
            source='Curated historical v0.36 local evidence; raw private reports are not published',
            public_source_changes_since_acceptance='Documentation/publication tooling, mixed-workload fixtures, legacy cooperative Unix-socket portability, Worker heartbeat/completion ordering, legacy command artifact fixes, native UID-slot append administration/compatible historical manifest validation, staged owner authentication/lifecycle guards, central owner-group append, Node/cache binding, durable commit/ready ACK, final owner activation, explicit activation abort, offline unit CAS, typed manager readback/reload journal, one-send startup/ready/heartbeat admission gating, Root final activation plus non-overwriting local UID credential handoff with controller restart/retry recovery, active-owner second-Node grants with monotonic authority revisions, explicit pending reauthorization and prospective slot reservations, and a Root Node participant coordinating native slots, immutable proposal, binding/ACK, explicit partial abort and post-binding Node-key rotation recovery; protected six-file runtime hashes unchanged. Historical Root54 is not a new acceptance of these edits; later checks retain frozen scopes. Manager mutations used a fixed adapter with actual installed processes, not actual PID1. Central onboarding now composes credential-free stage, native group quota, service lifecycle, protected Node packets/proposals/ACK/readiness and final local UID delivery with crash recovery; same-host CPU process onboarding accepted; NPU onboarding waits for explicit administrator qualification; explicit remote admin transport and initial Node/storage installation remain prerequisites. Staged NPU access now has a local administrator fixed-probe authorization with immutable Node/stage/context, monotonic revocation and identity-lock checks at grant/start/result and activation; the ordinary probe rejects missing active identity rather than falling back to the controller. Synthetic device observations are protocol evidence only. Explicit parent-flow candidate probes now submit once and preserve original jobs across observation, failure and expiry; completed-probe adoption atomically fences prior authorization and persists a CAS parent chain, with bounded observation and preserved prior snapshots. Candidate count can exceed concurrent NPU quota. Synthetic device protocols and actual CPU/negative CLI installation only; Process-mode onboarding now rejects new submissions while existing queued/active work drains, retains unknown leases and pending cleanup, snapshots explicit local process participants and restores them on pre-stage abort or late work; 49 same-host Root crash boundaries and single-attempt real CPU IR draining accepted. Managed service busy-drain, real PID1, NPU compute, two physical hosts and remote-home automation remain incomplete.',
            production_source_changes_since_acceptance=source_changes,
            reproduced_by_public_ci=False,raw_evidence_in_repository=False,
            physical_hosts_tested=1,real_host_accounts_created=False,
            prior_full_build={'scope':old['prior_full_build']['scope'],'wheel_record_files':5416,'current_runtime_full_build_reexecuted':False},
            important_failures=['A 90-second native workflow timed out; TLS/cleanup observation also timed out; root cause not established',
                'A fixture misclassified needs_attention as terminal; observation fixed to retain the original attempt',
                'Non-root NPU compute acceptance remains missing; earlier device/runtime failures do not establish numerical success'])
        atomic_json(ROOT/'reports/v036-public.json',public)
    assert (ROOT/'reports/v036-public.json').is_file(),'Public validation summary is required'
    secrets,count=private_values() if a.audit_local_private else (set(),0)
    records={};body=[]
    for path in source_paths():
        relative=str(path.relative_to(ROOT));info=path.lstat()
        assert stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size<=4*1024**2,relative
        assert not {'runtime','tools','.git','__pycache__'}&set(Path(relative).parts)
        raw=path.read_bytes()
        assert not any(s in raw for s in secrets),'Known private fixture credential in '+relative
        assert not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{32}',raw),'Private key material in '+relative
        assert not re.search(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',raw),'GitHub token pattern in '+relative
        text=raw.decode('utf-8')
        mode='100755' if info.st_mode&0o111 else '100644'
        records[relative]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'mode':mode}
        body.append({'path':relative,'mode':mode,'type':'blob','content':text})
    report={'schema':'ai-center-cann-publication/v1','recorded_at':now(),'files':records,
            'files_count':len(records),'total_bytes':sum(v['bytes'] for v in records.values()),
            'known_private_values_checked':len(secrets),'private_fixture_files_considered':count,
            'model_keys_read':False,'raw_runtime_or_evidence_included':False,'passed':True}
    atomic_json(ROOT/'reports/publication-manifest.json',report)
    report_path=ROOT/'reports/publication-manifest.json'
    body.append({'path':'reports/publication-manifest.json','mode':'100644','type':'blob','content':report_path.read_text()})
    atomic_json(ROOT/'runtime/publication/tree.json',body)
    print(json.dumps({k:report[k] for k in ['passed','files_count','total_bytes','known_private_values_checked','private_fixture_files_considered','model_keys_read']},indent=2))


if __name__=='__main__':main()

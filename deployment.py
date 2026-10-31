"""Root-owned versioned installation and process lifecycle. No model or tenant credentials in code packages."""
from __future__ import annotations

import grp
import gzip
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import nullcontext

from lab_client import atomic_json,read_json
from state_lock import state_lock

ROOT=Path(__file__).resolve().parent
CORE=['auth.py','owner_identity.py','owner_handoff.py','qualification_compatibility.py','owner_node_grant.py','node_owner_provision.py','owner_provision.py','owner_maintenance_drain.py','owner_device_probe.py','owner_probe_adoption.py','node_owner_ack.py','owner_activation.py','owner_activation_abort.py','owner_delivery.py','owner_finish.py','unit_refresh.py','unit_manager.py','unit_start.py','backends.py','collector.py','container_engine.py','container_entry.py','artifact_export.py',
      'environment_probe.py','environment_contract.py','device_qualification.py','attempt_inventory.py','executor.py','npu_admission.py','npu_device.py','inputs.py','jobs.py','lab_client.py','client_archive.py','archive_api.py','nodes.py','server.py',
      'templates.py','worker.py','workflow_runner.py','workflow_spec.py','workflow_results.py',
      'state_lock.py','resources.py','queue_policy.py','docker_runtime.py','listener.py','preflight.py','workspace_slots.py','filesystem_quota.py','quota_workspace.py','workspace_quota_admin.py','node_owner_admin.py','group_quota.py','payload_groups.py','payload_quota_admin.py','payload_owner_admin.py','io_policy.py','storage_io.py','payload_storage.py','storage_budget.py','storage_files.py','storage_lifecycle.py','node_storage.py','deployment.py','maintenance.py','servicectl','accessctl','workerctl',
      'spool_storage.py','spool_transfer.py','persistent_cache.py','dependencies.py','control_storage.py','control_filesystem.py','control_process.py','labctl','install-client','mcp_bridge.py','diagnostic_case.py','compiler_diagnostic.py','operator_profile.py','operator_node.py','operator_workflow.py','CLIENT.md','SERVER.md','API.md','WORKFLOWS.md','NPU.md','WORKSPACE.md','STORAGE.md','CACHE.md','DEPENDENCIES.md',
      'README.md','DEPLOYMENT.md','SCALING.md','HARNESS.md','OPERATOR_ANALYSIS.md','COMPILER_DIAGNOSTICS.md','ENVIRONMENTS.md','RECOVERY.md','HTTPS.md','SPOOL.md','OWNER_PROVISION.md']
SCHEMA=1
EXECUTABLES={'servicectl','accessctl','workerctl','labctl','install-client'}
# Bundles always include these helpers, but earlier compatible service releases
# do not need them to run existing workspaces. Do not make their absence alone
# prevent verification/rollback of an otherwise intact historical installation.
OPTIONAL_ADMIN_FILES={'qualification_compatibility.py','workspace_quota_admin.py','OWNER_PROVISION.md','owner_identity.py','payload_owner_admin.py','node_owner_admin.py','owner_handoff.py','owner_node_grant.py','node_owner_provision.py','owner_provision.py','owner_maintenance_drain.py','owner_device_probe.py','owner_probe_adoption.py','node_owner_ack.py','owner_activation.py','unit_refresh.py','unit_manager.py','unit_start.py','owner_activation_abort.py','owner_delivery.py','owner_finish.py'}


# Older intact services do not need the optional personal diagnostic reader.
OPTIONAL_ADMIN_FILES.add('operator_profile.py')
OPTIONAL_ADMIN_FILES.add('OPERATOR_ANALYSIS.md')
OPTIONAL_ADMIN_FILES.update({'operator_node.py','operator_workflow.py'})
OPTIONAL_ADMIN_FILES.update({'compiler_diagnostic.py','COMPILER_DIAGNOSTICS.md','SCALING.md'})
OPTIONAL_ADMIN_FILES.add('control_storage.py')
OPTIONAL_ADMIN_FILES.update({'control_filesystem.py','control_process.py'})
CORE.append('control_monitor.py')
OPTIONAL_ADMIN_FILES.add('control_monitor.py')
CORE.append('CONTROL_MONITOR.md')
OPTIONAL_ADMIN_FILES.add('CONTROL_MONITOR.md')
CORE.extend(['audit_retention.py','AUDIT.md'])
OPTIONAL_ADMIN_FILES.update({'audit_retention.py','AUDIT.md'})
CORE.extend(['experiment_budget.py','EXPERIMENT_BUDGET.md'])
OPTIONAL_ADMIN_FILES.update({'experiment_budget.py','EXPERIMENT_BUDGET.md'})
CORE.extend(['accuracy_data.py','accuracy_diagnostic.py','ACCURACY.md'])
OPTIONAL_ADMIN_FILES.update({'accuracy_data.py','accuracy_diagnostic.py','ACCURACY.md'})
CORE.append('dependency_conditions.py')
OPTIONAL_ADMIN_FILES.add('dependency_conditions.py')
CORE.extend(['simulation_data.py','simulation_diagnostic.py','SIMULATION.md'])
OPTIONAL_ADMIN_FILES.update({'simulation_data.py','simulation_diagnostic.py','SIMULATION.md'})
CORE.extend(['report_view.py','REPORT_VIEW.md'])
OPTIONAL_ADMIN_FILES.update({'report_view.py','REPORT_VIEW.md'})
CORE.extend(['source_grants.py','SOURCE_GRANTS.md'])
OPTIONAL_ADMIN_FILES.update({'source_grants.py','SOURCE_GRANTS.md'})


class DeployError(ValueError):pass


def require_root():
    if os.geteuid()!=0:raise DeployError('Administrator installation requires root; no system files were changed')


def runtime_capabilities(info):
    required=['MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','CPUSet','PidsLimit']
    missing=[name for name in required if info.get(name) is not True]
    if missing:raise DeployError('Docker runtime cannot enforce required limits: '+', '.join(missing))
    return {name:True for name in required}


def regular_json(path,max_bytes=2*1024**2,private=False):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as file:
            info=os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or private and info.st_mode&0o077:raise DeployError('Unsafe configuration file permissions or type')
            data=file.read(max_bytes+1)
        if len(data)>max_bytes:raise DeployError('Configuration file exceeds limit')
        value=json.loads(data)
        if not isinstance(value,dict):raise ValueError()
        return value
    except DeployError:raise
    except (OSError,ValueError):raise DeployError('Cannot read a valid installation configuration') from None


def safe_relative(name):
    return (isinstance(name,str) and name and len(name.encode())<4096 and '\\' not in name
            and not any(ord(c)<32 for c in name) and name==str(PurePosixPath(name))
            and not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts and name!='.')


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda:file.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def checked_manifest(manifest):
    if not isinstance(manifest,dict) or manifest.get('format')!='ascend-lab-release/v1' or manifest.get('state_schema')!=SCHEMA:raise DeployError('Unsupported release manifest')
    records=manifest.get('files')
    if not isinstance(records,dict) or not 1<=len(records)<=500 or not (set(CORE)-OPTIONAL_ADMIN_FILES)<=set(records):raise DeployError('Release required files are missing')
    for name,record in records.items():
        if (not safe_relative(name) or not isinstance(record,dict) or set(record)!={'size','sha256'}
            or type(record['size']) is not int or not 0<=record['size']<=4*1024**2
            or not isinstance(record['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',record['sha256'])):raise DeployError('Invalid release file manifest')
        if name=='release.json' or any(str(parent) in records for parent in PurePosixPath(name).parents):raise DeployError('Release file paths collide')
    identity=hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if manifest.get('id')!=identity:raise DeployError('Release manifest identity differs')
    return records


class BoundedRelease:
    def __init__(self,source):self.source=source;self.remaining=20*1024**2

    def read(self,size=-1):
        data=self.source.read(min(size,self.remaining+1) if size>=0 else self.remaining+1)
        self.remaining-=len(data)
        if self.remaining<0:raise DeployError('Release tar stream exceeds its decompression budget')
        return data


def inspect_release(archive_path,sink=None):
    """One bounded parser for a read-only plan and an actual extraction."""
    fd=os.open(archive_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as raw:
        before=os.fstat(raw.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>32*1024**2:
            raise DeployError('Release archive must be a bounded regular file without links')
        compressed_hash=hashlib.sha256()
        for block in iter(lambda:raw.read(65536),b''):compressed_hash.update(block)
        raw.seek(0);seen=set();total=0
        with gzip.GzipFile(fileobj=raw,mode='rb') as compressed:
            stream=BoundedRelease(compressed)
            with tarfile.open(fileobj=stream,mode='r|') as archive:
                first=archive.next()
                if first is None or first.name!='release.json' or not first.isfile() or not 0<first.size<=2*1024**2:
                    raise DeployError('Release manifest must be first')
                manifest=json.loads(archive.extractfile(first).read());records=checked_manifest(manifest)
                for item in iter(archive.next,None):
                    name=item.name
                    if (not safe_relative(name) or not item.isfile() or name not in records or name in seen
                        or item.size!=records[name]['size'] or item.size>4*1024**2):raise DeployError('Unsafe release member')
                    seen.add(name);total+=item.size
                    if total>16*1024**2:raise DeployError('Release code exceeds budget')
                    value=hashlib.sha256()
                    with archive.extractfile(item) as incoming,(sink(name) if sink else nullcontext()) as outgoing:
                        for block in iter(lambda:incoming.read(65536),b''):
                            value.update(block)
                            if outgoing is not None:outgoing.write(block)
                    if value.hexdigest()!=records[name]['sha256']:raise DeployError('Release checksum differs')
                if seen!=set(records):raise DeployError('Release is incomplete')
            while stream.read(65536):pass # Bound padding/PAX expansion and verify the gzip trailer too.
        after=os.fstat(raw.fileno())
        if any(getattr(before,k)!=getattr(after,k) for k in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):
            raise DeployError('Release archive changed during validation')
        return {'manifest':manifest,'sha256':compressed_hash.hexdigest(),'bytes':before.st_size,'unpacked_bytes':total}


def bundle(output,source=ROOT):
    output=Path(output).absolute()
    if output.exists():raise DeployError('Release output already exists')
    files=[Path(source)/name for name in CORE]
    for folder in ['web','examples']:
        files.extend(p for p in (Path(source)/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    records={}
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>4*1024**2:raise DeployError('Invalid release source file')
        records[str(path.relative_to(source))]={'sha256':digest(path),'size':path.stat().st_size}
    identity=hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    manifest={'format':'ascend-lab-release/v1','id':identity,'state_schema':SCHEMA,'files':records}
    output.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as target,tarfile.open(fileobj=target,mode='w:gz') as archive:
            data=json.dumps(manifest,sort_keys=True).encode();entry=tarfile.TarInfo('release.json');entry.size=len(data);entry.mode=0o444
            archive.addfile(entry,io.BytesIO(data))
            for name in sorted(records):
                entry=tarfile.TarInfo(name);entry.size=records[name]['size'];entry.mode=0o555 if name in EXECUTABLES else 0o444
                with (Path(source)/name).open('rb') as file:archive.addfile(entry,file)
    except BaseException:output.unlink(missing_ok=True);raise
    return {'release_id':identity,'archive':str(output),'sha256':digest(output),'files':len(records)}


def unpack_release(archive_path,parent):
    stage=Path(tempfile.mkdtemp(prefix='.release-',dir=parent))
    try:
        def sink(name):
            target=stage/name;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            return target.open('xb')
        manifest=inspect_release(archive_path,sink)['manifest']
        atomic_json(stage/'release.json',manifest)
        for path in stage.rglob('*'):
            if path.is_dir():path.chmod(0o755)
            else:path.chmod(0o555 if str(path.relative_to(stage)) in EXECUTABLES else 0o444)
        stage.chmod(0o755)
        return stage,manifest
    except BaseException:shutil.rmtree(stage);raise


def secure_parent(path):
    path=Path(path).absolute()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(path)) or '..' in path.parts:raise DeployError('Installation paths must be absolute portable paths without traversal')
    for parent in reversed([path.parent,*path.parent.parents]):
        if parent.is_symlink():raise DeployError('Installation ancestry contains a symlink')
        if parent.exists():
            info=parent.stat()
            if info.st_uid!=0 or info.st_mode&0o022:raise DeployError('Installation ancestry must be root-owned and not group/world writable')
        else:parent.mkdir(mode=0o755);parent.chmod(0o755)
    return path


def load_installation(prefix):
    prefix=Path(prefix).absolute();meta=regular_json(prefix/'installation.json')
    if meta.get('format')!='ascend-lab-installation/v1' or meta.get('role') not in {'controller','node'}:raise DeployError('Invalid installation metadata')
    for path in [prefix,prefix/'installation.json',prefix/'releases']:
        info=path.lstat()
        if path.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:raise DeployError('Installed code/metadata must be root-owned and immutable to service users')
    release_id=meta.get('release_id','')
    if not re.fullmatch(r'[0-9a-f]{64}',release_id):raise DeployError('Invalid active release')
    release=prefix/'releases'/release_id
    info=release.lstat()
    if release.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:raise DeployError('Release directory is not immutable')
    if (prefix/'current').resolve()!=release:raise DeployError('Active release link differs from installation metadata')
    return prefix,meta,release


def verify_code(prefix):
    prefix,meta,release=load_installation(prefix);manifest=regular_json(release/'release.json')
    checked_manifest(manifest)
    if manifest.get('id')!=meta['release_id'] or manifest.get('state_schema')!=meta['state_schema']:raise DeployError('Installed release identity differs')
    for name,record in manifest['files'].items():
        if not safe_relative(name):raise DeployError('Unsafe installed path')
        path=release/name
        info=path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or digest(path)!=record['sha256']:raise DeployError('Installed release checksum/permissions differ')
    return prefix,meta,release


def verify(prefix,quota_recovery=False,unit_recovery=False,manager_recovery=False,startup_recovery=False,delivery_recovery=False,finish_recovery=False,owner_recovery=False):
    prefix,meta,release=verify_code(prefix)
    if meta.get('control_filesystem'):
        from control_process import context
        context(meta).check()
    if not owner_recovery:
        from owner_provision import guard as owner_guard
        owner_guard(prefix)
    if not unit_recovery and ((prefix/'unit-refresh-change.json').exists() or (prefix/'unit-refresh-change.json').is_symlink()):
        raise DeployError('Resume the original pending unit refresh before starting or maintaining this installation')
    if not manager_recovery and ((prefix/'unit-manager-change.json').exists() or (prefix/'unit-manager-change.json').is_symlink()):
        raise DeployError('Resume or explicitly abort the original pending manager reload before starting or maintaining this installation')
    if not startup_recovery and any(p.exists() or p.is_symlink() for p in [prefix/'unit-start-change.json',prefix/'service-admission.json']):
        raise DeployError('Resume or explicitly abort the original pending unit startup before starting or maintaining this installation')
    if not delivery_recovery and ((prefix/'owner-delivery-change.json').exists() or (prefix/'owner-delivery-change.json').is_symlink()):
        raise DeployError('Resume or explicitly abort the original pending credential delivery before offline maintenance')
    if not finish_recovery and ((prefix/'owner-finish-change.json').exists() or (prefix/'owner-finish-change.json').is_symlink()):
        raise DeployError('Resume the original owner activation/delivery coordinator before offline maintenance')
    if meta['role']=='controller':
        config=regular_json(Path(meta['state_dir'])/'config.json',private=True)
        if config.get('scheduler',{}).get('control_filesystem')!=meta.get('control_filesystem'):
            raise DeployError('Controller filesystem differs from its protected installation binding')
        if not quota_recovery:
            if (prefix/'payload-owner-change.json').exists() or (prefix/'payload-owner-change.json').is_symlink():
                raise DeployError('Finish the pending owner storage append with its original plan before starting or maintaining this installation')
            if (prefix/'payload-quota-change.json').exists() or (prefix/'payload-quota-change.json').is_symlink():
                raise DeployError('Finish the pending payload quota change with its original plan before starting or maintaining this installation')
            if config.get('scheduler',{}).get('payload_storage')!=meta.get('payload_storage'):raise DeployError('Controller payload policy differs from the installed binding')
        # Package verification/stop must remain available if the disk is lost.
        # JobStore validates the physical mount before a service dispatches work.
    else:
        config=regular_json(Path(meta['state_dir'])/'config.json',private=True)
        from node_owner_provision import guard as participant_guard
        participant_guard(meta['state_dir'])
        if not quota_recovery:
            from node_owner_admin import guard
            guard(meta['state_dir'])
        journal=Path(meta['state_dir'])/'workspace-owner-change.json'
        if not quota_recovery and (journal.exists() or journal.is_symlink()):
            raise DeployError('Resume the original pending UID slot append before starting or maintaining this Node')
        if config.get('spool_storage')!=meta.get('spool_storage'):raise DeployError('Node spool policy differs from its installed binding')
        if not quota_recovery and config.get('persistent_cache')!=meta.get('persistent_cache'):raise DeployError('Node private cache policy differs from its installed binding')
    return prefix,meta,release


def unit_text(prefix,meta):
    prefix=str(prefix);state=meta['state_dir'];role=meta['role']
    payload=meta.get('payload_storage',{}).get('path')
    from payload_groups import gids
    supplementary=gids(meta.get('payload_storage')) if role=='controller' else []
    group_line='SupplementaryGroups='+' '.join(map(str,supplementary))+'\n' if supplementary else ''
    workspace=[];quota_enabled=False
    if role=='node' and (Path(state)/'config.json').exists():
        config=regular_json(Path(state)/'config.json',private=True)
        from quota_workspace import is_quota
        for slot in config.get('workspace',{}).get('slots',[]):
            workspace.append(str(Path(slot['path']).parent) if is_quota(slot) else slot['path'])
            quota_enabled=quota_enabled or is_quota(slot)
        if config.get('spool_storage'):workspace.append(config['spool_storage']['path'])
        for slot in config.get('persistent_cache',{}).get('slots',[]):
            workspace.append(str(Path(slot['path']).parent));quota_enabled=True
    def quoted(value):return '"'+value.replace('%','%%').replace('\\','\\\\').replace('"','\\"')+'"'
    extra=''.join(' '+quoted(path) for path in sorted(set(workspace)))
    native=meta.get('control_filesystem')
    mounts=([payload] if payload else [])+([native['path']] if native else [])+sorted(set(workspace))
    native_rw=(' '+quoted(native['path'])+' '+quoted(prefix)) if native else ''
    capabilities='CAP_SYS_ADMIN CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE' if native else 'CAP_SYS_ADMIN' if quota_enabled else ''
    mount_line='RequiresMountsFor='+' '.join(quoted(path) for path in mounts)+'\n' if mounts else ''
    return f'''[Unit]
Description=Ascend Lab {role}
After=network-online.target
Wants=network-online.target
{mount_line}StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User={0 if native else meta['uid']}
Group={0 if native else meta['gid']}
{group_line}WorkingDirectory={state}
Environment=HOME={state}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -I -B {prefix}/current/servicectl {'native-run' if native else 'run'} --prefix {prefix}
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={state}{(' '+quoted(payload)) if payload else ''}{extra}{native_rw}
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet={capabilities}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
'''


def install(archive,prefix,state,role,uid=None,gid=None,create_user=None,node_config=None,port=18765,
            bind_address='127.0.0.1',public_url=None,tls_cert=None,tls_key=None,tls_ca=None,payload_config=None,control_config=None):
    require_root()
    from preflight import plan,configuration_hash,account_creator
    assessment=plan(role,prefix,state,archive,uid,gid,create_user,node_config,port,bind_address,public_url,tls_cert,tls_key,tls_ca,payload_config,control_config)
    if not assessment['install_ready']:raise DeployError('Installation preflight blocked: '+', '.join(assessment['blockers'])+'; run servicectl doctor for the read-only report')
    prefix=secure_parent(prefix);state=secure_parent(state)
    if prefix.exists() or prefix.is_symlink() or state.is_symlink() or state.exists() and (not state.is_dir() or any(state.iterdir())):raise DeployError('Choose a new installation prefix and empty state directory')
    if state.exists() and state.stat().st_uid!=0:raise DeployError('A pre-created empty state directory must already be root-owned')
    if prefix==state or prefix.is_relative_to(state) or state.is_relative_to(prefix):raise DeployError('Code and state must be separate disjoint directories')
    if role not in {'controller','node'}:raise DeployError('Unknown role')
    native=None
    if control_config:
        from control_filesystem import inspect_new,fingerprint
        native=regular_json(control_config,private=True)
        expected=next(x['detail']['configuration_sha256'] for x in assessment['checks'] if x['id']=='finite_control_filesystem')
        if fingerprint(native)!=expected:raise DeployError('Control filesystem contract changed after doctor')
        inspect_new(native,state,prefix,regular_json(payload_config,private=True))
    if role=='node':
        node_config_path=node_config
        node_config=regular_json(node_config,private=True)
        original_hash=next(item['detail']['configuration_sha256'] for item in assessment['checks'] if item['id']=='node_config')
        if configuration_hash(node_config)!=original_hash:raise DeployError('Node configuration changed after preflight')
        from lab_client import resolve_ca
        node_config=resolve_ca(node_config,node_config_path)
        from lab_client import authority_bytes
        node_authority=authority_bytes(node_config['ca_file']) if node_config.get('ca_file') else None
        expected_ca=next(item['detail'].get('authority_sha256') for item in assessment['checks'] if item['id']=='node_controller')
        if (hashlib.sha256(node_authority).hexdigest() if node_authority else None)!=expected_ca:
            raise DeployError('Node trust authority changed after preflight')
        node_config={**node_config,'docker_runtime':assessment['docker']['binding']}
        from worker import NodeClient
        from backends import physical_identity
        NodeClient(node_config)
        if node_config.get('npu'):
            from npu_device import validate
            validate(node_config['npu'],host=True)
        if node_config.get('workspace'):
            from workspace_slots import validate
            validate(node_config['workspace'],host=True)
        from io_policy import limits
        limits(node_config.get('io_limits'))
        policy=node_config.get('policy',{});cpu_ids=policy.get('cpu_ids')
        if (not isinstance(cpu_ids,list) or not cpu_ids or any(type(c) is not int or c<0 for c in cpu_ids)
            or len(set(cpu_ids))!=len(cpu_ids) or not set(cpu_ids)<=os.sched_getaffinity(0)
            or type(policy.get('memory_mib')) is not int or policy['memory_mib']<256
            or type(policy.get('shm_mib')) is not int or not 0<=policy['shm_mib']<=policy['memory_mib']
            or type(policy.get('max_running')) is not int or not 1<=policy['max_running']<=min(32,len(cpu_ids))):raise DeployError('Node policy must fit visible CPUs, memory/SHM and supported concurrent slots')
        users=node_config.get('users')
        if not isinstance(users,dict) or not users or any(not isinstance(value,dict) or any(type(value.get(key)) is not int or value[key]<1000 for key in ['uid','gid']) for value in users.values()):raise DeployError('Node requires explicit non-root task UID/GID mappings')
        if node_config.get('physical_id')!=physical_identity():raise DeployError('Node enrollment belongs to a different physical host')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',node_config.get('image','')):raise DeployError('Node image must be pinned')
        from docker_runtime import command,discover
        argv,env=command(node_config['docker_runtime'],['docker','image','inspect',node_config['image'],'--format','{{.Id}}'])
        check=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=15)
        if check.returncode or check.stdout.strip()!=node_config['image']:raise DeployError('Pinned node image is not available; installer will not pull images or install Docker')
        _,info=discover(node_config['docker_runtime']);runtime_capabilities(info)
        uid=gid=0
    elif not create_user and (type(uid) is not int or uid<=0 or type(gid) is not int or gid<=0):raise DeployError('Controller requires a dedicated non-root UID/GID or --create-service-user')
    if role=='controller' and (type(port) is not int or not 1024<=port<=65535):raise DeployError('Controller port must be 1024..65535')
    materials={};listener_info=None
    if role=='controller':
        from listener import assess
        listener_info,_,materials=assess(bind_address,port,public_url,tls_cert,tls_key,tls_ca)
        if listener_info!=assessment['listener']:raise DeployError('Listener or TLS material changed after installation preflight')
    prefix.mkdir(mode=0o755)
    prefix.chmod(0o755)
    try:
        releases=prefix/'releases';releases.mkdir(mode=0o755);releases.chmod(0o755)
        stage,manifest=unpack_release(archive,releases)
        if manifest['id']!=assessment['release']['id']:raise DeployError('Release changed after installation preflight')
        target=releases/manifest['id'];stage.rename(target)
        if create_user:
            if role!='controller' or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',create_user):raise DeployError('Invalid dedicated service account name')
            try:pwd.getpwnam(create_user)
            except KeyError:pass
            else:raise DeployError('Service account already exists; choose its explicit UID/GID if intended')
            subprocess.run([account_creator(),'--system','--user-group','--no-create-home','--home-dir',str(state),
                            '--shell','/usr/sbin/nologin',create_user],capture_output=True,check=True,timeout=15)
            account=pwd.getpwnam(create_user);uid=account.pw_uid;gid=account.pw_gid
        state.mkdir(mode=0o700,exist_ok=True);state.chmod(0o700);os.chown(state,uid,gid)
        if role=='controller':
            try:docker_group=grp.getgrnam('docker').gr_gid
            except KeyError:docker_group=None
            try:account=pwd.getpwuid(uid);groups=os.getgrouplist(account.pw_name,gid)
            except KeyError:groups=[gid]
            if gid==0 or docker_group is not None and docker_group in groups:raise DeployError('Controller service identity cannot belong to root/Docker groups')
        with state_lock(state) as lock:os.fchown(lock.fileno(),uid,gid)
        if role=='controller':
            from control_storage import DEFAULT as CONTROL_DEFAULT
            cpus=sorted(os.sched_getaffinity(0))[-2:]
            config={'username':'lab_admin','password':secrets.token_urlsafe(32),'host':bind_address,'port':port,'container':'unused',
                    'audit_retention':{'mode':'archive_required','maximum_unarchived_rows':10000},
                    'monitor_local':False,'interval':15,'identity_workspace_root':str(state/'users'),
                    'scheduler':{'local_enabled':False,'workspace_root':str(state/'users'),'cpu_ids':cpus,
                                 'memory_mib':4096,'shm_mib':1024,'max_running':1,'npu_enabled':False,
                                 'control_storage':dict(CONTROL_DEFAULT),
                                 'require_bounded_workspace':True,'require_pinned_docker':True,'require_environment_contract':True,'require_attempt_inventory':True,'require_node_spool':True,'require_finite_node_spool':True}}
            if payload_config:
                from payload_storage import inspect_new,initialize
                payload=regular_json(payload_config,private=True)
                previous=next(item['detail']['configuration_sha256'] for item in assessment['checks'] if item['id']=='central_payload_storage')
                from preflight import configuration_hash
                if configuration_hash(payload)!=previous:raise DeployError('Central payload contract changed after preflight')
                inspect_new(payload,state,prefix);initialize(payload,state,uid,gid)
                config['scheduler']['payload_storage']=payload
            if materials:
                directory=state/'tls';directory.mkdir(mode=0o700);os.chown(directory,uid,gid)
                tls={}
                for name,data in materials.items():
                    path=directory/(name+'.pem')
                    with path.open('xb') as output:output.write(data)
                    path.chmod(0o600);os.chown(path,uid,gid);tls[name]=str(path)
                tls['material_sha256']=listener_info['material_sha256'];config['tls']=tls;config['controller']=listener_info['public_url']
                if 'authority' in tls:config['ca_file']=tls['authority']
        else:
            config={**node_config,'state_dir':str(state)}
            if config.get('persistent_cache'):
                from persistent_cache import Pool
                cache=Pool(state,config);cache.inspect_new();cache.initialize()
            if config.get('spool_storage'):
                from spool_storage import inspect_new,initialize
                inspect_new(config,state,prefix);initialize(config,state)
            if config.get('ca_file'):
                data=node_authority;directory=state/'trust';directory.mkdir(mode=0o700)
                path=directory/'controller-ca.pem'
                with path.open('xb') as output:output.write(data)
                path.chmod(0o600);config['ca_file']=str(path)
        atomic_json(state/'config.json',config);os.chown(state/'config.json',uid,gid)
        if role=='controller':(state/'users').mkdir(mode=0o700);os.chown(state/'users',uid,gid)
        meta={'format':'ascend-lab-installation/v1','role':role,'uid':uid,'gid':gid,'state_dir':str(state),
              'release_id':manifest['id'],'state_schema':manifest['state_schema'],'previous_releases':[],
              'preflight':{'format':assessment['format'],'checked_at':assessment['checked_at'],'install_ready':True}}
        if role=='controller' and config.get('scheduler',{}).get('payload_storage'):meta['payload_storage']=config['scheduler']['payload_storage']
        if native:
            from control_filesystem import initialize
            initialize(native,state,prefix,uid,gid)
            config['scheduler']['control_filesystem']=native;meta['control_filesystem']=native
            atomic_json(state/'config.json',config);os.chown(state/'config.json',uid,gid)
        if role=='node' and config.get('spool_storage'):meta['spool_storage']=config['spool_storage']
        if role=='node' and config.get('persistent_cache'):meta['persistent_cache']=config['persistent_cache']
        (prefix/'current').symlink_to('releases/'+manifest['id'])
        atomic_json(prefix/'installation.json',meta);(prefix/'installation.json').chmod(0o444)
        if native:
            from control_process import initialize as process_initialize
            process_initialize(prefix,meta)
        (prefix/'units').mkdir(mode=0o755);(prefix/'units').chmod(0o755)
        unit=prefix/'units'/('ascend-lab-'+role+'.service');unit.write_text(unit_text(prefix,meta));unit.chmod(0o444)
        return {'prefix':str(prefix),'role':role,'uid':uid,'gid':gid,'state_dir':str(state),'release_id':manifest['id'],'unit':str(unit),'started':False}
    except BaseException:
        # Retain any initialized private state/account for inspection; never delete user data on a failed install.
        raise


def process_status(prefix):
    from jobs import identity_alive
    prefix,meta,_=load_installation(prefix)
    if meta.get('control_filesystem'):
        from control_process import status
        return status(prefix)
    state=Path(meta['state_dir']);saved=read_json(state/'service-process.json')
    live=identity_alive(saved)
    if live:
        argv=Path('/proc')/str(saved['pid'])/'cmdline'
        expected=prefix/'releases'/saved.get('release_id','')/('server.py' if meta['role']=='controller' else 'worker.py')
        try:values=argv.read_bytes().split(b'\0')
        except OSError:
            if identity_alive(saved):
                return {'role':meta['role'],'running':True,'ready':False,'pid':saved['pid'],'release_id':saved.get('release_id'),
                        'state_dir':str(state),'reason':'Process inspection is temporarily unavailable; no replacement was started'}
            values=[]
        state_arg=state if meta['role']=='controller' else state/'config.json'
        live=(saved.get('uid')==meta['uid'] and re.fullmatch(r'[0-9a-f]{64}',saved.get('release_id',''))
              and str(expected).encode() in values and str(state_arg).encode() in values)
    ready=False
    if live and meta['role']=='controller':
        publication=read_json(state/'server-ready.json') or {}
        ready=all(publication.get(key)==saved.get(key) for key in ['pid','start_ticks','boot_id'])
    elif live:
        ack=read_json(state/'heartbeat-receipt.json') or {}
        ready=ack.get('acknowledged_at',0)>saved.get('started_epoch',time.time())
    return {'role':meta['role'],'running':bool(live),'ready':ready,'pid':saved['pid'] if live else None,
            'release_id':saved.get('release_id') if live else meta['release_id'],'state_dir':str(state)}


def _service_verify(prefix,startup_recovery=False,stopping=False):
    # Credential-only journals do not change execution/resource contracts.
    # Keeping the controller restartable avoids deadlocking live readiness
    # checks after an unrelated controller crash during final delivery.
    selected,meta,release=verify(prefix,startup_recovery=startup_recovery,delivery_recovery=True,finish_recovery=True,owner_recovery=True)
    from owner_provision import service_guard
    service_guard(selected,meta,stopping=stopping)
    if meta['role']!='controller' and any(p.exists() or p.is_symlink() for p in [selected/'owner-delivery-change.json',selected/'owner-finish-change.json']):
        raise DeployError('Credential delivery journals do not belong to a Node installation')
    return selected,meta,release


def run_service(prefix):
    from jobs import identity
    prefix,meta,release=_service_verify(prefix,startup_recovery=bool(os.environ.get('INVOCATION_ID')))
    if os.geteuid()!=meta['uid'] or os.getegid()!=meta['gid']:raise DeployError('Service must run as its configured UID/GID')
    if meta.get('control_filesystem'):
        from control_process import context
        context(meta).runtime()
    from unit_manager import service_entry
    service_entry(prefix)
    if meta['role']=='controller' and os.access('/var/run/docker.sock',os.R_OK|os.W_OK):raise DeployError('Controller identity must not have access to the Docker socket')
    state=Path(meta['state_dir']);lock=state_lock(state,inheritable=True)
    fd=os.open(state/'.service-run.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:os.close(fd);raise DeployError('This installation already has a service process') from None
    os.set_inheritable(fd,True)
    saved={**identity(os.getpid()),'release_id':meta['release_id'],'started_epoch':time.time(),
           'manager_invocation_id':os.environ.get('INVOCATION_ID')}
    atomic_json(state/'service-process.json',saved)
    script=release/('server.py' if meta['role']=='controller' else 'worker.py')
    argv=['/usr/bin/python3','-I','-B',str(script),*(['--state-dir',str(state)] if meta['role']=='controller' else ['--config',str(state/'config.json')])]
    # -I excludes the script directory; add only the verified release explicitly before running it.
    code='import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")'
    from unit_start import process_environment
    native_env={}
    if meta.get('control_filesystem'):
        from control_filesystem import ENVIRONMENT
        from control_process import context
        native_env={**{key:os.environ[key] for key in ENVIRONMENT},**context(meta).temporary_environment()}
    os.execve('/usr/bin/python3',['/usr/bin/python3','-I','-B','-c',code,str(release),str(script),*argv[4:]],
              {'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1',**process_environment(prefix),**native_env})


def start(prefix):
    if load_installation(prefix)[1].get('control_filesystem'):
        from control_process import start as native_start
        return native_start(prefix)
    prefix,meta,release=_service_verify(prefix)
    if os.geteuid() not in {0,meta['uid']}:raise DeployError('Service identity or root is required')
    status=process_status(prefix)
    if status['running']:return status
    state=Path(meta['state_dir'])
    def drop():
        if os.geteuid()==0:
            from payload_groups import gids
            os.setgroups(gids(meta.get('payload_storage')) if meta['role']=='controller' else [])
            os.setgid(meta['gid']);os.setuid(meta['uid'])
        os.umask(0o077)
    log_path=state/'bootstrap.log'
    fd=os.open(log_path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    if os.geteuid()==0:os.fchown(fd,meta['uid'],meta['gid'])
    with os.fdopen(fd,'ab') as log:
        code='import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")'
        child=subprocess.Popen(['/usr/bin/python3','-I','-B','-c',code,str(release),str(release/'servicectl'),'run','--prefix',str(prefix)],
              cwd=state,preexec_fn=drop,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
              env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if child.poll() is not None:raise DeployError('Service failed to start; inspect its private bootstrap.log')
        status=process_status(prefix)
        if status['ready']:return status
        time.sleep(.1)
    return status


def stop(prefix):
    if load_installation(prefix)[1].get('control_filesystem'):
        from control_process import stop as native_stop
        return native_stop(prefix)
    prefix,meta,_=_service_verify(prefix,stopping=True)
    if os.geteuid() not in {0,meta['uid']}:raise DeployError('Service identity or root is required')
    status=process_status(prefix)
    if not status['running']:return status
    from jobs import identity_alive
    saved=read_json(Path(meta['state_dir'])/'service-process.json')
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):raise DeployError('Safe process stop requires Linux/Python pidfd support; use the service manager on older systems')
    try:fd=os.pidfd_open(status['pid'])
    except ProcessLookupError:return process_status(prefix)
    try:
        if not identity_alive(saved):raise DeployError('Service identity changed before stop; no signal was sent')
        if os.geteuid()==0 and meta['uid']!=0:
            def drop():os.setgroups([]);os.setgid(meta['gid']);os.setuid(meta['uid'])
            code='import signal,sys;signal.pidfd_send_signal(int(sys.argv[1]),signal.SIGTERM)'
            result=subprocess.run(['/usr/bin/python3','-I','-c',code,str(fd)],pass_fds=(fd,),preexec_fn=drop,capture_output=True,timeout=5)
            if result.returncode and identity_alive(saved):raise DeployError('Service identity could not request its own stop')
        else:signal.pidfd_send_signal(fd,signal.SIGTERM)
    except ProcessLookupError:pass
    finally:os.close(fd)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        current=process_status(prefix)
        if not current['running']:return current
        time.sleep(.1)
    raise DeployError('Service stop is still pending; no process was force-killed or restarted')

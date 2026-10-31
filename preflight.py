"""Read-only installation planning. No directories/accounts/containers are created.

Reports prerequisites, not numerical NPU acceptance or production readiness.
The installer consumes this same plan before it makes any installation changes.
"""
import datetime as dt
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys

import deployment as deploy
from docker_runtime import discover,command,DockerRuntimeError
from listener import ListenerError


def paths(prefix,state):
    result=[]
    for value in [prefix,state]:
        if not Path(value).is_absolute():raise deploy.DeployError('Installation paths must be absolute')
        path=Path(value).absolute()
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(path)) or '..' in path.parts:
            raise deploy.DeployError('Installation paths must be portable and without traversal')
        for parent in reversed([path.parent,*path.parent.parents]):
            if parent.is_symlink():raise deploy.DeployError('Installation ancestry contains a symlink')
            if parent.exists():
                if not parent.is_dir():raise deploy.DeployError('Installation ancestry must consist of directories')
                info=parent.stat()
                if info.st_uid!=0 or info.st_mode&0o022:raise deploy.DeployError('Installation ancestry must be root-owned and not group/world writable')
        result.append(path)
    prefix,state=result
    if prefix.exists() or prefix.is_symlink():raise deploy.DeployError('Installation prefix already exists')
    if state.is_symlink() or state.exists() and (not state.is_dir() or any(state.iterdir())):
        raise deploy.DeployError('State directory must be new or empty')
    if state.exists() and state.stat().st_uid!=0:raise deploy.DeployError('Existing empty state must be root-owned')
    if prefix==state or prefix.is_relative_to(state) or state.is_relative_to(prefix):
        raise deploy.DeployError('Code and state directories must be disjoint')
    return {'prefix':str(prefix),'state_dir':str(state)}


def configuration_hash(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def task_identities(config):
    users=config.get('users')
    if not isinstance(users,dict) or not users:raise deploy.DeployError('Explicit task identities are required')
    for value in users.values():
        if not isinstance(value,dict) or any(type(value.get(k)) is not int or not 1000<=value[k]<2**31 for k in ['uid','gid']):
            raise deploy.DeployError('Task identities must be non-root numeric UID/GID mappings')
    if len({value['uid'] for value in users.values()})!=len(users):raise deploy.DeployError('Different task users require different UIDs')
    return {'users':len(users),'distinct_task_uids':len(users),'task_uid_minimum':1000}


def controller_identity(uid,gid,create_user):
    if create_user:
        if uid is not None or gid is not None or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',create_user):
            raise deploy.DeployError('Choose a new service account or explicit UID/GID')
        try:pwd.getpwnam(create_user)
        except KeyError:pass
        else:raise deploy.DeployError('Service account already exists')
        try:grp.getgrnam(create_user)
        except KeyError:pass
        else:raise deploy.DeployError('Dedicated service group already exists; choose explicit UID/GID if intended')
        tool=account_creator()
        return {'mode':'create-dedicated-account','uid':'assigned-during-install','docker_access':'forbidden','account_tool':tool}
    if type(uid) is not int or uid<=0 or type(gid) is not int or gid<=0:raise deploy.DeployError('Dedicated non-root UID/GID required')
    try:docker_gid=grp.getgrnam('docker').gr_gid
    except KeyError:docker_gid=None
    try:account=pwd.getpwuid(uid);groups=os.getgrouplist(account.pw_name,gid)
    except KeyError:groups=[gid]
    if 0 in groups or docker_gid is not None and docker_gid in groups:raise deploy.DeployError('Controller identity cannot belong to root/Docker groups')
    return {'mode':'explicit-uid-gid','uid':uid,'gid':gid,'docker_access':'must-be-inaccessible-at-start'}


def controller_path_access(prefix,state,uid,gid,create_user):
    # Process mode clears supplementary groups. A not-yet-created identity
    # cannot depend on an existing ancestor's group membership.
    for value in [prefix,state]:
        for parent in reversed(list(Path(value).absolute().parents)):
            if not parent.exists():continue # secure_parent creates new ancestors as 0755.
            info=parent.stat()
            if info.st_mode&0o001:continue
            if not create_user and info.st_gid==gid and info.st_mode&0o010:continue
            raise deploy.DeployError('Controller identity cannot traverse an installation ancestor; choose service-accessible protected paths')
    return {'existing_ancestors_traversable':True,'state_leaf':'assigned to service UID during installation'}


def account_creator():
    for name in ['/usr/sbin/useradd','/sbin/useradd']:
        try:
            path=Path(name).resolve(strict=True);info=path.stat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022 and os.access(path,os.X_OK)
                and all(p.stat().st_uid==0 and not p.stat().st_mode&0o022 for p in path.parents)):return str(path)
        except OSError:continue
    raise deploy.DeployError('A protected system useradd tool is required')


def port_available(port):
    if type(port) is not int or not 1024<=port<=65535:raise deploy.DeployError('Controller port must be 1024..65535')
    for name in ['/proc/net/tcp','/proc/net/tcp6']:
        try:lines=Path(name).read_text().splitlines()[1:]
        except FileNotFoundError:continue
        for line in lines:
            fields=line.split()
            if len(fields)>3 and fields[3]=='0A' and int(fields[1].split(':')[1],16)==port:
                raise deploy.DeployError('Controller port is already listening in this network namespace')
    return {'port':port,'checked_namespace':'current','listener_observed':False,'reservation':False}


def controller_transport(config):
    from worker import NodeClient
    node=config.get('node_id');key=config.get('node_key')
    if (not isinstance(node,str) or not re.fullmatch(r'worker-[0-9a-f]{12}',node)
        or not isinstance(key,str) or not 24<=len(key)<=4096 or any(ord(c)<32 for c in key)):
        raise deploy.DeployError('Complete node registration and credential fields are required')
    NodeClient(config)
    from lab_client import authority_bytes
    return {'transport_valid':True,'registration_fields_present':True,'authentication':'checked-by-heartbeat-after-start',
            'authority_sha256':hashlib.sha256(authority_bytes(config['ca_file'])).hexdigest() if config.get('ca_file') else None}


def plan(role,prefix=None,state=None,archive=None,uid=None,gid=None,create_user=None,node_config=None,port=18765,
         bind_address='127.0.0.1',public_url=None,tls_cert=None,tls_key=None,tls_ca=None,payload_config=None,control_config=None):
    checks=[];report={'format':'ascend-lab-preflight/v1','checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'role':role,'read_only':True,'production_ready':False,'checks':checks,
        'identity':{'euid':os.geteuid(),'egid':os.getegid()},
        'acceptance':{'runtime_task':'not_run','npu_initialization':'not_run','npu_numerical':'not_run',
                      'disk_backed_quota':'not_run','two_server_recovery':'not_run','systemd_unit_lifecycle':'not_run'}}
    def check(key,fn,required=True):
        try:
            value=fn();checks.append({'id':key,'status':'passed','required':required,'detail':value});return value
        except Exception as error:
            reason=str(error) if isinstance(error,(deploy.DeployError,DockerRuntimeError,ListenerError)) else 'Prerequisite is unavailable or invalid; no operation was attempted'
            checks.append({'id':key,'status':'failed','required':required,'reason':reason});return None
    check('python',lambda: {'version':'.'.join(map(str,sys.version_info[:3]))} if sys.version_info>=(3,10) else (_ for _ in ()).throw(ValueError()))
    checks.append({'id':'administrator','required':True,'status':'passed' if os.geteuid()==0 else 'requires_admin',
                   'reason':'Installation requires root; this report does not change permissions'})
    if role not in {'controller','node'}:
        checks.append({'id':'role','status':'failed','required':True,'reason':'Choose controller or node'})
    if role=='node' and any(v is not None for v in [uid,gid,create_user]) or role=='controller' and node_config:
        checks.append({'id':'role_options','status':'failed','required':True,'reason':'Do not mix node and controller identity options'})
    if role=='node' and (bind_address!='127.0.0.1' or any(x is not None for x in [public_url,tls_cert,tls_key,tls_ca])):
        checks.append({'id':'listener_role','status':'failed','required':True,'reason':'Listener TLS options belong to a controller installation'})
    if role=='node' and payload_config:checks.append({'id':'payload_role','status':'failed','required':True,'reason':'Central payload storage belongs to the controller'})
    if role=='node' and control_config:checks.append({'id':'control_role','status':'failed','required':True,'reason':'Finite control storage belongs to the controller'})
    if prefix is not None and state is not None:report['paths']=check('installation_paths',lambda:paths(prefix,state))
    else:checks.append({'id':'installation_paths','status':'not_checked','required':True,'reason':'Supply --prefix and --state-dir for an installation plan'})
    if archive:
        def release():
            value=deploy.inspect_release(archive)
            if role=='controller' and 'control_storage.py' not in value['manifest']['files']:
                raise deploy.DeployError('New controller installations require control storage admission support')
            if role=='controller' and 'audit_retention.py' not in value['manifest']['files']:
                raise deploy.DeployError('New controller installations require audit archive support')
            if control_config and not {'control_filesystem.py','control_process.py'}<=set(value['manifest']['files']):
                raise deploy.DeployError('Finite control installation needs namespace and filesystem support')
            return {'id':value['manifest']['id'],'sha256':value['sha256'],'bytes':value['bytes'],
                    'unpacked_bytes':value['unpacked_bytes'],'files':len(value['manifest']['files'])}
        report['release']=check('release_archive',release)
    else:checks.append({'id':'release_archive','status':'not_checked','required':True,'reason':'Supply --archive to verify the exact installation source'})
    report['host']=check('host_inventory',lambda:{'cpu_ids':sorted(os.sched_getaffinity(0)),
                'systemd_pid1':Path('/proc/1/comm').read_text().strip()=='systemd',
                'shm_scope':'current service IPC namespace; not the task SHM budget'})
    if role=='controller':
        from control_storage import DEFAULT as CONTROL_DEFAULT,observe as control_observe
        from listener import assess
        report['listener']=check('controller_listener',lambda:assess(bind_address,port,public_url,tls_cert,tls_key,tls_ca)[0])
        report['controller_defaults']={'local_execution':False,'require_bounded_workspace':True,'require_pinned_docker':True,'require_environment_contract':True,'require_attempt_inventory':True,'require_node_spool':True,'require_finite_node_spool':True}
        report['controller_defaults']['control_storage']=dict(CONTROL_DEFAULT)
        if report.get('paths'):
            def control_headroom():
                parent=Path(state)
                while not parent.exists():parent=parent.parent
                value=control_observe(parent,CONTROL_DEFAULT)
                if value['blocks_new_work']:
                    raise deploy.DeployError('Control filesystem lacks configured byte/inode headroom or cannot be observed; no operation was attempted')
                return value
            check('control_storage_admission',control_headroom)
        report['service_identity']=check('controller_identity',lambda:controller_identity(uid,gid,create_user))
        if report.get('paths') and report.get('service_identity'):
            check('controller_path_access',lambda:controller_path_access(prefix,state,uid,gid,create_user))
        else:checks.append({'id':'controller_path_access','status':'not_checked','required':True,'reason':'Validate paths and service identity first'})
        check('controller_port',lambda:port_available(port))
        if payload_config:
            def payload():
                from payload_storage import inspect_new
                value=deploy.regular_json(payload_config,private=True)
                from payload_groups import require_membership
                require_membership(value,primary=gid)
                result=inspect_new(value,state,prefix);result['configuration_sha256']=configuration_hash(value);return result
            report['payload_storage']=check('central_payload_storage',payload)
        if control_config:
            def native_control():
                from control_filesystem import inspect_new
                if not payload_config:raise deploy.DeployError('Finite control requires --payload-config on a separate filesystem')
                return inspect_new(deploy.regular_json(control_config,private=True),state,prefix,deploy.regular_json(payload_config,private=True))
            report['control_filesystem']=check('finite_control_filesystem',native_control)
            from control_process import probe
            check('native_controller_namespace',probe)
    elif role=='node':
        config=check('node_config',lambda:deploy.regular_json(node_config,private=True)) if node_config else None
        # Never serialize config or exception text containing URL/token contents.
        if config is not None:
            checks[-1]['detail']={'configuration_sha256':configuration_hash(config)}
            from lab_client import resolve_ca
            from backends import physical_identity
            from resources import validate_policy
            from io_policy import limits
            check('node_controller',lambda:controller_transport(resolve_ca(config,node_config)))
            check('physical_identity',lambda: {'matches':True} if config.get('physical_id')==physical_identity() else (_ for _ in ()).throw(ValueError()))
            def policy():
                value=validate_policy(config.get('policy'))
                if not set(value['cpu_ids'])<=os.sched_getaffinity(0):raise deploy.DeployError('Node CPU policy exceeds visible CPUs')
                return {**value,'default_workflow_memory_with_overhead_mib':640,
                        'default_workflow_fits_memory':value['memory_mib']>=640}
            report['policy']=check('node_policy',policy)
            report['task_identities']=check('task_identities',lambda:task_identities(config))
            check('io_limits',lambda:limits(config.get('io_limits')))
            if config.get('environment_manifest'):
                from environment_contract import verify_config
                check('certified_environment',lambda:{'sha256':verify_config(config)})
            else:checks.append({'id':'certified_environment','status':'not_configured','required':False,
                   'reason':'After installation, run a queued environment_probe and certify it before versioned workflows'})
            runtime=check('docker_transport',lambda:discover(config.get('docker_runtime')))
            if runtime:
                binding,info=runtime;checks[-1]['detail']={'endpoint':binding['endpoint'],'daemon_id':binding['daemon_id']}
                report['docker']={'binding':binding,'cgroup_version':info.get('CgroupVersion'),
                    'enforced_limits':{key:info[key] for key in ['MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','CPUSet','PidsLimit']}}
                def image():
                    import subprocess
                    value=config.get('image','')
                    if not isinstance(value,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',value):raise deploy.DeployError('Image must be pinned')
                    argv,env=command(binding,['docker','image','inspect',value,'--format','{{.Id}}'])
                    done=subprocess.run(argv,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=15)
                    if done.returncode or done.stdout.strip()!=value:raise deploy.DeployError('Pinned image is unavailable')
                    return {'sha256':value,'present':True,'pulled':False,'executed':False}
                report['image']=check('pinned_image',image)
                def memory():
                    capacity=info.get('MemTotal',0);wanted=config.get('policy',{}).get('memory_mib',0)*1024**2
                    if type(capacity) is not int or capacity<wanted+512*1024**2:raise deploy.DeployError('Daemon host memory envelope is insufficient')
                    return {'daemon_host_total_bytes':capacity,'budget_bytes':wanted,'reserve_bytes':512*1024**2,
                            'scope':'daemon host; service container cgroup is a separate budget'}
                check('daemon_memory_envelope',memory)
            else:checks.append({'id':'pinned_image','status':'not_checked','required':True,'reason':'Docker transport must pass first'})
            if config.get('workspace'):
                def workspace():
                    from workspace_slots import validate,observe
                    validate(config['workspace'],host=True)
                    from quota_workspace import separate_control
                    separate_control(config['workspace'],state or config['state_dir'])
                    reading=observe(config['workspace'])
                    if any(slot['status']!='ok' or not slot['clean'] for slot in reading['slots']):raise deploy.DeployError('Workspace must be clean before installation')
                    if config.get('policy',{}).get('max_running',1)>len(reading['slots']):raise deploy.DeployError('More node slots than workspaces')
                    return {'slots':len(reading['slots']),'clean':True,'policy_sha256':reading['policy_sha256'],
                            'disk_backed':all(slot['filesystem']!='tmpfs' for slot in config['workspace']['slots']),
                            'uid_quota_slots':sum(slot['filesystem']=='ext4-userquota' for slot in config['workspace']['slots'])}
                report['workspace']=check('bounded_workspace',workspace)
            else:checks.append({'id':'bounded_workspace','status':'not_configured','required':False,
                                'reason':'A new controller requires bounded workspaces before it can dispatch tasks'})
            if config.get('npu'):
                def npu():
                    from npu_device import validate,fingerprint
                    validate(config['npu'],host=True)
                    return {'policy_sha256':fingerprint(config['npu']),'device_count':len(config['npu']['device_ids']),
                            'static_contract_only':True,'management_query':'not_run','acl_initialization':'not_run'}
                report['npu']=check('npu_static_contract',npu)
            else:checks.append({'id':'npu_static_contract','status':'not_configured','required':False,'reason':'CPU node; no NPU devices will be authorized'})
            if config.get('spool_storage'):
                from spool_storage import inspect_new
                report['spool_storage']=check('finite_node_spool',lambda:inspect_new(config,state or config['state_dir'],prefix))
            else:checks.append({'id':'finite_node_spool','status':'not_configured','required':False,'reason':'A new controller requires a finite spool before dispatching user work'})
            if config.get('persistent_cache'):
                from persistent_cache import Pool
                report['persistent_cache']=check('private_persistent_cache',lambda:Pool(state or config['state_dir'],config).inspect_new())
        elif node_config is None:checks.append({'id':'node_config','status':'not_checked','required':True,'reason':'Supply --node-config'})
    report['install_ready']=all(item['status']=='passed' for item in checks if item['required'])
    report['task_dispatch_prerequisites']=bool(report['install_ready'] and role=='node' and report.get('workspace'))
    report['versioned_workflows_ready']=bool(report['task_dispatch_prerequisites'] and any(item['id']=='certified_environment' and item['status']=='passed' for item in checks))
    report['blockers']=[item['id'] for item in checks if item['required'] and item['status']!='passed']
    return report

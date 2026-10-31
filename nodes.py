"""Controller-side node protocol. All admission and receipts use the central ledger."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid

from jobs import ACTIVE,TERMINAL,JobError,atomic_json,now,public
from container_engine import MAX_ARCHIVE,MAX_FILE,MAX_LOG,safe_artifact,SCRIPTS
from workflow_spec import WORKFLOWS,CAPABILITIES
from resources import validate_policy,MULTI_SLOT,MAX_SLOTS,MANAGED_OVERHEAD_MIB,memory_cost
from docker_runtime import CAPABILITY as PINNED_RUNTIME
from environment_contract import CAPABILITY as ENVIRONMENT,PROBE

REQUIRED_CAPABILITIES={'start-ticket-v1','cleanup-before-complete-v1'}


class NodeRegistry:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS worker_nodes (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE,
                profile TEXT NOT NULL, last_seen REAL NOT NULL DEFAULT 0, report TEXT NOT NULL DEFAULT '{}', disabled INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS worker_uploads (attempt TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,
                PRIMARY KEY(attempt,name));
              CREATE TABLE IF NOT EXISTS worker_node_events (id INTEGER PRIMARY KEY,at TEXT NOT NULL,node_id TEXT NOT NULL,
                actor TEXT NOT NULL,revision INTEGER NOT NULL,policy TEXT NOT NULL);
            ''')
        from device_qualification import Qualifications
        self.qualifications=Qualifications(jobs)
        from owner_handoff import Handoff
        self.handoff=Handoff(self)

    def enroll(self,profile):
        if not isinstance(profile,dict) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,47}',profile.get('name','')):raise JobError('Invalid node name')
        from io_policy import limits
        try:profile={**profile,'io_limits':limits(profile.get('io_limits'))}
        except ValueError as error:raise JobError(str(error)) from None
        if 'image' in profile and not re.fullmatch('sha256:[0-9a-f]{64}',str(profile['image'])):raise JobError('Node image must be immutable')
        if 'environment_setup' in profile and profile['environment_setup'] is not None:
            from environment_contract import immutable_path
            if not immutable_path(profile['environment_setup']):raise JobError('Environment setup must be an immutable image path')
        if profile.get('environment_manifest'):
            from environment_contract import verify_config
            try:verify_config(profile,host=False)
            except (ValueError,OSError):raise JobError('Invalid node environment manifest') from None
        policy=profile.get('policy',{})
        cpus=policy.get('cpu_ids')
        if not isinstance(cpus,list) or not cpus or any(isinstance(c,bool) or not isinstance(c,int) or c<0 for c in cpus) or len(cpus)!=len(set(cpus)):raise JobError('Invalid node CPU policy')
        for name in ['memory_mib','shm_mib','max_running']:
            if isinstance(policy.get(name),bool) or not isinstance(policy.get(name),int) or policy[name]<0:raise JobError('Invalid node budget')
        validate_policy(policy)
        if not re.fullmatch(r'[0-9a-f]{64}',profile.get('physical_id','')):raise JobError('Node physical identity must be pinned')
        if profile['physical_id']==self.jobs.backend.physical_id and not set(cpus)<=set(self.jobs.policy['cpu_ids']):raise JobError('Local worker CPUs exceed the controller resource pool')
        if not profile.get('users') or any(not self.jobs.access.enrollment_allowed(user) or not isinstance(mapping,dict) or any(isinstance(mapping.get(k),bool) or not isinstance(mapping.get(k),int) or mapping[k]<1000 for k in ['uid','gid']) for user,mapping in profile['users'].items()):raise JobError('Invalid non-root execution identity map')
        if profile['physical_id']==self.jobs.backend.physical_id and any(mapping['uid']==self.controller_uid() for mapping in profile['users'].values()):
            raise JobError('Local user tasks cannot execute as the controller service UID')
        if set(profile.get('templates',[]))-set(SCRIPTS) or not profile.get('templates'):raise JobError('Unknown node template')
        if profile.get('npu'):
            from npu_device import validate
            try:validate(profile['npu'])
            except ValueError as error:raise JobError(str(error)) from None
        if profile.get('workspace'):
            from workspace_slots import validate
            try:validate(profile['workspace'])
            except ValueError as error:raise JobError(str(error)) from None
            if policy['max_running']>len(profile['workspace']['slots']):raise JobError('Node concurrency exceeds dedicated workspace slots')
        if profile.get('persistent_cache'):
            from persistent_cache import validate,aliases
            try:
                validate(profile['persistent_cache'],profile['users'])
                aliases(profile,{k:profile[k] for k in ['workspace','spool_storage'] if profile.get(k)})
                if profile['physical_id']==self.jobs.backend.physical_id:
                    devices={self.jobs.runtime.stat().st_dev}
                    if self.jobs.payload.policy:devices.add(self.jobs.payload.policy['device'])
                    if devices&{s['device'] for s in profile['persistent_cache']['slots']}:
                        raise ValueError('Cache cannot share central control or payload filesystems')
            except ValueError as error:raise JobError(str(error),409) from None
        if profile.get('spool_storage'):
            from spool_storage import validate
            try:
                validate(profile['spool_storage'])
                if any(s['device']==profile['spool_storage']['device'] for s in profile.get('workspace',{}).get('slots',[])):
                    raise ValueError('Node spool cannot share a user workspace filesystem')
                if profile['physical_id']==self.jobs.backend.physical_id:
                    devices={self.jobs.runtime.stat().st_dev}
                    if self.jobs.payload.policy:devices.add(self.jobs.payload.policy['device'])
                    if profile['spool_storage']['device'] in devices:raise ValueError('Node spool cannot share central control or payload storage')
            except ValueError as error:raise JobError(str(error),409) from None
        profile={**profile,'policy_revision':1}
        node_id='worker-'+uuid.uuid4().hex[:12];key=secrets.token_urlsafe(36)
        with self.jobs.transaction() as db:
            self.jobs.resources.validate_node(profile,db)
            held={j.get('node_id') for j in self.jobs.all(db) if j['state'] in ACTIVE}|self.handoff.held_nodes(db)
            for old_id,raw,disabled in db.execute('SELECT id,profile,disabled FROM worker_nodes'):
                old=self.handoff.reserved_profile(old_id,json.loads(raw),db)
                if old['physical_id']!=profile['physical_id'] or disabled and old_id not in held:continue
                from persistent_cache import aliases
                try:aliases(profile,old)
                except ValueError as error:raise JobError(str(error),409) from None
                for current,peer in [(profile,old),(old,profile)]:
                    spool=current.get('spool_storage')
                    if not spool:continue
                    other=peer.get('spool_storage')
                    if other and any(spool[key]==other[key] for key in ['device','path','storage_id']):
                        raise JobError('Finite node spool is already assigned on this physical host',409)
                    if any(s['device']==spool['device'] for s in peer.get('workspace',{}).get('slots',[])):
                        raise JobError('Finite node spool overlaps another user workspace',409)
            if profile.get('workspace'):
                from quota_workspace import sharing,capacity
                combined=list(profile['workspace']['slots'])
                held={j.get('node_id') for j in self.jobs.all(db) if j['state'] in ACTIVE}|self.handoff.held_nodes(db)
                for old_id,raw,disabled in db.execute('SELECT id,profile,disabled FROM worker_nodes'):
                    old=self.handoff.reserved_profile(old_id,json.loads(raw),db)
                    if old['physical_id']!=profile['physical_id'] or disabled and old_id not in held:continue
                    combined.extend(old.get('workspace',{}).get('slots',[]))
                    for a in profile['workspace']['slots']:
                        for b in old.get('workspace',{}).get('slots',[]):
                            if a['storage_id']==b['storage_id'] or a['device']==b['device'] and not sharing(a,b) or a['path']==b['path']:
                                raise JobError('Workspace filesystem is already assigned to another node',409)
                try:capacity(combined)
                except ValueError as error:raise JobError(str(error),409) from None
            db.execute('INSERT INTO worker_nodes(id,key_hash,profile) VALUES(?,?,?)',
                       (node_id,hashlib.sha256(key.encode()).hexdigest(),json.dumps(profile)))
            db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
                       (now(),node_id,self.jobs.owner,1,json.dumps(policy)))
        return node_id,key

    def set_policy(self,node_id,policy,expected_revision):
        policy=validate_policy(policy)
        with self.jobs.transaction() as db:
            profile=self.node(node_id,db)['profile']
            self.handoff.mutable(node_id,db)
            revision=profile.get('policy_revision',1)
            if type(expected_revision) is not int or expected_revision!=revision:raise JobError('Node policy revision changed; inspect before retrying',409)
            if any(job['state'] in ACTIVE and job.get('node_id')==node_id for job in self.jobs.all(db)):
                raise JobError('Stop admissions and finish or reconcile every node attempt before changing policy',409)
            profile={**profile,'policy':policy,'policy_revision':revision+1}
            if profile.get('workspace') and policy['max_running']>len(profile['workspace']['slots']):raise JobError('Node concurrency exceeds workspace slots')
            self.jobs.resources.validate_node(profile,db)
            db.execute('UPDATE worker_nodes SET profile=?,last_seen=0,report=? WHERE id=?',(json.dumps(profile),'{}',node_id))
            db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
                       (now(),node_id,self.jobs.owner,revision+1,json.dumps(policy)))
            return profile

    def policy_events(self,node_id):
        with self.jobs.connect() as db:
            return [{'at':row[0],'actor':row[1],'revision':row[2],'policy':json.loads(row[3])} for row in db.execute(
                'SELECT at,actor,revision,policy FROM worker_node_events WHERE node_id=? ORDER BY id DESC LIMIT 100',(node_id,))]

    def certify_environment(self,node_id,job_id,expected_revision):
        from environment_contract import from_receipt,fingerprint
        from archive_api import open_artifact
        with self.jobs.transaction() as db:
            node=self.node(node_id,db);profile=node['profile'];revision=profile.get('policy_revision',1)
            self.handoff.mutable(node_id,db)
            if type(expected_revision) is not int or expected_revision!=revision:raise JobError('Node revision changed',409)
            if any(job['state'] in ACTIVE and job.get('node_id')==node_id for job in self.jobs.all(db)):
                raise JobError('Finish or reconcile every node attempt before certifying its environment',409)
            job=self.jobs.get(job_id,db)
            self.jobs.lifecycle.available('archive',job_id,db)
            if (job['state']!='succeeded' or job.get('template')!=PROBE or job.get('node_id')!=node_id
                or job.get('node_policy_revision')!=revision):raise JobError('A current successful fixed probe from this node is required',409)
            def evidence(name):
                with open_artifact(self.jobs.directory(job_id),name,max_file=256*1024) as source:return json.load(source)
            try:manifest=from_receipt(evidence('results/_lab/environment.json'),evidence('environment.json'),job,revision)
            except (OSError,ValueError,KeyError,TypeError):raise JobError('Protected probe evidence is incomplete or invalid',409) from None
            if (time.time()-node['last_seen']>30 or manifest['image_sha256']!=node['report'].get('image_sha256')
                or manifest['environment_setup']!=node['report'].get('environment_setup')
                or manifest['observed']['python_executable']!=node['report'].get('python_executable')):
                raise JobError('Current node image/setup does not match the completed probe',409)
            profile={**profile,'image':manifest['image_sha256'],'environment_setup':manifest['environment_setup'],
                     'python':manifest['observed']['python_executable'],
                     'environment_manifest':manifest,'policy_revision':revision+1}
            db.execute('UPDATE worker_nodes SET profile=?,last_seen=0,report=? WHERE id=?',(json.dumps(profile),'{}',node_id))
            db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
                (now(),node_id,self.jobs.owner,revision+1,json.dumps({'kind':'environment-certified','sha256':fingerprint(manifest),'source_job':job_id})))
            return profile

    def authenticate(self,header):
        if not isinstance(header,str) or not header.startswith('Bearer ') or len(header)>1024:return None
        with self.jobs.connect() as db:
            row=db.execute('SELECT id FROM worker_nodes WHERE key_hash=? AND disabled=0',
                           (hashlib.sha256(header[7:].encode()).hexdigest(),)).fetchone()
        return row[0] if row else None

    def disable(self,node_id):
        with self.jobs.connect() as db:
            db.execute('UPDATE worker_nodes SET disabled=1 WHERE id=?',(node_id,))

    def set_templates(self,node_id,templates):
        if not isinstance(templates,list) or not templates or set(templates)-set(SCRIPTS):raise JobError('Unknown node template')
        with self.jobs.transaction() as db:
            profile=self.node(node_id,db)['profile'];profile['templates']=sorted(set(templates))
            self.handoff.mutable(node_id,db)
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),node_id))

    def rotate_key(self,node_id):
        key=secrets.token_urlsafe(36)
        with self.jobs.connect() as db:
            if not db.execute('SELECT id FROM worker_nodes WHERE id=?',(node_id,)).fetchone():raise JobError('Unknown node',404)
            db.execute('UPDATE worker_nodes SET key_hash=?,disabled=0,last_seen=0,report=? WHERE id=?',
                       (hashlib.sha256(key.encode()).hexdigest(),'{}',node_id))
        return key

    def node(self,node_id,db=None):
        if db is None:
            with self.jobs.connect() as connection:return self.node(node_id,connection)
        row=db.execute('SELECT profile,last_seen,report,disabled FROM worker_nodes WHERE id=?',(node_id,)).fetchone()
        if not row or row[3]:raise JobError('Node is not enrolled',403)
        return {'id':node_id,'profile':json.loads(row[0]),'last_seen':row[1],'report':json.loads(row[2]),
                'device_qualifications':self.qualifications.records(node_id,db)}

    def heartbeat(self,node_id,body):
        profile=self.node(node_id)['profile']
        from io_policy import limits,CAPABILITY
        if limits(body.get('io_limits'))!=limits(profile.get('io_limits')):raise JobError('Node IO policy differs from enrollment',409)
        if type(body.get('policy_revision',1)) is not int or body.get('policy_revision',1)!=profile.get('policy_revision',1):raise JobError('Node policy revision differs; install the current private node configuration',409)
        if body.get('protocol_version')!=1 or not REQUIRED_CAPABILITIES<=set(body.get('capabilities',[])):raise JobError('Node requires start-ticket-v1 and cleanup-before-complete-v1',409)
        if profile['policy']['max_running']>1 and MULTI_SLOT not in body.get('capabilities',[]):raise JobError('Concurrent node requires concurrent-attempts-v1',409)
        if body.get('physical_id')!=profile['physical_id']:raise JobError('Node physical identity mismatch',403)
        if not set(profile['policy']['cpu_ids'])<=set(body.get('cpu_ids',[])):raise JobError('Enrolled CPUs are not visible on node',409)
        report={}
        for key in ['memory_available_bytes','disk_free_bytes']:
            value=body.get(key)
            if isinstance(value,bool) or not isinstance(value,int) or value<0:raise JobError('Invalid node resource observation')
            report[key]=value
        report['sampled_at']=now()
        from workflow_spec import DEADLINE_CAPABILITY,MAX_TIMEOUT_SECONDS
        if DEADLINE_CAPABILITY in body.get('capabilities',[]):
            maximum=body.get('max_timeout_seconds')
            if type(maximum) is not int or not 1<=maximum<=MAX_TIMEOUT_SECONDS:raise JobError('Invalid node execution timeout limit')
            report['max_timeout_seconds']=maximum
        cpu=body.get('host_cpu_utilization_pct')
        if cpu is not None and (isinstance(cpu,bool) or not isinstance(cpu,(int,float)) or not math.isfinite(cpu) or not 0<=cpu<=100):raise JobError('Invalid node CPU observation')
        report['host_cpu_utilization_pct']=cpu
        report['capabilities']=sorted((REQUIRED_CAPABILITIES|CAPABILITIES|{MULTI_SLOT})&set(body.get('capabilities',[])))
        if DEADLINE_CAPABILITY in body.get('capabilities',[]):report['capabilities'].append(DEADLINE_CAPABILITY)
        from node_storage import CAPABILITY as SPOOL
        if SPOOL in body.get('capabilities',[]):
            if type(body.get('disk_free_inodes')) is not int or body['disk_free_inodes']<0 or type(body.get('artifact_gc_ready')) is not bool:
                raise JobError('Invalid node spool observation')
            report.update(disk_free_inodes=body['disk_free_inodes'],artifact_gc_ready=body['artifact_gc_ready'])
            report['capabilities'].append(SPOOL)
        if profile.get('spool_storage'):
            from spool_storage import CAPABILITY as FINITE_SPOOL,observation
            if FINITE_SPOOL not in body.get('capabilities',[]):raise JobError('Node must upgrade finite-node-spool-v1',409)
            try:report['finite_spool']=observation(body.get('spool_observation'),profile['spool_storage'])
            except ValueError as error:raise JobError(str(error),409) from None
            if (body.get('disk_free_bytes'),body.get('disk_free_inodes'))!=(report['finite_spool']['free_bytes'],report['finite_spool']['free_inodes']):
                raise JobError('Finite spool availability cannot be replaced by host free space',409)
            report['capabilities'].append(FINITE_SPOOL)
        if ENVIRONMENT in body.get('capabilities',[]):
            from environment_contract import immutable_path,fingerprint
            image=body.get('image_sha256');setup=body.get('environment_setup')
            executable=body.get('python_executable')
            if not re.fullmatch('sha256:[0-9a-f]{64}',str(image)) or setup is not None and not immutable_path(setup) or not immutable_path(executable):raise JobError('Invalid node environment identity')
            if profile.get('image') and image!=profile['image']:raise JobError('Node image differs from enrollment',409)
            if profile.get('environment_manifest'):
                if (setup!=profile.get('environment_setup') or body.get('environment_sha256')!=fingerprint(profile['environment_manifest'])
                    or executable!=profile['environment_manifest']['observed']['python_executable']):raise JobError('Node certified environment differs',409)
            report.update(image_sha256=image,environment_setup=setup,python_executable=executable,environment_sha256=body.get('environment_sha256'))
            report['capabilities'].append(ENVIRONMENT)
        from environment_contract import PROBE_MEMORY_CAPABILITY
        if PROBE_MEMORY_CAPABILITY in body.get('capabilities',[]):report['capabilities'].append(PROBE_MEMORY_CAPABILITY)
        if PINNED_RUNTIME in body.get('capabilities',[]):report['capabilities'].append(PINNED_RUNTIME)
        if CAPABILITY in body.get('capabilities',[]):report['capabilities'].append(CAPABILITY)
        if profile.get('npu'):
            from npu_device import observation,CAPABILITY
            if CAPABILITY not in body.get('capabilities',[]):raise JobError('NPU node requires device-whitelist capability',409)
            try:report['npu']=observation(body.get('npu_observation'),profile['npu'])
            except ValueError as error:raise JobError(str(error),409) from None
            report['capabilities'].append(CAPABILITY)
            from device_qualification import CAPABILITY as ACCESS
            if ACCESS in body.get('capabilities',[]):
                from workflow_spec import runtime_manifest
                if body.get('access_runtime_files')!=runtime_manifest():raise JobError('Node device qualification runtime differs; upgrade before new device work',409)
                host_sha=body.get('access_host_sha256')
                if host_sha is not None and not re.fullmatch('[0-9a-f]{64}',str(host_sha)):raise JobError('Invalid node device access host identity')
                report.update(access_runtime_files=body['access_runtime_files'],access_host_sha256=host_sha)
                report['capabilities'].append(ACCESS)
        if profile.get('workspace'):
            from workspace_slots import observation,CAPABILITY
            if CAPABILITY not in body.get('capabilities',[]):raise JobError('Workspace node requires filesystem-workspace-v1',409)
            try:report['workspace']=observation(body.get('workspace_observation'),profile['workspace'])
            except ValueError as error:raise JobError(str(error),409) from None
            report['capabilities'].append(CAPABILITY)
            from quota_workspace import CAPABILITY as UID_QUOTA,is_quota
            if any(is_quota(s) for s in profile['workspace']['slots']):
                if UID_QUOTA not in body.get('capabilities',[]):raise JobError('UID quota node requires uid-quota-workspace-v1',409)
                report['capabilities'].append(UID_QUOTA)
        if profile.get('persistent_cache'):
            from persistent_cache import observation,CAPABILITY
            if CAPABILITY not in body.get('capabilities',[]):raise JobError('Cache node requires private-persistent-cache-v1',409)
            try:report['persistent_cache']=observation(body.get('cache_observation'),profile['persistent_cache'])
            except ValueError as error:raise JobError(str(error),409) from None
            report['capabilities'].append(CAPABILITY)
        from attempt_inventory import CAPABILITY as INVENTORY
        if self.jobs.policy.get('require_attempt_inventory') and INVENTORY not in body.get('capabilities',[]):
            raise JobError('Node requires attempt-inventory-v1 before confirming availability',409)
        with self.handoff.transaction() as (db,identities):
            if INVENTORY in body.get('capabilities',[]):
                try:report['attempt_inventory']=self.jobs.inventory.review(node_id,profile['physical_id'],body.get('attempt_inventory'),db)
                except ValueError as error:raise JobError(str(error),409) from None
                report['capabilities'].append(INVENTORY)
            updated=db.execute('UPDATE worker_nodes SET last_seen=?,report=? WHERE id=? AND profile=? AND disabled=0',
                (time.time(),json.dumps(report),node_id,json.dumps(profile)))
            if updated.rowcount!=1:raise JobError('Node configuration changed while validating heartbeat; retry with current configuration',409)
            self.handoff.observe(node_id,profile,report,db,identities)
        return {'node_id':node_id,'accepted':True}

    def _grant(self,job):
        return {'version':1,'node_id':job['node_id'],'physical_id':job['_physical_id'],
                'job_id':job['id'],'attempt_id':job['attempt_id'],'lease_token':job['_token'],
                'owner':job['owner'],'project':job['project'],'environment':job['environment'],
                'template':job['template'],'parameters':job['parameters'],
                'source_sha256':job['source']['sha256'],'resources':job['resources'],
                'artifact_limits':job.get('artifact_limits',{'file_mib':64,'total_mib':128}),
                'allocation':job['allocation'],'timeout_seconds':job['timeout_seconds'],
                **({'input':job['input']} if job.get('input') else {}),
                **({'runtime_files':job['runtime_files']} if job.get('runtime_files') else {}),
                **({'environment_sha256':job['environment_sha256']} if job.get('environment_sha256') else {}),
                'node_policy_revision':job.get('node_policy_revision',1),
                **({'npu_policy_sha256':job['_npu_policy_sha256']} if job.get('_npu_policy_sha256') else {}),
                **({'npu_access_context_sha256':job['_npu_access_context']} if job.get('_npu_access_context') else {}),
                **({'workspace_policy_sha256':job['_workspace_policy_sha256']} if job.get('_workspace_policy_sha256') else {}),
                **({'spool_policy_sha256':job['_spool_policy_sha256']} if job.get('_spool_policy_sha256') else {}),
                **({'cache_policy_sha256':job['_cache_policy_sha256']} if job.get('_cache_policy_sha256') else {}),
                **({'start_ticket_required':True} if job.get('_requires_start_ticket') else {})}

    def controller_uid(self):return os.geteuid()

    def probe_submission(self,job,principal,db):
        if job.get('template') not in {'environment_probe','npu_access_probe'}:return
        node=self.node(job['parameters']['node_id'],db);profile=node['profile']
        if principal.id not in profile['users']:raise JobError('Selected fixed-probe Node does not map this owner',403)
        if job['template'] not in profile['templates']:raise JobError('Selected Node must explicitly authorize this fixed probe',403)
        if job['template']=='npu_access_probe' and job['parameters']['device_id'] not in profile.get('npu',{}).get('device_ids',[]):
            raise JobError('Selected device is outside this Node fixed-probe contract',403)
        from resources import require_managed_capacity
        require_managed_capacity(profile,self.jobs.resources.get(profile['physical_id'],db),job['resources']['memory_mib'])

    def placement(self,job,node,domain,active,db=None,identities=None):
        profile=node['profile'];node_id=node['id']
        reason=self.jobs.dispatch_permission(job,active,db,identities) or self.jobs.dependencies.reason(job)
        identity_reason=('用户任务不能使用同机中央服务UID，需管理员重新映射'
            if profile['physical_id']==self.jobs.backend.physical_id and profile['users'][job['owner']]['uid']==self.controller_uid() else None)
        inventory_reason=self.jobs.inventory.reason(profile['physical_id'],db)
        if job.get('template')=='npu_access_probe':
            from device_qualification import parameters
            target=parameters(job['parameters'])
            if target['node_id']!=node_id:reason='设备访问探针只可交给指定节点'
        if job.get('template')==PROBE:
            if job['parameters']['node_id']!=node_id:reason='环境探测只可交给指定节点'
            elif ENVIRONMENT not in node['report'].get('capabilities',[]):reason='节点需要升级环境探测能力'
            elif 'memory_mib' in job['parameters'] and __import__('environment_contract').PROBE_MEMORY_CAPABILITY not in node['report'].get('capabilities',[]):reason='节点需要升级大工作区环境探测能力'
        elif job.get('template') in WORKFLOWS:
            from environment_contract import differences,fingerprint,verify_config
            lock=job['parameters']['lock'];image=profile.get('image') or node['report'].get('image_sha256')
            if image and lock.get('image_sha256',image)!=image:reason='等待匹配任务镜像SHA的节点'
            manifest=profile.get('environment_manifest')
            if manifest:
                try:verify_config(profile,host=False);consistent=True
                except ValueError:consistent=False
                if not consistent:reason='节点登记的Python、镜像或脚本与环境证据不一致，需管理员重新认证'
                elif node['report'].get('environment_sha256')!=fingerprint(manifest):reason='等待节点确认认证环境'
                elif differences(lock,manifest):reason='等待匹配已锁定Python/包/工具/文件/架构的认证环境'
            elif self.jobs.policy.get('require_environment_contract'):reason='等待管理员认证该节点的环境清单'
            if self.jobs.policy.get('require_environment_contract') and job['resources']['npu'] and not lock.get('npu_name'):
                reason='设备工作流需要明确的NPU管理名称约束'
        if job.get('template') in WORKFLOWS and not CAPABILITIES<=set(node['report'].get('capabilities',[])):
            reason='节点需要升级固定输入、受保护环境和短路径归档能力'
        from workflow_spec import DEADLINE_CAPABILITY
        maximum=node['report'].get('max_timeout_seconds')
        if maximum is not None and job['timeout_seconds']>maximum:
            reason=reason or '节点执行时限不足，等待管理员调整或其他节点'
        elif job['timeout_seconds']>3600 and DEADLINE_CAPABILITY not in node['report'].get('capabilities',[]):
            reason=reason or '节点尚未确认长任务执行时限，需升级节点'
        group=[j for j in active if j.get('_physical_id',self.jobs.backend.physical_id)==profile['physical_id']]
        node_active=[j for j in group if j.get('node_id')==node_id]
        pool=domain['policy']
        cpus=[c for c in profile['policy']['cpu_ids'] if c in pool['cpu_ids'] and c not in {c for j in group for c in j['allocation'].get('cpu_ids',[])}]
        wanted=job['resources']
        from io_policy import for_job,fits,CAPABILITY
        contract=for_job(job)
        if not fits(contract,profile.get('io_limits')):reason='节点产物容量低于本任务冻结额度'
        if (contract['file_mib']>64 or contract['total_mib']>128) and CAPABILITY not in node['report']['capabilities']:
            reason='节点需要升级大产物分块与预算能力'
        if not reason and domain['draining']:reason='此物理资源域正在排空，暂停分配新任务'
        if not reason and (len(group)>=pool['max_running'] or len(node_active)>=profile['policy']['max_running']):reason='等待节点运行名额'
        if not reason and (len(cpus)<wanted['cpu'] or sum(memory_cost(j) for j in group)+memory_cost(job,True)>pool['memory_mib']):reason='等待节点 CPU/内存预算（含受控执行开销）'
        if not reason and sum(j['resources'].get('shm_mib',0) for j in group)+wanted.get('shm_mib',0)>pool['shm_mib']:reason='等待节点共享内存预算'
        if not reason and (sum(memory_cost(j) for j in node_active)+memory_cost(job,True)>profile['policy']['memory_mib'] or sum(j['resources'].get('shm_mib',0) for j in node_active)+wanted.get('shm_mib',0)>profile['policy']['shm_mib']):reason='等待此节点内存/共享内存预算（含受控执行开销）'
        if not reason and (node['report']['memory_available_bytes']<memory_cost(job,True)*1024**2+512*1024**2 or node['report']['disk_free_bytes']<512*1024**2):reason='节点实际内存或磁盘余量不足'
        from node_storage import pressure,CAPABILITY as SPOOL
        if not reason and self.jobs.policy.get('require_node_spool') and SPOOL not in node['report']['capabilities']:
            reason='节点需要升级暂存准入和已确认副本回收能力'
        if not reason:reason=pressure(job,node['report'],[j for j in group if j.get('_remote') and (not profile.get('spool_storage') or j.get('node_id')==node_id)])
        workspace=None;need=job.get('parameters',{}).get('workspace')
        if need is None and self.jobs.policy.get('require_bounded_workspace'):
            need={'min_mib':1,'min_inodes':1,'durable':False}
        if profile.get('workspace'):
            from workspace_slots import CAPABILITY,allocation
            if CAPABILITY not in node['report']['capabilities']:reason='节点需要升级工作区硬限额能力'
            held={j['allocation']['workspace']['storage_id'] for j in group if j.get('allocation',{}).get('workspace')}
            available={item['id']:item for item in node['report'].get('workspace',{}).get('slots',[])}
            for slot in sorted(profile['workspace']['slots'],key=lambda item:item['bytes']):
                from quota_workspace import is_quota
                if is_quota(slot) and slot['quota_uid']!=profile['users'][job['owner']]['uid']:continue
                reading=available.get(slot['id'],{})
                if slot['storage_id'] in held or reading.get('status')!='ok' or not reading.get('clean'):continue
                if slot['filesystem']=='tmpfs' and slot['bytes']>wanted['memory_mib']*1024**2:continue
                if job.get('input') and job.get('parameters',{}).get('input_mode','copy')=='copy' and reading['free_bytes']<job['input']['bytes']+1024**2:continue
                if need and (reading['free_bytes']<need['min_mib']*1024**2 or reading['free_inodes']<need['min_inodes'] or need['durable'] and slot['filesystem']=='tmpfs'):continue
                workspace=allocation(slot);break
            if not reason and workspace is None:reason='等待满足容量、inode和持久性要求的独占工作区'
        elif need and not reason:reason='此任务要求文件系统工作区硬限额，不能降级到普通目录'
        cache=None;cache_need=job.get('parameters',{}).get('cache')
        if cache_need:
            from persistent_cache import available,CAPABILITY
            selected=available(profile,job['owner'],node['report'].get('persistent_cache',{}),group)
            if not profile.get('environment_manifest'):reason=reason or '持久缓存需要管理员认证的精确环境'
            elif CAPABILITY not in node['report']['capabilities']:reason=reason or '节点未启用私有持久缓存能力'
            elif not selected:reason=reason or '等待本人可用的独占缓存租约，不能借用他人缓存'
            else:
                slot,reading=selected
                if reading['free_bytes']<cache_need['min_free_mib']*1024**2 or reading['free_inodes']<cache_need['min_free_inodes']:
                    reason=reason or '本人缓存字节或inode余量不足，等待管理员回收空闲版本'
                else:cache=slot
        npus=[]
        if wanted['npu']:
            from npu_device import fingerprint,CAPABILITY
            npu=node['report'].get('npu',{})
            if not profile.get('npu') or CAPABILITY not in node['report']['capabilities']:
                reason='此节点未开启受控 NPU 设备透传'
            elif wanted['npu']!=1:reason='此节点当前每任务支持一张 NPU'
            else:
                import datetime as dt
                try:fresh=0<=time.time()-dt.datetime.fromisoformat(npu.get('sampled_at')).timestamp()<=15
                except (TypeError,ValueError):fresh=False
                held={device for item in group for device in item['allocation'].get('npu_ids',[])}
                model=job.get('parameters',{}).get('lock',{}).get('npu_name')
                npus=[item['id'] for item in npu.get('devices',[]) if item['id'] not in held and item['health']=='OK' and item['utilization_pct']==0 and item['process_count']==0 and (not model or item.get('name')==model)]
                if job.get('template')=='npu_access_probe':
                    from device_qualification import decision
                    target=job['parameters']['device_id'];npus=[value for value in npus if value==target]
                    qualify=decision(node,job['owner'],target)
                    if qualify['context_sha256'] is None and not reason:reason=qualify['reason']
                elif job.get('template') in WORKFLOWS:
                    from device_qualification import decision
                    checked=[(value,decision(node,job['owner'],value)) for value in npus]
                    npus=[value for value,status in checked if status['status']=='verified']
                    if checked and not npus and not reason:reason=checked[0][1]['reason']
                if not reason and (npu.get('status')!='ok' or not fresh or not npus):reason='等待新鲜、健康且无占用的节点 NPU'
        try:self.jobs.payload.check()
        except JobError:reason='中央载荷文件系统不可用，暂停新执行且不释放原租约'
        spool_reason=None
        if profile.get('spool_storage'):
            if node['report'].get('finite_spool',{}).get('status')!='ok':spool_reason='节点有限暂存文件系统不可用，保留已有资源等待恢复'
        elif self.jobs.policy.get('require_finite_node_spool'):spool_reason='节点未启用有限磁盘暂存，等待管理员配置'
        return {'reason':identity_reason or inventory_reason or spool_reason or reason,'cpu_ids':cpus,'npu_ids':npus,'workspace':workspace,'cache':cache}

    def claim(self,node_id,body=None):
        from unit_start import admission_reason
        reason=admission_reason(self.jobs.runtime)
        if reason:return {'grant':None,'reason':reason}
        body={} if body is None else body
        excluded=body.get('exclude_attempts',[])
        if (not isinstance(excluded,list) or len(excluded)>MAX_SLOTS
            or any(not isinstance(item,str) or not re.fullmatch(r'attempt-[0-9a-f]{32}',item) for item in excluded)
            or len(excluded)!=len(set(excluded))):raise JobError('Invalid active attempt exclusions')
        with self.handoff.transaction() as (db,identities):
            reason=admission_reason(self.jobs.runtime)
            if reason:return {'grant':None,'reason':reason}
            node=self.node(node_id,db);profile=node['profile']
            if time.time()-node['last_seen']>30:raise JobError('Node heartbeat is stale',409)
            if not REQUIRED_CAPABILITIES<=set(node['report'].get('capabilities',[])):raise JobError('Node capabilities require an upgrade',409)
            if profile['policy']['max_running']>1 and MULTI_SLOT not in node['report'].get('capabilities',[]):raise JobError('Concurrent node capabilities require an upgrade',409)
            rows=self.jobs.all(db)
            active=[job for job in rows if job['state'] in ACTIVE]
            assigned=[job for job in active if job.get('_remote') and job.get('node_id')==node_id]
            recovery=next((job for job in assigned if job['attempt_id'] not in excluded),None)
            if recovery:return {'grant':self._grant(recovery)}
            from control_storage import HOLD as CONTROL_HOLD
            if self.jobs.control_storage()['blocks_new_work']:return {'grant':None,'reason':CONTROL_HOLD}
            from audit_retention import HOLD as AUDIT_HOLD
            if self.jobs.access.audit_status()['blocks_new_work']:return {'grant':None,'reason':AUDIT_HOLD}
            if self.handoff.pending(node_id,db):return {'grant':None,'reason':'Node owner handoff awaits durable central readiness'}
            if self.jobs.policy.get('require_pinned_docker') and PINNED_RUNTIME not in node['report'].get('capabilities',[]):
                raise JobError('Node must upgrade to pinned-docker-runtime-v1 before receiving new work',409)
            if self.jobs.policy.get('require_environment_contract') and (ENVIRONMENT not in node['report'].get('capabilities',[]) or not profile.get('image')):
                raise JobError('Node needs an enrolled immutable image and environment-admission-v1 before new work',409)
            domain=self.jobs.resources.get(profile['physical_id'],db)
            from queue_policy import ordered
            queued=[j for j in ordered(rows,self.jobs.policy) if j['_environment']['type']=='worker']
            for job in queued:
                if job['owner'] not in profile['users'] or job.get('template') not in profile['templates']:continue
                from queue_policy import protected,note_wait,WAIT_REASON
                placement=self.placement(job,node,domain,active,db,identities)
                reason=placement['reason'];wanted=job['resources']
                if reason:
                    idle=self.placement(job,node,domain,[],db,identities)
                    note_wait(self.jobs,job,node_id,domain,profile.get('policy_revision',1),db,
                              not idle['reason'] and not self.jobs.dispatch_permission(job,active,db,identities))
                else:
                    guard=protected(self.jobs,rows,profile['physical_id'],db,identities)
                    if guard and guard['id']!=job['id']:reason=WAIT_REASON
                if reason:
                    job['reason']=reason;self.jobs.write(db,job);continue
                cpus=placement['cpu_ids'];npus=placement['npu_ids'];workspace=placement['workspace']
                job.update(state='reserved',reason='节点已领取任务，等待受控容器启动',node_id=node_id,
                    node_policy_revision=profile.get('policy_revision',1),
                    execution_backend='managed-docker',
                    attempt_id='attempt-'+uuid.uuid4().hex,_token=secrets.token_hex(24),_remote=True,
                    _physical_id=profile['physical_id'],_worker_seen=time.time(),_cancel_requested=False,
                    _requires_start_ticket=True,_start_authorized=False,
                    reserved_at=now(),_reserved_epoch=time.time(),_launch_attempted=True,
                    allocation={'cpu_ids':cpus[:wanted['cpu']],'memory_mib':wanted['memory_mib'],'execution_overhead_mib':MANAGED_OVERHEAD_MIB,
                                'shm_mib':wanted.get('shm_mib',0),'npu_ids':npus[:wanted['npu']]})
                if profile.get('environment_manifest') and job.get('template') in WORKFLOWS:
                    from environment_contract import fingerprint as environment_fingerprint
                    job['environment_sha256']=environment_fingerprint(profile['environment_manifest'])
                if wanted['npu'] and npus:
                    job['allocation']['npu_name']=next(item.get('name','UNKNOWN') for item in node['report']['npu']['devices'] if item['id']==npus[0])
                if workspace:
                    from workspace_slots import fingerprint as workspace_fingerprint
                    job['allocation']['workspace']=workspace;job['_workspace_policy_sha256']=workspace_fingerprint(profile['workspace'])
                if profile.get('spool_storage'):
                    from spool_storage import fingerprint as spool_fingerprint
                    job['_spool_policy_sha256']=spool_fingerprint(profile['spool_storage'])
                if wanted['npu']:
                    from npu_device import fingerprint
                    job['_npu_policy_sha256']=fingerprint(profile['npu'])
                    if job.get('template')=='npu_access_probe' or job.get('template') in WORKFLOWS:
                        from device_qualification import decision
                        job['_npu_access_context']=decision(node,job['owner'],npus[0])['context_sha256']
                        job['_npu_access_image']=profile['image']
                if placement['cache']:
                    from persistent_cache import fingerprint,namespace
                    slot=placement['cache'];job['_cache_policy_sha256']=fingerprint(profile['persistent_cache'])
                    context={**job,'npu_policy_sha256':job.get('_npu_policy_sha256')}
                    job['allocation']['cache']={'slot_id':slot['id'],'storage_id':slot['storage_id'],'key':namespace(profile,context,host=False)}
                db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',
                    (job['attempt_id'],job['id'],node_id,'reserved',job['reserved_at']))
                self.jobs.write(db,job)
                return {'grant':self._grant(job)}
            return {'grant':None}

    def _owned(self,node_id,body,db):
        job=self.jobs.get(body.get('job_id'),db)
        if not job.get('_remote') or job.get('node_id')!=node_id or body.get('attempt_id')!=job.get('attempt_id') or not secrets.compare_digest(str(body.get('lease_token','')),job['_token']):
            raise JobError('Execution grant does not belong to this node or attempt',403)
        return job

    def authorize_start(self,node_id,body):
        with self.handoff.transaction() as (db,identities):
            job=self._owned(node_id,body,db)
            if job.get('_start_authorized'):
                return {'start_permitted':False,'already_authorized':True,'cancel':bool(job.get('_cancel_requested'))}
            if job['state'] in TERMINAL or job.get('_cancel_requested'):
                return {'start_permitted':False,'already_authorized':False,'cancel':True}
            inventory_reason=self.jobs.inventory.reason(job['_physical_id'],db)
            if inventory_reason:
                job['reason']=inventory_reason;job['_worker_seen']=time.time();self.jobs.write(db,job)
                return {'start_permitted':False,'already_authorized':False,'wait':True,'reason':inventory_reason}
            reason=self.jobs.dispatch_permission(job,[j for j in self.jobs.all(db) if j['state'] in ACTIVE and j['id']!=job['id']],db,identities)
            if reason:
                from unit_start import HOLD,OWNER_HOLD
                from control_storage import HOLD as CONTROL_HOLD
                from audit_retention import HOLD as AUDIT_HOLD
                if reason in {HOLD,OWNER_HOLD,CONTROL_HOLD,AUDIT_HOLD}:
                    job['reason']=reason;job['_worker_seen']=time.time();self.jobs.write(db,job)
                    return {'start_permitted':False,'already_authorized':False,'wait':True,'reason':reason}
                return {'start_permitted':False,'already_authorized':False,'reason':reason}
            job['_start_authorized']=True;job['_worker_seen']=time.time();self.jobs.write(db,job)
            return {'start_permitted':True,'already_authorized':False}

    def input_chunk(self,node_id,body):
        with self.jobs.transaction() as db:
            job=self._owned(node_id,body,db)
            if job['state'] in TERMINAL or not job.get('input'):raise JobError('No active assigned input',409)
            bundle=self.jobs.inputs.get(job['input']['id'],db=db,owner=job['owner'])
            if bundle['manifest_sha256']!=job['input']['manifest_sha256']:raise JobError('Assigned input manifest differs',409)
            result=self.jobs.inputs.chunk(bundle,body)
            job['_worker_seen']=time.time();self.jobs.write(db,job)
            return result

    def update(self,node_id,body):
        with self.jobs.transaction() as db:
            job=self._owned(node_id,body,db)
            if job['state'] in TERMINAL:return {'terminal':True,'cancel':False,'state':job['state']}
            job['_worker_seen']=time.time()
            if body.get('attention') is True:
                job.update(state='needs_attention',reason='节点无法确认执行状态；保留资源，不自动重跑')
                if body.get('output_pending') is True:
                    job['output_pending']=True
                    job['reason']='节点暂存或产物收集待恢复；保留原尝试和工作区，不重复执行'
                    if body.get('execution_ended') is True:job['execution_ended']=True
                if body.get('cache_pending') is True:
                    job['cache_pending']=True;job['reason']='节点缓存待恢复；保留原许可，不降级或重复执行'
            if body.get('running') is True and body.get('attention') is not True:
                job.pop('output_pending',None);job.pop('execution_ended',None);job.pop('cache_pending',None)
                if not job.get('_cancel_requested'):job.update(state='running',reason='受控节点容器运行中')
                if not job.get('started_at'):job['started_at']=now()
            preview=body.get('logs')
            if isinstance(preview,dict):
                job['_log_preview']={**job.get('_log_preview',{}),**{key:{'text':str(value.get('text',''))[-8192:],'bytes':min(max(0,int(value.get('bytes',0))),MAX_LOG),'truncated':bool(value.get('truncated'))}
                    for key,value in preview.items() if key in {'stdout','stderr'} and isinstance(value,dict)}
                }
            self.jobs.write(db,job)
            return {'terminal':False,'cancel':bool(job.get('_cancel_requested')),'state':job['state']}

    def upload(self,node_id,body):
        name=body.get('name')
        if name not in {'stdout.log','stderr.log','environment.json'} and not safe_artifact(name):raise JobError('Artifact name is not allowed')
        offset=body.get('offset')
        if isinstance(offset,bool) or not isinstance(offset,int) or offset<0:raise JobError('Invalid upload offset')
        try:data=base64.b64decode(body.get('data',''),validate=True)
        except (ValueError,TypeError):raise JobError('Invalid artifact chunk')
        if not 0<len(data)<=256*1024:raise JobError('Artifact chunk exceeds limit')
        with self.jobs.transaction() as db:
            job=self._owned(node_id,body,db)
            from io_policy import for_job,MIB
            budget=for_job(job)
            limit=MAX_LOG if name in {'stdout.log','stderr.log'} else budget['file_mib']*MIB
            if offset+len(data)>limit:raise JobError('Artifact exceeds per-file budget')
            if job['state'] in TERMINAL:raise JobError('Attempt is already terminal',409)
            row=db.execute('SELECT size FROM worker_uploads WHERE attempt=? AND name=?',(job['attempt_id'],name)).fetchone()
            size=row[0] if row else 0
            path=self.jobs.directory(job['id'])/name
            if path.is_symlink() or not path.resolve().is_relative_to(self.jobs.directory(job['id']).resolve()):raise JobError('Invalid artifact path')
            path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            if offset<size:
                with path.open('rb') as file:file.seek(offset);previous=file.read(len(data))
                if offset+len(data)>size or previous!=data:raise JobError('Conflicting upload retry',409)
                return {'size':size,'reused':True}
            if offset!=size:raise JobError('Upload offset is not contiguous',409)
            total,count=db.execute('SELECT COALESCE(SUM(size),0),COUNT(*) FROM worker_uploads WHERE attempt=?',(job['attempt_id'],)).fetchone()
            if total+len(data)>budget['total_mib']*MIB or count+(0 if row else 1)>2000:raise JobError('Attempt archive budget exceeded')
            from storage_io import write_chunk
            write_chunk(path,size,data,root=self.jobs.payload.base)
            db.execute('INSERT OR REPLACE INTO worker_uploads VALUES(?,?,?)',(job['attempt_id'],name,size+len(data)))
            job['_worker_seen']=time.time();self.jobs.write(db,job)
            return {'size':size+len(data),'reused':False}

    def complete(self,node_id,body):
        with self.handoff.transaction() as (db,identities):
            job=self._owned(node_id,body,db)
            if job['state'] in TERMINAL:return {'accepted':True,'state':job['state'],'reused':True,'artifact_receipt':job.get('_artifact_receipt')}
            result=body.get('result',{})
            if not isinstance(result,dict) or result.get('status') not in TERMINAL|{'needs_attention'}:raise JobError('Invalid attempt result')
            if result['status'] in TERMINAL and result.get('termination_confirmed') is not True:raise JobError('No confirmed task termination',409)
            if result['status'] in TERMINAL and job.get('_requires_start_ticket') and result.get('execution_cleanup_confirmed') is not True:raise JobError('Node execution cleanup has not completed',409)
            if result['status'] in TERMINAL and job['resources']['npu'] and result.get('npu_release_confirmed') is not True:raise JobError('Node has not confirmed NPU release',409)
            if result['status'] in TERMINAL and job['allocation'].get('workspace') and result.get('workspace_release_confirmed') is not True:raise JobError('Node has not confirmed workspace cleanup and release',409)
            if result['status'] in TERMINAL and job['allocation'].get('cache') and result.get('cache_release_confirmed') is not True:raise JobError('Node has not confirmed persistent cache lease release',409)
            if result['status']=='succeeded' and (result.get('exit_code')!=0 or result.get('oom_killed')):raise JobError('Success conflicts with container exit evidence',409)
            if result['status']=='succeeded' and job.get('_requires_start_ticket') and not job.get('_start_authorized'):raise JobError('Task never received a start ticket',409)
            files=body.get('artifacts')
            if not isinstance(files,list) or len(files)>2000:raise JobError('Missing artifact manifest')
            from node_storage import manifest_sha256
            try:manifest_sha256(files)
            except ValueError:raise JobError('Invalid artifact checksum manifest') from None
            uploaded={row[0]:row[1] for row in db.execute('SELECT name,size FROM worker_uploads WHERE attempt=?',(job['attempt_id'],))}
            if {entry.get('name') for entry in files}!=set(uploaded):raise JobError('Artifact manifest is incomplete',409)
            for entry in files:
                path=self.jobs.directory(job['id'])/entry['name']
                digest=hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
                if entry.get('size')!=uploaded[entry['name']] or digest.hexdigest()!=entry.get('sha256'):raise JobError('Artifact checksum mismatch',409)
            if result['status']=='succeeded' and 'environment.json' not in uploaded:raise JobError('Missing execution environment evidence')
            allowed={'status','exit_code','started_at','finished_at','reason','termination_confirmed','execution_cleanup_confirmed','execution_finished_at','oom_killed','failure_stage','execution_status','npu_release_confirmed','npu_release_observation','npu_release_basis','workspace_release_confirmed','cache_release_confirmed'}
            result={key:value for key,value in result.items() if key in allowed}
            if not job['resources']['npu']:
                result.pop('npu_release_confirmed',None);result.pop('npu_release_observation',None);result.pop('npu_release_basis',None)
            elif result.get('npu_release_observation') is not None:
                from npu_admission import selected_reading
                record=result['npu_release_observation']
                try:
                    if record['policy_sha256']!=job['_npu_policy_sha256']:raise ValueError('Different NPU contract')
                    selected=selected_reading(job['allocation']['npu_ids'],(record['devices'],[]))
                    result['npu_release_observation']={'sampled_at':str(record['sampled_at'])[:64],
                        'policy_sha256':record['policy_sha256'],'devices':selected}
                except (KeyError,TypeError,ValueError,RuntimeError):raise JobError('Invalid NPU release observation',409) from None
            from workflow_spec import WORKFLOWS
            if job.get('template') in WORKFLOWS:
                job['workflow_result']=self.jobs.results.record(job,result,db)
            staged_valid=True
            if job.get('_staged_device_probe'):
                from owner_device_probe import validate
                try:validate(self.jobs,job['_staged_device_probe'],db,identities,job)
                except (JobError,ValueError,KeyError,TypeError):staged_valid=False
            qualified=self.qualifications.observe(self.node(node_id,db),job,result,db) if staged_valid else None
            if job.get('template')=='npu_access_probe' and result['status']=='succeeded' and not qualified:
                result.update(status='failed',execution_status='succeeded',failure_stage='qualification_evidence',
                              reason='Fixed device probe lacks valid protected access evidence; no qualification was granted')
            atomic_json(self.jobs.directory(job['id'])/'artifact-hashes.json',{'version':1,'files':files})
            from executor import finish
            finish(self.jobs.directory(job['id']),job,result)
            job.update(state=result['status'],reason=result.get('reason',''),result=result,
                       started_at=result.get('started_at'),finished_at=result.get('finished_at'),_worker_seen=time.time())
            job.pop('output_pending',None);job.pop('execution_ended',None);job.pop('cache_pending',None)
            from node_storage import receipt
            job['_artifact_receipt']=receipt(job,files)
            self.jobs.write(db,job)
            return {'accepted':True,'state':job['state'],'reused':False,'artifact_receipt':job['_artifact_receipt']}

    def public_nodes(self):
        from workflow_spec import runtime_manifest
        current_runtime=runtime_manifest()
        with self.jobs.connect() as db:ids=[row[0] for row in db.execute('SELECT id FROM worker_nodes WHERE disabled=0')]
        result=[]
        domains={domain['physical_id']:domain for domain in self.jobs.resources.summary()}
        for node_id in ids:
            node=self.node(node_id);profile=node['profile']
            domain=domains.get(profile['physical_id'])
            npu=node['report'].get('npu',{})
            devices=[{**item,'external_process_count':item['process_count'],'scoped_process_count':0} for item in npu.get('devices',[])]
            from device_qualification import decision
            access={key:0 for key in ['verified','unverified','blocked','expired','stale','needs_upgrade','unavailable']}
            if profile.get('npu'):
                for owner in profile['users']:
                    for device in profile['npu']['device_ids']:access[decision(node,owner,device,current_runtime)['status']]+=1
            result.append({'id':node_id,'name':profile['name'],'physical_id':profile['physical_id'],'backend':'managed-docker','status':'online' if time.time()-node['last_seen']<30 else 'unknown',
                'sampled_at':node['report'].get('sampled_at'),'execution_uid':'per-owner mapping',
                'enforcement':'container-cgroups','transport':'authenticated-pull-v1',
                'runtime_pinned':PINNED_RUNTIME in node['report'].get('capabilities',[]),
                'max_timeout_seconds':node['report'].get('max_timeout_seconds'),
                'spool_admission':'node-spool-v1' in node['report'].get('capabilities',[]),
                'spool_storage':{'enforced':bool(profile.get('spool_storage')),
                    **({key:profile['spool_storage'][key] for key in ['filesystem','bytes','inodes']} if profile.get('spool_storage') else {}),
                    **{key:node['report'].get('finite_spool',{}).get(key) for key in ['status','free_bytes','free_inodes']}},
                'attempt_inventory':{key:node['report'].get('attempt_inventory',{}).get(key) for key in ['reconciled','pending','unmatched']},
                'environment':{'image_sha256':profile.get('image') or node['report'].get('image_sha256'),
                    'certified':bool(profile.get('environment_manifest')),
                    'sha256':node['report'].get('environment_sha256'),
                    **({key:profile['environment_manifest']['observed'][key] for key in ['python','machine','packages']}
                       if profile.get('environment_manifest') else {}),
                    **({'lock':__import__('environment_contract').lock_for(profile['environment_manifest'])} if profile.get('environment_manifest') else {})},
                'observed':{k:node['report'].get(k) for k in ['memory_available_bytes','disk_free_bytes','disk_free_inodes','artifact_gc_ready','host_cpu_utilization_pct']},
                'capacity':profile['policy'],'resource_domain':domain,'npu_configured':bool(profile.get('npu')),
                'device_access':{'counts':access,'scope':'device-files-only','acl_verified':False,'numerical_verified':False},
                'io_limits':__import__('io_policy').limits(profile.get('io_limits')),
                'workspace':{'enforced':bool(profile.get('workspace')),'slots':[
                    {**__import__('workspace_slots').allocation(slot),**next((item for item in node['report'].get('workspace',{}).get('slots',[]) if item['id']==slot['id']),{})}
                    for slot in profile.get('workspace',{}).get('slots',[])]},
                'persistent_cache':{'enforced':bool(profile.get('persistent_cache')),'slots':[
                    {'slot_id':s['id'],'bytes':s['bytes'],'inodes':s['inodes'],
                     **{k:v for k,v in next((r for r in node['report'].get('persistent_cache',{}).get('slots',[]) if r['id']==s['id']),{}).items() if k!='id'}}
                    for s in profile.get('persistent_cache',{}).get('slots',[])]},
                'devices':devices,'device_status':npu.get('status','not-enabled'),'device_sampled_at':npu.get('sampled_at')})
        return result

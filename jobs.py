"""Single-node cooperative scheduler; durable SQLite ledger and independent workers."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

from collector import ROOT, now
from auth import AccessControl, AccessError
from templates import expand_template

TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
ACTIVE = {'reserved', 'running', 'cancelling', 'needs_attention'}
KINDS = {'command', 'build', 'accuracy', 'performance', 'simulation', 'ir'}
ARTIFACT_NAMES = {'stdout.log', 'stderr.log', 'request.json', 'result.json', 'environment.json', 'artifacts.json','validated-result.json','artifact-hashes.json','npu-admission.json','dependency-inputs.json'}


class JobError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x') as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def identity(pid):
    try:
        p = Path('/proc') / str(pid)
        values = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start_ticks': int(values[19]), 'uid': p.stat().st_uid,
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'state': values[0]}
    except (OSError, ValueError, IndexError):
        return None


def identity_alive(saved):
    if not saved:
        return False
    actual = identity(saved.get('pid', -1))
    return bool(actual and actual['state'] != 'Z' and all(actual[k] == saved.get(k) for k in ('pid', 'uid', 'start_ticks', 'boot_id')))


def defaults(config):
    cpus = sorted(os.sched_getaffinity(0))[-2:]
    policy = {'cpu_ids': cpus, 'memory_mib': 2048, 'shm_mib': 1024, 'max_running': 1,
              'max_queued': 100, 'max_timeout_seconds': 3600,
              'max_log_bytes': 2 * 1024**2, 'npu_enabled': True,
              'workspace_root': str(Path.home()), 'max_jobs': 1000, 'local_enabled':True,
              'require_bounded_workspace':False,'require_pinned_docker':False,'require_environment_contract':False,'require_attempt_inventory':False,'require_node_spool':False,'require_finite_node_spool':False,'aging_seconds':300}
    policy.update(config.get('scheduler', {}))
    from workflow_spec import MAX_TIMEOUT_SECONDS
    if type(policy['max_timeout_seconds']) is not int or not 1<=policy['max_timeout_seconds']<=MAX_TIMEOUT_SECONDS:
        raise ValueError('Administrator execution timeout must be within 1..86400 seconds')
    from queue_policy import threshold
    threshold(policy)
    from io_policy import limits
    policy['io_limits']=limits(policy.get('io_limits'))
    environments = {
        'worker-python': {'label':'节点服务 · 隔离 Python','type':'worker','python':'/usr/bin/python3'},
        'host-python': {'label': '宿主机 · Python 3', 'type': 'host', 'python': '/usr/bin/python3'},
        'zdl-python': {'label': 'zdl · 系统 Python', 'type': 'docker', 'container': config['container'],
                       'python': '/usr/bin/python3', 'uid': os.getuid(), 'gid': os.getgid(),
                       'npu_id_mapping': 'identity'},
        'zdl-mxh-dev': {'label': 'zdl · mxh_dev', 'type': 'docker', 'container': config['container'],
                        'python': '/data/miniconda3/envs/mxh_dev/bin/python',
                        'uid': os.getuid(), 'gid': os.getgid(), 'npu_id_mapping': 'identity'},
    }
    for name,override in config.get('environments', {}).items():
        environments[name]={**environments.get(name,{}),**override}
    for env in environments.values():
        env.setdefault('cann_env', '/data/pri/cann/set_env.sh')
    return policy, environments


def public(job):
    return {k: v for k, v in job.items() if not k.startswith('_')}


class JobStore:
    def __init__(self, runtime, config):
        self.runtime = Path(runtime)
        from payload_storage import Payload
        self.payload=Payload(self.runtime,config)
        self.root = self.payload.path('jobs')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = self.runtime / 'jobs.sqlite3'
        self.owner = config['username']
        self.access = AccessControl(runtime, config)
        # Import lazily because backend implementations use the manifest helpers above.
        from backends import LocalExecutionBackend
        self.backend = LocalExecutionBackend(runtime)
        self.policy, self.environments = defaults(config)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(owner, idem_key));
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
        self.db_path.chmod(0o600)
        from resources import ResourceDomains
        self.resources=ResourceDomains(self)
        from nodes import NodeRegistry
        self.nodes=NodeRegistry(self)
        from attempt_inventory import Inventory
        self.inventory=Inventory(self)
        from inputs import InputStore
        self.inputs=InputStore(self)
        from workflow_results import ResultStore
        self.results=ResultStore(self)
        from storage_lifecycle import Lifecycle
        self.lifecycle=Lifecycle(self)
        from storage_budget import StorageBudget
        self.storage=StorageBudget(self)
        from dependencies import Dependencies
        self.dependencies=Dependencies(self)

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=5)

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def write(self, db, job):
        if job['state'] in TERMINAL:self.storage.settle(db,job)
        db.execute('UPDATE jobs SET state=?,data=? WHERE id=?',
                   (job['state'], json.dumps(job, ensure_ascii=False), job['id']))
        if job.get('attempt_id'):
            db.execute('UPDATE execution_attempts SET state=? WHERE id=? AND job_id=?',
                       (job['state'],job['attempt_id'],job['id']))

    def attempts(self, job_id, principal=None):
        self.get(job_id,principal=principal)
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM execution_attempts WHERE job_id=? ORDER BY created_at',(job_id,))]

    def all(self, db=None):
        if db is not None:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM jobs ORDER BY rowid')]
        with self.connect() as connection:
            return self.all(connection)

    def get(self, job_id, db=None, principal=None):
        if db is None:
            with self.connect() as connection:
                return self.get(job_id, connection, principal)
        row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise JobError('找不到此作业。', 404)
        job = json.loads(row[0])
        if principal and not self.access.can_read(principal, job):
            raise JobError('找不到此作业。', 404)
        return job

    def directory(self, job_id, owner=None):
        # IDs never become arbitrary file paths.
        if not isinstance(job_id, str) or len(job_id) != 16 or not job_id.startswith('job-') or any(c not in '0123456789abcdef' for c in job_id[4:]):
            raise JobError('无效的作业编号。', 404)
        self.payload.check()
        path=self.root/job_id
        if self.payload.policy and self.payload.policy.get('owner_groups'):
            if owner is None:
                with self.connect() as db:row=db.execute('SELECT owner FROM jobs WHERE id=?',(job_id,)).fetchone()
                if row is None:raise JobError('作业不存在。',404)
                owner=row[0]
            self.payload.object(path,owner)
        return path

    def normalize(self, request, principal=None):
        if not isinstance(request, dict):
            raise JobError('请求必须是 JSON 对象。')
        principal = principal or self.access.local
        if principal.mode == 'templates' or 'template' in request:
            try:
                request = expand_template(request)
            except ValueError as error:
                raise JobError(str(error), 400)
        allowed = {'name', 'environment', 'kind', 'cwd', 'argv', 'command', 'cpu', 'memory_mib', 'shm_mib', 'npu', 'timeout_seconds', 'idempotency_key', 'output', 'origin','project','template','parameters','depends_on','artifact_inputs'}
        if set(request) - allowed:
            raise JobError('请求含有不支持的字段。身份由登录认证确定，不能由请求指定。')
        argv = request.get('argv')
        if 'command' in request:
            if argv is not None:
                raise JobError('argv 和 command 只能提供一个。')
            try:
                argv = shlex.split(request['command'])
            except (ValueError, TypeError):
                raise JobError('命令引号不匹配。')
        if not isinstance(argv, list) or not 1 <= len(argv) <= 128 or any(not isinstance(a, str) or '\0' in a or len(a) > 4096 for a in argv) or not argv[0]:
            raise JobError('请提供有效的命令参数数组。')
        argv = list(argv)
        if any(a in {'|', '||', '&&', ';', '>', '>>', '<', '&'} for a in argv):
            raise JobError('请把管道或多步操作保存为脚本后提交；命令不会经过 shell 解释。')
        env_id = request.get('environment', 'host-python')
        if env_id not in self.environments:
            raise JobError('请选择已注册的执行环境。')
        if '*' not in principal.environments and env_id not in principal.environments:
            raise JobError('此身份不允许使用该执行环境。', 403)
        env = dict(self.environments[env_id])
        if env['type']=='worker' and not request.get('template'):
            raise JobError('受控节点当前只接收已注册模板。',403)
        if env['type']!='worker' and not self.policy['local_enabled']:
            raise JobError('此中央服务未启用本地执行；请使用节点环境。',403)
        if principal.mode == 'templates':
            env['sanitized_environment'] = True
            env['home'] = principal.workspace
        if argv[0] in {'python', 'python3'}:
            argv[0] = env['python']
        kind = request.get('kind', 'command')
        if kind not in KINDS:
            raise JobError('不支持的任务类型。')
        def integer(name, default, low, high):
            value = request.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise JobError(f'{name} 需要在 {low}–{high} 之间。')
            return value
        limits=self.policy
        if env['type']=='worker':
            pools=[domain['policy'] for domain in self.resources.summary()]
            limits={'cpu_ids':list(range(max(len(pool['cpu_ids']) for pool in pools))),
                    **{key:max(pool[key] for pool in pools) for key in ['memory_mib','shm_mib']}}
        cpu = integer('cpu', 1, 1, len(limits['cpu_ids']))
        memory = integer('memory_mib', 256, 64, limits['memory_mib'])
        shm = integer('shm_mib', 0, 0, limits['shm_mib'])
        if shm > memory:
            raise JobError('共享内存包含在总内存申报内，不能大于 memory_mib。')
        npu = integer('npu', 0, 0, 2)
        timeout = integer('timeout_seconds', 60, 1, self.policy['max_timeout_seconds'])
        if kind in {'accuracy', 'performance'} and npu == 0:
            raise JobError('精度和性能任务必须显式申请 NPU。')
        if npu and not self.policy['npu_enabled']:
            raise JobError('当前配置未开启 NPU 作业。')
        if npu and env['type']=='docker' and env.get('npu_gid') is not None:
            device_gid=env['npu_gid']
            # docker exec --user UID:GID does not retain host supplementary groups.
            # This is trusted environment policy, never a client-controlled field.
            if type(device_gid) is not int or device_gid<1 or device_gid not in {os.getgid(),*os.getgroups()}:
                raise JobError('注册 NPU 访问组不在服务账号的已授权组内。',403)
            env['gid']=device_gid
        input_manifest=None
        from io_policy import artifacts,fits
        parameters=request.get('parameters',{})
        if not isinstance(parameters,dict):raise JobError('parameters must be an object')
        if 'template' not in request and set(parameters)-{'artifacts'}:
            raise JobError('Legacy command parameters only support artifacts')
        try:artifact_request=artifacts(parameters.get('artifacts'))
        except ValueError as error:raise JobError(str(error)) from None
        if not fits(artifact_request,self.policy['io_limits']):raise JobError('Artifact request exceeds administrator IO limits',403)
        # Keep default command fingerprints unchanged for pre-upgrade retries.
        # A non-default declaration must participate in the saved spec and its
        # fingerprint, just as it already does for validated workflow templates.
        legacy_parameters={'artifacts':artifact_request} if 'template' not in request and artifact_request!=artifacts() else None
        if request.get('template') in __import__('workflow_spec').WORKFLOWS:
            if request['parameters'].get('workspace') and env['type']!='worker':raise JobError('Bounded workspace workflows require a managed worker')
            bundle=self.inputs.get(request['parameters']['input_id'],principal)
            if not bundle['sealed']:raise JobError('Workflow input must be sealed before submission',409)
            input_manifest={k:bundle[k] for k in ['id','owner','files','manifest_sha256','bytes']}
        workspace = Path(principal.workspace).resolve()
        cwd = Path(request.get('cwd') or (ROOT if principal.legacy else workspace)).expanduser().resolve()
        if not cwd.is_relative_to(workspace) or not cwd.is_dir():
            raise JobError('工作目录必须位于当前用户的已授权工作区内。')
        output = request.get('output')
        if output:
            output = Path(output).expanduser()
            output = (cwd / output).resolve() if not output.is_absolute() else output.resolve()
            if not output.is_relative_to(workspace):
                raise JobError('归档目录必须位于当前用户的已授权工作区内。')
            output = str(output)
        name = request.get('name') or Path(argv[0]).name
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise JobError('作业名称长度必须在 1–100 之间。')
        key = request.get('idempotency_key') or uuid.uuid4().hex
        if not isinstance(key, str) or not 1 <= len(key) <= 120:
            raise JobError('无效的幂等键。')
        origin = request.get('origin')
        if origin is not None and (not isinstance(origin, dict) or set(origin)-{'client','model','agent_run_id'} or any(not isinstance(v,str) or len(v)>200 for v in origin.values())):
            raise JobError('无效的客户端来源元数据。')
        project = request.get('project', 'personal:'+principal.id)
        if not isinstance(project,str) or len(project)>100 or self.access.project_role(principal,project) not in {'editor','manager'}:
            raise JobError('没有此项目的提交权限。', 403)
        for resource_name, value in {'cpu':cpu,'memory_mib':memory,'shm_mib':shm,'npu':npu}.items():
            if value>principal.quota.get(resource_name,value):
                raise JobError('任务超过当前用户的 '+resource_name+' 配额。', 403)
        dependencies=self.dependencies.validate(request,principal)
        return {'name': name, 'environment': env_id, '_environment': env, 'kind': kind,**dependencies,
                **({'input':input_manifest} if input_manifest is not None else {}),
                **({'project':project} if 'project' in request or not principal.legacy else {}),
                **({'template':request['template'],'parameters':request['parameters']} if 'template' in request else {}),
                **({'parameters':legacy_parameters} if legacy_parameters is not None else {}),
                **({'origin': origin} if origin is not None else {}),
                'cwd': str(cwd), 'argv': argv, 'resources': {'cpu': cpu, 'memory_mib': memory, 'shm_mib': shm, 'npu': npu},
                'timeout_seconds': timeout, 'idempotency_key': key, 'output': output}

    @contextmanager
    def submission_transaction(self, staged=False):
        if staged:
            with self.nodes.handoff.transaction() as pair:yield pair
        else:
            with self.transaction() as db:yield db,None

    def submit(self, request, principal=None, *, _staged_probe=None):
        from unit_start import admission_reason
        reason=admission_reason(self.runtime,submit=True)
        if reason:raise JobError(reason,503)
        principal = principal or self.access.local
        principal.require('jobs:submit')
        if _staged_probe is not None:
            from owner_device_probe import job_request,load
            with self.connect() as connection:original=load(connection,_staged_probe)
            if request!=job_request(original['request']) or principal.id!=original['request']['owner']:
                raise JobError('Staged probes require the original fixed administrator request',403)
        spec = self.normalize(request, principal)
        if _staged_probe is not None:spec['_staged_device_probe']=_staged_probe
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in spec.items() if k != 'idempotency_key'}, sort_keys=True).encode()).hexdigest()
        with self.submission_transaction(_staged_probe is not None) as (db,identities):
            reason=admission_reason(self.runtime,submit=True)
            if reason:raise JobError(reason,503)
            if _staged_probe is not None:
                from owner_device_probe import validate
                validate(self,_staged_probe,db,identities)
            existing = db.execute('SELECT data,fingerprint FROM jobs WHERE owner=? AND idem_key=?', (principal.id, spec['idempotency_key'])).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise JobError('该幂等键已经用于不同的任务。', 409)
                existing_job=json.loads(existing[0])
                if _staged_probe is not None:validate(self,_staged_probe,db,identities,existing_job)
                return public(existing_job), True
            self.nodes.probe_submission(spec,principal,db)
            if spec.get('input'):
                self.lifecycle.available('input',spec['input']['id'],db)
            self.dependencies.available(spec,db)
            rows = self.all(db)
            if sum(j['state'] == 'queued' for j in rows) >= self.policy['max_queued'] or sum(j.get('archive_status',{}).get('state')!='purged' for j in rows) >= self.policy['max_jobs']:
                raise JobError('本 Demo 的作业数量已达到上限。', 409)
            if sum(j['owner']==principal.id and j['state']=='queued' for j in rows)>=principal.quota.get('max_queued', self.policy['max_queued']):
                raise JobError('当前用户等待队列已满。', 409)
            job_id = 'job-' + uuid.uuid4().hex[:12]
            from io_policy import artifacts,MIB,META
            artifact_limits={'file_mib':1,'total_mib':2} if _staged_probe is not None else artifacts(spec.get('parameters',{}).get('artifacts'))
            self.storage.reserve(db,'archive',job_id,principal,artifact_limits['total_mib']*MIB+META)
            directory = self.directory(job_id,owner=principal.id)
            source = None
            source_content=None
            for arg in spec['argv'][1:]:
                if arg.endswith('.py'):
                    path = (Path(spec['cwd']) / arg).resolve()
                    from container_engine import SCRIPTS
                    if path.is_relative_to(Path(principal.workspace)) or spec.get('template') and path==SCRIPTS.get(spec['template']):
                        try:
                            if not path.is_file():continue
                            if path.stat().st_size > min(4*MIB,artifact_limits['file_mib']*MIB):
                                raise JobError('入口源码超过单文件归档额度或 4 MiB，请调整申报或使用较小的入口脚本。')
                            content=path.read_bytes()
                        except OSError:raise JobError('无法读取入口源码，请核对授权路径和安装文件权限。',403)
                        source={'path':str(path),'sha256':hashlib.sha256(content).hexdigest()}
                        source_content=(path.name,content)
                        break
            directory.mkdir(mode=0o700)
            self.payload.object(directory,principal.id,create=True)
            if source_content:
                (directory/'source').mkdir(mode=0o700)
                (directory/'source'/source_content[0]).write_bytes(source_content[1])
            job = {**spec, 'id': job_id, 'owner': principal.id, 'project':spec.get('project', 'personal:'+principal.id), 'state': 'queued',
                   'reason': '等待资源分配', 'created_at': now(), 'started_at': None,
                   'finished_at': None, 'allocation': {}, 'source': source,
                   'archive_dir': str(directory), 'result': None, '_token': secrets.token_hex(24),
                   '_max_log_bytes': self.policy['max_log_bytes'],'artifact_limits':artifact_limits}
            from workflow_spec import WORKFLOWS,runtime_manifest
            if job.get('template') in WORKFLOWS or job.get('template') in {'environment_probe','npu_access_probe'}:job['runtime_files']=runtime_manifest()
            if job.get('depends_on'):
                job['_dependency_base_input']=job['parameters']['input_id']
                job['dependency_state']={'status':'waiting','reason':'等待上游成功及阶段输入封存；尚未预留计算资源'}
                job['reason']=job['dependency_state']['reason']
            if _staged_probe is not None:
                from owner_device_probe import bind_job
                bind_job(self,_staged_probe,job,db,identities)
            atomic_json(directory / 'request.json', public(job))
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', (job_id, principal.id, spec['idempotency_key'], fingerprint, job['state'], json.dumps(job, ensure_ascii=False)))
            return public(job), False

    def cancel(self, job_id, principal=None):
        principal = principal or self.access.local
        principal.require('jobs:cancel')
        with self.transaction() as db:
            job = self.get(job_id, db, principal)
            if not self.access.can_cancel(principal,job):
                raise JobError('没有取消此作业的权限。',403)
            if job['state'] in TERMINAL:
                return public(job)
            if job['state'] == 'queued':
                job.update(state='cancelled', reason='用户取消了等待中的作业', finished_at=now())
                result = {'job_id': job_id, 'status': 'cancelled', 'exit_code': None, 'finished_at': job['finished_at'], 'reason': job['reason']}
                job['result'] = result
                atomic_json(self.directory(job_id) / 'result.json', result)
                atomic_json(self.directory(job_id) / 'artifacts.json', {'files': [
                    {'name': name, 'bytes': (self.directory(job_id)/name).stat().st_size}
                    for name in ['request.json', 'result.json']]})
            elif job.get('_remote'):
                job['_cancel_requested']=True
                if job['state']!='needs_attention':job.update(state='cancelling',reason='等待节点确认容器结束；确认前保留资源')
            else:
                atomic_json(self.directory(job_id) / 'cancel.json', {'token': job['_token'], 'requested_at': now()})
                if job['state'] != 'needs_attention':
                    job.update(state='cancelling', reason='等待执行器确认停止；确认前保留资源')
            self.write(db, job)
            return public(job)

    def summary(self, principal=None):
        principal = principal or self.access.local
        principal.require('jobs:read')
        rows = self.all()
        active = [j for j in rows if j['state'] in ACTIVE]
        readable = self.access.readable_jobs(principal,rows)
        visible = {j['id'] for j in readable[-100:]} | {j['id'] for j in readable if j['state'] in ACTIVE|{'queued'}}
        environments = {k:v for k,v in self.environments.items() if ('*' in principal.environments or k in principal.environments)
                        and (self.policy['local_enabled'] or v['type']=='worker')}
        policy = dict(self.policy)
        if policy.get('payload_storage'):
            storage=policy['payload_storage']
            policy['payload_storage']={'enforced':True,'filesystem':storage['filesystem'],'bytes':storage['bytes'],'inodes':storage['inodes']}
        domains=self.resources.summary(rows=rows)
        pools=[domain['policy'] for domain in domains]
        policy.update(cpu_ids=max((pool['cpu_ids'] for pool in pools),key=len),
                      memory_mib=max(pool['memory_mib'] for pool in pools),
                      shm_mib=max(pool['shm_mib'] for pool in pools),
                      max_running=sum(pool['max_running'] for pool in pools))
        if not principal.legacy:
            policy['workspace_root'] = principal.workspace
        for key in ['memory_mib','shm_mib','max_running','max_queued']:
            policy[key] = min(policy[key], principal.quota.get(key,policy[key]))
        if 'cpu' in principal.quota:
            policy['cpu_ids'] = policy['cpu_ids'][:principal.quota['cpu']]
        return {'enabled': True, 'backend': 'sqlite-single-node',
                'policy':policy,'identity':self.access.public_profile(principal),'storage':self.storage.summary(principal),
                'default_cwd': str(ROOT) if principal.legacy else principal.workspace,
                'environments': [{'id': k, 'label': v['label'], 'type': v['type']} for k, v in environments.items()],
                'counts': {s: sum(j['state'] == s for j in readable) for s in ['queued', 'reserved', 'running', 'cancelling', 'needs_attention', *sorted(TERMINAL)]},
                'reserved': {'cpu': sum(j['resources']['cpu'] for j in active),
                             'memory_mib': sum(j['resources']['memory_mib'] for j in active),
                             'shm_mib': sum(j['resources'].get('shm_mib', 0) for j in active),
                             'npu_ids': [n for j in active for n in j['allocation'].get('npu_ids', [])]},
                'user_reserved': {key:sum(j['resources'].get(key,0) for j in active if j['owner']==principal.id)
                                  for key in ['cpu','memory_mib','shm_mib','npu']},
                'resource_pool': self.resources.get(self.backend.physical_id)['policy'],
                'resource_domains':domains,
                'jobs': [public(j) for j in reversed(rows) if j['id'] in visible]}

    def activity(self):
        return {'jobs':[{k:j.get(k) for k in ['id','owner','kind','state','resources','allocation','created_at','started_at']}
                        for j in self.all() if j['state'] in ACTIVE | {'queued'}]}

    def log(self, job_id, stream='stdout', principal=None):
        self.get(job_id, principal=principal)
        if stream not in {'stdout', 'stderr'}:
            raise JobError('无效的日志类型。')
        with self.lifecycle.reader('archive',job_id):
            path = self.directory(job_id) / (stream + '.log')
            if path.is_symlink():
                raise JobError('拒绝读取符号链接。', 403)
            if not path.exists():
                job=self.get(job_id,principal=principal)
                if job.get('_remote') and stream in job.get('_log_preview',{}):return job['_log_preview'][stream]
                return {'text': '', 'bytes': 0, 'truncated': False}
            with path.open('rb') as f:
                size = os.fstat(f.fileno()).st_size
                f.seek(max(0, size - 65536))
                data = f.read(65536)
            return {'text': data.decode(errors='replace'), 'bytes': size, 'truncated': size > 65536}

    def dispatch_permission(self,job,active,db=None,identities=None):
        from unit_start import admission_reason
        reason=admission_reason(self.runtime)
        if reason:return reason
        from owner_device_probe import dispatch_principal
        try:principal=dispatch_principal(self,job,db,identities)
        except (JobError,ValueError,KeyError,TypeError,sqlite3.Error):return '管理员预激活探针原授权已失效；保留原任务等待明确协调'
        if not principal or self.access.project_role(principal,job.get('project','personal:'+job['owner'])) not in {'editor','manager'}:
            return '等待有效账号和项目提交权限；不会启动已撤权的作业'
        if '*' not in principal.environments and job['environment'] not in principal.environments:return '执行环境授权已撤销；不会启动作业'
        if principal.mode=='templates' and not job.get('template'):return '账号已限制为模板执行；不会启动旧的任意命令'
        user_active=[j for j in active if j['owner']==job['owner']]
        if 'max_running' in principal.quota and len(user_active)>=principal.quota['max_running']:return '等待当前用户的运行名额'
        if any(sum(j['resources'].get(k,0) for j in user_active)+job['resources'].get(k,0)>v
               for k,v in principal.quota.items() if k in {'cpu','memory_mib','shm_mib','npu'}):return '等待当前用户的资源配额'
        return None


class Scheduler:
    def __init__(self, jobs, observations, stop_event):
        self.jobs, self.observations, self.stop = jobs, observations, stop_event
        self.lock = None
        self.children = []

    def acquire(self):
        self.lock = (self.jobs.runtime / 'scheduler.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('Another scheduler already owns this runtime directory')

    def reconcile(self):
        with self.jobs.transaction() as db:
            for job in self.jobs.all(db):
                if job['state'] not in ACTIVE:
                    continue
                if job.get('_remote'):
                    if time.time()-job.get('_worker_seen',0)>30:
                        job.update(state='needs_attention',reason='节点回执超时；保留资源，不自动迁移或重跑')
                        self.jobs.write(db,job)
                    continue
                directory = self.jobs.directory(job['id'])
                completion = read_json(directory / 'completion.json')
                if completion and completion.get('token') == job['_token'] and completion.get('job_id') == job['id']:
                    if job.get('attempt_id') and (completion.get('attempt_id')!=job['attempt_id'] or completion.get('node_id')!=job['node_id']):
                        job.update(state='needs_attention',reason='完成记录的执行尝试或节点不匹配；保留资源')
                        self.jobs.write(db,job)
                        continue
                    result = completion.get('result')
                    if not isinstance(result, dict) or result.get('status') not in TERMINAL | {'needs_attention'}:
                        job.update(state='needs_attention', reason='执行器完成记录格式异常；保留资源等待人工核对')
                        self.jobs.write(db, job)
                        continue
                    job.update(state=result['status'], reason=result.get('reason', ''), result=result,
                               started_at=result.get('started_at'), finished_at=result.get('finished_at'))
                    self.jobs.write(db, job)
                    continue
                supervisor = read_json(directory / 'supervisor.json')
                heartbeat = read_json(directory / 'heartbeat.json')
                if supervisor and supervisor.get('token') == job['_token'] and identity_alive(supervisor):
                    if heartbeat and heartbeat.get('token') == job['_token']:
                        if job['state'] not in {'cancelling', 'needs_attention'}:
                            job.update(state='running', started_at=heartbeat.get('started_at'), reason='执行器运行中')
                        job['heartbeat_at'] = heartbeat.get('at')
                    self.jobs.write(db, job)
                elif time.time() - job.get('_reserved_epoch', time.time()) > 15:
                    job.update(state='needs_attention', reason='执行器状态无法确认；保留资源且不自动重跑，请人工核对')
                    self.jobs.write(db, job)
        self.children = [p for p in self.children if p.poll() is None]

    def placement(self,job,all_active,snapshot,domain):
        from resources import memory_cost
        active=[j for j in all_active if j.get('_physical_id',self.jobs.backend.physical_id)==self.jobs.backend.physical_id]
        pool=domain['policy']
        reason = self.jobs.dispatch_permission(job,all_active) or self.jobs.inventory.reason(domain['physical_id'])
        used_cpus = {c for j in active for c in j['allocation'].get('cpu_ids', [])}
        free_cpus = [c for c in pool['cpu_ids'] if c not in used_cpus]
        used_memory = sum(memory_cost(j) for j in active)
        used_shm = sum(j['resources'].get('shm_mib', 0) for j in active)
        wanted = job['resources']
        npus = []
        if reason:pass
        elif domain['draining']:
            reason='此物理资源域正在排空，暂停分配新任务'
        elif not self.jobs.policy['local_enabled']:
            reason='此中央服务已关闭本地执行'
        elif len(active) >= pool['max_running']:
            reason = '等待物理资源域运行名额'
        elif len(free_cpus) < wanted['cpu']:
            reason = '等待 CPU 预算'
        elif used_memory + wanted['memory_mib'] > pool['memory_mib']:
            reason = '等待内存申报预算'
        elif used_shm + wanted.get('shm_mib', 0) > pool['shm_mib']:
            reason = '等待共享内存申报预算'
        elif not snapshot or time.time() - dt.datetime.fromisoformat(snapshot['sampled_at']).timestamp() > 45:
            reason = '等待新鲜的节点采样'
        elif snapshot['host']['memory_available_bytes'] < wanted['memory_mib'] * 1024**2 + 512 * 1024**2:
            reason = '主机可用内存不足；保留 512 MiB 余量'
        elif snapshot['host']['workspace_disk']['free_bytes'] < 512 * 1024**2:
            reason = '工作区磁盘余量不足'
        if not reason and wanted.get('shm_mib', 0):
            scope = snapshot.get('container') if job['_environment']['type'] == 'docker' else snapshot['host']
            observed = (scope or {}).get('shm')
            if not observed or observed.get('free_bytes', 0) < wanted['shm_mib'] * 1024**2:
                reason = '执行环境的共享内存容量不足或采样不可用'
        if not reason and wanted['npu']:
            npu = snapshot['npu']
            if npu['status'] != 'ok' or not npu.get('sampled_at') or time.time() - dt.datetime.fromisoformat(npu['sampled_at']).timestamp() > 45:
                reason = '等待新鲜的 NPU 健康与占用采样'
            else:
                reserved = {n for j in active for n in j['allocation'].get('npu_ids', [])}
                npus = [d['id'] for d in npu['devices'] if d['id'] not in reserved and d['health'] == 'OK' and d['utilization_pct'] == 0 and d['scoped_process_count'] + d['external_process_count'] == 0]
                if len(npus) < wanted['npu']:
                    reason = '等待健康且未观察到占用的 NPU；不抢占未托管任务'
        return {'reason':reason,'cpu_ids':free_cpus,'npu_ids':npus}

    def tick(self):
        self.reconcile()
        self.jobs.dependencies.tick()
        snapshot = self.observations.latest()
        with self.jobs.transaction() as db:
            rows = self.jobs.all(db)
            active = [j for j in rows if j['state'] in ACTIVE]
            all_active=list(active)
            active=[j for j in active if j.get('_physical_id',self.jobs.backend.physical_id)==self.jobs.backend.physical_id]
            domain=self.jobs.resources.get(self.jobs.backend.physical_id,db)
            from queue_policy import ordered
            queued=ordered(rows,self.jobs.policy)
            for job in queued:
                if job['_environment']['type']=='worker':continue
                from queue_policy import protected,note_wait,WAIT_REASON
                placement=self.placement(job,all_active,snapshot,domain)
                reason=placement['reason'];wanted=job['resources']
                if reason:
                    idle=self.placement(job,[],snapshot,domain)
                    note_wait(self.jobs,job,self.jobs.backend.node_id,domain,None,db,
                              not idle['reason'] and not self.jobs.dispatch_permission(job,all_active))
                else:
                    guard=protected(self.jobs,rows,self.jobs.backend.physical_id,db)
                    if guard and guard['id']!=job['id']:reason=WAIT_REASON
                if reason:
                    job['reason']=reason;self.jobs.write(db,job);continue
                free_cpus=placement['cpu_ids'];npus=placement['npu_ids']
                job.update(state='reserved', reason='已预留资源，等待执行器启动',
                           allocation={'cpu_ids': free_cpus[:wanted['cpu']], 'memory_mib': wanted['memory_mib'], 'shm_mib': wanted.get('shm_mib', 0), 'npu_ids': npus[:wanted['npu']]},
                           reserved_at=now(), _reserved_epoch=time.time(),
                           node_id=self.jobs.backend.node_id, attempt_id='attempt-'+uuid.uuid4().hex,
                           _token=secrets.token_hex(24),_physical_id=self.jobs.backend.physical_id)
                db.execute('INSERT INTO execution_attempts VALUES(?,?,?,?,?)',
                           (job['attempt_id'],job['id'],job['node_id'],'reserved',job['reserved_at']))
                self.jobs.write(db, job)
                atomic_json(self.jobs.directory(job['id']) / 'dispatch.json', job)
                active.append(job)
                all_active.append(job)
        # Launch only once. An ambiguous crash stays reserved/needs_attention, never gets blindly re-run.
        for job in active:
            if job.get('_remote'):continue
            if job['state'] != 'reserved' or job.get('_launch_attempted'):
                continue
            with self.jobs.transaction() as db:
                actual = self.jobs.get(job['id'], db)
                if actual.get('_launch_attempted') or actual['state'] != 'reserved':
                    continue
                actual['_launch_attempted'] = True
                self.jobs.write(db, actual)
            directory = self.jobs.directory(job['id'])
            try:
                child = self.jobs.backend.launch(directory,job)
                self.children.append(child)
            except OSError:
                logging.exception('Could not launch job %s', job['id'])

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception:
                    logging.exception('Scheduler iteration failed')
                self.stop.wait(1)
        finally:
            if self.lock:
                self.lock.close()

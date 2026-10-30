"""Narrow managed-Docker runner. Payloads cannot select mounts, UIDs, images or capabilities."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import threading
import time

from jobs import ROOT,atomic_json,read_json,now
from workflow_spec import WORKFLOWS,validate as validate_workflow,runtime_manifest
from resources import MANAGED_OVERHEAD_MIB

LABEL='io.ascend-lab.attempt'
MAX_LOG=2*1024**2
MAX_FILE=64*1024**2
MAX_ARCHIVE=128*1024**2
SCRIPTS={'cpu_demo':ROOT/'examples/cpu_task.py','isolation_probe':ROOT/'examples/isolation_probe.py'}
SCRIPTS['npu_probe']=ROOT/'examples/npu_device_probe.py'
SCRIPTS['environment_probe']=ROOT/'environment_probe.py'
SCRIPTS['npu_access_probe']=ROOT/'environment_probe.py'
SCRIPTS.update({name:ROOT/'workflow_runner.py' for name in WORKFLOWS})


def run(argv,timeout=15,binding=None):
    from docker_runtime import command
    argv,env=command(binding,argv)
    result=subprocess.run(argv,env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Docker operation failed: '+result.stderr.decode(errors='replace')[-500:])
    return result.stdout


def safe_artifact(name):
    if not isinstance(name,str) or len(name)>240 or '\\' in name or '\0' in name:return False
    p=PurePosixPath(name)
    return not p.is_absolute() and '..' not in p.parts and name==str(p) and len(p.parts)>1 and p.parts[0] in {'results','ir','profile','source'}


class DockerAttempt:
    def __init__(self,state_root,grant,config):
        self.grant=grant;self.config=config
        self.protected_receipt=bool(grant.get('input')) or grant.get('template') in {'environment_probe','npu_access_probe'}
        attempt=grant['attempt_id']
        if not attempt.startswith('attempt-') or len(attempt)!=40 or any(c not in '0123456789abcdef' for c in attempt[8:]):
            raise ValueError('Invalid execution attempt')
        self.base=Path(state_root).resolve()/'attempts'/attempt
        self.base.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.control=self.base/'control';self.control.mkdir(mode=0o700,exist_ok=True)
        policy_path=self.control/'execution-policy.json'
        frozen=read_json(policy_path)
        self.fresh_policy=not frozen
        if frozen:self.config=frozen
        else:
            self.config={k:v for k,v in config.items() if k in {'image','python','users','policy','policy_revision','templates','probe_forbidden_paths','max_timeout_seconds','environment_setup','npu','workspace','io_limits','docker_runtime','environment_manifest','physical_id','spool_storage','node_id','persistent_cache'}}
            self.config['users']=dict(config['users']) if config.get('persistent_cache') else {grant['owner']:config['users'].get(grant['owner'])}
            atomic_json(policy_path,self.config)
        self.payload=self.base/'payload';self.payload.mkdir(mode=0o755,exist_ok=True)
        self.spool=None;self.spool_anchor=None;data=self.base
        if self.config.get('spool_storage'):
            from spool_storage import Spool,fingerprint
            if grant.get('spool_policy_sha256')!=fingerprint(self.config['spool_storage']):raise ValueError('Spool grant differs from pinned node policy')
            self.spool=Spool(state_root,{**self.config,'node_id':grant['node_id']},check=False)
            data=self.spool.directory(attempt)
        elif grant.get('spool_policy_sha256'):raise ValueError('Finite spool grant cannot fall back to a control directory')
        self.inputs=data/'inputs'
        self.input_bind=self.inputs
        self.receipt=self.base/'receipt'
        self.workspace=None
        if self.config.get('workspace'):
            from workspace_slots import Workspace
            self.workspace=Workspace(self.config['workspace'],grant,(self.config['users'].get(grant['owner']) or {}).get('uid'))
        elif grant.get('allocation',{}).get('workspace'):raise ValueError('Workspace grant cannot fall back to an ordinary work directory')
        self.cache=None
        if grant.get('allocation',{}).get('cache'):
            from persistent_cache import Pool,Lease
            if not self.config.get('persistent_cache'):raise ValueError('Persistent cache cannot fall back to the work directory')
            self.cache=Lease(Pool(state_root,self.config),grant)
        self.work=self.workspace.path if self.workspace else self.base/'work'
        if not self.workspace:self.work.mkdir(mode=0o700,exist_ok=True)
        self.artifacts=data/'artifacts' if self.spool else self.control/'artifacts'
        self.scratch=self.artifacts.parent/'export-scratch'
        self.state_path=self.control/'state.json'
        self.name='ascend-lab-'+attempt[8:]
        self.log_process=None;self.log_threads=[];self.log_error=None
        saved=read_json(self.control/'grant.json')
        if saved and saved!=grant:raise ValueError('Attempt grant changed; refusing another execution')
        atomic_json(self.control/'grant.json',grant)
        self.state=read_json(self.state_path) or {'stage':'new','started_epoch':None}
        if not self.spool and not (self.state.get('stage')=='cleaned' and self.state.get('controller_acknowledged')):
            self.artifacts.mkdir(mode=0o700,exist_ok=True)

    def prepare_spool(self):
        if self.spool:
            self.spool.prepare(self.grant)
            if self.spool_anchor is None:
                context=self.spool.anchor();anchor=context.__enter__();self.spool_anchor=context
                data=anchor/'attempts'/self.grant['attempt_id']
                self.inputs=data/'inputs';self.artifacts=data/'artifacts';self.scratch=data/'export-scratch'
        self.artifacts.mkdir(mode=0o700,exist_ok=True)

    def close_spool(self):
        if self.spool_anchor is not None:
            self.spool_anchor.__exit__(None,None,None);self.spool_anchor=None

    def save(self):atomic_json(self.state_path,self.state)

    def binding(self):
        from docker_runtime import discover,DockerRuntimeError
        if not self.config.get('docker_runtime'):
            if not self.fresh_policy:raise DockerRuntimeError('Historical attempt has no pinned Docker daemon; reconcile it without guessing its execution host')
            value,_=discover();self.config['docker_runtime']=value
            atomic_json(self.control/'execution-policy.json',self.config)
        return self.config['docker_runtime']

    def command(self,argv):
        from docker_runtime import command
        argv,env=command(self.binding(),argv)
        return argv,env

    def inspect(self):
        argv,env=self.command(['docker','inspect',self.name])
        result=subprocess.run(argv,env=env,capture_output=True,timeout=10)
        if result.returncode:
            if b'No such' in result.stderr:return None
            raise RuntimeError('Cannot confirm managed container state')
        info=json.loads(result.stdout)[0]
        if info['Config'].get('Labels',{}).get(LABEL)!=self.grant['attempt_id']:
            raise RuntimeError('Container identity label mismatch')
        return info

    def validate(self):
        g=self.grant
        if 'node_policy_revision' in g and g['node_policy_revision']!=self.config.get('policy_revision',1):raise ValueError('Grant belongs to another node policy revision')
        from io_policy import artifacts,fits
        if not fits(g.get('artifact_limits'),self.config.get('io_limits')):raise ValueError('Artifact budget exceeds node ceiling')
        if artifacts(g.get('artifact_limits'))!=artifacts(g.get('parameters',{}).get('artifacts')):raise ValueError('Artifact grant differs from pinned task request')
        if g['template'] not in SCRIPTS or g['template'] not in self.config.get('templates',SCRIPTS):
            raise ValueError('Template not registered on node')
        mapping=self.config['users'].get(g['owner'])
        if not isinstance(mapping,dict) or any(isinstance(mapping.get(k),bool) or not isinstance(mapping.get(k),int) or mapping[k]<1000 for k in ['uid','gid']):
            raise ValueError('Owner has no non-root node execution identity')
        resource=g['resources'];allocation=g['allocation']
        if resource['npu'] or allocation.get('npu_ids'):
            from npu_device import validate,fingerprint
            policy=validate(self.config.get('npu'),host=True)
            if (resource['npu']!=1 or len(allocation.get('npu_ids',[]))!=1 or allocation['npu_ids'][0] not in policy['device_ids']
                or g.get('npu_policy_sha256')!=fingerprint(policy)):raise ValueError('NPU allocation is outside the pinned node contract')
        if resource['cpu']!=len(allocation['cpu_ids']) or not set(allocation['cpu_ids'])<=set(self.config['policy']['cpu_ids']):raise ValueError('CPU allocation outside node policy')
        if not 64<=resource['memory_mib']<=self.config['policy']['memory_mib']:raise ValueError('Memory allocation outside node policy')
        if not 0<=resource.get('shm_mib',0)<=resource['memory_mib']:raise ValueError('Invalid shared memory allocation')
        if not 1<=g['timeout_seconds']<=self.config.get('max_timeout_seconds',300):raise ValueError('Invalid task deadline')
        if g['source_sha256']!=hashlib.sha256(SCRIPTS[g['template']].read_bytes()).hexdigest():raise ValueError('Controller/node template version mismatch')
        if g.get('environment_sha256'):
            from environment_contract import verify_config
            if verify_config(self.config)!=g['environment_sha256']:raise ValueError('Assigned environment differs from the frozen node certificate')
        if g['template']=='environment_probe':
            from environment_contract import probe_parameters
            if probe_parameters(g['parameters'])['node_id']!=g['node_id'] or g.get('runtime_files')!=runtime_manifest():raise ValueError('Probe node or protected runtime differs')
        if g['template']=='npu_access_probe' or g['template'] in WORKFLOWS and resource['npu']:
            from device_qualification import parameters,context,host_fingerprint
            if g.get('runtime_files')!=runtime_manifest():raise ValueError('Device qualification runtime differs')
            if g['template']=='npu_access_probe':
                target=parameters(g['parameters'])
                if target['node_id']!=g['node_id'] or allocation['npu_ids']!=[target['device_id']]:raise ValueError('Device probe target differs from allocation')
            expected=context(self.config,g['node_id'],g['owner'],allocation['npu_ids'][0],host_fingerprint(self.binding()),runtime_manifest())
            if g.get('npu_access_context_sha256')!=expected:raise ValueError('Device access qualification context changed before execution')
        if g['template'] in WORKFLOWS:
            if g.get('runtime_files')!=runtime_manifest():raise ValueError('Controller/node workflow runtime versions differ')
            parameters=validate_workflow(g['parameters'],WORKFLOWS[g['template']])
            if not g.get('input') or g['input']['id']!=parameters['input_id'] or g['input']['owner']!=g['owner']:raise ValueError('Workflow input does not belong to owner')
            if parameters['resources']!=g['resources'] or parameters['timeout_seconds']!=g['timeout_seconds']:raise ValueError('Workflow resources differ from grant')
            required=parameters['lock'].get('image_sha256')
            if required and required!=self.config['image']:raise ValueError('Pinned image differs from workflow lock')
            if bool(parameters.get('cache'))!=bool(self.cache):raise ValueError('Persistent cache request differs from assigned cache lease')
        if not self.config['image'].startswith('sha256:') or len(self.config['image'])!=71:raise ValueError('Image must be pinned by local SHA256 ID')
        return mapping

    def start(self):
        info=self.inspect()
        if self.state['stage']!='new':
            if info is None:raise RuntimeError('Previously attempted container disappeared; execution will not be repeated')
            if self.state['stage']=='prestart-rejected':raise RuntimeError('Device preflight refused before any start intent')
            if info['State']['Status']=='created':raise RuntimeError('Container start is unconfirmed; a created container is not a completed workload')
            return
        mapping=self.validate()
        self.prepare_spool()
        if info is not None:raise RuntimeError('Unexpected pre-existing container for a new attempt')
        if self.cache:
            self.state['cache_identity']=self.cache.prepare();self.save()
        if self.workspace:
            self.state['workspace_identity']=self.workspace.prepare()
            if self.workspace.quota_attestation:self.state['workspace_quota']=self.workspace.quota_attestation
            self.save()
        if self.protected_receipt:self.receipt.mkdir(mode=0o700,exist_ok=True)
        for source,name in [(ROOT/'container_entry.py','container_entry.py'),(ROOT/'artifact_export.py','artifact_export.py'),(SCRIPTS[self.grant['template']],'task.py')]:
            shutil.copyfile(source,self.payload/name);(self.payload/name).chmod(0o444)
        if self.protected_receipt:
            shutil.copyfile(ROOT/'environment_probe.py',self.payload/'environment_probe.py');(self.payload/'environment_probe.py').chmod(0o444)
        public={'uid':mapping['uid'],'gid':mapping['gid'],'job_id':self.grant['job_id'],
                'owner':self.grant['owner'],'template':self.grant['template'],'parameters':self.grant['parameters'],
                'cpu_ids':self.grant['allocation']['cpu_ids'],'timeout_seconds':self.grant['timeout_seconds'],
                'forbidden_paths':self.config.get('probe_forbidden_paths',[]),
                'image_sha256':self.config['image'],'environment_setup':self.config.get('environment_setup'),
                **({'input':self.grant['input']} if self.grant.get('input') else {})}
        public['artifact_limits']=self.grant.get('artifact_limits',{'file_mib':64,'total_mib':128})
        if self.workspace:public['workspace_identity']=self.state['workspace_identity']
        if self.cache:public['cache_identity']=self.state['cache_identity']
        npu_args=[]
        if self.grant['resources']['npu']:
            from npu_device import docker_arguments
            policy=self.config['npu'];ids=self.grant['allocation']['npu_ids']
            npu_args=docker_arguments(self.grant,policy)
            public.update(npu_ids=ids,npu_visible_ids=[policy['visible_ids'][str(value)] for value in ids],
                          supplementary_gids=[policy['device_gid']])
            if self.grant['allocation'].get('npu_name'):public['npu_name']=self.grant['allocation']['npu_name']
        elif self.grant['template']=='environment_probe' and self.config.get('npu'):
            from npu_device import validate
            policy=validate(self.config['npu'],host=True)
            public['supplementary_gids']=[policy['device_gid']]
            for path in policy['driver_paths']:npu_args+=['--mount','type=bind,src='+path+',dst='+path+',readonly']
        atomic_json(self.payload/'run.json',public);(self.payload/'run.json').chmod(0o444)
        self.payload.chmod(0o555)
        resource=self.grant['resources'];cpus=','.join(map(str,self.grant['allocation']['cpu_ids']))
        argv=['docker','create','--pull','never','--name',self.name,'--label',LABEL+'='+self.grant['attempt_id'],
              '--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','SETUID',
              '--cap-add','SETGID','--security-opt','no-new-privileges:true','--user','0:0',
              '--memory',str(resource['memory_mib'])+'m','--memory-swap',str(resource['memory_mib'])+'m',
              '--cpuset-cpus',cpus,'--cpus',str(resource['cpu']),'--pids-limit','128',
              *(['--shm-size',str(resource['shm_mib'])+'m'] if resource.get('shm_mib',0) else ['--ipc','none']),
              '--tmpfs','/tmp:rw,noexec,nosuid,size=16777216',
              '--log-opt','max-size=2m','--log-opt','max-file=1',
              '--mount','type=bind,src='+str(self.payload)+',dst=/payload,readonly',
              '--mount','type=bind,src='+str(self.work)+',dst=/work',
              *(['--mount','type=bind,src='+str(self.cache.path)+',dst=/cache'] if self.cache else []),
              *(['--mount','type=bind,src='+str(self.input_bind)+',dst=/inputs,readonly'] if self.grant.get('input') else []),
              *(['--mount','type=bind,src='+str(self.receipt)+',dst=/receipt'] if self.protected_receipt else []),
              *npu_args,
              '--entrypoint',self.config.get('python','/usr/bin/python3'),self.config['image'],
              '-I','-B','/payload/container_entry.py']
        # Persist each intent before an external mutation. Ambiguous starts are never retried.
        self.state.update(stage='creating');self.save()
        try:run(argv,timeout=30,binding=self.binding())
        except Exception as error:
            self.state.update(stage='create-failed',create_failure=type(error).__name__);self.save()
            raise RuntimeError('Docker create failed before any task start') from error
        if self.grant['resources']['npu']:
            from npu_device import check
            try:atomic_json(self.control/'npu-admission.json',check(self.grant,self.config['npu']))
            except Exception as error:
                self.state.update(stage='prestart-rejected');self.save()
                raise RuntimeError('NPU preflight changed after container creation; no start was issued') from error
        self.state.update(stage='start-intent',started_epoch=time.time());self.save()
        run(['docker','start',self.name],timeout=20,binding=self.binding())
        self.state.update(stage='running',started_at=now());self.save()

    def follow_logs(self):
        if self.log_process:return
        argv,env=self.command(['docker','logs','--follow',self.name])
        self.log_process=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        def pump(stream,path):
            written=0
            try:
                with path.open('wb') as target:
                    while True:
                        block=os.read(stream.fileno(),8192)
                        if not block:break
                        if written<MAX_LOG:
                            part=block[:MAX_LOG-written];target.write(part);target.flush();written+=len(part)
            except OSError as error:self.log_error=error
            finally:stream.close()
        for stream,name in [(self.log_process.stdout,'stdout.log'),(self.log_process.stderr,'stderr.log')]:
            thread=threading.Thread(target=pump,args=(stream,self.artifacts/name),daemon=True)
            thread.start();self.log_threads.append(thread)

    def stop_log_client(self):
        if self.log_process and self.log_process.poll() is None:
            self.log_process.terminate()
            try:self.log_process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.log_process.kill();self.log_process.wait()
        for thread in self.log_threads:thread.join(timeout=3)
        if any(thread.is_alive() for thread in self.log_threads):raise RuntimeError('Log capture has not terminated; preserve the attempt')
        self.log_process=None;self.log_threads=[]

    def preview(self):
        result={}
        for stream in ['stdout','stderr']:
            path=self.artifacts/(stream+'.log')
            if path.exists():
                with path.open('rb') as file:
                    size=path.stat().st_size;file.seek(max(0,size-8192));body=file.read(8192)
                result[stream]={'text':body.decode(errors='replace'),'bytes':size,'truncated':size>8192}
        return result

    def collect_outputs(self):
        self.prepare_spool()
        from io_policy import for_job,MIB
        from spool_transfer import publish,reset_scratch
        reset_scratch(self.scratch)
        budget=for_job(self.grant)
        total=0;count=0
        diagnostic=self.control/'exporter.log'
        argv=['docker','run','--pull','never','--rm','--label',LABEL+'='+self.grant['attempt_id'],
            '--network','none','--read-only','--cap-drop','ALL','--cap-add','DAC_OVERRIDE',
            '--security-opt','no-new-privileges:true','--user','0:0','--memory',str(MANAGED_OVERHEAD_MIB)+'m','--memory-swap',str(MANAGED_OVERHEAD_MIB)+'m',
            '--cpuset-cpus',str(self.grant['allocation']['cpu_ids'][0]),'--pids-limit','32',
            '--mount','type=bind,src='+str(self.work)+',dst=/work,readonly',
            '--mount','type=bind,src='+str(self.payload)+',dst=/payload,readonly',
            *(['--mount','type=bind,src='+str(self.receipt)+',dst=/receipt,readonly'] if self.protected_receipt else []),
            '--entrypoint',self.config.get('python','/usr/bin/python3'),self.config['image'],
            '-I','-B','/payload/artifact_export.py']
        argv,env=self.command(argv)
        with diagnostic.open('wb') as error_stream:
            process=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=error_stream)
        try:
            with tarfile.open(fileobj=process.stdout,mode='r|') as archive:
                for member in archive:
                    name=member.name
                    if not member.isfile() or not safe_artifact(name):raise RuntimeError('Unsafe artifact link or path in task output')
                    total+=member.size;count+=1
                    if member.size>budget['file_mib']*MIB or total>budget['total_mib']*MIB or count>2000:raise RuntimeError('Task artifact size/count exceeded node budget')
                    target=self.artifacts/name;target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                    source=archive.extractfile(member)
                    publish(source,target,member.size,self.scratch)
            if process.wait(timeout=10):raise RuntimeError('Read-only artifact exporter rejected task outputs')
        except (tarfile.ReadError,RuntimeError) as error:
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:pass
            from spool_transfer import SpoolIntegrityError
            if isinstance(error,SpoolIntegrityError):raise
            diagnostic_data=read_json(diagnostic)
            if diagnostic_data and diagnostic_data.get('error') in {'unsafe-output-link','unsafe-output-link-or-type','output-budget-exceeded','output-read-error','reserved-output-name','unsafe-protected-receipt'}:
                raise RuntimeError('Artifact export rejected: '+diagnostic_data['error']) from error
            raise
        finally:
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close()

    def tick(self,cancel=False):
        from spool_transfer import SpoolIntegrityError
        from storage_io import exhausted
        try:return self._tick(cancel)
        except (OSError,SpoolIntegrityError) as error:
            self.state['output_pending']={'kind':'capacity' if exhausted(error) else 'integrity-or-io','observed_at':now()}
            if self.state.get('execution_result') or exhausted(error) or isinstance(error,SpoolIntegrityError):
                self.save()
            raise

    def _tick(self,cancel=False):
        saved=read_json(self.control/'result.json')
        if saved:return saved
        info=self.inspect()
        if info is None:raise RuntimeError('Managed container is missing; termination cannot be confirmed')
        if info['State']['Running']:
            elapsed=time.time()-(self.state.get('started_epoch') or time.time())
            if cancel or elapsed>self.grant['timeout_seconds']+5:
                self.state['stop_reason']='cancelled' if cancel else 'timed_out';self.save()
                run(['docker','stop','--time','2',self.name],timeout=10,binding=self.binding())
                return None
            self.prepare_spool();self.follow_logs()
            return None
        if info['State'].get('Pid') or info['State']['Status'] not in {'exited','dead'}:
            raise RuntimeError('Docker has not confirmed container termination')
        code=info['State']['ExitCode']
        status=self.state.get('stop_reason') or ('succeeded' if code==0 else 'timed_out' if code==124 else 'failed')
        if not self.state.get('execution_result'):
            self.state['execution_result']={'status':status,'exit_code':code,'started_at':self.state.get('started_at'),
                'finished_at':now(),'reason':'Managed container ended; Docker confirms no running task',
                'termination_confirmed':True,'oom_killed':info['State'].get('OOMKilled',False)}
            self.save()
        result=dict(self.state['execution_result'])
        self.prepare_spool();self.log_error=None;self.follow_logs()
        if self.log_process:
            try:self.log_process.wait(timeout=5)
            except subprocess.TimeoutExpired:pass
        self.stop_log_client()
        if self.log_error:raise self.log_error
        if result['oom_killed']:result.update(status='failed',failure_stage='memory_limit',reason='Docker reports task memory limit exceeded; execution has ended')
        try:self.collect_outputs()
        except Exception as error:
            from storage_io import exhausted
            from spool_transfer import SpoolIntegrityError
            if isinstance(error,(OSError,SpoolIntegrityError)):raise
            result.update(status='failed',execution_status=status,failure_stage='artifact_collection',reason=str(error)[:500])
        if result['status']=='failed' and not result.get('failure_stage') and self.protected_receipt:
            protected=read_json(self.artifacts/'results/_lab/environment.json')
            if (protected and protected.get('schema')=='ascend-lab-environment/v1'
                and protected.get('failure_stage')=='npu_device_access' and protected.get('locked') is False
                and protected.get('workload_started') is False and protected.get('probe_exit_code')==78):
                result.update(failure_stage='npu_device_access',reason='Task-identity NPU access was rejected before workload start; see results/_lab/environment.json')
        if self.grant['template']=='npu_probe' and result['status']=='failed':
            probe=read_json(self.artifacts/'results/npu-device-probe.json')
            if probe and probe.get('passed') is False:
                result.update(failure_stage='npu_device_access',reason='NPU device access probe failed; see results/npu-device-probe.json')
        metadata={'runtime':'managed-docker','image':info['Image'],'node_daemon_uid':os.getuid(),'task_uid':self.config['users'][self.grant['owner']]['uid'],
                  'environment_setup':self.config.get('environment_setup'),
                  'artifact_limits':self.grant.get('artifact_limits',{'file_mib':64,'total_mib':128}),
                  'npu_ids':self.grant['allocation'].get('npu_ids',[]),'device_nodes':info['HostConfig'].get('Devices',[]),
                  **({'runtime_files':self.grant['runtime_files']} if self.grant.get('runtime_files') else {}),
                  'task_gid':self.config['users'][self.grant['owner']]['gid'],
                  'container_id':info['Id'],'read_only_root':info['HostConfig']['ReadonlyRootfs'],
                  'network_mode':info['HostConfig']['NetworkMode'],'privileged':info['HostConfig']['Privileged'],
                  'memory_limit':info['HostConfig']['Memory'],'memory_swap_limit':info['HostConfig']['MemorySwap'],
                  'cpuset_cpus':info['HostConfig']['CpusetCpus'],'pids_limit':info['HostConfig']['PidsLimit'],
                  'ipc_mode':info['HostConfig']['IpcMode'],
                  'workspace':{'allocation':self.grant['allocation'].get('workspace'),
                               'verified_identity':self.state.get('workspace_identity'),
                               **({'user_quota':self.state['workspace_quota']} if self.state.get('workspace_quota') else {}),
                               'cache_mode':'persistent-private-uid-quota' if self.cache else 'ephemeral-in-workspace' if self.workspace else 'unbounded-host-directory'},
                  'persistent_cache':{'enforced':bool(self.cache),**({'key':self.cache.key,'bytes':self.cache.slot['bytes'],
                       'inodes':self.cache.slot['inodes'],'verified_identity':self.state.get('cache_identity')} if self.cache else {})},
                  'node_spool':{'enforced':bool(self.spool),**({key:self.spool.policy[key] for key in ['bytes','inodes','filesystem']} if self.spool else {})},
                  'shm_bytes':0 if info['HostConfig']['IpcMode']=='none' else info['HostConfig']['ShmSize'],'security_options':info['HostConfig']['SecurityOpt'],
                  'mounts':[{'destination':m['Destination'],'writable':m['RW']} for m in info['Mounts']],
                  'termination':{k:info['State'].get(k) for k in ['Status','Running','Pid','ExitCode','OOMKilled']}}
        from lab_client import atomic_json as artifact_json
        artifact_json(self.artifacts/'environment.json',metadata)
        source=self.artifacts/'source';source.mkdir(mode=0o700,exist_ok=True)
        from spool_transfer import publish,reset_scratch
        with (self.payload/'task.py').open('rb') as stream:publish(stream,source/'task.py',(self.payload/'task.py').stat().st_size,self.scratch)
        reset_scratch(self.scratch,remove=True)
        self.state.pop('output_pending',None)
        atomic_json(self.control/'result.json',result)
        self.state['stage']='finished';self.save()
        return result

    def cleanup(self):
        info=self.inspect()
        if info and info['State']['Running']:raise RuntimeError('Refusing cleanup while task container is running')
        if self.spool:self.spool.check()
        if info:run(['docker','rm',self.name],timeout=15,binding=self.binding())
        if self.inputs.exists():shutil.rmtree(self.inputs)
        if (not self.work.exists() or self.work.stat().st_uid==os.getuid()) and (not self.receipt.exists() or self.receipt.stat().st_uid==os.getuid()):
            try:
                if self.work.exists():shutil.rmtree(self.work)
                if self.receipt.exists():shutil.rmtree(self.receipt)
                if self.workspace:self.workspace.release()
                if self.cache:self.cache.release(termination_confirmed=True)
                self.state['stage']='cleaned';self.save();return
            except PermissionError:pass
        # Work is owned by the task UID. A tightly mounted helper only restores this work tree.
        helper='import os\nfor p in '+repr((['/work'] if self.work.exists() else [])+(['/receipt'] if self.receipt.exists() else []))+':\n for root,dirs,files in os.walk(p,followlinks=False):\n  for n in dirs+files:\n   q=os.path.join(root,n);os.lchown(q,'+str(os.getuid())+','+str(os.getgid())+')\n   if n in dirs and not os.path.islink(q):os.chmod(q,0o700)\n os.chown(p,'+str(os.getuid())+','+str(os.getgid())+')\n os.chmod(p,0o700)\n'
        run(['docker','run','--pull','never','--rm','--label',LABEL+'='+self.grant['attempt_id'],
             '--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER',
             '--security-opt','no-new-privileges:true','--user','0:0','--memory',str(MANAGED_OVERHEAD_MIB)+'m','--memory-swap',str(MANAGED_OVERHEAD_MIB)+'m',
             '--cpuset-cpus',str(self.grant['allocation']['cpu_ids'][0]),'--pids-limit','32',
             *(['--mount','type=bind,src='+str(self.work)+',dst=/work'] if self.work.exists() else []),
             *(['--mount','type=bind,src='+str(self.receipt)+',dst=/receipt'] if self.receipt.exists() else []),
             '--entrypoint',self.config.get('python','/usr/bin/python3'),self.config['image'],'-I','-c',helper],timeout=30,binding=self.binding())
        if self.work.exists():shutil.rmtree(self.work)
        if self.receipt.exists():shutil.rmtree(self.receipt)
        if self.workspace:self.workspace.release()
        if self.cache:self.cache.release(termination_confirmed=True)
        self.state['stage']='cleaned';self.save()

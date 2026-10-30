"""Independent pull worker. Node credentials never enter task containers."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from backends import physical_identity
from container_engine import DockerAttempt
from jobs import atomic_json,identity,now,read_json
from lab_client import Client, ClientError
from workflow_spec import CAPABILITIES
from state_lock import state_lock
from resources import MULTI_SLOT,validate_policy,memory_cost

STOP=threading.Event()


class NotStartedCancelled(RuntimeError):pass


class NodeClient:
    def __init__(self,config):
        self.config=config
        self.client=Client({'controller':config['controller'],'token':config['node_key'],
                            **({'ca_file':config['ca_file']} if config.get('ca_file') else {})})

    def call(self,action,body):
        if action not in {'heartbeat','claim','start','update','blob','complete','input','owner-commit','owner-ready'}:
            raise ValueError('Invalid worker operation')
        try:
            return self.client.request('/api/worker/'+action,body)
        except ClientError as error:
            raise RuntimeError('Controller node operation '+action+': '+str(error)) from None


class Worker:
    def __init__(self,config):
        self.config=config;self.client=NodeClient(config)
        self.inventory_lock=threading.RLock()
        self.root=Path(config['state_dir']).resolve()
        if config.get('workspace'):
            from quota_workspace import separate_control
            separate_control(config['workspace'],self.root)
        self.root.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.state_lock=state_lock(self.root)
        from node_owner_admin import guard
        try:
            from node_owner_provision import guard as participant_guard
            participant_guard(self.root);guard(self.root)
        except BaseException:
            self.state_lock.close();raise
        self.previous_cpu=None
        self.lock=(self.root/'worker.lock').open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from docker_runtime import discover,DockerRuntimeError
        runtime_path=self.root/'docker-runtime.json'
        previous=read_json(runtime_path)
        if previous and config.get('docker_runtime') and previous!=config['docker_runtime']:
            raise DockerRuntimeError('Configured Docker daemon differs from saved node identity; reconcile without overwriting the old binding')
        binding,_=discover(config.get('docker_runtime') or previous)
        self.config={**self.config,'docker_runtime':binding}
        atomic_json(runtime_path,binding)
        if physical_identity()!=config['physical_id']:raise ValueError('Worker host differs from enrolled physical identity')
        if not set(config['policy']['cpu_ids'])<=os.sched_getaffinity(0):raise ValueError('Worker cannot access enrolled CPUs')
        validate_policy(config['policy'])
        if config.get('npu'):
            from npu_device import validate
            validate(config['npu'],host=True)
        if config.get('workspace'):
            from workspace_slots import validate
            validate(config['workspace'],host=True)
        self.spool=None
        if config.get('spool_storage'):
            from spool_storage import Spool
            self.spool=Spool(self.root,self.config,check=False)
        self.cache=None
        if config.get('persistent_cache'):
            from persistent_cache import Pool
            self.cache=Pool(self.root,self.config)
        self.npu_cache=None;self.npu_sampled=0
        self.artifact_gc_ready=True
        atomic_json(self.root/'worker-process.json',identity(os.getpid()))

    def heartbeat(self):
        from docker_runtime import discover,image_available,CAPABILITY as PINNED_RUNTIME
        discover(self.config['docker_runtime'])
        image_available(self.config['docker_runtime'],self.config['image'])
        from environment_contract import CAPABILITY as ENVIRONMENT,PROBE_MEMORY_CAPABILITY,verify_config
        environment_sha=verify_config(self.config)
        available=0
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):available=int(line.split()[1])*1024
        stat=os.statvfs(self.root)
        spool_reading=self.spool.observe() if self.spool else None
        fields=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        current=(sum(fields),fields[3]+fields[4]);cpu=None
        if self.previous_cpu and current[0]>self.previous_cpu[0]:
            cpu=max(0,min(100,100*(1-(current[1]-self.previous_cpu[1])/(current[0]-self.previous_cpu[0]))))
        self.previous_cpu=current
        from attempt_inventory import CAPABILITY as INVENTORY,local as attempt_inventory
        capabilities=['start-ticket-v1','cleanup-before-complete-v1',PINNED_RUNTIME,ENVIRONMENT,INVENTORY,MULTI_SLOT,*sorted(CAPABILITIES)]
        capabilities.append(PROBE_MEMORY_CAPABILITY)
        from workflow_spec import DEADLINE_CAPABILITY,MAX_TIMEOUT_SECONDS
        maximum=self.config.get('max_timeout_seconds',300)
        if type(maximum) is not int or not 1<=maximum<=MAX_TIMEOUT_SECONDS:raise ValueError('Invalid node execution timeout limit')
        capabilities.append(DEADLINE_CAPABILITY)
        from io_policy import limits,CAPABILITY
        capabilities.append(CAPABILITY)
        from node_storage import CAPABILITY as SPOOL
        capabilities.append(SPOOL)
        npu={}
        if self.spool:
            from spool_storage import CAPABILITY
            capabilities.append(CAPABILITY);npu['spool_observation']=spool_reading
        if self.cache:
            from persistent_cache import CAPABILITY
            capabilities.append(CAPABILITY);npu['cache_observation']=self.cache.observe()
        if self.config.get('workspace'):
            from workspace_slots import observe,CAPABILITY
            capabilities.append(CAPABILITY);npu['workspace_observation']=observe(self.config['workspace'])
            from quota_workspace import CAPABILITY as UID_QUOTA,is_quota
            if any(is_quota(s) for s in self.config['workspace']['slots']):capabilities.append(UID_QUOTA)
        if self.config.get('npu'):
            from npu_device import observe,fingerprint,CAPABILITY
            capabilities.append(CAPABILITY)
            if time.monotonic()-self.npu_sampled>=10 or self.npu_cache is None:
                self.npu_sampled=time.monotonic()
                try:self.npu_cache=observe(self.config['npu'])
                except Exception:self.npu_cache={'status':'error','sampled_at':None,'devices':[],'policy_sha256':fingerprint(self.config['npu'])}
            npu.update(npu_observation=self.npu_cache)
            from device_qualification import CAPABILITY as ACCESS,host_fingerprint
            from workflow_spec import runtime_manifest
            capabilities.append(ACCESS)
            try:host_sha=host_fingerprint(self.config['docker_runtime'])
            except (OSError,ValueError):host_sha=None
            npu.update(access_host_sha256=host_sha,access_runtime_files=runtime_manifest())
        body={'physical_id':physical_identity(),
             'image_sha256':self.config['image'],'environment_setup':self.config.get('environment_setup'),
             'python_executable':self.config.get('python','/usr/bin/python3'),
             'environment_sha256':environment_sha,
             'max_timeout_seconds':maximum,
             'policy_revision':self.config.get('policy_revision',1),
             'protocol_version':1,'capabilities':capabilities,**npu,
             'io_limits':limits(self.config.get('io_limits')),
             'cpu_ids':sorted(os.sched_getaffinity(0)),'memory_available_bytes':available,
             'host_cpu_utilization_pct':cpu,
             'disk_free_bytes':spool_reading['free_bytes'] if self.spool else stat.f_bavail*stat.f_frsize,
             'disk_free_inodes':spool_reading['free_inodes'] if self.spool else stat.f_favail,
             'artifact_gc_ready':self.artifact_gc_ready}
        # A running snapshot must reach the controller before that attempt's
        # terminal completion can commit. Otherwise an ordinary concurrent
        # finish turns a valid, older snapshot into a durable inventory hold.
        # Keep slow resource discovery and bulk uploads outside this lock.
        with self.inventory_lock:
            body['attempt_inventory']=attempt_inventory(self.root)
            receipt=self.client.call('heartbeat',body)
        if receipt.get('accepted') is not True or receipt.get('node_id')!=self.config['node_id']:raise RuntimeError('Heartbeat did not acknowledge this Node')
        from unit_start import START_ENV
        atomic_json(self.root/'heartbeat-receipt.json',{**identity(os.getpid()),'node_id':self.config['node_id'],
            'acknowledged_at':time.time(),'service_start_id':os.environ.get(START_ENV)})
        return receipt

    def auth_body(self,grant):
        return {key:grant[key] for key in ['job_id','attempt_id','lease_token']}

    def complete(self,body):
        # Cleanup was durably recorded before upload. Drain any heartbeat that
        # captured the earlier running state before sending complete. Future
        # snapshots see cleaned=True, so they may proceed during a slow final
        # checksum/ACK; holding this lock across complete would hide liveness.
        with self.inventory_lock:pass
        return self.client.call('complete',body)

    def upload(self,engine,result):
        if engine.spool:engine.spool.check()
        auth=self.auth_body(engine.grant);manifest=[]
        for file in sorted(engine.artifacts.rglob('*')):
            if not file.is_file() or file.is_symlink():continue
            name=str(file.relative_to(engine.artifacts));size=file.stat().st_size
            if not size:continue
            digest=hashlib.sha256()
            with file.open('rb') as source:
                offset=0
                while True:
                    data=source.read(256*1024)
                    if not data:break
                    digest.update(data)
                    self.client.call('blob',{**auth,'name':name,'offset':offset,'data':base64.b64encode(data).decode()})
                    offset+=len(data)
            manifest.append({'name':name,'size':size,'sha256':digest.hexdigest()})
        atomic_json(engine.control/'artifact-transfer.json',{'manifest':manifest})
        response=self.complete({**auth,'result':result,'artifacts':manifest})
        self.acknowledge(engine,response,manifest)
        return response

    def acknowledge(self,engine,response,manifest):
        if response.get('accepted') is not True or response.get('state') not in {'succeeded','failed','cancelled','timed_out'}:
            raise RuntimeError('Controller did not confirm a terminal result; node copies retained')
        from node_storage import confirmed,prune
        receipt=response.get('artifact_receipt')
        engine.state['controller_acknowledged']=True
        if receipt is not None:
            if not confirmed(engine.grant,manifest,receipt):raise RuntimeError('Controller checksum receipt differs; node copies retained')
            engine.state['artifact_receipt']=receipt
            engine.state['artifact_gc_requested']=True
        engine.save()
        if receipt is not None:
            try:
                for _ in range(40):
                    if prune(self.root,engine.grant['attempt_id'],self.config):break
                else:self.artifact_gc_ready=False
            except Exception:
                self.artifact_gc_ready=False
                raise

    def reap(self):
        from node_storage import prune,pending
        requests=pending(self.root)
        self.artifact_gc_ready=not requests
        if requests:
            prune(self.root,requests[0],self.config)
            self.artifact_gc_ready=not pending(self.root)

    def finish_attempt(self,engine,result):
        # Helpers and workspace cleanup remain inside the reserved resource lifetime.
        if engine.grant['resources']['npu']:
            if engine.state.get('started_epoch'):
                from npu_device import check
                result['npu_release_observation']=check(engine.grant,engine.config['npu'])
                result['npu_release_basis']='clear-device-observation'
            else:
                result['npu_release_basis']='no-container-start-intent'
            result['npu_release_confirmed']=True
        engine.cleanup()
        if engine.workspace:result['workspace_release_confirmed']=True
        if engine.cache:result['cache_release_confirmed']=True
        result['execution_cleanup_confirmed']=True
        result.setdefault('execution_finished_at',result.get('finished_at'))
        atomic_json(engine.control/'result.json',result)
        self.upload(engine,result)

    def download_input(self,engine):
        from inputs import safe_name,sha256_file
        manifest=engine.grant.get('input')
        if not manifest:return
        engine.inputs.mkdir(mode=0o755,exist_ok=True)
        for item in manifest['files']:
            name=item['name']
            if not safe_name(name):raise ValueError('Unsafe assigned input name')
            target=engine.inputs/name;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            if target.is_symlink() or not target.resolve().is_relative_to(engine.inputs.resolve()):raise ValueError('Unsafe node input path')
            if target.exists() and target.stat().st_size==item['size'] and sha256_file(target)==item['sha256']:continue
            offset=target.stat().st_size if target.exists() else 0
            if offset>item['size']:raise ValueError('Assigned input exceeds manifest size')
            with target.open('ab') as file:
                while offset<item['size']:
                    response=self.client.call('input',{**self.auth_body(engine.grant),'name':name,'offset':offset})
                    chunk=base64.b64decode(response['data'],validate=True)
                    if response['offset']!=offset or response['name']!=name or response['size']!=item['size'] or not 0<len(chunk)<=256*1024 or offset+len(chunk)>item['size']:raise ValueError('Invalid assigned input response')
                    file.write(chunk);file.flush();offset+=len(chunk)
            if sha256_file(target)!=item['sha256']:raise ValueError('Node input checksum differs')
            target.chmod(0o444)
        # Preserve the immutable manifest's execute bit even when the input is mounted read-only.
        # Re-run this on recovery too: a completed download may still have old/default permissions.
        modes={item['name']:0o555 if item.get('executable',False) else 0o444 for item in manifest['files']}
        for path in engine.inputs.rglob('*'):
            if path.is_symlink():raise ValueError('Assigned input contains a symlink')
            if path.is_dir():path.chmod(0o755)
            else:
                name=str(path.relative_to(engine.inputs))
                if name not in modes:raise ValueError('Unregistered file in assigned input directory')
                path.chmod(modes[name])
        engine.inputs.chmod(0o755)

    def attempt(self,grant):
        if grant.get('version')!=1 or grant.get('node_id')!=self.config['node_id'] or grant.get('physical_id')!=self.config['physical_id']:
            raise ValueError('Execution grant is assigned to another node')
        engine=DockerAttempt(self.root,grant,self.config)
        if engine.state.get('stage')=='cleaned' and engine.state.get('controller_acknowledged'):return
        try:
            receipt=self.client.call('update',{**self.auth_body(grant),'running':False})
            if receipt.get('terminal'):
                engine.cleanup()
                transfer=read_json(engine.control/'artifact-transfer.json')
                saved_result=read_json(engine.control/'result.json')
                if transfer is not None and saved_result is not None:
                    response=self.complete({**self.auth_body(grant),'result':saved_result,'artifacts':transfer['manifest']})
                    self.acknowledge(engine,response,transfer['manifest'])
                else:engine.state['controller_acknowledged']=True;engine.save()
                return
            if receipt.get('cancel') and engine.state.get('stage')=='new':
                raise NotStartedCancelled('Task was cancelled before the saved start intent was executed')
            saved_result=read_json(engine.control/'result.json')
            if saved_result:
                self.finish_attempt(engine,saved_result)
                return
            if engine.state.get('stage')=='new':
                engine.prepare_spool()
                engine.validate()
                from node_storage import pressure,CAPABILITY as SPOOL
                space=os.statvfs(engine.spool.base if engine.spool else self.root)
                held=[item for item in self.recover() if item['attempt_id']!=grant['attempt_id']]
                reason=pressure(grant,{'disk_free_bytes':space.f_bavail*space.f_frsize,'disk_free_inodes':space.f_favail,
                                     'capabilities':[SPOOL],'artifact_gc_ready':self.artifact_gc_ready},held)
                if reason:raise RuntimeError(reason)
                self.download_input(engine)
            if STOP.is_set():return
            if grant['resources']['npu'] and engine.state.get('stage')=='new':
                from npu_device import check
                atomic_json(engine.control/'npu-admission.json',check(grant,engine.config['npu']))
            if grant.get('start_ticket_required') and engine.state.get('stage')=='new' and not engine.state.get('start_authorized_locally'):
                if engine.inspect() is not None:
                    engine.state['stage']='uncertain';engine.save()
                else:
                    while not STOP.is_set():
                        permission=self.client.call('start',self.auth_body(grant))
                        if not permission.get('wait'):break
                        state=self.client.call('update',{**self.auth_body(grant),'running':False})
                        if state.get('cancel'):raise NotStartedCancelled('Task was cancelled while awaiting inventory reconciliation')
                        STOP.wait(.5)
                    if STOP.is_set():return
                    if not permission['start_permitted']:
                        if permission.get('already_authorized'):
                            engine.state['stage']='uncertain';engine.save()
                            raise RuntimeError('Start ticket was already consumed; missing local state will not trigger another execution')
                        if permission.get('cancel'):raise NotStartedCancelled('Task was cancelled before any start permission')
                        raise RuntimeError(permission.get('reason','Node start permission was refused'))
                    engine.state['start_authorized_locally']=True;engine.save()
            if STOP.is_set():return
            engine.start()
            if receipt.get('cancel'):engine.tick(cancel=True)
            while not STOP.is_set():
                result=engine.tick()
                if result:
                    self.finish_attempt(engine,result)
                    print(json.dumps({'at':now(),'job_id':grant['job_id'],'attempt_id':grant['attempt_id'],'state':result['status']}),flush=True)
                    return
                state=self.client.call('update',{**self.auth_body(grant),'running':True,'logs':engine.preview()})
                if state.get('cancel'):engine.tick(cancel=True)
                STOP.wait(.5)
        except Exception as error:
            try:info=engine.inspect()
            except Exception:
                try:self.client.call('update',{**self.auth_body(grant),'running':False,'attention':True})
                except RuntimeError:pass
                raise
            from storage_io import exhausted
            from spool_storage import SpoolUnavailable
            from persistent_cache import CacheUnavailable
            if isinstance(error,CacheUnavailable):
                try:self.client.call('update',{**self.auth_body(grant),'running':False,'attention':True,'cache_pending':True})
                except RuntimeError:pass
                raise
            if exhausted(error) or isinstance(error,SpoolUnavailable):
                engine.state['output_pending']={'kind':'capacity-or-spool','observed_at':now()};engine.save()
                try:self.client.call('update',{**self.auth_body(grant),'running':False,'attention':True,
                    'output_pending':True,'execution_ended':bool(engine.state.get('execution_result'))})
                except RuntimeError:pass
                raise
            if engine.state.get('stage') in {'new','create-failed','prestart-rejected'} and (info is None or not info['State']['Running'] and info['State']['Status']=='created'):
                # No container start intent was issued, even if create already succeeded.
                result={'status':'cancelled' if isinstance(error,NotStartedCancelled) else 'failed','exit_code':None,'started_at':None,'finished_at':now(),
                        'termination_confirmed':True,'failure_stage':'container_create' if engine.state.get('stage')=='create-failed' else 'node_preflight',
                        'reason':'Docker create failed before task start: '+engine.state.get('create_failure','unknown') if engine.state.get('stage')=='create-failed' else str(error)[:500]}
                atomic_json(engine.artifacts/'environment.json',{'runtime':'managed-docker','execution_started':False})
                atomic_json(engine.control/'result.json',result)
                engine.state['stage']='preflight-failed';engine.save()
                self.finish_attempt(engine,result)
                return
            try:self.client.call('update',{**self.auth_body(grant),'running':False,'attention':True})
            except RuntimeError:pass
            raise
        finally:
            try:engine.stop_log_client()
            finally:engine.close_spool()

    def recover(self):
        grants=[]
        for path in sorted((self.root/'attempts').glob('*/control/grant.json')):
            state=read_json(path.parent/'state.json') or {}
            if state.get('stage')=='cleaned' and state.get('controller_acknowledged'):continue
            grants.append(read_json(path))
        return grants

    def check_capacity(self,grant,held):
        # A controller grant still has to fit this node's local policy as a group.
        group={item['attempt_id']:item for item in [*held,grant]}
        policy=self.config['policy'];cpus=[cpu for item in group.values() for cpu in item['allocation']['cpu_ids']]
        npus=[device for item in group.values() for device in item['allocation'].get('npu_ids',[])]
        if (len(group)>policy['max_running'] or len(cpus)!=len(set(cpus)) or not set(cpus)<=set(policy['cpu_ids'])
            or len(npus)!=len(set(npus))
            or sum(memory_cost(item,True) for item in group.values())>policy['memory_mib']
            or sum(item['resources'].get('shm_mib',0) for item in group.values())>policy['shm_mib']):
            raise RuntimeError('Assigned attempts exceed the local node envelope; reservations remain held')

    def run(self):
        slots=self.config['policy']['max_running'];futures={};retry_after={};submitted={};pulsed={}
        with ThreadPoolExecutor(max_workers=slots,thread_name_prefix='attempt') as pool:
            while not STOP.is_set():
                try:
                    from node_owner_provision import guard as participant_guard
                    participant_guard(self.root)
                    try:self.reap()
                    except Exception:
                        self.artifact_gc_ready=False
                    self.heartbeat()
                    from unit_start import admission_reason
                    if admission_reason(self.root):
                        STOP.wait(.5)
                        continue
                    for attempt,(future,grant) in list(futures.items()):
                        if not future.done():continue
                        del futures[attempt]
                        submitted.pop(attempt,None);pulsed.pop(attempt,None)
                        try:future.result();retry_after.pop(attempt,None)
                        except Exception as error:
                            retry_after[attempt]=time.monotonic()+5
                            print(json.dumps({'at':now(),'attempt_id':attempt,'error':str(error)[:500]}),flush=True)
                    for attempt,(future,grant) in futures.items():
                        if time.monotonic()-pulsed.get(attempt,0)>=3:
                            self.pulse_attempt(grant,submitted[attempt]);pulsed[attempt]=time.monotonic()
                    held={grant['attempt_id']:grant for grant in self.recover()}
                    held.update({attempt:grant for attempt,(_,grant) in futures.items()})
                    for attempt,grant in held.items():
                        if len(futures)>=slots:break
                        if attempt in futures or time.monotonic()<retry_after.get(attempt,0):continue
                        self.check_capacity(grant,list(held.values()))
                        submitted[attempt]=time.monotonic()
                        futures[attempt]=(pool.submit(self.attempt,grant),grant)
                    if len(futures)<slots and not STOP.is_set() and self.artifact_gc_ready:
                        grant=self.client.call('claim',{'exclude_attempts':list(held)})['grant']
                        if grant:
                            self.check_capacity(grant,list(held.values()))
                            submitted[grant['attempt_id']]=time.monotonic()
                            futures[grant['attempt_id']]=(pool.submit(self.attempt,grant),grant)
                except Exception as error:
                    # Errors carry operation names, never headers, credentials or complete grants.
                    print(json.dumps({'at':now(),'error':str(error)[:500]}),flush=True)
                STOP.wait(.5)

    def pulse_attempt(self,grant,submitted_at):
        # Liveness only: it never asserts container state or releases a reservation.
        # Opaque operations cannot be kept alive indefinitely. Upload/download chunks
        # separately record actual progress at the controller.
        if time.monotonic()-submitted_at>grant.get('timeout_seconds',30)+120:return False
        self.client.call('update',{**self.auth_body(grant),'running':False})
        return True


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    os.umask(0o077);os.nice(10)
    for signum in [signal.SIGTERM,signal.SIGINT]:signal.signal(signum,lambda *_:STOP.set())
    from lab_client import resolve_ca
    worker=Worker(resolve_ca(read_json(args.config),args.config))
    try:worker.run()
    finally:
        (worker.root/'worker-process.json').unlink(missing_ok=True)
        worker.lock.close()


if __name__=='__main__':main()

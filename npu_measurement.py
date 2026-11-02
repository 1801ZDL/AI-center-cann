"""Attempt-scoped measurement markers and bounded, Node-owned observations.

The workload chooses markers. Node clocks and observations cannot attest that
all GPU work occurred between them, or certify frequency stability/performance.
No operation runs a workload, changes resources, or accepts arbitrary queries.
"""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import threading
import time

CAPABILITY='npu-measurement-window-v1'
SCHEMA='ascend-lab-measurement-windows/v1'
MAX_WINDOWS=4
MAX_CONNECTIONS=32
SOCKET='endpoint.sock'
MOUNT='/lab-measurement'
ID=re.compile('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,47}')


def source_sha():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def epoch(value):
    if not isinstance(value,str) or len(value)>64:raise ValueError('Invalid clock')
    value=dt.datetime.fromisoformat(value)
    if value.tzinfo is None:raise ValueError('Clock needs timezone')
    return value.timestamp()
def contract():return {'schema':SCHEMA,'source_sha256':source_sha(),'max_windows':MAX_WINDOWS}
def receive(connection):
    raw=b''
    while b'\n' not in raw:
        block=connection.recv(min(2048,8193-len(raw)))
        if not block:raise ValueError('Incomplete measurement message')
        raw+=block
        if len(raw)>8192:raise ValueError('Measurement message too large')
    line,extra=raw.split(b'\n',1)
    if extra:raise ValueError('Extra measurement data')
    value=json.loads(line)
    if not isinstance(value,dict):raise ValueError('Expected measurement object')
    return value


def request(operation,window_id,configuration=None):
    """Task-side marker. Retry the same ID after a lost response, never invent one."""
    if operation not in {'begin','end'} or not isinstance(window_id,str) or not ID.fullmatch(window_id):raise ValueError('Invalid measurement marker')
    configuration=configuration or json.loads(Path('/payload/run.json').read_text())
    binding=configuration.get('measurement')
    if not binding:return {'ok':False,'status':'unsupported'}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(20);connection.connect(binding['socket'])
        peer=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if peer[1]!=binding['node_uid']:raise ValueError('Unexpected measurement endpoint identity')
        connection.sendall(json.dumps({'operation':operation,'window_id':window_id,'token':binding['token'],
            'job_id':configuration['job_id'],'attempt_id':binding['attempt_id']}).encode()+b'\n')
        return receive(connection)


class Broker:
    """One listener per supervised attempt; no timer or unbounded sampler."""
    def __init__(self,control,grant,uid,probe=None):
        self.control=Path(control);self.grant=grant;self.uid=uid;self.probe=probe
        self.directory=self.control/'measurement-endpoint';self.path=self.control/'measurement.json'
        self.listener=None;self.thread=None;self.current=None;self.fd=None;self.lock=None
        self.stopping=threading.Event();self.identity=None;self.closed=False

    def save(self):
        from jobs import atomic_json
        atomic_json(self.path,self.state)

    def open(self):
        import fcntl
        from jobs import read_json
        if self.grant.get('measurement_contract')!=contract():raise ValueError('Measurement runtime differs from frozen grant')
        self.lock=(self.control/'measurement.lock').open('a+b')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.state=read_json(self.path)
            binding={k:self.grant.get(k) for k in ['job_id','attempt_id','node_id','physical_id','environment_sha256','npu_policy_sha256']}
            binding['device_ids']=self.grant['allocation']['npu_ids']
            if self.state:
                if self.state['binding']!=binding or self.state['source_sha256']!=source_sha():raise ValueError('Retained measurement identity differs')
                for row in self.state['windows']:
                    if row['state'] in {'begin_pending','open','end_pending'}:row['state']='interrupted'
            else:
                self.state={'schema':SCHEMA,'source_sha256':source_sha(),'binding':binding,'token':secrets.token_hex(24),
                    'windows':[],'queries':0,'connections':0,'restarts':-1,'binding_intent':False,'endpoint_identity':None}
            self.state['restarts']+=1
            self.directory.mkdir(mode=0o711,exist_ok=True)
            info=self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o022:raise ValueError('Unprotected measurement endpoint directory')
            self.directory.chmod(0o711)
            self.fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:old=os.stat(SOCKET,dir_fd=self.fd,follow_symlinks=False)
            except FileNotFoundError:old=None
            if old:
                expected=self.state.get('endpoint_identity')
                if (not stat.S_ISSOCK(old.st_mode) or old.st_uid!=self.uid
                    or not (expected==[old.st_dev,old.st_ino] or self.state.get('binding_intent'))):raise ValueError('Unexpected measurement endpoint retained')
                os.unlink(SOCKET,dir_fd=self.fd)
            self.state.update(binding_intent=True,endpoint_identity=None);self.save()
            self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.listener.bind('/proc/self/fd/'+str(self.fd)+'/'+SOCKET)
            os.chown(SOCKET,self.uid,-1,dir_fd=self.fd,follow_symlinks=False)
            os.chmod(SOCKET,0o600,dir_fd=self.fd,follow_symlinks=False)
            info=os.stat(SOCKET,dir_fd=self.fd,follow_symlinks=False);self.identity=[info.st_dev,info.st_ino]
            self.state.update(binding_intent=False,endpoint_identity=self.identity);self.save()
            self.listener.listen(2);self.listener.settimeout(.2)
            self.thread=threading.Thread(target=self.serve,daemon=True,name='task-measurement');self.thread.start()
            return self
        except BaseException:
            self.close();raise

    def public(self):
        return {'schema':SCHEMA,'socket':MOUNT+'/'+SOCKET,'token':self.state['token'],
                'attempt_id':self.grant['attempt_id'],'node_uid':os.geteuid()}

    def observation(self):
        if not self.grant['allocation']['npu_ids']:return {'status':'not_applicable'}
        self.state['queries']+=1;self.save()  # Charge before querying; a crash cannot reissue it.
        try:
            if self.probe:raw=self.probe()
            else:
                from npu_device import observe
                raw=observe(self.policy,frequency_ids=self.grant['allocation']['npu_ids'])
            from npu_conditions import sample
            return {'status':'sampled',**sample(raw,{**self.grant,'_npu_policy_sha256':self.grant['npu_policy_sha256']})}
        except Exception:return {'status':'unavailable'}

    def handle(self,value,peer_uid):
        if (set(value)!={'operation','window_id','token','job_id','attempt_id'} or peer_uid!=self.uid
            or value['job_id']!=self.grant['job_id'] or value['attempt_id']!=self.grant['attempt_id']
            or not isinstance(value['token'],str) or not secrets.compare_digest(value['token'],self.state['token'])
            or not isinstance(value['window_id'],str) or not ID.fullmatch(value['window_id'])
            or value['operation'] not in {'begin','end'}):raise ValueError('Measurement identity or operation refused')
        operation=value['operation'];identifier=value['window_id']
        row=next((r for r in self.state['windows'] if r['id']==identifier),None)
        if row and row['state']=='interrupted':return {'ok':False,'status':'interrupted','window_id':identifier}
        if operation=='begin':
            if row:return row.get('begin_reply',{'ok':False,'status':'unconfirmed'})
            if len(self.state['windows'])>=MAX_WINDOWS or any(r['state'] in {'begin_pending','open','end_pending'} for r in self.state['windows']):raise ValueError('Measurement window limit or overlap')
            row={'id':identifier,'state':'begin_pending'};self.state['windows'].append(row);self.save()
            row['before']=self.observation()
            row.update(started_at=now(),started_monotonic=time.monotonic(),state='open')
            row['begin_reply']={'ok':True,'status':'open','window_id':identifier,'node_observed_at':row['started_at']}
            self.save();return row['begin_reply']
        if row is None:raise ValueError('Measurement end has no original begin')
        if row['state']=='closed':return row['end_reply']
        if row['state']!='open':return {'ok':False,'status':'unconfirmed','window_id':identifier}
        row.update(ended_at=now(),ended_monotonic=time.monotonic(),state='end_pending');self.save()
        row['after']=self.observation();row['state']='closed'
        row['end_reply']={'ok':True,'status':'closed','window_id':identifier,'node_observed_at':row['ended_at']}
        self.save();return row['end_reply']

    def serve(self):
        while not self.stopping.is_set() and self.state['connections']<MAX_CONNECTIONS:
            try:connection,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:return
            with connection:
                self.current=connection;connection.settimeout(1)
                try:
                    self.state['connections']+=1;self.save()
                    peer=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    reply=self.handle(receive(connection),peer[1])
                except Exception:reply={'ok':False,'status':'refused'}
                try:connection.sendall(json.dumps(reply,allow_nan=False).encode()+b'\n')
                except OSError:pass
                self.current=None

    def close(self):
        if self.closed:return
        self.stopping.set()
        if self.current:
            try:self.current.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        if self.thread:
            self.thread.join(20)
            if self.thread.is_alive():raise RuntimeError('Measurement query has not settled; original attempt remains held')
        if hasattr(self,'state'):
            for row in self.state['windows']:
                if row['state'] in {'begin_pending','open','end_pending'}:row['state']='interrupted'
            self.save()
        if self.listener:self.listener.close();self.listener=None
        if self.fd is not None:
            try:
                info=os.stat(SOCKET,dir_fd=self.fd,follow_symlinks=False)
                if [info.st_dev,info.st_ino]==self.identity:os.unlink(SOCKET,dir_fd=self.fd)
            except FileNotFoundError:pass
            finally:os.close(self.fd);self.fd=None
        if self.lock:self.lock.close();self.lock=None
        self.closed=True

    def document(self):
        return {k:v for k,v in self.state.items() if k in {'schema','source_sha256','binding','queries','restarts','windows'}}


def normalize(job,value,trace=None,execution_finished_at=None):
    """Validate authenticated Node metadata; never fail/rewrite workload results."""
    if not job.get('_measurement_contract'):return None
    base={'schema':SCHEMA,'source_sha256':job['_measurement_contract']['source_sha256'],
        'job_id':job['id'],'attempt_id':job['attempt_id'],'node_id':job['node_id'],
        'physical_id':job.get('_physical_id'),'device_ids':job['allocation']['npu_ids'],
        'status':'unconfirmed','windows':[],'observed_alerts':0,'performance_qualified':False,
        'scope':'Workload-requested markers observed by Node; boundary reads are outside the timed interval. No continuous clock or GPU-work attestation.'}
    if value is None:base['reason']='missing_node_evidence';return base
    try:
        if len(json.dumps(value,allow_nan=False))>65536:raise ValueError()
        binding={k:job.get(k if k not in {'job_id','physical_id','npu_policy_sha256'} else {'job_id':'id','physical_id':'_physical_id','npu_policy_sha256':'_npu_policy_sha256'}[k])
                 for k in ['job_id','attempt_id','node_id','physical_id','environment_sha256','npu_policy_sha256']}
        binding['device_ids']=job['allocation']['npu_ids']
        if (value.get('schema')!=SCHEMA or value.get('source_sha256')!=job['_measurement_contract']['source_sha256'] or value.get('binding')!=binding
            or type(value.get('queries')) is not int or not 0<=value['queries']<=MAX_WINDOWS*2
            or type(value.get('restarts')) is not int or value['restarts']<0
            or not isinstance(value.get('windows'),list) or len(value['windows'])>MAX_WINDOWS):raise ValueError()
        identifiers=set()
        for raw in value['windows']:
            identifier=raw.get('id')
            if not isinstance(identifier,str) or not ID.fullmatch(identifier) or identifier in identifiers:raise ValueError()
            identifiers.add(identifier);row={'id':identifier,'status':'interrupted'};base['windows'].append(row)
            if raw.get('state')!='closed':continue
            start=epoch(raw['started_at']);end=epoch(raw['ended_at'])
            if any(type(raw.get(k)) not in {float,int} or not math.isfinite(raw[k]) for k in ['started_monotonic','ended_monotonic']):raise ValueError()
            duration=raw['ended_monotonic']-raw['started_monotonic']
            if (type(duration) not in {float,int} or not math.isfinite(duration) or not 0<=duration<=job['timeout_seconds']+15
                or not epoch(job['reserved_at'])-15<=start<=end<=time.time()+1 or abs(end-start-duration)>5
                or execution_finished_at is not None and end>epoch(execution_finished_at)+1):
                row['status']='clock_unconfirmed';continue
            row.update(started_at=raw['started_at'],ended_at=raw['ended_at'],duration_seconds=duration,
                       status='boundary_observations_only',observations={},heartbeat_samples_in_window=0,frequency_samples_in_window=0,
                       heartbeat_retention_incomplete=bool((trace or {}).get('dropped_samples')))
            for side in ['before','after']:
                observation=raw.get(side,{})
                if observation.get('status')=='sampled':
                    from npu_conditions import sample
                    normalized=sample({**observation,'status':'ok','policy_sha256':job.get('_npu_policy_sha256')},job)
                    when=epoch(normalized['sampled_at'])
                    if not epoch(job['reserved_at'])-15<=when<=time.time()+1:raise ValueError()
                    if side=='before' and when>start or side=='after' and when<end:raise ValueError()
                    row['observations'][side]=normalized
                    base['observed_alerts']+=sum(card['health'] in {'Warning','Alarm'} for card in normalized['devices'])
                else:row['observations'][side]={'status':'not_applicable' if not binding['device_ids'] else 'unavailable'}
            inside=[];frequencies={}
            for reading in (trace or {}).get('samples',[]):
                if start<=epoch(reading['sampled_at'])<=end:
                    inside.append(reading);row['heartbeat_samples_in_window']+=1
                    base['observed_alerts']+=sum(card['health'] in {'Warning','Alarm'} for card in reading['devices'])
                    for card in reading['devices']:
                        frequency=card.get('frequency',{})
                        if frequency.get('status')=='sampled' and start<=epoch(frequency['sampled_at'])<=end:
                            row['frequency_samples_in_window']+=1
                            values=frequencies.setdefault(card['id'],[]);values.append(frequency['current_mhz'])
            row['heartbeat_observations_in_window']=inside
            row['frequency_in_window']=[{'device_id':card,'samples':len(values),'min_mhz':min(values),'max_mhz':max(values)} for card,values in sorted(frequencies.items())]
            if not binding['device_ids']:row['status']='not_applicable'
            elif any('sampled_at' not in v for v in row['observations'].values()):row['status']='boundary_observations_unavailable'
        base.update(status=('unconfirmed' if any(r['status'] in {'interrupted','clock_unconfirmed','boundary_observations_unavailable'} for r in base['windows']) else 'recorded') if base['windows'] else 'not_requested',queries=value['queries'],restarts=value['restarts'])
        return base
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
        base.update(status='unconfirmed',reason='invalid_node_evidence',windows=[]);return base


if __name__=='__main__':
    import sys
    print(json.dumps(request(*sys.argv[1:]),allow_nan=False))

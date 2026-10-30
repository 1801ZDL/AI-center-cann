"""Host-side management query for the cooperative local Docker executor.

This separates management access from task identity. It does not provide device
isolation or replace managed-node admission. No workload can choose a query argv.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time

from collector import command,parse_npu
from jobs import now

MAX_MESSAGE=8192
RECEIPT_TTL=5
SOCKET_NAME='npu-admission.sock'


def query(cann_env='/data/pri/cann/set_env.sh',tool=None):
    tool=tool or shutil.which('npu-smi') or '/usr/local/sbin/npu-smi'
    return parse_npu(command(['bash','--noprofile','--norc','-c',
        'source "$1" >/dev/null 2>&1 || exit 126; exec "$2" info',
        'ascend-lab-host-admission',cann_env,tool],timeout=12))


def selected_reading(device_ids,observation):
    devices,processes=observation;selected=[]
    if not device_ids or len(device_ids)!=len(set(device_ids)) or any(type(value) is not int or value<0 for value in device_ids):
        raise RuntimeError('Invalid allocated NPU set; user workload was not started')
    for device_id in device_ids:
        device=next((item for item in devices if item['id']==device_id),None)
        count=sum(item['device_id']==device_id for item in processes)
        if not device or device['health']!='OK' or device['utilization_pct']!=0 or count or device.get('process_count',0)!=0:
            raise RuntimeError('NPU health or occupancy changed before launch; user workload was not started')
        selected.append({'id':device_id,'health':device['health'],'utilization_pct':device['utilization_pct'],'process_count':count})
    return selected


def _receive(connection):
    result=b''
    while b'\n' not in result:
        block=connection.recv(min(2048,MAX_MESSAGE+1-len(result)))
        if not block:raise RuntimeError('NPU admission channel ended before a receipt')
        result+=block
        if len(result)>MAX_MESSAGE:raise RuntimeError('NPU admission message exceeds limit')
    line,extra=result.split(b'\n',1)
    if extra:raise RuntimeError('Unexpected extra NPU admission data')
    value=json.loads(line)
    if not isinstance(value,dict):raise RuntimeError('Invalid NPU admission message')
    return value


class HostBroker:
    def __init__(self,directory,job,probe=None):
        self.path=Path(directory)/SOCKET_NAME;self.job=job;self.probe=probe or query
        self.stop=threading.Event();self.used=False;self.listener=None;self.thread=None;self.current=None

    def __enter__(self):
        # Never remove a pre-existing endpoint or guess that a previous supervisor died.
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path));self.bound_identity=self.path.stat().st_ino
            self.path.chmod(0o600);self.listener.listen(2);self.listener.settimeout(.2)
            self.thread=threading.Thread(target=self.serve,name='npu-admission',daemon=True);self.thread.start()
            return self
        except BaseException:
            self.listener.close()
            if hasattr(self,'bound_identity'):self.path.unlink(missing_ok=True)
            raise

    def __exit__(self,*_):
        self.stop.set()
        if self.current:
            try:self.current.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        # A bounded in-flight management query is allowed to finish before teardown.
        if self.thread:self.thread.join(15)
        self.listener.close()
        if self.path.exists() and self.path.stat().st_ino==self.bound_identity:self.path.unlink()

    def serve(self):
        while not self.stop.is_set():
            try:connection,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:return
            with connection:
                self.current=connection
                connection.settimeout(15)
                try:
                    peer=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    request=_receive(connection)
                    if (peer[1]!=self.job['_environment']['uid'] or request.get('job_id')!=self.job['id']
                        or request.get('attempt_id')!=self.job['attempt_id']
                        or not isinstance(request.get('token'),str)
                        or not secrets.compare_digest(request['token'],self.job['_token'])
                        or not isinstance(request.get('nonce'),str) or len(request['nonce'])!=32):
                        raise RuntimeError('NPU admission identity mismatch')
                    if self.stop.is_set() or self.used:raise RuntimeError('NPU admission was already requested or closed; execution is not repeated')
                    self.used=True
                    try:devices=selected_reading(self.job['allocation']['npu_ids'],self.probe())
                    except (OSError,ValueError,RuntimeError,TimeoutError,subprocess.TimeoutExpired) as error:
                        raise RuntimeError('Host NPU management check refused launch ('+type(error).__name__+'); no user workload was started') from None
                    reply={'ok':True,'schema':'ascend-lab-npu-admission/v1','job_id':self.job['id'],
                        'attempt_id':self.job['attempt_id'],'node_id':self.job['node_id'],'nonce':request['nonce'],
                        'sampled_at':now(),'issued_monotonic':time.monotonic(),'ttl_seconds':RECEIPT_TTL,
                        'query_uid':os.getuid(),'task_uid':peer[1],'devices':devices,
                        'scope':'cooperative host management check; no hardware isolation guarantee'}
                    connection.sendall(json.dumps(reply).encode()+b'\n')
                except (OSError,ValueError,RuntimeError,TimeoutError):
                    try:connection.sendall(b'{"ok":false,"error":"Host NPU management admission refused; no user workload was started"}\n')
                    except OSError:pass
                finally:self.current=None


def request_receipt(directory,job):
    nonce=secrets.token_hex(16)
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(15);connection.connect(str(Path(directory)/SOCKET_NAME))
            peer=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if peer[1]!=os.getuid():raise RuntimeError('Unexpected NPU management endpoint identity')
            connection.sendall(json.dumps({'job_id':job['id'],'attempt_id':job['attempt_id'],
                'token':job['_token'],'nonce':nonce}).encode()+b'\n')
            receipt=_receive(connection)
        validate_receipt(receipt,job,nonce)
        return {key:value for key,value in receipt.items() if key not in {'nonce'}}
    except (OSError,ValueError,RuntimeError,TimeoutError):
        raise RuntimeError('宿主 NPU 启动前复查未通过或回执不可用；未启动用户命令') from None


def validate_receipt(receipt,job,nonce=None):
    if (receipt.get('ok') is not True or receipt.get('schema')!='ascend-lab-npu-admission/v1'
        or receipt.get('job_id')!=job['id'] or receipt.get('attempt_id')!=job['attempt_id']
        or receipt.get('node_id')!=job['node_id'] or nonce is not None and receipt.get('nonce')!=nonce
        or type(receipt.get('issued_monotonic')) not in {int,float} or receipt.get('ttl_seconds')!=RECEIPT_TTL
        or not 0<=time.monotonic()-receipt['issued_monotonic']<=RECEIPT_TTL
        or not isinstance(receipt.get('devices'),list) or any(not isinstance(item,dict) for item in receipt['devices'])
        or [item.get('id') for item in receipt['devices']]!=job['allocation']['npu_ids']):
        raise RuntimeError('NPU admission receipt does not match this execution or has expired')
    selected_reading(job['allocation']['npu_ids'],(receipt['devices'],[]))

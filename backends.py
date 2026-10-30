"""Execution backend boundary. Current implementation is explicitly one local node."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import time
import uuid
import hashlib

from jobs import ROOT, atomic_json, read_json


def physical_identity():
    return hashlib.sha256(Path('/etc/machine-id').read_bytes().strip()).hexdigest()


class LocalExecutionBackend:
    name = 'local-process'

    def __init__(self, runtime):
        self.runtime=Path(runtime)
        path=self.runtime/'node.json'
        node=read_json(path)
        if not node:
            node={'id':'node-'+uuid.uuid4().hex[:12], 'backend':self.name}
            atomic_json(path,node)
        self.node_id=node['id']
        self.physical_id=physical_identity()

    def launch(self, directory, job):
        if job['node_id']!=self.node_id or not job.get('attempt_id'):
            raise ValueError('Execution attempt is not assigned to this node')
        with (directory/'launcher.log').open('ab') as log:
            return subprocess.Popen(['/usr/bin/python3',str(ROOT/'executor.py'),str(directory)],
                                    stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                    cwd=ROOT,start_new_session=True,close_fds=True)

    def inventory(self, snapshot, policy):
        if not isinstance(snapshot,dict):snapshot={}
        sampled=(snapshot or {}).get('sampled_at')
        try:fresh=0<=time.time()-dt.datetime.fromisoformat(sampled).timestamp()<45
        except (ValueError,TypeError):fresh=False
        return {'id':self.node_id,'backend':self.name,'status':'online' if fresh else 'unknown',
                'physical_id':self.physical_id,'name':'当前宿主机',
                'sampled_at':sampled,'execution_uid':os.getuid(),
                'enforcement':'cooperative-reservations','transport':'local-process',
                'capacity':{k:policy[k] for k in ['cpu_ids','memory_mib','shm_mib','max_running']},
                'observed':{'memory_available_bytes':snapshot.get('host',{}).get('memory_available_bytes'),
                            'disk_free_bytes':snapshot.get('host',{}).get('workspace_disk',{}).get('free_bytes'),
                            'host_cpu_utilization_pct':snapshot.get('host',{}).get('cpu_utilization_pct')},
                'device_status':snapshot.get('npu',{}).get('status','unknown'),
                'device_sampled_at':snapshot.get('npu',{}).get('sampled_at'),
                'devices':[{k:d.get(k) for k in ['id','name','health','utilization_pct','external_process_count','scoped_process_count']}
                           for d in (snapshot or {}).get('npu',{}).get('devices',[])]}

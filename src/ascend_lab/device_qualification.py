"""Protected, per-identity single-device access evidence. Never certifies ACL/numerics."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
import time

CAPABILITY='npu-access-qualification-v1'
PROBE='npu_access_probe'
VALID_SECONDS=1800
_FILE_CACHE={}


def invalidate_restored(path):
    """An old backup must not revive permission revoked after the backup was taken."""
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_qualifications'").fetchone():return
        for node,owner,device,raw in db.execute('SELECT node_id,owner,device_id,record FROM device_qualifications').fetchall():
            record=json.loads(raw)
            if record['status']=='verified':
                record.update(expires_at=0,invalidated_by_restore=True)
                db.execute('UPDATE device_qualifications SET record=? WHERE node_id=? AND owner=? AND device_id=?',
                           (json.dumps(record),node,owner,device))


def parameters(value):
    if not isinstance(value,dict) or set(value)-{'node_id','device_id','memory_mib'}:
        raise ValueError('Device probe accepts only node_id, device_id and optional memory_mib')
    if not re.fullmatch('worker-[0-9a-f]{12}',str(value.get('node_id',''))):raise ValueError('Choose one enrolled node')
    if type(value.get('device_id')) is not int or not 0<=value['device_id']<256:raise ValueError('Choose one physical device ID')
    memory=value.get('memory_mib',256)
    if type(memory) is not int or not 256<=memory<=65536:raise ValueError('Device probe memory must be 256..65536 MiB')
    return {'node_id':value['node_id'],'device_id':value['device_id'],'memory_mib':memory}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def host_fingerprint(binding):
    """Detect host boot, Docker binding and selected protected driver-file changes."""
    boot=Path('/proc/sys/kernel/random/boot_id').read_bytes()
    if not 1<=len(boot)<=128:raise ValueError('Unrecognized boot identity')
    files={}
    for name in ['/usr/local/Ascend/driver/version.info','/usr/local/Ascend/driver/lib64/driver/libascend_hal.so']:
        path=Path(name)
        if not path.exists():files[name]=None;continue
        resolved=path.resolve(strict=True)
        if not resolved.is_relative_to('/usr/local/Ascend/driver'):raise ValueError('Driver identity resolves outside approved driver tree')
        parent=resolved
        while parent!=Path('/usr/local/Ascend/driver').parent:
            info=parent.lstat()
            if info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Driver identity path is not protected')
            parent=parent.parent
        descriptor=os.open(resolved,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0<=info.st_size<=64*1024**2:raise ValueError('Driver identity file is not bounded regular data')
            key=(str(resolved),info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            if key not in _FILE_CACHE:
                sha=hashlib.sha256();remaining=info.st_size
                while remaining:
                    block=stream.read(min(remaining,1024**2))
                    if not block:raise ValueError('Driver identity changed while reading')
                    sha.update(block);remaining-=len(block)
                after=os.fstat(stream.fileno())
                if (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('Driver identity changed while reading')
                if len(_FILE_CACHE)>=16:_FILE_CACHE.clear()
                _FILE_CACHE[key]=sha.hexdigest()
            files[name]=_FILE_CACHE[key]
    return digest({'boot_sha256':hashlib.sha256(boot).hexdigest(),'docker':binding,'driver_files':files})


def context(profile,node_id,owner,device,host_sha256,runtime_files):
    from environment_contract import fingerprint as environment_sha
    from npu_device import fingerprint as npu_sha
    if not re.fullmatch('[0-9a-f]{64}',str(host_sha256)):raise ValueError('Missing current host/driver identity')
    return digest({'node_id':node_id,'physical_id':profile['physical_id'],'owner':owner,
        'identity':profile['users'][owner],'device_id':device,'npu_policy':npu_sha(profile['npu']),
        'image':profile['image'],'python':profile.get('environment_manifest',{}).get('observed',{}).get('python_executable',profile.get('python','/usr/bin/python3')),
        'setup':profile.get('environment_setup'),'revision':profile.get('policy_revision',1),
        'environment':environment_sha(profile['environment_manifest']) if profile.get('environment_manifest') else None,
        'host_sha256':host_sha256,'runtime_files':runtime_files})


def decision(node,owner,device,runtime_files=None):
    from workflow_spec import runtime_manifest
    runtime_files=runtime_manifest() if runtime_files is None else runtime_files
    report=node['report'];profile=node['profile']
    if CAPABILITY not in report.get('capabilities',[]) or report.get('access_runtime_files')!=runtime_files:
        return {'status':'needs_upgrade','reason':'节点需升级受保护设备访问资格协议','context_sha256':None}
    try:current=context(profile,node['id'],owner,device,report.get('access_host_sha256'),runtime_files)
    except (KeyError,TypeError,ValueError):return {'status':'unavailable','reason':'节点主机/驱动身份不可核对，暂停设备工作流','context_sha256':None}
    record=next((r for r in node.get('device_qualifications',[]) if r['owner']==owner and r['device_id']==device),None)
    state='unverified' if record is None else 'stale' if record['context_sha256']!=current else record['status']
    if state=='verified' and time.time()>=record['expires_at']:state='expired'
    reasons={'unverified':'等待本用户在此卡上的固定设备访问探针验收','stale':'节点环境或执行身份已变更，需重新验证设备访问',
             'blocked':'已确认本用户设备访问失败，等待显式探针重新验证','expired':'设备访问验收已过期，等待显式探针续验','verified':''}
    return {'status':state,'reason':reasons[state],'context_sha256':current}


class Qualifications:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS device_qualifications (node_id TEXT NOT NULL, owner TEXT NOT NULL, device_id INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY(node_id,owner,device_id))')

    def records(self,node_id,db):
        return [json.loads(row[0]) for row in db.execute('SELECT record FROM device_qualifications WHERE node_id=?',(node_id,))]

    def observe(self,node,job,result,db):
        """Called only after authenticated upload SHA checks and confirmed termination."""
        from archive_api import open_artifact
        from environment_probe import access_confirmed
        from workflow_spec import runtime_manifest
        if (not job['resources']['npu'] or not job.get('_npu_access_context')
            or job.get('runtime_files')!=runtime_manifest()):return
        try:
            def load(name):
                with open_artifact(self.jobs.directory(job['id']),name,max_file=256*1024) as source:return json.load(source)
            receipt=load('results/_lab/environment.json');meta=load('environment.json')
            ids=job['allocation']['npu_ids'];mapping=node['profile']['users'][job['owner']]
            if (len(ids)!=1 or receipt.get('schema')!='ascend-lab-environment/v1'
                or meta.get('runtime')!='managed-docker' or meta.get('task_uid')!=mapping['uid'] or meta.get('task_gid')!=mapping['gid']
                or meta.get('privileged') is not False or meta.get('read_only_root') is not True or meta.get('network_mode')!='none'
                or meta.get('image')!=job.get('_npu_access_image') or meta.get('runtime_files')!=job['runtime_files']):return
            passed=(job.get('template')==PROBE and result['status']=='succeeded' and result.get('exit_code')==0
                    and receipt.get('probe_exit_code')==0 and receipt.get('probe_only') is True
                    and receipt.get('locked') is True and receipt.get('workload_started') is False
                    and access_confirmed(receipt,ids))
            failed=(result['status']=='failed' and result.get('exit_code')==78 and receipt.get('probe_exit_code')==78
                    and receipt.get('failure_stage')=='npu_device_access' and receipt.get('locked') is False
                    and receipt.get('workload_started') is False and (receipt.get('device_access') or {}).get('passed') is False)
            if not passed and not failed:return
            record={'owner':job['owner'],'device_id':ids[0],'status':'verified' if passed else 'blocked',
                'context_sha256':job['_npu_access_context'],'recorded_at':time.time(),'expires_at':time.time()+VALID_SECONDS if passed else None,
                'job_id':job['id'],'attempt_id':job['attempt_id'],'receipt_sha256':digest(receipt),
                'scope':'Device-file access only; ACL, runtime mapping, numerics and performance unverified'}
            db.execute('INSERT OR REPLACE INTO device_qualifications VALUES(?,?,?,?)',(node['id'],job['owner'],ids[0],json.dumps(record)))
            db.execute('INSERT INTO worker_node_events(at,node_id,actor,revision,policy) VALUES(?,?,?,?,?)',
                (__import__('jobs').now(),node['id'],'protected-node-receipt',node['profile'].get('policy_revision',1),
                 json.dumps({'kind':'device-access-'+record['status'],'owner':job['owner'],'device_id':ids[0],'source_job':job['id'],'context_sha256':record['context_sha256']})))
            return record
        except (OSError,ValueError,TypeError,KeyError):return

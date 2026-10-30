"""Node spool admission and resumable removal of controller-confirmed copies.

No user paths, shared caches, live work trees or legacy unacknowledged archives
are collected. Metadata/leases and the controller's digest receipt stay private.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from contextlib import ExitStack

import storage_files as fs

CAPABILITY='node-spool-v1'
HEADROOM=512*1024**2
METADATA=16*1024**2


def requirement(job):
    from io_policy import for_job,MIB
    from pathlib import PurePosixPath
    from workflow_spec import WORKFLOWS
    bundle=job.get('input') or {}
    directories={str(parent) for item in bundle.get('files',[]) for parent in PurePosixPath(item['name']).parents}
    return {'bytes':for_job(job)['total_mib']*MIB+bundle.get('bytes',0)+METADATA,
            'inodes':(2000*121 if job.get('template') in WORKFLOWS else 4096)+len(bundle.get('files',[]))+len(directories)+128}


def pressure(job,report,held):
    demand=[requirement(item) for item in [*held,job]]
    if report['disk_free_bytes']<HEADROOM+sum(item['bytes'] for item in demand):return '节点输入/归档暂存空间不足'
    if CAPABILITY in report.get('capabilities',[]) and report.get('disk_free_inodes',0)<sum(item['inodes'] for item in demand):return '节点输入/归档暂存 inode 不足'
    if report.get('artifact_gc_ready') is False:return '节点归档副本回收尚未确认，暂停新任务'
    return None


def manifest_sha256(manifest):
    if not isinstance(manifest,list) or len(manifest)>2000:raise ValueError('Invalid confirmed artifact manifest')
    from inputs import safe_name
    names=set()
    for item in manifest:
        if (not isinstance(item,dict) or set(item)!={'name','size','sha256'} or not safe_name(item['name'])
            or item['name'] in names or type(item['size']) is not int or item['size']<=0
            or not re.fullmatch('[0-9a-f]{64}',str(item['sha256']))):raise ValueError('Invalid confirmed artifact entry')
        names.add(item['name'])
    return fs.digest(sorted(manifest,key=lambda item:item['name']))


def receipt(job,manifest):
    return {'job_id':job['id'],'attempt_id':job['attempt_id'],
            'manifest_sha256':manifest_sha256(manifest),'state':job['state']}


def confirmed(grant,manifest,value):
    return (isinstance(value,dict) and value.get('job_id')==grant['job_id']
            and value.get('attempt_id')==grant['attempt_id']
            and value.get('state') in {'succeeded','failed','timed_out','cancelled'}
            and value.get('manifest_sha256')==manifest_sha256(manifest))


def control_path(root,attempt):
    if not isinstance(attempt,str) or not re.fullmatch('attempt-[0-9a-f]{32}',attempt):raise ValueError('Invalid spool attempt identity')
    # Reopen every parent without following links; preserve unknown data on errors.
    path=Path(root)
    for part in ['attempts',attempt,'control']:
        path=path/part
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise ValueError('Unsafe private node spool parent')
    return path


def prune(root,attempt,config=None):
    from jobs import atomic_json,read_json,now
    control=control_path(root,attempt);state_path=control/'state.json'
    with fs.lock(root,'node-spool-'+attempt,exclusive=True),ExitStack() as anchors:
        state=read_json(state_path) or {};grant=read_json(control/'grant.json') or {}
        transfer=read_json(control/'artifact-transfer.json') or {};manifest=transfer.get('manifest')
        if state.get('stage')!='cleaned' or state.get('controller_acknowledged') is not True:
            raise ValueError('Node copies remain until cleanup and controller acknowledgement')
        if grant.get('attempt_id')!=attempt or not confirmed(grant,manifest,state.get('artifact_receipt')):
            raise ValueError('No matching controller checksum receipt; node copies retained')
        gc=state.get('artifact_gc') or {};source=control/'artifacts';retired=control/'artifacts-retired'
        frozen=read_json(control/'execution-policy.json') or {}
        spool=None
        if frozen.get('spool_storage'):
            from spool_storage import Spool
            selected=config if gc.get('state')=='purged' and config else frozen
            if gc.get('state')!='purged' and config and config.get('spool_storage')!=frozen['spool_storage']:
                raise ValueError('Node spool policy changed before cleanup; retained data was not removed')
            spool=Spool(root,{**selected,'node_id':grant['node_id']})
            anchor=anchors.enter_context(spool.anchor())
            parent=anchor/'attempts'/attempt;source=parent/'artifacts';retired=parent/'artifacts-retired'
        if gc.get('state')=='purged':
            if fs.exists(source) or fs.exists(retired):raise ValueError('New spool data appeared after confirmed cleanup')
            if spool:spool.remove_attempt(grant)
            return True
        if not gc:
            snapshot=fs.scan(source)
            actual={item['name']:item for item in snapshot['entries'] if not stat.S_ISDIR(item['mode']) and item['size']}
            if set(actual)!={item['name'] for item in manifest}:raise ValueError('Unconfirmed node archive files remain')
            # An altered local copy is evidence; never silently remove unuploaded bytes.
            for item in manifest:
                path=source/item['name']
                if not stat.S_ISREG(actual[item['name']]['mode']) or actual[item['name']]['size']!=item['size']:
                    raise ValueError('Node archive differs from confirmed manifest')
                digest=hashlib.sha256()
                from archive_api import open_artifact
                with open_artifact(source,item['name'],item['size']) as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
                if digest.hexdigest()!=item['sha256']:raise ValueError('Node archive checksum changed after upload')
            if fs.digest(fs.scan(source))!=fs.digest(snapshot):raise ValueError('Node archive changed during checksum validation')
            gc={'state':'pending','inventory':snapshot,'fingerprint':fs.digest(snapshot),'payload_bytes':snapshot['bytes'],
                'manifest_sha256':manifest_sha256(manifest),'started_at':now()}
            state['artifact_gc']=gc;atomic_json(state_path,state)
        if gc.get('state')!='pending' or gc.get('manifest_sha256')!=manifest_sha256(manifest):raise ValueError('Node spool journal changed')
        if fs.exists(source):
            if fs.exists(retired) or fs.digest(fs.scan(source))!=gc['fingerprint']:raise ValueError('Node spool locations or identity changed')
            fs.move(source,retired)
        # Missing retired root is permitted after a crash following its final unlink.
        complete,_=fs.purge(retired,gc['inventory'])
        if complete:
            if spool:spool.remove_attempt(grant)
            state['artifact_gc']={key:value for key,value in gc.items() if key not in {'inventory','fingerprint'}}
            state['artifact_gc'].update(state='purged',finished_at=now())
            state['artifact_gc_requested']=False;atomic_json(state_path,state)
        return complete


def pending(root):
    from jobs import read_json
    return [path.parent.parent.name for path in sorted((Path(root)/'attempts').glob('*/control/state.json'))
            if (read_json(path) or {}).get('artifact_gc_requested') is True]

"""Root-only, offline CAS resize of existing central owner storage quotas.

The private durable journal blocks normal start/backup/activation until the same
plan finishes. No user/group creation, mount, format, deletion or quota disable.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

import group_quota as quota
from deployment import DeployError,regular_json,require_root,verify
from lab_client import now
from payload_storage import Payload,MARKER,binding,root_fd,validate
from state_lock import state_lock

JOURNAL='payload-quota-change.json'
HISTORY='payload-quota-history'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def sync(directory):
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def write(path,value,uid=0,gid=0,mode=0o600):
    path=Path(path);temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    if path.is_symlink():raise DeployError('Quota change refuses symbolic links')
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as output:
            json.dump(value,output,ensure_ascii=False,indent=2);output.write('\n')
            output.flush();os.fchmod(output.fileno(),mode);os.fchown(output.fileno(),uid,gid);os.fsync(output.fileno())
        os.replace(temporary,path);sync(path.parent)
    finally:temporary.unlink(missing_ok=True)


def _checkpoint(stage):
    """Named boundaries for process-failure acceptance; no environment switches."""


def _base_config(config):
    value=copy.deepcopy(config);value.get('scheduler',{}).pop('payload_storage',None);return fingerprint(value)


def _base_meta(meta):
    return fingerprint({k:v for k,v in meta.items() if k not in {'payload_storage','payload_quota_revision'}})


def _pending(state,owner):
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return 0
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_allocations'").fetchone():return 0
        return db.execute('SELECT COALESCE(SUM(bytes),0) FROM storage_allocations WHERE owner=? AND settled=0',(owner,)).fetchone()[0]


def _limits(item):return {k:item[k] for k in ['bytes','inodes']}


def _capacity(current,pending,target):
    if (target['bytes']<current['bytes_used']+pending+65536
        or target['inodes']<current['inodes_used']+16):
        raise DeployError('New owner quota lacks space for retained data, pending uploads and metadata headroom')


def _context(prefix,recovery=False):
    require_root();prefix,meta,release=verify(prefix,quota_recovery=recovery)
    if meta['role']!='controller' or not meta.get('payload_storage',{}).get('owner_groups'):
        raise DeployError('Quota resize requires an installed controller with explicit owner group quotas')
    state=Path(meta['state_dir']);config=regular_json(state/'config.json',private=True)
    return prefix,meta,state,config


def plan(prefix,owner,byte_hard,inode_hard):
    prefix,meta,state,config=_context(prefix)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,'controller');Payload(state,config,uid=meta['uid'])
        before=meta['payload_storage'];after=copy.deepcopy(before)
        if owner not in before['owner_groups']:raise DeployError('Resize selects an existing owner; provision new owners separately')
        after['owner_groups'][owner].update(bytes=byte_hard,inodes=inode_hard)
        validate(after)
        if after==before:raise DeployError('Owner quota already has these limits')
        pending=_pending(state,owner)
        with root_fd(before,meta['uid']) as fd:
            item=before['owner_groups'][owner];current=quota.require(fd,item['gid'],item['bytes'],item['inodes'])
        _capacity(current,pending,after['owner_groups'][owner])
        value={'format':'ascend-lab-payload-quota-plan/v1','prefix':str(prefix),'state_dir':str(state),
            'release_id':meta['release_id'],'owner':owner,'before':before,'after':after,
            'revision':meta.get('payload_quota_revision',0),'config_base_sha256':_base_config(config),
            'installation_base_sha256':_base_meta(meta),'usage':{k:current[k] for k in ['bytes_used','inodes_used']},
            'pending_bytes':pending}
        value['id']=fingerprint(value)
        return {'read_only':True,'plan':value,'service_restart_required':True}


def _validate_plan(value):
    keys={'format','prefix','state_dir','release_id','owner','before','after','revision','config_base_sha256',
          'installation_base_sha256','usage','pending_bytes','id'}
    if (not isinstance(value,dict) or set(value)!=keys or value['format']!='ascend-lab-payload-quota-plan/v1'
        or value['id']!=fingerprint({k:v for k,v in value.items() if k!='id'})
        or type(value['revision']) is not int or value['revision']<0
        or type(value['pending_bytes']) is not int or value['pending_bytes']<0):
        raise DeployError('Invalid quota change plan')
    before=validate(value['before']);after=validate(value['after']);owner=value['owner']
    if not isinstance(owner,str) or owner not in before.get('owner_groups',{}):raise DeployError('Unknown planned owner')
    expected=copy.deepcopy(before);expected['owner_groups'][owner].update(_limits(after['owner_groups'][owner]))
    if expected!=after or before==after:raise DeployError('A quota plan may change only one existing owner limits')
    if (not isinstance(value['usage'],dict) or set(value['usage'])!={'bytes_used','inodes_used'}
        or any(type(v) is not int or v<0 for v in value['usage'].values())):raise DeployError('Invalid planned quota usage')


def _documents(prefix,meta,state,config,value):
    if (str(prefix)!=value['prefix'] or str(state)!=value['state_dir'] or meta['release_id']!=value['release_id']
        or _base_config(config)!=value['config_base_sha256'] or _base_meta(meta)!=value['installation_base_sha256']):
        raise DeployError('Quota plan belongs to another or changed installation')
    before=value['before'];after=value['after']
    if config.get('scheduler',{}).get('payload_storage') not in [before,after]:raise DeployError('Controller policy changed outside this quota operation')
    if meta.get('payload_storage') not in [before,after]:raise DeployError('Installed payload binding changed outside this quota operation')
    revision=meta.get('payload_quota_revision',0)
    if revision not in {value['revision'],value['revision']+1}:raise DeployError('Quota plan revision is stale')
    if (meta['payload_storage']==after)!=(revision==value['revision']+1):raise DeployError('Installed quota revision and policy differ')
    marker=regular_json(Path(before['path'])/MARKER,private=True)
    if marker not in [binding(before,state),binding(after,state)]:raise DeployError('Payload binding changed outside this operation')
    return marker


def apply(prefix,value):
    require_root();_validate_plan(value)
    prefix,meta,state,config=_context(prefix,recovery=True)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        other=prefix/'payload-owner-change.json'
        if other.exists() or other.is_symlink():raise DeployError('Finish the original pending owner storage append before resizing quotas')
        assert_quiescent(state,'controller')
        journal=prefix/JOURNAL;history=prefix/HISTORY;receipt_path=history/(value['id']+'.json')
        if history.exists():
            item=history.lstat()
            if not stat.S_ISDIR(item.st_mode) or item.st_uid!=0 or item.st_mode&0o077:raise DeployError('Quota audit directory must be root-private')
        marker=_documents(prefix,meta,state,config,value)
        before=value['before'];after=value['after'];owner=value['owner'];item=before['owner_groups'][owner]
        pending=_pending(state,owner)
        if pending!=value['pending_bytes']:raise DeployError('Pending uploads changed; create a fresh quota plan')
        resumed=journal.exists() or journal.is_symlink()
        if resumed:
            info=journal.lstat()
            if info.st_uid!=0 or regular_json(journal,private=True)!=value:raise DeployError('Resume only the original root-owned quota plan')
        elif receipt_path.exists():
            if (meta['payload_storage']!=after or config['scheduler']['payload_storage']!=after
                or marker!=binding(after,state)):raise DeployError('An intervening operation superseded this quota plan')
            with root_fd(after,meta['uid']) as fd:
                quota.require(fd,item['gid'],after['owner_groups'][owner]['bytes'],after['owner_groups'][owner]['inodes'])
            return {'applied':True,'already_applied':True,'receipt':regular_json(receipt_path,private=True)}
        elif (meta['payload_storage']!=before or config['scheduler']['payload_storage']!=before
              or marker!=binding(before,state) or meta.get('payload_quota_revision',0)!=value['revision']):
            raise DeployError('Quota plan is stale; no kernel limits were changed')
        with root_fd(before,meta['uid']) as fd:
            for name,assigned in before['owner_groups'].items():
                if name!=owner:quota.require(fd,assigned['gid'],assigned['bytes'],assigned['inodes'])
            current=quota.read(fd,item['gid'])
            if {k:current[k] for k in value['usage']}!=value['usage']:
                raise DeployError('Owner usage changed since planning; preserve any pending operation for reconciliation')
            _capacity(current,pending,after['owner_groups'][owner])
            if not resumed:
                quota.require(fd,item['gid'],item['bytes'],item['inodes'])
                if history.exists() and len(os.listdir(history))>=1024:raise DeployError('Archive quota audit receipts before adding further changes')
                if not history.exists():history.mkdir(mode=0o700);sync(prefix)
                write(journal,value);_checkpoint('journal')
            quota.resize(fd,item['gid'],_limits(item),_limits(after['owner_groups'][owner]));_checkpoint('quota')
            updated_config=copy.deepcopy(config);updated_config['scheduler']['payload_storage']=after
            write(state/'config.json',updated_config,meta['uid'],meta['gid']);_checkpoint('config')
            updated_meta={**meta,'payload_storage':after,'payload_quota_revision':value['revision']+1}
            write(prefix/'installation.json',updated_meta,mode=0o444);_checkpoint('metadata')
            write(Path(before['path'])/MARKER,binding(after,state),meta['uid'],meta['gid']);_checkpoint('binding')
            for assigned in after['owner_groups'].values():quota.require(fd,assigned['gid'],assigned['bytes'],assigned['inodes'])
        receipt={'format':'ascend-lab-payload-quota-receipt/v1','plan_id':value['id'],'owner':owner,
            'revision':value['revision']+1,'before':_limits(item),'after':_limits(after['owner_groups'][owner]),
            'preserved_usage':value['usage'],'pending_upload_bytes_preserved':pending,'completed_at':now()}
        if receipt_path.exists():
            receipt=regular_json(receipt_path,private=True)
            if receipt['plan_id']!=value['id']:raise DeployError('Quota audit receipt differs; preserve the journal')
        else:write(receipt_path,receipt)
        _checkpoint('receipt');journal.unlink();sync(prefix)
        return {'applied':True,'already_applied':False,'resumed':resumed,'service_restart_required':True,'receipt':receipt}

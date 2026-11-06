"""Offline, Root-only append of one staged owner's central storage group.

This is a provisioning primitive, not activation. Never creates OS groups,
changes old owner limits, adopts an occupied GID or issues a user credential.
"""
import copy
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat

from deployment import DeployError,regular_json,require_root,verify
from lab_client import now
import group_quota as quota
import owner_identity
import payload_quota_admin as resize
from payload_storage import Payload,MARKER,binding,root_fd,validate
from state_lock import state_lock

JOURNAL='payload-owner-change.json'
HISTORY='payload-owner-history'
FORMAT='ascend-lab-payload-owner-plan/v1'
ZERO=('byte_hard','byte_soft','inode_hard','inode_soft','bytes_used','inodes_used')


def _checkpoint(stage):
    """Named durable boundaries for real process-crash acceptance."""


def _context(prefix,recovery=False):
    require_root();prefix,meta,release=verify(prefix,quota_recovery=recovery)
    helper=release/Path(__file__).name
    if not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():
        raise DeployError('Upgrade this controller to matching owner administration and recovery guards first')
    if meta['role']!='controller' or not meta.get('payload_storage',{}).get('owner_groups'):
        raise DeployError('Owner storage append requires an installed controller with explicit group quotas')
    state=Path(meta['state_dir']);config=regular_json(state/'config.json',private=True)
    return prefix,meta,state,config


def _exclusive_operation(prefix):
    competing=Path(prefix)/resize.JOURNAL
    if competing.exists() or competing.is_symlink():
        raise DeployError('Finish the original existing-owner quota change before owner storage append')


def _stage(state,owner,uid):
    """Read only under the caller's exclusive service-state lock."""
    path=Path(state)/'identities.sqlite3';info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_mode&0o077 or info.st_nlink!=1:
        raise DeployError('Staged identities require the private service-owned database')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT profile,disabled,password_hash FROM users WHERE id=?',(owner,)).fetchone()
        tokens=db.execute('SELECT COUNT(*) FROM tokens WHERE user=?',(owner,)).fetchone()[0]
    if not row:raise DeployError('Create a credential-free staged identity before allocating its storage')
    import json
    profile=json.loads(row[0])
    if row[1]!=1 or row[2]!='!' or tokens or not owner_identity.staged(owner,profile):
        raise DeployError('Owner must remain staged, disabled and credential-free')
    return profile


def _storage_budget(profile,item):
    wanted=sum(profile['quota'][k] for k in ['input_storage_mib','archive_storage_mib'])*1024**2
    if wanted+65536>item['bytes']:
        raise DeployError('Native owner storage must cover staged input/archive budgets and metadata headroom')


def _control(state):
    info=Path(state).lstat()
    if not stat.S_ISDIR(info.st_mode):raise DeployError('Owner append requires a real control directory')
    return {'device':info.st_dev,'inode':info.st_ino}


def _usage(fd,policy):
    return {owner:{key:current[key] for key in ['bytes_used','inodes_used']}
            for owner,item in policy['owner_groups'].items()
            for current in [quota.require(fd,item['gid'],item['bytes'],item['inodes'])]}


def _unassigned(current):
    if any(current[key] for key in ZERO):raise DeployError('New GID already has native quota or data; refusing adoption')


def _capacity(fd,item):
    space=os.fstatvfs(fd)
    if space.f_bavail*space.f_frsize<item['bytes']+32*1024**2 or space.f_favail<item['inodes']+512:
        raise DeployError('Payload filesystem lacks actual free space/inodes plus metadata reserve')


def plan(prefix,owner,gid,byte_hard,inode_hard):
    prefix,meta,state,config=_context(prefix)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        _exclusive_operation(prefix);assert_quiescent(state,'controller');Payload(state,config,uid=meta['uid'])
        profile=_stage(state,owner,meta['uid']);before=meta['payload_storage'];after=copy.deepcopy(before)
        if owner in before['owner_groups']:raise DeployError('Owner already has storage; use existing-owner quota administration')
        if gid==meta['gid']:raise DeployError('Storage GID must differ from the service primary group')
        item={'gid':gid,'bytes':byte_hard,'inodes':inode_hard};after['owner_groups'][owner]=item;validate(after)
        _storage_budget(profile,item)
        with root_fd(before,meta['uid']) as fd:
            _unassigned(quota.read(fd,gid));_capacity(fd,item);usage=_usage(fd,before)
        value={'format':FORMAT,'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],
               'owner':owner,'stage_id':profile['provisioning']['id'],'before':before,'after':after,
               'revision':meta.get('payload_quota_revision',0),'config_base_sha256':resize._base_config(config),
               'installation_base_sha256':resize._base_meta(meta),'control_identity':_control(state),'usage':usage}
        value['id']=resize.fingerprint(value)
        return {'read_only':True,'plan':value,'service_restart_required':True,
                'systemd_unit_group_refresh_required':True,'owner_activation_still_required':True}


def _validate_plan(value):
    keys={'format','prefix','state_dir','release_id','owner','stage_id','before','after','revision',
          'config_base_sha256','installation_base_sha256','control_identity','usage','id'}
    if (not isinstance(value,dict) or set(value)!=keys or value['format']!=FORMAT
        or value['id']!=resize.fingerprint({k:v for k,v in value.items() if k!='id'})
        or type(value['revision']) is not int or value['revision']<0):raise DeployError('Invalid owner append plan')
    for key in ['id','stage_id','release_id','config_base_sha256','installation_base_sha256']:
        if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise DeployError('Invalid owner append identity')
    for key in ['prefix','state_dir']:
        path=value[key]
        if not isinstance(path,str) or not path.startswith('/') or path!=str(Path(path)) or '..' in Path(path).parts:
            raise DeployError('Invalid owner append installation path')
    owner=value['owner'];before=validate(value['before']);after=validate(value['after'])
    if not before.get('owner_groups') or not after.get('owner_groups'):raise DeployError('Explicit existing owner groups are required')
    if not isinstance(owner,str) or owner in before['owner_groups'] or owner not in after['owner_groups']:
        raise DeployError('Owner append must select exactly one new owner')
    expected=copy.deepcopy(before);expected['owner_groups'][owner]=after['owner_groups'][owner]
    if expected!=after:raise DeployError('Owner append may only add one group without changing existing owners')
    control=value['control_identity']
    if not isinstance(control,dict) or set(control)!={'device','inode'} or any(type(v) is not int or v<=0 for v in control.values()):
        raise DeployError('Invalid owner append control identity')
    usage=value['usage']
    if (not isinstance(usage,dict) or set(usage)!=set(before['owner_groups']) or any(
        not isinstance(item,dict) or set(item)!={'bytes_used','inodes_used'} or
        any(type(v) is not int or v<0 for v in item.values()) for item in usage.values())):
        raise DeployError('Invalid existing owner usage snapshot')


def _documents(prefix,meta,state,config,value):
    if (str(prefix)!=value['prefix'] or str(state)!=value['state_dir'] or meta['release_id']!=value['release_id']
        or resize._base_config(config)!=value['config_base_sha256'] or resize._base_meta(meta)!=value['installation_base_sha256']
        or _control(state)!=value['control_identity']):raise DeployError('Owner plan belongs to another or changed installation')
    before=value['before'];after=value['after'];revision=meta.get('payload_quota_revision',0)
    if config.get('scheduler',{}).get('payload_storage') not in [before,after] or meta.get('payload_storage') not in [before,after]:
        raise DeployError('Payload policy changed outside this owner append')
    if revision not in {value['revision'],value['revision']+1} or (meta['payload_storage']==after)!=(revision==value['revision']+1):
        raise DeployError('Owner append revision is stale or inconsistent')
    marker=regular_json(Path(before['path'])/MARKER,private=True)
    if marker not in [binding(before,state),binding(after,state)]:raise DeployError('Payload marker changed outside this owner append')
    profile=_stage(state,value['owner'],meta['uid'])
    if profile['provisioning']['id']!=value['stage_id']:raise DeployError('Staged owner definition changed; preserve pending journal for reconciliation')
    item=after['owner_groups'][value['owner']]
    if item['gid']==meta['gid']:raise DeployError('Storage group aliases the service primary group')
    _storage_budget(profile,item)
    return marker


def apply(prefix,value):
    require_root();_validate_plan(value);prefix,meta,state,config=_context(prefix,recovery=True)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        _exclusive_operation(prefix);assert_quiescent(state,'controller')
        marker=_documents(prefix,meta,state,config,value)
        before=value['before'];after=value['after'];owner=value['owner'];item=after['owner_groups'][owner]
        journal=prefix/JOURNAL;history=prefix/HISTORY;receipt_path=history/(value['id']+'.json')
        if history.exists():
            info=history.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise DeployError('Owner receipt directory must be root-private')
        resumed=journal.exists() or journal.is_symlink()
        if resumed:
            if journal.lstat().st_uid!=0 or regular_json(journal,private=True)!=value:raise DeployError('Resume only the original root-owned owner append plan')
        elif receipt_path.exists():
            if meta['payload_storage']!=after or config['scheduler']['payload_storage']!=after or marker!=binding(after,state):
                raise DeployError('An intervening operation superseded this owner append')
            receipt=regular_json(receipt_path,private=True)
            if receipt.get('plan_id')!=value['id']:raise DeployError('Owner receipt identity differs')
            with root_fd(after,meta['uid']) as fd:_usage(fd,after)
            return {'applied':True,'already_applied':True,'receipt':receipt,'service_restart_required':True,
                    'systemd_unit_group_refresh_required':True,'owner_activation_still_required':True}
        elif (meta['payload_storage']!=before or config['scheduler']['payload_storage']!=before
              or marker!=binding(before,state) or meta.get('payload_quota_revision',0)!=value['revision']):
            raise DeployError('Owner append plan is stale; no kernel limits were changed')
        with root_fd(before,meta['uid']) as fd:
            if _usage(fd,before)!=value['usage']:raise DeployError('Existing owner usage changed; preserve pending operation for reconciliation')
            current=quota.read(fd,item['gid'])
            if not resumed:_unassigned(current)
            elif any(current[k] for k in ZERO):
                current=quota.require(fd,item['gid'],item['bytes'],item['inodes'])
                if current['bytes_used'] or current['inodes_used']:raise DeployError('Unexpected data appeared in a pending new storage group')
            _capacity(fd,item)
            if not resumed:
                if history.exists() and len(os.listdir(history))>=1024:raise DeployError('Archive owner audit receipts before further append operations')
                if not history.exists():history.mkdir(mode=0o700);resize.sync(prefix)
                resize.write(journal,value);_checkpoint('journal')
            if not any(current[k] for k in ZERO):quota.provision(fd,item['gid'],item['bytes'],item['inodes'])
            quota.quota_call(fd,quota.Q_SYNC,0,None);_checkpoint('quota')
            updated=copy.deepcopy(config);updated['scheduler']['payload_storage']=after
            resize.write(state/'config.json',updated,meta['uid'],meta['gid']);_checkpoint('config')
            resize.write(prefix/'installation.json',{**meta,'payload_storage':after,'payload_quota_revision':value['revision']+1},mode=0o444)
            _checkpoint('metadata')
            resize.write(Path(before['path'])/MARKER,binding(after,state),meta['uid'],meta['gid']);_checkpoint('binding')
            _usage(fd,after)
        receipt={'format':'ascend-lab-payload-owner-receipt/v1','plan_id':value['id'],'owner':owner,
                 'stage_id':value['stage_id'],'revision':value['revision']+1,'storage':item,'preserved_usage':value['usage'],
                 'completed_at':now(),'owner_activation_still_required':True}
        if receipt_path.exists():
            receipt=regular_json(receipt_path,private=True)
            if receipt.get('plan_id')!=value['id']:raise DeployError('Owner audit receipt differs; preserve journal')
        else:resize.write(receipt_path,receipt)
        _checkpoint('receipt');journal.unlink();resize.sync(prefix);_checkpoint('published')
        return {'applied':True,'already_applied':False,'resumed':resumed,'receipt':receipt,
                'service_restart_required':True,'systemd_unit_group_refresh_required':True,'owner_activation_still_required':True}

"""Per-owner central payload groups; trusted service owns all files.

Clients cannot choose GIDs and must never execute as the central service UID.
No host group creation, mount, formatting or automatic quota activation.
"""
import grp
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat

import group_quota as quota

KEY='owner_groups'
INTENT='.ascend-lab-group-preparing.json'


def validate(policy):
    if KEY not in policy:return {}
    groups=policy[KEY]
    if not isinstance(groups,dict) or not 1<=len(groups)<=256:raise ValueError('Configure 1..256 explicit owner storage groups')
    gids=set()
    try:docker_gid=grp.getgrnam('docker').gr_gid
    except KeyError:docker_gid=None
    for owner,item in groups.items():
        if (not isinstance(owner,str) or not re.fullmatch('[a-zA-Z][a-zA-Z0-9_-]{0,47}',owner)
            or not isinstance(item,dict) or set(item)!={'gid','bytes','inodes'}):raise ValueError('Invalid owner storage quota contract')
        if (type(item['gid']) is not int or not 1000<=item['gid']<2**31 or item['gid'] in gids or item['gid']==docker_gid
            or type(item['bytes']) is not int or not 16*1024**2<=item['bytes']<=1024**4 or item['bytes']%4096
            or type(item['inodes']) is not int or not 128<=item['inodes']<=10000000):raise ValueError('Invalid or reused owner storage GID/capacity')
        gids.add(item['gid'])
    if (sum(item['bytes'] for item in groups.values())>policy['bytes']-32*1024**2
        or sum(item['inodes'] for item in groups.values())>policy['inodes']-max(512,4*len(groups))):
        raise ValueError('Owner storage quotas exceed backing capacity or metadata reserve')
    return groups


def gids(policy):return sorted(item['gid'] for item in validate(policy or {}).values())


def require_membership(policy,primary=None):
    wanted=set(gids(policy))
    if primary in wanted:raise ValueError('Owner storage groups must differ from the primary service group')
    if os.geteuid()!=0 and not wanted<={os.getegid(),*os.getgroups()}:
        raise ValueError('Central service lacks its explicit supplementary storage groups')


@contextmanager
def preparing_identity(policy):
    """A bounded Root installer may lack FSETID; preserve SGID by membership.

    This changes only the current short-lived administrative process, never
    host account/group databases. Always restore its original group vector.
    """
    wanted=set(gids(policy))
    if not wanted:yield;return
    if os.geteuid()!=0:raise ValueError('Payload ownership preparation requires the trusted administrator')
    previous=os.getgroups();changed=not wanted<={os.getegid(),*previous}
    if changed:os.setgroups(sorted(set(previous)|wanted))
    try:yield
    finally:
        if changed:os.setgroups(previous)


def check_all(fd,policy,empty=False):
    result={}
    for owner,item in validate(policy).items():
        current=quota.require(fd,item['gid'],item['bytes'],item['inodes'])
        if empty and (current['bytes_used'] or current['inodes_used']):raise ValueError('Owner storage group already has data; refusing initial adoption')
        if current['bytes_used']>current['byte_hard'] or current['inodes_used']>current['inode_hard']:
            raise ValueError('Owner storage quota usage exceeds its hard limit')
        result[owner]=current
    return result


def prepare(policy,plan=False):
    """Administrator preprovisions only an explicitly bound, empty dedicated FS."""
    if os.geteuid()!=0:raise ValueError('Storage group preparation requires root')
    from payload_storage import root_fd
    from lab_client import atomic_json
    groups=validate(policy)
    if not groups:raise ValueError('Provide explicit owner_groups in the payload contract')
    with root_fd(policy,0) as fd:
        if os.listdir(fd):raise ValueError('Group quota preparation requires an empty filesystem; reconcile interrupted preparation first')
        previous={}
        for owner,item in groups.items():
            current=quota.read(fd,item['gid']);previous[owner]=current
            if current['bytes_used'] or current['inodes_used']:raise ValueError('Storage group already owns filesystem data')
            if any(current[key] for key in ['byte_hard','byte_soft','inode_hard','inode_soft']):
                quota.require(fd,item['gid'],item['bytes'],item['inodes'])
        if plan:return {'read_only':True,'owners':len(groups),'groups':gids(policy),'existing_limits_compatible':True}
        marker=Path(policy['path'])/INTENT
        atomic_json(marker,{'format':'central-group-prepare/v1','policy':policy});os.fsync(fd)
        for owner,item in groups.items():
            current=previous[owner]
            if not any(current[key] for key in ['byte_hard','byte_soft','inode_hard','inode_soft']):
                quota.provision(fd,item['gid'],item['bytes'],item['inodes'])
        check_all(fd,policy,empty=True);marker.unlink();os.fsync(fd)
        return {'prepared':True,'owners':len(groups),'groups':gids(policy),'filesystem_formatted_or_enabled':False}


def owner(policy,identifier):
    item=validate(policy).get(identifier)
    if item is None:
        from jobs import JobError
        raise JobError('No dedicated central storage quota is provisioned for this owner',409)
    return item


def check_owner(fd,policy,identifier):
    item=owner(policy,identifier)
    return quota.require(fd,item['gid'],item['bytes'],item['inodes'])


def initialize_object(path,policy,identifier):
    item=owner(policy,identifier);fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:quota.assign_empty(fd,item['gid'])
    finally:os.close(fd)


def verify_object(path,policy,identifier):
    item=owner(policy,identifier)
    try:fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:return
    try:quota.verify_directory(fd,item['gid'])
    finally:os.close(fd)


def restored_owners(control):
    with sqlite3.connect('file:'+str(Path(control)/'jobs.sqlite3')+'?mode=ro',uri=True) as db:
        return {'jobs':dict(db.execute('SELECT id,owner FROM jobs')),
                'inputs':dict(db.execute('SELECT id,owner FROM input_bundles'))}


def restored_gid(relative,owners,policy,default):
    parts=Path(relative).parts
    if len(parts)<2:return default
    if parts[0] in {'jobs','inputs'}:
        kind=parts[0];identifier=parts[1]
    elif parts[0]=='storage-trash' and len(parts)>=3:
        kind={'archive':'jobs','input':'inputs'}.get(parts[1]);identifier=parts[2]
    else:return default
    if kind not in owners or identifier not in owners[kind]:raise ValueError('Restored payload object has no authoritative owner')
    return owner(policy,owners[kind][identifier])['gid']

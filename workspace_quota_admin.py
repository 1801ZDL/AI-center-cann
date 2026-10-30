"""Root-only native UID-slot append, one stage of owner provisioning.

Requires a stopped, quiescent installed Node and an already registered quota
filesystem. It preserves existing slots/data/limits; it does not enable users or
change the Node enrollment/configuration. A durable intent on both filesystems
allows resuming only the original plan after interruption.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from deployment import DeployError,require_root,verify
from lab_client import now
from state_lock import state_lock
import filesystem_quota as quota
from quota_workspace import directory,read_marker,requested_limits,capacity,MARKER,EXTEND_INTENT,FORMAT,is_quota
from workspace_slots import validate

JOURNAL='workspace-owner-change.json'
HISTORY='workspace-owner-history'
PLAN='ascend-lab-uid-slot-add-plan/v1'
MAX_RECORD=1024**2


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def _checkpoint(stage):
    """Process-failure acceptance hook. No runtime fault-injection switches."""


def _read(fd,name,optional=False):
    try:source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    except FileNotFoundError:
        if optional:return None
        raise
    with os.fdopen(source,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or info.st_mode&0o077:
            raise DeployError('UID slot administration metadata must be Root-private regular files')
        raw=stream.read(MAX_RECORD+1)
    if len(raw)>MAX_RECORD:raise DeployError('UID slot administration record exceeds its bound')
    value=json.loads(raw)
    if not isinstance(value,dict):raise DeployError('Invalid UID slot administration record')
    return value


def _write(fd,name,value):
    raw=json.dumps(value,sort_keys=True,ensure_ascii=False).encode()
    if len(raw)>MAX_RECORD:raise DeployError('UID slot administration record exceeds its bound')
    temporary='.'+name+'.'+uuid.uuid4().hex
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(raw);stream.flush();os.fchmod(stream.fileno(),0o600);os.fchown(stream.fileno(),0,0);os.fsync(stream.fileno())
        os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd);os.fsync(fd)
    finally:
        try:os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError:pass


def _marker(value,root):
    fields={'format','filesystem_id','device','root_inode','bytes','inodes','slots'}
    if not isinstance(value,dict) or set(value)!=fields or value['format']!=FORMAT:raise DeployError('Invalid native quota source binding')
    if any(type(value[k]) is not int or value[k]<=0 for k in ['device','root_inode','bytes','inodes']):
        raise DeployError('Invalid native quota geometry')
    validate({'slots':value['slots']})
    if any(not is_quota(s) or str(Path(s['path']).parent)!=root or
           (s['filesystem_id'],s['device'],s['filesystem_bytes'],s['filesystem_inodes'])!=
           (value['filesystem_id'],value['device'],value['bytes'],value['inodes']) for s in value['slots']):
        raise DeployError('Quota slots do not belong to the selected native filesystem')
    return value


def _candidate(value,item,inode=1):
    before=value['before']
    return {'id':item['id'],'storage_id':item['storage_id'],'path':str(Path(value['root'])/item['id']),
        'quota_uid':item['uid'],'bytes':item['bytes'],'inodes':item['inodes'],'root_inode':inode,
        'filesystem':'ext4-userquota','filesystem_id':before['filesystem_id'],'device':before['device'],
        'filesystem_bytes':before['bytes'],'filesystem_inodes':before['inodes']}


def _validate_plan(value):
    fields={'format','prefix','release_id','node_id','control','control_identity','configuration_sha256','root','before','additions','id'}
    if (not isinstance(value,dict) or set(value)!=fields or value['format']!=PLAN
        or value['id']!=fingerprint({k:v for k,v in value.items() if k!='id'})):
        raise DeployError('Invalid UID slot append plan')
    from quota_workspace import canonical
    if any(not canonical(value[k]) for k in ['prefix','control','root']):raise DeployError('UID slot plans require canonical absolute paths')
    if (not re.fullmatch('[0-9a-f]{64}',str(value['release_id'])) or not re.fullmatch('worker-[0-9a-f]{12}',str(value['node_id']))
        or not re.fullmatch('[0-9a-f]{64}',str(value['configuration_sha256']))
        or not isinstance(value['control_identity'],list) or len(value['control_identity'])!=2
        or any(type(n) is not int or n<=0 for n in value['control_identity'])):raise DeployError('Invalid installed Node identity in plan')
    before=_marker(value['before'],value['root'])
    additions=value['additions']
    if not isinstance(additions,list) or not additions:raise DeployError('Choose explicit new UID allocations')
    if any(not isinstance(item,dict) or set(item)!={'uid','bytes','inodes','id','storage_id'} for item in additions):
        raise DeployError('Unknown UID allocation fields')
    requested_limits([{k:item[k] for k in ['uid','bytes','inodes']} for item in additions])
    if any(not re.fullmatch('slot-[0-9a-f]{12}',str(item['id'])) or not re.fullmatch('[0-9a-f]{32}',str(item['storage_id'])) for item in additions):
        raise DeployError('Invalid planned slot identity')
    if {s['quota_uid'] for s in before['slots']}&{item['uid'] for item in additions}:
        raise DeployError('Append cannot replace or resize an existing UID')
    validate({'slots':before['slots']+[_candidate(value,item) for item in additions]})


@contextmanager
def _context(prefix,root,recovery=False):
    require_root();prefix,meta,release=verify(prefix,quota_recovery=recovery)
    if meta['role']!='node':raise DeployError('Native UID slot administration requires an installed Node')
    from deployment import regular_json
    addon=regular_json(release/'release.json')['files'].get('workspace_quota_admin.py')
    if not addon or addon['sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise DeployError('Activate matching native UID administration code before editing this Node')
    state=Path(meta['state_dir'])
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,'node')
        control=os.open(state,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            info=os.fstat(control)
            if info.st_uid!=0 or info.st_mode&0o777!=0o700:raise DeployError('Node control must be Root-private')
            config=_read(control,'config.json')
            slots=[s for key in ['workspace','persistent_cache'] for s in config.get(key,{}).get('slots',[])]
            if not any(is_quota(s) and str(Path(s['path']).parent)==root for s in slots):
                raise DeployError('Select a quota filesystem already registered in this Node')
            with directory(root) as native:
                if os.fstat(native).st_dev==info.st_dev:raise DeployError('UID slot intent/control must be outside the quota filesystem')
                fcntl.flock(native,fcntl.LOCK_EX|fcntl.LOCK_NB)
                yield prefix,meta,config,control,native
        finally:os.close(control)


def _identity(fd,marker):
    info=os.fstat(fd);fs=os.fstatvfs(fd)
    if (info.st_dev,info.st_ino,fs.f_blocks*fs.f_frsize,fs.f_files)!=(marker['device'],marker['root_inode'],marker['bytes'],marker['inodes']):
        raise DeployError('Native quota filesystem changed since planning')


def _empty_quota(current):
    return not any(current[k] for k in ['byte_hard','byte_soft','inode_hard','inode_soft','bytes_used','inodes_used'])


def _space(native,items):
    current=os.fstatvfs(native)
    if (current.f_bavail*current.f_frsize<sum(i['bytes'] for i in items)+16*1024**2
        or current.f_favail<sum(i['inodes'] for i in items)+128):
        raise DeployError('Native free space cannot cover new UID quotas and metadata headroom')


def plan(prefix,root,limits):
    requested=requested_limits(limits)
    with _context(prefix,root) as (prefix,meta,config,control,native):
        if _read(native,EXTEND_INTENT,True) is not None:raise DeployError('Resume the original pending UID slot append plan')
        marker=_marker(read_marker(native),root);_identity(native,marker)
        validate({'slots':marker['slots']},host=True)
        for item in requested:
            if not _empty_quota(quota.read(native,item['uid'])):
                raise DeployError('New UID already has native data or limits; no automatic adoption')
        value={'format':PLAN,'prefix':str(prefix),'release_id':meta['release_id'],'node_id':config['node_id'],
            'control':meta['state_dir'],'control_identity':[os.fstat(control).st_dev,os.fstat(control).st_ino],
            'configuration_sha256':fingerprint(config),'root':root,'before':marker,
            'additions':[{**item,'id':'slot-'+uuid.uuid4().hex[:12],'storage_id':uuid.uuid4().hex} for item in requested]}
        value['id']=fingerprint(value);_validate_plan(value)
        _space(native,value['additions'])
        return {'read_only':True,'plan':value,'accounts_or_node_configuration_changed':False}


def _matches_after(value,marker):
    n=len(value['before']['slots'])
    if marker.get('slots',[])[:n]!=value['before']['slots'] or len(marker.get('slots',[]))!=n+len(value['additions']):return False
    expected=copy.deepcopy(value['before'])
    expected['slots']+=[_candidate(value,item,slot.get('root_inode')) for item,slot in zip(value['additions'],marker['slots'][n:])]
    return marker==expected


def _check_old(native,value):
    for slot in value['before']['slots']:quota.require(native,slot['quota_uid'],slot['bytes'],slot['inodes'])
    validate({'slots':value['before']['slots']},host=True)


def apply(prefix,value):
    require_root();_validate_plan(value)
    with _context(prefix,value['root'],True) as (prefix,meta,config,control,native):
        if (str(prefix),meta['release_id'],config['node_id'],meta['state_dir'],fingerprint(config),
            [os.fstat(control).st_dev,os.fstat(control).st_ino])!=(value['prefix'],value['release_id'],value['node_id'],value['control'],
            value['configuration_sha256'],value['control_identity']):raise DeployError('UID plan belongs to another or changed Node installation')
        marker=_marker(read_marker(native),value['root']);_identity(native,value['before'])
        intent=_read(native,EXTEND_INTENT,True);journal=_read(control,JOURNAL,True)
        if any(record is not None and record!=value for record in [intent,journal]):raise DeployError('Resume only the original pending UID append plan')
        receipt_name=value['id']+'.json'
        try:history=os.open(HISTORY,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=control)
        except FileNotFoundError:history=None
        try:
            if history is not None:
                info=os.fstat(history)
                if info.st_uid!=0 or info.st_mode&0o777!=0o700:raise DeployError('UID slot audit directory must be Root-private')
            receipt=_read(history,receipt_name,True) if history is not None else None
            if receipt is not None:
                if (receipt.get('plan_id')!=value['id'] or marker!=receipt.get('after') or not _matches_after(value,marker)):
                    raise DeployError('An intervening change superseded this UID append receipt')
                validate({'slots':marker['slots']},host=True)
            else:
                resumed=intent is not None or journal is not None
                if marker!=value['before'] and not (resumed and _matches_after(value,marker)):
                    raise DeployError('UID append plan is stale; no new quota was changed')
                _check_old(native,value)
                if not resumed:
                    if any(item['id'] in os.listdir(native) for item in value['additions']):raise DeployError('A planned new slot path already exists')
                    if any(not _empty_quota(quota.read(native,item['uid'])) for item in value['additions']):raise DeployError('New UID data/limits changed after planning')
                    if history is not None and len(os.listdir(history))>=1024:raise DeployError('Archive UID append receipts before further changes')
                    _space(native,value['additions'])
                if journal is None:_write(control,JOURNAL,value)
                _checkpoint('journal')
                if intent is None:_write(native,EXTEND_INTENT,value)
                _checkpoint('intent')
                added=[]
                for item in value['additions']:
                    current=quota.read(native,item['uid'])
                    if _empty_quota(current):quota.provision(native,item['uid'],item['bytes'],item['inodes'])
                    else:
                        quota.require(native,item['uid'],item['bytes'],item['inodes'])
                        if current['bytes_used'] or current['inodes_used']:raise DeployError('New UID was used before append completion; preserve intent')
                    _checkpoint('quota')
                    try:os.mkdir(item['id'],0o700,dir_fd=native);os.fsync(native)
                    except FileExistsError:pass
                    descriptor=os.open(item['id'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=native)
                    try:
                        info=os.fstat(descriptor)
                        if info.st_uid!=0 or info.st_mode&0o777!=0o700 or info.st_dev!=value['before']['device'] or os.listdir(descriptor):
                            raise DeployError('Planned new quota slot is unsafe or no longer empty')
                        added.append(_candidate(value,item,info.st_ino));os.fsync(descriptor)
                    finally:os.close(descriptor)
                    _checkpoint('directory')
                after=copy.deepcopy(value['before']);after['slots']+=added
                if marker not in [value['before'],after]:raise DeployError('Published slot inode identities changed; preserve intent')
                _write(native,MARKER,after);_checkpoint('marker');validate({'slots':after['slots']},host=True)
                if history is None:
                    os.mkdir(HISTORY,0o700,dir_fd=control);os.fsync(control)
                    history=os.open(HISTORY,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=control)
                receipt={'format':'ascend-lab-uid-slot-add-receipt/v1','plan_id':value['id'],'after':after,
                    'existing_slots_unchanged':True,'accounts_or_node_configuration_changed':False,'completed_at':now()}
                _write(history,receipt_name,receipt);_checkpoint('receipt')
            for fd,name in [(native,EXTEND_INTENT),(control,JOURNAL)]:
                if _read(fd,name,True) is not None:os.unlink(name,dir_fd=fd);os.fsync(fd)
            _checkpoint('published')
            return {'applied':True,'plan_id':value['id'],'policy':{'slots':receipt['after']['slots']},
                'receipt':receipt,'node_and_central_owner_binding_still_required':True}
        finally:
            if history is not None:os.close(history)

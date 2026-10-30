"""Administrator-provisioned, dedicated finite filesystems. Never mount or resize a host FS.

A slot contains one root-owned lease and one task work tree. The node verifies actual
filesystem identity/limits; a user can neither choose a host path nor release a lease.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

CAPABILITY='filesystem-workspace-v1'
from quota_workspace import PROVIDER,EXTRA,is_quota,sharing,capacity


def validate(policy, host=False):
    if not isinstance(policy,dict) or set(policy)!={'slots'} or not isinstance(policy['slots'],list) or not 1<=len(policy['slots'])<=256:
        raise ValueError('Workspace policy requires 1..256 registered slots')
    if len(policy['slots'])>32 and any(not is_quota(s) for s in policy['slots']):raise ValueError('More than 32 slots requires UID quota workspaces')
    ids=set();paths=set();devices=[];storage=set()
    for slot in policy['slots']:
        keys={'id','storage_id','path','bytes','inodes','device','root_inode','filesystem'}
        if not isinstance(slot,dict) or set(slot)!=keys|(EXTRA if is_quota(slot) else set()):raise ValueError('Invalid workspace slot contract')
        if not isinstance(slot['id'],str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}',slot['id']):raise ValueError('Invalid workspace slot ID')
        if not isinstance(slot['storage_id'],str) or not re.fullmatch('[0-9a-f]{32}',slot['storage_id']):raise ValueError('Workspace storage identity must be unique and pinned')
        path=slot['path']
        if (not isinstance(path,str) or not Path(path).is_absolute() or path!=str(Path(path)) or '..' in Path(path).parts
                or ',' in path or '\\' in path or any(ord(c)<32 for c in path) or path=='/'):
            raise ValueError('Workspace requires a canonical absolute mount path')
        if slot['filesystem'] not in {'ext4','tmpfs',PROVIDER}:raise ValueError('Unsupported finite workspace provider')
        for key,minimum,maximum in [('bytes',16*1024**2,1024**4),('inodes',128,10000000),('device',1,2**64-1),('root_inode',1,2**64-1)]:
            if type(slot[key]) is not int or not minimum<=slot[key]<=maximum:raise ValueError('Invalid finite workspace capacity or identity')
        if is_quota(slot):
            if (type(slot['quota_uid']) is not int or not 1000<=slot['quota_uid']<2**31
                or not isinstance(slot['filesystem_id'],str) or not re.fullmatch('[0-9a-f]{32}',slot['filesystem_id'])
                or type(slot['filesystem_bytes']) is not int or not 32*1024**2<=slot['filesystem_bytes']<=1024**5
                or type(slot['filesystem_inodes']) is not int or not 256<=slot['filesystem_inodes']<=2**63-1
                or slot['bytes']%4096):raise ValueError('Invalid UID quota backing identity or capacity')
        if (slot['id'] in ids or path in paths or slot['storage_id'] in storage
            or any(old['device']==slot['device'] and not sharing(old,slot) for old in devices)):
            raise ValueError('Workspace slots must have distinct IDs, paths and backing allocations')
        ids.add(slot['id']);paths.add(path);devices.append(slot);storage.add(slot['storage_id'])
        if host:
            with root_fd(slot):pass
    capacity(policy['slots']);return policy


def fingerprint(policy):
    return hashlib.sha256(json.dumps(validate(policy),sort_keys=True,separators=(',',':')).encode()).hexdigest()


@contextmanager
def root_fd(slot):
    if os.geteuid()!=0:raise ValueError('Bounded filesystem workspace requires a trusted root node')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for component in Path(slot['path']).parts[1:]:
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        info=os.fstat(fd);fs=os.fstatvfs(fd)
        if info.st_uid!=0 or info.st_mode&0o777!=0o700:raise ValueError('Workspace mount root must be root-owned mode 700')
        if (info.st_dev,info.st_ino)!=(slot['device'],slot['root_inode']):raise ValueError('Workspace filesystem identity changed')
        if not is_quota(slot) and (fs.f_blocks*fs.f_frsize!=slot['bytes'] or fs.f_files!=slot['inodes']):
            raise ValueError('Actual workspace byte/inode hard limits differ from contract')
        if fs.f_flag&os.ST_RDONLY or not fs.f_flag&os.ST_NOSUID or not fs.f_flag&os.ST_NODEV:
            raise ValueError('Workspace filesystem must be writable with nosuid,nodev')
        # A bind of a subdirectory on an unbounded host filesystem is not a slot.
        if is_quota(slot):
            from quota_workspace import verify
            verify(slot,fd)
        else:
            parent=os.stat(Path(slot['path']).parent)
            if parent.st_dev==info.st_dev:raise ValueError('Workspace must be a dedicated filesystem mount root')
            mount=None
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                before,after=line.split(' - ',1);parts=before.split()
                decoded=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),parts[4])
                if decoded==slot['path']:mount=after.split()[0]
            if mount!=slot['filesystem']:raise ValueError('Workspace mount type differs from contract')
        yield fd
    except OSError:raise ValueError('Dedicated workspace filesystem is unavailable or unsafe') from None
    finally:os.close(fd)


def read_lease(fd):
    try:source=os.open('.ascend-lab-lease.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    except FileNotFoundError:return None
    with os.fdopen(source,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
            raise ValueError('Unsafe workspace lease record')
        raw=stream.read(4097)
    try:
        if len(raw)>4096:raise ValueError()
        record=json.loads(raw)
        if not isinstance(record,dict):raise ValueError()
        return record
    except ValueError:raise ValueError('Workspace lease is incomplete; administrator reconciliation required') from None


def observe(policy):
    validate(policy);slots=[]
    for slot in policy['slots']:
        try:
            with root_fd(slot) as fd:
                fs=os.fstatvfs(fd);lease=read_lease(fd)
                free_bytes=fs.f_bavail*fs.f_frsize;free_inodes=fs.f_favail
                if is_quota(slot):
                    from filesystem_quota import require
                    reading=require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
                    free_bytes=min(free_bytes,max(0,slot['bytes']-reading['bytes_used']))
                    free_inodes=min(free_inodes,max(0,slot['inodes']-reading['inodes_used']))
                slots.append({'id':slot['id'],'status':'ok','clean':lease is None and not os.listdir(fd),
                              'free_bytes':free_bytes,'free_inodes':free_inodes})
        except (OSError,ValueError):slots.append({'id':slot['id'],'status':'error','clean':False,'free_bytes':0,'free_inodes':0})
    return {'policy_sha256':fingerprint(policy),'slots':slots}


def observation(value,policy):
    if not isinstance(value,dict) or value.get('policy_sha256')!=fingerprint(policy):raise ValueError('Workspace observation contract differs')
    entries=value.get('slots')
    if not isinstance(entries,list) or len(entries)!=len(policy['slots']):raise ValueError('Missing workspace observations')
    result=[];seen=set();registered={slot['id']:slot for slot in policy['slots']}
    for entry in entries:
        if not isinstance(entry,dict) or entry.get('id') not in registered or entry['id'] in seen:raise ValueError('Unknown or duplicate workspace observation')
        slot=registered[entry['id']];seen.add(slot['id'])
        if entry.get('status') not in {'ok','error'} or type(entry.get('clean')) is not bool:raise ValueError('Invalid workspace state')
        for key,maximum in [('free_bytes',slot['bytes']),('free_inodes',slot['inodes'])]:
            if type(entry.get(key)) is not int or not 0<=entry[key]<=maximum:raise ValueError('Invalid workspace available capacity')
        result.append({key:entry[key] for key in ['id','status','clean','free_bytes','free_inodes']})
    return {'policy_sha256':fingerprint(policy),'slots':result}


def allocation(slot):
    return {'slot_id':slot['id'],'storage_id':slot['storage_id'],'bytes':slot['bytes'],'inodes':slot['inodes'],
            'durable':slot['filesystem']!='tmpfs',**({'provider':'uid-quota-v1'} if is_quota(slot) else {})}


def selected(policy,grant,task_uid=None):
    validate(policy)
    if grant.get('workspace_policy_sha256')!=fingerprint(policy):raise ValueError('Workspace grant differs from node contract')
    granted=grant['allocation'].get('workspace')
    slot=next((slot for slot in policy['slots'] if isinstance(granted,dict) and slot['id']==granted.get('slot_id')),None)
    if slot is None or allocation(slot)!=granted:raise ValueError('Workspace allocation differs from pinned slot')
    if is_quota(slot) and task_uid!=slot['quota_uid']:raise ValueError('Workspace user quota belongs to another execution UID')
    if slot['filesystem']=='tmpfs' and slot['bytes']>grant['resources']['memory_mib']*1024**2:
        raise ValueError('Volatile workspace must fit the reserved task memory')
    return slot


class Workspace:
    def __init__(self,policy,grant,task_uid=None):
        self.slot=selected(policy,grant,task_uid)
        self.quota_attestation=None
        self.name=grant['attempt_id']
        if not re.fullmatch('attempt-[0-9a-f]{32}',self.name):raise ValueError('Invalid workspace attempt identity')
        self.path=Path(self.slot['path'])/self.name
        self.lease={'attempt_id':self.name,'job_id':grant['job_id'],'node_id':grant['node_id'],
                    'workspace_policy_sha256':fingerprint(policy),'storage_id':self.slot['storage_id']}

    def prepare(self):
        with root_fd(self.slot) as fd:
            if is_quota(self.slot):
                from filesystem_quota import require
                self.quota_attestation=require(fd,self.slot['quota_uid'],self.slot['bytes'],self.slot['inodes'])
            old=read_lease(fd)
            if old is None:
                if os.listdir(fd):raise ValueError('Workspace is not empty; refusing deletion or reuse')
                record=os.open('.ascend-lab-lease.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                with os.fdopen(record,'w') as stream:
                    json.dump(self.lease,stream);stream.flush();os.fsync(stream.fileno())
                os.fsync(fd)
            elif old!=self.lease:raise ValueError('Workspace is held by another attempt; refusing reuse')
            if set(os.listdir(fd))-set([self.name,'.ascend-lab-lease.json']):raise ValueError('Unexpected files on workspace filesystem')
            try:os.mkdir(self.name,0o700,dir_fd=fd)
            except FileExistsError:pass
            child=os.open(self.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                info=os.fstat(child);fs=os.fstatvfs(child)
                if info.st_dev!=self.slot['device']:raise ValueError('Work directory is on another filesystem')
                return {'device':info.st_dev,'inode':info.st_ino,'bytes':fs.f_blocks*fs.f_frsize,'inodes':fs.f_files}
            finally:os.close(child)

    def release(self):
        # Caller must first confirm container termination and remove only its own work tree.
        with root_fd(self.slot) as fd:
            lease=read_lease(fd)
            if lease is None:
                if os.listdir(fd):raise ValueError('Workspace lease is missing but files remain')
                return
            if lease!=self.lease:raise ValueError('Refusing release of another attempt workspace')
            if set(os.listdir(fd))!={'.ascend-lab-lease.json'}:raise ValueError('Workspace still contains files; reservation retained')
            os.unlink('.ascend-lab-lease.json',dir_fd=fd);os.fsync(fd)

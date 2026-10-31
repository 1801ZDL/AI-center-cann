"""Finite controller filesystem binding. Root prepares mounts; no format/mount.

The service's private mount namespace pins SQLite's canonical paths. An origin
directory FD observes outside mount changes separately from that stable view.
"""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat

CAPABILITY='finite-control-filesystem-v1'
MARKER='.ascend-lab-control.json'
ORIGIN_FD='ASCEND_LAB_CONTROL_ORIGIN_FD'
NAMESPACE_FD='ASCEND_LAB_CONTROL_NAMESPACE_FD'
LOCK_FD='ASCEND_LAB_CONTROL_LOCK_FD'
ENVIRONMENT=[ORIGIN_FD,NAMESPACE_FD,LOCK_FD]
HANDLE='native-control-process.json'


class Unavailable(OSError):
    def __init__(self):super().__init__(errno.ENODEV,'Required controller filesystem is unavailable; original state is retained')


def validate(value):
    from payload_storage import validate as payload
    if not isinstance(value,dict) or set(value)!={'path','filesystem','device','root_inode','bytes','inodes','storage_id'}:
        raise ValueError('Invalid finite control filesystem contract')
    payload(value)
    if value['bytes']<128*1024**2 or value['inodes']<1024:raise ValueError('Finite control filesystem needs at least128 MiB and1024 inodes')
    return value


def fingerprint(value):return hashlib.sha256(json.dumps(validate(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def protected(path,maximum=2*1024**2):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o444 or info.st_nlink!=1:
            raise ValueError('Control binding requires a protected Root record')
        raw=file.read(maximum+1)
    if len(raw)>maximum:raise ValueError('Control binding record too large')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Invalid control binding record')
    return value


def mount_id(fd):
    from storage_files import mount
    return mount(fd)


@contextmanager
def root_fd(policy,*,origin=None):
    validate(policy);path=Path(policy['path'])
    if origin is None:
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            for name in path.parts[1:]:
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        except BaseException:os.close(fd);raise
    else:fd=os.open(path.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=origin)
    try:
        info=os.fstat(fd);space=os.fstatvfs(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o755:
            raise ValueError('Control filesystem mount root must be Root-owned755')
        if (info.st_dev,info.st_ino)!=(policy['device'],policy['root_inode']):raise ValueError('Control filesystem identity changed')
        if (space.f_blocks*space.f_frsize,space.f_files)!=(policy['bytes'],policy['inodes']):raise ValueError('Control filesystem geometry changed')
        if space.f_flag&os.ST_RDONLY or not space.f_flag&os.ST_NOSUID or not space.f_flag&os.ST_NODEV:
            raise ValueError('Control filesystem needs writable,nosuid,nodev')
        if origin is None:
            mounts=[]
            for row in Path('/proc/self/mountinfo').read_text().splitlines():
                before,after=row.split(' - ',1)
                if before.split()[4]==str(path):mounts.append((before.split()[3],after.split()[0]))
            if mounts!=[('/','ext4')] or path.parent.stat().st_dev==info.st_dev:raise ValueError('Control root must mount the entire dedicated ext4 filesystem, not a bound subdirectory')
        elif os.fstat(origin).st_dev==info.st_dev:raise ValueError('Origin mount is no longer separate')
        yield fd
    finally:os.close(fd)


def binding(policy,state,prefix,uid,gid):
    if Path(state)!=Path(policy['path'])/'state':raise ValueError('Finite control state must be the mount root/state child')
    return {'format':'ascend-lab-control-filesystem/v1','policy_sha256':fingerprint(policy),
            'state_dir':str(state),'prefix':str(prefix),'uid':uid,'gid':gid}


def inspect_new(policy,state,prefix,payload):
    validate(policy)
    if os.geteuid()!=0:raise ValueError('Finite control preparation requires Root')
    if not payload or payload['device']==policy['device']:raise ValueError('Finite control requires a separate payload filesystem')
    binding(policy,state,prefix,0,0)
    for ancestor in Path(policy['path']).parents:
        item=ancestor.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid!=0 or item.st_mode&0o022:raise ValueError('Control ancestors must be protected Root directories')
    for other in [Path(prefix),Path(payload['path'])]:
        own=Path(policy['path'])
        if own==other or own.is_relative_to(other) or other.is_relative_to(own):raise ValueError('Control/code/payload must be disjoint')
        parent=other
        while not parent.exists():parent=parent.parent
        if parent.stat().st_dev==policy['device']:raise ValueError('Control/code/payload must use separate filesystems')
    with root_fd(policy) as fd:
        if os.listdir(fd):raise ValueError('Provide a new empty control filesystem')
        space=os.fstatvfs(fd)
        return {'enforced':True,'filesystem':'ext4','bytes':policy['bytes'],'inodes':policy['inodes'],
                'free_bytes':space.f_bavail*space.f_frsize,'separate_payload':True,'configuration_sha256':fingerprint(policy)}


def initialize(policy,state,prefix,uid,gid):
    if os.geteuid()!=0:raise ValueError('Control binding publication requires Root')
    record=binding(policy,state,prefix,uid,gid)
    with root_fd(policy) as fd:
        if os.listdir(fd)!=['state']:raise ValueError('Control candidate changed; preserve it for inspection')
        item=os.stat('state',dir_fd=fd,follow_symlinks=False)
        if not stat.S_ISDIR(item.st_mode) or item.st_uid!=uid or stat.S_IMODE(item.st_mode)!=0o700:
            raise ValueError('Control state ownership differs')
        marker=os.open(MARKER,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444,dir_fd=fd)
        with os.fdopen(marker,'wb') as file:
            os.fchmod(file.fileno(),0o444);file.write((json.dumps(record,sort_keys=True)+'\n').encode());file.flush();os.fsync(file.fileno())
        os.fsync(fd)
    return record


class Filesystem:
    def __init__(self,state,config,uid=None):
        self.state=Path(state);self.policy=config.get('scheduler',{}).get('control_filesystem');self.uid=os.geteuid() if uid is None else uid
        if self.policy is not None:
            validate(self.policy)
            scheduler=config.get('scheduler',{})
            from control_storage import validate as headroom
            if scheduler.get('local_enabled') is not False or headroom(scheduler.get('control_storage')) is None:
                raise ValueError('Finite control requires managed-only execution and headroom admission')
            payload=scheduler.get('payload_storage')
            if not payload or payload.get('device')==self.policy['device']:raise ValueError('Finite control requires separate payload storage')

    def _check(self,origin=None):
        with root_fd(self.policy,origin=origin) as fd:
            record=protected(Path('/proc/self/fd')/str(fd)/MARKER,16384)
            if (record.get('format')!='ascend-lab-control-filesystem/v1' or record.get('policy_sha256')!=fingerprint(self.policy)
                or record.get('state_dir')!=str(self.state) or record.get('uid')!=self.uid):raise ValueError('Control binding differs')
            prefix=Path(record['prefix'])
            for parent in [prefix,*prefix.parents]:
                item=parent.lstat()
                if not stat.S_ISDIR(item.st_mode) or item.st_uid!=0 or item.st_mode&0o022:raise ValueError('Control installation ancestry changed')
            meta=protected(prefix/'installation.json')
            if (meta.get('role')!='controller' or meta.get('state_dir')!=str(self.state) or meta.get('uid')!=self.uid
                or meta.get('gid')!=record['gid'] or meta.get('control_filesystem')!=self.policy):raise ValueError('Control installation binding differs')
            child=os.open('state',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                item=os.fstat(child)
                if item.st_uid!=self.uid or stat.S_IMODE(item.st_mode)!=0o700 or item.st_dev!=self.policy['device']:
                    raise ValueError('Control state identity changed')
            finally:os.close(child)
            return prefix

    def check(self):
        if self.policy is None:return
        try:return self._check()
        except (OSError,ValueError,KeyError,TypeError):raise Unavailable() from None

    def runtime(self):
        prefix=self.check()
        if prefix is None:return
        try:
            from jobs import identity
            handle=protected(prefix/HANDLE);current=identity(os.getpid())
            if any(handle.get(k)!=current.get(k) for k in ['pid','start_ticks','boot_id']):raise ValueError('No protected runtime identity')
            for key in ENVIRONMENT:
                if not re.fullmatch('[0-9]{1,6}',os.environ.get(key,'')):raise ValueError('Missing controller namespace descriptor')
            origin=int(os.environ[ORIGIN_FD]);namespace=int(os.environ[NAMESPACE_FD]);lock=int(os.environ[LOCK_FD])
            info=os.fstat(origin);observed={'device':info.st_dev,'inode':info.st_ino,'mount_id':mount_id(origin)}
            if observed!=handle['origin_parent'] or os.fstat(namespace).st_ino!=handle['origin_namespace']:
                raise ValueError('Controller origin descriptors differ')
            if os.stat('/proc/self/ns/mnt').st_ino!=handle['private_namespace'] or handle['private_namespace']==handle['origin_namespace']:
                raise ValueError('Controller is not in its pinned private mount namespace')
            expected=(prefix/'native-control.lock').stat();actual=os.fstat(lock)
            if (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):raise ValueError('Controller lock descriptor differs')
            return origin
        except (OSError,ValueError,KeyError,TypeError):raise Unavailable() from None

    def origin(self):
        if self.policy is None:return
        if not any(key in os.environ for key in ENVIRONMENT):return self.check()
        origin=self.runtime()
        try:self._check(origin)
        except (OSError,ValueError,KeyError,TypeError):raise Unavailable() from None

    def temporary_environment(self):
        if self.policy is None:return {}
        self.check()
        parent=os.open(self.state,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            try:os.mkdir('tmp',0o700,dir_fd=parent)
            except FileExistsError:pass
            fd=os.open('tmp',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            try:
                item=os.fstat(fd)
                if item.st_dev!=self.policy['device'] or item.st_uid!=self.uid or stat.S_IMODE(item.st_mode)!=0o700:
                    raise Unavailable()
                os.fsync(fd);os.fsync(parent)
            finally:os.close(fd)
        finally:os.close(parent)
        return {'TMPDIR':str(self.state/'tmp'),'SQLITE_TMPDIR':str(self.state/'tmp')}

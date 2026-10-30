"""Explicit finite central payload filesystem; no mount, format or fallback.

Control SQLite/auth/locks remain in state. Inputs, archives and quarantine share
one separately provisioned ext4 filesystem. Every path resolution rechecks it.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

MARKER='.ascend-lab-payload.json'
TREES={'jobs','inputs','storage-trash'}


def validate(value):
    keys={'path','filesystem','device','root_inode','bytes','inodes','storage_id'}
    if not isinstance(value,dict) or set(value)-{'owner_groups'}!=keys:raise ValueError('Invalid dedicated central payload contract')
    path=value['path']
    if not isinstance(path,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',path) or path!=str(Path(path)) or '..' in Path(path).parts or path=='/':
        raise ValueError('Central payload path must be protected, canonical and absolute')
    if value['filesystem']!='ext4' or not re.fullmatch('[0-9a-f]{32}',str(value['storage_id'])):raise ValueError('A pinned ext4 storage identity is required')
    for key,low,high in [('device',1,2**64-1),('root_inode',1,2**64-1),('bytes',64*1024**2,1024**4),('inodes',512,10000000)]:
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('Invalid central payload capacity or identity')
    from payload_groups import validate as groups
    groups(value)
    return value


@contextmanager
def root_fd(value,uid):
    validate(value);fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for component in Path(value['path']).parts[1:]:
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        item=os.fstat(fd);space=os.fstatvfs(fd)
        if item.st_uid!=uid or item.st_mode&0o777!=0o700:raise ValueError('Central payload root ownership or permissions differ')
        if (item.st_dev,item.st_ino)!=(value['device'],value['root_inode']):raise ValueError('Central payload filesystem identity changed')
        if (space.f_blocks*space.f_frsize,space.f_files)!=(value['bytes'],value['inodes']):raise ValueError('Central payload filesystem capacity changed')
        if space.f_flag&os.ST_RDONLY or not space.f_flag&os.ST_NOSUID or not space.f_flag&os.ST_NODEV:raise ValueError('Central payload filesystem requires writable,nosuid,nodev')
        mounts=[]
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before,after=line.split(' - ',1)
            if before.split()[4]==value['path']:mounts.append(after.split()[0])
        if mounts!=['ext4'] or os.stat(Path(value['path']).parent).st_dev==item.st_dev:
            raise ValueError('Central payload must be a dedicated ext4 mount root, not an ordinary directory')
        yield fd
    finally:os.close(fd)


def disjoint(value,state,prefix=None):
    path=Path(validate(value)['path'])
    for other in [Path(state),*([Path(prefix)] if prefix else [])]:
        if path==other or path.is_relative_to(other) or other.is_relative_to(path):raise ValueError('Central payload, control state and code must be disjoint')


def inspect_new(value,state,prefix=None):
    if os.geteuid()!=0:raise ValueError('Payload installation needs the trusted administrator')
    disjoint(value,state,prefix)
    for ancestor in Path(value['path']).parents:
        item=ancestor.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid!=0 or item.st_mode&0o022:raise ValueError('Central payload ancestors must be protected root directories')
    with root_fd(value,0) as fd:
        if os.listdir(fd):raise ValueError('Provide a new empty dedicated payload filesystem')
        if value.get('owner_groups'):
            from payload_groups import check_all
            check_all(fd,value,empty=True)
        space=os.fstatvfs(fd);parent=Path(state).parent
        while not parent.exists():parent=parent.parent
        if parent.stat().st_dev==value['device']:raise ValueError('Control state must be on a separate filesystem')
        return {'storage_id':value['storage_id'],'filesystem':'ext4','bytes':value['bytes'],'inodes':value['inodes'],
                'free_bytes':space.f_bavail*space.f_frsize,'empty':True,'control_filesystem_separate':True,
                'owner_hard_quotas':len(value.get('owner_groups',{}))}


def initialize(value,state,uid,gid,staged=None,ownership_state=None):
    """Only installation/verified restore calls this on its new empty filesystem."""
    from lab_client import atomic_json
    from payload_groups import preparing_identity
    with preparing_identity(value),root_fd(value,0) as fd:
        owners=None
        if value.get('owner_groups'):
            from payload_groups import check_all,require_membership,restored_owners
            require_membership(value,primary=gid);check_all(fd,value,empty=True)
            if staged is not None:
                if ownership_state is None:raise ValueError('Grouped payload restore requires authoritative owner databases')
                owners=restored_owners(ownership_state)
        expected=[] if staged is None else [Path(staged).name]
        if sorted(os.listdir(fd))!=sorted(expected):raise ValueError('Payload contents changed before initialization')
        if staged is not None:
            if Path(staged).parent!=Path(value['path']):raise ValueError('Restore staging must be on the new payload filesystem')
            if set(p.name for p in Path(staged).iterdir())-TREES:raise ValueError('Unexpected restored payload tree')
        for name in sorted(TREES):
            target=Path(value['path'])/name
            source=Path(staged)/name if staged is not None else None
            if source is not None and source.exists():source.rename(target)
            else:target.mkdir(mode=0o700)
            for parent,dirs,files in os.walk(target,followlinks=False):
                for entry in [Path(parent),*(Path(parent)/n for n in dirs+files)]:
                    if entry.is_symlink():raise ValueError('Payload restore cannot publish symlinks')
                    actual_gid=gid
                    if owners is not None:
                        from payload_groups import restored_gid
                        actual_gid=restored_gid(entry.relative_to(Path(value['path'])),owners,value,gid)
                    os.chown(entry,uid,actual_gid)
                    if entry.is_dir():
                        mode=0o2700 if actual_gid!=gid else 0o700
                        entry.chmod(mode)
                        current=entry.stat()
                        if (current.st_uid,current.st_gid,current.st_mode&0o7777)!=(uid,actual_gid,mode):
                            raise ValueError('Restored directory ownership or inheritance was not applied; preserve the candidate for reconciliation')
        if staged is not None:Path(staged).rmdir()
        if value.get('owner_groups'):
            from payload_groups import check_all
            check_all(fd,value)
        atomic_json(Path(value['path'])/MARKER,binding(value,state))
        os.chown(Path(value['path'])/MARKER,uid,gid)
        os.fchown(fd,uid,gid);os.fchmod(fd,0o700);os.fsync(fd)


def binding(policy,state):
    value={'format':'central-payload/v1','storage_id':policy['storage_id'],'state_dir':str(state)}
    if policy.get('owner_groups'):
        import hashlib
        value['owner_groups_sha256']=hashlib.sha256(json.dumps(policy['owner_groups'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return value


class Payload:
    def __init__(self,state,config,uid=None):
        self.state=Path(state);self.policy=config.get('scheduler',{}).get('payload_storage');self.uid=os.geteuid() if uid is None else uid
        if self.policy is not None:validate(self.policy)
        self.base=Path(self.policy['path']) if self.policy is not None else self.state
        if self.policy is not None:
            if config.get('monitor_local') is not False or config.get('scheduler',{}).get('local_enabled') is not False:
                raise ValueError('Dedicated payload storage requires a controller without local execution')
            disjoint(self.policy,self.state);self.check()
            if self.policy.get('owner_groups'):
                from payload_groups import check_all
                with root_fd(self.policy,self.uid) as fd:check_all(fd,self.policy)
            if any((self.state/name).exists() for name in TREES):raise ValueError('Legacy and dedicated payload trees cannot be mixed')

    def check(self):
        if not self.policy:return
        try:
            with root_fd(self.policy,self.uid) as fd:
                if self.state.stat().st_dev==self.policy['device']:raise ValueError('Control and payload filesystems must be separate')
                if set(os.listdir(fd))!=TREES|{MARKER}:raise ValueError('Unknown files or an unfinished restore remain on the payload filesystem')
                source=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
                with os.fdopen(source,'rb') as file:
                    item=os.fstat(file.fileno())
                    if not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or item.st_uid!=self.uid or item.st_mode&0o077:raise ValueError('Invalid payload binding marker')
                    raw=file.read(4097)
                expected=binding(self.policy,self.state)
                if len(raw)>4096 or json.loads(raw)!=expected:raise ValueError('Payload belongs to another control state')
                for name in TREES:
                    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    try:
                        item=os.fstat(child)
                        if item.st_dev!=self.policy['device'] or item.st_uid!=self.uid or item.st_mode&0o777!=0o700:raise ValueError('Invalid central payload tree')
                    finally:os.close(child)
                if self.policy.get('owner_groups'):
                    from payload_groups import require_membership
                    from group_quota import state
                    require_membership(self.policy)
                    status=state(fd)
                    if not status['accounting'] or not status['enforced']:raise ValueError('Group quota enforcement is inactive')
        except (OSError,ValueError,TypeError):
            from jobs import JobError
            raise JobError('Central payload filesystem unavailable or identity changed; no fallback directory was used',503) from None

    def path(self,name):
        if name not in TREES:raise ValueError('Unknown central payload tree')
        self.check();return self.base/name

    def owner_space(self,owner):
        if not self.policy or not self.policy.get('owner_groups'):return None
        self.check()
        from payload_groups import check_owner
        try:
            with root_fd(self.policy,self.uid) as fd:return check_owner(fd,self.policy,owner)
        except (OSError,ValueError):
            from jobs import JobError
            raise JobError('Owner central storage quota unavailable or changed; no fallback was used',503) from None

    def object(self,path,owner,create=False):
        if not self.policy or not self.policy.get('owner_groups'):return
        self.owner_space(owner)
        if create:
            from payload_groups import initialize_object
            initialize_object(path,self.policy,owner)
        else:
            from payload_groups import verify_object
            try:verify_object(path,self.policy,owner)
            except (OSError,ValueError):
                from jobs import JobError
                raise JobError('Owner storage directory group or inheritance changed; no fallback was used',503) from None

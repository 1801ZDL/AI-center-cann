"""Root-prepared UID quota slots on one dedicated, already mounted ext4 filesystem."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import uuid

import filesystem_quota as quota

PROVIDER='ext4-userquota'
CAPABILITY='uid-quota-workspace-v1'
MARKER='.ascend-lab-userquota.json'
INTENT='.ascend-lab-userquota-preparing.json'
FORMAT='ascend-lab-userquota/v1'
EXTRA={'quota_uid','filesystem_id','filesystem_bytes','filesystem_inodes'}


def is_quota(slot):return isinstance(slot,dict) and slot.get('filesystem')==PROVIDER


def sharing(a,b):
    return (is_quota(a) and is_quota(b) and a['device']==b['device'] and a['quota_uid']!=b['quota_uid']
            and a['filesystem_id']==b['filesystem_id']
            and a['filesystem_bytes']==b['filesystem_bytes'] and a['filesystem_inodes']==b['filesystem_inodes'])


def capacity(slots):
    groups={}
    for slot in slots:
        if is_quota(slot):groups.setdefault(slot['device'],[]).append(slot)
    for group in groups.values():
        first=group[0]
        if any((s['filesystem_id'],s['filesystem_bytes'],s['filesystem_inodes'])!=
               (first['filesystem_id'],first['filesystem_bytes'],first['filesystem_inodes']) for s in group):
            raise ValueError('Shared quota filesystem identity/capacity differs')
        if len({s['quota_uid'] for s in group})!=len(group):raise ValueError('A UID quota cannot be leased by multiple slots on the same filesystem')
        if (sum(s['bytes'] for s in group)>first['filesystem_bytes']-16*1024**2
            or sum(s['inodes'] for s in group)>first['filesystem_inodes']-max(128,len(group)*2+32)):
            raise ValueError('User quota totals leave insufficient shared filesystem or metadata capacity')


def separate_control(policy,path):
    devices={s['device'] for s in policy.get('slots',[]) if is_quota(s)}
    if not devices:return
    current=Path(path).absolute()
    while not current.exists():
        if current==current.parent:raise ValueError('Cannot establish node control filesystem')
        current=current.parent
    if current.stat().st_dev in devices:raise ValueError('Node control/spool must be outside the dedicated user-quota filesystem')


def canonical(path):
    return (isinstance(path,str) and Path(path).is_absolute() and path==str(Path(path)) and '..' not in Path(path).parts
            and path!='/' and not any(c in path for c in [',','\\']) and not any(ord(c)<32 for c in path))


@contextmanager
def directory(path):
    if not canonical(path) or os.geteuid()!=0:raise ValueError('Quota workspaces require a trusted Root and canonical dedicated mount')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for name in Path(path).parts[1:]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        info=os.fstat(fd);fs=os.fstatvfs(fd)
        if info.st_uid!=0 or info.st_mode&0o777!=0o700:raise ValueError('Quota mount must be Root-owned mode 700')
        if info.st_dev==os.stat(Path(path).parent).st_dev:raise ValueError('Quota root must be a dedicated mounted filesystem')
        if fs.f_flag&os.ST_RDONLY or not fs.f_flag&os.ST_NOSUID or not fs.f_flag&os.ST_NODEV:
            raise ValueError('Quota filesystem must be writable with nosuid,nodev')
        matches=[]
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before,after=line.split(' - ',1)
            decoded=re.sub(r'\\([0-7]{3})',lambda match:chr(int(match[1],8)),before.split()[4])
            if decoded==path:matches.append(after.split()[0])
        if matches!=['ext4']:raise ValueError('Quota workspace requires one exact ext4 mount')
        yield fd
    finally:os.close(fd)


def read_marker(fd):
    source=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    with os.fdopen(source,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or info.st_mode&0o077:
            raise ValueError('Unsafe quota filesystem marker')
        raw=stream.read(256*1024+1)
    if len(raw)>256*1024:raise ValueError('Quota marker exceeds budget')
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('format')!=FORMAT:raise ValueError('Unsupported quota filesystem marker')
    return value


def verify(slot,fd):
    """Slot FD is already checked by workspace_slots; bind it to its private parent."""
    root=str(Path(slot['path']).parent)
    with directory(root) as parent:
        fs=os.fstatvfs(parent);info=os.fstat(parent);marker=read_marker(parent)
        if (info.st_dev!=slot['device'] or marker.get('filesystem_id')!=slot['filesystem_id']
            or marker.get('device')!=info.st_dev or marker.get('root_inode')!=info.st_ino
            or marker.get('bytes')!=fs.f_blocks*fs.f_frsize or marker.get('inodes')!=fs.f_files
            or (fs.f_blocks*fs.f_frsize,fs.f_files)!=(slot['filesystem_bytes'],slot['filesystem_inodes'])
            or slot not in marker.get('slots',[])):
            raise ValueError('Quota filesystem source binding differs from its registered slot')
    return quota.require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])


def requested_limits(quotas):
    if not isinstance(quotas,list) or not 1<=len(quotas)<=256:raise ValueError('Choose 1..256 explicit user quota limits')
    result=[];seen=set()
    for item in quotas:
        if not isinstance(item,dict) or set(item)!={'uid','bytes','inodes'}:raise ValueError('Quota request requires uid, bytes and inodes')
        if (type(item['uid']) is not int or not 1000<=item['uid']<2**31 or item['uid'] in seen
            or type(item['bytes']) is not int or not 16*1024**2<=item['bytes']<=1024**4 or item['bytes']%4096
            or type(item['inodes']) is not int or not 128<=item['inodes']<=10000000):raise ValueError('Invalid or duplicate user quota limits')
        seen.add(item['uid']);result.append(dict(item))
    return sorted(result,key=lambda item:item['uid'])


def prepare(path,quotas,plan=False):
    """No formatting/enable/resize. Completed preparations are idempotent; partial ones stop."""
    requested=requested_limits(quotas)
    with directory(path) as fd:
        fs=os.fstatvfs(fd);info=os.fstat(fd);names=set(os.listdir(fd))
        if INTENT in names:raise ValueError('Interrupted quota preparation remains; reconcile it before retrying')
        if MARKER in names:
            marker=read_marker(fd);slots=marker['slots']
            if [{'uid':s['quota_uid'],'bytes':s['bytes'],'inodes':s['inodes']} for s in slots]!=requested:
                raise ValueError('Existing quota workspace limits differ; refusing reconfiguration')
            from workspace_slots import validate
            policy={'slots':slots};validate(policy,host=True)
            return {'policy':policy,'already_prepared':True,'read_only_plan':plan}
        if names-{'lost+found'}:raise ValueError('Refusing to provision a filesystem containing unknown data')
        identity=uuid.uuid4().hex
        candidates=[{'quota_uid':r['uid'],'bytes':r['bytes'],'inodes':r['inodes'],'device':info.st_dev,
                     'filesystem':PROVIDER,'filesystem_id':identity,'filesystem_bytes':fs.f_blocks*fs.f_frsize,
                     'filesystem_inodes':fs.f_files} for r in requested]
        capacity(candidates)
        previous=[]
        for request in requested:
            current=quota.read(fd,request['uid']);previous.append(current)
            if current['bytes_used'] or current['inodes_used']:raise ValueError('User already owns filesystem data; refusing initial provisioning')
            if any(current[key] for key in ['byte_hard','byte_soft','inode_hard','inode_soft']):
                quota.require(fd,request['uid'],request['bytes'],request['inodes'])
        if plan:return {'read_only_plan':True,'already_prepared':False,'requested':requested,'filesystem_bytes':fs.f_blocks*fs.f_frsize,'filesystem_inodes':fs.f_files}
        intent=os.open(INTENT,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(intent,'w') as stream:
            json.dump({'format':FORMAT,'requested':requested},stream);stream.flush();os.fsync(stream.fileno())
        os.fsync(fd);slots=[]
        for request,current,candidate in zip(requested,previous,candidates):
            if not any(current[key] for key in ['byte_hard','byte_soft','inode_hard','inode_soft']):
                quota.provision(fd,request['uid'],request['bytes'],request['inodes'])
            name='slot-'+uuid.uuid4().hex[:12];os.mkdir(name,0o700,dir_fd=fd)
            entry=os.stat(name,dir_fd=fd,follow_symlinks=False)
            slots.append({**candidate,'id':name,'storage_id':uuid.uuid4().hex,'path':str(Path(path)/name),'root_inode':entry.st_ino})
        marker={'format':FORMAT,'filesystem_id':identity,'device':info.st_dev,'root_inode':info.st_ino,
                'bytes':fs.f_blocks*fs.f_frsize,'inodes':fs.f_files,'slots':slots}
        record=os.open(INTENT,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW,dir_fd=fd)
        with os.fdopen(record,'w') as stream:json.dump(marker,stream);stream.flush();os.fsync(stream.fileno())
        os.link(INTENT,MARKER,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
        os.unlink(INTENT,dir_fd=fd);os.fsync(fd)
        from workspace_slots import validate
        validate({'slots':slots},host=True)
        return {'policy':{'slots':slots},'already_prepared':False,'read_only_plan':False}

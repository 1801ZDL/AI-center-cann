"""Group-quota primitives for trusted, non-root central storage writers.

Logical owners get dedicated storage GIDs. Only the trusted service belongs to
these groups; clients/tasks never run as that UID or write this filesystem.
This module does not mount/enable quotas or implement a payload provider.
"""
import ctypes
import os
import platform
import stat

from filesystem_quota import DiskQuota,QuotaInfo,QuotaState,Q_GETINFO,Q_GETQUOTA,Q_SETQUOTA,Q_XGETQSTATV,BLOCK

GRPQUOTA=1
Q_SYNC=0x800001


def quota_call(fd,command,gid,record):
    if type(fd) is not int or fd<0 or type(gid) is not int or not 0<=gid<2**31:
        raise ValueError('Invalid group quota descriptor or GID')
    if command not in {Q_SYNC,Q_GETINFO,Q_GETQUOTA,Q_SETQUOTA,Q_XGETQSTATV,0x800003}:
        raise ValueError('Unsupported group quota operation')
    libc=ctypes.CDLL(None,use_errno=True);operation=(command<<8)|GRPQUOTA
    if hasattr(libc,'quotactl_fd'):
        call=libc.quotactl_fd;call.argtypes=[ctypes.c_int,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p];call.restype=ctypes.c_int
        result=call(fd,operation,gid,ctypes.byref(record) if record is not None else None)
    else:
        if platform.system()!='Linux' or platform.machine() not in {'x86_64','aarch64'} or ctypes.sizeof(ctypes.c_void_p)!=8:
            raise ValueError('No supported native group quotactl_fd ABI; refusing fallback')
        libc.syscall.restype=ctypes.c_long
        result=libc.syscall(ctypes.c_long(443),ctypes.c_int(fd),ctypes.c_uint(operation),ctypes.c_uint(gid),
                            ctypes.byref(record) if record is not None else ctypes.c_void_p())
    if result!=0:
        code=ctypes.get_errno();raise OSError(code,os.strerror(code))


def state(fd):
    current=QuotaState(version=1);quota_call(fd,Q_XGETQSTATV,0,current)
    if current.version!=1:raise ValueError('Unsupported group quota state layout')
    return {'accounting':bool(current.flags&4),'enforced':bool(current.flags&8),'flags':int(current.flags)}


def read(fd,gid):
    if type(gid) is not int or not 1000<=gid<2**31:raise ValueError('Storage quota requires an assigned non-system GID')
    status=state(fd)
    if not status['accounting'] or not status['enforced']:raise ValueError('Group quota accounting or enforcement is inactive')
    info=QuotaInfo();quota_call(fd,Q_GETINFO,0,info)
    value=DiskQuota();quota_call(fd,Q_GETQUOTA,gid,value)
    return {'gid':gid,'byte_hard':int(value.block_hard)*BLOCK,'byte_soft':int(value.block_soft)*BLOCK,
            'bytes_used':int(value.space_used),'inode_hard':int(value.inode_hard),'inode_soft':int(value.inode_soft),
            'inodes_used':int(value.inodes_used),'quota_flags':int(info.flags),'enforcement':status}


def require(fd,gid,byte_hard,inode_hard):
    if type(byte_hard) is not int or byte_hard<=0 or byte_hard%BLOCK or type(inode_hard) is not int or inode_hard<=0:
        raise ValueError('Group quota hard limits must be positive aligned integers')
    current=read(fd,gid)
    if (current['byte_hard']!=byte_hard or current['byte_soft'] not in {0,byte_hard}
        or current['inode_hard']!=inode_hard or current['inode_soft'] not in {0,inode_hard}):
        raise ValueError('Active group quota differs from the pinned hard limits')
    return current


def provision(fd,gid,byte_hard,inode_hard):
    if os.geteuid()!=0:raise ValueError('Group quota provisioning requires the trusted administrator')
    if type(byte_hard) is not int or byte_hard<=0 or byte_hard%BLOCK or type(inode_hard) is not int or inode_hard<=0:
        raise ValueError('Group quota hard limits must be positive aligned integers')
    previous=read(fd,gid)
    if any(previous[key] for key in ['byte_hard','byte_soft','inode_hard','inode_soft','bytes_used','inodes_used']):
        raise ValueError('Refusing to replace existing group quotas or adopt existing group data')
    record=DiskQuota(block_hard=byte_hard//BLOCK,block_soft=byte_hard//BLOCK,
                     inode_hard=inode_hard,inode_soft=inode_hard,valid=5)
    quota_call(fd,Q_SETQUOTA,gid,record)
    return require(fd,gid,byte_hard,inode_hard)


def resize(fd,gid,before,after):
    """CAS an existing assigned group; callers hold offline installation lock.

    Only hard/soft limits change. Usage, group ownership and quota enforcement
    are never reset. The journal-owning caller resumes an already applied CAS.
    """
    if os.geteuid()!=0:raise ValueError('Group quota resize requires the trusted administrator')
    for item in [before,after]:
        if (not isinstance(item,dict) or set(item)!={'bytes','inodes'} or type(item['bytes']) is not int
            or item['bytes']<=0 or item['bytes']%BLOCK or type(item['inodes']) is not int or item['inodes']<=0):
            raise ValueError('Invalid group quota resize limits')
    current=read(fd,gid)
    pair=(current['byte_hard'],current['inode_hard'])
    if pair not in {(before['bytes'],before['inodes']),(after['bytes'],after['inodes'])}:
        raise ValueError('Group quota limits changed outside this operation')
    if (current['byte_soft'] not in {0,current['byte_hard']} or current['inode_soft'] not in {0,current['inode_hard']}):
        raise ValueError('Group quota soft limits differ from the assigned contract')
    if after['bytes']<current['bytes_used'] or after['inodes']<current['inodes_used']:
        raise ValueError('Group quota resize cannot discard existing usage')
    if pair!=(after['bytes'],after['inodes']):
        record=DiskQuota(block_hard=after['bytes']//BLOCK,block_soft=after['bytes']//BLOCK,
                         inode_hard=after['inodes'],inode_soft=after['inodes'],valid=5)
        quota_call(fd,Q_SETQUOTA,gid,record)
    quota_call(fd,Q_SYNC,0,None)
    return require(fd,gid,after['bytes'],after['inodes'])


def assign_empty(fd,gid):
    """Called before any content exists; group members may set their own inode GID."""
    info=os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid()
        or info.st_mode&0o777!=0o700 or os.listdir(fd)):
        raise ValueError('Assign only a private, owned, empty storage directory')
    if type(gid) is not int or gid<1000 or gid>=2**31 or gid not in {os.getegid(),*os.getgroups()}:
        raise ValueError('Trusted service does not belong to this storage group')
    os.fchown(fd,-1,gid);os.fchmod(fd,0o2700)
    verify_directory(fd,gid);os.fsync(fd)


def verify_directory(fd,gid):
    info=os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid()
        or info.st_gid!=gid or info.st_mode&0o7777!=0o2700):
        raise ValueError('Private storage directory lacks the pinned group or SGID inheritance')

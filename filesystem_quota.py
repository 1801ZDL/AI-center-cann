"""Linux user-quota primitives for trusted, preprovisioned filesystem providers.

No path discovery, mount, automatic quota enable/disable, or statvfs fallback.
All operations address an already-verified filesystem FD. Not wired into job
admission until its workspace contract/provider is explicitly implemented.
"""
import ctypes
import errno
import os
import platform

USRQUOTA=0
Q_GETINFO=0x800005
Q_GETQUOTA=0x800007
Q_SETQUOTA=0x800008
Q_XGETQSTATV=(ord('X')<<8)|8
QIF_BLIMITS=1
QIF_ILIMITS=4
BLOCK=1024


class DiskQuota(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint64) for name in [
        'block_hard','block_soft','space_used','inode_hard','inode_soft','inodes_used','block_time','inode_time']]+[('valid',ctypes.c_uint32)]


class QuotaInfo(ctypes.Structure):
    _fields_=[('block_grace',ctypes.c_uint64),('inode_grace',ctypes.c_uint64),('flags',ctypes.c_uint32),('valid',ctypes.c_uint32)]


class QuotaFile(ctypes.Structure):
    _fields_=[('inode',ctypes.c_uint64),('blocks',ctypes.c_uint64),('extents',ctypes.c_uint32),('pad',ctypes.c_uint32)]


class QuotaState(ctypes.Structure):
    _fields_=[('version',ctypes.c_int8),('pad1',ctypes.c_uint8),('flags',ctypes.c_uint16),('incore',ctypes.c_uint32),
        ('user',QuotaFile),('group',QuotaFile),('project',QuotaFile),
        ('block_grace',ctypes.c_int32),('inode_grace',ctypes.c_int32),('rt_grace',ctypes.c_int32),
        ('block_warn',ctypes.c_uint16),('inode_warn',ctypes.c_uint16),('rt_warn',ctypes.c_uint16),
        ('pad3',ctypes.c_uint16),('pad4',ctypes.c_uint32),('pad2',ctypes.c_uint64*7)]


def quota_call(fd,command,uid,record):
    if type(fd) is not int or fd<0 or type(uid) is not int or not 0<=uid<2**31:raise ValueError('Invalid quota descriptor or UID')
    if command not in {Q_GETINFO,Q_GETQUOTA,Q_SETQUOTA,Q_XGETQSTATV,0x800003}:raise ValueError('Unsupported quota operation')
    libc=ctypes.CDLL(None,use_errno=True);value=(command<<8)|USRQUOTA
    if hasattr(libc,'quotactl_fd'):
        call=libc.quotactl_fd;call.argtypes=[ctypes.c_int,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p];call.restype=ctypes.c_int
        result=call(fd,value,uid,ctypes.byref(record) if record is not None else None)
    else:
        # Linux asm-generic and x86-64 UAPI both assign 443 to quotactl_fd.
        if platform.system()!='Linux' or platform.machine() not in {'x86_64','aarch64'} or ctypes.sizeof(ctypes.c_void_p)!=8:
            raise ValueError('No supported native quotactl_fd ABI; refusing unbounded fallback')
        libc.syscall.restype=ctypes.c_long
        result=libc.syscall(ctypes.c_long(443),ctypes.c_int(fd),ctypes.c_uint(value),ctypes.c_uint(uid),
                            ctypes.byref(record) if record is not None else ctypes.c_void_p())
    if result!=0:
        code=ctypes.get_errno();raise OSError(code,os.strerror(code))


def state(fd):
    current=QuotaState(version=1);quota_call(fd,Q_XGETQSTATV,0,current)
    if current.version!=1:raise ValueError('Unsupported quota state layout')
    return {'accounting':bool(current.flags&1),'enforced':bool(current.flags&2),'flags':int(current.flags)}


def read(fd,uid):
    if type(uid) is not int or not 1000<=uid<2**31:raise ValueError('Quota identity must be an assigned non-root UID')
    status=state(fd)
    if not status['accounting'] or not status['enforced']:raise ValueError('User quota accounting or enforcement is inactive')
    info=QuotaInfo();quota_call(fd,Q_GETINFO,0,info)
    quota=DiskQuota();quota_call(fd,Q_GETQUOTA,uid,quota)
    return {'uid':uid,'byte_hard':int(quota.block_hard)*BLOCK,'byte_soft':int(quota.block_soft)*BLOCK,
            'bytes_used':int(quota.space_used),'inode_hard':int(quota.inode_hard),'inode_soft':int(quota.inode_soft),
            'inodes_used':int(quota.inodes_used),'quota_flags':int(info.flags),'enforcement':status}


def require(fd,uid,byte_hard,inode_hard):
    if (type(byte_hard) is not int or byte_hard<=0 or byte_hard%BLOCK
        or type(inode_hard) is not int or inode_hard<=0):raise ValueError('Quota hard limits must be positive integers')
    current=read(fd,uid)
    if (current['byte_hard']!=byte_hard or current['byte_soft'] not in {0,byte_hard}
        or current['inode_hard']!=inode_hard or current['inode_soft'] not in {0,inode_hard}
        or byte_hard<=0 or inode_hard<=0):raise ValueError('Active user quota differs from the pinned hard limits')
    return current


def provision(fd,uid,byte_hard,inode_hard):
    """Administrator-only; caller must pin/verify the owned filesystem first."""
    if os.geteuid()!=0:raise PermissionError(errno.EPERM,'Only a trusted administrator may provision quotas')
    if (type(uid) is not int or not 1000<=uid<2**31 or type(byte_hard) is not int
        or byte_hard<1024**2 or byte_hard%BLOCK or type(inode_hard) is not int or inode_hard<16):
        raise ValueError('Provide a non-root UID and positive aligned byte/inode limits')
    previous=read(fd,uid)
    if any(previous[key] for key in ['byte_hard','byte_soft','inode_hard','inode_soft']):
        raise ValueError('Refusing to replace an existing quota; reconcile it explicitly')
    quota=DiskQuota(block_hard=byte_hard//BLOCK,block_soft=byte_hard//BLOCK,
                    inode_hard=inode_hard,inode_soft=inode_hard,valid=QIF_BLIMITS|QIF_ILIMITS)
    quota_call(fd,Q_SETQUOTA,uid,quota)
    return require(fd,uid,byte_hard,inode_hard)

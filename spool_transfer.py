"""Atomic, repeatable import from retained task outputs into private node spool.

Only a fixed one-file scratch directory is cleared. Committed files are verified
against the replayed export stream, never overwritten on a mismatching retry.
"""
import os
import errno
from pathlib import Path
import stat

import storage_files as fs


class SpoolIntegrityError(RuntimeError):pass


def private_directory(path):
    path=Path(path);path.mkdir(mode=0o700,exist_ok=True)
    try:fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in {errno.ELOOP,errno.ENOTDIR}:raise SpoolIntegrityError('Export scratch is not a private directory') from None
        raise
    info=os.fstat(fd)
    if info.st_uid!=os.geteuid() or info.st_mode&0o777!=0o700:
        os.close(fd);raise SpoolIntegrityError('Unsafe private export scratch directory')
    return fd


def reset_scratch(path,remove=False):
    path=Path(path);fd=private_directory(path)
    try:
        if set(os.listdir(fd))-{'partial'}:raise SpoolIntegrityError('Unknown export scratch entries remain')
        try:child=os.open('partial',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError:child=None
        if child is not None:
            try:
                info=os.fstat(child)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1
                    or info.st_dev!=os.fstat(fd).st_dev or fs.mount(child)!=fs.mount(fd)):
                    raise SpoolIntegrityError('Unknown file or mount in private export scratch')
                current=os.stat('partial',dir_fd=fd,follow_symlinks=False)
                if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise SpoolIntegrityError('Export scratch changed')
                os.unlink('partial',dir_fd=fd);os.fsync(fd)
            finally:os.close(child)
    finally:os.close(fd)
    if remove:path.rmdir()


def publish(source,target,size,scratch):
    if type(size) is not int or size<0:raise ValueError('Invalid exported size')
    target=Path(target);target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    try:existing=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:existing=None
    except OSError as error:
        if error.errno in {errno.ELOOP,errno.ENOTDIR}:raise SpoolIntegrityError('Previously collected output is not a regular owned file') from None
        raise
    if existing is not None:
        with os.fdopen(existing,'rb') as stored:
            before=os.fstat(stored.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.geteuid() or before.st_size!=size:
                raise SpoolIntegrityError('Previously collected output has an unexpected identity or size')
            remaining=size
            while remaining:
                data=source.read(min(65536,remaining))
                if not data or data!=stored.read(len(data)):raise SpoolIntegrityError('Previously collected output differs; retained work was not deleted')
                remaining-=len(data)
            if source.read(1):raise SpoolIntegrityError('Export exceeds its declared member size')
            after=os.fstat(stored.fileno())
            if any(getattr(before,key)!=getattr(after,key) for key in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):
                raise SpoolIntegrityError('Previously collected output changed during replay')
        return {'reused':True,'bytes':size}
    reset_scratch(scratch)
    temporary=Path(scratch)/'partial'
    try:
        with temporary.open('xb') as out:
            remaining=size
            while remaining:
                data=source.read(min(65536,remaining))
                if not data or len(data)>remaining:raise SpoolIntegrityError('Export ended before or exceeded its declared member size')
                out.write(data);remaining-=len(data)
            if source.read(1):raise SpoolIntegrityError('Export exceeds its declared member size')
            out.flush();os.fsync(out.fileno())
        fs.move(temporary,target)
    finally:
        reset_scratch(scratch)
    return {'reused':False,'bytes':size}

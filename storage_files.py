"""Bounded Linux FD-relative storage operations; no symlink or mount traversal."""
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

MAX_ENTRIES=10000
MAX_DEPTH=128
MAX_RECORD=8*1024**2


def error(message):
    from jobs import JobError
    return JobError(message,409)


def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def digest(value):return hashlib.sha256(encode(value)).hexdigest()


def directory(path):return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)


def mount(fd):
    for line in Path('/proc/self/fdinfo/'+str(fd)).read_text().splitlines():
        if line.startswith('mnt_id:'):return int(line.split()[1])
    raise error('Cannot verify storage mount identity')


def identity(fd):
    value=os.fstat(fd)
    return {'dev':value.st_dev,'ino':value.st_ino,'mount':mount(fd)}


def metadata(fd):
    value=os.fstat(fd)
    return {**identity(fd),'mode':value.st_mode,'size':value.st_size,'mtime':value.st_mtime_ns,'ctime':value.st_ctime_ns}


@contextmanager
def lock(runtime,name,exclusive=False):
    parent=Path(runtime)/'storage-locks'
    parent.mkdir(mode=0o700,exist_ok=True)
    info=parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise error('Unsafe storage lock directory')
    root=directory(parent)
    try:fd=os.open(name+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=root)
    finally:os.close(root)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise error('Unsafe storage lock')
        try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        except BlockingIOError:raise error('Storage is being read or maintained; retry this operation later') from None
        yield
    finally:os.close(fd)


def scan(path):
    root=directory(path)
    try:
        root_identity=identity(root);entries=[];total=0
        parent=directory(Path(path).parent)
        try:
            if identity(parent)['dev']!=root_identity['dev'] or mount(parent)!=root_identity['mount']:raise error('Storage object root is a mount boundary')
        finally:os.close(parent)
        def visit(fd,prefix,depth):
            nonlocal total
            if depth>MAX_DEPTH:raise error('Storage tree exceeds bounded maintenance depth')
            before=os.fstat(fd)
            for name in sorted(os.listdir(fd)):
                rel=prefix+'/'+name if prefix else name
                child=os.open(name,os.O_PATH|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    value=metadata(child);info=os.fstat(child)
                    if value['dev']!=root_identity['dev'] or value['mount']!=root_identity['mount']:raise error('Storage maintenance refuses nested mounts, including bind mounts')
                    if not stat.S_ISDIR(info.st_mode) and info.st_nlink!=1:raise error('Storage maintenance refuses multiply-linked files')
                    if len(entries)>=MAX_ENTRIES:raise error('Storage tree exceeds bounded maintenance entry count')
                    entries.append({'name':rel,**value})
                    if not stat.S_ISDIR(info.st_mode):total+=info.st_size
                    if stat.S_ISDIR(info.st_mode):
                        nested=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                        try:
                            if identity(nested)!=identity(child):raise error('Storage directory changed during scan')
                            visit(nested,rel,depth+1)
                        finally:os.close(nested)
                finally:os.close(child)
            after=os.fstat(fd)
            if any(getattr(before,key)!=getattr(after,key) for key in ['st_ino','st_mtime_ns','st_ctime_ns']):raise error('Storage tree changed during scan')
        visit(root,'',0)
        value={'root':root_identity,'entries':entries,'bytes':total}
        if len(encode(value))>MAX_RECORD:raise error('Storage maintenance record exceeds limit')
        return value
    finally:os.close(root)


def exists(path):
    try:Path(path).lstat();return True
    except FileNotFoundError:return False


def private_tree(root,parts):
    fd=directory(root)
    try:
        for name in parts:
            if not name or '/' in name or name in {'.','..'}:raise error('Unsafe private storage directory')
            try:os.mkdir(name,0o700,dir_fd=fd)
            except FileExistsError:pass
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            info=os.fstat(child)
            if info.st_uid!=os.geteuid() or info.st_mode&0o077:
                os.close(child);raise error('Unsafe private storage directory ownership')
            os.close(fd);fd=child
    finally:os.close(fd)


def move(source,target):
    if source==target:raise error('Storage source and destination must be distinct')
    a=directory(source.parent);b=directory(target.parent)
    try:
        if identity(a)['dev']!=identity(b)['dev'] or mount(a)!=mount(b):raise error('Storage quarantine must stay on the same filesystem/mount')
        libc=ctypes.CDLL(None,use_errno=True)
        try:rename=libc.renameat2
        except AttributeError:raise error('Linux renameat2 is required for non-overwriting quarantine') from None
        rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];rename.restype=ctypes.c_int
        if rename(a,os.fsencode(source.name),b,os.fsencode(target.name),1):
            value=ctypes.get_errno()
            if value==errno.EEXIST:raise error('Storage destination exists; nothing was overwritten')
            raise OSError(value,os.strerror(value))
        os.fsync(a);os.fsync(b)
    finally:os.close(a);os.close(b)


def purge(path,original,budget=256):
    """Delete at most budget approved entries. Pending state retains full billing."""
    if not exists(path):return True,0
    current=scan(path)
    if current['root']!=original['root']:raise error('Quarantine identity differs; preserve it for reconciliation')
    approved={item['name']:item for item in original['entries']}
    for item in current['entries']:
        expected=approved.get(item['name'])
        keys=['dev','ino','mount','mode'] if stat.S_ISDIR(item['mode']) else ['dev','ino','mount','mode','size','mtime','ctime']
        if not expected or any(item[key]!=expected[key] for key in keys):raise error('Quarantine content changed; purge was stopped')
    remaining=[budget];removed=[0];root=directory(path)
    try:
        if identity(root)!=original['root']:raise error('Quarantine changed before purge')
        def visit(fd,prefix):
            for name in sorted(os.listdir(fd)):
                if remaining[0]<=0:return False
                rel=prefix+'/'+name if prefix else name;expected=approved.get(rel)
                child=os.open(name,os.O_PATH|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    actual=metadata(child)
                    keys=['dev','ino','mount','mode'] if expected and stat.S_ISDIR(expected['mode']) else ['dev','ino','mount','mode','size','mtime','ctime']
                    if not expected or any(actual[key]!=expected[key] for key in keys):raise error('Storage entry changed before removal')
                    if stat.S_ISDIR(actual['mode']):
                        nested=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                        try:
                            if identity(nested)!=identity(child):raise error('Storage directory changed before removal')
                            if not visit(nested,rel):return False
                        finally:os.close(nested)
                        if remaining[0]<=0:return False
                        os.rmdir(name,dir_fd=fd)
                    else:os.unlink(name,dir_fd=fd)
                    remaining[0]-=1;removed[0]+=1
                finally:os.close(child)
            return True
        empty=visit(root,'')
        if empty and remaining[0]>0:
            parent=directory(path.parent)
            try:
                check=os.open(path.name,os.O_PATH|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    if identity(check)!=original['root']:raise error('Quarantine root changed before removal')
                finally:os.close(check)
                os.rmdir(path.name,dir_fd=parent);os.fsync(parent);removed[0]+=1
                return True,removed[0]
            finally:os.close(parent)
        os.fsync(root);return False,removed[0]
    finally:os.close(root)

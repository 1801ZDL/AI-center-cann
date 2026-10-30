"""Durable bounded chunks and path-free reporting of exhausted storage.

Callers serialize by the existing SQLite write transaction. Only the database's
committed prefix is authoritative; a failed/uncertain write never advances it.
"""
import errno
import os
from pathlib import Path
import sqlite3
import stat


def exhausted(error):
    if isinstance(error, OSError):
        return error.errno in {errno.ENOSPC, errno.EDQUOT}
    if isinstance(error, sqlite3.Error):
        code = getattr(error, 'sqlite_errorcode', None)
        if isinstance(code, int):
            return code & 255 == sqlite3.SQLITE_FULL
        # CPython 3.10 does not expose sqlite_errorcode. Match SQLite's exact
        # canonical message; busy, permissions and corruption are not disk full.
        return isinstance(error, sqlite3.OperationalError) and str(error) == 'database or disk is full'
    return False


def response():
    return {'error': 'storage_unavailable', 'retryable': True,
            'message': 'Storage capacity or quota exhausted. Inspect or retry the original operation after restoring capacity; this response does not authorize resubmitting computation or releasing leases.'}


def sync_tree(path, root):
    """Persist directory entries from the changed leaf up to the payload root.

    The caller holds its SQLite write transaction. Use no-follow directory FDs,
    including for ancestors of the configured root; never walk a user symlink.
    Existing directories are synced too, so retrying after an uncertain failure
    cannot accidentally skip an entry created by the preceding attempt.
    """
    from jobs import JobError
    path=Path(path);root=Path(root)
    if not root.is_absolute() or '..' in root.parts or '..' in path.parts:
        raise JobError('Unsafe durable storage root',409)
    try:parts=path.relative_to(root).parts
    except ValueError:raise JobError('Storage path is outside its payload root',409) from None
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);handles=[]
    try:
        for name in root.parts[1:]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        handles.append(fd);fd=None
        for name in parts:
            handles.append(os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=handles[-1]))
        for directory in reversed(handles):os.fsync(directory)
    finally:
        if fd is not None:os.close(fd)
        for directory in reversed(handles):os.close(directory)


def write_chunk(path, committed, data, *, root):
    from jobs import JobError
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        item = os.fstat(fd)
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            raise JobError('Unsafe upload file', 409)
        if item.st_size < committed:
            raise JobError('Stored upload is shorter than committed state; manual recovery required', 409)
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, committed)
        os.lseek(fd, committed, os.SEEK_SET)
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, 'Upload write made no progress')
                remaining = remaining[written:]
            os.fsync(fd)
        except OSError as error:
            if exhausted(error):
                # Never remove committed bytes. Shrinking may itself fail on an
                # exhausted metadata device; the next retry still truncates to
                # the unchanged ledger offset before appending anything.
                try:
                    os.ftruncate(fd, committed)
                    os.fsync(fd)
                except OSError:
                    pass
            raise
    finally:
        os.close(fd)
    sync_tree(path.parent,root)

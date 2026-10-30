"""Shared state users exclude offline backup/activation; the descriptor owns the lock."""
import fcntl
import os
from pathlib import Path


def state_lock(directory,exclusive=False,inheritable=False):
    path=Path(directory)/'.state.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    file=os.fdopen(fd,'r+')
    try:
        fcntl.flock(file,fcntl.LOCK_NB|(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH))
        os.set_inheritable(file.fileno(),inheritable)
        return file
    except BaseException:
        file.close()
        raise RuntimeError('State is in use or under offline maintenance') from None

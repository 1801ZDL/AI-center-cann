"""Read-only system-manager prerequisites; never loads, reloads or starts a unit."""
import os
from pathlib import Path
import stat

import unit_manager
from lab_client import now


def native_binary(path):
    """Reject compatibility scripts without executing them, including --version."""
    path=Path(path)
    if not path.is_absolute():raise ValueError('Expected an absolute executable')
    path=path.resolve(strict=True)
    for parent in [path.parent,*path.parent.parents]:
        info=parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('Executable ancestry is not protected')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022
            or not info.st_mode&0o111 or source.read(4)!=b'\x7fELF'):
            raise ValueError('Expected a protected Root-owned native ELF executable')
    return {'path':str(path),'native_elf':True,'root_owned':True}


def pid1():
    name=Path('/proc/1/comm').read_text().strip()
    executable=os.readlink('/proc/1/exe')
    if name!='systemd' or Path(executable).name!='systemd':
        raise ValueError('PID1 is not the native systemd system manager')
    return native_binary(executable)


def cgroups():
    with Path('/proc/self/mountinfo').open('rb') as source:text=source.read(256*1024+1)
    if len(text)>256*1024:raise ValueError('Mount inventory exceeds inspection limit')
    mounts=[]
    for line in text.decode().splitlines():
        before,after=line.split(' - ',1);fields=before.split();kind=after.split()[0]
        if fields[4]=='/sys/fs/cgroup' and kind in {'cgroup','cgroup2'}:
            mounts.append({'version':2 if kind=='cgroup2' else 1,
                           'mount_read_only':'ro' in fields[5].split(',')})
    if len(mounts)!=1:raise ValueError('No single recognized cgroup mount at /sys/fs/cgroup')
    return {**mounts[0],'delegation_verified':False,'writability_tested':False,
            'note':'Read-only mount is an observation, not proof of delegation or a host service failure'}


def assess():
    checks=[]
    def inspect(key,fn,required=True):
        try:
            detail=fn();checks.append({'id':key,'status':'passed','required':required,'detail':detail})
            return True
        except (OSError,ValueError,RuntimeError):
            checks.append({'id':key,'status':'unavailable','required':required,
                           'reason':'Prerequisite could not be verified; no configuration was changed'})
            return False
    has_pid1=inspect('native_systemd_pid1',pid1)
    has_busctl=inspect('native_busctl',lambda:native_binary('/usr/bin/busctl'))
    def native_library():
        from unit_bus_pin import library_file
        return {'path':str(library_file()),'root_owned':True,'native_elf':True,'loaded_or_connected':False}
    inspect('native_libsystemd',native_library)
    if has_pid1 and has_busctl:
        inspect('system_manager_identity',lambda:unit_manager.Bus().anchor())
    else:
        checks.append({'id':'system_manager_identity','status':'not_checked','required':True,
                       'reason':'Native PID1 and busctl are required before contacting the system bus'})
    inspect('cgroup_mount',cgroups,required=False)
    blockers=[v['id'] for v in checks if v['required'] and v['status']!='passed']
    return {'format':'ascend-lab-systemd-preflight/v1','checked_at':now(),'read_only':True,
            'prerequisites_ready':not blockers,'blockers':blockers,'checks':checks,
            'unit_configuration_verified':False,'service_start_verified':False,
            'cgroup_delegation_verified':False,'production_ready':False,
            'scope':'Local system-manager prerequisites only. No unit load/reload/start, account, mount, device or Docker operation.'}

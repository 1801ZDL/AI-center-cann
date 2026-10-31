"""Trusted native-controller launcher. Root only until namespace/UID setup.

No task arguments, plugins or model code run before the privilege drop. A Root
record and lifetime flock outside control storage preserve exact stop identity.
"""
import ctypes
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
import uuid

from control_filesystem import Filesystem,Unavailable,protected,HANDLE,ORIGIN_FD,NAMESPACE_FD,LOCK_FD,mount_id


def publish(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as file:
            file.write((json.dumps(value,sort_keys=True)+'\n').encode());os.fchmod(file.fileno(),0o444);file.flush();os.fsync(file.fileno())
        os.replace(temporary,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if temporary.exists():temporary.unlink()


def context(meta):
    from control_storage import DEFAULT
    return Filesystem(meta['state_dir'],{'scheduler':{'control_filesystem':meta.get('control_filesystem'),
        'payload_storage':meta.get('payload_storage'),'local_enabled':False,'control_storage':dict(DEFAULT)}},uid=meta['uid'])


def initialize(prefix,meta):
    publish(Path(prefix)/HANDLE,{'format':'ascend-lab-native-process/v1','phase':'never_started',
        'uid':meta['uid'],'gid':meta['gid'],'state_dir':meta['state_dir'],'release_id':meta['release_id']})


def alive(saved):
    from jobs import identity
    current=identity(saved.get('pid',-1))
    return bool(current and current['state']!='Z' and all(current[k]==saved.get(k) for k in ['pid','start_ticks','boot_id']))


def namespaces():
    libc=ctypes.CDLL(None,use_errno=True);libc.unshare.argtypes=[ctypes.c_int]
    libc.mount.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_void_p]
    if libc.unshare(0x20000) or libc.mount(None,b'/',None,16384|262144,None):
        raise OSError(ctypes.get_errno(),'Cannot create private controller mount namespace')


def probe():
    # Execute only this fixed setup in an isolated child during read-only doctor.
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise ValueError('Native controller requires safe Linux pidfd process control')
    result=subprocess.run(['/usr/bin/python3','-I','-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from control_process import namespaces;namespaces()',str(Path(__file__).resolve().parent)],
        capture_output=True,text=True,timeout=10)
    if result.returncode:raise ValueError('Native controller needs trusted Root mount-namespace setup')
    return {'private_mount_namespace':True,'root_only_initialization':True,'probe_created_no_files':True}


def process_lock(prefix):
    import deployment as deploy
    fd=os.open(Path(prefix)/'native-control.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        item=os.fstat(fd)
        if item.st_uid!=0 or not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or stat.S_IMODE(item.st_mode)!=0o600:
            raise deploy.DeployError('Unsafe native controller process lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise deploy.DeployError('A native controller process or maintenance operation owns this installation') from None
        return fd
    except BaseException:os.close(fd);raise


@contextmanager
def offline(prefix,meta):
    if not meta.get('control_filesystem'):
        yield;return
    import deployment as deploy
    deploy.require_root();fd=process_lock(prefix)
    try:
        if alive(protected(Path(prefix)/HANDLE)):
            raise deploy.DeployError('A protected native controller is still alive; no maintenance or handle replacement permitted')
        yield
    finally:os.close(fd)


def run(prefix):
    import deployment as deploy
    from jobs import identity
    deploy.require_root();prefix,meta,release=deploy.verify_code(prefix)
    if meta['role']!='controller' or not meta.get('control_filesystem'):raise deploy.DeployError('Native launcher requires a bound controller')
    context(meta).check()
    lock=process_lock(prefix)
    previous=protected(prefix/HANDLE)
    if alive(previous) and previous.get('pid')!=os.getpid():raise deploy.DeployError('The previous protected native process is still alive')
    origin=os.open(Path(meta['control_filesystem']['path']).parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    namespace=os.open('/proc/self/ns/mnt',os.O_RDONLY)
    info=os.fstat(origin);origin_record={'device':info.st_dev,'inode':info.st_ino,'mount_id':mount_id(origin)}
    namespaces()
    for fd,key in [(origin,ORIGIN_FD),(namespace,NAMESPACE_FD),(lock,LOCK_FD)]:
        os.set_inheritable(fd,True);os.environ[key]=str(fd)
    value={**identity(os.getpid()),'format':'ascend-lab-native-process/v1','phase':'issued','uid':meta['uid'],
        'gid':meta['gid'],'state_dir':meta['state_dir'],'release_id':meta['release_id'],'started_epoch':time.time(),
        'origin_parent':origin_record,'origin_namespace':os.fstat(namespace).st_ino,
        'private_namespace':os.stat('/proc/self/ns/mnt').st_ino,'manager_invocation_id':os.environ.get('INVOCATION_ID')}
    publish(prefix/HANDLE,value)
    from payload_groups import gids
    os.setgroups(gids(meta.get('payload_storage')));os.setgid(meta['gid']);os.setuid(meta['uid']);os.umask(0o077)
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    os.environ.update(context(meta).temporary_environment())
    # Root is gone before loading private service configuration or user state.
    deploy.run_service(prefix)


def status(prefix):
    import deployment as deploy
    prefix,meta,_=deploy.load_installation(prefix)
    saved=protected(prefix/HANDLE)
    if saved.get('format')!='ascend-lab-native-process/v1':raise deploy.DeployError('Invalid protected native process handle')
    healthy=True
    try:context(meta).check()
    except Unavailable:healthy=False
    result={'role':meta['role'],'running':False,'ready':False,'pid':None,'state_dir':meta['state_dir'],
            'release_id':meta['release_id'],'control_filesystem_available':healthy,'signal_verified':False}
    if saved.get('phase')=='never_started':return result
    if saved.get('phase')!='issued' or not re.fullmatch('[0-9a-f]{64}',str(saved.get('release_id',''))):
        raise deploy.DeployError('Native process handle has an unknown phase')
    # Same PID/start/boot may still be the transient Root launcher before drop.
    live=alive(saved)
    if not live:return result
    try:
        values=(Path('/proc')/str(saved['pid'])/'cmdline').read_bytes().split(b'\0')
        status_lines=dict(x.split(':',1) for x in (Path('/proc')/str(saved['pid'])/'status').read_text().splitlines() if ':' in x)
        actual_uid=int(status_lines['Uid'].split()[0])
    except OSError:
        return {**result,'running':True,'pid':saved['pid'],'reason':'Known native process inspection unavailable; no replacement permitted'}
    # SIGTERM/exec may finish between the first identity sample and proc reads.
    # An empty zombie cmdline is a confirmed end, not an unexpected live task.
    if not alive(saved):return result
    if not any(values):
        return {**result,'running':True,'pid':saved['pid'],
                'reason':'Original process command is transitioning; no signal or replacement permitted until verified'}
    launcher=(str(prefix/'releases'/saved['release_id']/'servicectl').encode() in values
              and b'native-run' in values and str(prefix).encode() in values
              and actual_uid in {0,meta['uid']} and saved.get('uid')==meta['uid'])
    service=(str(prefix/'releases'/saved['release_id']/'server.py').encode() in values
             and str(saved.get('state_dir')).encode() in values and actual_uid==saved.get('uid')==meta['uid'])
    if not (launcher or service):raise deploy.DeployError('Protected native PID exists with an unexpected identity; no signal or replacement permitted')
    result.update(running=True,pid=saved['pid'],release_id=saved['release_id'],initializing=launcher,signal_verified=True)
    if service and healthy:
        from lab_client import read_json
        ready=read_json(Path(meta['state_dir'])/'server-ready.json') or {}
        result['ready']=all(ready.get(k)==saved.get(k) for k in ['pid','start_ticks','boot_id'])
    return result


def start(prefix):
    import deployment as deploy
    deploy.require_root();current=status(prefix)
    if current['running']:return current
    prefix,meta,release=deploy._service_verify(prefix)
    log=os.open(Path(meta['state_dir'])/'bootstrap.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    os.fchown(log,meta['uid'],meta['gid'])
    with os.fdopen(log,'ab') as output:
        child=subprocess.Popen(['/usr/bin/python3','-I','-B',str(release/'servicectl'),'native-run','--prefix',str(prefix)],
            stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True,
            env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    until=time.monotonic()+20
    while time.monotonic()<until:
        if child.poll() is not None:raise deploy.DeployError('Native launcher ended; inspect its private bootstrap log and original handle')
        current=status(prefix)
        if current['ready']:return current
        time.sleep(.1)
    return current


def stop(prefix):
    import deployment as deploy
    deploy.require_root();prefix,meta,_=deploy.verify_code(prefix);current=status(prefix)
    if not current['running']:return current
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise deploy.DeployError('Native controller stop requires Linux pidfd support; preserve its original process handle')
    saved=protected(prefix/HANDLE)
    if saved.get('pid')!=current['pid']:raise deploy.DeployError('Protected native process changed during stop readback; no signal sent')
    waiting=time.monotonic()+20
    while current['running'] and not current['signal_verified']:
        if time.monotonic()>=waiting:raise deploy.DeployError('Original native process identity is unavailable; preserve its handle and retry the same stop')
        time.sleep(.05)
        if protected(prefix/HANDLE)!=saved:raise deploy.DeployError('Protected native process was replaced during readback; no replacement signal sent')
        current=status(prefix)
    if not current['running']:return current
    if current['pid']!=saved['pid'] or protected(prefix/HANDLE)!=saved:
        raise deploy.DeployError('Protected native process changed before stop; no replacement signal sent')
    try:fd=os.pidfd_open(current['pid'])
    except ProcessLookupError:return status(prefix)
    try:
        if not alive(saved):raise deploy.DeployError('Native process changed before stop')
        try:signal.pidfd_send_signal(fd,signal.SIGTERM)
        except PermissionError:
            # The trusted installer/unit may deliberately lack CAP_KILL after
            # the target drops UID. Ask that exact service identity to signal
            # the same pidfd; never widen Root capabilities or use a bare PID.
            def drop():os.setgroups([]);os.setgid(meta['gid']);os.setuid(meta['uid'])
            code='import signal,sys;signal.pidfd_send_signal(int(sys.argv[1]),signal.SIGTERM)'
            result=subprocess.run(['/usr/bin/python3','-I','-c',code,str(fd)],pass_fds=(fd,),
                preexec_fn=drop,capture_output=True,timeout=5)
            if result.returncode and alive(saved):raise deploy.DeployError('Original native service identity could not request its own stop; preserve its handle')
        except ProcessLookupError:pass
    finally:os.close(fd)
    until=time.monotonic()+20
    while time.monotonic()<until:
        if protected(prefix/HANDLE)!=saved:raise deploy.DeployError('Original controller stop observed a replacement; no replacement signal sent')
        current=status(prefix)
        if not current['running']:return current
        time.sleep(.1)
    raise deploy.DeployError('Original native controller stop is pending; no force kill or replacement')

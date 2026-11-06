"""Temporary native system-bus RefUnit lease; no job or service operations.

The helper has one fixed method and one stdin EOF lifetime. No shell, arbitrary
bus address, method, remote endpoint or user bus is accepted.
"""
import ctypes
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import sysconfig
import time


class PinError(ValueError):pass
class PinMissing(PinError):pass


def library_file():
    multi=sysconfig.get_config_var('MULTIARCH') or ''
    if multi and not re.fullmatch('[A-Za-z0-9_-]+',multi):raise PinError('Unsupported native library architecture')
    choices=([Path('/usr/lib')/multi/'libsystemd.so.0',Path('/lib')/multi/'libsystemd.so.0'] if multi else [])
    choices.extend(Path(root)/'libsystemd.so.0' for root in ['/usr/lib64','/lib64','/usr/lib','/lib'])
    for candidate in choices:
        try:
            path=candidate.resolve(strict=True)
            for parent in [path.parent,*path.parent.parents]:
                info=parent.stat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise PinError('Native library ancestry is not protected')
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as source:
                info=os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or source.read(4)!=b'\x7fELF':
                    raise PinError('Native library must be a protected Root-owned ELF file')
            return path
        except FileNotFoundError:continue
    raise PinError('Protected native libsystemd is unavailable')


def validate(value):
    if (not isinstance(value,dict) or set(value)!={'unit','owner','bus_id','pid1'}
        or not isinstance(value['unit'],str) or value['unit'] not in {'ascend-lab-controller.service','ascend-lab-node.service'}
        or not re.fullmatch(r':[0-9]+\.[0-9]+',str(value['owner']))
        or not re.fullmatch('[0-9a-f]{32}',str(value['bus_id']))
        or not isinstance(value['pid1'],dict) or set(value['pid1'])!={'pid','uid','start_ticks','boot_id'}
        or type(value['pid1']['pid']) is not int or value['pid1']['pid']!=1
        or type(value['pid1']['uid']) is not int or value['pid1']['uid']!=0
        or type(value['pid1']['start_ticks']) is not int or value['pid1']['start_ticks']<=0
        or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',str(value['pid1']['boot_id']))):
        raise PinError('Invalid fixed unit/reference identity')


class Error(ctypes.Structure):
    _fields_=[('name',ctypes.c_char_p),('message',ctypes.c_char_p),('_need_free',ctypes.c_int)]


def pid1_matches(value):
    fields=Path('/proc/1/stat').read_text().rsplit(')',1)[1].split()
    return (int(fields[19])==value['start_ticks'] and Path('/proc/1').stat().st_uid==0
            and Path('/proc/sys/kernel/random/boot_id').read_text().strip()==value['boot_id'])


def helper_file():
    helper=Path(__file__).resolve()
    for parent in [helper.parent,*helper.parent.parents]:
        info=parent.stat()
        if info.st_uid!=0 or info.st_mode&0o022:raise PinError('Use the protected installed unit reference helper')
    info=helper.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise PinError('Unit reference helper must be Root-owned')
    return helper


def retain(value):
    """Called only in the isolated helper process with a sanitized environment."""
    validate(value)
    if any(k.startswith(('DBUS_','LD_','PYTHON')) for k in os.environ):raise PinError('Inherited bus/loader configuration is forbidden')
    path=library_file();lib=ctypes.CDLL(str(path),mode=os.RTLD_LOCAL|os.RTLD_NOW)
    pointer=ctypes.c_void_p
    lib.sd_bus_open_system.argtypes=[ctypes.POINTER(pointer)];lib.sd_bus_open_system.restype=ctypes.c_int
    lib.sd_bus_set_method_call_timeout.argtypes=[pointer,ctypes.c_uint64];lib.sd_bus_set_method_call_timeout.restype=ctypes.c_int
    lib.sd_bus_call_method.argtypes=[pointer,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.POINTER(Error),ctypes.POINTER(pointer),ctypes.c_char_p]
    lib.sd_bus_call_method.restype=ctypes.c_int
    lib.sd_bus_message_get_signature.argtypes=[pointer,ctypes.c_int];lib.sd_bus_message_get_signature.restype=ctypes.c_char_p
    lib.sd_bus_message_read_basic.argtypes=[pointer,ctypes.c_char,ctypes.c_void_p];lib.sd_bus_message_read_basic.restype=ctypes.c_int
    lib.sd_bus_message_unref.argtypes=[pointer];lib.sd_bus_message_unref.restype=pointer
    lib.sd_bus_flush_close_unref.argtypes=[pointer];lib.sd_bus_flush_close_unref.restype=pointer
    lib.sd_bus_error_free.argtypes=[ctypes.POINTER(Error)];lib.sd_bus_error_free.restype=None
    bus=pointer()
    if lib.sd_bus_open_system(ctypes.byref(bus))<0:raise PinError('Native system bus is unavailable')
    try:
        if lib.sd_bus_set_method_call_timeout(bus,8*1000000)<0:raise PinError('Cannot bound the native method deadline')
        def call(destination,path,interface,method,arg=None,reply=''):
            error=Error();message=pointer()
            try:
                fixed=[bus,destination.encode(),path.encode(),interface.encode(),method.encode(),ctypes.byref(error),ctypes.byref(message),b's' if arg is not None else b'']
                result=lib.sd_bus_call_method(*fixed,*([ctypes.c_char_p(arg.encode())] if arg is not None else []))
                if result<0:
                    if error.name==b'org.freedesktop.systemd1.NoSuchUnit':raise PinMissing('Selected unit cannot be referenced')
                    denied=error.name in {b'org.freedesktop.DBus.Error.AccessDenied',b'org.freedesktop.DBus.Error.AuthFailed'}
                    raise PinError('Native unit-reference '+method+(' denied' if denied else ' rejected'))
                if lib.sd_bus_message_get_signature(message,1)!=(reply.encode()):raise PinError('Native unit-reference reply signature differs')
                if not reply:return None
                data=ctypes.c_char_p() if reply=='s' else ctypes.c_uint32()
                if lib.sd_bus_message_read_basic(message,reply.encode(),ctypes.byref(data))<=0:raise PinError('Native unit-reference reply is incomplete')
                return data.value.decode() if reply=='s' else data.value
            finally:
                if message:lib.sd_bus_message_unref(message)
                lib.sd_bus_error_free(ctypes.byref(error))
        dbus='org.freedesktop.DBus';obj='/org/freedesktop/DBus'
        if call(dbus,obj,dbus,'GetId',reply='s')!=value['bus_id']:raise PinError('System-bus identity changed before unit reference')
        if call(dbus,obj,dbus,'GetNameOwner','org.freedesktop.systemd1','s')!=value['owner']:raise PinError('Manager owner changed before unit reference')
        if call(dbus,obj,dbus,'GetConnectionUnixUser',value['owner'],'u')!=0 or call(dbus,obj,dbus,'GetConnectionUnixProcessID',value['owner'],'u')!=1:
            raise PinError('Unit reference requires the Root PID1 manager')
        if not pid1_matches(value['pid1']):
            raise PinError('PID1 process identity changed before unit reference')
        call(value['owner'],'/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager','RefUnit',value['unit'])
        print(json.dumps({'ready':True,'unit':value['unit'],'owner':value['owner'],'bus_id':value['bus_id']}),flush=True)
        sys.stdin.buffer.read(1) # EOF only; no commands or renewal protocol
    finally:
        lib.sd_bus_flush_close_unref(bus) # disconnect automatically drops the reference


class Hold:
    def __init__(self,unit,anchor):
        self.value={'unit':unit,**{key:anchor[key] for key in ['owner','bus_id','pid1']}};validate(self.value);self.process=None

    def _close(self):
        if self.process is None:return
        if self.process.stdin:self.process.stdin.close()
        try:self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
        if self.process.stdout:self.process.stdout.close()

    def __enter__(self):
        helper=helper_file()
        try:
            self.process=subprocess.Popen(['/usr/bin/python3','-I','-B',str(helper),json.dumps(self.value,separators=(',',':'))],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},cwd='/',close_fds=True)
            data=b'';deadline=time.monotonic()+10
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout,selectors.EVENT_READ)
                while b'\n' not in data:
                    remaining=deadline-time.monotonic()
                    if remaining<=0 or not selector.select(remaining):raise PinError('Native unit reference did not become ready')
                    block=os.read(self.process.stdout.fileno(),1025-len(data))
                    if not block:raise PinError('Native unit reference closed before readiness')
                    data+=block
                    if len(data)>1024:raise PinError('Native unit reference exceeded its output contract')
            value=json.loads(data)
            if value=={'ready':False,'reason':'missing'}:raise PinMissing('Selected unit cannot be referenced')
            if (isinstance(value,dict) and set(value)=={'ready','reason','detail'} and value['ready'] is False
                and value['reason']=='unavailable' and isinstance(value['detail'],str) and len(value['detail'])<=200
                and re.fullmatch('[A-Za-z0-9 ._-]+',value['detail'])):
                raise PinError(value['detail'])
            expected={'ready':True,**{key:self.value[key] for key in ['unit','owner','bus_id']}}
            if value!=expected:raise PinError('Native unit-reference identity could not be confirmed')
            return self
        except BaseException:self._close();raise

    def __exit__(self,kind,error,traceback):
        lost=self.process.poll() is not None
        self._close()
        if kind is None and lost:raise PinError('Native unit reference was lost during readback')
        return False


if __name__=='__main__':
    signal.alarm(120)
    try:
        if len(sys.argv)!=2 or len(sys.argv[1])>2048:raise PinError('Expected one bounded reference identity')
        retain(json.loads(sys.argv[1]))
    except PinMissing:
        print(json.dumps({'ready':False,'reason':'missing'}),flush=True);raise SystemExit(2)
    except (OSError,ValueError,RuntimeError) as error:
        detail=str(error) if isinstance(error,PinError) else type(error).__name__
        if not re.fullmatch('[A-Za-z0-9 ._-]+',detail) or len(detail)>200:detail='Native reference unavailable'
        print(json.dumps({'ready':False,'reason':'unavailable','detail':detail}),flush=True);raise SystemExit(2)

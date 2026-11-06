"""Explicit local rootful Docker transport and a pinned daemon identity.

Read-only discovery never pulls images, starts containers or changes contexts.
Every later operation checks the same daemon and overrides ambient CLI routing.
"""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess

HARD_LIMITS=('MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','CPUSet','PidsLimit')
CAPABILITY='pinned-docker-runtime-v1'


class DockerRuntimeError(RuntimeError):pass


def binary():
    for name in ['/usr/bin/docker','/usr/local/bin/docker']:
        try:
            path=Path(name).resolve(strict=True);info=path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or not os.access(path,os.X_OK):continue
            if any(p.stat().st_uid!=0 or p.stat().st_mode&0o022 for p in path.parents):continue
            return str(path)
        except OSError:continue
    raise DockerRuntimeError('A protected system Docker CLI is required')


def endpoint(value):
    if not isinstance(value,str) or not value.startswith('unix:///') or len(value)>1024:
        raise DockerRuntimeError('Managed nodes require a local Unix Docker socket; remote Docker endpoints are unsupported')
    path=value[7:]
    if path!=str(Path(path)) or '..' in Path(path).parts or any(ord(c)<32 for c in path) or any(c in path for c in '?&#'):
        raise DockerRuntimeError('Docker Unix socket path must be canonical')
    return value


def selected_endpoint(environ=None):
    environ=os.environ if environ is None else environ
    # Docker documents DOCKER_CONTEXT as overriding DOCKER_HOST.
    context=environ.get('DOCKER_CONTEXT')
    if not context and environ.get('DOCKER_HOST'):return endpoint(environ['DOCKER_HOST'])
    command=[binary(),'context','inspect','--format','{{(index .Endpoints "docker").Host}}']
    if context:
        if len(context)>128 or any(ord(c)<32 for c in context):raise DockerRuntimeError('Invalid Docker context selection')
        command+=['--',context]
    try:
        result=subprocess.run(command,env=dict(environ),stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=5)
        if result.returncode:raise DockerRuntimeError('Cannot inspect the selected Docker context')
        return endpoint(result.stdout.strip())
    except (OSError,subprocess.SubprocessError):raise DockerRuntimeError('Docker context discovery is unavailable') from None


def validate_binding(binding):
    if (not isinstance(binding,dict) or set(binding)!={'endpoint','daemon_id'}
        or not isinstance(binding.get('daemon_id'),str) or not re.fullmatch(r'[a-zA-Z0-9:-]{1,128}',binding['daemon_id'])):
        raise DockerRuntimeError('A complete pinned Docker daemon binding is required')
    endpoint(binding['endpoint']);return binding


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self,path):
        super().__init__('localhost',timeout=5);self.path=path

    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def information(address,route='/info'):
    path=endpoint(address)[7:]
    connection=None
    try:
        info=Path(path).stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o002:
            raise DockerRuntimeError('Managed Docker socket must be root-owned and not world writable')
        connection=UnixHTTP(path);connection.request('GET',route,headers={'Connection':'close'})
        response=connection.getresponse();raw=response.read(2*1024**2+1)
        if response.status!=200 or len(raw)>2*1024**2:raise DockerRuntimeError('Local Docker daemon did not return a bounded inventory')
        value=json.loads(raw)
        if not isinstance(value,dict):raise DockerRuntimeError('Invalid Docker daemon inventory')
        return value
    except DockerRuntimeError:raise
    except (OSError,ValueError,http.client.HTTPException):
        raise DockerRuntimeError('Cannot verify the local Docker daemon; existing attempts remain unconfirmed') from None
    finally:
        if connection is not None:connection.close()


def limits(info):
    missing=[name for name in HARD_LIMITS if info.get(name) is not True]
    if missing:raise DockerRuntimeError('Docker runtime cannot enforce required limits: '+', '.join(missing))
    return {name:True for name in HARD_LIMITS}


def discover(expected=None):
    address=validate_binding(expected)['endpoint'] if expected else selected_endpoint()
    info=information(address)
    binding={'endpoint':address,'daemon_id':info.get('ID')};validate_binding(binding)
    if expected and binding!=expected:raise DockerRuntimeError('Docker daemon identity changed; reconcile existing attempts before changing the binding')
    if any('rootless' in str(option).lower() for option in info.get('SecurityOptions',[])):
        raise DockerRuntimeError('This managed backend requires a local rootful Docker daemon')
    limits(info)
    return binding,info


def image_available(binding,image):
    validate_binding(binding)
    if not isinstance(image,str) or not re.fullmatch('sha256:[0-9a-f]{64}',image):raise DockerRuntimeError('An immutable image is required')
    if information(binding['endpoint'],'/images/'+image+'/json').get('Id')!=image:
        raise DockerRuntimeError('The enrolled immutable image is unavailable')


def command(binding,argv):
    validate_binding(binding)
    info=information(binding['endpoint'])
    if info.get('ID')!=binding['daemon_id']:
        raise DockerRuntimeError('Docker daemon identity changed; resources remain held pending reconciliation')
    limits(info)
    if not isinstance(argv,list) or not argv or argv[0]!='docker':raise DockerRuntimeError('Expected a fixed Docker command')
    # A nonexistent child of trusted code provides an empty CLI configuration.
    # Refuse an existing object rather than reading personal headers/proxy/auth.
    empty=Path(__file__).resolve().parent/'_docker_cli_empty'
    if empty.exists() or empty.is_symlink():raise DockerRuntimeError('Docker empty-config path unexpectedly exists; do not reuse personal Docker configuration')
    env={k:v for k,v in os.environ.items() if not k.startswith('DOCKER_')}
    return [binary(),'--config',str(empty),'--host',binding['endpoint'],*argv[1:]],env

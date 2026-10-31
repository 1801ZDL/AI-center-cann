"""Portable stdlib client. No imports from, or access to, controller state."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import re
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid
import hashlib

ROOT = Path(__file__).resolve().parent
WORKFLOW_TEMPLATES={'accuracy_v1','performance_v1','simulation_v1','build_v1','ir_v1'}


class ClientError(ValueError):
    pass


class ClientHTTPError(ClientError):
    """A received HTTP status, without retaining remote bodies or credentials."""
    def __init__(self, status):
        self.status = status
        super().__init__('Controller HTTP ' + str(status))


class ClientUnavailable(ClientError):
    """No trustworthy response; retry must retain the original operation identity."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write('\n'); file.flush(); os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def resolve_ca(config,profile_path):
    ca=config.get('ca_file')
    if ca is None:return config
    if not isinstance(ca,str) or not ca or '\0' in ca:raise ClientError('Invalid certificate authority file')
    path=Path(ca)
    if not path.is_absolute():
        if '..' in path.parts or ca!=str(path):raise ClientError('Relative certificate authority paths must stay within the profile directory')
        path=Path(profile_path).expanduser().absolute().parent/path
    return {**config,'ca_file':str(path)}


def authority_bytes(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid not in {0,os.geteuid()}
            or info.st_mode&0o022 or info.st_size>256*1024):raise ClientError('Authority must be a protected, bounded regular PEM file')
        data=source.read(256*1024+1)
    if not data or len(data)>256*1024:raise ClientError('Invalid certificate authority size')
    try:ssl.create_default_context(cadata=data.decode('ascii'))
    except (UnicodeError,ssl.SSLError):raise ClientError('Invalid trusted certificate authority') from None
    return data


def transport_profile(config,directory,ca_data=None):
    """Copy public trust separately from credentials; relative path survives directory transfer."""
    result={'controller':controller_url(config)} if config.get('controller') else {'host':'127.0.0.1','port':config['port']}
    if config.get('ca_file'):
        data=authority_bytes(config['ca_file']) if ca_data is None else ca_data
        target=Path(directory)/'controller-ca.pem'
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as source:source.write(data)
        result['ca_file']='controller-ca.pem'
    return result


def private_profile(path):
    """Read only a regular owner-only credential file; never print its contents."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ClientError('Client profile must be an owner-only regular file (chmod 600)')
            content = file.read(65537)
        if len(content) > 65536:
            raise ClientError('Client profile is too large')
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError()
        return resolve_ca(value,path)
    except ClientError:
        raise
    except (OSError, ValueError):
        raise ClientError('Cannot read private client profile') from None


def controller_url(config):
    value = config.get('controller')
    if value is None:
        # Compatibility with local v0.4 profiles. Never infer a non-loopback host.
        port = config.get('port')
        if config.get('host') != '127.0.0.1' or type(port) is not int or not 1 <= port <= 65535:
            raise ClientError('Profile requires an HTTPS controller URL or loopback SSH tunnel')
        value = 'http://127.0.0.1:' + str(port)
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value) or '\\' in value:
        raise ClientError('Invalid controller URL')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ClientError('Invalid controller URL') from None
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or re.search(r'[^/A-Za-z0-9._~-]', parsed.path)
            or any(part in {'.', '..'} for part in parsed.path.split('/'))):
        raise ClientError('Controller URL cannot contain credentials, query, or unsafe path')
    if parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', '::1'}:
        raise ClientError('Remote controller requires verified HTTPS; HTTP is limited to literal loopback')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ClientError('Controller redirect refused; update the trusted profile explicitly')


class Client:
    def __init__(self, config):
        self.config = config
        self.url = controller_url(config)
        token = config.get('token')
        if token:
            if not isinstance(token, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise ClientError('Invalid client token')
            self.authorization = 'Bearer ' + token
        else:
            username, password = config.get('username'), config.get('password')
            if (not isinstance(username, str) or ':' in username or not isinstance(password, str)
                    or not username or not password):
                raise ClientError('Profile requires a personal token or username/password')
            self.authorization = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
        ca = config.get('ca_file')
        if ca is not None and not isinstance(ca, str):
            raise ClientError('ca_file must name a trusted PEM file')
        try:
            context = ssl.create_default_context(cafile=ca)
            context.hostname_checks_common_name=False
        except (OSError, ssl.SSLError):
            raise ClientError('Cannot load the configured trusted certificate authority') from None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=context))

    @classmethod
    def from_profile(cls, path):
        return cls(private_profile(path))

    def request(self, path, body=None, raw=False, max_bytes=2 * 1024**2):
        if (not isinstance(path, str) or not path.startswith('/api/') or path.startswith('//')
                or any(ord(c) <= 32 for c in path) or '#' in path):
            raise ClientError('Invalid API path')
        headers = {'Authorization': self.authorization, 'Accept': 'application/json'}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            slow=path.endswith('/seal') or path.endswith('/archive-manifest') or path=='/api/worker/complete'
            with self.opener.open(req, timeout=120 if slow else 15) as response:
                content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ClientError('Controller response exceeds client limit')
            return content if raw else json.loads(content)
        except urllib.error.HTTPError as error:
            error.close()
            # Do not echo remotely supplied text, credentials, URLs, or TLS diagnostics.
            raise ClientHTTPError(error.code) from None
        except ClientError:
            raise
        except (OSError, ValueError):
            raise ClientUnavailable('Controller connection or response unavailable; no task was run locally') from None

    def upload_input(self,source):
        from io_policy import limits,MIB
        description=self.request('/api/me');ceiling=limits(description.get('io_limits'))
        chunk_size=description.get('transfer_chunk_bytes',12*1024)
        if type(chunk_size) is not int or not 1<=chunk_size<=256*1024:raise ClientError('Unsupported input transfer chunk size')
        source=Path(source).expanduser().absolute()
        if source.is_symlink() or not source.exists():raise ClientError('Choose a regular input file or directory')
        paths=[]
        if source.is_file():paths=[source];base=source.parent
        elif source.is_dir():
            base=source
            for parent,dirs,names in os.walk(source,followlinks=False):
                for name in dirs+names:
                    if name.startswith('.') or name=='__pycache__':continue
                    if (Path(parent)/name).is_symlink():raise ClientError('Input contains a symlink; select regular files explicitly')
                dirs[:]=[name for name in dirs if not name.startswith('.') and name!='__pycache__']
                paths.extend(Path(parent)/name for name in names if not name.startswith('.') and name!='__pycache__')
                if len(paths)>128:raise ClientError('Input directory exceeds 128 files; provide a source archive explicitly')
        else:raise ClientError('Input is not a regular file or directory')
        if not paths:raise ClientError('Input is empty; hidden files are excluded by default')
        manifest=[]
        for path in sorted(paths):
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as file:
                info=os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size>ceiling['file_mib']*MIB:raise ClientError('Input file exceeds administrator limit')
                value=hashlib.sha256()
                for block in iter(lambda:file.read(1024*1024),b''):value.update(block)
            manifest.append({'name':str(path.relative_to(base)),'size':info.st_size,'sha256':value.hexdigest(),'executable':bool(info.st_mode&0o111)})
        bundle=self.request('/api/inputs',{'files':manifest})['input']
        if bundle['sealed']:return bundle
        for item in manifest:
            offset=bundle['uploaded'].get(item['name'],0)
            fd=os.open(base/item['name'],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):raise ClientError('Input changed into a special file')
                file.seek(offset)
                while offset<item['size']:
                    chunk=file.read(min(chunk_size,item['size']-offset))
                    if not chunk:raise ClientError('Input changed during upload; no workflow was submitted')
                    self.request('/api/inputs/'+bundle['id']+'/blob',{'name':item['name'],'offset':offset,'data':base64.b64encode(chunk).decode()})
                    offset+=len(chunk)
        return self.request('/api/inputs/'+bundle['id']+'/seal',{})['input']


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'job-[0-9a-f]{12}', value):
        raise ClientError('Invalid job ID')
    return value


def bound_workflow(client,path,environment):
    try:
        raw=Path(path).expanduser().read_bytes()
        if len(raw)>32768:raise ValueError()
        request=json.loads(raw)
    except (OSError,ValueError):raise ClientError('Cannot read bounded workflow JSON') from None
    allowed={'template','parameters','name','environment','project','output','cwd','idempotency_key','origin','depends_on','artifact_inputs'}
    if not isinstance(request,dict) or set(request)-allowed or request.get('template') not in WORKFLOW_TEMPLATES:
        raise ClientError('Choose one versioned workflow request')
    if request.get('environment',environment)!=environment:raise ClientError('Workflow environment differs from session policy')
    request={k:v for k,v in request.items() if k not in {'idempotency_key','origin'}}
    request['environment']=environment
    approved=client.request('/api/jobs/validate',request)['request']
    request['parameters']=approved['parameters'];request['name']=approved['name']
    return request

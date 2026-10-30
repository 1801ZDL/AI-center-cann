"""Personal archive preferences, local submission context and verified resumable download.

Only the authenticated client writes personal paths. No controller filesystem imports.
"""
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import urllib.parse

from lab_client import ClientError, atomic_json, job_id, now, private_profile

CHUNK = 256 * 1024
FINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
RESERVED = {'.lab-archive.json', '.lab-complete.json', '.lab-lock', '.lab-partial'}


def local_path(value):
    value=os.path.abspath(os.path.expanduser(str(value)))
    if any(ord(c) < 32 or ord(c) == 127 for c in value):raise ClientError('Invalid local archive path')
    return Path(value)


@contextmanager
def directory(path, create=False, private=False):
    """Walk with directory FDs; never follow a symlink in the chosen path."""
    path=local_path(path);fd=os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:pass
            child=os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd);fd=child
        info=os.fstat(fd)
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ClientError('Personal archive state and job directories must be owned by you with mode 700')
        yield fd
    except OSError:
        raise ClientError('Cannot access a regular local directory; symlink paths are refused') from None
    finally:os.close(fd)


def regular(fd):
    info=os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ClientError('Archive file must be private, regular, owned by you, and not hard-linked')
    return info


def read_record(parent, name):
    try:fd=os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:return None
    except OSError:raise ClientError('Unsafe local archive record') from None
    with os.fdopen(fd, 'rb') as source:
        regular(source.fileno());content=source.read(2*1024**2+1)
    try:
        if len(content)>2*1024**2:raise ValueError()
        value=json.loads(content)
        if not isinstance(value, dict):raise ValueError()
        return value
    except ValueError:raise ClientError('Invalid local archive record; existing files were retained') from None


def write_new(parent, name, value):
    try:fd=os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    except FileExistsError:raise ClientError('Local archive record already exists; no overwrite performed') from None
    with os.fdopen(fd, 'w') as target:
        json.dump(value, target, ensure_ascii=False, sort_keys=True, indent=2);target.write('\n')
        target.flush();os.fsync(target.fileno())
    os.fsync(parent)


def state_path(profile):
    profile=local_path(profile)
    return profile.parent/(profile.name+'.state')


def remember_input(profile, client, source, bundle):
    identity=client.request('/api/me')['id'];source=local_path(source)
    value={'controller':client.url, 'identity':identity, 'source':str(source)}
    key='input-'+hashlib.sha256((client.url+'\0'+identity+'\0'+bundle['id']).encode()).hexdigest()+'.json'
    with directory(state_path(profile), create=True, private=True) as parent:
        old=read_record(parent, key)
        # Identical content may originate in several local trees. Explicit --local-source resolves it.
        if old is None:write_new(parent, key, value)


def remember_submission(profile, client, job, source=None, input_id=None, fallback=None):
    identity=client.request('/api/me')['id'];identifier=job_id(job['id'])
    with directory(state_path(profile), create=True, private=True) as parent:
        if source is None and input_id:
            key='input-'+hashlib.sha256((client.url+'\0'+identity+'\0'+input_id).encode()).hexdigest()+'.json'
            record=read_record(parent, key)
            if record and record.get('controller')==client.url and record.get('identity')==identity:source=record['source']
        source=local_path(source or fallback or Path.cwd())
        with directory(source if source.is_dir() else source.parent):pass
        base=source if source.is_dir() else source.parent
        value={'controller':client.url, 'identity':identity, 'job_id':identifier,
               'local_source':str(source), 'default_root':str(base/'.ascend-lab-results')}
        name=identifier+'.json';old=read_record(parent, name)
        if old is not None:
            if old['controller']!=client.url or old['identity']!=identity:raise ClientError('Local job binding belongs to another controller or identity')
            return old
        write_new(parent, name, value);return value


def archive_root(client, identifier, profile=None, root=None, source=None):
    if root is not None:return local_path(root)
    preference=client.config.get('archive_root')
    if preference is not None:
        if not isinstance(preference, str) or not Path(preference).is_absolute():raise ClientError('archive_root must be an absolute local directory')
        return local_path(preference)
    if source is not None:
        source=local_path(source)
        return (source if source.is_dir() else source.parent)/'.ascend-lab-results'
    if profile:
        with directory(state_path(profile), create=True, private=True) as parent:binding=read_record(parent, identifier+'.json')
        if binding:
            if binding.get('controller')!=client.url or binding.get('identity')!=client.request('/api/me')['id']:
                raise ClientError('Local job binding belongs to another controller or identity')
            return local_path(binding['default_root'])
    return Path.cwd()/'.ascend-lab-results'


def checked_manifest(value, identifier):
    from io_policy import artifacts,MIB,META
    if not isinstance(value, dict) or value.get('version')!=1:raise ClientError('Unsupported archive manifest')
    try:budget=artifacts(value.get('limits'))
    except ValueError:raise ClientError('Invalid archive limits') from None
    job=value.get('job')
    if not isinstance(job, dict) or job.get('id')!=identifier or job.get('state') not in FINAL:raise ClientError('Job archive is not terminal')
    files=value.get('files')
    if not isinstance(files, list) or not 1<=len(files)<=2000:raise ClientError('Invalid archive file count')
    seen=set();total=0
    for entry in files:
        if not isinstance(entry, dict):raise ClientError('Invalid archive file entry')
        name=entry.get('name');size=entry.get('size');digest=entry.get('sha256')
        if (not isinstance(name, str) or not 0<len(name.encode())<=4096 or '\\' in name
                or any(ord(c)<32 or ord(c)==127 for c in name) or name!=str(PurePosixPath(name))
                or PurePosixPath(name).is_absolute()
                or any(part in {'.','..'} or len(part.encode())>255 for part in name.split('/'))
                or name.split('/')[0] in RESERVED or name in seen):raise ClientError('Unsafe or duplicate archive name')
        if type(size) is not int or not 0<=size<=budget['file_mib']*MIB or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ClientError('Invalid archive size or SHA256')
        seen.add(name);total+=size
    if total>budget['total_mib']*MIB+META or value.get('bytes')!=total:raise ClientError('Archive exceeds personal download budget')
    # Files may not replace another file with a directory.
    if any('/'.join(name.split('/')[:n]) in seen for name in seen for n in range(1, len(name.split('/')))):
        raise ClientError('Conflicting archive paths')
    return value


def digest_file(fd):
    os.lseek(fd, 0, os.SEEK_SET);digest=hashlib.sha256()
    while True:
        data=os.read(fd, CHUNK)
        if not data:break
        digest.update(data)
    return digest


def download(client, identifier, profile=None, root=None, source=None):
    identifier=job_id(identifier);identity=client.request('/api/me')['id']
    manifest=checked_manifest(client.request('/api/jobs/'+identifier+'/archive-manifest'), identifier)
    target=archive_root(client, identifier, profile, root, source)/identifier
    fingerprint=hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',',':'), ensure_ascii=False).encode()).hexdigest()
    binding={'version':1, 'controller':client.url, 'identity':identity,
             'manifest_sha256':fingerprint, 'manifest':manifest}
    transferred=0;resumed=0;verified=0
    with directory(target, create=True, private=True) as parent:
        lock=os.open('.lab-lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            regular(lock)
            try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:raise ClientError('This job archive is already being downloaded') from None
            old=read_record(parent, '.lab-archive.json')
            if old is None:write_new(parent, '.lab-archive.json', binding)
            elif old!=binding:raise ClientError('Archive belongs to another identity, controller or manifest; choose a new directory')
            for entry in manifest['files']:
                destination=target/entry['name'];partial_name=hashlib.sha256(entry['name'].encode()).hexdigest()+'.part'
                with directory(destination.parent, create=True, private=True) as dest, directory(target/'.lab-partial', create=True, private=True) as stage:
                    # A crash between exclusive publication and unlink can leave precisely these two links.
                    try:
                        a=os.stat(destination.name, dir_fd=dest, follow_symlinks=False)
                        b=os.stat(partial_name, dir_fd=stage, follow_symlinks=False)
                        if (a.st_dev, a.st_ino)==(b.st_dev, b.st_ino) and a.st_nlink==2 and stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid():
                            os.unlink(partial_name, dir_fd=stage);os.fsync(stage)
                    except FileNotFoundError:pass
                    try:existing=os.open(destination.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dest)
                    except FileNotFoundError:existing=None
                    except OSError:raise ClientError('Existing archive destination is unsafe; no overwrite performed') from None
                    if existing is not None:
                        try:
                            info=regular(existing)
                            if info.st_size!=entry['size'] or digest_file(existing).hexdigest()!=entry['sha256']:
                                raise ClientError('Existing archive content differs; no overwrite performed')
                        finally:os.close(existing)
                        verified+=1;continue
                    fd=os.open(partial_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=stage)
                    try:
                        info=regular(fd);offset=info.st_size
                        if offset>entry['size']:raise ClientError('Partial archive exceeds expected size; retained for review')
                        digest=digest_file(fd);resumed+=offset
                        while offset<entry['size']:
                            length=min(CHUNK, entry['size']-offset)
                            query=urllib.parse.urlencode({'name':entry['name'],'offset':offset,'length':length})
                            response=client.request('/api/jobs/'+identifier+'/artifact-chunk?'+query)
                            try:data=base64.b64decode(response['data'], validate=True)
                            except (KeyError, TypeError, ValueError):raise ClientError('Invalid archive chunk') from None
                            if (response.get('name')!=entry['name'] or response.get('offset')!=offset or response.get('size')!=entry['size']
                                    or len(data)!=length or response.get('sha256')!=hashlib.sha256(data).hexdigest()):raise ClientError('Archive chunk does not match requested range')
                            view=memoryview(data)
                            while view:
                                written=os.write(fd, view)
                                if written<=0:raise ClientError('Local archive write did not progress')
                                view=view[written:]
                            os.fsync(fd);digest.update(data);offset+=len(data);transferred+=len(data)
                        if digest.hexdigest()!=entry['sha256']:raise ClientError('Archive SHA256 mismatch; partial file retained, no final file published')
                        regular(fd)
                        try:os.link(partial_name, destination.name, src_dir_fd=stage, dst_dir_fd=dest, follow_symlinks=False)
                        except FileExistsError:raise ClientError('Archive destination appeared during download; no overwrite performed') from None
                        os.unlink(partial_name, dir_fd=stage);os.fsync(dest);os.fsync(stage);verified+=1
                    finally:os.close(fd)
            complete=read_record(parent, '.lab-complete.json')
            if complete is None:write_new(parent, '.lab-complete.json', {'version':1, 'manifest_sha256':fingerprint,
                    'verified_at':now(), 'files':verified, 'bytes':manifest['bytes'], 'job_state':manifest['job']['state']})
            elif complete.get('manifest_sha256')!=fingerprint:raise ClientError('Existing archive completion record differs')
        finally:os.close(lock)
    return {'job_id':identifier, 'job_state':manifest['job']['state'], 'directory':str(target),
            'files_verified':verified, 'bytes':manifest['bytes'], 'bytes_downloaded':transferred,
            'bytes_resumed':resumed, 'manifest_sha256':fingerprint, 'complete':True}

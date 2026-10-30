"""Bounded immutable-job archive reads, using the caller's existing job ACL."""
import base64
import hashlib
import os
from pathlib import PurePosixPath
import stat

from jobs import JobError, TERMINAL, read_json

MAX_FILE = 64 * 1024**2
MAX_TOTAL = 160 * 1024**2  # Includes controller-generated receipts in addition to workload output.
CHUNK = 256 * 1024


def relative_name(name):
    return (isinstance(name, str) and 0 < len(name.encode()) <= 4096 and '\\' not in name
            and not any(ord(c) < 32 or ord(c) == 127 for c in name)
            and name == str(PurePosixPath(name)) and not PurePosixPath(name).is_absolute()
            and all(part not in {'.', '..'} and len(part.encode()) <= 255 for part in name.split('/')))


def open_artifact(directory, name, max_file=MAX_FILE):
    if not relative_name(name):raise JobError('Invalid artifact name', 404)
    descriptors=[]
    try:
        parent=os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW);descriptors.append(parent)
        parts=name.split('/')
        for part in parts[:-1]:
            parent=os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent);descriptors.append(parent)
        fd=os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_file:
            os.close(fd);raise JobError('Unsafe or oversized archived file', 409)
        return os.fdopen(fd, 'rb')
    except OSError:
        raise JobError('Archived file is unavailable', 404) from None
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


def indexed_names(directory):
    entries=(read_json(directory/'artifacts.json') or {}).get('files', [])
    if not isinstance(entries, list) or len(entries) > 2000:raise JobError('Invalid archive index', 409)
    names=[entry.get('name') for entry in entries if isinstance(entry, dict)]
    if len(names) != len(entries) or any(not relative_name(name) for name in names) or len(set(names)) != len(names):
        raise JobError('Invalid archive index', 409)
    return sorted(names)


def manifest(directory, job):
    from io_policy import for_job,MIB,META
    budget=for_job(job)
    if job['state'] not in TERMINAL:raise JobError('Archive is available only after confirmed terminal state', 409)
    files=[];total=0
    for name in indexed_names(directory):
        with open_artifact(directory, name, budget['file_mib']*MIB) as source:
            before=os.fstat(source.fileno());digest=hashlib.sha256()
            for block in iter(lambda:source.read(CHUNK), b''):digest.update(block)
            after=os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise JobError('Archive changed during manifest read', 409)
        total+=after.st_size
        if total > budget['total_mib']*MIB+META:raise JobError('Archive exceeds download budget', 409)
        files.append({'name':name, 'size':after.st_size, 'sha256':digest.hexdigest()})
    if not files:raise JobError('Terminal archive is not ready', 409)
    return {'version':1, 'job':{key:job.get(key) for key in
            ['id','owner','project','state','template','environment','kind','node_id','attempt_id',
             'resources','allocation','started_at','finished_at','result']}, 'files':files, 'bytes':total,
            **({'limits':budget} if budget!={'file_mib':64,'total_mib':128} else {})}


def chunk(directory, job, query):
    if job['state'] not in TERMINAL:raise JobError('Archive is not terminal', 409)
    name=query.get('name', [''])[0]
    if name not in indexed_names(directory):raise JobError('Artifact is not indexed', 404)
    try:offset=int(query.get('offset', ['0'])[0]);length=int(query.get('length', [str(CHUNK)])[0])
    except (TypeError, ValueError):raise JobError('Invalid archive range') from None
    if offset < 0 or not 1 <= length <= CHUNK:raise JobError('Invalid archive range')
    from io_policy import for_job,MIB
    with open_artifact(directory, name,for_job(job)['file_mib']*MIB) as source:
        size=os.fstat(source.fileno()).st_size
        if offset > size:raise JobError('Archive offset exceeds file size', 409)
        source.seek(offset);data=source.read(length)
    return {'name':name, 'offset':offset, 'size':size, 'data':base64.b64encode(data).decode(),
            'sha256':hashlib.sha256(data).hexdigest()}

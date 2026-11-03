"""Frozen whole-archive manifests and FD-relative, chunk-confirmed cold copies.

The manifest is administrator-private. Indexed user artifacts and unindexed
controller evidence are enumerated separately; no new user download is created.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

import storage_files as fs
import storage_inventory as inventory

CHUNK=256*1024
MAX_BYTES=8*1024**3
MAX_MANIFEST=16*1024**2


def digest(raw):return hashlib.sha256(raw).hexdigest()


def checked_file(root,entry):
    """The root is already bounded by fs.scan; still check every live FD mount."""
    from archive_api import relative_name
    if not isinstance(entry,dict) or not relative_name(entry.get('name')):raise fs.error('Invalid cold source name')
    fd=fs.directory(root);handles=[fd]
    try:
        mount=fs.mount(fd);device=os.fstat(fd).st_dev
        parts=entry['name'].split('/')
        for name in parts[:-1]:
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);handles.append(fd)
            if os.fstat(fd).st_dev!=device or fs.mount(fd)!=mount:raise fs.error('Cold copy refuses a new parent mount')
        child=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        actual=fs.metadata(child);info=os.fstat(child)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or fs.mount(child)!=mount
            or any(actual[k]!=entry[k] for k in ['dev','ino','mode','size','mtime','ctime'])):
            os.close(child);raise fs.error('Cold source file changed or crosses a mount')
        return os.fdopen(child,'rb')
    finally:
        for item in reversed(handles):os.close(item)


def manifest(root,maximum,charged_bytes,private_allowlist=()):
    from archive_api import relative_name
    if type(maximum) is not int or not 1<=maximum<=MAX_BYTES:raise fs.error('Invalid cold archive byte bound')
    if type(charged_bytes) is not int or charged_bytes<0:raise fs.error('Invalid original archive charge')
    snapshot=fs.scan(root)
    if snapshot['bytes']>min(maximum,charged_bytes):
        raise fs.error('Whole archive exceeds its cold limit or original allocation; private evidence needs an explicit separate retention contract')
    if any(not stat.S_ISDIR(e['mode']) and not stat.S_ISREG(e['mode']) for e in snapshot['entries']):raise fs.error('Cold copies refuse links and special files')
    entries={e['name']:e for e in snapshot['entries']};index=entries.get('artifacts.json')
    if not index or not stat.S_ISREG(index['mode']) or index['size']>4*1024**2:raise fs.error('Cold source has no bounded immutable artifact index')
    with checked_file(root,index) as stream:raw=stream.read(4*1024**2+1)
    if len(raw)>4*1024**2:raise fs.error('Cold artifact index grew beyond its bound')
    body=json.loads(raw)
    items=body.get('files') if isinstance(body,dict) else None
    if not isinstance(items,list) or not 1<=len(items)<=2000:raise fs.error('Invalid cold artifact index')
    names=[e.get('name') for e in items if isinstance(e,dict)]
    if len(names)!=len(items) or any(not relative_name(n) for n in names) or len(set(names))!=len(names):raise fs.error('Invalid indexed artifact names')
    indexed=set(names);files=[];directories=[]
    if not isinstance(private_allowlist,(list,tuple)) or len(private_allowlist)>100 or any(not relative_name(n) for n in private_allowlist):raise fs.error('Use an explicit bounded private-file list')
    unindexed={e['name'] for e in snapshot['entries'] if stat.S_ISREG(e['mode'])}-indexed
    if unindexed-set(private_allowlist)-{'artifacts.json'}:
        raise fs.error('Unregistered private evidence is present; explicitly approve files before reading or copying them')
    for entry in snapshot['entries']:
        if stat.S_ISDIR(entry['mode']):directories.append(entry['name']);continue
        if not stat.S_ISREG(entry['mode']):raise fs.error('Cold copies refuse links and special files')
        hashes=[];full=hashlib.sha256();size=0
        with checked_file(root,entry) as source:
            while True:
                block=source.read(CHUNK)
                if not block:break
                hashes.append(digest(block));full.update(block);size+=len(block)
            if fs.metadata(source.fileno())!=entry_meta(entry,source.fileno()):raise fs.error('Cold source changed while hashing')
        if size!=entry['size']:raise fs.error('Cold source length changed')
        files.append({'name':entry['name'],'size':size,'sha256':full.hexdigest(),'chunks':hashes})
    names={f['name'] for f in files}
    if not indexed or not indexed<=names:raise fs.error('Cold source must preserve every indexed artifact')
    if fs.inventory_digest(fs.scan(root))!=fs.inventory_digest(snapshot):raise fs.error('Cold source changed during its manifest')
    value={'format':'ascend-lab-cold-files/v1','inventory':snapshot,'files':files,'directories':directories,
        'indexed':sorted(indexed),'private':sorted(names-indexed),'bytes':snapshot['bytes']}
    validate(value)
    return value


def entry_meta(entry,fd):
    return {k:(fs.mount(fd) if k=='mount' else entry[k]) for k in ['dev','ino','mount','mode','size','mtime','ctime']}


def validate(value):
    from archive_api import relative_name
    if (not isinstance(value,dict) or set(value)!={'format','inventory','files','directories','indexed','private','bytes'}
        or value['format']!='ascend-lab-cold-files/v1' or len(fs.encode(value))>MAX_MANIFEST):raise fs.error('Invalid cold file manifest')
    if inventory.version(value['inventory'])!=2:raise fs.error('Cold exports require a portable validated inventory')
    inventory.persistent(value['inventory'])
    if type(value['bytes']) is not int or value['bytes']!=value['inventory']['bytes'] or not 0<=value['bytes']<=MAX_BYTES:raise fs.error('Cold manifest byte count differs')
    entries={x['name']:x for x in value['inventory']['entries']};files=value['files'];dirs=value['directories']
    if not isinstance(files,list) or not isinstance(dirs,list) or len(files)+len(dirs)!=len(entries):raise fs.error('Cold manifest entries differ')
    seen=set();total=0
    for item in files:
        if not isinstance(item,dict) or set(item)!={'name','size','sha256','chunks'}:raise fs.error('Invalid cold manifest file')
        name=item['name']
        if not relative_name(name):raise fs.error('Invalid cold file name')
        entry=entries.get(name)
        if (name in seen or not entry or not stat.S_ISREG(entry['mode'])
            or type(item['size']) is not int or item['size']!=entry['size'] or not isinstance(item['chunks'],list)
            or len(item['chunks'])!=(item['size']+CHUNK-1)//CHUNK):raise fs.error('Invalid cold file identity or chunks')
        if any(not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for v in [item['sha256'],*item['chunks']]):raise fs.error('Invalid cold content hash')
        seen.add(name);total+=item['size']
    for name in dirs:
        if not relative_name(name) or name in seen or name not in entries or not stat.S_ISDIR(entries[name]['mode']):raise fs.error('Invalid cold directory')
        seen.add(name)
    for key in ['indexed','private']:
        if not isinstance(value[key],list) or any(not relative_name(n) for n in value[key]):raise fs.error('Invalid cold manifest partition')
    if (seen!=set(entries) or total!=value['bytes'] or not value['indexed']
        or sorted(set(value['indexed']))!=value['indexed'] or sorted(set(value['private']))!=value['private']
        or set(value['indexed'])&set(value['private']) or set(value['indexed'])|set(value['private'])!={x['name'] for x in files}):raise fs.error('Cold public/private manifest partition differs')
    for name in seen:
        parent=Path(name).parent
        while str(parent)!='.':
            if str(parent) not in dirs:raise fs.error('Cold manifest omits an ancestor directory')
            parent=parent.parent


def unchanged(root,value):
    if fs.inventory_digest(fs.scan(root))!=fs.inventory_digest(value['inventory']):raise fs.error('Original archive changed; cold operation remains charged and unfinished')


def target_fd(root,name,create=False,uid=None):
    """No intermediate symlink or bind traversal, even in a private copy tree."""
    from archive_api import relative_name
    if not relative_name(name):raise fs.error('Invalid cold target name')
    fd=fs.directory(root);handles=[fd]
    try:
        mount=fs.mount(fd);device=os.fstat(fd).st_dev
        for part in name.split('/')[:-1]:
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);handles.append(fd)
            if os.fstat(fd).st_dev!=device or fs.mount(fd)!=mount:raise fs.error('Cold target parent mount changed')
        flags=os.O_RDWR|(os.O_CREAT|os.O_EXCL if create else 0)
        child=os.open(name.split('/')[-1],flags|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=fd)
        info=os.fstat(child)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_dev!=device or fs.mount(child)!=mount
            or info.st_uid not in {0,os.geteuid(),uid}):
            os.close(child);raise fs.error('Unsafe cold target file')
        os.fsync(fd)
        return child
    finally:
        for handle in reversed(handles):os.close(handle)


def prefix(fd,item,offset):
    if offset<0 or offset>item['size'] or offset!=item['size'] and offset%CHUNK:raise fs.error('Invalid committed cold offset')
    os.lseek(fd,0,os.SEEK_SET)
    for ordinal in range((offset+CHUNK-1)//CHUNK):
        length=min(CHUNK,offset-ordinal*CHUNK);block=os.read(fd,length)
        if len(block)!=length or digest(block)!=item['chunks'][ordinal]:raise fs.error('Confirmed cold prefix changed; no tail was discarded')


def verify(root,value,sealed_uid=None):
    validate(value);snapshot=fs.scan(root);actual={x['name']:x for x in snapshot['entries']}
    if set(actual)!={x['name'] for x in value['files']}|set(value['directories']):raise fs.error('Cold copy has missing or unexpected entries')
    for name in value['directories']:
        if not stat.S_ISDIR(actual[name]['mode']):raise fs.error('Cold directory was replaced by another type')
    if sealed_uid is not None:
        for path in [Path(root),*(Path(root)/name for name in value['directories'])]:
            info=path.lstat()
            if info.st_uid!=sealed_uid or stat.S_IMODE(info.st_mode)!=0o500:raise fs.error('Cold directory is not sealed to its controller')
    for item in value['files']:
        entry=actual[item['name']]
        if entry['size']!=item['size']:raise fs.error('Cold copy length differs')
        full=hashlib.sha256()
        with checked_file(root,entry) as source:
            for block in iter(lambda:source.read(CHUNK),b''):full.update(block)
            if sealed_uid is not None:
                info=os.fstat(source.fileno())
                if info.st_uid!=sealed_uid or stat.S_IMODE(info.st_mode)!=0o400:raise fs.error('Cold copy file is not sealed to its controller')
        if full.hexdigest()!=item['sha256']:raise fs.error('Cold copy content checksum differs')
    if fs.inventory_digest(fs.scan(root))!=fs.inventory_digest(snapshot):raise fs.error('Cold copy changed during verification')
    return snapshot


def seal(root,value,uid,gid):
    snapshot=verify(root,value)
    for entry in snapshot['entries']:
        if stat.S_ISDIR(entry['mode']):continue
        fd=target_fd(root,entry['name'],uid=uid)
        try:os.fchown(fd,uid,gid);os.fchmod(fd,0o400);os.fsync(fd)
        finally:os.close(fd)
    for name in sorted(value['directories'],key=lambda n:(n.count('/'),n),reverse=True):
        path=Path(root)/name;fd=fs.directory(path)
        try:os.fchown(fd,uid,gid);os.fchmod(fd,0o500);os.fsync(fd)
        finally:os.close(fd)
    fd=fs.directory(root)
    try:os.fchown(fd,uid,gid);os.fchmod(fd,0o500);os.fsync(fd)
    finally:os.close(fd)
    return verify(root,value,sealed_uid=uid)

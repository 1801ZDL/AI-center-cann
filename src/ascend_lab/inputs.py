"""Owner-bound immutable input bundles; bytes never name a controller filesystem path."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import re


def safe_name(name):
    if not isinstance(name,str) or not name or len(name.encode())>240 or '\\' in name or any(ord(c)<32 for c in name):return False
    path=PurePosixPath(name)
    return (not path.is_absolute() and name==str(path) and '..' not in path.parts
            and name!='.' and all(len(part.encode())<=200 for part in path.parts))


def sha256_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


class InputStore:
    MAX_FILES=128
    MAX_BYTES=128*1024**2
    MAX_FILE=64*1024**2
    CHUNK=256*1024

    def __init__(self,jobs):
        self.jobs=jobs
        from io_policy import limits,MIB
        configured=limits(jobs.policy.get('io_limits'));self.MAX_FILE=configured['file_mib']*MIB;self.MAX_BYTES=configured['input_mib']*MIB
        with jobs.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS input_bundles(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,manifest TEXT NOT NULL,bytes INTEGER NOT NULL,
              sealed INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS input_uploads(bundle TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,
              PRIMARY KEY(bundle,name));''')

    def directory(self,bundle_id,owner=None):
        from jobs import JobError
        if not isinstance(bundle_id,str) or not re.fullmatch(r'input-[0-9a-f]{24}',bundle_id):raise JobError('Invalid input ID')
        path=self.jobs.payload.path('inputs')/bundle_id
        if self.jobs.payload.policy and self.jobs.payload.policy.get('owner_groups'):
            if owner is None:
                with self.jobs.connect() as db:row=db.execute('SELECT owner FROM input_bundles WHERE id=?',(bundle_id,)).fetchone()
                if row is None:raise JobError('Input bundle not found',404)
                owner=row[0]
            self.jobs.payload.object(path,owner)
        return path

    def manifest(self,value):
        from jobs import JobError
        if not isinstance(value,list) or not 1<=len(value)<=self.MAX_FILES:raise JobError('Input manifest requires 1..128 files')
        result=[]
        for item in value:
            if (not isinstance(item,dict) or set(item)-{'name','size','sha256','executable'} or not {'name','size','sha256'}<=set(item) or not safe_name(item.get('name'))
                or type(item.get('size')) is not int or not 0<=item['size']<=self.MAX_FILE
                or type(item.get('executable',False)) is not bool
                or not isinstance(item.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',item['sha256'])):
                raise JobError('Invalid input file manifest')
            result.append({**item,'executable':item.get('executable',False)})
        names={x['name'] for x in result}
        if len(names)!=len(result) or any(str(parent) in names for name in names for parent in PurePosixPath(name).parents if str(parent)!='.'):
            raise JobError('Input names collide')
        if sum(x['size'] for x in result)>self.MAX_BYTES:raise JobError('Input bundle exceeds administrator total limit')
        return sorted(result,key=lambda x:x['name'])

    def identity(self,owner,files):
        manifest=self.manifest(files);canonical=json.dumps(manifest,sort_keys=True,separators=(',',':'))
        return 'input-'+hashlib.sha256((owner+':'+hashlib.sha256(canonical.encode()).hexdigest()).encode()).hexdigest()[:24]

    def create(self,principal,body):
        from jobs import JobError
        principal.require('jobs:submit')
        if not isinstance(body,dict) or set(body)!={'files'}:raise JobError('Provide only the input file manifest')
        manifest=self.manifest(body['files']);canonical=json.dumps(manifest,sort_keys=True,separators=(',',':'))
        bundle_id=self.identity(principal.id,manifest)
        size=sum(x['size'] for x in manifest)
        with self.jobs.transaction() as db:
            row=db.execute('SELECT manifest FROM input_bundles WHERE id=?',(bundle_id,)).fetchone()
            if row:
                if row[0]!=canonical:raise JobError('Input ID collision',409)
                state=self.jobs.lifecycle.record('input',bundle_id,db)['state']
                if state=='purged':
                    count=db.execute("SELECT COUNT(*) FROM input_bundles b LEFT JOIN storage_lifecycle l ON l.kind='input' AND l.id=b.id WHERE b.owner=? AND COALESCE(l.state,'available')!='purged'",(principal.id,)).fetchone()[0]
                    if count>=self.jobs.policy.get('input_max_bundles',32):raise JobError('Personal input bundle limit exceeded',409)
                    self.jobs.lifecycle.revive_input(db,bundle_id,principal,size)
                elif state!='available':raise JobError('Input payload is '+state+'; restore or reconcile it before upload',410)
                return self.get(bundle_id,principal,db),True
            count=db.execute("SELECT COUNT(*) FROM input_bundles b LEFT JOIN storage_lifecycle l ON l.kind='input' AND l.id=b.id WHERE b.owner=? AND COALESCE(l.state,'available')!='purged'",(principal.id,)).fetchone()[0]
            if count>=self.jobs.policy.get('input_max_bundles',32):
                raise JobError('Personal input storage budget exceeded',409)
            self.jobs.storage.reserve(db,'input',bundle_id,principal,size)
            directory=self.directory(bundle_id,owner=principal.id)
            if directory.is_symlink():raise JobError('Unsafe input storage path')
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            self.jobs.payload.object(directory,principal.id,create=True)
            from storage_io import sync_tree
            sync_tree(directory,self.jobs.payload.base)
            db.execute('INSERT INTO input_bundles VALUES(?,?,?,?,0)',(bundle_id,principal.id,canonical,size))
            return self.get(bundle_id,principal,db),False

    def get(self,bundle_id,principal=None,db=None,owner=None):
        from jobs import JobError
        if db is None:
            with self.jobs.connect() as connection:return self.get(bundle_id,principal,connection,owner)
        row=db.execute('SELECT owner,manifest,bytes,sealed FROM input_bundles WHERE id=?',(bundle_id,)).fetchone()
        if not row or (principal is not None and row[0]!=principal.id) or (owner is not None and row[0]!=owner):
            raise JobError('Input bundle not found',404)
        self.directory(bundle_id,owner=row[0])
        files=json.loads(row[1]);progress=dict(db.execute('SELECT name,size FROM input_uploads WHERE bundle=?',(bundle_id,)))
        return {'id':bundle_id,'owner':row[0],'files':files,'bytes':row[2],'sealed':bool(row[3]),
                'retention':self.jobs.lifecycle.public('input',bundle_id,db),
                'manifest_sha256':hashlib.sha256(row[1].encode()).hexdigest(),'uploaded':progress}

    def upload(self,bundle_id,principal,body):
        from jobs import JobError
        principal.require('jobs:submit')
        if not isinstance(body,dict) or set(body)!={'name','offset','data'}:raise JobError('Invalid input chunk')
        name=body['name'];offset=body['offset']
        if not safe_name(name) or type(offset) is not int or offset<0:raise JobError('Invalid input name/offset')
        try:data=base64.b64decode(body['data'],validate=True)
        except (ValueError,TypeError):raise JobError('Invalid input chunk encoding')
        if not 0<len(data)<=self.CHUNK:raise JobError('Input chunk exceeds bounded transfer size')
        with self.jobs.transaction() as db:
            bundle=self.get(bundle_id,principal,db)
            self.jobs.lifecycle.available('input',bundle_id,db)
            item=next((x for x in bundle['files'] if x['name']==name),None)
            if item is None:raise JobError('Input file is not in the manifest')
            if bundle['sealed']:raise JobError('Input bundle is immutable',409)
            if offset+len(data)>item['size']:raise JobError('Input chunk exceeds declared size')
            size=bundle['uploaded'].get(name,0);path=self.directory(bundle_id)/name
            if path.is_symlink() or not path.resolve().is_relative_to(self.directory(bundle_id).resolve()):raise JobError('Unsafe input storage path')
            if offset<size:
                with path.open('rb') as file:file.seek(offset);old=file.read(len(data))
                if offset+len(data)>size or old!=data:raise JobError('Conflicting input retry',409)
                return {'size':size,'reused':True}
            if offset!=size:raise JobError('Input offset must be contiguous',409)
            from control_storage import require
            require(self.jobs.runtime,self.jobs.policy.get('control_storage'),self.jobs.control_filesystem)
            path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            from storage_io import write_chunk
            write_chunk(path,size,data,root=self.jobs.payload.base)
            db.execute('INSERT OR REPLACE INTO input_uploads VALUES(?,?,?)',(bundle_id,name,size+len(data)))
            return {'size':size+len(data),'reused':False}

    def seal(self,bundle_id,principal):
        from jobs import JobError
        principal.require('jobs:submit')
        with self.jobs.transaction() as db:
            bundle=self.get(bundle_id,principal,db)
            self.jobs.lifecycle.available('input',bundle_id,db)
            if bundle['sealed']:return bundle
            from control_storage import require
            require(self.jobs.runtime,self.jobs.policy.get('control_storage'),self.jobs.control_filesystem)
            for item in bundle['files']:
                path=self.directory(bundle_id)/item['name']
                if item['size']==0 and not path.exists():
                    from storage_io import write_chunk
                    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                    write_chunk(path,0,b'',root=self.jobs.payload.base)
                if bundle['uploaded'].get(item['name'],0)!=item['size'] or path.is_symlink() or sha256_file(path)!=item['sha256']:
                    raise JobError('Input is incomplete or checksum differs',409)
            from storage_io import sync_tree
            import stat
            for item in bundle['files']:
                path=self.directory(bundle_id)/item['name']
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                try:
                    actual=os.fstat(fd)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink!=1 or actual.st_size!=item['size']:
                        raise JobError('Unsafe input file before seal',409)
                    os.fchmod(fd,0o400);os.fsync(fd)
                finally:os.close(fd)
                sync_tree(path.parent,self.jobs.payload.base)
            sync_tree(self.directory(bundle_id),self.jobs.payload.base)
            db.execute('UPDATE input_bundles SET sealed=1 WHERE id=?',(bundle_id,))
            db.execute('UPDATE storage_allocations SET settled=1 WHERE kind=? AND id=?',('input',bundle_id))
            return self.get(bundle_id,principal,db)

    def reset_file(self,bundle_id,principal,body):
        from jobs import JobError
        principal.require('jobs:submit')
        if not isinstance(body,dict) or set(body)!={'name'}:raise JobError('Provide one manifest file name')
        with self.jobs.transaction() as db:
            bundle=self.get(bundle_id,principal,db)
            self.jobs.lifecycle.available('input',bundle_id,db)
            if bundle['sealed']:raise JobError('Input bundle is immutable',409)
            if body['name'] not in {x['name'] for x in bundle['files']}:raise JobError('Input file not found',404)
            # Revoke the authoritative prefix without deleting bytes first.
            # A crash/rollback cannot leave a durable old offset with no file.
            # The next upload truncates the now-uncommitted old tail under the
            # same write transaction; reserved storage remains unchanged.
            # A preceding interrupted seal may have made this file read-only.
            # Restoring service write permission does not alter committed data.
            import stat
            try:fd=os.open(self.directory(bundle_id)/body['name'],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            except FileNotFoundError:fd=None
            if fd is not None:
                try:
                    actual=os.fstat(fd)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink!=1:raise JobError('Unsafe input reset file',409)
                    os.fchmod(fd,0o600);os.fsync(fd)
                finally:os.close(fd)
            db.execute('DELETE FROM input_uploads WHERE bundle=? AND name=?',(bundle_id,body['name']))
        return {'reset':True}

    def chunk(self,bundle,body):
        from jobs import JobError
        name=body.get('name');offset=body.get('offset')
        item=next((x for x in bundle['files'] if x['name']==name),None)
        if not item or type(offset) is not int or not 0<=offset<=item['size']:raise JobError('Invalid assigned input range')
        with self.jobs.lifecycle.reader('input',bundle['id']):
            path=self.directory(bundle['id'])/name
            if not bundle['sealed'] or path.is_symlink():raise JobError('Input is not sealed',409)
            with path.open('rb') as file:file.seek(offset);data=file.read(self.CHUNK)
            return {'name':name,'offset':offset,'size':item['size'],'data':base64.b64encode(data).decode()}

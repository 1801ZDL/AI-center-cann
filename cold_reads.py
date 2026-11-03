"""Original job authority over a sealed cold copy; verified bounded range reads.

Callers retain their existing job/project/experiment ACL and storage reader lock.
The directory object is deliberately not a Path: private file IO must go through
archive_api.open_artifact so each returned block is checked against the copy plan.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import stat

import cold_exports as copies
import cold_files as files
import storage_files as fs

FORMAT='ascend-lab-cold-reader/v1'
MAX_RECORD=65536


def semantic(job):
    # Verified state restore relocates archive_dir and revises storage metadata;
    # neither changes execution, input, original authorization or result truth.
    return fs.digest({k:v for k,v in job.items() if k not in {'archive_dir','archive_status'}})


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS cold_readers(job_id TEXT PRIMARY KEY,phase TEXT NOT NULL,record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS cold_reader_changes(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,record TEXT NOT NULL)')


def record(db,job_id):
    from storage_lifecycle import identifier
    identifier('archive',job_id)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_readers'").fetchone():return None
    row=db.execute('SELECT phase,record FROM cold_readers WHERE job_id=?',(job_id,)).fetchone()
    if row is None:return None
    phase,raw=row
    if phase not in {'active','retiring','retired','restore_frozen'} or len(raw.encode())>MAX_RECORD:raise fs.error('Invalid retained cold reader')
    value=json.loads(raw)
    if (not isinstance(value,dict) or value.get('format')!=FORMAT or value.get('job_id')!=job_id
        or value.get('hot_state') not in {'retained','retiring','retired'}):raise fs.error('Cold reader identity or state differs')
    expected={'retained':'active','retiring':'retiring','retired':'retired'}
    if phase!='restore_frozen' and phase!=expected[value['hot_state']]:raise fs.error('Cold reader row and record state disagree')
    return phase,value


def public(db,job_id):
    found=record(db,job_id)
    if found is None:return None
    phase,value=found
    return {'tier':'cold','state':phase,'original_state':value['hot_state'],
        'user_reads_enabled':phase!='restore_frozen','online_payload_removed':value['hot_state']=='retired',
        'copy_id':value['copy_id'],'original_job_retained':True}


def selected(db,value):
    pool_row=copies.one(db,'cold_pools',value['pool_id']);copy_row=copies.one(db,'cold_transfers',value['copy_id'])
    if (pool_row is None or copy_row is None or pool_row[0] not in {'registered','restore_frozen'}
        or copy_row[0] not in {'verified','restore_frozen'}):raise fs.error('Cold reader has no retained verified copy')
    pool=pool_row[1];transfer=copy_row[1]
    if (transfer['job_id']!=value['job_id'] or transfer['owner']!=value['owner'] or transfer['pool_id']!=value['pool_id']
        or transfer['plan_sha256']!=value['copy_plan_sha256'] or transfer.get('verified_at') is None
        or transfer.get('target_inventory_sha256')!=value['target_inventory_sha256']):raise fs.error('Cold reader and copy receipt differ')
    return pool,transfer


def documents(pool,value):
    """Fixed private metadata only; never enumerate a model/session directory."""
    from deployment import regular_json
    copies.pool_check(pool,read_only=True);incoming,target=copies.object_paths(pool,{'id':value['copy_id']})
    if fs.exists(incoming) or not fs.exists(target):raise fs.error('Authoritative cold copy is unavailable')
    copies.object_directory(pool,target)
    if set(os.listdir(target))!={'data','control.json','receipt.json'}:raise fs.error('Cold object has unexpected private metadata')
    for path,mode in [(target,0o500),(target/'data',0o500),(target/'control.json',0o400),(target/'receipt.json',0o400)]:
        info=path.lstat()
        if info.st_uid!=pool['binding']['uid'] or stat.S_IMODE(info.st_mode)!=mode:raise fs.error('Authoritative cold object is not sealed')
    plan=regular_json(target/'control.json',max_bytes=files.MAX_MANIFEST+65536,private=True)
    copies.check_plan(plan,'copy')
    if (plan['id']!=value['copy_id'] or plan['pool_id']!=value['pool_id'] or plan['job_id']!=value['job_id']
        or plan['owner']!=value['owner'] or fs.digest(plan)!=value['copy_plan_sha256']):raise fs.error('Authoritative cold manifest changed')
    receipt=regular_json(target/'receipt.json',private=True)
    expected={'format':copies.FORMAT,'id':plan['id'],'plan_sha256':fs.digest(plan),'manifest_sha256':fs.digest(plan['manifest'])}
    if receipt!=expected:raise fs.error('Authoritative cold receipt changed')
    return target/'data',plan


class Directory:
    def __init__(self,path,manifest,snapshot,uid):
        self.path=Path(path);self.cold_manifest=manifest;self.entries={e['name']:e for e in snapshot['entries']}
        self.files={e['name']:e for e in manifest['files']};self.uid=uid
    def __fspath__(self):return str(self.path)
    def open(self,name,maximum):
        item=self.files.get(name);entry=self.entries.get(name)
        if item is None or entry is None:raise fs.error('Cold file is not in the original private manifest')
        if item['size']>maximum:raise fs.error('Cold file exceeds this reader budget')
        source=files.checked_file(self.path,entry)
        try:
            info=os.fstat(source.fileno())
            if info.st_uid!=self.uid or stat.S_IMODE(info.st_mode)!=0o400:raise fs.error('Cold file seal changed')
            return CheckedStream(source,item,entry)
        except BaseException:source.close();raise


class CheckedStream(io.IOBase):
    """At most one 256KiB cached block; arbitrary ranges check original hashes."""
    def __init__(self,source,item,entry):
        self.source=source;self.item=item;self.entry=entry;self.position=0;self.cached=None;self.cached_number=None
    def fileno(self):return self.source.fileno()
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.position
    def seek(self,offset,whence=os.SEEK_SET):
        base={os.SEEK_SET:0,os.SEEK_CUR:self.position,os.SEEK_END:self.item['size']}.get(whence)
        if base is None or type(offset) is not int or base+offset<0:raise ValueError('Invalid cold archive seek')
        self.position=base+offset;return self.position
    def check(self):
        current=fs.metadata(self.fileno())
        if any(current[k]!=self.entry[k] for k in ['dev','ino','mode','size','mtime','ctime']):raise fs.error('Cold file changed during range read')
    def read(self,size=-1):
        self._checkClosed();self.check()
        if size is None:size=-1
        if type(size) is not int:raise TypeError('Read size must be integer')
        end=self.item['size'] if size<0 else min(self.item['size'],self.position+size);output=[]
        while self.position<end:
            number=self.position//files.CHUNK
            if self.cached_number!=number:
                block=os.pread(self.fileno(),min(files.CHUNK,self.item['size']-number*files.CHUNK),number*files.CHUNK)
                if files.digest(block)!=self.item['chunks'][number]:raise fs.error('Cold range checksum differs from its approved copy')
                self.cached=block;self.cached_number=number
            offset=self.position%files.CHUNK;count=min(end-self.position,len(self.cached)-offset)
            if count<=0:raise fs.error('Cold file ended before its confirmed range')
            output.append(self.cached[offset:offset+count]);self.position+=count
        self.check();return b''.join(output)
    def close(self):
        if not self.closed:self.source.close()
        super().close()


def resolve(jobs,job_id,db):
    found=record(db,job_id)
    if found is None:return None
    phase,value=found
    if phase=='restore_frozen':raise fs.error('Restored cold reader needs administrator revalidation before any archive read')
    job=jobs.get(job_id,db)
    if job['owner']!=value['owner'] or semantic(job)!=value['job_semantic_sha256']:raise fs.error('Cold job identity or result changed')
    try:
        pool,transfer=selected(db,value);path,plan=documents(pool,value)
        snapshot=fs.scan(path)
        if fs.inventory_digest(snapshot)!=value['target_inventory_sha256']:raise fs.error('Cold tree metadata differs from its verified copy')
        return Directory(path,plan['manifest'],snapshot,pool['binding']['uid'])
    except (OSError,ValueError,KeyError,TypeError) as error:
        from jobs import JobError
        if isinstance(error,JobError):raise
        raise fs.error('Authoritative cold archive unavailable; no original-path fallback was used') from None

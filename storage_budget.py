"""Transactional central storage accounting, including queued/unknown tasks and retained results.

This is application admission, not a filesystem quota or a garbage collector.
"""
import json
from pathlib import Path
import stat

from io_policy import MIB,META,for_job


class StorageBudget:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS storage_allocations(kind TEXT,id TEXT,owner TEXT,bytes INTEGER,settled INTEGER DEFAULT 0,PRIMARY KEY(kind,id))')
            for identifier,owner,size in db.execute('SELECT id,owner,bytes FROM input_bundles').fetchall():
                retained=jobs.lifecycle.record('input',identifier,db)
                if retained['state']=='purged':size=retained['metadata_bytes']
                elif retained.get('metadata_bytes'):size+=retained['metadata_bytes']
                db.execute('INSERT OR IGNORE INTO storage_allocations VALUES(?,?,?,?,1)',('input',identifier,owner,size))
            for job in jobs.all(db):
                if db.execute('SELECT 1 FROM storage_allocations WHERE kind=? AND id=?',('archive',job['id'])).fetchone():continue
                terminal=job['state'] in {'succeeded','failed','cancelled','timed_out'}
                size=self.actual(job) if terminal else for_job(job)['total_mib']*MIB+META
                db.execute('INSERT INTO storage_allocations VALUES(?,?,?,?,?)',('archive',job['id'],job['owner'],size,int(terminal)))

    def reserve(self,db,kind,identifier,principal,size):
        from jobs import JobError
        if kind not in {'input','archive'} or type(size) is not int or size<0:raise JobError('Invalid storage allocation')
        old=db.execute('SELECT owner,bytes FROM storage_allocations WHERE kind=? AND id=?',(kind,identifier)).fetchone()
        if old:
            if old!=(principal.id,size):raise JobError('Storage allocation conflicts',409)
            return
        from control_storage import require
        require(self.jobs.runtime,self.jobs.policy.get('control_storage'))
        cap=self.jobs.policy.get(kind+'_budget_mib')
        user_limit=principal.quota.get(kind+'_storage_mib',cap if cap is not None else 512 if kind=='input' else 2048)
        if cap is not None:user_limit=min(user_limit,cap)
        user_limit*=MIB
        total_limit=self.jobs.policy.get('storage_budget_mib',32768)*MIB
        personal=db.execute('SELECT COALESCE(SUM(bytes),0) FROM storage_allocations WHERE kind=? AND owner=?',(kind,principal.id)).fetchone()[0]
        total=db.execute('SELECT COALESCE(SUM(bytes),0) FROM storage_allocations').fetchone()[0]
        owner_space=self.jobs.payload.owner_space(principal.id)
        if owner_space is not None:
            pending_owner=db.execute('SELECT COALESCE(SUM(bytes),0) FROM storage_allocations WHERE owner=? AND settled=0',(principal.id,)).fetchone()[0]
            if (size+pending_owner+65536>owner_space['byte_hard']-owner_space['bytes_used']
                or owner_space['inode_hard']-owner_space['inodes_used']<16):
                raise JobError('Personal central filesystem quota lacks reserved bytes or inodes; retained data was not removed',409)
        if self.jobs.payload.policy:
            self.jobs.payload.check()
            space=__import__('os').statvfs(self.jobs.payload.base)
            pending=db.execute('SELECT COALESCE(SUM(bytes),0) FROM storage_allocations WHERE settled=0').fetchone()[0]
            reserve=32*MIB
            if total+size>self.jobs.payload.policy['bytes']-reserve or space.f_bavail*space.f_frsize<pending+size+reserve:
                raise JobError('Dedicated central payload storage lacks reserved capacity; retained data was not removed',409)
        if personal+size>user_limit or total+size>total_limit:raise JobError('Central '+kind+' storage budget exceeded; retained data was not deleted',409)
        db.execute('INSERT INTO storage_allocations VALUES(?,?,?,?,0)',(kind,identifier,principal.id,size))

    def actual(self,job):
        from jobs import read_json
        if job.get('archive_status',{}).get('state')=='purged':return job['archive_status']['metadata_bytes']
        directory=self.jobs.directory(job['id']);total=META
        entries=(read_json(directory/'artifacts.json') or {}).get('files',[])
        if not isinstance(entries,list) or len(entries)>2000:raise ValueError('Invalid retained archive index')
        seen=set()
        for entry in entries:
            name=entry.get('name')
            if not isinstance(name,str) or name in seen or Path(name).is_absolute() or '..' in Path(name).parts:continue
            seen.add(name);path=directory/name
            try:info=path.lstat()
            except FileNotFoundError:continue
            if stat.S_ISREG(info.st_mode) and not path.is_symlink() and path.resolve().is_relative_to(directory.resolve()):total+=info.st_size
        return total

    def settle(self,db,job):
        row=db.execute('SELECT bytes,settled FROM storage_allocations WHERE kind=? AND id=?',('archive',job['id'])).fetchone()
        if not row or row[1]:return
        actual=self.actual(job)
        # Retain the larger accounting value if unexpected metadata exceeded its reservation.
        db.execute('UPDATE storage_allocations SET bytes=?,settled=1 WHERE kind=? AND id=?',(actual,'archive',job['id']))

    def summary(self,principal):
        with self.jobs.connect() as db:
            mine={kind:total for kind,total in db.execute('SELECT kind,SUM(bytes) FROM storage_allocations WHERE owner=? GROUP BY kind',(principal.id,))}
            result={'personal_bytes':mine,'personal_limits_mib':{kind:principal.quota.get(kind+'_storage_mib',self.jobs.policy.get(kind+'_budget_mib',512 if kind=='input' else 2048)) for kind in ['input','archive']},
                    'scope':'Application reservations and retained indexed bytes; not filesystem hard quota'}
            if self.jobs.payload.policy and principal.id in self.jobs.payload.policy.get('owner_groups',{}):
                quota=self.jobs.payload.owner_space(principal.id)
                result['filesystem_quota']={key:quota[key] for key in ['byte_hard','bytes_used','inode_hard','inodes_used']}
                result['filesystem_quota'].update(enforced=True,shared_by='all retained inputs, archives and trash for this owner')
            return result

    def resize(self,db,kind,identifier,principal,size):
        from jobs import JobError
        old=db.execute('SELECT owner,bytes FROM storage_allocations WHERE kind=? AND id=?',(kind,identifier)).fetchone()
        if not old or old[0]!=principal.id or type(size) is not int or size<0:raise JobError('Invalid storage resize',409)
        # Removing only this row makes reserve's per-user/global tests account for the delta.
        db.execute('DELETE FROM storage_allocations WHERE kind=? AND id=?',(kind,identifier))
        self.reserve(db,kind,identifier,principal,size)

"""Owner-authorized, journaled central-payload quarantine/restore/bounded purge.

No automatic collection is enabled. Compute state, leases, results and audit are
retained; quota credit follows confirmed removal, never just a rename.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import time

import storage_files as fs

PENDING={'moving_to_trash','restoring','purging'}
STATES={'available','trash','purged',*PENDING}


def grace(policy):
    value=policy.get('storage_trash_grace_seconds',7*86400)
    if type(value) is not int or not 1<=value<=365*86400:raise ValueError('Storage trash grace must be 1 second..365 days')
    return value


def identifier(kind,value):
    from jobs import JobError
    pattern={'input':'input-[0-9a-f]{24}','archive':'job-[0-9a-f]{12}'}.get(kind)
    if not pattern or not isinstance(value,str) or not re.fullmatch(pattern,value):raise JobError('Invalid storage object',404)
    return value


class Lifecycle:
    def __init__(self,jobs):
        self.jobs=jobs;grace(jobs.policy)
        with jobs.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS storage_lifecycle(
              kind TEXT NOT NULL,id TEXT NOT NULL,owner TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
              record TEXT NOT NULL,PRIMARY KEY(kind,id));
              CREATE TABLE IF NOT EXISTS storage_lifecycle_events(
              sequence INTEGER PRIMARY KEY,at REAL NOT NULL,actor TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,
              action TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,detail TEXT NOT NULL);''')

    def record(self,kind,value,db=None):
        identifier(kind,value)
        if db is None:
            with self.jobs.connect() as connection:return self.record(kind,value,connection)
        row=db.execute('SELECT record FROM storage_lifecycle WHERE kind=? AND id=?',(kind,value)).fetchone()
        return json.loads(row[0]) if row else {'kind':kind,'id':value,'state':'available','revision':0}

    def public(self,kind,value,db=None):
        record=self.record(kind,value,db)
        return {key:record[key] for key in ['kind','id','state','revision','purge_after','metadata_bytes','payload_bytes'] if key in record}

    def available(self,kind,value,db=None):
        from jobs import JobError
        state=self.record(kind,value,db)['state']
        if state!='available':raise JobError('Payload is '+state+'; inspect storage status and restore or reconcile it',409 if state in PENDING else 410)

    @contextmanager
    def reader(self,kind,value):
        identifier(kind,value)
        with fs.lock(self.jobs.runtime,kind+'-'+value):
            self.available(kind,value);yield

    def paths(self,kind,value):
        identifier(kind,value)
        return (self.jobs.directory(value) if kind=='archive' else self.jobs.inputs.directory(value),
                self.jobs.payload.path('storage-trash')/kind/value)

    def subject(self,kind,value,principal,db):
        from jobs import JobError
        if kind=='archive':
            subject=self.jobs.get(value,db,principal)
            if subject['owner']!=principal.id:raise JobError('Storage object not found',404)
        else:subject=self.jobs.inputs.get(value,principal,db)
        return subject

    def guard(self,kind,value,principal,db):
        from jobs import JobError,TERMINAL
        subject=self.subject(kind,value,principal,db)
        if self.jobs.inventory.holds(db=db):raise JobError('Reconcile outstanding node execution inventories before storage maintenance',409)
        if kind=='archive':
            if subject['state'] not in TERMINAL:raise JobError('Running, queued or unknown tasks cannot be retired',409)
            if self.jobs.dependencies.pinned(value,db):raise JobError('A waiting, running or unknown downstream stage still pins this producer archive',409)
            for raw, in db.execute('SELECT profile FROM worker_nodes WHERE disabled=0'):
                if json.loads(raw).get('environment_manifest',{}).get('source',{}).get('job_id')==value:
                    raise JobError('Enabled node environment certification still pins this probe evidence',409)
        elif any(job['state'] not in TERMINAL and (job.get('input',{}).get('id')==value
                 or job.get('_dependency_base_input')==value or job.get('dependency_state',{}).get('input_id')==value) for job in self.jobs.all(db)):
            raise JobError('A queued, running or unknown task still references this input',409)
        return subject

    def event(self,db,record,actor,action,detail=None):
        db.execute('INSERT INTO storage_lifecycle_events(at,actor,kind,id,action,state,revision,detail) VALUES(?,?,?,?,?,?,?,?)',
          (time.time(),actor,record['kind'],record['id'],action,record['state'],record['revision'],json.dumps(detail or {},sort_keys=True)))

    def save(self,db,record,subject=None):
        db.execute('INSERT INTO storage_lifecycle VALUES(?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET '
          'owner=excluded.owner,state=excluded.state,revision=excluded.revision,record=excluded.record',
          (record['kind'],record['id'],record['owner'],record['state'],record['revision'],json.dumps(record,sort_keys=True)))
        if record['kind']=='archive':
            job=subject or self.jobs.get(record['id'],db)
            job['archive_status']={key:record[key] for key in ['state','revision','purge_after','metadata_bytes'] if key in record}
            self.jobs.write(db,job)

    def metadata_bytes(self,record,subject,db):
        compact={key:value for key,value in record.items() if key!='inventory'}
        size=len(fs.encode(compact))+len(fs.encode({k:v for k,v in subject.items() if not k.startswith('_')}))
        if record['kind']=='archive':
            row=db.execute('SELECT record FROM workflow_results WHERE job_id=?',(record['id'],)).fetchone()
            if row:size+=len(row[0].encode())
        return 65536+size

    def plan(self,principal,body):
        try:return self._plan(principal,body)
        except OSError:raise fs.error('Storage payload cannot be safely inspected; no plan was committed') from None

    def _plan(self,principal,body):
        from jobs import JobError
        principal.require('jobs:read')
        if not isinstance(body,dict) or set(body)!={'kind','id','action'}:raise JobError('Provide storage kind, ID and action')
        kind,value,action=body['kind'],body['id'],body['action'];identifier(kind,value)
        if action not in {'trash','restore','purge','resume'}:raise JobError('Invalid storage action')
        with fs.lock(self.jobs.runtime,'maintenance',exclusive=True),fs.lock(self.jobs.runtime,kind+'-'+value):
            with self.jobs.transaction() as db:
                subject=self.guard(kind,value,principal,db);record=self.record(kind,value,db);state=record['state']
                source,trash=self.paths(kind,value)
                if action=='resume':
                    if state not in PENDING:raise JobError('There is no pending storage operation',409)
                    fingerprint=record['fingerprint'];snapshot=record['inventory']
                else:
                    if (action=='trash' and state!='available') or (action in {'restore','purge'} and state!='trash'):
                        raise JobError('Storage state changed; inspect it before choosing an action',409)
                    snapshot=fs.scan(source if action=='trash' else trash);fingerprint=fs.digest(snapshot)
                    if action!='trash' and fingerprint!=record['fingerprint']:raise JobError('Quarantine content changed; preserve it for reconciliation',409)
                eligible=action!='purge' or time.time()>=record['purge_after']
                return {'plan':{'kind':kind,'id':value,'action':action,'revision':record['revision'],'fingerprint':fingerprint},
                  'object':self.public(kind,value,db),'eligible':eligible,'payload_bytes':snapshot['bytes'],'entries':len(snapshot['entries']),
                  'can_apply':'storage:manage' in principal.scopes,'purge_after':record.get('purge_after'),
                  'scope':'Central payload only; compute history, result rows and personal/node copies are retained'}

    def apply(self,principal,plan):
        try:return self._apply(principal,plan)
        except OSError:raise fs.error('Storage filesystem operation is unconfirmed; inspect and resume the original operation') from None

    def _apply(self,principal,plan):
        from jobs import JobError
        principal.require('storage:manage')
        if (not isinstance(plan,dict) or set(plan)!={'kind','id','action','revision','fingerprint'}
            or type(plan['revision']) is not int or not re.fullmatch('[0-9a-f]{64}',str(plan['fingerprint']))):raise JobError('Apply an exact storage plan')
        kind,value,action=plan['kind'],plan['id'],plan['action'];identifier(kind,value)
        if action not in {'trash','restore','purge','resume'}:raise JobError('Invalid storage action')
        request_sha=fs.digest(plan)
        with fs.lock(self.jobs.runtime,'maintenance',exclusive=True),fs.lock(self.jobs.runtime,kind+'-'+value,exclusive=True):
            with self.jobs.transaction() as db:
                subject=self.guard(kind,value,principal,db);record=self.record(kind,value,db)
                if record.get('request_sha256')==request_sha and record['state'] not in PENDING:
                    return {'object':self.public(kind,value,db),'reused':True,'pending':False}
                if action=='resume' and record['state'] not in PENDING and record['revision']==plan['revision'] and record.get('fingerprint')==plan['fingerprint']:
                    return {'object':self.public(kind,value,db),'reused':True,'pending':False}
                if action=='resume':
                    if record['state'] not in PENDING or record['revision']!=plan['revision'] or record['fingerprint']!=plan['fingerprint']:
                        raise JobError('Pending storage operation changed; inspect it before resuming',409)
                elif record.get('request_sha256')==request_sha and record['state'] in PENDING:pass
                else:
                    state=record['state']
                    if record['revision']!=plan['revision'] or action=='trash' and state!='available' or action in {'restore','purge'} and state!='trash':
                        raise JobError('Storage revision or state changed; create a fresh plan',409)
                    source,trash=self.paths(kind,value);snapshot=fs.scan(source if action=='trash' else trash)
                    if fs.digest(snapshot)!=plan['fingerprint']:raise JobError('Storage content changed after preview; create a fresh plan',409)
                    if action!='trash' and plan['fingerprint']!=record.get('fingerprint'):raise JobError('Quarantine changed after retirement',409)
                    if action=='purge' and time.time()<record['purge_after']:raise JobError('Trash grace period has not expired',409)
                    index=record.get('manifest')
                    evidence=record.get('evidence',{})
                    if action=='trash':
                        if kind=='archive':
                            from archive_api import open_artifact
                            with open_artifact(source,'artifacts.json',max_file=4*1024**2) as f:index=json.load(f)
                            evidence={}
                            for name,key,limit in [('artifact-hashes.json','content_hashes',4*1024**2),('results/_lab/environment.json','environment',256*1024)]:
                                if fs.exists(source/name):
                                    with open_artifact(source,name,max_file=limit) as f:evidence[key]=json.load(f)
                        else:index={'files':subject['files'],'manifest_sha256':subject['manifest_sha256']}
                    allocation=db.execute('SELECT bytes FROM storage_allocations WHERE kind=? AND id=?',(kind,value)).fetchone()
                    if not allocation:raise JobError('Storage allocation is unavailable',409)
                    record={**record,'owner':principal.id,'state':{'trash':'moving_to_trash','restore':'restoring','purge':'purging'}[action],
                      'revision':record['revision']+1,'request_sha256':request_sha,'operation':action,'inventory':snapshot,
                      'fingerprint':plan['fingerprint'],'payload_bytes':snapshot['bytes'],'manifest':index,'evidence':evidence,
                      'allocation_before':allocation[0],**({'purge_after':time.time()+grace(self.jobs.policy)} if action=='trash' else {})}
                    record['manifest_sha256']=fs.digest(index)
                    self.save(db,record,subject);self.event(db,record,principal.id,'begin-'+action)
            # The committed intent blocks readers/new input consumers before any rename/unlink.
            return self.finish(principal,record)

    def finish(self,principal,record):
        from jobs import JobError
        kind,value=record['kind'],record['id'];source,trash=self.paths(kind,value);action=record['operation']
        parent=trash.parent
        fs.private_tree(self.jobs.payload.base,['storage-trash',kind])
        for directory in [parent,parent.parent]:
            info=directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise JobError('Unsafe quarantine parent',409)
        removed=0
        if action in {'trash','restore'}:
            origin,target=(source,trash) if action=='trash' else (trash,source)
            if fs.exists(origin):
                if fs.exists(target):raise JobError('Storage destination already exists; no data was overwritten',409)
                if fs.digest(fs.scan(origin))!=record['fingerprint']:raise JobError('Pending storage payload changed; preserve both state and data',409)
                fs.move(origin,target)
            elif not fs.exists(target):raise JobError('Storage payload location is unconfirmed; quota remains charged',409)
            snapshot=fs.scan(target)
            if fs.digest(snapshot)!=record['fingerprint']:raise JobError('Moved payload identity/content differs; quota remains charged',409)
            state='trash' if action=='trash' else 'available'
        else:
            if fs.exists(source):raise JobError('Unexpected original payload exists; purge is blocked',409)
            complete,removed=fs.purge(trash,record['inventory'])
            if not complete:
                return {'object':self.public(kind,value),'pending':True,'removed_entries':removed,'quota_released':False}
            state='purged'
        with self.jobs.transaction() as db:
            subject=self.guard(kind,value,principal,db);current=self.record(kind,value,db)
            if current['revision']!=record['revision'] or current['state'] not in PENDING:raise JobError('Storage operation changed during filesystem maintenance',409)
            record={**record,'state':state,'completed_at':time.time()}
            if state=='purged':record['inventory']=None
            record['metadata_bytes']=self.metadata_bytes(record,subject,db)
            old=db.execute('SELECT bytes FROM storage_allocations WHERE kind=? AND id=?',(kind,value)).fetchone()[0]
            billed=record['metadata_bytes'] if state=='purged' else max(old,record['payload_bytes']+record['metadata_bytes']+len(fs.encode(record.get('inventory'))))
            db.execute('UPDATE storage_allocations SET bytes=?,settled=1 WHERE kind=? AND id=?',(billed,kind,value))
            self.save(db,record,subject);self.event(db,record,principal.id,'complete-'+action,{'charged_bytes':billed,'previous_charged_bytes':old})
            return {'object':self.public(kind,value,db),'pending':False,'removed_entries':removed,
                    'released_reserved_bytes':max(0,old-billed),'quota_released':state=='purged' and billed<old}

    def revive_input(self,db,value,principal,size):
        record=self.record('input',value,db)
        if record['state']!='purged':raise fs.error('Only a purged input can be uploaded again')
        source,trash=self.paths('input',value)
        if fs.exists(trash):raise fs.error('Purged input still has a quarantine payload; reconcile it first')
        if fs.exists(source):
            fd=fs.directory(source)
            try:
                if os.listdir(fd):raise fs.error('Input re-upload destination is not empty')
            finally:os.close(fd)
        self.jobs.storage.resize(db,'input',value,principal,size+record.get('metadata_bytes',0))
        fs.private_tree(self.jobs.payload.base,['inputs']);source.mkdir(mode=0o700,exist_ok=True)
        self.jobs.payload.object(source,principal.id,create=True)
        db.execute('UPDATE input_bundles SET sealed=0 WHERE id=?',(value,));db.execute('DELETE FROM input_uploads WHERE bundle=?',(value,))
        record={**record,'state':'available','revision':record['revision']+1,'inventory':None,'operation':None,'request_sha256':None}
        self.save(db,record);self.event(db,record,principal.id,'reupload')

    def listing(self,principal,limit=100,offset=0,state=None):
        from jobs import JobError
        principal.require('jobs:read');items=[]
        if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0 or state is not None and state not in STATES:
            raise JobError('Invalid storage page')
        with self.jobs.connect() as db:
            selection="SELECT j.id,'archive' kind,COALESCE(l.state,'available') state FROM jobs j LEFT JOIN storage_lifecycle l ON l.kind='archive' AND l.id=j.id WHERE j.owner=? UNION ALL SELECT b.id,'input' kind,COALESCE(l.state,'available') state FROM input_bundles b LEFT JOIN storage_lifecycle l ON l.kind='input' AND l.id=b.id WHERE b.owner=?"
            where=' WHERE state=?' if state else '';params=[principal.id,principal.id]+([state] if state else [])
            total=db.execute('SELECT COUNT(*) FROM ('+selection+')'+where,params).fetchone()[0]
            rows=db.execute('SELECT * FROM ('+selection+')'+where+' ORDER BY kind,id LIMIT ? OFFSET ?',[*params,limit,offset])
            items=[self.public(kind,value,db) for value,kind,_ in rows]
        return {'objects':items,'trash_grace_seconds':grace(self.jobs.policy),'can_manage':'storage:manage' in principal.scopes,
                'total':total,'limit':limit,'offset':offset,
                'automatic_gc':False,'scope':'Central payload lifecycle; not filesystem quota or node/personal cache cleanup'}


def rebind_restored(state,payload_base=None):
    """Called only after verified offline snapshot extraction and ownership assignment."""
    import sqlite3
    state=Path(state)
    with sqlite3.connect(state/'jobs.sqlite3') as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_lifecycle'").fetchone():return
        for kind,value,raw in db.execute('SELECT kind,id,record FROM storage_lifecycle').fetchall():
            record=json.loads(raw)
            if record['state'] in PENDING:raise fs.error('Finish pending storage maintenance before restoring state')
            if record['state']=='trash':
                snapshot=fs.scan(Path(payload_base or state)/'storage-trash'/kind/value);record.update(inventory=snapshot,fingerprint=fs.digest(snapshot))
                record['revision']+=1
            elif record['state']=='available':record['inventory']=None;record['revision']+=1
            db.execute('UPDATE storage_lifecycle SET revision=?,record=? WHERE kind=? AND id=?',(record['revision'],json.dumps(record,sort_keys=True),kind,value))
            if kind=='archive':
                row=db.execute('SELECT data FROM jobs WHERE id=?',(value,)).fetchone()
                if row:
                    job=json.loads(row[0]);job['archive_status']={key:record[key] for key in ['state','revision','purge_after','metadata_bytes'] if key in record}
                    db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(job),value))
            db.execute('INSERT INTO storage_lifecycle_events(at,actor,kind,id,action,state,revision,detail) VALUES(?,?,?,?,?,?,?,?)',
              (time.time(),'verified-offline-restore',kind,value,'rebind',record['state'],record['revision'],'{}'))

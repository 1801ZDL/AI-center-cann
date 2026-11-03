"""Root-managed finite cold pools and resumable, non-authoritative archive copies.

This stage does not switch application readers, retire originals or release a
job/history allocation. Plans include private controller evidence and stay
administrator-private. Normal artifact ACLs and indexes remain unchanged.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import deployment as deploy
from deployment import DeployError
import cold_files as files
import storage_files as fs

FORMAT='ascend-lab-cold-export/v1'
MARKER='.ascend-lab-cold.json'
RESERVE=16*1024**2
MAX_RECORDS=10000
ADMIN_UID=0


def _checkpoint(stage):pass


def policy(spec):
    from auth import IDENTIFIER
    from payload_storage import validate
    if not isinstance(spec,dict) or set(spec)!={'storage','budget_bytes','budget_inodes','owners','max_job_bytes','max_transfers'}:
        raise DeployError('Provide one explicit finite cold storage and owner policy')
    storage=validate(spec['storage'])
    if storage.get('owner_groups'):raise DeployError('Cold copy policy uses controller-only writes and application owner reservations; group quotas are not claimed')
    for key,low,high in [('budget_bytes',1024**2,storage['bytes']-RESERVE),('budget_inodes',16,storage['inodes']-128),
                         ('max_job_bytes',1,files.MAX_BYTES),('max_transfers',1,MAX_RECORDS)]:
        if type(spec[key]) is not int or not low<=spec[key]<=high:raise DeployError('Invalid finite cold policy '+key)
    owners=spec['owners']
    if not isinstance(owners,dict) or not 1<=len(owners)<=1000:raise DeployError('Cold pools need explicit owner budgets')
    for owner,limit in owners.items():
        if (not isinstance(owner,str) or not IDENTIFIER.fullmatch(owner) or not isinstance(limit,dict) or set(limit)!={'bytes','inodes'}
            or any(type(limit[k]) is not int or not 1<=limit[k]<=spec['budget_'+k] for k in ['bytes','inodes'])):
            raise DeployError('Invalid cold owner budget')
    return spec


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS cold_pools(id TEXT PRIMARY KEY,state TEXT NOT NULL,record TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS cold_transfers(id TEXT PRIMARY KEY,pool_id TEXT NOT NULL,job_id TEXT NOT NULL,owner TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)')


def rows(db,table):
    if table not in {'cold_pools','cold_transfers'}:raise DeployError('Invalid cold history table')
    if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():return {}
    values={}
    for key,state,text in db.execute('SELECT id,state,record FROM '+table+' LIMIT ?', (MAX_RECORDS+1,)):
        if len(values)>=MAX_RECORDS or len(text.encode())>(1024**2 if table=='cold_pools' else 65536) or not re.fullmatch('[0-9a-f]{64}',str(key)):
            raise DeployError('Cold metadata exceeds its bounded schema')
        value=json.loads(text)
        if not isinstance(value,dict) or value.get('id')!=key:raise DeployError('Cold row and record identity disagree')
        values[key]=(state,value)
    return values


def one(db,table,key):
    """Archive readers fetch one bounded receipt, not the entire cold history."""
    if table not in {'cold_pools','cold_transfers'} or not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key):raise DeployError('Invalid cold receipt identity')
    if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():return None
    row=db.execute('SELECT state,record FROM '+table+' WHERE id=?',(key,)).fetchone()
    if row is None:return None
    state,raw=row
    if len(raw.encode())>(1024**2 if table=='cold_pools' else 65536):raise DeployError('Cold receipt exceeds its bound')
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('id')!=key:raise DeployError('Cold receipt row and record differ')
    return state,value


@contextmanager
def context(prefix):
    from audit_retention import Database,service_identity
    from state_lock import state_lock
    from control_process import offline
    from control_monitor import directory
    from maintenance import assert_quiescent
    from payload_storage import Payload
    deploy.require_root();prefix,meta,release=deploy.verify(prefix)
    if meta['role']!='controller':raise DeployError('Cold export belongs to one stopped controller')
    for module in [Path(__file__),Path(files.__file__)]:
        if not (release/module.name).is_file() or deploy.digest(release/module.name)!=deploy.digest(module):
            raise DeployError('Activate matching cold export code before using it')
    state=Path(meta['state_dir'])
    if meta.get('control_filesystem'):
        from control_process import namespaces,context as control_context
        namespaces();control_context(meta).check()
    with service_identity(meta):lock=state_lock(state,exclusive=True)
    with lock,offline(prefix,meta):
        with directory(state,meta['uid']) as parent:
            fd=os.open('jobs.sqlite3',os.O_PATH|os.O_NOFOLLOW,dir_fd=parent)
            try:
                info=os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=meta['uid'] or info.st_nlink!=1 or info.st_mode&0o077
                    or info.st_dev!=os.fstat(parent).st_dev or fs.mount(fd)!=fs.mount(parent)):raise DeployError('Unsafe cold control database')
            finally:os.close(fd)
        config=deploy.regular_json(state/'config.json',private=True)
        if config.get('monitor_local') is not False or config.get('scheduler',{}).get('local_enabled') is not False:
            raise DeployError('Cold copies require a controller without local task execution')
        payload=Payload(state,config,uid=meta['uid'])
        with service_identity(meta):connection=sqlite3.connect(state/'jobs.sqlite3',timeout=5)
        db=Database(connection,meta)
        try:
            db.execute('BEGIN IMMEDIATE');assert_quiescent(state,'controller',cold_export=True)
            binding={'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],
                'installation_sha256':deploy.digest(prefix/'installation.json'),'configuration_sha256':deploy.digest(state/'config.json'),
                'database_device':info.st_dev,'database_inode':info.st_ino,'uid':meta['uid'],'gid':meta['gid']}
            yield db,binding,payload,meta
            with service_identity(meta):connection.commit()
        except BaseException:
            with service_identity(meta):connection.rollback()
            raise
        finally:
            with service_identity(meta):connection.close()


def commit(db,meta):
    from audit_retention import service_identity
    with service_identity(meta):db.connection.commit()
    db.execute('BEGIN IMMEDIATE')


def seal_plan(value):
    value['id']=fs.digest(value);return value


def check_plan(value,kind):
    if (not isinstance(value,dict) or value.get('format')!=FORMAT or value.get('kind')!=kind
        or not re.fullmatch('[0-9a-f]{64}',str(value.get('id')))
        or value['id']!=fs.digest({k:v for k,v in value.items() if k!='id'}) or len(fs.encode(value))>files.MAX_MANIFEST+65536):
        raise DeployError('Invalid or changed cold plan')
    common={'format','kind','binding','id'}
    expected=common|({'spec'} if kind=='register' else {'pool_id','pool_sha256','job_id','owner','job_sha256','allocation','manifest','reserved_bytes','reserved_inodes'})
    if set(value)!=expected:raise DeployError('Invalid cold plan schema')
    binding=value['binding'];strings={'prefix','state_dir','release_id','installation_sha256','configuration_sha256'}
    if (not isinstance(binding,dict) or set(binding)!=strings|{'database_device','database_inode','uid','gid'}
        or any(not isinstance(binding[k],str) or not binding[k] for k in strings)
        or any(type(binding[k]) is not int or binding[k]<0 for k in ['database_device','database_inode','uid','gid'])):raise DeployError('Invalid cold source binding')
    if kind=='register':policy(value['spec']);return
    from auth import IDENTIFIER
    from storage_lifecycle import identifier
    identifier('archive',value['job_id'])
    if not isinstance(value['owner'],str) or not IDENTIFIER.fullmatch(value['owner']):raise DeployError('Invalid cold owner')
    for key in ['pool_id','pool_sha256','job_sha256']:
        if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise DeployError('Invalid cold object identity')
    allocation=value['allocation']
    if (not isinstance(allocation,list) or len(allocation)!=3 or allocation[0]!=value['owner']
        or type(allocation[1]) is not int or allocation[1]<0 or type(allocation[2]) is not int or allocation[2]!=1
        or any(type(value[k]) is not int or value[k]<1 for k in ['reserved_bytes','reserved_inodes'])):raise DeployError('Invalid cold allocation')
    files.validate(value['manifest'])
    if value['manifest']['bytes']>allocation[1]:raise DeployError('Cold manifest exceeds original settled charge')


@contextmanager
def cold_root(spec,uid,*,read_only=False):
    from payload_storage import root_fd
    path=spec['storage']['path'];mounts=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before,after=line.split(' - ',1);parts=before.split()
        if parts[4]==path:mounts.append((parts[3],after.split()[0]))
    if mounts!=[('/','ext4')]:raise DeployError('Cold pool must expose an entire dedicated ext4 filesystem, not a subdirectory bind')
    with root_fd(spec['storage'],uid,writable=not read_only) as fd:yield fd


def location(spec,binding,payload):
    from payload_storage import disjoint
    disjoint(spec['storage'],binding['state_dir'],binding['prefix'])
    path=Path(spec['storage']['path'])
    if path.is_relative_to(payload.base) or payload.base.is_relative_to(path):raise DeployError('Cold and original payload trees must be disjoint')
    if spec['storage']['device'] in {Path(binding['state_dir']).stat().st_dev,payload.base.stat().st_dev}:raise DeployError('Cold storage must use a distinct finite filesystem')
    from unit_refresh import _directory
    _directory(path.parent)


def register_plan(prefix,spec):
    policy(spec)
    with context(prefix) as (db,binding,payload,meta):
        if any(s in {'registering','registered'} for s,r in rows(db,'cold_pools').values()):raise DeployError('This controller already has an active or pending cold pool')
        location(spec,binding,payload)
        with cold_root(spec,0) as fd:
            if os.listdir(fd):raise DeployError('Cold registration requires a new empty already-mounted filesystem')
        return seal_plan({'format':FORMAT,'kind':'register','binding':binding,'spec':spec})


def marker(pool):return {'format':FORMAT,'kind':'pool','id':pool['id'],'binding':pool['binding'],'spec_sha256':fs.digest(pool['spec'])}


def publish_json(path,value):
    """Fixed private temporary + no-replace rename, resumable after partial IO."""
    from payload_quota_admin import sync
    path=Path(path);raw=fs.encode(value);temporary=path.with_name('.'+path.name+'.pending')
    if path.exists() or path.is_symlink():
        if deploy.regular_json(path,max_bytes=files.MAX_MANIFEST+65536,private=True)!=value:raise DeployError('Cold immutable document differs')
        return
    fd=os.open(temporary,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=ADMIN_UID or info.st_size>len(raw):raise DeployError('Unsafe cold document temporary')
        old=os.read(fd,len(raw)+1)
        if not raw.startswith(old):raise DeployError('Cold document temporary is not the original prefix')
        os.ftruncate(fd,0);os.lseek(fd,0,os.SEEK_SET);view=memoryview(raw)
        while view:
            written=os.write(fd,view)
            if written<=0:raise OSError('Cold document write made no progress')
            view=view[written:]
        os.fchmod(fd,0o600);os.fsync(fd)
    finally:os.close(fd)
    fs.move(temporary,path);sync(path.parent)


def pool_check(pool,initial=False,*,read_only=False):
    expected=marker(pool);path=Path(pool['spec']['storage']['path'])
    uid=path.lstat().st_uid
    if uid not in ({0,pool['binding']['uid']} if initial else {pool['binding']['uid']}):raise DeployError('Cold root owner changed')
    with cold_root(pool['spec'],uid,read_only=read_only) as fd:
        names=set(os.listdir(fd))
        allowed={MARKER,'incoming','objects'}
        if initial:
            if names-allowed-{'.'+MARKER+'.pending'}:raise DeployError('Unexpected cold registration contents')
            if MARKER not in names:
                if names- {'.'+MARKER+'.pending'}:raise DeployError('Cold registration marker is missing')
                return
        elif names!=allowed:raise DeployError('Cold filesystem contents or registration are unconfirmed')
        source=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(source,'rb') as file:
            info=os.fstat(file.fileno());raw=file.read(65537)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid not in {0,pool['binding']['uid']}
                or info.st_mode&0o077 or len(raw)>65536 or json.loads(raw)!=expected):raise DeployError('Cold marker binding differs')
        for name in ['incoming','objects']:
            if initial and name not in names:continue
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                item=os.fstat(child)
                if item.st_uid not in {0,pool['binding']['uid']} or item.st_mode&0o777!=0o700 or fs.mount(child)!=fs.mount(fd):raise DeployError('Unsafe cold namespace')
            finally:os.close(child)


def register_apply(prefix,value):
    from payload_quota_admin import write,sync
    check_plan(value,'register');policy(value['spec'])
    with context(prefix) as (db,binding,payload,meta):
        if binding!=value['binding']:raise DeployError('Cold registration plan binding is stale')
        existing=rows(db,'cold_pools')
        if value['id'] in existing and existing[value['id']][0]!='registering':
            state,pool=existing[value['id']]
            if state!='registered' or pool!=value:raise DeployError('Cold registration receipt differs or was frozen')
            pool_check(pool)
            journal=Path(prefix)/'cold-registration-change.json'
            if journal.exists() or journal.is_symlink():
                if journal.lstat().st_uid!=0 or deploy.regular_json(journal,private=True)!=value:raise DeployError('Pending cold registration differs')
                journal.unlink();sync(journal.parent)
            return {'registered':True,'reused':True,'pool_id':value['id']}
        if value['id'] in existing and existing[value['id']][1]!=value:raise DeployError('Pending cold registration differs')
        if any(s in {'registering','registered'} and k!=value['id'] for k,(s,r) in existing.items()):raise DeployError('Another cold registration is active')
        if len(existing)>=MAX_RECORDS and value['id'] not in existing:raise DeployError('Cold pool history is full')
        location(value['spec'],binding,payload)
        if value['id'] not in existing:
            initialize(db)
            db.execute('INSERT INTO cold_pools VALUES(?,?,?)',(value['id'],'registering',json.dumps(value,sort_keys=True)))
            commit(db,meta);_checkpoint('registration_reserved')
        root=Path(value['spec']['storage']['path']);uid=meta['uid'];gid=meta['gid']
        # Root intent precedes filesystem adoption. It is not a reference to
        # user data; no transfer can begin until the pool transaction commits.
        journal=Path(prefix)/'cold-registration-change.json'
        if journal.exists() or journal.is_symlink():
            if journal.lstat().st_uid!=0 or deploy.regular_json(journal,private=True)!=value:raise DeployError('Resume the original cold registration')
        else:write(journal,value)
        pool_check(value,initial=True)
        publish_json(root/MARKER,marker(value))
        _checkpoint('registration_marker')
        for name in ['incoming','objects']:
            path=root/name;path.mkdir(mode=0o700,exist_ok=True);os.chown(path,uid,gid);path.chmod(0o700);sync(path)
        os.chown(root/MARKER,uid,gid);(root/MARKER).chmod(0o400);os.chown(root,uid,gid);root.chmod(0o700);sync(root)
        pool_check(value);initialize(db)
        db.execute('UPDATE cold_pools SET state="registered" WHERE id=?',(value['id'],))
        commit(db,meta);_checkpoint('registration_committed')
        journal.unlink();sync(journal.parent)
        return {'registered':True,'reused':False,'pool_id':value['id'],'owner_budgets':'application reservations; controller-only writes','original_storage_changed':False}


def job_record(db,job_id):
    from storage_lifecycle import identifier
    from jobs import TERMINAL
    identifier('archive',job_id)
    row=db.execute('SELECT owner,state,data FROM jobs WHERE id=?',(job_id,)).fetchone()
    if row is None:raise DeployError('Original archive job is unavailable')
    job=json.loads(row[2])
    if (job.get('id'),job.get('owner'),job.get('state'))!=(job_id,row[0],row[1]) or row[1] not in TERMINAL:
        raise DeployError('Cold copies require an intact confirmed terminal job')
    state=db.execute('SELECT state FROM storage_lifecycle WHERE kind="archive" AND id=?',(job_id,)).fetchone()
    if state and state[0]!='available':raise DeployError('Restore the original archive before preparing a cold copy')
    allocation=db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind="archive" AND id=?',(job_id,)).fetchone()
    if allocation is None or allocation[0]!=job['owner'] or allocation[2]!=1:raise DeployError('Original archive allocation is not settled')
    return job,allocation


def copy_plan(prefix,job_id,private_files=()):
    with context(prefix) as (db,binding,payload,meta):
        pools=[r for s,r in rows(db,'cold_pools').values() if s=='registered']
        if len(pools)!=1:raise DeployError('Register exactly one active cold pool')
        pool=pools[0];pool_check(pool);job,allocation=job_record(db,job_id)
        if job['owner'] not in pool['spec']['owners']:raise DeployError('This owner has no cold allocation')
        if any(r['pool_id']==pool['id'] and r['job_id']==job_id for s,r in rows(db,'cold_transfers').values()):
            raise DeployError('This archive already has a cold operation; preserve and inspect its original plan')
        source=payload.path('jobs')/job_id;payload.object(source,job['owner'])
        from jobs import ARTIFACT_NAMES
        private=list(ARTIFACT_NAMES|{'dispatch.json','completion.json','heartbeat.json','supervisor.json','command-process.json','cancel.json'})
        if job.get('source'):private.append('source/'+Path(job['source']['path']).name)
        from archive_api import relative_name
        if not isinstance(private_files,(list,tuple)) or len(private_files)>32 or any(not relative_name(n) for n in private_files):raise DeployError('Choose at most 32 explicit additional private files')
        data=files.manifest(source,pool['spec']['max_job_bytes'],allocation[1],private_allowlist=sorted(set(private)|set(private_files)))
        cost,inodes=copy_cost(pool,data)
        return seal_plan({'format':FORMAT,'kind':'copy','binding':binding,'pool_id':pool['id'],'pool_sha256':fs.digest(pool),
            'job_id':job_id,'owner':job['owner'],'job_sha256':fs.digest(job),'allocation':list(allocation),'manifest':data,
            'reserved_bytes':cost,'reserved_inodes':inodes})


def copy_cost(pool,data):
    block=os.statvfs(pool['spec']['storage']['path']).f_frsize
    metadata=((len(fs.encode(data))+65536+block-1)//block)*block
    return (sum(((f['size']+block-1)//block)*block for f in data['files'])+(len(data['directories'])+8)*block+metadata,
        len(data['files'])+len(data['directories'])+8)


def reserve(db,pool,plan):
    values=[r for s,r in rows(db,'cold_transfers').values() if r['pool_id']==pool['id']]
    if len(values)>=pool['spec']['max_transfers']:raise DeployError('Cold transfer history is full')
    for scope,limits in [('all',{'bytes':pool['spec']['budget_bytes'],'inodes':pool['spec']['budget_inodes']}),
                         ('owner',pool['spec']['owners'][plan['owner']])]:
        selected=[r for r in values if scope=='all' or r['owner']==plan['owner']]
        for key in ['bytes','inodes']:
            if sum(r['reserved_'+key] for r in selected)+plan['reserved_'+key]>limits[key]:raise DeployError('Cold '+scope+' '+key+' budget exceeded; no original was removed')
    space=os.statvfs(pool['spec']['storage']['path'])
    pending=[r for s,r in rows(db,'cold_transfers').values() if r['pool_id']==pool['id'] and s=='copying']
    if (space.f_bavail*space.f_frsize<plan['reserved_bytes']+sum(max(0,r['reserved_bytes']-r['copied_bytes']) for r in pending)+RESERVE
        or space.f_favail<plan['reserved_inodes']+sum(r['reserved_inodes'] for r in pending)+128):raise DeployError('Cold filesystem lacks reserved headroom')


def save(db,value,state='copying'):
    db.execute('UPDATE cold_transfers SET state=?,record=? WHERE id=?',(state,json.dumps(value,sort_keys=True),value['id']))


def object_paths(pool,plan):
    root=Path(pool['spec']['storage']['path']);return root/'incoming'/plan['id'],root/'objects'/plan['id']


def prepare_tree(pool,plan,record):
    from payload_quota_admin import write,sync
    incoming,complete=object_paths(pool,plan)
    if fs.exists(incoming) and fs.exists(complete):raise DeployError('Two cold object locations exist; no overwrite was attempted')
    if fs.exists(complete):object_directory(pool,complete);return complete
    incoming.mkdir(mode=0o700,exist_ok=True)
    info=incoming.lstat()
    allowed_uid={ADMIN_UID,pool['binding']['uid']} if record['file']==len(plan['manifest']['files']) else {ADMIN_UID}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_uid or info.st_mode&0o077:raise DeployError('Unsafe private incoming cold object')
    object_directory(pool,incoming)
    if set(os.listdir(incoming))-{'control.json','.control.json.pending','data','receipt.json','.receipt.json.pending'}:raise DeployError('Unexpected cold object metadata')
    control=incoming/'control.json'
    if control.exists() or control.is_symlink():
        if deploy.regular_json(control,max_bytes=files.MAX_MANIFEST+65536,private=True)!=plan:raise DeployError('Cold object belongs to another plan')
    else:
        if {p.name for p in incoming.iterdir()}-{'.control.json.pending'}:raise DeployError('Unconfirmed files in a new cold object')
        publish_json(control,plan)
    data=incoming/'data';data.mkdir(mode=0o700,exist_ok=True)
    parent=fs.directory(incoming)
    try:
        identity=fs.identity(parent)
        check=fs.directory(data)
        try:
            if fs.identity(check)['dev']!=identity['dev'] or fs.mount(check)!=identity['mount']:raise DeployError('Cold data root crosses a mount')
        finally:os.close(check)
    finally:os.close(parent)
    for name in sorted(plan['manifest']['directories'],key=lambda n:(n.count('/'),n)):
        path=data/name;path.mkdir(mode=0o700,exist_ok=True)
        if not stat.S_ISDIR(path.lstat().st_mode) or path.is_symlink():raise DeployError('Unsafe cold copy directory')
        fd=fs.directory(path)
        try:
            if fs.identity(fd)['dev']!=identity['dev'] or fs.mount(fd)!=identity['mount']:raise DeployError('Cold directory crosses a mount')
        finally:os.close(fd)
    sync(data);sync(incoming);sync(incoming.parent)
    return incoming


def copy_step(prefix,plan,maximum=4*1024**2):
    from payload_quota_admin import write,sync
    check_plan(plan,'copy');files.validate(plan['manifest'])
    if type(maximum) is not int or not files.CHUNK<=maximum<=16*1024**2 or maximum%files.CHUNK:raise DeployError('Cold step is 256KiB..16MiB in whole chunks')
    with context(prefix) as (db,binding,payload,meta):
        if binding!=plan['binding']:raise DeployError('Cold source/control binding is stale; do not replace the operation')
        pools=rows(db,'cold_pools');state,pool=pools.get(plan['pool_id'],(None,None))
        if state!='registered' or fs.digest(pool)!=plan['pool_sha256']:raise DeployError('Cold pool is unavailable or frozen')
        if (plan['reserved_bytes'],plan['reserved_inodes'])!=copy_cost(pool,plan['manifest']):raise DeployError('Cold original reservation differs from its full copy cost')
        pool_check(pool);job,allocation=job_record(db,plan['job_id'])
        if fs.digest(job)!=plan['job_sha256'] or list(allocation)!=plan['allocation']:raise DeployError('Original archive record or accounting changed')
        source=payload.path('jobs')/plan['job_id'];payload.object(source,job['owner']);files.unchanged(source,plan['manifest'])
        existing=rows(db,'cold_transfers');found=existing.get(plan['id'])
        if found:
            status,record=found
            if record['plan_sha256']!=fs.digest(plan):raise DeployError('Cold original plan differs')
            if status=='verified':verify_object(pool,plan);return response(record,'verified',True)
            if status!='copying':raise DeployError('Restored cold transfer is frozen; only historical verification is allowed')
        else:
            reserve(db,pool,plan)
            record={'id':plan['id'],'pool_id':pool['id'],'job_id':plan['job_id'],'owner':plan['owner'],'plan_sha256':fs.digest(plan),
                'reserved_bytes':plan['reserved_bytes'],'reserved_inodes':plan['reserved_inodes'],'copied_bytes':0,
                'file':0,'offset':0,'identity':None,'creating':False,'verified_at':None}
            db.execute('INSERT INTO cold_transfers VALUES(?,?,?,?,?,?)',(record['id'],pool['id'],plan['job_id'],plan['owner'],'copying',json.dumps(record,sort_keys=True)))
            commit(db,meta);_checkpoint('reserved')
        obj=prepare_tree(pool,plan,record);incoming,complete=object_paths(pool,plan)
        if obj==complete:
            if record['file']!=len(plan['manifest']['files']):raise DeployError('Cold object was published before its committed copy finished')
        else:
            left=maximum;completed=0;entries={v['name']:v for v in plan['manifest']['inventory']['entries']}
            while record['file']<len(plan['manifest']['files']) and left>0 and completed<64:
                item=plan['manifest']['files'][record['file']]
                if record['identity'] is None:
                    if not record['creating']:
                        record['creating']=True;save(db,record);commit(db,meta)
                    try:fd=files.target_fd(obj/'data',item['name'],create=True)
                    except FileExistsError:
                        fd=files.target_fd(obj/'data',item['name'])
                        if os.fstat(fd).st_size!=0:os.close(fd);raise DeployError('Unknown nonempty cold file; no original was changed')
                    _checkpoint('file_created')
                    info=os.fstat(fd);record.update(identity=[info.st_dev,info.st_ino],creating=False);save(db,record);commit(db,meta)
                else:fd=files.target_fd(obj/'data',item['name'])
                try:
                    info=os.fstat(fd)
                    if [info.st_dev,info.st_ino]!=record['identity'] or info.st_size<record['offset']:raise DeployError('Cold committed file identity/prefix disappeared')
                    if info.st_size>record['offset']:
                        files.prefix(fd,item,record['offset']);os.ftruncate(fd,record['offset']);os.fsync(fd)
                    with files.checked_file(source,entries[item['name']]) as original:
                        original.seek(record['offset']);os.lseek(fd,record['offset'],os.SEEK_SET)
                        while record['offset']<item['size'] and left>0:
                            block=original.read(min(files.CHUNK,item['size']-record['offset']))
                            if not block or files.digest(block)!=item['chunks'][record['offset']//files.CHUNK]:raise DeployError('Cold source chunk checksum changed')
                            if len(block)>left:return response(record,'copying',False)
                            view=memoryview(block)
                            while view:
                                n=os.write(fd,view)
                                if n<=0:raise OSError('Cold write made no progress')
                                view=view[n:]
                            os.fsync(fd);_checkpoint('data_synced')
                            record['offset']+=len(block);record['copied_bytes']+=len(block);left-=len(block)
                            save(db,record);commit(db,meta);_checkpoint('progress_committed')
                    if record['offset']==item['size']:
                        record.update(file=record['file']+1,offset=0,identity=None);completed+=1;save(db,record);commit(db,meta)
                finally:os.close(fd)
            if record['file']<len(plan['manifest']['files']):return response(record,'copying',False)
            files.unchanged(source,plan['manifest'])
            files.seal(obj/'data',plan['manifest'],meta['uid'],meta['gid'])
            publish_json(obj/'receipt.json',{'format':FORMAT,'id':plan['id'],'plan_sha256':fs.digest(plan),'manifest_sha256':fs.digest(plan['manifest'])})
            for name in ['control.json','receipt.json']:
                os.chown(obj/name,meta['uid'],meta['gid']);(obj/name).chmod(0o400)
            os.chown(obj,meta['uid'],meta['gid']);obj.chmod(0o500);sync(obj);_checkpoint('sealed')
            fs.move(incoming,complete);_checkpoint('copy_published')
        signature=verify_object(pool,plan);record.update(verified_at=time.time(),target_inventory_sha256=fs.inventory_digest(signature))
        save(db,record,'verified');commit(db,meta);_checkpoint('verified')
        return response(record,'verified',False)


def object_directory(pool,path):
    root=fs.directory(pool['spec']['storage']['path'])
    try:
        child=fs.directory(path)
        try:
            if os.fstat(child).st_dev!=os.fstat(root).st_dev or fs.mount(child)!=fs.mount(root):raise DeployError('Cold object root crosses a mount')
        finally:os.close(child)
    finally:os.close(root)


def verify_object(pool,plan):
    pool_check(pool);incoming,target=object_paths(pool,plan)
    if fs.exists(incoming) or not fs.exists(target):raise DeployError('Cold copy publication is incomplete')
    object_directory(pool,target)
    if set(os.listdir(target))!={'data','control.json','receipt.json'}:raise DeployError('Cold object has unregistered private metadata')
    for path,mode in [(target,0o500),(target/'control.json',0o400),(target/'receipt.json',0o400)]:
        info=path.lstat()
        if info.st_uid!=pool['binding']['uid'] or stat.S_IMODE(info.st_mode)!=mode:raise DeployError('Cold object metadata is not sealed')
    expected={'format':FORMAT,'id':plan['id'],'plan_sha256':fs.digest(plan),'manifest_sha256':fs.digest(plan['manifest'])}
    if deploy.regular_json(target/'receipt.json',private=True)!=expected or deploy.regular_json(target/'control.json',max_bytes=files.MAX_MANIFEST+65536,private=True)!=plan:
        raise DeployError('Cold copy receipt or original manifest differs')
    return files.verify(target/'data',plan['manifest'],sealed_uid=pool['binding']['uid'])


def response(record,state,reused):
    return {'id':record['id'],'state':state,'reused':reused,'copied_bytes':record['copied_bytes'],
        'original_retained':True,'read_pointer_changed':False,'history_capacity_released':False,'quota_released':False}


def verify_copy(prefix,plan):
    check_plan(plan,'copy')
    with context(prefix) as (db,binding,payload,meta):
        row=rows(db,'cold_transfers').get(plan['id']);pool_row=rows(db,'cold_pools').get(plan['pool_id'])
        if row is None or pool_row is None or row[0] not in {'verified','restore_frozen'} or row[1]['verified_at'] is None:
            raise DeployError('A complete retained cold copy is required')
        if row[1]['plan_sha256']!=fs.digest(plan):raise DeployError('Original cold plan differs')
        verify_object(pool_row[1],plan)
        return {**response(row[1],row[0],True),'copy_bytes_verified':True,'historical_only':row[0]=='restore_frozen'}


def pinned(db,job_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_transfers'").fetchone():return False
    return db.execute('SELECT 1 FROM cold_transfers WHERE job_id=? AND state="copying" LIMIT 1',(job_id,)).fetchone() is not None


def public_copies(db,job_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_transfers'").fetchone():return []
    from cold_reads import public
    authority=public(db,job_id)
    return [{**response(json.loads(text),state,True),'available_for_user_reads':bool(authority and authority['user_reads_enabled'] and authority['copy_id']==json.loads(text)['id'])} for state,text in db.execute('SELECT state,record FROM cold_transfers WHERE job_id=? LIMIT 100',(job_id,))]


def require_compatible(state,role,manifest):
    if role!='controller' or {'cold_exports.py','cold_files.py'}<=set(manifest.get('files',{})):return
    path=Path(state)/'jobs.sqlite3'
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if rows(db,'cold_pools') or rows(db,'cold_transfers'):raise DeployError('Retained cold copies require compatible code; refusing downgrade')


def require_history(current,staged):
    def read(state):
        path=Path(state)/'jobs.sqlite3'
        if not path.exists():return {'cold_pools':{},'cold_transfers':{}}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:return {table:{k:r for k,(s,r) in rows(db,table).items()} for table in ['cold_pools','cold_transfers']}
    old=read(current);new=read(staged)
    if any(new[table].get(key)!=record for table in old for key,record in old[table].items()):
        raise DeployError('Restore would lose cold pool, copy, budget or verification history')


def freeze_restored(state):
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path) as db:
        for table in ['cold_pools','cold_transfers']:
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():db.execute('UPDATE '+table+' SET state="restore_frozen"')

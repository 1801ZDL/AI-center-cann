"""Atomic bounded DAG admission; committed preparation never starts half a graph.

All ordinary job/account/project/experiment/storage gates are reused in one
SQLite transaction. Fixed launcher snapshots are materialized only after that
commit. A durable preparing record blocks dispatch and resumes the same IDs.
"""
import base64
from contextlib import ExitStack, closing
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from jobs import JobError,now
from workflow_spec import WORKFLOWS

CAPABILITY='atomic-job-graphs-v1'
MAX_NODES=16
MAX_GRAPHS=4096
MAX_BYTES=32768
MAX_PREPARE_RETRIES=32
MAX_RECORD_BYTES=65536
MAX_JOB_BYTES=32*1024**2
MAX_RESTORE_BYTES=64*1024**2
MAX_RESTORE_ATTEMPTS=32
MAX_RESTORE_SQL_STEPS=4000000
ID=re.compile(r'graph-[0-9a-f]{24}')
KEY=re.compile(r'[a-z][a-z0-9_-]{0,31}')
GRAPH_COLUMNS='id,owner,idem_key,fingerprint,state,retry_after,record'


def graph_columns():
    # Refuse an oversized record inside SQLite, before materializing it in Python.
    return GRAPH_COLUMNS.rsplit(',',1)[0]+',CASE WHEN length(CAST(record AS BLOB))<='+str(MAX_RECORD_BYTES)+' THEN record END'


def _checkpoint(stage):pass


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def validate(value):
    if not isinstance(value,dict) or set(value)-{'schema','name','idempotency_key','nodes'} or value.get('schema','ascend-lab-job-graph/v1')!='ascend-lab-job-graph/v1':
        raise JobError('Invalid task graph request')
    try:raw=canonical(value).encode()
    except (ValueError,TypeError,OverflowError):raise JobError('Task graph must be finite JSON') from None
    if len(raw)>MAX_BYTES:raise JobError('Task graph exceeds32KiB')
    key=value.get('idempotency_key');name=value.get('name','Task graph');nodes=value.get('nodes')
    if not isinstance(key,str) or not 1<=len(key)<=120 or any(ord(c)<32 for c in key):raise JobError('Task graph requires one bounded idempotency key')
    if not isinstance(name,str) or not 1<=len(name)<=100 or any(ord(c)<32 for c in name):raise JobError('Invalid task graph name')
    if not isinstance(nodes,list) or not 1<=len(nodes)<=MAX_NODES:raise JobError('Task graph requires1..16 nodes')
    result={};parents={}
    for node in nodes:
        if not isinstance(node,dict) or set(node)-{'key','job','after','artifact_inputs'} or not isinstance(node.get('key'),str) or not KEY.fullmatch(node['key']):raise JobError('Invalid graph node key or fields')
        label=node['key'];body=node.get('job');after=node.get('after',[]);imports=node.get('artifact_inputs',[])
        if label in result:raise JobError('Duplicate graph node key')
        if (not isinstance(body,dict) or body.get('template') not in WORKFLOWS or body.get('environment')!='worker-python'
            or 'idempotency_key' in body):raise JobError('Graph nodes require ordinary managed workflows and server-owned job keys')
        if any(key in body and not isinstance(body[key],list) for key in ['depends_on','artifact_inputs']):raise JobError('Invalid external graph dependency fields')
        if not isinstance(after,list) or len(after)>8 or any(not isinstance(v,str) or not KEY.fullmatch(v) for v in after) or len(set(after))!=len(after):raise JobError('Invalid graph local dependency list')
        if not isinstance(imports,list) or len(imports)>32:raise JobError('Invalid graph local artifact inputs')
        clean=[]
        for item in imports:
            if (not isinstance(item,dict) or set(item)-{'node','name','target','sha256','executable','when'}
                or not {'node','name','target'}<=set(item) or not isinstance(item['node'],str) or item['node'] not in after):raise JobError('Local artifact input must name a declared local parent')
            clean.append(copy.deepcopy(item))
        result[label]={'key':label,'job':copy.deepcopy(body),'after':sorted(after),
                       'artifact_inputs':sorted(clean,key=lambda x:canonical(x))}
        parents[label]=set(after)
    if any(not p<=set(result) for p in parents.values()):raise JobError('Graph parent does not exist')
    order=[];remaining=dict(parents)
    while remaining:
        ready=sorted(label for label,after in remaining.items() if not after-set(order))
        if not ready:raise JobError('Task graph contains a cycle')
        order.extend(ready)
        for label in ready:remaining.pop(label)
    return {'schema':'ascend-lab-job-graph/v1','name':name,'idempotency_key':key,'nodes':[result[label] for label in order]}


def fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def directory(path):
    try:path.mkdir(mode=0o700)
    except FileExistsError:pass
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid():raise JobError('Graph archive directory identity differs',409)


def write_once(path,raw):
    """Only a known uncommitted preparation temporary may be replaced."""
    def existing(candidate):
        fd=os.open(candidate,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():raise JobError('Graph archive file identity differs',409)
            return source.read(len(raw)+1)
    try:old=existing(path)
    except FileNotFoundError:old=None
    if old is not None:
        if old!=raw:raise JobError('Existing graph archive differs from the original snapshot',409)
        return
    temporary=path.with_name('.graph-'+path.name+'.tmp')
    try:existing(temporary)
    except FileNotFoundError:pass
    else:temporary.unlink()
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as output:output.write(raw);output.flush();os.fsync(output.fileno())
    os.replace(temporary,path);fsync_directory(path.parent)


def decode_graph(row):
    """Check both SQL dispatch fields and the bounded JSON authority."""
    try:
        identifier,owner,key,fingerprint,state,retry,raw=row
        if not isinstance(raw,str) or len(raw.encode())>MAX_RECORD_BYTES:raise ValueError()
        value=json.loads(raw)
        def text(item,limit):return isinstance(item,str) and 1<=len(item)<=limit and all(ord(c)>=32 for c in item)
        if (not isinstance(value,dict) or not isinstance(identifier,str) or not ID.fullmatch(identifier)
            or not text(owner,128) or not text(key,120) or not isinstance(fingerprint,str)
            or not re.fullmatch('[a-f0-9]{64}',fingerprint)
            or state not in {'preparing','submitted','needs_attention'}
            or (value.get('id'),value.get('owner'),value.get('state'))!=(identifier,owner,state)):
            raise ValueError()
        delay=value.get('retry_after',0)
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in (retry,delay)) or retry!=delay:raise ValueError()
        nodes=value.get('jobs');experiment=value.get('experiment_id');failures=value.get('preparation_failures',0)
        if (not isinstance(nodes,dict) or not 1<=len(nodes)<=MAX_NODES
            or any(not isinstance(k,str) or not KEY.fullmatch(k) or not isinstance(v,str) or not re.fullmatch('job-[a-f0-9]{12}',v) for k,v in nodes.items())
            or len(set(nodes.values()))!=len(nodes)
            or experiment is not None and (not isinstance(experiment,str) or not re.fullmatch('experiment-[a-f0-9]{24}',experiment))
            or not text(value.get('name'),100) or not text(value.get('created_at'),64)
            or isinstance(failures,bool) or not isinstance(failures,int) or not 0<=failures<2**63):raise ValueError()
        if 'prepared_at' in value and not text(value['prepared_at'],64):raise ValueError()
        if value.get('reason') is not None and not text(value['reason'],2048):raise ValueError()
        canonical(value)  # Reject nonfinite values even in extension metadata.
        return value
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
        raise JobError('Task graph SQL/JSON record is invalid; original tasks retained',409) from None


def bound_stage(graph,label,job):
    return (job.get('id')==graph['jobs'].get(label) and job.get('owner')==graph['owner']
            and job.get('_graph_id')==graph['id'] and job.get('experiment_id')==graph.get('experiment_id')
            and job.get('submission_graph')=={'id':graph['id'],'node':label})


def _restore_graph(db,row,spend):
    """One compact digest per stage, at most one hydrated private copy in memory."""
    from storage_files import encode
    from control_rotation import read as read_job, row_record
    from cooperative_start import DEFERRED
    from jobs import ACTIVE,TERMINAL
    graph=decode_graph(row);spend(row[-1])
    groups={}
    for label,identifier in graph['jobs'].items():
        hint=row_record(db,identifier)
        groups.setdefault(hint['export_id'] if hint else '',[]).append((label,identifier))
    cache={};stages={}
    try:
        for members in groups.values():
            for label,identifier in members:
                task=db.execute('SELECT id,owner,idem_key,fingerprint,state,CASE WHEN length(CAST(data AS BLOB))<=? THEN data END FROM jobs WHERE id=?',
                                (MAX_JOB_BYTES,identifier)).fetchone()
                if task is None or task[-1] is None:raise ValueError('Missing or oversized graph task')
                spend(task[-1]);job=read_job(db,task[-1],cache=cache)
                if ((job.get('id'),job.get('owner'),job.get('state'))!=(task[0],task[1],task[4])
                    or not bound_stage(graph,label,job) or graph['state']=='submitted' and '_graph_payload' in job):raise ValueError('Graph stage binding differs')
                attempts=list(db.execute('SELECT id,job_id,node_id,state,created_at FROM execution_attempts WHERE job_id=? ORDER BY id LIMIT ?',
                                         (identifier,MAX_RESTORE_ATTEMPTS+1)))
                if len(attempts)>MAX_RESTORE_ATTEMPTS:raise ValueError('Graph attempt audit exceeds finite bound')
                if len({a[0] for a in attempts})!=len(attempts):raise ValueError('Duplicate graph attempt')
                current=job.get('attempt_id');found=False
                for attempt in attempts:
                    if (any(not isinstance(v,str) or not 1<=len(v)<=128 for v in attempt)
                        or attempt[1]!=identifier or attempt[3] not in ACTIVE|TERMINAL|{DEFERRED}):raise ValueError('Invalid graph attempt')
                    spend(canonical(attempt))
                    if attempt[0]==current:
                        found=True
                        if (attempt[2],attempt[3])!=(job.get('node_id'),job['state']):raise ValueError('Current graph attempt differs')
                    elif attempt[3] in ACTIVE:raise ValueError('Unbound active graph attempt')
                if current is not None and not found:raise ValueError('Current graph attempt is missing')
                # Same semantic encoding as cold_reads.semantic. Count hydrated
                # bytes too, so compact SQL cannot hide unlimited snapshot work.
                facts=encode({k:v for k,v in job.items() if k not in {'archive_dir','archive_status'}})
                spend(facts)
                stages[identifier]=hashlib.sha256(canonical([list(task[:5]),hashlib.sha256(facts).hexdigest(),attempts]).encode()).hexdigest()
                del facts,job
    finally:cache.clear()
    return (tuple(row[:6]),canonical(graph),stages)


def require_restore_history(current,staged,role):
    """Validate staged bindings, then preserve all surviving graph/attempt facts."""
    if role!='controller':return
    from deployment import DeployError
    remaining=MAX_RESTORE_BYTES
    def spend(raw):
        nonlocal remaining
        remaining-=len(raw.encode() if isinstance(raw,str) else raw)
        if remaining<0:raise ValueError('Graph restore audit exceeds byte budget')
    with ExitStack() as stack:
        def open_db(directory):
            path=Path(directory)/'jobs.sqlite3'
            if not path.exists():return None
            db=stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)))
            steps=0
            def progress():
                nonlocal steps
                steps+=1000
                return steps>MAX_RESTORE_SQL_STEPS
            db.set_progress_handler(progress,1000);db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_graphs'").fetchone():return None
            return db
        try:
            old=open_db(current);new=open_db(staged)
            for db,other in ((old,new),(new,old)):
                if db is None:continue
                count=0
                for row in db.execute('SELECT '+graph_columns()+' FROM job_graphs ORDER BY id LIMIT ?',(MAX_GRAPHS+1,)):
                    count+=1
                    if count>MAX_GRAPHS:raise ValueError('Graph restore audit exceeds row budget')
                    # Existing staged graphs are audited during comparison above.
                    # Audit extra staged graphs as well, including a fresh restore.
                    if db is new and other is not None and other.execute('SELECT 1 FROM job_graphs WHERE id=?',(row[0],)).fetchone():continue
                    original=_restore_graph(db,row,spend)
                    if db is old:
                        target=other.execute('SELECT '+graph_columns()+' FROM job_graphs WHERE id=?',(row[0],)).fetchone() if other is not None else None
                        if target is None or original[:2]!=(tuple(target[:6]),canonical(decode_graph(target))):
                            raise DeployError('Retained graph admission keys or metadata would be lost or rewound')
                        if original!=_restore_graph(other,target,spend):
                            raise DeployError('Retained graph tasks or original attempts would be lost or rewound')
        except DeployError:raise
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,sqlite3.Error,OSError):
            raise DeployError('Graph restore audit failed: invalid binding, original attempts, or finite audit budget; state retained') from None


class Graphs:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS job_graphs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,idem_key TEXT NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,retry_after REAL NOT NULL,record TEXT NOT NULL,UNIQUE(owner,idem_key))')
            db.execute('CREATE INDEX IF NOT EXISTS graph_prepare ON job_graphs(state,retry_after)')

    def load(self,identifier,db,principal=None):
        if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise JobError('Task graph not found',404)
        row=db.execute('SELECT '+graph_columns()+' FROM job_graphs WHERE id=?',(identifier,)).fetchone()
        if row is None:raise JobError('Task graph not found',404)
        if principal and row[1]!=principal.id:raise JobError('Task graph not found',404)
        value=decode_graph(row)
        if principal and (value['owner']!=principal.id or principal.experiment_id and principal.experiment_id!=value['experiment_id']):raise JobError('Task graph not found',404)
        return value

    def save(self,db,value):
        row=db.execute('SELECT '+graph_columns()+' FROM job_graphs WHERE id=?',(value['id'],)).fetchone()
        old=decode_graph(row)
        if (any(old.get(k)!=value.get(k) for k in ['id','owner','name','created_at','jobs','experiment_id'])
            or old['state']=='submitted' and value.get('state')!='submitted'):
            raise JobError('Original graph identity or published state cannot change',409)
        decode_graph((*row[:4],value['state'],value.get('retry_after',0),canonical(value)))
        db.execute('UPDATE job_graphs SET state=?,retry_after=?,record=? WHERE id=?',
            (value['state'],value.get('retry_after',0),json.dumps(value,ensure_ascii=False),value['id']))

    def reason(self,job,db=None):
        identifier=job.get('_graph_id')
        if not identifier:
            return '任务图原任务绑定缺失，保留原任务等待核对' if 'submission_graph' in job or '_graph_payload' in job else None
        if db is None:
            with self.jobs.connect() as connection:return self.reason(job,connection)
        try:value=self.load(identifier,db)
        except (JobError,ValueError):return '任务图记录缺失，保留原任务等待核对'
        binding=job.get('submission_graph')
        if not isinstance(binding,dict) or not isinstance(binding.get('node'),str) or not bound_stage(value,binding['node'],job):return '任务图原任务绑定不符，保留原任务等待核对'
        if value['state']!='submitted':return '任务图归档准备未完成，尚未允许任何阶段执行'
        if '_graph_payload' in job:return '任务图阶段仍有未完成归档，保留原任务等待核对'
        return None

    def public(self,value,db,principal):
        principal.require('jobs:read')
        nodes=[]
        for label,ident in value['jobs'].items():
            job=self.jobs.get(ident,db,principal)
            nodes.append({'key':label,'job_id':ident,'state':job['state'],'reason':job['reason']})
        return {**{key:value[key] for key in ['id','name','owner','created_at','state','experiment_id']},
                'nodes':nodes,'preparation_failures':value.get('preparation_failures',0),
                'reason':value.get('reason'),'atomic_admission':True,'atomic_execution':False}

    def get(self,identifier,principal):
        principal.require('jobs:read')
        with self.jobs.connect() as db:return self.public(self.load(identifier,db,principal),db,principal)

    def submit(self,request,principal):
        principal.require('jobs:submit');principal.require('jobs:read')
        from unit_start import admission_reason
        reason=admission_reason(self.jobs.runtime,submit=True)
        if reason:raise JobError(reason,503)
        clean=validate(request);fingerprint=hashlib.sha256(canonical(clean).encode()).hexdigest()
        with self.jobs.transaction() as db:
            reason=admission_reason(self.jobs.runtime,submit=True)
            if reason:raise JobError(reason,503)
            old=db.execute('SELECT id,fingerprint FROM job_graphs WHERE owner=? AND idem_key=?',(principal.id,clean['idempotency_key'])).fetchone()
            if old:
                if old[1]!=fingerprint:raise JobError('Graph idempotency key already belongs to another request',409)
                value=self.load(old[0],db,principal);reused=True
            else:
                if db.execute('SELECT count(*) FROM job_graphs').fetchone()[0]>=MAX_GRAPHS:raise JobError('Task graph history is full; retained evidence was not removed',409)
                identifier='graph-'+uuid.uuid4().hex[:24]
                mapping={node['key']:'job-'+uuid.uuid4().hex[:12] for node in clean['nodes']}
                experiments=set()
                for node in clean['nodes']:
                    body=copy.deepcopy(node['job']);body['idempotency_key']=identifier+':'+node['key']
                    body.setdefault('name',clean['name'][:60]+' / '+node['key'])
                    body['depends_on']=[*body.get('depends_on',[]),*(mapping[p] for p in node['after'])]
                    body['artifact_inputs']=[*body.get('artifact_inputs',[]),*[{**{k:v for k,v in item.items() if k!='node'},'job_id':mapping[item['node']]} for item in node['artifact_inputs']]]
                    spec=self.jobs.normalize(body,principal,_db=db)
                    experiments.add(spec.get('experiment_id'))
                    if len(experiments)>1:raise JobError('All graph stages must use the same experiment budget')
                    spec.update(_graph_id=identifier,submission_graph={'id':identifier,'node':node['key']})
                    _,duplicate=self.jobs._submit_record(db,None,spec,principal,_defer_archive=True,_job_id=mapping[node['key']])
                    if duplicate:raise JobError('Graph task key conflicts with an existing task',409)
                    _checkpoint('admitted:'+node['key'])
                value={'id':identifier,'name':clean['name'],'owner':principal.id,'created_at':now(),'state':'preparing',
                    'jobs':mapping,'experiment_id':next(iter(experiments)),'preparation_failures':0,'retry_after':0}
                db.execute('INSERT INTO job_graphs VALUES(?,?,?,?,?,?,?)',(identifier,principal.id,clean['idempotency_key'],fingerprint,'preparing',0,json.dumps(value,ensure_ascii=False)))
                _checkpoint('before_admission_commit');reused=False
        _checkpoint('admission_committed')
        self.materialize(value['id'])
        return self.get(value['id'],principal),reused

    def materialize(self,identifier,force=False):
        try:
            with self.jobs.transaction() as db:
                value=self.load(identifier,db)
                if value['state']=='submitted':return
                if not force and (value['state']=='needs_attention' or value.get('retry_after',0)>time.time()):return
                for label,ident in value['jobs'].items():
                    job=self.jobs.get(ident,db);pending=job.get('_graph_payload')
                    if not isinstance(pending,dict) or not bound_stage(value,label,job) or job['state']!='queued' or job.get('attempt_id'):
                        raise JobError('Graph preparation binding or original queued task changed',409)
                    path=self.jobs.directory(ident,owner=value['owner'])
                    if not path.exists():
                        directory(path);self.jobs.payload.object(path,value['owner'],create=True);fsync_directory(path.parent)
                    else:directory(path)
                    source=pending['source']
                    if source is not None:
                        if not isinstance(source,dict) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.py',source['name']):raise JobError('Invalid saved fixed launcher snapshot',409)
                        raw=base64.b64decode(source['base64'],validate=True)
                        if len(raw)>4*1024**2 or hashlib.sha256(raw).hexdigest()!=job['source']['sha256']:raise JobError('Saved launcher snapshot hash differs',409)
                        directory(path/'source');write_once(path/'source'/source['name'],raw)
                    raw=(json.dumps(pending['request'],ensure_ascii=False,indent=2)+'\n').encode()
                    write_once(path/'request.json',raw);fsync_directory(path)
                    _checkpoint('archive:'+label)
                _checkpoint('before_publish')
                for ident in value['jobs'].values():
                    job=self.jobs.get(ident,db);job.pop('_graph_payload');self.jobs.write(db,job)
                value.update(state='submitted',prepared_at=now(),retry_after=0,reason=None)
                self.save(db,value)
            _checkpoint('published')
        except (JobError,OSError,ValueError,KeyError,TypeError) as error:
            with self.jobs.transaction() as db:
                value=self.load(identifier,db)
                if value['state']=='submitted':return
                failures=value.get('preparation_failures',0)+1
                value.update(state='needs_attention' if failures>=MAX_PREPARE_RETRIES else 'preparing',
                    preparation_failures=failures,retry_after=time.time()+5,
                    reason='Graph archives are not confirmed ready; original tasks and reservations retained ('+type(error).__name__+')')
                self.save(db,value)

    def resume(self,identifier,principal):
        principal.require('jobs:submit')
        with self.jobs.connect() as db:self.load(identifier,db,principal)
        self.materialize(identifier,force=True)
        return self.get(identifier,principal)

    def recover(self):
        with self.jobs.connect() as db:
            row=db.execute("SELECT id FROM job_graphs WHERE state='preparing' AND retry_after<=? ORDER BY rowid LIMIT 1",(time.time(),)).fetchone()
        if row:
            try:self.materialize(row[0])
            except sqlite3.Error:pass  # Original pending record remains authoritative.

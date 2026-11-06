"""Atomic bounded DAG admission; committed preparation never starts half a graph.

All ordinary job/account/project/experiment/storage gates are reused in one
SQLite transaction. Fixed launcher snapshots are materialized only after that
commit. A durable preparing record blocks dispatch and resumes the same IDs.
"""
import base64
import copy
import hashlib
import json
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
ID=re.compile(r'graph-[0-9a-f]{24}')
KEY=re.compile(r'[a-z][a-z0-9_-]{0,31}')


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


def require_restore_history(current,staged,role):
    """A surviving controller must not forget committed graph keys or attempts."""
    if role!='controller':return
    from deployment import DeployError
    def read(directory):
        path=Path(directory)/'jobs.sqlite3'
        if not path.exists():return {},{},{}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='job_graphs'").fetchone():return {},{},{}
            graphs={row[0]:row for row in db.execute('SELECT id,owner,idem_key,fingerprint,state,record FROM job_graphs')}
            jobs={};attempts={}
            for graph in graphs.values():
                value=json.loads(graph[-1])
                for ident in value['jobs'].values():
                    row=db.execute('SELECT fingerprint,data FROM jobs WHERE id=?',(ident,)).fetchone()
                    if not row:raise DeployError('Retained graph task is missing')
                    from control_rotation import read as read_job
                    job=read_job(db,row[1]);jobs[ident]=(row[0],job['owner'],job['state'],job.get('attempt_id'),job.get('_graph_id'))
                    attempts[ident]=list(db.execute('SELECT id,node_id,state,created_at FROM execution_attempts WHERE job_id=? ORDER BY id',(ident,)))
            return graphs,jobs,attempts
    old,jobs,attempts=read(current);new,other,other_attempts=read(staged)
    for ident,row in old.items():
        if ident not in new or row[:5]!=new[ident][:5] or json.loads(row[-1])['jobs']!=json.loads(new[ident][-1])['jobs']:
            raise DeployError('Retained graph admission keys or stage IDs would be lost or rewound')
    if any(other.get(ident)!=row or other_attempts.get(ident)!=attempts[ident] for ident,row in jobs.items()):
        raise DeployError('Retained graph tasks or original attempts would be lost or rewound')


class Graphs:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS job_graphs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,idem_key TEXT NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,retry_after REAL NOT NULL,record TEXT NOT NULL,UNIQUE(owner,idem_key))')
            db.execute('CREATE INDEX IF NOT EXISTS graph_prepare ON job_graphs(state,retry_after)')

    def load(self,identifier,db,principal=None):
        if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise JobError('Task graph not found',404)
        row=db.execute('SELECT record FROM job_graphs WHERE id=?',(identifier,)).fetchone()
        if row is None:raise JobError('Task graph not found',404)
        value=json.loads(row[0])
        if principal and (value['owner']!=principal.id or principal.experiment_id and principal.experiment_id!=value['experiment_id']):raise JobError('Task graph not found',404)
        return value

    def save(self,db,value):
        db.execute('UPDATE job_graphs SET state=?,retry_after=?,record=? WHERE id=?',
            (value['state'],value.get('retry_after',0),json.dumps(value,ensure_ascii=False),value['id']))

    def reason(self,job,db=None):
        identifier=job.get('_graph_id')
        if not identifier:return None
        if db is None:
            with self.jobs.connect() as connection:return self.reason(job,connection)
        try:value=self.load(identifier,db)
        except (JobError,ValueError):return '任务图记录缺失，保留原任务等待核对'
        if value['owner']!=job['owner'] or value['jobs'].get(job.get('submission_graph',{}).get('node'))!=job['id']:return '任务图原任务绑定不符，保留原任务等待核对'
        if value['state']!='submitted':return '任务图归档准备未完成，尚未允许任何阶段执行'
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
                    if not isinstance(pending,dict) or job.get('_graph_id')!=identifier or job['owner']!=value['owner'] or job['state']!='queued' or job.get('attempt_id'):
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

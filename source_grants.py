"""Explicit, file-scoped handoff between an owner's experiment budgets.

These records authorize the controller's dependency copier. They never grant
the delegated principal access to the producer job, logs or archive endpoints.
"""
import hashlib
import json
import math
import os
import re
import time
import uuid

from archive_api import open_artifact,relative_name
from dependency_conditions import confirmation
from experiment_budget import identity as budget_identity,select
from jobs import JobError
from node_storage import manifest_sha256
from report_view import parse,raw_file
from workflow_spec import WORKFLOWS

CAPABILITY='owner-source-grants-v1'
PATTERN=re.compile(r'source-grant-[0-9a-f]{24}')
MAX_PER_OWNER=1000
MAX_LIFETIME=86400
MAX_PLAN=32000


def identity(value):
    if not isinstance(value,str) or not PATTERN.fullmatch(value):raise JobError('Invalid source grant ID')
    return value


def names(value):
    if not isinstance(value,list) or not 1<=len(value)<=32:raise JobError('Choose one to 32 explicit source files')
    out=[]
    for item in value:
        if (not isinstance(item,dict) or set(item)-{'name','executable'} or 'name' not in item
            or not relative_name(item['name']) or len(item['name'].encode())>512
            or not item['name'].startswith(('results/','ir/','profile/'))
            or type(item.get('executable',False)) is not bool):raise JobError('Invalid source grant file selection')
        out.append({'name':item['name'],'executable':item.get('executable',False)})
    if len({x['name'] for x in out})!=len(out):raise JobError('Duplicate source grant file')
    return sorted(out,key=lambda x:x['name'])


class SourceGrants:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS source_grants(
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,idem_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,data TEXT NOT NULL,source_job_id TEXT NOT NULL,
                expires_at REAL NOT NULL,state TEXT NOT NULL,UNIQUE(owner,idem_key))''')
            db.execute('CREATE INDEX IF NOT EXISTS source_grant_pin ON source_grants(source_job_id,state,expires_at)')

    def approver(self,principal):
        principal.require('jobs:submit')
        if principal.experiment_id or principal.legacy or principal.admin:
            raise JobError('Use an ordinary personal account to approve source files',403)

    def get(self,db,key,principal):
        identity(key)
        row=db.execute('SELECT owner,data FROM source_grants WHERE id=?',(key,)).fetchone()
        if not row or row[0]!=principal.id:raise JobError('Source grant not found',404)
        value=json.loads(row[1])
        if principal.experiment_id and principal.experiment_id!=value['destination_experiment_id']:
            raise JobError('Source grant not found',404)
        return {**value,'usable':value['state']=='active' and value['expires_at']>time.time()}

    def list(self,principal):
        principal.require('jobs:read')
        with self.jobs.connect() as db:
            # Bound the query as well as the response, including historical grants.
            rows=db.execute('SELECT id,data FROM source_grants WHERE owner=? ORDER BY rowid DESC LIMIT ?',
                (principal.id,MAX_PER_OWNER)).fetchall()
            ids=[key for key,raw in rows if not principal.experiment_id or
                json.loads(raw)['destination_experiment_id']==principal.experiment_id][:100]
            return {'grants':[self.get(db,key,principal) for key in ids],'limit':100,'capability':CAPABILITY}

    def source(self,db,key,owner,files):
        if not isinstance(key,str) or not re.fullmatch('job-[0-9a-f]{12}',key):raise JobError('Source job not found',404)
        parent=self.jobs.get(key,db)
        if parent['owner']!=owner:raise JobError('Source job not found',404)
        if parent.get('template') not in WORKFLOWS or parent.get('_environment',{}).get('type')!='worker':
            raise JobError('Source must be a managed versioned workflow',409)
        self.jobs.lifecycle.available('archive',key,db)
        try:
            proof,reason=confirmation(parent)
            if reason:raise ValueError(reason)
            directory=self.jobs.directory(key)
            hashes=parse(raw_file(directory,'artifact-hashes.json',2*1024**2))
            if (not isinstance(hashes,dict) or hashes.get('version')!=1 or not isinstance(hashes.get('files'),list)
                or len(hashes['files'])>2000 or manifest_sha256(hashes['files'])!=proof['artifact_manifest_sha256']):
                raise ValueError('Confirmed source manifest differs')
            index=parse(raw_file(directory,'artifacts.json',2*1024**2))
            entries=index.get('files') if isinstance(index,dict) else None
            if not isinstance(entries,list) or len(entries)>2000:raise ValueError('Invalid source archive index')
            indexed=[x.get('name') if isinstance(x,dict) else None for x in entries]
            if any(not relative_name(x) for x in indexed) or len(set(indexed))!=len(indexed):raise ValueError('Invalid source archive index')
            selected=[]
            for item in files:
                matched=[x for x in hashes['files'] if x.get('name')==item['name']]
                if len(matched)!=1 or item['name'] not in indexed:raise ValueError('Source file is not confirmed and indexed')
                entry=matched[0]
                if (type(entry.get('size')) is not int or not 0<=entry['size']<=self.jobs.inputs.MAX_FILE
                    or not isinstance(entry.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',entry['sha256'])):
                    raise ValueError('Invalid source file receipt')
                with open_artifact(directory,item['name'],self.jobs.inputs.MAX_FILE) as stream:
                    if os.fstat(stream.fileno()).st_size!=entry['size']:raise ValueError('Source file size changed')
                selected.append({**item,'size':entry['size'],'sha256':entry['sha256']})
        except (ValueError,KeyError,TypeError,RecursionError) as error:
            raise JobError('Source confirmation is unavailable or changed',409) from error
        return proof,selected

    def plan(self,principal,request,db=None,created=None):
        self.approver(principal)
        required={'source_job_id','destination_experiment_id','files','lifetime_seconds','idempotency_key'}
        if not isinstance(request,dict) or set(request)!=required:raise JobError('Provide explicit source, destination budget, files, lifetime and key')
        key=request['idempotency_key'];ttl=request['lifetime_seconds']
        if not isinstance(key,str) or not 1<=len(key)<=120 or type(ttl) is not int or not 1<=ttl<=MAX_LIFETIME:
            raise JobError('Invalid source grant key or lifetime')
        chosen=names(request['files']);budget_identity(request['destination_experiment_id'])
        if db is None:
            with self.jobs.connect() as connection:return self.plan(principal,request,connection,created)
        budget=self.jobs.experiment_budgets.get(db,request['destination_experiment_id'],principal)
        if not budget['accepts_new_jobs']:raise JobError('Destination experiment cannot accept new work',409)
        proof,selected=self.source(db,request['source_job_id'],principal.id,chosen)
        stamp=time.time() if created is None else created
        value={'schema':CAPABILITY,'owner':principal.id,'source_job_id':request['source_job_id'],
            'destination_experiment_id':budget['id'],'source':proof,'files':selected,
            'created_at':stamp,'expires_at':min(stamp+ttl,budget['expires_at']),
            'lifetime_seconds':ttl,'idempotency_key':key}
        if len(json.dumps(value).encode())>MAX_PLAN:raise JobError('Source grant plan exceeds interface budget',413)
        return value

    def create(self,principal,plan):
        self.approver(principal)
        required={'schema','owner','source_job_id','destination_experiment_id','source','files',
            'created_at','expires_at','lifetime_seconds','idempotency_key'}
        if not isinstance(plan,dict) or set(plan)!=required or plan['schema']!=CAPABILITY or plan['owner']!=principal.id:
            raise JobError('Approve the exact source grant preview')
        if (not isinstance(plan['idempotency_key'],str) or not 1<=len(plan['idempotency_key'])<=120
            or len(json.dumps(plan).encode())>MAX_PLAN):raise JobError('Invalid source grant plan or key')
        fingerprint=hashlib.sha256(json.dumps(plan,sort_keys=True,allow_nan=False).encode()).hexdigest()
        with self.jobs.transaction() as db:
            row=db.execute('SELECT id,fingerprint FROM source_grants WHERE owner=? AND idem_key=?',
                (principal.id,plan['idempotency_key'])).fetchone()
            if row:
                if row[1]!=fingerprint:raise JobError('Source grant key has a different original approval',409)
                return self.get(db,row[0],principal),True
            if db.execute('SELECT count(*) FROM source_grants WHERE owner=?',(principal.id,)).fetchone()[0]>=MAX_PER_OWNER:
                raise JobError('Owner source grant retention limit reached',409)
            stamp=plan['created_at'];expires=plan['expires_at']
            if (type(stamp) not in {int,float} or not math.isfinite(stamp) or stamp>time.time()+1
                or type(expires) not in {int,float} or not math.isfinite(expires) or expires<=time.time()):
                raise JobError('Source grant preview is expired or has an invalid time',409)
            files=plan['files']
            if not isinstance(files,list) or any(not isinstance(x,dict) or set(x)!={'name','executable','size','sha256'}
                or type(x['size']) is not int or not isinstance(x['sha256'],str) for x in files):
                raise JobError('Invalid frozen source file selection')
            request={k:plan[k] for k in ['source_job_id','destination_experiment_id','lifetime_seconds','idempotency_key']}
            request['files']=[{k:x[k] for k in ['name','executable']} for x in files]
            expected=self.plan(principal,request,db,stamp)
            if json.dumps(expected,sort_keys=True,allow_nan=False)!=json.dumps(plan,sort_keys=True,allow_nan=False):
                raise JobError('Source grant preview changed; review a fresh preview',409)
            from control_storage import require
            require(self.jobs.runtime,self.jobs.policy.get('control_storage'),self.jobs.control_filesystem)
            from audit_retention import require
            require(self.jobs.access)
            key='source-grant-'+uuid.uuid4().hex[:24]
            value={**plan,'id':key,'state':'active','approved_at':time.time()}
            db.execute('INSERT INTO source_grants VALUES(?,?,?,?,?,?,?,?)',
                (key,principal.id,plan['idempotency_key'],fingerprint,json.dumps(value,sort_keys=True),
                 value['source_job_id'],value['expires_at'],value['state']))
            return self.get(db,key,principal),False

    def revoke(self,principal,key):
        self.approver(principal)
        with self.jobs.transaction() as db:
            value=self.get(db,key,principal);value.pop('usable')
            if value['state']=='active':
                value.update(state='revoked',revoked_at=time.time())
                db.execute('UPDATE source_grants SET data=?,state=? WHERE id=?',(json.dumps(value,sort_keys=True),value['state'],key))
            return self.get(db,key,principal)

    def check(self,spec,db,*,active=True):
        """Validate selected imports; caller already established the true owner."""
        imports=[x for x in spec.get('artifact_inputs',[]) if 'source_grant' in x]
        if not imports:return
        owner=spec['owner'];destination=spec.get('experiment_id')
        for key in sorted({x['source_grant'] for x in imports}):
            row=db.execute('SELECT owner,data FROM source_grants WHERE id=?',(identity(key),)).fetchone()
            if not row or row[0]!=owner:raise JobError('Source grant not found',404)
            grant=json.loads(row[1])
            if not destination or grant['destination_experiment_id']!=destination:raise JobError('Source grant not found',404)
            if active and (grant['state']!='active' or grant['expires_at']<=time.time()):raise JobError('Source grant is revoked, expired or restored frozen',409)
            items=[x for x in imports if x['source_grant']==key];selected={x['name']:x for x in grant['files']}
            for item in items:
                file=selected.get(item['name'])
                if (item['job_id']!=grant['source_job_id'] or file is None
                    or item.get('sha256',file['sha256'])!=file['sha256']
                    or item.get('executable',False) and not file['executable']):raise JobError('Source grant does not authorize this file import',403)
            # Structural normalization permits replay after revocation/expiry.
            # New admissions and every unstarted execution recheck live proof.
            if active:
                proof,files=self.source(db,grant['source_job_id'],owner,[{k:x[k] for k in ['name','executable']} for x in grant['files']])
                if proof!=grant['source'] or files!=grant['files']:raise JobError('Source grant producer evidence changed',409)

    def pinned(self,parent,db):
        return db.execute('SELECT 1 FROM source_grants WHERE source_job_id=? AND state=? AND expires_at>? LIMIT 1',
            (parent,'active',time.time())).fetchone() is not None


def require_compatible(state):
    import sqlite3
    from pathlib import Path
    from deployment import DeployError
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_grants'").fetchone() and db.execute('SELECT 1 FROM source_grants LIMIT 1').fetchone():
            raise DeployError('Historical source grants require matching code; refusing downgrade')
        for raw, in db.execute('SELECT data FROM jobs'):
            if any('source_grant' in x for x in json.loads(raw).get('artifact_inputs',[])):
                raise DeployError('Source grant workflow history requires matching code; refusing downgrade')


def rebind_restored(state):
    import sqlite3
    from pathlib import Path
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_grants'").fetchone():return
        for key,raw in db.execute('SELECT id,data FROM source_grants').fetchall():
            value=json.loads(raw);value['state']='restored_frozen'
            value['restore_notice']='Review original source and approve a fresh grant to a new experiment; old authority is never restored.'
            db.execute('UPDATE source_grants SET data=?,state=? WHERE id=?',(json.dumps(value,sort_keys=True),value['state'],key))

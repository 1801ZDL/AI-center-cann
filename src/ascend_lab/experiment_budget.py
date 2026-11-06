"""Owner-approved cumulative admission envelopes; no runtime billing or refunds."""
import hashlib
import json
import re
import time
import uuid

from jobs import JobError

CAPABILITY='cumulative-experiment-budget-v1'
PATTERN=re.compile(r'experiment-[0-9a-f]{24}')
TEMPLATES={'cpu_demo','accuracy_v1','performance_v1','simulation_v1','build_v1','ir_v1'}
DEFAULT={'max_jobs':64,'max_cpu_seconds':1000000,'max_npu_seconds':86400,
         'max_lifetime_seconds':604800,'max_budgets_per_owner':1000}


def policy(value):
    if value is None:return dict(DEFAULT)
    if not isinstance(value,dict) or set(value)-set(DEFAULT):raise ValueError('Invalid experiment budget policy')
    result={**DEFAULT,**value}
    bounds={'max_jobs':(1,10000),'max_cpu_seconds':(1,10**12),'max_npu_seconds':(0,10**12),
            'max_lifetime_seconds':(1,30*86400),'max_budgets_per_owner':(1,10000)}
    if any(type(result[k]) is not int or not low<=result[k]<=high for k,(low,high) in bounds.items()):
        raise ValueError('Invalid experiment budget policy limits')
    return result


def identity(value):
    if not isinstance(value,str) or not PATTERN.fullmatch(value):raise JobError('Invalid experiment budget ID')
    return value


def select(request,principal):
    bound=principal.experiment_id
    chosen=request.get('experiment_id',bound)
    if bound and chosen!=bound:raise JobError('Credential is bound to another experiment budget',403)
    if chosen is not None:identity(chosen)
    return {'experiment_id':chosen} if chosen is not None else {}


class Budgets:
    def __init__(self,jobs):
        self.jobs=jobs;self.policy=policy(jobs.policy.get('experiment_budget'))
        with jobs.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS experiment_budgets(
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,idem_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(owner,idem_key));
                CREATE TABLE IF NOT EXISTS experiment_charges(
                job_id TEXT PRIMARY KEY,budget_id TEXT NOT NULL,
                cpu_seconds INTEGER NOT NULL CHECK(cpu_seconds>=0),
                npu_seconds INTEGER NOT NULL CHECK(npu_seconds>=0));
                CREATE INDEX IF NOT EXISTS experiment_charge_budget ON experiment_charges(budget_id);''')

    def approver(self,principal):
        principal.require('jobs:submit')
        if principal.experiment_id or principal.legacy or principal.admin:
            raise JobError('Use an ordinary personal account to approve experiment budgets',403)

    def get(self,db,budget_id,principal):
        identity(budget_id)
        if principal.experiment_id and principal.experiment_id!=budget_id:raise JobError('Experiment budget not found',404)
        row=db.execute('SELECT owner,data FROM experiment_budgets WHERE id=?',(budget_id,)).fetchone()
        if not row or row[0]!=principal.id:raise JobError('Experiment budget not found',404)
        value=json.loads(row[1]);used=db.execute('SELECT count(*),COALESCE(sum(cpu_seconds),0),COALESCE(sum(npu_seconds),0) FROM experiment_charges WHERE budget_id=?',(budget_id,)).fetchone()
        value['charged']=dict(zip(['jobs','cpu_seconds','npu_seconds'],used))
        value['remaining']={key:value['limits'][key]-value['charged'][key] for key in value['limits']}
        if any(x<0 for x in value['remaining'].values()):raise JobError('Experiment budget ledger is inconsistent',503)
        value['accepts_new_jobs']=value['state']=='open' and value['expires_at']>time.time() and value['remaining']['jobs']>0
        value['accounting']='Declared payload CPU/NPU count times timeout; no automatic refunds or actual runtime billing'
        return value

    def create(self,principal,request):
        self.approver(principal)
        if not isinstance(request,dict) or set(request)!={'name','idempotency_key','limits','templates','lifetime_seconds'}:raise JobError('Provide a named immutable experiment budget, templates, lifetime and key')
        name=request['name'];key=request['idempotency_key'];limits=request['limits'];templates=request['templates'];ttl=request['lifetime_seconds']
        if not isinstance(name,str) or not 1<=len(name)<=100 or not isinstance(key,str) or not 1<=len(key)<=120:raise JobError('Invalid experiment budget name or key')
        if not isinstance(limits,dict) or set(limits)!={'jobs','cpu_seconds','npu_seconds'}:raise JobError('Provide jobs/cpu_seconds/npu_seconds limits')
        for field in limits:
            minimum=0 if field=='npu_seconds' else 1
            if type(limits[field]) is not int or not minimum<=limits[field]<=self.policy['max_'+field]:raise JobError('Experiment budget exceeds administrator '+field+' limit')
        if not isinstance(templates,list) or not templates or len(templates)>len(TEMPLATES) or any(not isinstance(x,str) or x not in TEMPLATES for x in templates) or len(set(templates))!=len(templates):raise JobError('Choose registered experiment workflow templates')
        if type(ttl) is not int or not 1<=ttl<=self.policy['max_lifetime_seconds']:raise JobError('Invalid experiment budget lifetime')
        spec={**request,'templates':sorted(templates)};fingerprint=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
        with self.jobs.transaction() as db:
            row=db.execute('SELECT id,fingerprint FROM experiment_budgets WHERE owner=? AND idem_key=?',(principal.id,key)).fetchone()
            if row:
                if row[1]!=fingerprint:raise JobError('Experiment budget key has a different original request',409)
                return self.get(db,row[0],principal),True
            if db.execute('SELECT count(*) FROM experiment_budgets WHERE owner=?',(principal.id,)).fetchone()[0]>=self.policy['max_budgets_per_owner']:raise JobError('Owner experiment budget retention limit reached',409)
            # Reuse the same control/payload headroom guard before new records.
            from control_storage import require
            require(self.jobs.runtime,self.jobs.policy.get('control_storage'),self.jobs.control_filesystem)
            from audit_retention import require
            require(self.jobs.access)
            budget_id='experiment-'+uuid.uuid4().hex[:24];created=time.time()
            value={'id':budget_id,'owner':principal.id,'name':name,'limits':limits,'templates':spec['templates'],
                   'created_at':created,'expires_at':created+ttl,'state':'open','capability':CAPABILITY}
            db.execute('INSERT INTO experiment_budgets VALUES(?,?,?,?,?)',(budget_id,principal.id,key,fingerprint,json.dumps(value,sort_keys=True)))
            return self.get(db,budget_id,principal),False

    def list(self,principal):
        with self.jobs.connect() as db:
            ids=[principal.experiment_id] if principal.experiment_id else [r[0] for r in db.execute('SELECT id FROM experiment_budgets WHERE owner=? ORDER BY rowid DESC LIMIT 100',(principal.id,))]
            return {'budgets':[self.get(db,key,principal) for key in ids],'policy':self.policy,'limit':100,'capability':CAPABILITY}

    def summary(self,principal):
        value={'capability':CAPABILITY,'bound_id':principal.experiment_id}
        if principal.experiment_id:
            with self.jobs.connect() as db:value['budget']=self.get(db,principal.experiment_id,principal)
        return value

    def close(self,principal,budget_id):
        self.approver(principal)
        with self.jobs.transaction() as db:
            value=self.get(db,budget_id,principal);value={k:v for k,v in value.items() if k not in {'charged','remaining','accepts_new_jobs','accounting'}}
            value['state']='closed'
            db.execute('UPDATE experiment_budgets SET data=? WHERE id=?',(json.dumps(value,sort_keys=True),budget_id))
            return self.get(db,budget_id,principal)

    def delegate(self,principal,budget_id,*,read_only=False):
        self.approver(principal)
        if type(read_only) is not bool:raise JobError('Invalid experiment delegation mode')
        with self.jobs.transaction() as db:
            value=self.get(db,budget_id,principal)
            if not read_only and not value['accepts_new_jobs']:raise JobError('Experiment budget is closed, expired or exhausted',409)
            lifetime=86400 if read_only else min(86400,int(value['expires_at']-time.time()))
            if lifetime<1:raise JobError('Experiment budget expires too soon',409)
            from control_storage import require
            require(self.jobs.runtime,self.jobs.policy.get('control_storage'),self.jobs.control_filesystem)
            from audit_retention import require
            require(self.jobs.access)
            scopes={'jobs:read'} if read_only else {'jobs:read','jobs:submit','jobs:cancel'}
            token=self.jobs.access.issue_token(principal.id,scopes,lifetime,experiment_id=budget_id)
            return {'experiment_id':budget_id,'token':token,'lifetime_seconds':lifetime,'scopes':sorted(scopes)}

    def check(self,db,spec,principal):
        key=spec.get('experiment_id')
        if not key:return None
        value=self.get(db,key,principal)
        cost={'jobs':1,'cpu_seconds':spec['resources']['cpu']*spec['timeout_seconds'],
              'npu_seconds':spec['resources']['npu']*spec['timeout_seconds']}
        if not value['accepts_new_jobs']:raise JobError('Experiment budget is closed, expired or exhausted',409)
        if spec.get('template') not in value['templates'] or spec.get('environment')!='worker-python':raise JobError('Experiment budget does not approve this managed template',403)
        if any(cost[k]>value['remaining'][k] for k in cost):raise JobError('Experiment cumulative budget would be exceeded',409)
        return {'experiment_id':key,**cost}

    def charge(self,db,spec,principal,job_id):
        cost=self.check(db,spec,principal)
        if cost:db.execute('INSERT INTO experiment_charges VALUES(?,?,?,?)',(job_id,cost['experiment_id'],cost['cpu_seconds'],cost['npu_seconds']))
        return {'experiment_charge':cost} if cost else {}


def require_compatible(state):
    """Old authentication code would ignore a binding; preserve this barrier."""
    import sqlite3
    from pathlib import Path
    from deployment import DeployError
    for filename,table,condition in [('jobs.sqlite3','experiment_budgets','1'),
                                     ('identities.sqlite3','tokens','experiment_id IS NOT NULL')]:
        path=Path(state)/filename
        if not path.exists():continue
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
            if table=='tokens' and 'experiment_id' not in {r[1] for r in db.execute('PRAGMA table_info(tokens)')}:continue
            if db.execute('SELECT 1 FROM '+table+' WHERE '+condition+' LIMIT 1').fetchone():
                raise DeployError('Cumulative experiment budgets and bound credentials require matching code; refusing downgrade')


def rebind_restored(state):
    """A checkpoint may omit later spend: freeze approvals and revoke old tokens.

    Known charges remain history. No counter is replenished and no old delegated
    credential is allowed to spend the apparent remainder of a stale snapshot.
    """
    import sqlite3
    from pathlib import Path
    state=Path(state)
    if (state/'jobs.sqlite3').exists():
        with sqlite3.connect(state/'jobs.sqlite3') as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='experiment_budgets'").fetchone():
                for key,raw in list(db.execute('SELECT id,data FROM experiment_budgets')):
                    value=json.loads(raw);value['state']='restored_frozen'
                    value['restore_notice']='Checkpoint-known charges only; later spend may be absent. Separately approve any new execution.'
                    db.execute('UPDATE experiment_budgets SET data=? WHERE id=?',(json.dumps(value,sort_keys=True),key))
    if (state/'identities.sqlite3').exists():
        with sqlite3.connect(state/'identities.sqlite3') as db:
            if 'experiment_id' in {r[1] for r in db.execute('PRAGMA table_info(tokens)')}:
                db.execute('UPDATE tokens SET revoked=1 WHERE experiment_id IS NOT NULL AND revoked=0')

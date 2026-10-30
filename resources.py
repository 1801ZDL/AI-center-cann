"""One durable admission budget per physical host, shared by every execution entry."""
from __future__ import annotations

import json
import re

from jobs import ACTIVE,JobError,now

KEYS=('cpu_ids','memory_mib','shm_mib','max_running')
MULTI_SLOT='concurrent-attempts-v1'
MAX_SLOTS=32
MANAGED_OVERHEAD_MIB=128


def memory_cost(job,managed=None):
    managed=job.get('_remote',False) if managed is None else managed
    return job['resources']['memory_mib']+(MANAGED_OVERHEAD_MIB if managed else 0)


def require_managed_capacity(profile,domain,memory_mib=256):
    """Static one-CPU/zero-SHM envelope; existing reservations are irrelevant.

    A busy but capable target must queue normally. This checks current declared
    ceilings, not heartbeat freshness, device health or immediate headroom.
    """
    if type(memory_mib) is not int or not 256<=memory_mib<=65536:
        raise JobError('Fixed managed probe memory must be 256..65536 MiB',400)
    policy=validate_policy(profile['policy']);pool=validate_policy(domain['policy'])
    required=memory_cost({'resources':{'memory_mib':memory_mib}},True)
    if not set(policy['cpu_ids'])&set(pool['cpu_ids']):
        raise JobError('Selected Node and physical domain have no shared CPU capacity for a fixed probe',409)
    for name,limit in [('Node',policy['memory_mib']),('physical domain',pool['memory_mib'])]:
        if required>limit:
            raise JobError(f'Selected {name} memory budget {limit} MiB cannot fit {memory_mib} MiB payload + {MANAGED_OVERHEAD_MIB} MiB managed overhead = {required} MiB',409)


def validate_policy(policy):
    if not isinstance(policy,dict) or set(policy)!=set(KEYS):raise JobError('Resource policy requires CPU, memory, SHM and slot budgets')
    cpus=policy['cpu_ids']
    if (not isinstance(cpus,list) or not 1<=len(cpus)<=4096
        or any(type(cpu) is not int or not 0<=cpu<=1048575 for cpu in cpus) or len(set(cpus))!=len(cpus)):
        raise JobError('Invalid resource CPU list')
    if (type(policy['memory_mib']) is not int or not 256<=policy['memory_mib']<=2**30
        or type(policy['shm_mib']) is not int or not 0<=policy['shm_mib']<=policy['memory_mib']
        or type(policy['max_running']) is not int or not 1<=policy['max_running']<=min(MAX_SLOTS,len(cpus))):
        raise JobError('Resource budgets require valid memory/SHM and exclusive CPU slots')
    return {**policy,'cpu_ids':sorted(cpus)}


class ResourceDomains:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS resource_domains (
                physical_id TEXT PRIMARY KEY,policy TEXT NOT NULL,draining INTEGER NOT NULL,
                revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS resource_domain_events (
                id INTEGER PRIMARY KEY,physical_id TEXT NOT NULL,revision INTEGER NOT NULL,
                at TEXT NOT NULL,actor TEXT NOT NULL,data TEXT NOT NULL);
            ''')
        with jobs.transaction() as db:
            local=jobs.backend.physical_id
            if not db.execute('SELECT 1 FROM resource_domains WHERE physical_id=?',(local,)).fetchone():
                policy=validate_policy({key:jobs.policy[key] for key in KEYS})
                self._write(db,local,policy,False,1)

    def _write(self,db,physical_id,policy,draining,revision):
        at=now();data={'policy':policy,'draining':draining}
        db.execute('INSERT INTO resource_domains VALUES(?,?,?,?,?) ON CONFLICT(physical_id) DO UPDATE SET '
                   'policy=excluded.policy,draining=excluded.draining,revision=excluded.revision,updated_at=excluded.updated_at',
                   (physical_id,json.dumps(policy),int(draining),revision,at))
        db.execute('INSERT INTO resource_domain_events(physical_id,revision,at,actor,data) VALUES(?,?,?,?,?)',
                   (physical_id,revision,at,self.jobs.owner,json.dumps(data)))

    def get(self,physical_id,db=None):
        if db is None:
            with self.jobs.connect() as connection:return self.get(physical_id,connection)
        row=db.execute('SELECT policy,draining,revision,updated_at FROM resource_domains WHERE physical_id=?',(physical_id,)).fetchone()
        if not row:raise JobError('Physical resource domain must be registered by the administrator',409)
        return {'physical_id':physical_id,'policy':json.loads(row[0]),'draining':bool(row[1]),'revision':row[2],'updated_at':row[3]}

    def active(self,physical_id,rows):
        return [job for job in rows if job['state'] in ACTIVE and job.get('_physical_id',self.jobs.backend.physical_id)==physical_id]

    def configure(self,physical_id,policy,expected_revision,draining=False):
        if not isinstance(physical_id,str) or not re.fullmatch(r'[0-9a-f]{64}',physical_id):raise JobError('Invalid physical identity')
        policy=validate_policy(policy)
        if type(expected_revision) is not int or expected_revision<0 or type(draining) is not bool:raise JobError('A revision and boolean drain state are required')
        if physical_id==self.jobs.backend.physical_id:
            if (not set(policy['cpu_ids'])<=set(self.jobs.policy['cpu_ids'])
                or any(policy[key]>self.jobs.policy[key] for key in ['memory_mib','shm_mib'])):
                raise JobError('Local physical budget exceeds this controller deployment envelope')
        with self.jobs.transaction() as db:
            row=db.execute('SELECT revision FROM resource_domains WHERE physical_id=?',(physical_id,)).fetchone()
            if (row[0] if row else 0)!=expected_revision:raise JobError('Resource domain revision changed; inspect it before retrying',409)
            active=self.active(physical_id,self.jobs.all(db))
            if (len(active)>policy['max_running']
                or not {cpu for job in active for cpu in job['allocation'].get('cpu_ids',[])}<=set(policy['cpu_ids'])
                or sum(memory_cost(job) for job in active)>policy['memory_mib']
                or sum(job['resources'].get('shm_mib',0) for job in active)>policy['shm_mib']):
                raise JobError('New budget would invalidate existing reservations, including unknown attempts',409)
            self._write(db,physical_id,policy,draining,expected_revision+1)
            return self.get(physical_id,db)

    def validate_node(self,profile,db):
        domain=self.get(profile['physical_id'],db);policy=profile['policy'];pool=domain['policy']
        if (not set(policy['cpu_ids'])<=set(pool['cpu_ids'])
            or any(policy[key]>pool[key] for key in ['memory_mib','shm_mib','max_running'])):
            raise JobError('Node envelope exceeds the authoritative physical resource domain')

    def summary(self,db=None,rows=None):
        if db is None:
            with self.jobs.connect() as connection:return self.summary(connection,rows)
        rows=self.jobs.all(db) if rows is None else rows
        result=[]
        for row in db.execute('SELECT physical_id FROM resource_domains ORDER BY physical_id'):
            domain=self.get(row[0],db);active=self.active(row[0],rows)
            domain['reserved']={key:sum(job['resources'].get(key,0) for job in active) for key in ['cpu','memory_mib','shm_mib']}
            domain['reserved']['workload_memory_mib']=domain['reserved']['memory_mib']
            domain['reserved']['memory_mib']=sum(memory_cost(job) for job in active)
            domain['reserved']['execution_overhead_mib']=sum(memory_cost(job)-job['resources']['memory_mib'] for job in active)
            domain['reserved'].update(slots=len(active),cpu_ids=sorted({cpu for job in active for cpu in job['allocation'].get('cpu_ids',[])}))
            domain['unknown_attempts']=sum(job['state']=='needs_attention' for job in active)
            if hasattr(self.jobs,'inventory'):
                reason=self.jobs.inventory.reason(row[0],db)
                domain['admission']={'blocked':bool(reason),'reason':reason,
                    'inventory_required':bool(self.jobs.policy.get('require_attempt_inventory')),
                    'inventory_holds':len(self.jobs.inventory.holds(row[0],db))}
            result.append(domain)
        return result

    def events(self,physical_id):
        with self.jobs.connect() as db:
            return [{'revision':row[0],'at':row[1],'actor':row[2],**json.loads(row[3])} for row in db.execute(
                'SELECT revision,at,actor,data FROM resource_domain_events WHERE physical_id=? ORDER BY id DESC LIMIT 100',(physical_id,))]

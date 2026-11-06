"""Node-owned unresolved execution inventory and durable physical-domain quarantine.

This detects lost/mismatched controller records; it is not a multi-controller
consensus mechanism and cannot attest to a malicious Root node or deleted state.
"""
import hashlib
import json
import re
import time

CAPABILITY='attempt-inventory-v1'
HOLD_REASON='物理资源域存在未对账的节点执行记录，暂停新分配'
WAIT_REASON='等待此物理资源域所有启用节点向当前中央确认执行清单'


def validate(items):
    if not isinstance(items,list) or len(items)>64:raise ValueError('Invalid bounded attempt inventory')
    seen=set();result=[]
    for item in items:
        if (not isinstance(item,dict) or set(item)!={'job_id','attempt_id','lease_sha256','cleaned'}
            or not re.fullmatch('job-[0-9a-f]{12}',str(item['job_id']))
            or not re.fullmatch('attempt-[0-9a-f]{32}',str(item['attempt_id']))
            or not re.fullmatch('[0-9a-f]{64}',str(item['lease_sha256']))
            or type(item['cleaned']) is not bool or item['attempt_id'] in seen):raise ValueError('Invalid attempt inventory identity')
        seen.add(item['attempt_id']);result.append(dict(item))
    return sorted(result,key=lambda x:x['attempt_id'])


def local(root):
    from jobs import read_json
    items=[]
    for path in sorted((root/'attempts').glob('*/control/grant.json')):
        state=read_json(path.parent/'state.json') or {}
        if state.get('stage')=='cleaned' and state.get('controller_acknowledged'):continue
        grant=read_json(path)
        items.append({'job_id':grant['job_id'],'attempt_id':grant['attempt_id'],
          'lease_sha256':hashlib.sha256(grant['lease_token'].encode()).hexdigest(),'cleaned':state.get('stage')=='cleaned'})
    return validate(items)


class Inventory:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS worker_inventory_holds(
                node_id TEXT PRIMARY KEY,physical_id TEXT NOT NULL,revision INTEGER NOT NULL,
                held INTEGER NOT NULL,details TEXT NOT NULL,updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS worker_inventory_events(
                id INTEGER PRIMARY KEY,node_id TEXT NOT NULL,revision INTEGER NOT NULL,held INTEGER NOT NULL,
                at REAL NOT NULL,actor TEXT NOT NULL,details TEXT NOT NULL);''')

    def review(self,node_id,physical_id,items,db):
        from jobs import JobError,TERMINAL
        items=validate(items);unmatched=[]
        for item in items:
            try:job=self.jobs.get(item['job_id'],db)
            except JobError:job=None
            if (not job or job.get('node_id')!=node_id or job.get('attempt_id')!=item['attempt_id']
                or hashlib.sha256(job.get('_token','').encode()).hexdigest()!=item['lease_sha256']
                or job['state'] in TERMINAL and not item['cleaned']):
                unmatched.append({'job_id':item['job_id'],'attempt_id':item['attempt_id']})
        if unmatched:
            details=json.dumps(unmatched,sort_keys=True);row=db.execute('SELECT revision,held,details FROM worker_inventory_holds WHERE node_id=?',(node_id,)).fetchone()
            if not row or not row[1] or row[2]!=details:
                revision=(row[0] if row else 0)+1;at=time.time()
                db.execute('INSERT INTO worker_inventory_holds VALUES(?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET '
                    'physical_id=excluded.physical_id,revision=excluded.revision,held=1,details=excluded.details,updated_at=excluded.updated_at',
                    (node_id,physical_id,revision,1,details,at))
                db.execute('INSERT INTO worker_inventory_events(node_id,revision,held,at,actor,details) VALUES(?,?,?,?,?,?)',
                    (node_id,revision,1,at,'node-inventory',details))
        return {'reconciled':not unmatched,'pending':len(items),'unmatched':len(unmatched),
                'controller_instance':getattr(self.jobs,'controller_instance',None)}

    def holds(self,physical_id=None,db=None):
        if db is None:
            with self.jobs.connect() as connection:return self.holds(physical_id,connection)
        rows=db.execute('SELECT node_id,physical_id,revision,details,updated_at FROM worker_inventory_holds WHERE held=1'+
          (' AND physical_id=?' if physical_id else ''),(physical_id,) if physical_id else ())
        return [{'node_id':x[0],'physical_id':x[1],'revision':x[2],'unmatched':json.loads(x[3]),'at':x[4]} for x in rows]

    def reason(self,physical_id,db=None,*,controller_instance=None):
        if db is None:
            with self.jobs.connect() as connection:return self.reason(physical_id,connection,controller_instance=controller_instance)
        if self.holds(physical_id,db):return HOLD_REASON
        if self.jobs.policy.get('require_attempt_inventory'):
            for raw,seen,report in db.execute('SELECT profile,last_seen,report FROM worker_nodes WHERE disabled=0'):
                profile=json.loads(raw)
                if profile['physical_id']!=physical_id:continue
                report=json.loads(report);inventory=report.get('attempt_inventory',{})
                if (time.time()-seen>30 or inventory.get('reconciled') is not True
                    or inventory.get('controller_instance')!=(controller_instance if controller_instance is not None else getattr(self.jobs,'controller_instance',None))):
                    return WAIT_REASON
        return None

    def release(self,node_id,expected_revision):
        from jobs import JobError
        with self.jobs.transaction() as db:
            hold=next((x for x in self.holds(db=db) if x['node_id']==node_id),None)
            if not hold or type(expected_revision) is not int or hold['revision']!=expected_revision:raise JobError('Inventory hold revision changed or does not exist',409)
            node=self.jobs.nodes.node(node_id,db);report=node['report'].get('attempt_inventory',{})
            if (time.time()-node['last_seen']>30 or report.get('reconciled') is not True
                or report.get('controller_instance')!=getattr(self.jobs,'controller_instance',None)):
                raise JobError('A current authenticated, fully reconciled node inventory is required; disabling the node cannot release its hold',409)
            revision=hold['revision']+1;at=time.time()
            db.execute('UPDATE worker_inventory_holds SET held=0,revision=?,updated_at=? WHERE node_id=?',(revision,at,node_id))
            db.execute('INSERT INTO worker_inventory_events(node_id,revision,held,at,actor,details) VALUES(?,?,?,?,?,?)',
                (node_id,revision,0,at,self.jobs.owner,json.dumps({'confirmed_revision':expected_revision})))
            return {'node_id':node_id,'released':True,'revision':revision,'execution_leases_released':False}

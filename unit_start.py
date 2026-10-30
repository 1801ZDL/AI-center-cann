"""Root startup coordination: one StartUnit send, held admission, explicit recovery.

The durable issued boundary precedes the RPC. An unknown response is never
retried. A matching live invocation/claim/ready receipt can finish it; otherwise
an operator must reconcile/stop the unit and explicitly abort the old plan.
"""
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
from jobs import identity,identity_alive,read_json
from payload_quota_admin import fingerprint,sync,write
from state_lock import state_lock
import unit_manager as manager
import unit_refresh as files

FORMAT='ascend-lab-unit-start-plan/v1'
JOURNAL='unit-start-change.json'
GATE='service-admission.json'
HISTORY='unit-start-history'
CLAIMS='service-start-claims'
PREFIX_ENV='ASCEND_LAB_SERVICE_PREFIX'
START_ENV='ASCEND_LAB_START_ID'
CAPABILITY='systemd-startup-gate-v1'
HOLD='服务启动尚未确认，暂停提交和执行；保留原启动计划'


def _checkpoint(stage):pass


class Bus(manager.Bus):
    def start(self,name):
        return self.call(self.owner or manager.BUS,manager.MANAGER,manager.INTERFACE,'StartUnit','ss',[name,'fail'],'o')


def _root_json(path,mode=0o600):
    info=Path(path).lstat()
    if info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=mode:raise DeployError('Startup records require exact protected Root ownership/mode')
    return json.loads(files._file(path,mode)['text'])


def _gate(prefix):
    path=Path(prefix)/GATE
    if not path.exists() and not path.is_symlink():return None
    value=_root_json(path,0o644)
    if (not isinstance(value,dict) or set(value)!={'plan_id','nonce','phase','prefix','state_dir','uid','gid','role','unit_dir','release_id'}
        or not isinstance(value['phase'],str) or value['phase'] not in {'prepared','issued'} or not re.fullmatch('[0-9a-f]{64}',str(value['plan_id']))
        or not re.fullmatch('[0-9a-f]{32}',str(value['nonce'])) or value['prefix']!=str(prefix)):
        raise DeployError('Invalid startup admission gate')
    if any(type(value[k]) is not int or value[k]<0 for k in ['uid','gid']):raise DeployError('Invalid startup gate identity')
    return value


def admission_reason(state):
    prefix=os.environ.get(PREFIX_ENV)
    if prefix is None:return None
    try:
        prefix,meta,_=deploy.load_installation(prefix);files._directory(prefix)
        if str(Path(state))!=meta['state_dir']:return HOLD
        if any(p.exists() or p.is_symlink() for p in [prefix/JOURNAL,prefix/GATE]):return HOLD
    except (OSError,ValueError):return HOLD
    return None


def _claim_path(state,nonce):return Path(state)/CLAIMS/(nonce+'.json')


def _claim(state,gate):
    directory=Path(state)/CLAIMS;info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise DeployError('Unsafe startup claim directory')
    value={'plan_id':gate['plan_id'],'nonce':gate['nonce'],'invocation_id':os.environ['INVOCATION_ID'],
           'process':identity(os.getpid()),'release_id':gate['release_id']}
    path=_claim_path(state,gate['nonce'])
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:raise DeployError('This startup nonce was already consumed; do not launch another invocation') from None
    with os.fdopen(fd,'wb') as file:file.write((json.dumps(value,sort_keys=True)+'\n').encode());file.flush();os.fsync(file.fileno())
    sync(directory)


def entry(prefix):
    gate=_gate(prefix)
    if gate is None:
        if (Path(prefix)/JOURNAL).exists():raise DeployError('Startup journal has no published entry gate')
        manager.check(prefix,running=True,entry=True,credential_recovery=True)
        return
    if gate['phase']!='issued' or os.geteuid()!=gate['uid'] or os.getegid()!=gate['gid']:raise DeployError('This invocation has no issued startup permission')
    prefix,meta,_,_ = manager._context(prefix,startup_recovery=True)
    if any(gate[k]!=meta[k] for k in ['state_dir','uid','gid','role','release_id']):raise DeployError('Startup entry contract changed')
    manager.check(prefix,gate['unit_dir'],running=True,entry=True,startup_recovery=True)
    _claim(meta['state_dir'],gate)


def process_environment(prefix):
    gate=_gate(prefix);env={PREFIX_ENV:str(prefix)}
    if gate is not None:
        claim=deploy.regular_json(_claim_path(gate['state_dir'],gate['nonce']),private=True)
        if (claim.get('plan_id')!=gate['plan_id'] or claim.get('invocation_id')!=os.environ.get('INVOCATION_ID')
            or not identity_alive(claim.get('process')) or claim['process']['pid']!=os.getpid()):raise DeployError('Startup claim does not belong to this process')
        env[START_ENV]=gate['nonce']
    elif (Path(prefix)/JOURNAL).exists():raise DeployError('Unpublished startup gate')
    return env


def _nodes(state,role):
    if role!='controller':return []
    path=Path(state)/'jobs.sqlite3'
    if not path.exists():return []
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='worker_nodes'").fetchone():return []
        rows=db.execute('SELECT id,profile FROM worker_nodes WHERE disabled=0 ORDER BY id').fetchall()
    return [{'id':node,'profile_sha256':fingerprint(json.loads(profile))} for node,profile in rows]


def _context(prefix):
    # Only startup recovery is bypassed; other manager/unit/quota journals stay blocking.
    deploy.verify(prefix,startup_recovery=True)
    prefix,meta,state,control=manager._context(prefix,startup_recovery=True)
    source=Path(__file__);installed=prefix/'current'/source.name
    if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(installed.read_bytes()).digest():raise DeployError('Use matching installed startup recovery code')
    return prefix,meta,state,control


def _validate(value):
    try:
        if not isinstance(value,dict) or set(value)!={'format','contract','manager','nonce','required_nodes','id'} or value['format']!=FORMAT:raise ValueError()
        if fingerprint({k:v for k,v in value.items() if k!='id'})!=value['id']:raise ValueError()
        other={k:v for k,v in value.items() if k!='required_nodes'};other['format']=manager.FORMAT;other['id']=fingerprint({k:v for k,v in other.items() if k!='id'});manager._plan_check(other)
        nodes=value['required_nodes']
        if (not isinstance(nodes,list) or len(nodes)>256 or any(not isinstance(x,dict) or set(x)!={'id','profile_sha256'}
            or not re.fullmatch('worker-[0-9a-f]{12}',str(x['id'])) or not re.fullmatch('[0-9a-f]{64}',str(x['profile_sha256'])) for x in nodes)
            or len({x['id'] for x in nodes})!=len(nodes)):raise ValueError()
        if len((json.dumps({'plan':value,'phase':'issued','job':'/org/freedesktop/systemd1/job/4294967295'},ensure_ascii=False,indent=2)+'\n').encode())>files.MAX_BYTES:
            raise ValueError()
    except (KeyError,TypeError,ValueError):raise DeployError('Invalid startup plan') from None


def plan(prefix,unit_dir='/etc/systemd/system',bus=None):
    deploy.require_root();prefix,meta,state,control=_context(prefix);bus=bus or Bus()
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,meta['role'])
        if any(p.exists() or p.is_symlink() for p in [prefix/JOURNAL,prefix/GATE]):raise DeployError('Resume or abort the original startup first')
        if deploy.process_status(prefix)['running']:raise DeployError('Stop the managed service before planning startup')
        contract=manager._contract(prefix,meta,state,control,unit_dir);anchor=bus.anchor();current=bus.unit(contract['unit_name'])
        manager.inactive(current);manager.effective(current,contract['expected'],contract['unit_name'],Path(unit_dir)/contract['unit_name'])
        import uuid
        value={'format':FORMAT,'contract':contract,'manager':anchor,'nonce':uuid.uuid4().hex,'required_nodes':_nodes(state,meta['role'])};value['id']=fingerprint(value)
        _validate(value)
        if bus.anchor()!=anchor:raise DeployError('Manager changed while planning startup')
        return {'read_only':True,'plan':value,'start_sent':False}


def _gate_value(value,phase):
    binding=value['contract']['binding'];meta=deploy.regular_json(Path(binding['prefix'])/'installation.json')
    return {'plan_id':value['id'],'nonce':value['nonce'],'phase':phase,'prefix':binding['prefix'],'state_dir':binding['state_dir'],
        **{k:meta[k] for k in ['uid','gid','role','release_id']},'unit_dir':value['contract']['unit_dir']}


def _ready(prefix,meta,state,value,bus):
    current=bus.unit(value['contract']['unit_name'])
    manager.effective(current,value['contract']['expected'],value['contract']['unit_name'],Path(value['contract']['unit_dir'])/value['contract']['unit_name'])
    status=deploy.process_status(prefix)
    if not status['running'] or not status['ready']:return None
    claim_path=_claim_path(state,value['nonce']);info=claim_path.lstat();claim=deploy.regular_json(claim_path,private=True)
    if (info.st_uid!=meta['uid'] or claim.get('plan_id')!=value['id'] or claim.get('nonce')!=value['nonce'] or claim.get('release_id')!=meta['release_id']
        or not identity_alive(claim.get('process')) or claim['process']['pid']!=status['pid'] or bytes(current['InvocationID']).hex()!=claim.get('invocation_id')):
        raise DeployError('Live startup claim, process or manager invocation differs')
    manager.check(prefix,value['contract']['unit_dir'],running=True,bus=bus,startup_recovery=True)
    publication=deploy.regular_json(state/('server-ready.json' if meta['role']=='controller' else 'heartbeat-receipt.json'),private=True)
    if publication.get('service_start_id')!=value['nonce']:raise DeployError('Service ready/heartbeat is not from this startup')
    if meta['role']=='controller':
        if _nodes(state,meta['role'])!=value['required_nodes']:raise DeployError('Required Node definitions changed')
        with sqlite3.connect('file:'+str(state/'jobs.sqlite3')+'?mode=ro',uri=True) as db:
            for node in value['required_nodes']:
                row=db.execute('SELECT last_seen,report FROM worker_nodes WHERE id=? AND disabled=0',(node['id'],)).fetchone()
                report=json.loads(row[1]) if row else {};inventory=report.get('attempt_inventory',{})
                if (not row or not 0<=time.time()-row[0]<=30 or inventory.get('reconciled') is not True
                    or inventory.get('controller_instance')!=publication.get('controller_instance')):return None
            if db.execute('SELECT 1 FROM worker_inventory_holds WHERE held=1 LIMIT 1').fetchone():return None
    else:
        config=deploy.regular_json(state/'config.json',private=True)
        if (publication.get('node_id')!=config['node_id'] or not 0<=time.time()-publication.get('acknowledged_at',0)<=30
            or any(publication.get(k)!=claim['process'].get(k) for k in ['pid','start_ticks','boot_id'])):return None
    if bus.anchor()!=value['manager']:raise DeployError('Manager changed while confirming startup')
    return {'invocation_id':claim['invocation_id'],'process':{k:claim['process'][k] for k in ['pid','uid','boot_id','start_ticks']},'claim_sha256':fingerprint(claim)}


def _receipt(value,result):
    base={'plan_id','state','start_sent_again','admission_opened'}
    if (not isinstance(result,dict) or result.get('plan_id')!=value['id'] or not isinstance(result.get('state'),str)
        or result['state'] not in {'ready','aborted'} or result.get('start_sent_again') is not False):raise DeployError('Startup receipt differs')
    if result['state']=='aborted':
        if set(result)!=base or result['admission_opened'] is not False:raise DeployError('Aborted startup receipt differs')
        return
    if (set(result)!=base|{'ready','required_nodes'} or result['admission_opened'] is not True
        or type(result['required_nodes']) is not int or result['required_nodes']!=len(value['required_nodes'])):raise DeployError('Ready startup receipt differs')
    ready=result['ready']
    if (not isinstance(ready,dict) or set(ready)!={'invocation_id','process','claim_sha256'}
        or not re.fullmatch('[0-9a-f]{32}',str(ready['invocation_id'])) or not re.fullmatch('[0-9a-f]{64}',str(ready['claim_sha256']))
        or not isinstance(ready['process'],dict) or set(ready['process'])!={'pid','uid','boot_id','start_ticks'}
        or any(type(ready['process'][k]) is not int or ready['process'][k]<(0 if k=='uid' else 1) for k in ['pid','uid','start_ticks'])
        or not isinstance(ready['process']['boot_id'],str) or not re.fullmatch('[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}',ready['process']['boot_id'])):
        raise DeployError('Ready startup identity is malformed')


def apply(prefix,value,bus=None,abort=False):
    deploy.require_root();_validate(value);prefix,meta,state,control=_context(prefix);bus=bus or Bus()
    with state_lock(prefix,exclusive=True):
        contract=manager._contract(prefix,meta,state,control,value['contract']['unit_dir'])
        if contract!=value['contract']:raise DeployError('Startup installation/files/configuration changed')
        journal=prefix/JOURNAL;gate=prefix/GATE;history=prefix/HISTORY;receipt=history/(value['id']+'.json')
        pending=_root_json(journal) if journal.exists() or journal.is_symlink() else None
        if pending is not None and (not isinstance(pending,dict) or set(pending)!={'plan','phase','job'} or pending['plan']!=value
            or not isinstance(pending['phase'],str) or pending['phase'] not in {'prepared','issued'}
            or pending['job'] is not None and not manager._typed('o',pending['job'])):raise DeployError('Resume only the original startup operation')
        if history.exists() or history.is_symlink():files._directory(history,private=True)
        completed=_root_json(receipt) if receipt.exists() or receipt.is_symlink() else None
        if completed is not None:_receipt(value,completed)
        if pending is None and completed is not None:
            if gate.exists() or gate.is_symlink():raise DeployError('Unexpected leftover startup gate')
            return {**completed,'historical_receipt_only':True}
        if abort:
            if pending is None:raise DeployError('No pending startup to abort')
            with state_lock(state,exclusive=True):
                from maintenance import assert_quiescent
                assert_quiescent(state,meta['role']);manager.inactive(bus.unit(contract['unit_name']))
                if deploy.process_status(prefix)['running']:raise DeployError('Stop/reconcile the service before aborting startup')
                result={'plan_id':value['id'],'state':'aborted','start_sent_again':False,'admission_opened':False}
                write(receipt,result);gate.unlink(missing_ok=True);sync(prefix);journal.unlink();sync(prefix)
                return result
        if bus.anchor()!=value['manager']:raise DeployError('Manager identity changed; reconcile or explicitly abort the original startup')
        if pending is None:
            with state_lock(state,exclusive=True):
                from maintenance import assert_quiescent
                assert_quiescent(state,meta['role']);current=bus.unit(contract['unit_name']);manager.inactive(current)
                manager.effective(current,contract['expected'],contract['unit_name'],Path(contract['unit_dir'])/contract['unit_name'])
                if deploy.process_status(prefix)['running'] or _nodes(state,meta['role'])!=value['required_nodes']:raise DeployError('Startup process or required Node definitions changed')
                if not history.exists():history.mkdir(mode=0o700);sync(prefix)
                if len(list(history.glob('*.json')))>=1024:raise DeployError('Archive old startup receipts before more operations')
                claims=state/CLAIMS
                if not claims.exists():claims.mkdir(mode=0o700);os.chown(claims,meta['uid'],meta['gid']);sync(state)
                info=claims.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid!=meta['uid'] or info.st_mode&0o077:raise DeployError('Unsafe startup claim directory')
                if _claim_path(state,value['nonce']).exists():raise DeployError('Startup nonce was already claimed')
                pending={'plan':value,'phase':'prepared','job':None};write(journal,pending);_checkpoint('journal')
        if pending['phase']=='prepared':
            # Persist uncertainty BEFORE the request. No later apply may send again.
            with state_lock(state,exclusive=True):
                from maintenance import assert_quiescent
                assert_quiescent(state,meta['role']);manager.inactive(bus.unit(contract['unit_name']))
                expected=_gate_value(value,'prepared');actual_gate=_gate(prefix)
                if actual_gate is not None and actual_gate!=expected:raise DeployError('Prepared startup gate differs')
                write(gate,expected,mode=0o644);_checkpoint('prepared')
                pending={**pending,'phase':'issued'};write(journal,pending);write(gate,_gate_value(value,'issued'),mode=0o644);_checkpoint('issued')
            job=bus.start(contract['unit_name']);pending={**pending,'job':job};write(journal,pending);_checkpoint('response')
        else:
            expected=_gate_value(value,'issued')
            actual_gate=_gate(prefix)
            if actual_gate!=expected and not (actual_gate is None and completed is not None and completed['state']=='ready'):
                raise DeployError('Issued startup gate differs; reconcile before continuing')
        ready=_ready(prefix,meta,state,value,bus)
        if ready is None:return {'plan_id':value['id'],'state':'pending','reason':'waiting_for_original_invocation_ready_and_fresh_heartbeats','start_sent_again':False,'admission_opened':False}
        _checkpoint('ready')
        result={'plan_id':value['id'],'state':'ready','start_sent_again':False,'admission_opened':True,'ready':ready,'required_nodes':len(value['required_nodes'])}
        if completed is not None and completed!=result:raise DeployError('Startup ready identity changed after receipt')
        write(receipt,result);_checkpoint('receipt')
        gate.unlink(missing_ok=True);sync(prefix);_checkpoint('gate_removed')
        journal.unlink();sync(prefix);_checkpoint('published')
        return {**result,'historical_receipt_only':False}

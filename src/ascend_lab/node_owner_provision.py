"""Root Node participant for recoverable work/cache preparation and owner ACK.

One explicit request owns its native plans and immutable proposal. The central
service approves that proposal independently. No Root transport, account
creation, credential issuance, service start or device work is performed here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from itertools import islice

import deployment as deploy
from deployment import DeployError
import node_owner_admin as binding
import workspace_quota_admin as native
import payload_quota_admin as files

FORMAT='ascend-lab-node-owner-provision/v1'
JOURNAL='node-owner-provision-change.json'
HISTORY='node-owner-provision-history'
MAX_RECORD=12*1024**2
MAX_HISTORY_BYTES=128*1024**2
MAX_HISTORY_FILES=256
_PARTICIPANT=ContextVar('ascend_lab_root_node_participant',default=None)


def _checkpoint(stage):pass


def request(value):
    fields={'operation_id','owner','stage_id','uid','gid','workspace','cache'}
    if not isinstance(value,dict) or set(value)!=fields:raise DeployError('Node participant requires an exact operation/owner/UID/workspace/cache request')
    if not isinstance(value['operation_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['operation_id']):raise DeployError('Choose one explicit 32-hex participant operation_id')
    binding.request({**{k:value[k] for k in ['owner','stage_id','uid','gid']},'workspace_slot':'work','cache_slot':'cache'})
    for role in ['workspace','cache']:
        item=value[role]
        if (not isinstance(item,dict) or set(item)!={'root','bytes','inodes'}
            or not isinstance(item['root'],str) or not item['root'].startswith('/')
            or item['root']=='/'
            or str(Path(item['root']))!=item['root'] or '..' in Path(item['root']).parts
            or type(item['bytes']) is not int or not 1024**2<=item['bytes']<=1024**4
            or type(item['inodes']) is not int or not 16<=item['inodes']<=10**7):raise DeployError('Invalid finite native '+role+' request')
    if value['workspace']['root']==value['cache']['root']:raise DeployError('Work and private cache must select separate registered quota filesystems')
    return copy.deepcopy(value)


def guard(state):
    path=Path(state)/JOURNAL
    if path.exists() or path.is_symlink():
        if os.geteuid()==0 and _PARTICIPANT.get()==str(Path(state)):return
        raise DeployError('Resume the original Node owner provisioning participant before startup or maintenance')


def _read(path):
    value=deploy.regular_json(path,max_bytes=MAX_RECORD,private=True);info=Path(path).lstat()
    if info.st_uid!=0:raise DeployError('Node participant records must remain Root-owned')
    return value


def _write(path,value):
    if len((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())>MAX_RECORD:
        raise DeployError('Node participant record exceeds its finite metadata limit')
    files.write(path,value)


def _history(directory,reserve=False):
    if not directory.exists():
        if not reserve:return
        directory.mkdir(mode=0o700);files.sync(directory.parent)
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o777!=0o700:raise DeployError('Unsafe participant history directory')
    entries=list(islice(directory.iterdir(),MAX_HISTORY_FILES+1));total=0
    if len(entries)>MAX_HISTORY_FILES:raise DeployError('Participant history entry limit exceeded')
    for path in entries:
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_nlink!=1
            or info.st_size>MAX_RECORD or not re.fullmatch('(?:[0-9a-f]{64}\\.(?:proposal|receipt)\\.json|\\.[0-9a-f]{64}\\.(?:proposal|receipt)\\.json\\.[0-9a-f]{32})',path.name)):
            raise DeployError('Unknown or unsafe participant history entry; retain it for reconciliation')
        total+=info.st_size
    if total+(3*MAX_RECORD if reserve else 0)>MAX_HISTORY_BYTES or reserve and len(entries)+2>MAX_HISTORY_FILES:
        raise DeployError('Archive participant metadata before further provisioning')


def _temporaries(state):
    """Retain interrupted atomic writes, with a finite recovery budget."""
    paths=list(islice(state.glob('.'+JOURNAL+'.*'),17));total=0
    if len(paths)>16:raise DeployError('Archive interrupted participant writes before continuing')
    for path in paths:
        info=path.lstat()
        if (not re.fullmatch(re.escape('.'+JOURNAL)+r'\.[0-9a-f]{32}',path.name)
            or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077
            or info.st_nlink!=1 or info.st_size>MAX_RECORD):raise DeployError('Unsafe interrupted participant write')
        total+=info.st_size
    if total>24*1024**2:raise DeployError('Interrupted participant metadata exceeds its finite budget')


def _identity(prefix,meta,state,config):
    return {'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],
        'control_identity':binding.control(state),'node_id':config['node_id'],'physical_id':config['physical_id'],
        'base_configuration_sha256':binding.base_config(config),'base_installation_sha256':binding.base_meta(meta),
        'credential_independent_configuration_sha256':binding.sha({k:v for k,v in config.items() if k not in {*binding.PROJECT,'node_key'}})}


def _ack_context(record,current):
    # Rotating this Node's own transport credential after local binding must
    # not strand a persisted ACK. Keep the original proposal hash, while all
    # other transport/configuration and physical bindings must still match.
    before=record.get('binding',{})
    if isinstance(before,dict) and record.get('phase') in {'acknowledging','acknowledged'}:
        expected={**current,'base_configuration_sha256':before.get('base_configuration_sha256')}
        if expected==before:return expected
    return current


def _validate(record,identifier,spec,context):
    if (not isinstance(record,dict) or set(record)!={'format','id','request','binding','native_plans','slots','phase','proposal_id'}
        or record['format']!=FORMAT or record['id']!=identifier or record['request']!=spec or record['binding']!=context
        or record['phase'] not in {'preparing','awaiting_approval','binding','acknowledging','acknowledged'}
        or not isinstance(record['native_plans'],dict) or set(record['native_plans'])!={'workspace','cache'}
        or not isinstance(record['slots'],dict) or set(record['slots'])-{'workspace','cache'}):raise DeployError('Resume only the original unchanged Node participant request')
    for role,plan in record['native_plans'].items():
        native._validate_plan(plan)
        if (plan['prefix']!=context['prefix'] or plan['node_id']!=context['node_id'] or plan['root']!=spec[role]['root']
            or plan['release_id']!=context['release_id'] or plan['control']!=context['state_dir']
            or plan['control_identity']!=[context['control_identity'][k] for k in ['device','inode']]
            or len(plan['additions'])!=1 or any(plan['additions'][0][k]!=spec[role][k] for k in ['bytes','inodes'])
            or plan['additions'][0]['uid']!=spec['uid']):raise DeployError('Native participant plan differs from the original resource request')
        if role in record['slots']:
            slot=record['slots'][role];added=plan['additions'][0]
            if (not isinstance(slot,dict) or any(slot.get(k)!=added[k] for k in ['id','storage_id','bytes','inodes'])
                or slot.get('quota_uid')!=spec['uid']):raise DeployError('Participant slot differs from its frozen native allocation')
    if record['phase']=='preparing':
        if record['proposal_id'] is not None:raise DeployError('Unexpected prepared proposal in the native phase')
    elif (set(record['slots'])!={'workspace','cache'} or not isinstance(record['proposal_id'],str)
          or not re.fullmatch('[0-9a-f]{64}',record['proposal_id'])):raise DeployError('Prepared participant proposal binding is incomplete')


def _proposal(record,path):
    proposal=_read(path);binding._plan_check(proposal);spec=record['request'];context=record['binding']
    wanted={**{k:spec[k] for k in ['owner','stage_id','uid','gid']},
        'workspace_slot':record['slots']['workspace']['id'],'cache_slot':record['slots']['cache']['id']}
    if (proposal['request']!=wanted or proposal['node_id']!=context['node_id'] or proposal['physical_id']!=context['physical_id']
        or proposal['prefix']!=context['prefix'] or proposal['state_dir']!=context['state_dir']
        or proposal['release_id']!=context['release_id'] or proposal['config_base_sha256']!=context['base_configuration_sha256']
        or proposal['meta_base_sha256']!=context['base_installation_sha256'] or proposal['control_identity']!=context['control_identity']
        or record['proposal_id'] is not None and proposal['id']!=record['proposal_id']):raise DeployError('Prepared proposal belongs to another participant or resource contract')
    return proposal


def _children(record,state,proposal):
    path=state/native.JOURNAL
    if path.exists() or path.is_symlink():
        child=_read(path)
        if child not in record['native_plans'].values():raise DeployError('An unrelated native append is pending')
    path=state/binding.JOURNAL
    if path.exists() or path.is_symlink():
        child=_read(path)
        if proposal is None or child!=proposal:raise DeployError('An unrelated Node owner binding is pending')
    for name in [binding.AWAIT,'node-owner-ack-progress.json']:
        path=state/name
        if path.exists() or path.is_symlink():
            if proposal is None or _read(path).get('plan_id')!=proposal['id']:raise DeployError('An unrelated Node acknowledgement is pending')


def _native(record,role,prefix,journal):
    result=native.apply(prefix,record['native_plans'][role]);added=record['native_plans'][role]['additions'][0]
    slot=next(s for s in result['policy']['slots'] if s['storage_id']==added['storage_id'])
    if role in record['slots'] and record['slots'][role]!=slot:raise DeployError('Prepared native slot changed')
    record['slots'][role]=slot;_write(journal,record);_checkpoint(role)


def _result(record,state,proposal_path):
    return {'format':FORMAT,'id':record['id'],'operation_id':record['request']['operation_id'],
        'owner':record['request']['owner'],'node_id':record['binding']['node_id'],'state':state,
        'proposal_id':record['proposal_id'],'proposal_path':str(proposal_path) if record['proposal_id'] else None,
        'credentials_issued':False,'resources_retained':bool(record['slots']),
        'node_start_and_fresh_heartbeat_required':True,'central_owner_activation_still_required':True}


def _receipt(saved,identifier,spec):
    fields={'format','id','operation_id','owner','node_id','state','proposal_id','proposal_path','credentials_issued',
        'resources_retained','node_start_and_fresh_heartbeat_required','central_owner_activation_still_required'}
    if (not isinstance(saved,dict) or set(saved)!={'format','id','request','binding','result'}
        or saved['format']!=FORMAT or saved['id']!=identifier or saved['request']!=spec
        or not isinstance(saved['result'],dict) or set(saved['result'])!=fields):raise DeployError('Participant historical receipt differs')
    result=saved['result'];context=saved['binding']
    if (not isinstance(context,dict) or set(context)!={'prefix','state_dir','release_id','control_identity','node_id','physical_id','base_configuration_sha256','base_installation_sha256','credential_independent_configuration_sha256'}
        or not isinstance(context['node_id'],str) or not re.fullmatch('worker-[0-9a-f]{12}',context['node_id'])
        or result['format']!=FORMAT or result['id']!=identifier or result['operation_id']!=spec['operation_id']
        or result['owner']!=spec['owner'] or result['node_id']!=context.get('node_id') or result['state'] not in {'bound','aborted'}
        or result['credentials_issued'] is not False or type(result['resources_retained']) is not bool
        or result['node_start_and_fresh_heartbeat_required'] is not True or result['central_owner_activation_still_required'] is not True):
        raise DeployError('Participant historical result differs')
    if result['state']=='aborted':
        if result['proposal_id'] is not None or result['proposal_path'] is not None:raise DeployError('Aborted participant unexpectedly exported a proposal')
    elif (not result['resources_retained'] or not isinstance(result['proposal_id'],str) or not re.fullmatch('[0-9a-f]{64}',result['proposal_id'])
          or result['proposal_path']!=str(Path(context['state_dir'])/HISTORY/(identifier+'.proposal.json'))):
        raise DeployError('Bound participant receipt lost its original proposal')
    return result


def provision(prefix,value,action='prepare'):
    deploy.require_root();spec=request(value)
    if action not in {'prepare','complete','abort'}:raise DeployError('Invalid Node participant action')
    prefix,meta,release=deploy.load_installation(prefix);state=Path(meta['state_dir'])
    if meta['role']!='node' or meta['uid']!=0:raise DeployError('Node participant requires an installed trusted Root Node')
    source=Path(__file__);installed=release/source.name
    if not installed.is_file() or hashlib.sha256(installed.read_bytes()).digest()!=hashlib.sha256(source.read_bytes()).digest():
        raise DeployError('Use matching installed Node participant code')
    identifier=binding.sha({'prefix':str(prefix),'operation_id':spec['operation_id']});journal=state/JOURNAL;history=state/HISTORY
    proposal_path=history/(identifier+'.proposal.json');receipt=history/(identifier+'.receipt.json')
    fd=os.open(prefix/'.node-owner-provision.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    token=None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_nlink!=1:raise DeployError('Unsafe Root participant lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DeployError('Another Node participant is running') from None
        binding.control(state);_history(history);_temporaries(state)
        pending=_read(journal) if journal.exists() or journal.is_symlink() else None
        if pending is not None and (pending.get('id')!=identifier or pending.get('request')!=spec):raise DeployError('Another Node participant request is pending')
        token=_PARTICIPANT.set(str(state))
        prefix,meta,release=deploy.verify(prefix,quota_recovery=pending is not None)
        config=deploy.regular_json(state/'config.json',private=True);context=_identity(prefix,meta,state,config)
        from backends import physical_identity
        if config['physical_id']!=physical_identity():raise DeployError('Node participant requires the registered physical host')
        if pending is not None:context=_ack_context(pending,context)
        if receipt.exists() or receipt.is_symlink():
            saved=_read(receipt)
            historical=_receipt(saved,identifier,spec)
            if pending is not None:
                _validate(pending,identifier,spec,context);journal.unlink();files.sync(state);_checkpoint('published')
            return {**historical,'historical_receipt_only':True}
        if pending is None:
            if action!='prepare':raise DeployError('Prepare the original Node participant request first')
            from maintenance import assert_quiescent
            from state_lock import state_lock
            with state_lock(state,exclusive=True):
                assert_quiescent(state,'node');binding.guard(state)
                if spec['owner'] in config['users'] or any(x['uid']==spec['uid'] for x in config['users'].values()):
                    raise DeployError('Participant can only add a new owner and unused execution UID')
            plans={role:native.plan(prefix,spec[role]['root'],[{'uid':spec['uid'],**{k:spec[role][k] for k in ['bytes','inodes']}}])['plan'] for role in ['workspace','cache']}
            _history(history,reserve=True)
            pending={'format':FORMAT,'id':identifier,'request':spec,'binding':context,'native_plans':plans,'slots':{},'phase':'preparing','proposal_id':None}
            with state_lock(state,exclusive=True):
                # Close the gap between read-only planning and the admission
                # marker. A live Worker retains a shared lock and must win or
                # lose before this journal is published.
                current=deploy.regular_json(state/'config.json',private=True)
                if current!=config:raise DeployError('Node configuration changed during participant planning')
                _write(journal,pending);_checkpoint('journal')
        _validate(pending,identifier,spec,context)
        if (pending['phase'] in {'preparing','awaiting_approval'}
            and any(plan['configuration_sha256']!=native.fingerprint(config) for plan in pending['native_plans'].values())):
            raise DeployError('Node configuration changed before participant binding')
        proposal=_proposal(pending,proposal_path) if proposal_path.exists() or proposal_path.is_symlink() else None
        _children(pending,state,proposal)
        if pending['phase']!='preparing' and proposal is None:
            raise DeployError('Original participant proposal is missing; no replacement may be generated')
        if action=='abort':
            if pending['phase']!='preparing' or proposal is not None:raise DeployError('Exported or binding proposals require central reconciliation; resume the original request')
            active=state/native.JOURNAL
            if active.exists() or active.is_symlink():
                inflight=_read(active)
                role=next(role for role,plan in pending['native_plans'].items() if plan==inflight)
                _native(pending,role,prefix,journal)
            for role,plan in pending['native_plans'].items():
                child=state/native.HISTORY/(plan['id']+'.json')
                if role not in pending['slots'] and child.exists():_native(pending,role,prefix,journal)
            result=_result(pending,'aborted',proposal_path)
        else:
            if pending['phase']=='preparing':
                for role in ['workspace','cache']:
                    # The Node has one shared native journal. Do not replay a
                    # completed work append over a pending cache append.
                    if role not in pending['slots']:_native(pending,role,prefix,journal)
                if proposal is None:
                    proposal=binding.plan(prefix,{**{k:spec[k] for k in ['owner','stage_id','uid','gid']},
                        'workspace_slot':pending['slots']['workspace']['id'],'cache_slot':pending['slots']['cache']['id']})['plan']
                    _write(proposal_path,proposal);_checkpoint('proposal')
                proposal=_proposal(pending,proposal_path)
                pending.update(phase='awaiting_approval',proposal_id=proposal['id']);_write(journal,pending);_checkpoint('prepared')
            if action=='prepare':return {**_result(pending,pending['phase'],proposal_path),'historical_receipt_only':False}
            if proposal is None:raise DeployError('Original participant proposal is missing; no replacement may be generated')
            if pending['phase']=='awaiting_approval':
                pending['phase']='binding';_write(journal,pending);_checkpoint('binding_intent')
            if pending['phase']=='binding':
                binding.apply(prefix,proposal);_checkpoint('binding_applied')
                pending['phase']='acknowledging';_write(journal,pending);_checkpoint('ack_intent')
            if pending['phase']=='acknowledging':
                from node_owner_ack import complete
                complete(prefix,proposal['id']);_checkpoint('acknowledged')
                pending['phase']='acknowledged';_write(journal,pending);_checkpoint('node_ready')
            result=_result(pending,'bound',proposal_path)
        saved={'format':FORMAT,'id':identifier,'request':spec,'binding':pending['binding'],'result':result}
        _write(receipt,saved);_checkpoint('receipt');journal.unlink();files.sync(state);_checkpoint('published')
        return {**result,'historical_receipt_only':False}
    finally:
        if token is not None:_PARTICIPANT.reset(token)
        os.close(fd)

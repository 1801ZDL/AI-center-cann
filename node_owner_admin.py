"""Offline owner binding across Node users, prepared work slots and cache metadata.

The new owner remains unavailable until a later central acknowledgement. Old
namespace data is never copied, deleted, chowned or re-keyed by this operation.
"""
from contextlib import ExitStack,contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from deployment import DeployError,require_root,verify,regular_json
from lab_client import now
from state_lock import state_lock
import payload_quota_admin as files
import persistent_cache as cache
import quota_workspace as native
from workspace_slots import root_fd,validate as validate_slots
import filesystem_quota as quota
from workspace_quota_admin import _write as native_write

JOURNAL='node-owner-change.json'
AWAIT='node-owner-awaiting-controller.json'
HISTORY='node-owner-history'
FORMAT='ascend-lab-node-owner-plan/v1'
MAX_RECORD=8*1024**2
PROJECT=('users','workspace','persistent_cache','policy_revision','owner_bindings')


def _checkpoint(stage):
    """Named boundaries used only by process-crash acceptance."""


def guard(state):
    for name in [JOURNAL,AWAIT,'node-owner-ack-progress.json']:
        path=Path(state)/name
        if path.exists() or path.is_symlink():
            raise DeployError('Finish the original Node owner binding and central acknowledgement before startup or maintenance')


def read(path):return regular_json(path,max_bytes=MAX_RECORD,private=True)


def sha(value):return cache.sha(value)


def projection(config):
    return {k:copy.deepcopy(config.get(k,1 if k=='policy_revision' else {})) for k in PROJECT}


def base_config(config):return sha({k:v for k,v in config.items() if k not in PROJECT})


def base_meta(meta):return sha({k:v for k,v in meta.items() if k not in {'persistent_cache','node_owner_revision'}})


def control(state):
    info=Path(state).lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise DeployError('Node owner binding requires Root-private control state')
    return {'device':info.st_dev,'inode':info.st_ino}


def request(value):
    if (not isinstance(value,dict) or set(value)!={'owner','stage_id','uid','gid','workspace_slot','cache_slot'}
        or not isinstance(value['owner'],str) or not re.fullmatch('[a-zA-Z][a-zA-Z0-9_-]{0,47}',value['owner'])
        or not isinstance(value['stage_id'],str) or not re.fullmatch('[0-9a-f]{64}',value['stage_id'])
        or any(type(value[k]) is not int or not 1000<=value[k]<2**31 for k in ['uid','gid'])
        or any(not isinstance(value[k],str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',value[k]) for k in ['workspace_slot','cache_slot'])):
        raise DeployError('Owner binding needs one staged owner ID, definition SHA, non-root UID/GID and prepared slot IDs')
    return value


def extend(before,value,work,private):
    request(value);after=copy.deepcopy(before);owner=value['owner']
    if owner in before['users'] or any(m.get('uid')==value['uid'] for m in before['users'].values()):
        raise DeployError('Owner or execution UID is already mapped; refusing alias/replacement')
    if owner in before['owner_bindings']:raise DeployError('Owner already has a binding record')
    if not before['persistent_cache'].get('slots') or not before['workspace'].get('slots'):
        raise DeployError('Owner append requires existing finite work and private cache policies')
    for key,slot in [('workspace',work),('persistent_cache',private)]:
        if not native.is_quota(slot) or slot['quota_uid']!=value['uid'] or slot['id']!=value['workspace_slot' if key=='workspace' else 'cache_slot']:
            raise DeployError('Prepared slot does not match the requested owner UID and role')
        if any(s['id']==slot['id'] or s['storage_id']==slot['storage_id'] for s in before[key]['slots']):
            raise DeployError('Only an unused prepared slot may be appended')
        if not any(native.is_quota(s) and Path(s['path']).parent==Path(slot['path']).parent for s in before[key]['slots']):
            raise DeployError('Select a native filesystem already registered for this storage role')
        after[key]['slots'].append(copy.deepcopy(slot))
    after['users'][owner]={'uid':value['uid'],'gid':value['gid']}
    after['owner_bindings'][owner]={'stage_id':value['stage_id'],'state':'awaiting-controller'}
    if type(before['policy_revision']) is not int or before['policy_revision']<1:raise DeployError('Invalid current Node policy revision')
    after['policy_revision']+=1
    validate_slots(after['workspace']);cache.validate(after['persistent_cache'],after['users'])
    return after


def _context(prefix,recovery=False):
    require_root();prefix,meta,release=verify(prefix,quota_recovery=recovery)
    helper=release/Path(__file__).name
    if (meta['role']!='node' or not helper.is_file()
        or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest()):
        raise DeployError('Upgrade this Node to matching owner-binding administration and recovery guards first')
    state=Path(meta['state_dir']);control(state)
    return prefix,meta,state,read(state/'config.json')


def _other(state):
    path=Path(state)/'workspace-owner-change.json'
    if path.exists() or path.is_symlink():raise DeployError('Finish the original native UID append before binding an owner')


def _sources(config):
    result={}
    for key in ['workspace','persistent_cache']:
        for slot in config.get(key,{}).get('slots',[]):
            if not native.is_quota(slot):continue
            root=str(Path(slot['path']).parent)
            if root in result:continue
            with native.directory(root) as fd:
                if native.EXTEND_INTENT in os.listdir(fd):raise DeployError('An unfinished native UID append remains on the source filesystem')
                marker=native.read_marker(fd);validate_slots({'slots':marker['slots']},host=True)
                result[root]=marker
    return result


def _select(config,key,identifier,uid,sources):
    roots={str(Path(s['path']).parent) for s in config[key]['slots'] if native.is_quota(s)}
    matches=[s for root in roots for s in sources[root]['slots'] if s['id']==identifier and s['quota_uid']==uid]
    if len(matches)!=1:raise DeployError('Requested prepared UID slot is missing or ambiguous')
    return copy.deepcopy(matches[0])


def _names(pool,slot):
    with root_fd(slot) as fd:
        result=[]
        for name in sorted(set(os.listdir(fd))-{cache.MARKER}):
            info=pool.inspect_key(fd,name,slot)
            result.append({'key':name,'device':info.st_dev,'inode':info.st_ino,'uid':info.st_uid,'gid':info.st_gid,'mode':info.st_mode})
        usage=quota.require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
        return {'directories':result,'usage':{k:usage[k] for k in ['bytes_used','inodes_used']}}


def _new_slots(after,value,allow_marker=False):
    for key,identifier in [('workspace',value['workspace_slot']),('persistent_cache',value['cache_slot'])]:
        slot=next(s for s in after[key]['slots'] if s['id']==identifier)
        with root_fd(slot) as fd:
            names=set(os.listdir(fd))
            if names and not (allow_marker and key=='persistent_cache' and names=={cache.MARKER}):
                raise DeployError('New owner slot contains data; refusing adoption')
            usage=quota.require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
            if usage['bytes_used'] or usage['inodes_used']:raise DeployError('New owner UID already owns native filesystem data')


def _plan_check(value):
    fields={'format','prefix','state_dir','release_id','node_id','physical_id','request','before','after',
            'config_base_sha256','meta_base_sha256','control_identity','revision','sources','caches','id'}
    if (not isinstance(value,dict) or set(value)!=fields or value['format']!=FORMAT
        or len(json.dumps(value).encode())>MAX_RECORD or value['id']!=sha({k:v for k,v in value.items() if k!='id'})):
        raise DeployError('Invalid Node owner binding plan')
    request(value['request'])
    for key in ['id','release_id','physical_id','config_base_sha256','meta_base_sha256']:
        if not re.fullmatch('[0-9a-f]{64}',str(value[key])):raise DeployError('Invalid Node owner binding digest')
    if not re.fullmatch('worker-[0-9a-f]{12}',str(value['node_id'])):raise DeployError('Invalid Node owner identity')
    if any(not native.canonical(value[k]) for k in ['prefix','state_dir']):raise DeployError('Node owner plan paths must be canonical')
    if type(value['revision']) is not int or value['revision']<0:raise DeployError('Invalid Node owner binding revision')
    identity=value['control_identity']
    if not isinstance(identity,dict) or set(identity)!={'device','inode'} or any(type(v) is not int or v<=0 for v in identity.values()):
        raise DeployError('Invalid Node owner control identity')
    before=value['before'];after=value['after']
    if not isinstance(before,dict) or not isinstance(after,dict) or set(before)!=set(PROJECT) or set(after)!=set(PROJECT):
        raise DeployError('Invalid Node owner projection')
    v=value['request']
    work=next((s for s in after['workspace'].get('slots',[]) if s['id']==v['workspace_slot']),None)
    private=next((s for s in after['persistent_cache'].get('slots',[]) if s['id']==v['cache_slot']),None)
    if work is None or private is None or extend(before,v,work,private)!=after:raise DeployError('Owner plan may only append the requested mapping and two slots')
    if not isinstance(value['sources'],dict) or not value['sources']:raise DeployError('Native source bindings are required')
    for path,source in value['sources'].items():
        if not native.canonical(path) or not isinstance(source,dict) or source.get('format')!=native.FORMAT:
            raise DeployError('Invalid native source snapshot')
        validate_slots({'slots':source.get('slots')})
    snapshots=value['caches']
    if not isinstance(snapshots,dict) or set(snapshots)!={s['storage_id'] for s in before['persistent_cache']['slots']}:
        raise DeployError('Every old cache requires a preserved control snapshot')
    for storage_id,snapshot in snapshots.items():
        if not isinstance(snapshot,dict) or set(snapshot)!={'state','directories','usage'}:raise DeployError('Invalid cache snapshot')
        state=snapshot['state']
        if (not isinstance(state,dict) or set(state)!={'binding','generation','lease','keys'} or state['lease'] is not None
            or type(state['generation']) is not int or state['generation']<1 or not isinstance(state['keys'],dict)
            or len(state['keys'])>cache.MAX_KEYS):raise DeployError('Cache snapshot must be idle and complete')
        slot=next(s for s in before['persistent_cache']['slots'] if s['storage_id']==storage_id)
        expected={'format':'private-persistent-cache/v1','node_id':value['node_id'],'physical_id':value['physical_id'],
                  'state_dir':value['state_dir'],'policy_sha256':cache.fingerprint(before['persistent_cache']),
                  'storage_id':storage_id,'quota_uid':slot['quota_uid']}
        if state['binding']!=expected or any(not re.fullmatch('[0-9a-f]{64}',k) for k in state['keys']):
            raise DeployError('Cache snapshot identity differs from the original policy')
        usage=snapshot['usage'];directories=snapshot['directories']
        if (not isinstance(usage,dict) or set(usage)!={'bytes_used','inodes_used'}
            or any(type(v) is not int or v<0 for v in usage.values()) or not isinstance(directories,list)
            or len(directories)>cache.MAX_KEYS):raise DeployError('Invalid cache native snapshot')
        seen=set()
        for item in directories:
            if (not isinstance(item,dict) or set(item)!={'key','device','inode','uid','gid','mode'}
                or not re.fullmatch('[0-9a-f]{64}',str(item['key'])) or item['key'] in seen
                or any(type(item[k]) is not int or item[k]<0 for k in ['device','inode','uid','gid','mode'])):
                raise DeployError('Invalid cache namespace snapshot')
            seen.add(item['key'])


def plan(prefix,value):
    request(value);prefix,meta,state,config=_context(prefix)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        _other(state);guard(state);assert_quiescent(state,'node')
        before=projection(config);sources=_sources(config)
        work=_select(config,'workspace',value['workspace_slot'],value['uid'],sources)
        private=_select(config,'persistent_cache',value['cache_slot'],value['uid'],sources)
        after=extend(before,value,work,private);target={**config,**after}
        validate_slots(after['workspace'],host=True);cache.validate(after['persistent_cache'],after['users'],host=True)
        old=cache.Pool(state,config);cache.Pool(state,target);_new_slots(after,value)
        snapshots={}
        for slot in before['persistent_cache']['slots']:
            with old.locked(slot) as current,old.anchor(slot):
                snapshots[slot['storage_id']]={'state':copy.deepcopy(current),**_names(old,slot)}
        result={'format':FORMAT,'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],
                'node_id':config['node_id'],'physical_id':config['physical_id'],'request':copy.deepcopy(value),
                'before':before,'after':after,'config_base_sha256':base_config(config),'meta_base_sha256':base_meta(meta),
                'control_identity':control(state),'revision':meta.get('node_owner_revision',0),'sources':sources,'caches':snapshots}
        result['id']=sha(result);_plan_check(result)
        return {'read_only':True,'plan':result,'central_acknowledgement_still_required':True,'accounts_enabled':False}


@contextmanager
def _cache_lock(pool,slot):
    for path in [pool.control,pool.paths(slot)]:
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise DeployError('Unsafe cache control path during owner binding')
    fd=os.open(pool.paths(slot)/'lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:raise DeployError('Unsafe cache control lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:os.close(fd)


def apply(prefix,value):
    require_root();_plan_check(value);prefix,meta,state,config=_context(prefix,recovery=True)
    with state_lock(state,exclusive=True),ExitStack() as stack:
        from maintenance import assert_quiescent
        _other(state);assert_quiescent(state,'node',check_spool=False)
        if config.get('spool_storage'):
            from spool_storage import Spool
            Spool(state,config).assert_empty()
        if (str(prefix)!=value['prefix'] or str(state)!=value['state_dir'] or meta['release_id']!=value['release_id']
            or control(state)!=value['control_identity'] or base_config(config)!=value['config_base_sha256']
            or base_meta(meta)!=value['meta_base_sha256'] or config.get('node_id')!=value['node_id'] or config.get('physical_id')!=value['physical_id']):
            raise DeployError('Node owner plan belongs to another or changed installation')
        before=value['before'];after=value['after'];current=projection(config);revision=meta.get('node_owner_revision',0)
        if current not in [before,after] or meta.get('persistent_cache') not in [before['persistent_cache'],after['persistent_cache']]:
            raise DeployError('Node owner configuration changed outside this plan')
        if revision not in {value['revision'],value['revision']+1} or (meta['persistent_cache']==after['persistent_cache'])!=(revision==value['revision']+1):
            raise DeployError('Node owner binding revision is stale')
        old=cache.Pool(state,{**config,**before});new=cache.Pool(state,{**config,**after})
        journal=state/JOURNAL;awaiting=state/AWAIT;history=state/HISTORY;receipt_path=history/(value['id']+'.json')
        if history.exists():
            info=history.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise DeployError('Owner binding receipts must be Root-private')
        resumed=journal.exists() or journal.is_symlink()
        expected_wait={'format':'ascend-lab-node-owner-await/v1','plan_id':value['id'],'node_id':value['node_id'],
                       'owner':value['request']['owner'],'stage_id':value['request']['stage_id']}
        if awaiting.exists() or awaiting.is_symlink():
            if read(awaiting)!=expected_wait:raise DeployError('A different owner binding awaits central acknowledgement')
        if resumed:
            if read(journal)!=value:raise DeployError('Resume only the original Node owner binding plan')
        elif receipt_path.exists():
            if current!=after or meta.get('persistent_cache')!=after['persistent_cache'] or not awaiting.exists():
                raise DeployError('Owner receipt no longer matches this configuration/central handoff')
            new.assert_idle();receipt=read(receipt_path)
            if receipt.get('plan_id')!=value['id']:raise DeployError('Owner binding receipt differs')
            return {'applied':True,'already_applied':True,'receipt':receipt,'central_acknowledgement_still_required':True,'startup_blocked':True}
        elif current!=before or revision!=value['revision'] or awaiting.exists():raise DeployError('Node owner plan is stale')
        if _sources(config)!=value['sources']:raise DeployError('Prepared native filesystem bindings changed since planning')
        _new_slots(after,value['request'],allow_marker=resumed)
        new_slot=new.slot(value['request']['cache_slot'])
        for slot in before['persistent_cache']['slots']:
            stack.enter_context(_cache_lock(old,slot));snapshot=value['caches'][slot['storage_id']]
            previous=snapshot['state'];target={**previous,'binding':new.binding(slot),'generation':previous['generation']+1}
            if cache.record(old.paths(slot)/'state.json') not in [previous,target]:raise DeployError('Old cache metadata/lease changed since planning')
            with root_fd(slot) as fd:
                if cache.record(Path('/proc/self/fd')/str(fd)/cache.MARKER) not in [old.binding(slot),new.binding(slot)]:
                    raise DeployError('Old native cache binding changed outside this operation')
            if _names(old,slot)!={k:snapshot[k] for k in ['directories','usage']}:raise DeployError('Old cache directories/usage changed since planning')
        leaf=new.paths(new_slot)
        if not resumed and leaf.exists():raise DeployError('New cache control path already exists; refusing adoption')
        if not resumed:
            if history.exists() and len(os.listdir(history))>=1024:raise DeployError('Archive owner binding audit receipts before further changes')
            if not history.exists():history.mkdir(mode=0o700);files.sync(state)
            files.write(journal,value);_checkpoint('journal')
        for slot in before['persistent_cache']['slots']:
            previous=value['caches'][slot['storage_id']]['state']
            files.write(old.paths(slot)/'state.json',{**previous,'binding':new.binding(slot),'generation':previous['generation']+1})
            _checkpoint('cache_control')
            with root_fd(slot) as fd:
                native_write(fd,cache.MARKER,new.binding(slot))
            _checkpoint('cache_marker')
        if not leaf.exists():leaf.mkdir(mode=0o700);files.sync(new.control)
        info=leaf.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise DeployError('New cache control path is unsafe')
        if set(os.listdir(leaf))-{'lock','state.json'}:raise DeployError('Unknown new cache control data; preserve the journal')
        try:fd=os.open(leaf/'lock',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:fd=None
        if fd is not None:os.fsync(fd);os.close(fd);files.sync(leaf)
        stack.enter_context(_cache_lock(new,new_slot))
        initial={'binding':new.binding(new_slot),'generation':1,'lease':None,'keys':{}}
        if (leaf/'state.json').exists() and cache.record(leaf/'state.json')!=initial:raise DeployError('New cache control contains unexpected state')
        files.write(leaf/'state.json',initial);_checkpoint('new_control')
        with root_fd(new_slot) as fd:
            marker=Path('/proc/self/fd')/str(fd)/cache.MARKER
            if marker.exists() and cache.record(marker)!=new.binding(new_slot):raise DeployError('New native cache has another binding')
            native_write(fd,cache.MARKER,new.binding(new_slot))
        _checkpoint('new_marker')
        updated={**config,**after};files.write(state/'config.json',updated);_checkpoint('config')
        files.write(prefix/'installation.json',{**meta,'persistent_cache':after['persistent_cache'],'node_owner_revision':value['revision']+1},mode=0o444)
        _checkpoint('metadata')
        receipt={'format':'ascend-lab-node-owner-receipt/v1','plan_id':value['id'],'node_id':value['node_id'],'physical_id':value['physical_id'],
            'owner':value['request']['owner'],'stage_id':value['request']['stage_id'],'revision':value['revision']+1,
            'policy_revision':after['policy_revision'],'before_projection_sha256':sha(before),'after_projection':after,
            'after_configuration_sha256':sha(updated),'completed_at':now(),'central_acknowledgement_still_required':True}
        if receipt_path.exists():
            receipt=read(receipt_path)
            if receipt.get('plan_id')!=value['id']:raise DeployError('Owner binding receipt identity differs')
        else:files.write(receipt_path,receipt)
        _checkpoint('receipt');files.write(awaiting,expected_wait);_checkpoint('handoff')
        journal.unlink();files.sync(state);_checkpoint('published')
        return {'applied':True,'already_applied':False,'resumed':resumed,'receipt':receipt,
                'central_acknowledgement_still_required':True,'startup_blocked':True,'accounts_enabled':False}

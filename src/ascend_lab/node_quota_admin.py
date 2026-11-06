"""Stopped Node quota CAS with central approval and preserved cache namespaces."""
from contextlib import contextmanager,ExitStack
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from deployment import DeployError,require_root,verify,regular_json
import filesystem_quota as quota
from lab_client import now
from node_owner_admin import projection,control,_cache_lock,PROJECT
import payload_quota_admin as files
import persistent_cache as cache
import quota_workspace as native
from state_lock import state_lock
from workspace_quota_admin import _read,_write,_marker,_identity
from workspace_slots import validate as validate_slots,root_fd

CAPABILITY='node-quota-resize-v1'
FORMAT='ascend-lab-node-quota-plan/v1'
JOURNAL='node-quota-change.json'
HISTORY='node-quota-history'
INTENT='.ascend-lab-userquota-resizing.json'
MAX_RECORD=1024**2


def sha(value):return cache.sha(value)
def limits(slot):return {k:slot[k] for k in ['bytes','inodes']}
def base_config(config):return sha({k:v for k,v in config.items() if k not in {*PROJECT,'node_key'}})
def base_meta(meta):return sha({k:v for k,v in meta.items() if k not in {'persistent_cache','node_quota_revision'}})
def _checkpoint(stage):pass


def guard(state):
    path=Path(state)/JOURNAL
    if path.exists() or path.is_symlink():raise DeployError('Resume the original pending Node quota change before startup or maintenance')


def selected(value,which='before'):
    uid=value[which]['users'][value['owner']]['uid']
    matches=[s for s in value[which][value['kind']]['slots'] if s.get('quota_uid')==uid]
    if len(matches)!=1:raise DeployError('Choose one existing mapped UID quota allocation')
    return matches[0]


def validate_plan(value):
    expected={'format','prefix','release_id','state_dir','control_identity','config_base_sha256','meta_base_sha256',
        'revision','node_id','physical_id','owner','kind','before','after','root','native_before','native_after','usage','cache_snapshots','id'}
    if (not isinstance(value,dict) or set(value)!=expected or value['format']!=FORMAT
        or value['id']!=sha({k:v for k,v in value.items() if k!='id'})):raise DeployError('Invalid immutable Node quota plan')
    if len(json.dumps(value,allow_nan=False).encode())>MAX_RECORD:raise DeployError('Node quota plan exceeds native journal budget')
    if any(not native.canonical(value[k]) for k in ['prefix','state_dir','root']):raise DeployError('Quota plan paths must be canonical')
    if (not re.fullmatch('worker-[0-9a-f]{12}',str(value['node_id'])) or not re.fullmatch('[0-9a-f]{64}',str(value['physical_id']))
        or any(not re.fullmatch('[0-9a-f]{64}',str(value[k])) for k in ['release_id','config_base_sha256','meta_base_sha256'])
        or type(value['revision']) is not int or value['revision']<0 or value['kind'] not in {'workspace','persistent_cache'}):
        raise DeployError('Invalid Node quota identity or revision')
    if (not isinstance(value['control_identity'],dict) or set(value['control_identity'])!={'device','inode'}
        or any(type(x) is not int or x<=0 for x in value['control_identity'].values())):raise DeployError('Invalid quota control identity')
    before=value['before'];after=value['after']
    if (not isinstance(before,dict) or set(before)!={'users','workspace','persistent_cache','policy_revision','owner_bindings'}
        or not isinstance(after,dict) or set(after)!=set(before) or not isinstance(value['owner'],str) or value['owner'] not in before['users']
        or type(before['policy_revision']) is not int or before['policy_revision']<1):raise DeployError('Invalid quota profile projection')
    old=selected(value);new=selected(value,'after')
    native.requested_limits([{'uid':old['quota_uid'],**limits(new)}])
    wanted=copy.deepcopy(before);wanted[value['kind']]['slots']=[{**s,**limits(new)} if s['id']==old['id'] else s for s in wanted[value['kind']]['slots']]
    wanted['policy_revision']+=1
    if wanted!=after or limits(old)==limits(new):raise DeployError('Only one existing owner limits and the policy revision may change')
    for policy in [before,after]:
        validate_slots(policy['workspace']);
        if policy['persistent_cache']:cache.validate(policy['persistent_cache'],policy['users'])
    for key in ['native_before','native_after']:_marker(value[key],value['root'])
    source=value['native_before'];target=copy.deepcopy(source)
    target['slots']=[new if s==old else s for s in target['slots']]
    current=[s for s in before[value['kind']]['slots'] if str(Path(s['path']).parent)==value['root']]
    if (not native.is_quota(old) or str(Path(old['path']).parent)!=value['root']
        or sorted(current,key=lambda s:s['id'])!=sorted(source['slots'],key=lambda s:s['id'])
        or target!=value['native_after']):raise DeployError('Resize requires this Node to register the whole selected native filesystem')
    if (not isinstance(value['usage'],dict) or set(value['usage'])!={'bytes_used','inodes_used'}
        or any(type(x) is not int or x<0 for x in value['usage'].values())):raise DeployError('Invalid planned quota usage')
    capacity(value['usage'],new)
    if not isinstance(value['cache_snapshots'],dict):raise DeployError('Invalid private cache snapshots')
    expected_ids={s['storage_id'] for s in before['persistent_cache'].get('slots',[])} if value['kind']=='persistent_cache' else set()
    if set(value['cache_snapshots'])!=expected_ids:raise DeployError('Cache quota resize must preserve every existing cache binding')
    for snapshot in value['cache_snapshots'].values():
        if not isinstance(snapshot,dict) or set(snapshot)!={'state','directories','usage'}:raise DeployError('Invalid preserved cache snapshot')
        state=snapshot['state']
        if (not isinstance(state,dict) or set(state)!={'binding','generation','lease','keys'} or state['lease'] is not None
            or type(state['generation']) is not int or state['generation']<1 or not isinstance(state['keys'],dict)
            or len(state['keys'])>cache.MAX_KEYS or not isinstance(snapshot['directories'],list)
            or len(snapshot['directories'])>cache.MAX_KEYS):raise DeployError('Cache must be idle and bounded before resizing')
        if (not isinstance(snapshot['usage'],dict) or set(snapshot['usage'])!={'bytes_used','inodes_used'}
            or any(type(x) is not int or x<0 for x in snapshot['usage'].values())):raise DeployError('Invalid preserved cache usage')
        if len({x.get('key') for x in snapshot['directories'] if isinstance(x,dict)})!=len(snapshot['directories']):
            raise DeployError('Duplicate cache namespace snapshot')
        for item in snapshot['directories']:
            if (not isinstance(item,dict) or set(item)!={'key','device','inode','uid','gid','mode'}
                or not re.fullmatch('[0-9a-f]{64}',str(item['key']))
                or any(type(item[k]) is not int or item[k]<0 for k in ['device','inode','uid','gid','mode'])):
                raise DeployError('Invalid preserved namespace identity')
    return value


def capacity(usage,slot):
    if slot['bytes']<usage['bytes_used']+65536 or slot['inodes']<usage['inodes_used']+16:
        raise DeployError('New UID quota lacks space for retained data and metadata headroom')


def native_capacity(fd,marker):
    available=os.fstatvfs(fd);remaining_bytes=0;remaining_inodes=0
    for slot in marker['slots']:
        current=quota.read(fd,slot['quota_uid'])
        remaining_bytes+=max(0,slot['bytes']-current['bytes_used'])
        remaining_inodes+=max(0,slot['inodes']-current['inodes_used'])
    if (available.f_bavail*available.f_frsize<remaining_bytes+16*1024**2
        or available.f_favail<remaining_inodes+128):raise DeployError('Native free capacity cannot cover approved UID limits and metadata headroom')


@contextmanager
def context(prefix,recovery=False):
    require_root();prefix,meta,release=verify(prefix,quota_recovery=recovery)
    if meta['role']!='node' or hashlib.sha256((release/Path(__file__).name).read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():
        raise DeployError('Activate matching Node quota administration code first')
    state=Path(meta['state_dir']);config=regular_json(state/'config.json',private=True)
    if not recovery:
        from node_quota_change import node_history
        if node_history(state)['revision']!=meta.get('node_quota_revision',0):raise DeployError('Node quota history does not match the installed revision')
    from node_owner_admin import guard as owner_guard
    from node_owner_provision import guard as participant_guard
    owner_guard(state);participant_guard(state)
    if (state/'workspace-owner-change.json').exists():raise DeployError('Finish native UID append before resizing')
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,'node',check_spool=not recovery)
        if recovery and config.get('spool_storage'):
            from spool_storage import Spool
            Spool(state,config).assert_empty()
        yield prefix,meta,state,config


def directories(pool,slot,fd):
    result=[]
    for name in sorted(set(os.listdir(fd))-{cache.MARKER}):
        info=pool.inspect_key(fd,name,slot)
        result.append({'key':name,'device':info.st_dev,'inode':info.st_ino,'uid':info.st_uid,'gid':info.st_gid,'mode':info.st_mode})
    return result


def plan(prefix,owner,kind,byte_hard,inode_hard):
    if kind not in {'workspace','persistent_cache'}:raise DeployError('Choose workspace or persistent_cache')
    with context(prefix) as (prefix,meta,state,config):
        guard(state);before=projection(config)
        if owner not in before['users']:raise DeployError('Choose an existing mapped owner')
        chosen=selected({'before':before,'owner':owner,'kind':kind});root=str(Path(chosen['path']).parent)
        after=copy.deepcopy(before)
        for slot in after[kind]['slots']:
            if slot['id']==chosen['id']:slot.update(bytes=byte_hard,inodes=inode_hard)
        after['policy_revision']+=1
        with native.directory(root) as fd:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);marker=_marker(native.read_marker(fd),root);_identity(fd,marker)
            current=quota.require(fd,chosen['quota_uid'],chosen['bytes'],chosen['inodes'])
            target=copy.deepcopy(marker);target['slots']=[next(s for s in after[kind]['slots'] if s['id']==chosen['id']) if s==chosen else s for s in target['slots']]
            snapshots={}
            if kind=='persistent_cache':
                pool=cache.Pool(state,config)
                for slot in before[kind]['slots']:
                    with pool.locked(slot) as saved,pool.anchor(slot) as (native_fd,_):
                        usage=quota.require(native_fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
                        snapshots[slot['storage_id']]={'state':copy.deepcopy(saved),'directories':directories(pool,slot,native_fd),
                            'usage':{k:usage[k] for k in ['bytes_used','inodes_used']}}
            value={'format':FORMAT,'prefix':str(prefix),'release_id':meta['release_id'],'state_dir':str(state),'control_identity':control(state),
                'config_base_sha256':base_config(config),'meta_base_sha256':base_meta(meta),'revision':meta.get('node_quota_revision',0),
                'node_id':config['node_id'],'physical_id':config['physical_id'],'owner':owner,'kind':kind,'before':before,'after':after,
                'root':root,'native_before':marker,'native_after':target,'usage':{k:current[k] for k in ['bytes_used','inodes_used']},'cache_snapshots':snapshots}
            value['id']=sha(value);validate_plan(value)
            if len(json.dumps(value).encode())>MAX_RECORD:raise DeployError('Node quota plan exceeds the native journal budget')
            native_capacity(fd,target)
            return {'read_only':True,'plan':value,'central_approval_required':True,'cache_bytes_modified':False}


@contextmanager
def slot_fd(parent,slot):
    fd=os.open(Path(slot['path']).name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    try:
        info=os.fstat(fd)
        from storage_files import mount
        if (info.st_dev,info.st_ino,info.st_uid,stat.S_IMODE(info.st_mode))!=(slot['device'],slot['root_inode'],0,0o700) or mount(fd)!=mount(parent):
            raise DeployError('Quota slot physical identity changed')
        yield fd
    finally:os.close(fd)


def acknowledgement(value,plan):
    if (not isinstance(value,dict) or set(value)!={'format','plan_id','approval_id','node_id','physical_id','before_sha256','after_sha256','state'}
        or value['format']!='ascend-lab-node-quota-approval/v1' or not re.fullmatch('[0-9a-f]{32}',str(value['approval_id']))
        or any(value[k]!=wanted for k,wanted in {'plan_id':plan['id'],'node_id':plan['node_id'],'physical_id':plan['physical_id'],
            'before_sha256':sha(plan['before']),'after_sha256':sha(plan['after'])}.items())
        or value['state'] not in {'approved','ready','observed'}):raise DeployError('Central Node quota approval differs from this plan')
    return value


def apply(prefix,value):
    require_root();validate_plan(value)
    with context(prefix,True) as (prefix,meta,state,config),ExitStack() as stack:
        if (str(prefix),meta['state_dir'],meta['release_id'],control(state),base_config(config),base_meta(meta))!=(
            value['prefix'],value['state_dir'],value['release_id'],value['control_identity'],value['config_base_sha256'],value['meta_base_sha256']):
            raise DeployError('Node quota plan belongs to another or changed installation')
        before=value['before'];after=value['after'];old=selected(value);new=selected(value,'after')
        if (projection(config) not in [before,after] or meta.get('node_quota_revision',0) not in {value['revision'],value['revision']+1}
            or meta.get('persistent_cache',{}) not in [before['persistent_cache'],after['persistent_cache']]):
            raise DeployError('Node quota plan has been superseded')
        journal=state/JOURNAL;history=state/HISTORY;receipt_path=history/(value['id']+'.json')
        if history.exists() or history.is_symlink():
            info=history.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700:
                raise DeployError('Node quota receipts must be Root-private directories')
        resumed=journal.exists() or journal.is_symlink()
        if resumed and regular_json(journal,max_bytes=MAX_RECORD,private=True)!=value:raise DeployError('Resume only the original Node quota plan')
        if not resumed and receipt_path.exists():
            if projection(config)!=after or meta.get('node_quota_revision',0)!=value['revision']+1:raise DeployError('Historical quota receipt is no longer current')
            validate_slots(after['workspace'],host=True)
            if after['persistent_cache']:cache.Pool(state,config).assert_idle()
            return {'applied':True,'already_applied':True,'historical_receipt_only':True,'receipt':regular_json(receipt_path,private=True)}
        if not resumed and (projection(config)!=before or meta.get('node_quota_revision',0)!=value['revision']):raise DeployError('Quota plan is stale')
        from worker import NodeClient
        client=NodeClient(config);approval=acknowledgement(client.call('quota-status',{'plan_id':value['id']}),value)
        fd=stack.enter_context(native.directory(value['root']));fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        _identity(fd,value['native_before']);marker=_read(fd,native.MARKER);intent=_read(fd,INTENT,True)
        if intent is not None and intent!=value:raise DeployError('Another quota operation owns this filesystem')
        if marker not in [value['native_before'],value['native_after']] or not resumed and marker!=value['native_before']:
            raise DeployError('Native quota marker changed outside this operation')
        for slot in value['native_before']['slots']:
            current=quota.read(fd,slot['quota_uid'])
            if slot['id']==old['id']:
                if {k:current[k] for k in value['usage']}!=value['usage']:raise DeployError('Retained UID data changed since planning')
                capacity(current,new)
                if not resumed:quota.require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
            else:quota.require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
        native_capacity(fd,value['native_after'])
        old_pool=cache.Pool(state,{**config,**before}) if value['kind']=='persistent_cache' else None
        new_pool=cache.Pool(state,{**config,**after}) if old_pool else None
        if not old_pool and config.get('persistent_cache'):cache.Pool(state,config).assert_idle()
        anchors={}
        for slot in before[value['kind']]['slots']:
            if str(Path(slot['path']).parent)==value['root']:anchors[slot['id']]=stack.enter_context(slot_fd(fd,slot))
        if old_pool:
            for slot in before['persistent_cache']['slots']:
                stack.enter_context(_cache_lock(old_pool,slot))
                snapshot=value['cache_snapshots'][slot['storage_id']];saved=cache.record(old_pool.paths(slot)/'state.json')
                target={**snapshot['state'],'binding':new_pool.binding(slot),'generation':snapshot['state']['generation']+1}
                if snapshot['state']['binding']!=old_pool.binding(slot) or saved not in [snapshot['state'],target]:raise DeployError('Private cache control changed since planning')
                anchor=anchors.get(slot['id'])
                if anchor is None:anchor=stack.enter_context(root_fd(slot));anchors[slot['id']]=anchor
                if directories(old_pool,slot,anchor)!=snapshot['directories']:raise DeployError('Retained cache namespaces changed since planning')
                current=quota.read(anchor,slot['quota_uid'])
                if {k:current[k] for k in snapshot['usage']}!=snapshot['usage']:raise DeployError('Retained cache usage changed since planning')
                binding=_read(anchor,cache.MARKER)
                if binding not in [old_pool.binding(slot),new_pool.binding(slot)]:raise DeployError('Private cache filesystem binding changed')
        if not resumed:
            if history.exists() and len(os.listdir(history))>=1024:raise DeployError('Node quota receipt limit reached; preserve history for an explicit migration')
            if not history.exists():history.mkdir(mode=0o700);files.sync(state)
            files.write(journal,value)
        _checkpoint('journal')
        if intent is None:_write(fd,INTENT,value)
        _checkpoint('intent')
        quota.resize(fd,old['quota_uid'],limits(old),limits(new));_checkpoint('quota')
        _write(fd,native.MARKER,value['native_after']);_checkpoint('native_marker')
        if old_pool:
            for slot in before['persistent_cache']['slots']:
                snapshot=value['cache_snapshots'][slot['storage_id']]
                target={**snapshot['state'],'binding':new_pool.binding(slot),'generation':snapshot['state']['generation']+1}
                files.write(old_pool.paths(slot)/'state.json',target);_checkpoint('cache_control')
                _write(anchors[slot['id']],cache.MARKER,new_pool.binding(slot));_checkpoint('cache_marker')
        updated={**config,**after};files.write(state/'config.json',updated);_checkpoint('config')
        files.write(prefix/'installation.json',{**meta,'persistent_cache':after['persistent_cache'],'node_quota_revision':value['revision']+1},mode=0o444)
        _checkpoint('metadata')
        for slot in value['native_after']['slots']:quota.require(fd,slot['quota_uid'],slot['bytes'],slot['inodes'])
        if _read(fd,native.MARKER)!=value['native_after']:raise DeployError('Native quota publication differs from the approved policy')
        receipt={'format':'ascend-lab-node-quota-receipt/v1','plan_id':value['id'],'approval_id':approval['approval_id'],
            'node_id':value['node_id'],'owner':value['owner'],'kind':value['kind'],'revision':value['revision']+1,
            'before':limits(old),'after':limits(new),'preserved_usage':value['usage'],'namespace_data_modified':False,
            'after_sha256':sha(after),'completed_at':now()}
        if receipt_path.exists():
            receipt=regular_json(receipt_path,private=True)
            if receipt['plan_id']!=value['id'] or receipt['approval_id']!=approval['approval_id']:raise DeployError('Quota receipt differs from central approval')
        else:files.write(receipt_path,receipt)
        _checkpoint('receipt')
        reply=acknowledgement(client.call('quota-ready',{'plan_id':value['id'],'approval_id':approval['approval_id'],
            'after_sha256':sha(after),'receipt_sha256':sha(receipt)}),value)
        if reply['state'] not in {'ready','observed'}:raise DeployError('Central quota acknowledgement is incomplete')
        _checkpoint('ready')
        if _read(fd,INTENT,True) is not None:os.unlink(INTENT,dir_fd=fd);os.fsync(fd)
        _checkpoint('intent_removed');journal.unlink();files.sync(state);_checkpoint('published')
        return {'applied':True,'already_applied':False,'resumed':resumed,'receipt':receipt,
            'node_restart_required':True,'central_healthy_heartbeat_still_required':True}

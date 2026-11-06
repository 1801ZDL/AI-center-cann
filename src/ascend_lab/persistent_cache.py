"""Private persistent cache provider and explicit admission contracts.

Uses an independent, already prepared UID-quota filesystem. Control records
live on the node control filesystem. No automatic mounting, provisioning,
cross-user sharing, unconfirmed lease release, or ordinary-directory fallback.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from lab_client import atomic_json,now
from workspace_slots import root_fd,validate as validate_slots
from quota_workspace import is_quota
from filesystem_quota import require

CAPABILITY='private-persistent-cache-v1'
MARKER='.ascend-lab-cache.json'
MAX_KEYS=128


class CacheUnavailable(RuntimeError):pass
class CacheBusy(CacheUnavailable):pass


def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate(policy,users=None,host=False):
    validate_slots(policy,host=host)
    if any(not is_quota(s) for s in policy['slots']):raise ValueError('Persistent cache requires prepared ext4 UID hard quotas')
    if len({s['quota_uid'] for s in policy['slots']})!=len(policy['slots']):raise ValueError('A cache owner must have one registered quota allocation')
    if users is not None:
        for slot in policy['slots']:
            mapped=[v for v in users.values() if v.get('uid')==slot['quota_uid']]
            if len(mapped)!=1:raise ValueError('Cache quota UID must map to exactly one owner')
    return policy


def fingerprint(policy):return sha(validate(policy))


def namespace(config,grant,host=True):
    from environment_contract import verify_config
    environment=verify_config(config,host=host)
    if not environment or grant.get('environment_sha256')!=environment:
        raise ValueError('Persistent cache requires the exact certified node environment')
    if grant.get('owner') not in config['users'] or not grant.get('runtime_files'):
        raise ValueError('Persistent cache requires a mapped owner and frozen execution runtime')
    return sha({'format':'private-cache-key/v1','owner':grant['owner'],'image':config['image'],
        'environment_sha256':environment,'runtime_files':grant['runtime_files'],
        'lock':grant['parameters']['lock'],'environment':grant['parameters'].get('environment',{}),
        'npu_policy_sha256':grant.get('npu_policy_sha256')})


def observation(value,policy):
    validate(policy)
    if not isinstance(value,dict) or set(value)!={'policy_sha256','slots'} or value['policy_sha256']!=fingerprint(policy):
        raise ValueError('Missing or mismatched persistent cache observation')
    slots=value['slots'];registered={s['id']:s for s in policy['slots']};seen=set()
    if not isinstance(slots,list) or len(slots)!=len(registered):raise ValueError('Missing registered cache observations')
    for item in slots:
        if not isinstance(item,dict) or set(item)!={'id','status','leased','free_bytes','free_inodes','namespaces'}:
            raise ValueError('Invalid cache observation fields')
        if item['id'] not in registered or item['id'] in seen:raise ValueError('Unknown or duplicate cache observation')
        seen.add(item['id']);slot=registered[item['id']]
        if item['status'] not in {'ok','error'} or type(item['leased']) is not bool:raise ValueError('Invalid cache status or lease')
        for key,limit in [('free_bytes',slot['bytes']),('free_inodes',slot['inodes']),('namespaces',MAX_KEYS)]:
            if type(item[key]) is not int or not 0<=item[key]<=limit:raise ValueError('Cache observation exceeds registered bounds')
        if item['status']=='error' and (not item['leased'] or item['free_bytes'] or item['free_inodes']):
            raise ValueError('Unavailable cache cannot advertise reusable capacity')
    return value


def aliases(current,peer):
    """Cache filesystems are exclusively assigned to one node, across all roles."""
    for a,b in [(current,peer),(peer,current)]:
        caches=a.get('persistent_cache',{}).get('slots',[])
        others=[*b.get('persistent_cache',{}).get('slots',[]),*b.get('workspace',{}).get('slots',[]),
            *([b['spool_storage']] if b.get('spool_storage') else [])]
        for cache in caches:
            for other in others:
                if (cache['device']==other['device'] or cache['storage_id']==other['storage_id']
                    or Path(cache['path']).is_relative_to(other['path']) or Path(other['path']).is_relative_to(cache['path'])):
                    raise ValueError('Persistent cache aliases another node storage allocation')


def available(profile,owner,reading,active):
    policy=profile.get('persistent_cache')
    if not policy:return None
    held={j['allocation']['cache']['storage_id'] for j in active if j.get('allocation',{}).get('cache')}
    seen={s['id']:s for s in reading.get('slots',[])}
    uid=profile['users'][owner]['uid']
    for slot in policy['slots']:
        status=seen.get(slot['id'],{})
        if slot['quota_uid']==uid and slot['storage_id'] not in held and status.get('status')=='ok' and status.get('leased') is False:
            return slot,status
    return None


def record(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
            raise CacheUnavailable('Unsafe private cache metadata')
        data=stream.read(256*1024+1)
    if len(data)>256*1024:raise CacheUnavailable('Cache metadata exceeds its bound')
    try:value=json.loads(data)
    except ValueError:raise CacheUnavailable('Cache metadata is incomplete; preserve it for reconciliation') from None
    if not isinstance(value,dict):raise CacheUnavailable('Invalid cache metadata')
    return value


class Pool:
    def __init__(self,state,config):
        self.state=Path(state).absolute();self.config=config
        self.policy=validate(config['persistent_cache'],config['users'])
        if os.geteuid()!=0:raise CacheUnavailable('Private cache requires the trusted Root node')
        if not re.fullmatch('worker-[0-9a-f]{12}',str(config.get('node_id'))) or not re.fullmatch('[0-9a-f]{64}',str(config.get('physical_id'))):
            raise CacheUnavailable('Cache requires an enrolled physical node identity')
        self.control=self.state/'persistent-cache'
        devices={s['device'] for s in self.policy['slots']}
        parent=self.state
        while not parent.exists():parent=parent.parent
        if parent.stat().st_dev in devices:raise CacheUnavailable('Cache and node control filesystems must be separate')
        if devices&{s['device'] for s in config.get('workspace',{}).get('slots',[])}:
            raise CacheUnavailable('Cache cannot share the user workspace filesystem')
        if config.get('spool_storage',{}).get('device') in devices:
            raise CacheUnavailable('Cache cannot share the trusted node spool filesystem')
        for slot in self.policy['slots']:
            cache=Path(slot['path'])
            for path in [self.state,*[Path(s['path']) for s in config.get('workspace',{}).get('slots',[])],
                         *([Path(config['spool_storage']['path'])] if config.get('spool_storage') else [])]:
                if path.is_relative_to(cache) or cache.is_relative_to(path):raise CacheUnavailable('Cache and other node paths must be disjoint')

    def binding(self,slot):
        return {'format':'private-persistent-cache/v1','node_id':self.config['node_id'],
            'physical_id':self.config['physical_id'],'state_dir':str(self.state),
            'policy_sha256':fingerprint(self.policy),'storage_id':slot['storage_id'],'quota_uid':slot['quota_uid']}

    def slot(self,identifier):
        matches=[s for s in self.policy['slots'] if s['id']==identifier]
        if len(matches)!=1:raise CacheUnavailable('Unknown registered cache slot')
        return matches[0]

    def paths(self,slot):return self.control/slot['storage_id']

    def initialize(self):
        # Validate every native source before creating any control or marker.
        info=self.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:
            raise CacheUnavailable('Initialize requires an existing Root-private control directory')
        validate(self.policy,self.config['users'],host=True)
        for slot in self.policy['slots']:
            with root_fd(slot) as fd:
                if os.listdir(fd):raise CacheUnavailable('Initialize requires empty prepared cache slots')
        if self.control.exists():raise CacheUnavailable('Cache control directory already exists; do not overwrite it')
        self.control.mkdir(mode=0o700)
        for slot in self.policy['slots']:
            state=self.paths(slot);state.mkdir(mode=0o700)
            fd=os.open(state/'lock',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:os.fsync(fd)
            finally:os.close(fd)
            value={'binding':self.binding(slot),'generation':1,'lease':None,'keys':{}}
            atomic_json(state/'state.json',value)
            with root_fd(slot) as fd:
                atomic_json(Path('/proc/self/fd')/str(fd)/MARKER,self.binding(slot));os.fsync(fd)

    def inspect_new(self):
        validate(self.policy,self.config['users'],host=True)
        for slot in self.policy['slots']:
            with root_fd(slot) as fd:
                if os.listdir(fd):raise CacheUnavailable('Installation requires empty prepared private cache slots')
        return {'slots':len(self.policy['slots']),'policy_sha256':fingerprint(self.policy),'native_uid_hard_quotas':True,
                'control_workspace_spool_separate':True,'read_only':True}

    def assert_idle(self):
        for slot in self.policy['slots']:
            with self.locked(slot) as value,self.anchor(slot) as (fd,base):
                if value['lease'] is not None or value.get('creating'):raise CacheBusy('Reconcile every persistent cache lease before offline maintenance')
                for name in set(os.listdir(fd))-{MARKER}:self.inspect_key(fd,name,slot)

    def listing(self):
        result=[]
        for slot in self.policy['slots']:
            with self.locked(slot) as value,self.anchor(slot) as (fd,base):
                result.append({'slot_id':slot['id'],'bytes':slot['bytes'],'inodes':slot['inodes'],
                    'generation':value['generation'],'lease':({k:value['lease'][k] for k in ['job_id','attempt_id','key']} if value['lease'] else None),
                    'keys':[{ 'key':name,**value['keys'].get(name,{})} for name in sorted(set(os.listdir(fd))-{MARKER})]})
        return {'policy_sha256':fingerprint(self.policy),'slots':result}

    def rebind(self,state,config,staged):
        """Offline same-node/same-cache restore. Data is retained, never retagged."""
        self.assert_idle()
        if (config.get('persistent_cache')!=self.policy or config.get('node_id')!=self.config['node_id']
            or config.get('physical_id')!=self.config['physical_id']):raise CacheUnavailable('Cache restore requires the same physical node and cache policy')
        target=Pool(state,config);staged=Path(staged)
        for slot in self.policy['slots']:
            with self.locked(slot) as value,self.anchor(slot) as (fd,base):
                # Use the current cache metadata rather than rewinding eviction
                # generations to an older snapshot. All native data stays put.
                value['binding']=target.binding(slot);value['generation']+=1
                leaf=staged/'persistent-cache'/slot['storage_id']
                if not leaf.is_dir() or leaf.is_symlink():raise CacheUnavailable('Snapshot lacks registered cache control paths')
                atomic_json(leaf/'state.json',value)
                atomic_json(base/MARKER,target.binding(slot));os.fsync(fd)

    @contextmanager
    def locked(self,slot):
        for path in [self.state,self.control,self.paths(slot)]:
            info=path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:
                raise CacheUnavailable('Cache control path must remain Root-private')
        fd=os.open(self.paths(slot)/'lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
                raise CacheUnavailable('Unsafe cache control lock')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise CacheBusy('Cache control transaction is in use') from None
            value=record(self.paths(slot)/'state.json')
            if set(value)-{'binding','generation','lease','keys','creating'} or not {'binding','generation','lease','keys'}<=set(value):
                raise CacheUnavailable('Unknown or missing cache control fields')
            if value.get('binding')!=self.binding(slot) or type(value.get('generation')) is not int or value['generation']<1:
                raise CacheUnavailable('Cache control binding differs from this node')
            if not isinstance(value.get('keys'),dict) or len(value['keys'])>MAX_KEYS or any(not re.fullmatch('[0-9a-f]{64}',k) for k in value['keys']):
                raise CacheUnavailable('Invalid cache namespace metadata')
            yield value
        finally:os.close(fd)

    @contextmanager
    def anchor(self,slot):
        try:
            with root_fd(slot) as fd:
                base=Path('/proc/self/fd')/str(fd)
                if record(base/MARKER)!=self.binding(slot):raise CacheUnavailable('Native cache is bound to another node/control state')
                names=set(os.listdir(fd))-{MARKER}
                if len(names)>MAX_KEYS or any(not re.fullmatch('[0-9a-f]{64}',name) for name in names):
                    raise CacheUnavailable('Unknown native cache entries; preserve them')
                yield fd,base
        except (OSError,ValueError) as error:raise CacheUnavailable('Native cache source unavailable or changed; no directory fallback') from error

    def save(self,slot,value):
        value['generation']+=1;atomic_json(self.paths(slot)/'state.json',value)

    def inspect_key(self,fd,key,slot):
        info=os.stat(key,dir_fd=fd,follow_symlinks=False)
        owner=next(v for v in self.config['users'].values() if v['uid']==slot['quota_uid'])
        if (not stat.S_ISDIR(info.st_mode) or info.st_dev!=slot['device'] or info.st_uid!=owner['uid']
            or info.st_gid!=owner['gid'] or info.st_mode&0o777!=0o700):raise CacheUnavailable('Unsafe cache namespace ownership/type')
        return info

    def observe(self):
        result=[]
        for slot in self.policy['slots']:
            try:
                with self.locked(slot) as value,self.anchor(slot) as (fd,base):
                    quota=require(fd,slot['quota_uid'],slot['bytes'],slot['inodes']);fs=os.fstatvfs(fd)
                    result.append({'id':slot['id'],'status':'ok','leased':value['lease'] is not None,
                        'free_bytes':min(fs.f_bavail*fs.f_frsize,max(0,slot['bytes']-quota['bytes_used'])),
                        'free_inodes':min(fs.f_favail,max(0,slot['inodes']-quota['inodes_used'])),
                        'namespaces':len(set(os.listdir(fd))-{MARKER})})
            except (OSError,ValueError,CacheUnavailable):
                result.append({'id':slot['id'],'status':'error','leased':True,'free_bytes':0,'free_inodes':0,'namespaces':0})
        return {'policy_sha256':fingerprint(self.policy),'slots':result}

    def prune(self,slot_id,key,expected=None):
        if not re.fullmatch('[0-9a-f]{64}',str(key)):raise ValueError('Choose one exact cache namespace SHA256')
        slot=self.slot(slot_id)
        with self.locked(slot) as value,self.anchor(slot) as (fd,base):
            if value['lease'] is not None:raise CacheBusy('Cache lease has not been released; do not delete retained data')
            info=self.inspect_key(fd,key,slot)
            plan={'slot_id':slot_id,'key':key,'policy_sha256':fingerprint(self.policy),
                'generation':value['generation'],'device':info.st_dev,'inode':info.st_ino}
            if expected is None:return {'read_only':True,'plan':plan}
            if expected!=plan:raise CacheUnavailable('Cache plan is stale; inspect again before pruning')
            def remove(parent,name,depth=0):
                if depth>64:raise CacheUnavailable('Cache cleanup depth exceeded; remainder preserved')
                item=os.stat(name,dir_fd=parent,follow_symlinks=False)
                if item.st_dev!=slot['device']:raise CacheUnavailable('Cache cleanup crossed its native filesystem')
                if stat.S_ISDIR(item.st_mode):
                    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                    try:
                        if os.fstat(child).st_ino!=item.st_ino:raise CacheUnavailable('Cache directory changed during cleanup')
                        for sub in os.listdir(child):remove(child,sub,depth+1)
                    finally:os.close(child)
                    os.rmdir(name,dir_fd=parent)
                elif stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode):os.unlink(name,dir_fd=parent)
                else:raise CacheUnavailable('Unknown cache object type; preserve it for reconciliation')
            remove(fd,key);os.fsync(fd)
            value['keys'].pop(key,None);self.save(slot,value)
            return {'removed':True,'slot_id':slot_id,'key':key,'generation':value['generation']}


class Lease:
    def __init__(self,pool,grant):
        self.pool=pool;self.grant=grant
        if grant.get('cache_policy_sha256')!=fingerprint(pool.policy):raise CacheUnavailable('Cache grant policy differs from the node')
        wanted=grant.get('allocation',{}).get('cache')
        if not isinstance(wanted,dict) or set(wanted)!={'slot_id','storage_id','key'}:raise CacheUnavailable('Missing cache allocation')
        self.slot=pool.slot(wanted['slot_id']);self.key=namespace(pool.config,grant)
        if wanted!={'slot_id':self.slot['id'],'storage_id':self.slot['storage_id'],'key':self.key}:
            raise CacheUnavailable('Cache grant namespace or native slot differs')
        if pool.config['users'][grant['owner']]['uid']!=self.slot['quota_uid']:
            raise CacheUnavailable('Another owner cannot acquire this cache quota')
        if not re.fullmatch('attempt-[0-9a-f]{32}',str(grant.get('attempt_id'))) or not re.fullmatch('job-[0-9a-f]{12}',str(grant.get('job_id'))):
            raise CacheUnavailable('Invalid cache attempt identity')
        self.binding={'attempt_id':grant['attempt_id'],'job_id':grant['job_id'],'node_id':pool.config['node_id'],
            'key':self.key,'lease_sha256':hashlib.sha256(grant['lease_token'].encode()).hexdigest()}
        self.path=Path(self.slot['path'])/self.key

    def prepare(self):
        slot=self.slot;pool=self.pool
        with pool.locked(slot) as value,pool.anchor(slot) as (fd,base):
            if value['lease'] is not None and value['lease']!=self.binding:raise CacheBusy('Cache is held by another attempt')
            if self.key not in os.listdir(fd) and len(set(os.listdir(fd))-{MARKER})>=MAX_KEYS:
                raise CacheUnavailable('Cache namespace count reached its fixed bound; explicitly prune idle namespaces')
            try:existing=os.stat(self.key,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:existing=None
            if existing and existing.st_uid==0 and (value['lease']!=self.binding or value.get('creating')!=
                {'key':self.key,'device':existing.st_dev,'inode':existing.st_ino}):
                raise CacheUnavailable('Unknown Root-owned namespace; refuse ownership transfer')
            if value['lease'] is None:
                need=self.grant.get('parameters',{}).get('cache')
                if need:
                    from workflow_spec import cache_request
                    need=cache_request(need);quota=require(fd,slot['quota_uid'],slot['bytes'],slot['inodes']);space=os.fstatvfs(fd)
                    if (min(space.f_bavail*space.f_frsize,max(0,slot['bytes']-quota['bytes_used']))<need['min_free_mib']*1024**2
                        or min(space.f_favail,max(0,slot['inodes']-quota['inodes_used']))<need['min_free_inodes']):
                        raise CacheUnavailable('Assigned cache headroom changed before start; preserve the grant and wait')
                value['lease']=self.binding;pool.save(slot,value)
            try:
                os.mkdir(self.key,0o700,dir_fd=fd)
                created=os.stat(self.key,dir_fd=fd,follow_symlinks=False)
                value['creating']={'key':self.key,'device':created.st_dev,'inode':created.st_ino};pool.save(slot,value)
            except FileExistsError:pass
            # A crash between mkdir and chown may leave an empty Root-owned
            # namespace under this exact persisted lease; no user data is retagged.
            info=os.stat(self.key,dir_fd=fd,follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and info.st_dev==slot['device'] and info.st_uid==0:
                child=os.open(self.key,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    if os.listdir(child):raise CacheUnavailable('Unknown Root-owned cache data; refusing ownership transfer')
                    mapping=pool.config['users'][self.grant['owner']];os.fchown(child,mapping['uid'],mapping['gid'])
                finally:os.close(child)
            info=pool.inspect_key(fd,self.key,slot);os.fsync(fd)
            value.pop('creating',None);value['keys'][self.key]={'last_acquired_at':now()};pool.save(slot,value)
            return {'device':info.st_dev,'inode':info.st_ino,'uid':info.st_uid,'gid':info.st_gid,
                    'bytes':slot['bytes'],'inodes':slot['inodes'],'key':self.key}

    def release(self,termination_confirmed=False):
        if termination_confirmed is not True:raise CacheUnavailable('Caller must confirm container termination before releasing its cache')
        with self.pool.locked(self.slot) as value,self.pool.anchor(self.slot) as (fd,base):
            if value['lease'] is None:return
            if value['lease']!=self.binding:raise CacheBusy('Refusing release of another cache lease')
            self.pool.inspect_key(fd,self.key,self.slot)
            value['lease']=None;value['keys'][self.key]={'last_released_at':now()};self.pool.save(self.slot,value)

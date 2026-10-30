"""Explicit finite node input/output spool, separate from control and task work.

Only administrator-prepared ext4 mount roots are accepted. No mount/format,
ordinary-directory fallback, automatic migration or unconfirmed-data deletion.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from contextlib import contextmanager

from lab_client import atomic_json
from payload_storage import root_fd,validate as payload_contract,inspect_new as inspect_payload

CAPABILITY='finite-node-spool-v1'
MARKER='.ascend-lab-spool.json'
ATTEMPT='.ascend-lab-attempt.json'


class SpoolUnavailable(RuntimeError):pass


def validate(value):
    payload_contract(value)
    if 'owner_groups' in value:raise ValueError('Node spool requires its own finite filesystem, not central owner groups')
    return value


def fingerprint(value):return hashlib.sha256(json.dumps(validate(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def identity(config,state):
    if (not re.fullmatch('worker-[0-9a-f]{12}',str(config.get('node_id')))
        or not re.fullmatch('[0-9a-f]{64}',str(config.get('physical_id')))):
        raise ValueError('Node spool requires an enrolled node and physical identity')
    return {'format':'node-spool/v1','node_id':config['node_id'],'physical_id':config['physical_id'],
            'state_dir':str(Path(state).absolute()),'policy_sha256':fingerprint(config['spool_storage'])}


def disjoint(config,state,prefix=None):
    policy=validate(config['spool_storage']);path=Path(policy['path']);state=Path(state).absolute()
    for other in [state,*([Path(prefix)] if prefix else [])]:
        if path==other or path.is_relative_to(other) or other.is_relative_to(path):raise ValueError('Node spool and control/code paths must be disjoint')
    parent=state
    while not parent.exists():parent=parent.parent
    if parent.stat().st_dev==policy['device']:raise ValueError('Node spool and control must be separate filesystems')
    for slot in config.get('workspace',{}).get('slots',[]):
        other=Path(slot['path'])
        if slot['device']==policy['device'] or path.is_relative_to(other) or other.is_relative_to(path):
            raise ValueError('Node spool and user workspace allocations must be separate')


def inspect_new(config,state,prefix=None):
    identity(config,state);disjoint(config,state,prefix)
    result=inspect_payload(config['spool_storage'],state,prefix)
    result.update(node_spool=True,control_separate=True,user_workspaces_separate=True)
    return result


def initialize(config,state):
    expected=identity(config,state);disjoint(config,state)
    with root_fd(config['spool_storage'],0) as fd:
        if os.listdir(fd):raise ValueError('Provide a new empty node spool filesystem')
        os.mkdir('attempts',0o700,dir_fd=fd)
        atomic_json(Path(config['spool_storage']['path'])/MARKER,expected);os.fsync(fd)


def private_record(path,limit=4096):
    source=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(source,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_nlink!=1:
            raise ValueError('Unsafe private node spool binding')
        body=stream.read(limit+1)
    if len(body)>limit:raise ValueError('Node spool record exceeds its bound')
    return json.loads(body)


class Spool:
    def __init__(self,state,config,check=True):
        self.state=Path(state).absolute();self.config=config;self.policy=validate(config['spool_storage'])
        self.base=Path(self.policy['path']);self.expected=identity(config,self.state)
        if check:self.check()

    def check(self):
        try:
            if os.geteuid()!=0:raise ValueError('Finite node spool requires its trusted Root service')
            disjoint(self.config,self.state)
            with root_fd(self.policy,0) as fd:
                if set(os.listdir(fd))!={'attempts',MARKER} or private_record(self.base/MARKER)!=self.expected:
                    raise ValueError('Node spool belongs to another state or has unknown entries')
                child=os.open('attempts',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    info=os.fstat(child)
                    if info.st_uid!=0 or info.st_mode&0o777!=0o700 or info.st_dev!=self.policy['device']:
                        raise ValueError('Unsafe node spool attempts directory')
                finally:os.close(child)
        except (OSError,ValueError,TypeError):raise SpoolUnavailable('Finite node spool unavailable or binding changed; control and retained work were preserved') from None

    def observe(self):
        try:
            self.check()
            with root_fd(self.policy,0) as fd:space=os.fstatvfs(fd)
            return {'status':'ok','policy_sha256':fingerprint(self.policy),'free_bytes':space.f_bavail*space.f_frsize,'free_inodes':space.f_favail}
        except SpoolUnavailable:return {'status':'error','policy_sha256':fingerprint(self.policy),'free_bytes':0,'free_inodes':0}

    def directory(self,attempt):
        if not re.fullmatch('attempt-[0-9a-f]{32}',str(attempt)):raise ValueError('Invalid node spool attempt')
        return self.base/'attempts'/attempt

    def binding(self,grant):
        return {'attempt_id':grant['attempt_id'],'job_id':grant['job_id'],'node_id':grant['node_id'],
                'lease_sha256':hashlib.sha256(grant['lease_token'].encode()).hexdigest()}

    def prepare(self,grant):
        self.directory(grant['attempt_id'])
        with self.anchor() as anchor:
            path=anchor/'attempts'/grant['attempt_id'];path.mkdir(mode=0o700,exist_ok=True)
            self.verify_attempt(path)
            names=set(p.name for p in path.iterdir());expected=self.binding(grant)
            if not names:atomic_json(path/ATTEMPT,expected)
            elif ATTEMPT not in names or private_record(path/ATTEMPT)!=expected or names-{ATTEMPT,'inputs','artifacts','artifacts-retired','export-scratch'}:
                raise SpoolUnavailable('Unknown or mismatched node spool attempt; preserve it for reconciliation')
        return self.directory(grant['attempt_id'])

    @contextmanager
    def anchor(self):
        self.check()
        with root_fd(self.policy,0) as fd:
            path=Path('/proc/self/fd')/str(fd)
            if private_record(path/MARKER)!=self.expected:raise SpoolUnavailable('Pinned spool descriptor has a different binding')
            yield path

    def verify_attempt(self,path):
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o777!=0o700 or info.st_dev!=self.policy['device']:
            raise SpoolUnavailable('Unsafe node spool attempt directory')

    def remove_attempt(self,grant):
        self.directory(grant['attempt_id'])
        with self.anchor() as anchor:
            path=anchor/'attempts'/grant['attempt_id']
            if not path.exists():return
            self.verify_attempt(path)
            if private_record(path/ATTEMPT)!=self.binding(grant) or set(p.name for p in path.iterdir())!={ATTEMPT}:
                raise SpoolUnavailable('Node spool still has retained or unknown data; do not release it')
            (path/ATTEMPT).unlink();path.rmdir()
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def assert_empty(self):
        self.check()
        if any((self.base/'attempts').iterdir()):raise SpoolUnavailable('Node spool still has attempts; finish payload cleanup before offline maintenance')

    def rebind(self,state,config):
        self.assert_empty()
        if config.get('spool_storage')!=self.policy or config.get('node_id')!=self.config.get('node_id') or config.get('physical_id')!=self.config.get('physical_id'):
            raise SpoolUnavailable('Rebinding cannot replace the physical node or spool policy')
        expected=identity(config,state);disjoint(config,state)
        with root_fd(self.policy,0) as fd:
            atomic_json(self.base/MARKER,expected);os.fsync(fd)


def observation(value,policy):
    if not isinstance(value,dict) or set(value)!={'status','policy_sha256','free_bytes','free_inodes'}:
        raise ValueError('Missing finite node spool observation')
    if value['status'] not in {'ok','error'} or value['policy_sha256']!=fingerprint(policy):raise ValueError('Node spool observation differs from enrollment')
    for key,cap in [('free_bytes',policy['bytes']),('free_inodes',policy['inodes'])]:
        if type(value[key]) is not int or not 0<=value[key]<=cap or value['status']=='error' and value[key]!=0:
            raise ValueError('Invalid finite node spool free capacity')
    return dict(value)

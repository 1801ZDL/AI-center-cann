"""Read-only control-filesystem headroom admission; not quota or retention.

Stop adding work before exhaustion. Existing attempts, leases and results remain
authoritative and must still reconcile. No scan, deletion, DB write or caching.
"""
import os

from jobs import JobError

MIB=1024**2
CAPABILITY='control-storage-admission-v1'
DEFAULT={'minimum_free_mib':64,'minimum_free_inodes':256}
HOLD='中央控制存储余量不足或无法观测，暂停新工作；保留原任务、租约和结果'


def validate(value):
    if value is None:return None
    if not isinstance(value,dict) or set(value)!=set(DEFAULT):
        raise ValueError('Control storage admission requires byte and inode thresholds')
    for key,low,high in [('minimum_free_mib',16,65536),('minimum_free_inodes',64,1000000)]:
        if type(value[key]) is not int or not low<=value[key]<=high:
            raise ValueError('Control storage admission threshold out of range')
    return dict(value)


def observe(directory,policy,filesystem=None):
    policy=validate(policy)
    result={'capability':CAPABILITY,'enabled':policy is not None,'status':'disabled',
            'blocks_new_work':False,'reason_code':None,'minimum_free_bytes':None,
            'minimum_free_inodes':None,'free_bytes':None,'free_inodes':None,
            'scope':'Control filesystem headroom snapshot; not reserved space, quota, retention or a start guarantee'}
    if policy is None:return result
    if filesystem is not None and filesystem.policy is not None:
        result.update(filesystem_enforced=True,filesystem='ext4',
                      filesystem_bytes=filesystem.policy['bytes'],filesystem_inodes=filesystem.policy['inodes'])
    result.update(minimum_free_bytes=policy['minimum_free_mib']*MIB,
                  minimum_free_inodes=policy['minimum_free_inodes'])
    try:
        if filesystem is not None:filesystem.origin()
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:space=os.fstatvfs(fd)
        finally:os.close(fd)
        if space.f_frsize<=0 or space.f_bavail<0 or space.f_favail<0 or space.f_files<=0:
            raise ValueError('Unusable control filesystem counters')
        result.update(free_bytes=space.f_bavail*space.f_frsize,free_inodes=space.f_favail)
        code=('control_storage_readonly' if space.f_flag&os.ST_RDONLY else
              'control_storage_bytes' if result['free_bytes']<result['minimum_free_bytes'] else
              'control_storage_inodes' if result['free_inodes']<result['minimum_free_inodes'] else None)
        result.update(status='pressure' if code else 'ready',blocks_new_work=bool(code),reason_code=code)
    except (OSError,ValueError):
        result.update(status='unavailable',blocks_new_work=True,reason_code='control_storage_unavailable',free_bytes=None,free_inodes=None)
    return result


class Pressure(JobError):
    def __init__(self,observation):
        super().__init__(HOLD,503)
        self.observation=observation

    def response(self):
        return {'error':'control_storage_pressure','message':HOLD,'reason_code':self.observation['reason_code'],
                'retryable':True,'original_operation_required':True}


def require(directory,policy,filesystem=None):
    observation=observe(directory,policy,filesystem)
    if observation['blocks_new_work']:raise Pressure(observation)

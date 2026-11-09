"""Permanent plan revocation, preserving original incomplete operations."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

SCHEMA='ascend-lab-maintenance-revocation/v1'
REASONS={'operator_stop','resource_pressure','superseded_after_review','maintenance_incident'}


def marker(journal):return journal.with_name(journal.name+'.revoked.json')


def checked_revocation(value,record):
    from maintenance_runner import encoded
    if (not isinstance(record,dict) or set(record)!={'schema','plan_id','prefix','reason','revoked_epoch','journal_before_sha256','anchor_before_sha256','journal_annotation'}
        or record['schema']!=SCHEMA or record['plan_id']!=value['id'] or record['prefix']!=value['prefix']
        or not isinstance(record['reason'],str) or record['reason'] not in REASONS
        or type(record['journal_annotation']) is not bool):raise ValueError('Invalid original maintenance revocation')
    at=record['revoked_epoch']
    if type(at) not in {int,float} or not 0<=at<=253402300799 or not math.isfinite(at):raise ValueError('Invalid maintenance revocation time')
    import re
    for key in ['journal_before_sha256','anchor_before_sha256']:
        item=record[key]
        if key=='journal_before_sha256' and item is None:continue
        if not isinstance(item,str) or not re.fullmatch('[a-f0-9]{64}',item):raise ValueError('Invalid revocation before-image')
    if len(encoded(record))>2048:raise ValueError('Maintenance revocation exceeds its bound')
    return record


def anchor(value,record):
    from maintenance_runner import encoded
    return encoded({'schema':'ascend-lab-maintenance-revoked-anchor/v1','plan_id':value['id'],
                    'revocation_sha256':hashlib.sha256(encoded(record)).hexdigest()})


def stopped(value,journal,state,raw):
    """Read-only fail-closed status, including interrupted revocation writes."""
    from maintenance_runner import load_journal,anchor_bytes,checked_state,progress,encoded,initial_state
    record=load_journal(marker(journal),maximum=2048)
    in_state=state.get('revocation') if isinstance(state,dict) else None
    if record is None:
        recovery=journal.parent.parent/'maintenance-recovered'
        if journal.parent.name=='maintenance-runs' and recovery.exists():
            from maintenance_plan_backup import recovered_fence
            if recovered_fence(recovery,value['id']):raise ValueError('Recovered maintenance evidence exists; restore the original stop records before proceeding')
        if in_state is not None:raise ValueError('Original maintenance revocation marker is missing; do not resume')
        if raw and (value['schema']=='ascend-lab-maintenance/v1' or raw!=anchor_bytes(value)):
            raise ValueError('Original maintenance revocation or execution anchor is missing or changed')
        return None
    checked_revocation(value,record)
    if state is not None:checked_state(value,state)
    if in_state is not None and in_state!=record:raise ValueError('Maintenance revocation journal differs')
    if not record['journal_annotation'] and in_state is not None:raise ValueError('Unapproved maintenance journal annotation')
    expected=anchor(value,record)
    original=anchor_bytes(value)
    previous=next((item for item in [b'',original] if hashlib.sha256(item).hexdigest()==record['anchor_before_sha256']),None)
    if previous is None:raise ValueError('Unknown original maintenance anchor')
    # A crash during the in-place seal can leave a new prefix and the old tail.
    # Only that finite set of known byte sequences is recoverable, and only
    # while the independently fsynced marker and original journal still agree.
    partial=any(raw==expected[:n]+previous[n:] for n in range(1,len(expected)+1))
    if raw not in {b'',previous,expected} and not partial:raise ValueError('Original revocation anchor differs')
    base={k:v for k,v in state.items() if k!='revocation'} if state is not None else None
    before=hashlib.sha256(encoded(base)).hexdigest() if base is not None else None
    if record['journal_before_sha256'] is None:
        if base is not None and (not record['journal_annotation'] or base!=initial_state(value)):raise ValueError('New revocation journal changed execution history')
    elif before!=record['journal_before_sha256']:raise ValueError('Maintenance journal changed after revocation intent')
    complete=(in_state==record if record['journal_annotation'] else in_state is None) and raw==expected
    return {'status':'revoked' if complete else 'revocation_pending','reason':record['reason'],
            'revoked_epoch':record['revoked_epoch'],'writes':False,'starts_service':False,'starts_workload':False,
            **(progress(value,state) if state is not None else {'plan_id':value['id'],'next_step':None,'pending':None}),
            'original_journal_present':record['journal_before_sha256'] is not None}


def _checkpoint(stage):pass


def revocation_record(value,saved,raw,reason,epoch):
    from maintenance_runner import encoded,initial_state,MAX_PLAN_BYTES
    record={'schema':SCHEMA,'plan_id':value['id'],'prefix':value['prefix'],'reason':reason,'revoked_epoch':epoch,
            'journal_before_sha256':hashlib.sha256(encoded(saved)).hexdigest() if saved is not None else None,
            'anchor_before_sha256':hashlib.sha256(raw).hexdigest(),'journal_annotation':True}
    base=initial_state(value) if saved is None else saved
    if len(encoded({**base,'revocation':record}))>MAX_PLAN_BYTES:record['journal_annotation']=False
    return checked_revocation(value,record)


def revoke_once(value,journal,*,reason='operator_stop',apply=False,epoch=None):
    from maintenance_runner import checked,execution_lock
    value=checked(value);journal=Path(journal)
    if not isinstance(reason,str) or reason not in REASONS:raise ValueError('Invalid maintenance revocation reason')
    if not apply:return _revoke_serialized(value,journal,reason=reason,apply=False,epoch=epoch)
    parent=journal.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode&0o077:raise ValueError('Maintenance directory must remain private')
    fd=execution_lock(journal.parent)
    try:return _revoke_serialized(value,journal,reason=reason,apply=True,epoch=epoch)
    finally:os.close(fd)


def _revoke_serialized(value,journal,*,reason='operator_stop',apply=False,epoch=None):
    """Internal: caller holds the prefix execution lock for the whole change."""
    from maintenance_runner import checked,load_journal,checked_state,initial_state,check_anchor,save_journal
    value=checked(value);journal=Path(journal)
    if not isinstance(reason,str) or reason not in REASONS:raise ValueError('Invalid maintenance revocation reason')
    if not apply:return {'status':'preview','plan_id':value['id'],'reason':reason,'writes':False,'starts_service':False,'starts_workload':False}
    parent=journal.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode&0o077:raise ValueError('Maintenance directory must remain private')
    fd=None
    try:
        path=journal.with_name(journal.name+'.lock')
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>512:raise ValueError('Unsafe original maintenance anchor')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);raw=os.read(fd,513)
        saved=load_journal(journal);record=load_journal(marker(journal),maximum=2048)
        if record is None:
            if saved is not None:
                checked_state(value,saved)
                if 'revocation' in saved:raise ValueError('Revocation marker is missing; preserve the original journal')
            if value['schema']=='ascend-lab-maintenance/v2':check_anchor(value,raw,saved)
            elif raw:raise ValueError('Original revocation marker is missing or changed')
            at=time.time() if epoch is None else epoch
            record=revocation_record(value,saved,raw,reason,at)
            checked_revocation(value,record);save_journal(marker(journal),record,exclusive=True)
            _checkpoint('marker_persisted')
        else:
            checked_revocation(value,record)
            # Repeating revoke resumes the original marker, even if the caller
            # now supplies a different reason/time. It never creates new credit.
        current=stopped(value,journal,saved,raw)
        if current['status']=='revoked':return {**current,'reused':True}
        state=saved
        if record['journal_annotation']:
            state={**(initial_state(value) if saved is None else saved),'revocation':record}
            save_journal(journal,state);_checkpoint('journal_persisted')
        else:_checkpoint('journal_retained')  # A full journal is preserved byte for byte.
        final=anchor(value,record);os.lseek(fd,0,os.SEEK_SET);offset=0
        while offset<len(final):
            size=os.write(fd,final[offset:])
            if size<=0:raise OSError('Revocation anchor write made no progress')
            offset+=size
        os.ftruncate(fd,len(final));os.fsync(fd)
        directory=os.open(journal.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
        _checkpoint('anchor_persisted')
        return {**stopped(value,journal,state,final),'writes':True,'reused':False}
    finally:
        if fd is not None:os.close(fd)


@contextmanager
def coordination(prefix):
    """Code activation and revocation share the same nonblocking plan lock."""
    from maintenance_runner import execution_lock
    path=Path(prefix)/'maintenance-runs';path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('Maintenance coordination directory differs')
    fd=execution_lock(path)
    try:yield
    finally:os.close(fd)


def require_compatible(prefix,manifest):
    """No silent downgrade to code that ignores a surviving revocation."""
    from maintenance_runner import read_anchor,encoded
    recovered=Path(prefix)/'maintenance-recovered'
    if recovered.exists() and 'maintenance_plan_backup.py' not in manifest.get('files',{}):
        raise ValueError('Recovered maintenance evidence requires compatible backup code')
    directory=Path(prefix)/'maintenance-runs'
    if not directory.exists():return
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('Maintenance lifecycle directory differs')
    required=False
    with os.scandir(directory) as entries:
        for index,entry in enumerate(entries):
            if index>=1024:raise ValueError('Maintenance lifecycle scan exceeds1024 entries; retain all records')
            if entry.name.endswith('.revoked.json'):required=True
            elif entry.name.endswith('.json.lock'):
                raw=read_anchor(Path(entry.path[:-5]))
                if raw:
                    try:
                        old=json.loads(raw)
                        normal=(isinstance(old,dict) and set(old)=={'schema','plan_id'}
                                and old['schema']=='ascend-lab-maintenance-anchor/v1'
                                and isinstance(old['plan_id'],str) and len(old['plan_id'])==64 and encoded(old)==raw)
                    except (ValueError,TypeError):normal=False
                    # Unknown/partial seals must not be hidden by code rollback.
                    if not normal:required=True
    if required and 'maintenance_lifecycle.py' not in manifest.get('files',{}):raise ValueError('Revoked maintenance requires compatible lifecycle code')

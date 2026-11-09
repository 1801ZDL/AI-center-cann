"""One bounded, explicitly approved offline maintenance step. No daemon/timer."""
import fcntl
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

MAX_PLAN_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 256 * 1024


def execution_contract(value):
    fields={'not_before_epoch','expires_epoch','start_minute_utc','window_minutes','max_invocations'}
    if not isinstance(value,dict) or set(value)!=fields:
        raise ValueError('Invalid maintenance execution contract')
    for key,low,high in [('not_before_epoch',0,253402300798),('expires_epoch',1,253402300799),
                         ('start_minute_utc',0,1439),('window_minutes',1,180),('max_invocations',1,128)]:
        if type(value[key]) is not int or not low<=value[key]<=high:
            raise ValueError('Invalid maintenance execution bound: '+key)
    if not 0<value['expires_epoch']-value['not_before_epoch']<=7*86400:
        raise ValueError('Maintenance execution approval must expire within seven days')
    return dict(value)


def execution_gate(contract,epoch):
    if type(epoch) not in {int,float} or not 0<=epoch<=253402300799 or not math.isfinite(epoch):
        raise ValueError('Invalid maintenance clock')
    if epoch<contract['not_before_epoch']:return 'before_start'
    if epoch>=contract['expires_epoch']:return 'expired'
    current=dt.datetime.fromtimestamp(epoch,dt.timezone.utc)
    if (current.hour*60+current.minute-contract['start_minute_utc'])%1440>=contract['window_minutes']:
        return 'outside_window'
    return None


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def checked(value):
    if not isinstance(value,dict):raise ValueError('Invalid maintenance plan')
    expected={'schema','prefix','steps','id'}
    if value.get('schema')=='ascend-lab-maintenance/v2':
        expected.add('execution');execution_contract(value.get('execution'))
    elif value.get('schema')!='ascend-lab-maintenance/v1':raise ValueError('Invalid maintenance plan')
    if set(value)!=expected:
        raise ValueError('Invalid maintenance plan')
    if value['id'] != hashlib.sha256(encoded({k: v for k, v in value.items() if k != 'id'})).hexdigest():
        raise ValueError('Maintenance approval digest differs')
    prefix = value['prefix']
    if not isinstance(prefix, str) or not prefix.startswith('/') or str(Path(prefix)) != prefix or '..' in Path(prefix).parts:
        raise ValueError('Maintenance prefix must be canonical')
    steps = value['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32 or len(encoded(value)) > MAX_PLAN_BYTES:
        raise ValueError('Maintenance selects at most 32 bounded operations')
    rotations=set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError('Invalid maintenance step')
        action = step.get('action')
        if action in {'control_copy', 'control_verify', 'control_rotate','result_rotate','grant_rotate'}:
            if set(step) != {'action', 'plan'} or not isinstance(step['plan'], dict):
                raise ValueError('An original control copy approval is required')
            from control_history import check
            check(step['plan'])
            if step['plan']['source']['bytes'] > MAX_SOURCE_BYTES:
                raise ValueError('Automatic maintenance accepts copies at most 256KiB')
            if action in {'control_rotate','result_rotate','grant_rotate'}:
                key=(action,step['plan']['id'])
                if key in rotations:raise ValueError('A control copy may be rotated only once per action in a maintenance plan')
                rotations.add(key)
        elif action == 'audit_export':
            if set(step) != {'action', 'output'} or not isinstance(step['output'], str) or not step['output'].startswith('/'):
                raise ValueError('Audit export needs a protected absolute destination')
        elif action == 'audit_prune':
            if set(step) != {'action', 'export_step'} or type(step['export_step']) is not int or not 0 <= step['export_step'] < index or steps[step['export_step']].get('action') != 'audit_export':
                raise ValueError('Audit pruning must refer to its earlier approved export')
        else:
            raise ValueError('Unregistered maintenance operation')
    return value


def plan(prefix, steps, *, execution=None):
    value = {'schema': 'ascend-lab-maintenance/v1', 'prefix': str(prefix), 'steps': steps}
    if execution is not None:
        value.update(schema='ascend-lab-maintenance/v2',execution=execution_contract(execution))
    value['id'] = hashlib.sha256(encoded(value)).hexdigest()
    return checked(value)


def with_execution(value,execution):
    value=checked(value)
    if value['schema']!='ascend-lab-maintenance/v1':
        raise ValueError('Retain the existing bounded approval; do not silently replace its execution budget')
    return plan(value['prefix'],value['steps'],execution=execution)


def operation(prefix, step, results, steps):
    action = step['action']
    if action == 'control_copy':
        from control_history import step as copy
        result = copy(prefix, step['plan'], maximum_bytes=64 * 1024)
        return result, result['state'] in {'copied', 'verified'}
    if action == 'control_verify':
        from control_history import verify_copy
        return verify_copy(prefix, step['plan']), True
    if action == 'control_rotate':
        from control_rotation import apply
        original = step['plan']
        index=next(index for index,item in enumerate(steps) if item is step)
        value=results[str(index)]['prepared_rotation_plan']
        if [item['job_id'] for item in value['indexes']] != original['selection']:
            raise ValueError('Rotation changed the approved original job selection')
        if value['export_id']!=original['id']:
            raise ValueError('Prepared rotation references another original control copy')
        return apply(prefix, value), True
    if action=='result_rotate':
        from result_history import apply
        index=next(index for index,item in enumerate(steps) if item is step)
        value=results[str(index)]['prepared_result_rotation_plan']
        if value['export_id']!=step['plan']['id'] or value['selection']!=step['plan']['selection']:
            raise ValueError('Result rotation differs from the original approved snapshot')
        return apply(prefix,value),True
    if action=='grant_rotate':
        from grant_history import apply
        index=next(index for index,item in enumerate(steps) if item is step)
        value=results[str(index)]['prepared_grant_rotation_plan']
        if value['export_id']!=step['plan']['id'] or value['selection']!=step['plan']['selection']:
            raise ValueError('Grant rotation differs from the original approved snapshot')
        return apply(prefix,value),True
    if action == 'audit_export':
        from audit_retention import export
        return export(prefix, step['output'], max_rows=32, max_bytes=64 * 1024), True
    if action == 'audit_prune':
        from audit_retention import prune
        index = step['export_step']
        receipt = results[str(index)]
        return prune(prefix, steps[index]['output'], receipt['sha256']), True
    raise ValueError('Unknown maintenance operation')


def prepare_rotation(prefix, step):
    if step['action']=='grant_rotate':
        from grant_history import plan as grant_plan
        value=grant_plan(prefix,step['plan']['id'])
        if value['selection']!=step['plan']['selection']:raise ValueError('Grant rotation changed the original producer selection')
        return value
    if step['action']=='result_rotate':
        from result_history import plan as result_plan
        value=result_plan(prefix,step['plan']['id'])
        if value['selection']!=step['plan']['selection']:raise ValueError('Result rotation changed the original job selection')
        return value
    from control_rotation import plan as rotate_plan
    value=rotate_plan(prefix,step['plan']['id'])
    if value['export_id']!=step['plan']['id'] or [item['job_id'] for item in value['indexes']]!=step['plan']['selection']:
        raise ValueError('Prepared rotation differs from the approved original selection')
    return value


def load_journal(journal,maximum=MAX_PLAN_BYTES):
    try:source=os.open(journal,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:return None
    with os.fdopen(source,'rb') as stream:
        item=os.fstat(stream.fileno())
        if not stat.S_ISREG(item.st_mode) or item.st_uid!=os.geteuid() or item.st_mode&0o077 or item.st_nlink!=1 or item.st_size>maximum:
            raise ValueError('Unsafe maintenance journal contents')
        # An existing empty or corrupt journal is never a fresh approval.
        return json.loads(stream.read(maximum+1))


def save_journal(journal,state,*,exclusive=False):
    raw=encoded(state)
    if len(raw)>MAX_PLAN_BYTES:raise ValueError('Maintenance journal exceeds its budget')
    directory=os.open(journal.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary=journal.name+'.tmp-'+uuid.uuid4().hex
    try:
        output=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        try:
            offset=0
            while offset<len(raw):
                size=os.write(output,raw[offset:])
                if size<=0:raise OSError('Maintenance journal write made no progress')
                offset+=size
            os.fsync(output)
        finally:os.close(output)
        if exclusive:os.link(temporary,journal.name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
        else:os.replace(temporary,journal.name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:os.unlink(temporary,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)


def checked_state(value,state):
    bounded=value.get('schema')=='ascend-lab-maintenance/v2'
    required={'plan_id','index','results'}|({'calls','last_invocation_epoch'} if bounded else set())
    if not isinstance(state,dict) or not required<=set(state) or set(state)-required-{'pending','revocation'} or state['plan_id']!=value['id']:
        raise ValueError('Resume the original maintenance approval; do not replace its journal')
    index=state['index']
    if type(index) is not int or not 0<=index<=len(value['steps']):raise ValueError('Invalid maintenance progress')
    results=state['results']
    if not isinstance(results,dict) or len(results)>len(value['steps']) or any(
        not isinstance(k,str) or not k.isascii() or not k.isdigit() or str(int(k))!=k or not 0<=int(k)<len(value['steps']) for k in results):
        raise ValueError('Invalid maintenance results')
    if not {str(i) for i in range(index)}<=set(results) or any(int(k)>index for k in results):
        raise ValueError('Maintenance completed results are missing or ahead of progress')
    if 'pending' in state and (type(state['pending']) is not int or state['pending']!=index or index==len(value['steps'])):
        raise ValueError('Invalid pending maintenance step')
    if 'revocation' in state:
        from maintenance_lifecycle import checked_revocation
        checked_revocation(value,state['revocation'])
    if bounded:
        calls=state['calls'];last=state['last_invocation_epoch']
        if type(calls) is not int or not index<=calls<=value['execution']['max_invocations']:
            raise ValueError('Invalid persistent maintenance invocation count')
        if calls==0:
            if last is not None or 'pending' in state:raise ValueError('Invalid unused maintenance budget')
        else:
            if last is None or execution_gate(value['execution'],last) is not None:
                raise ValueError('Invalid original maintenance execution time')
    return state


def initial_state(value):
    state={'plan_id':value['id'],'index':0,'results':{}}
    if value.get('schema')=='ascend-lab-maintenance/v2':state.update(calls=0,last_invocation_epoch=None)
    return state


def anchor_bytes(value):
    return encoded({'schema':'ascend-lab-maintenance-anchor/v1','plan_id':value['id']})


def check_anchor(value,raw,state):
    if raw:
        if raw!=anchor_bytes(value) or state is None:
            raise ValueError('Original maintenance anchor or journal is missing or changed; do not reset its budget')
        return True
    if state is not None:
        raise ValueError('Original maintenance execution anchor is missing; preserve the journal')
    return False


def read_anchor(journal):
    try:fd=os.open(journal.with_name(journal.name+'.lock'),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:return b''
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>512:
            raise ValueError('Unsafe maintenance execution anchor')
        return file.read(513)


def progress(value,state):
    result={'plan_id':value['id'],'next_step':state['index'],'pending':state.get('pending') is not None}
    if value.get('schema')=='ascend-lab-maintenance/v2':
        result.update(invocations=state['calls'],remaining_invocations=value['execution']['max_invocations']-state['calls'])
    return result


def status(value,journal):
    value=checked(value);journal=Path(journal)
    if journal.parent.exists():
        parent=journal.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode&0o077:
            raise ValueError('Maintenance journal directory must be private')
    state=load_journal(journal)
    from maintenance_lifecycle import stopped
    revoked=stopped(value,journal,state,read_anchor(journal))
    if revoked:return revoked
    if value.get('schema')=='ascend-lab-maintenance/v2':check_anchor(value,read_anchor(journal),state)
    if state is None:return {'status':'not_started','writes':False,**progress(value,initial_state(value))}
    state=checked_state(value,state)
    name='complete' if state['index']==len(value['steps']) else ('pending' if 'pending' in state else 'ready')
    if name!='complete' and value.get('schema')=='ascend-lab-maintenance/v2' and state['calls']==value['execution']['max_invocations']:
        name='budget_exhausted'
    return {'status':name,'writes':False,**progress(value,state)}


def execution_lock(parent):
    fd=os.open(parent/'.execution.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size:
            raise ValueError('Unsafe maintenance execution lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd);raise


def run_once(value, journal, *, apply=False, executor=operation, prepare=prepare_rotation, epoch=None):
    """Dry-run by default. Each call resumes exactly the same approved step.

    Real operations retain their existing Root, matching-release, stopped-role,
    source identity, archive verification and interrupted-operation guards.
    No service is stopped or started here, and there is no arbitrary deletion.
    """
    value = checked(value)
    journal=Path(journal)
    contract=value.get('execution') if value.get('schema')=='ascend-lab-maintenance/v2' else None
    clock=lambda:time.time() if epoch is None else epoch
    if not apply:
        result={'status': 'preview', 'plan_id': value['id'], 'steps': len(value['steps']),
                'writes': False, 'starts_service': False, 'starts_workload': False}
        if contract:result.update(execution=contract,execution_blocker=execution_gate(contract,clock()))
        return result
    from maintenance_lifecycle import stopped
    if journal.parent.exists():
        parent=journal.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode&0o077:raise ValueError('Maintenance journal directory must be private')
    revoked=stopped(value,journal,load_journal(journal),read_anchor(journal))
    if revoked:return revoked
    if contract:
        blocker=execution_gate(contract,clock())
        if blocker:return {'status':'deferred','reason':blocker,'plan_id':value['id'],'writes':False}
    # The CLI checks installation ownership; the journal must also be private.
    journal = Path(journal)
    parent = journal.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('Maintenance journal directory must be private')
    # Lock a stable separate inode; atomic journal replacement must not release
    # exclusivity or expose a partially overwritten previous checkpoint.
    shared=execution_lock(journal.parent);fd=None
    try:
        fd = os.open(journal.with_name(journal.name+'.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_PLAN_BYTES:
            raise ValueError('Unsafe maintenance journal')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved=load_journal(journal)
        os.lseek(fd,0,os.SEEK_SET)
        revoked=stopped(value,journal,saved,os.read(fd,513))
        if revoked:return revoked
        os.lseek(fd,0,os.SEEK_SET)
        anchored=False
        if contract:
            if info.st_size>512:raise ValueError('Invalid original maintenance anchor')
            anchored=check_anchor(value,os.read(fd,513),saved)
        state=checked_state(value,initial_state(value) if saved is None else saved)
        index = state['index']
        if index == len(value['steps']):
            return {'status': 'complete', 'reused': True,**progress(value,state)}
        if contract:
            now=clock();blocker=execution_gate(contract,now)
            if blocker:return {'status':'deferred','reason':blocker,'writes':False,**progress(value,state)}
            if state['last_invocation_epoch'] is not None and now<state['last_invocation_epoch']:
                return {'status':'deferred','reason':'clock_regressed','writes':False,**progress(value,state)}
            if state['calls']>=contract['max_invocations']:
                return {'status':'budget_exhausted','writes':False,**progress(value,state)}
            state['calls']+=1;state['last_invocation_epoch']=now

        def save():
            save_journal(journal,state)

        state['pending'] = index
        if contract and not anchored:
            # Keep an independent, never-replaced marker in the locked inode.
            # A lost journal alone can no longer silently renew the budget.
            raw=anchor_bytes(value);offset=0
            while offset<len(raw):
                size=os.write(fd,raw[offset:])
                if size<=0:raise OSError('Maintenance anchor write made no progress')
                offset+=size
            os.fsync(fd)
            directory=os.open(journal.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(directory)
            finally:os.close(directory)
        save()  # Persist original intent before an existing idempotent operation.
        # Fsync or lock acquisition may cross a window boundary. A reserved
        # invocation is retained even if no operation can start afterwards.
        if contract:
            now=clock();blocker=execution_gate(contract,now)
            if not blocker and now<state['last_invocation_epoch']:blocker='clock_regressed'
            if blocker:return {'status':'deferred','reason':blocker,'writes':True,**progress(value,state)}
        action=value['steps'][index]['action']
        if action in {'control_rotate','result_rotate','grant_rotate'} and str(index) not in state['results']:
            # Capture the derived CAS before-image before applying it. Replanning
            # after a committed mutation would lose idempotent crash recovery.
            key={'control_rotate':'prepared_rotation_plan','result_rotate':'prepared_result_rotation_plan','grant_rotate':'prepared_grant_rotation_plan'}[action]
            state['results'][str(index)]={key:prepare(value['prefix'],value['steps'][index])}
            save()
            return {'status':'pending','step':index,'step_completed':False,'prepared':True,**progress(value,state)}
        result, complete = executor(value['prefix'], value['steps'][index], state['results'], value['steps'])
        if complete:
            state['results'][str(index)] = result
            state['index'] += 1
            state.pop('pending', None)
        save()
        return {'status': 'complete' if state['index'] == len(value['steps']) else 'pending',
                'step': index, 'step_completed': complete,**progress(value,state)}
    finally:
        if fd is not None:os.close(fd)
        os.close(shared)

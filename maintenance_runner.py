"""One bounded, explicitly approved offline maintenance step. No daemon/timer."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

MAX_PLAN_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 256 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def checked(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'prefix', 'steps', 'id'} or value['schema'] != 'ascend-lab-maintenance/v1':
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
        if action in {'control_copy', 'control_verify', 'control_rotate'}:
            if set(step) != {'action', 'plan'} or not isinstance(step['plan'], dict):
                raise ValueError('An original control copy approval is required')
            from control_history import check
            check(step['plan'])
            if step['plan']['source']['bytes'] > MAX_SOURCE_BYTES:
                raise ValueError('Automatic maintenance accepts copies at most 256KiB')
            if action=='control_rotate':
                if step['plan']['id'] in rotations:raise ValueError('A control copy may be rotated only once per maintenance plan')
                rotations.add(step['plan']['id'])
        elif action == 'audit_export':
            if set(step) != {'action', 'output'} or not isinstance(step['output'], str) or not step['output'].startswith('/'):
                raise ValueError('Audit export needs a protected absolute destination')
        elif action == 'audit_prune':
            if set(step) != {'action', 'export_step'} or type(step['export_step']) is not int or not 0 <= step['export_step'] < index or steps[step['export_step']].get('action') != 'audit_export':
                raise ValueError('Audit pruning must refer to its earlier approved export')
        else:
            raise ValueError('Unregistered maintenance operation')
    return value


def plan(prefix, steps):
    value = {'schema': 'ascend-lab-maintenance/v1', 'prefix': str(prefix), 'steps': steps}
    value['id'] = hashlib.sha256(encoded(value)).hexdigest()
    return checked(value)


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
    from control_rotation import plan as rotate_plan
    value=rotate_plan(prefix,step['plan']['id'])
    if value['export_id']!=step['plan']['id'] or [item['job_id'] for item in value['indexes']]!=step['plan']['selection']:
        raise ValueError('Prepared rotation differs from the approved original selection')
    return value


def run_once(value, journal, *, apply=False, executor=operation, prepare=prepare_rotation):
    """Dry-run by default. Each call resumes exactly the same approved step.

    Real operations retain their existing Root, matching-release, stopped-role,
    source identity, archive verification and interrupted-operation guards.
    No service is stopped or started here, and there is no arbitrary deletion.
    """
    value = checked(value)
    if not apply:
        return {'status': 'preview', 'plan_id': value['id'], 'steps': len(value['steps']),
                'writes': False, 'starts_service': False, 'starts_workload': False}
    # The CLI checks installation ownership; the journal must also be private.
    journal = Path(journal)
    parent = journal.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('Maintenance journal directory must be private')
    # Lock a stable separate inode; atomic journal replacement must not release
    # exclusivity or expose a partially overwritten previous checkpoint.
    fd = os.open(journal.with_name(journal.name+'.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_PLAN_BYTES:
            raise ValueError('Unsafe maintenance journal')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            source=os.open(journal,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        except FileNotFoundError:
            saved=b''
        else:
            with os.fdopen(source,'rb') as stream:
                item=os.fstat(stream.fileno())
                if not stat.S_ISREG(item.st_mode) or item.st_uid!=os.geteuid() or item.st_mode&0o077 or item.st_nlink!=1 or item.st_size>MAX_PLAN_BYTES:
                    raise ValueError('Unsafe maintenance journal contents')
                saved=stream.read(MAX_PLAN_BYTES+1)
        state = json.loads(saved) if saved else {'plan_id': value['id'], 'index': 0, 'results': {}}
        if set(state) - {'plan_id', 'index', 'results', 'pending'} or state['plan_id'] != value['id']:
            raise ValueError('Resume the original maintenance approval; do not replace its journal')
        index = state['index']
        if type(index) is not int or not 0 <= index <= len(value['steps']):
            raise ValueError('Invalid maintenance progress')
        if index == len(value['steps']):
            return {'status': 'complete', 'plan_id': value['id'], 'reused': True}

        def save():
            raw = encoded(state)
            if len(raw) > MAX_PLAN_BYTES:
                raise ValueError('Maintenance journal exceeds its budget')
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
                os.replace(temporary,journal.name,src_dir_fd=directory,dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError:pass
                os.close(directory)

        state['pending'] = index
        save()  # Persist original intent before an existing idempotent operation.
        if value['steps'][index]['action']=='control_rotate' and str(index) not in state['results']:
            # Capture the derived CAS before-image before applying it. Replanning
            # after a committed mutation would lose idempotent crash recovery.
            state['results'][str(index)]={'prepared_rotation_plan':prepare(value['prefix'],value['steps'][index])}
            save()
            return {'status':'pending','plan_id':value['id'],'step':index,'step_completed':False,
                    'next_step':index,'prepared':True}
        result, complete = executor(value['prefix'], value['steps'][index], state['results'], value['steps'])
        if complete:
            state['results'][str(index)] = result
            state['index'] += 1
            state.pop('pending', None)
        save()
        return {'status': 'complete' if state['index'] == len(value['steps']) else 'pending',
                'plan_id': value['id'], 'step': index, 'step_completed': complete,
                'next_step': state['index']}
    finally:
        os.close(fd)

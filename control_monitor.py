"""Read-only, one-shot installed control storage observation.

No network/DB connection, task/agent invocation, traversal, cleanup, journal
recovery, cached sample or configuration writes. The observer's mount view is
explicitly distinct from a native controller's pinned private namespace.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import time

MIB=1024**2
CAPABILITY='control-storage-monitor-v1'
COMPONENTS={'jobs':('jobs.sqlite3',),'identities':('identities.sqlite3',),
            'observations':('observations.sqlite3',),
            'journals':tuple(name+suffix for name in ['jobs.sqlite3','identities.sqlite3','observations.sqlite3']
                             for suffix in ['-journal','-wal','-shm']),
            'logs':('server.log','server.log.1','server.log.2','bootstrap.log')}
LEVELS={'ready':0,'warning':1,'critical':2,'unknown':3}


def thresholds(warning_free_mib,critical_free_mib,warning_free_inodes,critical_free_inodes,component_warning_mib):
    values=locals()
    for key,value in values.items():
        maximum=1000000 if 'inodes' in key else 1048576
        if type(value) is not int or not 1<=value<=maximum:raise ValueError('Invalid monitoring threshold')
    if warning_free_mib<critical_free_mib or warning_free_inodes<critical_free_inodes:
        raise ValueError('Warning threshold must not be below critical')
    return {'warning_free_bytes':warning_free_mib*MIB,'critical_free_bytes':critical_free_mib*MIB,
            'warning_free_inodes':warning_free_inodes,'critical_free_inodes':critical_free_inodes,
            'component_warning_bytes':component_warning_mib*MIB}


@contextmanager
def directory(path,uid):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('Invalid control state path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        info=os.fstat(fd)
        if info.st_uid!=uid or stat.S_IMODE(info.st_mode)!=0o700:raise ValueError('Unsafe control state')
        yield fd
    finally:os.close(fd)


def configuration(fd,uid):
    from storage_files import mount
    child=os.open('config.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    with os.fdopen(child,'rb') as file:
        info=os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=uid or stat.S_IMODE(info.st_mode)!=0o600
            or info.st_dev!=os.fstat(fd).st_dev or mount(file.fileno())!=mount(fd)):
            raise ValueError('Unsafe control configuration')
        raw=file.read(2*1024**2+1)
    if len(raw)>2*1024**2:raise ValueError('Oversized control configuration')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Invalid control configuration')
    return value


def footprint(fd):
    from storage_files import mount
    result={}
    for component,names in COMPONENTS.items():
        row={'present_files':0,'apparent_bytes':0,'allocated_bytes':0}
        for name in names:
            try:child=os.open(name,os.O_PATH|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:continue
            try:
                info=os.fstat(child)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_dev!=os.fstat(fd).st_dev
                    or mount(child)!=mount(fd)):
                    raise ValueError('Unsafe fixed control component')
                row['present_files']+=1;row['apparent_bytes']+=info.st_size;row['allocated_bytes']+=info.st_blocks*512
            finally:os.close(child)
        result[component]=row
    return result


def classify(space,components,limits,admission):
    if space.f_frsize<=0 or space.f_bavail<0 or space.f_favail<0 or space.f_files<=0:
        raise ValueError('Unusable control filesystem counters')
    free_bytes=space.f_frsize*space.f_bavail;free_inodes=space.f_favail;readonly=bool(space.f_flag&os.ST_RDONLY)
    critical=[];warning=[]
    if readonly:critical.append('control_readonly')
    if free_bytes<limits['critical_free_bytes']:critical.append('control_free_bytes')
    if free_inodes<limits['critical_free_inodes']:critical.append('control_free_inodes')
    if free_bytes<limits['warning_free_bytes']:warning.append('control_free_bytes')
    if free_inodes<limits['warning_free_inodes']:warning.append('control_free_inodes')
    for name,row in components.items():
        if row['allocated_bytes']>=limits['component_warning_bytes']:warning.append('component_'+name)
    blocked=None if admission is None else bool(readonly or free_bytes<admission['minimum_free_mib']*MIB or free_inodes<admission['minimum_free_inodes'])
    if blocked:critical.append('admission_paused')
    level='critical' if critical else 'warning' if warning else 'ready'
    return {'status':level,'exit_code':LEVELS[level],'reason_codes':sorted(set(critical or warning)),
        'free_bytes':free_bytes,'free_inodes':free_inodes,'readonly':readonly,
        'admission_enabled':admission is not None,'admission_blocks_new_work':blocked}


def inspect(prefix,*,warning_free_mib=128,critical_free_mib=64,warning_free_inodes=512,critical_free_inodes=256,component_warning_mib=32):
    result={'capability':CAPABILITY,'observed_at':time.time(),'status':'unknown','exit_code':3,'check_success':False,
        'reason_codes':['monitor_unavailable'],'observer_view':'caller_mount_namespace',
        'components':None,'free_bytes':None,'free_inodes':None,'readonly':None,
        'admission_enabled':None,'admission_blocks_new_work':None,
        'scope':'Fixed control files only; not a total tree, task/process readiness, storage reservation, payload quota or audit export'}
    try:
        limits=thresholds(warning_free_mib,critical_free_mib,warning_free_inodes,critical_free_inodes,component_warning_mib)
        result['thresholds']=limits
        from deployment import verify_code,DeployError
        selected,meta,release=verify_code(prefix)
        if meta['role']!='controller' or os.geteuid() not in {0,meta['uid']}:raise ValueError('Controller service identity or Root required')
        own=release/Path(__file__).name
        if not own.is_file() or hashlib.sha256(own.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():
            raise ValueError('Use matching installed monitoring code')
        native=meta.get('control_filesystem')
        result['native_filesystem_required']=bool(native)
        if native:
            from control_process import context
            from control_filesystem import Unavailable
            try:context(meta).check()
            except Unavailable:
                result.update(status='critical',exit_code=2,reason_codes=['required_control_filesystem_unavailable'])
                return result
        with directory(meta['state_dir'],meta['uid']) as fd:
            if native and os.fstat(fd).st_dev!=native['device']:raise ValueError('Controller state mount changed')
            config=configuration(fd,meta['uid'])
            if config.get('scheduler',{}).get('control_filesystem')!=native:raise ValueError('Control policy binding changed')
            from control_storage import validate
            admission=validate(config.get('scheduler',{}).get('control_storage'))
            components=footprint(fd);space=os.fstatvfs(fd)
        result.update(classify(space,components,limits,admission),components=components,check_success=True)
        # The policy comes from the matching release's authentication code.
        import auth
        if hashlib.sha256((release/'auth.py').read_bytes()).digest()==hashlib.sha256(Path(auth.__file__).read_bytes()).digest():
            from audit_retention import summary
            result['audit_retention']=summary(config.get('audit_retention'))
        result['admission_scope']='Filesystem headroom only; audit backlog is not queried by this read-only monitor'
        return result
    except (OSError,ValueError,KeyError,TypeError):return result


def prometheus(result):
    lines=[]
    def metric(name,help_text,values):
        name='ascend_lab_control_'+name
        lines.extend(['# HELP '+name+' '+help_text,'# TYPE '+name+' gauge'])
        for suffix,value in values:
            if value is not None:lines.append(name+suffix+' '+str(int(value)))
    metric('check_success','One when the current fixed-file and filesystem observation succeeded.',[('',result['check_success'])])
    metric('observation_timestamp_seconds','Time of this one-shot observation.',[('',result['observed_at'])])
    metric('alert','Current alert level; this is not task or service readiness.',[('{severity="'+name+'"}',result['status']==name) for name in LEVELS])
    for field in ['free_bytes','free_inodes','readonly','admission_enabled','admission_blocks_new_work']:
        metric(field,'Current '+field.replace('_',' ')+'; unknown values have no sample.',[('',result.get(field))])
    for field in ['apparent_bytes','allocated_bytes','present_files']:
        metric('component_'+field,'Fixed registered control files only; not total state usage.',
            [('{component="'+name+'"}',row[field]) for name,row in (result.get('components') or {}).items()])
    return '\n'.join(lines)+'\n'

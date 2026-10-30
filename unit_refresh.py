"""Root-only offline CAS refresh of an explicitly selected managed unit file.

Never talks to a systemd manager or restarts a service. Original bytes, file
identities and installation/config bindings are durable before replacement.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

import deployment as deploy
from deployment import DeployError
from payload_quota_admin import fingerprint,sync,write
from state_lock import state_lock

FORMAT='ascend-lab-unit-refresh-plan/v1'
JOURNAL='unit-refresh-change.json'
HISTORY='unit-refresh-history'
MAX_BYTES=65536


def _checkpoint(stage):pass


def _sha(data):return hashlib.sha256(data).hexdigest()


def _directory(path,private=False):
    path=Path(path)
    if not path.is_absolute() or str(path)!=os.path.normpath(path) or not re.fullmatch('/[A-Za-z0-9_./-]+',str(path)) or '..' in path.parts:
        raise DeployError('Unit paths must be canonical absolute portable paths')
    for component in reversed([path,*path.parents]):
        info=component.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise DeployError('Unit ancestry must be protected Root-owned directories without links')
    info=path.stat()
    if private and info.st_mode&0o077:raise DeployError('Unit history requires a Root-private directory')
    return {'device':info.st_dev,'inode':info.st_ino}


def _file(path,mode=None):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_nlink!=1
            or info.st_size>MAX_BYTES or mode is not None and stat.S_IMODE(info.st_mode)!=mode):raise DeployError('Unit files must be bounded, Root-owned regular files without aliases or unsafe permissions')
        raw=file.read(MAX_BYTES+1);after=os.fstat(file.fileno())
        if len(raw)!=info.st_size or (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise DeployError('Unit file changed while reading')
    try:text=raw.decode('utf-8')
    except UnicodeError:raise DeployError('Managed unit must be UTF-8 text') from None
    return {'device':info.st_dev,'inode':info.st_ino,'size':info.st_size,'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns,
            'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid,'sha256':_sha(raw),'text':text}


def _publish(path,text,mode):
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as file:
            file.write(text.encode());file.flush();os.fchmod(file.fileno(),mode);os.fchown(file.fileno(),0,0);os.fsync(file.fileno())
        os.replace(temporary,path);sync(path.parent)
    finally:temporary.unlink(missing_ok=True)


def _overrides(directory,name):
    stem=name.removesuffix('.service');dropins=[name+'.d','service.d']
    dropins.extend(stem[:match.start()+1]+'.service.d' for match in re.finditer('-',stem))
    roots={Path(directory)}
    if str(directory)=='/etc/systemd/system':
        roots.update(Path(p) for p in ['/etc/systemd/system.control','/run/systemd/system','/run/systemd/system.control',
            '/usr/local/lib/systemd/system','/usr/lib/systemd/system','/lib/systemd/system'])
    for root in roots:
        for name in dropins:
            candidate=root/name
            if candidate.exists() or candidate.is_symlink():raise DeployError('Reconcile systemd unit, prefix or type-wide drop-ins before automatic refresh')


def _managed(text,prefix,meta,desired):
    if text==desired:return
    # Initial versions kept an old group list after appending owner storage.
    # Accept only this one historical variation; every other byte must match
    # the current generated unit, including all security settings and ExecStart.
    from payload_groups import gids
    allowed=set(gids(meta.get('payload_storage'))) if meta['role']=='controller' else set()
    matches=re.findall(r'^SupplementaryGroups=([^\n]*)\n',text,re.M)
    if len(matches)>1:raise DeployError('Unit is customized; automatic refresh refused')
    groups=[]
    if matches:
        fields=matches[0].split()
        if any(not re.fullmatch('[0-9]+',x) for x in fields):raise DeployError('Unit has a customized group list')
        groups=list(map(int,fields))
        if sorted(set(groups))!=groups or not set(groups)<=allowed:raise DeployError('Unit storage groups differ from the managed assignment')
    normalized=re.sub(r'^SupplementaryGroups=[^\n]*\n','',text,flags=re.M)
    expected=re.sub(r'^SupplementaryGroups=[^\n]*\n','',desired,flags=re.M)
    if normalized!=expected:raise DeployError('Unit is customized or belongs to another installation; automatic refresh refused')


def _context(prefix,recovery=False):
    deploy.require_root();prefix,meta,release=deploy.verify(prefix,unit_recovery=recovery)
    helper=release/Path(__file__).name
    if not helper.is_file() or _sha(helper.read_bytes())!=_sha(Path(__file__).read_bytes()):
        raise DeployError('Activate matching unit refresh and recovery code before updating a unit')
    _directory(prefix);_directory(prefix/'units');state=Path(meta['state_dir']);info=state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=meta['uid'] or info.st_mode&0o077:raise DeployError('Installation control directory identity or permissions differ')
    return prefix,meta,state,{'device':info.st_dev,'inode':info.st_ino}


def _binding(prefix,meta,state,control):
    config=deploy.regular_json(state/'config.json',private=True)
    return {'prefix':str(prefix),'state_dir':str(state),'control_identity':control,'role':meta['role'],'release_id':meta['release_id'],
        'installation_sha256':fingerprint(meta),'configuration_sha256':fingerprint(config)}


def _plan_check(value):
    fields={'format','id','binding','unit_dir','unit_directory_identity','template_directory_identity','unit_name','installed','template','desired'}
    if (not isinstance(value,dict) or set(value)!=fields or value['format']!=FORMAT
        or not isinstance(value['id'],str) or re.fullmatch('[0-9a-f]{64}',value['id']) is None
        or fingerprint({k:v for k,v in value.items() if k!='id'})!=value['id']):raise DeployError('Invalid unit refresh plan/digest')
    binding=value['binding']
    if (not isinstance(binding,dict) or set(binding)!={'prefix','state_dir','control_identity','role','release_id','installation_sha256','configuration_sha256'}
        or binding['role'] not in {'controller','node'}):raise DeployError('Invalid unit installation binding')
    if value['unit_name']!='ascend-lab-'+binding['role']+'.service':
        raise DeployError('Unit role/name differs from selected installation')
    for path in [binding['prefix'],binding['state_dir'],value['unit_dir']]:
        if (not isinstance(path,str) or not path.startswith('/') or path!=str(Path(path)) or '..' in Path(path).parts
            or not re.fullmatch('/[A-Za-z0-9_./-]+',path)):raise DeployError('Invalid unit plan path')
    for key in ['release_id','installation_sha256','configuration_sha256']:
        if not isinstance(binding[key],str) or re.fullmatch('[0-9a-f]{64}',binding[key]) is None:raise DeployError('Invalid unit installation digest')
    for identity in [binding['control_identity'],value['unit_directory_identity'],value['template_directory_identity']]:
        if not isinstance(identity,dict) or set(identity)!={'device','inode'} or any(type(v) is not int or v<=0 for v in identity.values()):
            raise DeployError('Invalid unit directory identity')
    for item in [value['installed'],value['template']]:
        if (not isinstance(item,dict) or set(item)!={'device','inode','size','mtime_ns','ctime_ns','mode','uid','gid','sha256','text'}
            or any(type(item[k]) is not int or item[k]<0 for k in ['device','inode','size','mtime_ns','ctime_ns','mode','uid','gid'])
            or not isinstance(item['text'],str) or len(item['text'].encode())>MAX_BYTES
            or item['sha256']!=_sha(item['text'].encode()) or item['size']!=len(item['text'].encode()) or item['uid']!=0):
            raise DeployError('Invalid original unit snapshot')
    if value['installed']['mode']!=0o644 or value['template']['mode']!=0o444:raise DeployError('Invalid original unit modes')
    if not isinstance(value['desired'],str) or len(value['desired'].encode())>MAX_BYTES:raise DeployError('Unit plan exceeds its text budget')


def plan(prefix,unit_dir='/etc/systemd/system'):
    prefix,meta,state,control=_context(prefix)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,meta['role'])
        directory=Path(unit_dir);directory_id=_directory(directory);name='ascend-lab-'+meta['role']+'.service'
        _overrides(directory,name);desired=deploy.unit_text(prefix,meta)
        installed=_file(directory/name,0o644);template=_file(prefix/'units'/name,0o444)
        _managed(installed['text'],prefix,meta,desired);_managed(template['text'],prefix,meta,desired)
        value={'format':FORMAT,'binding':_binding(prefix,meta,state,control),'unit_dir':str(directory),'unit_directory_identity':directory_id,
               'unit_name':name,'template_directory_identity':_directory(prefix/'units'),'installed':installed,'template':template,'desired':desired}
        value['id']=fingerprint(value)
        return {'read_only':True,'plan':value,'unit_changed':installed['text']!=desired,'systemd_manager_contacted':False}


def _original_or_desired(path,snapshot,desired,mode):
    actual=_file(path,mode)
    if actual['text']==desired:return actual
    if actual!=snapshot:raise DeployError('Unit changed since plan; preserve the original plan and reconcile the file')
    return actual


def apply(prefix,value):
    _plan_check(value);prefix,meta,state,control=_context(prefix,recovery=True)
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,meta['role'])
        if value['binding']!=_binding(prefix,meta,state,control) or value['desired']!=deploy.unit_text(prefix,meta):
            raise DeployError('Unit plan installation/configuration binding is stale')
        directory=Path(value['unit_dir']);name=value['unit_name'];desired=value['desired']
        if _directory(directory)!=value['unit_directory_identity']:raise DeployError('Unit directory identity changed')
        if _directory(prefix/'units')!=value['template_directory_identity']:raise DeployError('Managed template directory identity changed')
        _overrides(directory,name)
        journal=prefix/JOURNAL;history=prefix/HISTORY;leaf=history/value['id'];receipt=leaf/'receipt.json'
        if history.exists() or history.is_symlink():_directory(history,private=True)
        if leaf.exists() or leaf.is_symlink():_directory(leaf,private=True)
        installed=directory/name;template=prefix/'units'/name
        for path,snapshot,mode in [(installed,value['installed'],0o644),(template,value['template'],0o444)]:
            _original_or_desired(path,snapshot,desired,mode)
        pending=None
        if journal.exists() or journal.is_symlink():
            if journal.lstat().st_uid!=0:raise DeployError('Unit refresh journal must be Root-owned')
            pending=deploy.regular_json(journal,max_bytes=512*1024,private=True)
            if pending!=value:raise DeployError('Resume only the original pending unit refresh plan')
        expected={'format':'ascend-lab-unit-refresh-receipt/v1','plan_id':value['id'],'unit':str(installed),
            'desired_sha256':_sha(desired.encode()),'unit_changed':value['installed']['text']!=desired,
            'systemd_manager_contacted':False,'daemon_reload_required':True,'service_restart_required':True}
        completed=receipt.exists() or receipt.is_symlink()
        if completed:
            result=deploy.regular_json(receipt,private=True)
            if result!=expected:raise DeployError('Unit refresh receipt differs')
            if _file(installed,0o644)['text']!=desired or _file(template,0o444)['text']!=desired:raise DeployError('Completed unit was modified after refresh')
        else:
            if pending is None:
                if _file(installed,0o644)!=value['installed'] or _file(template,0o444)!=value['template']:
                    raise DeployError('Unit file identity changed before journal publication')
                if not history.exists():history.mkdir(mode=0o700);sync(prefix)
                if len(os.listdir(history))>=1024:raise DeployError('Archive completed unit refresh records before further changes')
                write(journal,value);_checkpoint('journal')
            if not leaf.exists():leaf.mkdir(mode=0o700);sync(history)
            for path,snapshot in [(leaf/'installed-before.service',value['installed']),(leaf/'template-before.service',value['template'])]:
                if path.exists() or path.is_symlink():
                    if _file(path,0o600)['text']!=snapshot['text']:raise DeployError('Original unit backup differs')
                else:_publish(path,snapshot['text'],0o600)
            _checkpoint('backup')
            if _file(installed,0o644)['text']!=desired:_publish(installed,desired,0o644)
            _checkpoint('installed')
            if _file(template,0o444)['text']!=desired:_publish(template,desired,0o444)
            _checkpoint('template')
            result=expected
            write(receipt,result);_checkpoint('receipt')
        # Verify backups even on receipt replay; never manufacture missing
        # preimages from a unit already changed by a previous attempt.
        for path,snapshot in [(leaf/'installed-before.service',value['installed']),(leaf/'template-before.service',value['template'])]:
            if _file(path,0o600)['text']!=snapshot['text']:raise DeployError('Original unit backup is missing or changed')
        if journal.exists():journal.unlink();sync(prefix)
        _checkpoint('published')
        return {**result,'already_applied':completed,'receipt':str(receipt)}

"""Root-only delivery of an existing activated credential to one private UID.

Plans/journals contain identities and hashes only. No overwrite, credential
issuance, account enable, shell, model configuration or remote home access.
The administrator supplies a private Root spool on the destination filesystem.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import time
import uuid

import deployment as deploy
from deployment import DeployError
from payload_quota_admin import fingerprint,sync,write
from state_lock import state_lock
from storage_files import identity as fd_identity
import unit_refresh as protected

FORMAT='ascend-lab-owner-delivery-plan/v1'
JOURNAL='owner-delivery-change.json'
HISTORY='owner-delivery-history'
MAX_FILE=256*1024


def _checkpoint(stage):pass


def request(value):
    from auth import IDENTIFIER
    if (not isinstance(value,dict) or set(value)!={'owner','parent','name','uid','gid','spool'}
        or not isinstance(value['owner'],str) or not IDENTIFIER.fullmatch(value['owner'])
        or not isinstance(value['name'],str) or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}',value['name'])
        or any(type(value[k]) is not int or not 1<=value[k]<2**31 for k in ['uid','gid'])):
        raise DeployError('Delivery requires owner, explicit UID/GID, existing parent, new directory name and private Root spool')
    for key in ['parent','spool']:
        path=value[key]
        if not isinstance(path,str) or not re.fullmatch('/[A-Za-z0-9_./-]+',path) or path!=os.path.normpath(path) or '..' in Path(path).parts:
            raise DeployError('Delivery paths must be canonical absolute portable paths')
    return dict(value)


def _walk(path,owners):
    """Pin every component; caller cannot redirect the walk with symlinks."""
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);chain=[]
    try:
        for name in ['.',*Path(path).parts[1:]]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
            info=os.fstat(fd)
            if info.st_uid not in owners or info.st_mode&0o022:raise DeployError('Delivery ancestry must have trusted owners and no group/world write permissions')
            chain.append({**fd_identity(fd),'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode)})
        return fd,chain
    except BaseException:os.close(fd);raise


def _directories(spec):
    parent,chain=_walk(spec['parent'],{0,spec['uid']})
    try:
        info=os.fstat(parent)
        if info.st_uid!=spec['uid'] or info.st_gid!=spec['gid']:raise DeployError('Delivery parent must belong to the explicitly selected personal UID/GID')
        spool,spool_chain=_walk(spec['spool'],{0})
        try:
            if stat.S_IMODE(os.fstat(spool).st_mode)!=0o700:raise DeployError('Delivery spool must be Root-owned mode 0700')
            if (fd_identity(parent)['dev'],fd_identity(parent)['mount'])!=(fd_identity(spool)['dev'],fd_identity(spool)['mount']):
                raise DeployError('Delivery spool and personal parent must share one filesystem/mount for non-overwriting atomic publication')
            return parent,spool,{'parent':chain,'spool':spool_chain}
        except BaseException:os.close(spool);raise
    except BaseException:os.close(parent);raise


def _exists(fd,name):
    try:return os.stat(name,dir_fd=fd,follow_symlinks=False)
    except FileNotFoundError:return None


def _file(parent,name,uid,allow_root=False):
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid not in ({0,uid} if allow_root else {uid})
            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>MAX_FILE):raise DeployError('Delivery files must be bounded private regular files with exact ownership and no aliases')
        raw=file.read(MAX_FILE+1);after=os.fstat(file.fileno())
        if len(raw)!=info.st_size or (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise DeployError('Delivery source changed while reading')
    return raw,{'device':info.st_dev,'inode':info.st_ino,'size':len(raw),'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns,
                'uid':info.st_uid,'gid':info.st_gid,'mode':0o600,'sha256':hashlib.sha256(raw).hexdigest()}


def _context(prefix,recovery=False,finish=False):
    deploy.require_root();prefix,meta,release=deploy.verify(prefix,delivery_recovery=recovery,finish_recovery=finish)
    if meta['role']!='controller':raise DeployError('Credential delivery requires the selected controller installation')
    protected._directory(prefix)
    if hashlib.sha256((release/Path(__file__).name).read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():
        raise DeployError('Use matching installed credential delivery code')
    state=Path(meta['state_dir']);info=state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=meta['uid'] or info.st_mode&0o077:raise DeployError('Unsafe controller state directory')
    return prefix,meta,state,{'device':info.st_dev,'inode':info.st_ino}


@contextmanager
def material(state,meta,owner):
    """Reserve the identity writer until ownership handoff, without changing DB."""
    db=sqlite3.connect(state/'identities.sqlite3',timeout=5)
    try:
        db.execute('BEGIN IMMEDIATE')
        for folder in ['owner-activations','owner-activation-aborts']:
            if (state/folder/(owner+'.pending.json')).exists():raise DeployError('Finish this owner activation/abort before credential delivery')
        row=db.execute('SELECT disabled,password_hash,salt,profile FROM users WHERE id=?',(owner,)).fetchone()
        activated=db.execute('SELECT id,record FROM owner_activations WHERE owner=?',(owner,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='owner_activations'").fetchone() else None
        from owner_identity import login_allowed
        if not row or row[0] or not activated or not login_allowed(json.loads(row[3])):raise DeployError('Delivery requires a currently enabled, committed activated owner')
        profile=json.loads(row[3]);record=json.loads(activated[1]);marker=profile.get('provisioned',{})
        if marker.get('id')!=activated[0] or record.get('id')!=activated[0] or record.get('owner')!=owner:raise DeployError('Committed activation identity differs')
        receipt=deploy.regular_json(state/'owner-activations'/(owner+'.json'),private=True)
        if receipt!=record or record.get('credentials')!=str(state/'personas'/owner/'client.json'):raise DeployError('Activation delivery receipt differs from its committed record')
        source,chain=_walk(str(state/'personas'/owner),{0,meta['uid']})
        try:
            data,snapshot=_file(source,'client.json',meta['uid']);client=json.loads(data)
            if (not isinstance(client,dict) or set(client) not in [{'username','password','token','controller'},{'username','password','token','controller','ca_file'}]
                or client.get('username')!=owner or any(not isinstance(client.get(k),str) or not 1<=len(client[k])<=1024 for k in ['password','token','controller'])):
                raise DeployError('Activated client has an unexpected credential/transport schema')
            from lab_client import controller_url
            config=deploy.regular_json(state/'config.json',private=True)
            if controller_url(client)!=controller_url(config):raise DeployError('Activated client points to another controller')
            digest=hashlib.sha256(client['token'].encode()).hexdigest()
            token=db.execute('SELECT user,scopes,revoked,expires_at FROM tokens WHERE digest=?',(digest,)).fetchone()
            if (not token or token[0]!=owner or token[2] or not isinstance(token[3],(int,float)) or not math.isfinite(token[3]) or token[3]<=time.time()
                or sorted(json.loads(token[1]))!=['jobs:cancel','jobs:read','jobs:submit']):raise DeployError('Original activated credential was revoked, expired or changed')
            password=hashlib.pbkdf2_hmac('sha256',client['password'].encode(),bytes.fromhex(row[2]),300000).hex()
            if not hmac.compare_digest(password,row[1]):raise DeployError('Original activated password changed')
            content={'client.json':data};snapshots={'client.json':snapshot}
            if client.get('ca_file') is not None:
                if client['ca_file']!='controller-ca.pem':raise DeployError('Only the activation relative CA file may be delivered')
                data,snapshot=_file(source,'controller-ca.pem',meta['uid']);ssl.create_default_context(cadata=data.decode('ascii'))
                content['controller-ca.pem']=data;snapshots['controller-ca.pem']=snapshot
            binding={'activation_id':activated[0],'stage_id':marker.get('stage_id'),'activation_sha256':fingerprint(record),'valid_until_epoch':token[3],
                'authentication_sha256':fingerprint({'user':row,'token':token}),'source_chain':chain,'files':snapshots}
            yield binding,content
        finally:os.close(source)
    finally:db.rollback();db.close()


def plan(prefix,spec,finish=False):
    spec=request(spec);prefix,meta,state,control=_context(prefix,finish=finish)
    if spec['uid']==meta['uid'] or spec['gid']==meta['gid']:raise DeployError('Personal delivery cannot use the controller service UID/GID')
    with state_lock(state):
        parent,spool,directories=_directories(spec)
        try:
            if _exists(parent,spec['name']):raise DeployError('Choose a new personal profile directory; existing data is never overwritten')
            with material(state,meta,spec['owner']) as (source,content):
                value={'format':FORMAT,'request':spec,'binding':{'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],
                    'installation_sha256':fingerprint(meta),'configuration_sha256':fingerprint(deploy.regular_json(state/'config.json',private=True)),
                    'control_identity':control},'directories':directories,'source':source,'nonce':uuid.uuid4().hex}
                value['id']=fingerprint(value);_plan_check(value)
                return {'read_only':True,'plan':value,'credentials_issued':False}
        finally:os.close(parent);os.close(spool)


def _plan_check(value):
    try:
        if (not isinstance(value,dict) or set(value)!={'format','request','binding','directories','source','nonce','id'} or value['format']!=FORMAT
            or value['id']!=fingerprint({k:v for k,v in value.items() if k!='id'}) or not re.fullmatch('[0-9a-f]{32}',value['nonce'])
            or not re.fullmatch('[0-9a-f]{64}',value['id']) or len(json.dumps(value).encode())>65536):raise ValueError()
        request(value['request'])
        if set(value['binding'])!={'prefix','state_dir','release_id','installation_sha256','configuration_sha256','control_identity'}:raise ValueError()
        if set(value['directories'])!={'parent','spool'} or not all(isinstance(v,list) and v for v in value['directories'].values()):raise ValueError()
        if (set(value['source'])!={'activation_id','stage_id','activation_sha256','authentication_sha256','valid_until_epoch','source_chain','files'}
            or set(value['source']['files']) not in [{'client.json'},{'client.json','controller-ca.pem'}]):raise ValueError()
        if type(value['source']['valid_until_epoch']) not in {int,float} or not math.isfinite(value['source']['valid_until_epoch']):raise ValueError()
        for name,record in value['source']['files'].items():
            if set(record)!={'device','inode','size','mtime_ns','ctime_ns','uid','gid','mode','sha256'} or not re.fullmatch('[0-9a-f]{64}',record['sha256']):raise ValueError()
    except (ValueError,KeyError,TypeError):raise DeployError('Invalid private credential delivery plan') from None


def _rename(parent,source,destination,name):
    libc=ctypes.CDLL(None,use_errno=True)
    try:rename=libc.renameat2
    except AttributeError:raise DeployError('Credential delivery requires Linux renameat2 NOREPLACE') from None
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];rename.restype=ctypes.c_int
    if rename(parent,os.fsencode(source),destination,os.fsencode(name),1):raise OSError(ctypes.get_errno(),'Non-overwriting credential publication failed')


def _root_record(path):
    if path.lstat().st_uid!=0:raise DeployError('Delivery recovery record must be Root-owned')
    return deploy.regular_json(path,max_bytes=128*1024,private=True)


def _receipt(value,result):
    if (not isinstance(result,dict) or set(result)!={'format','plan_id','owner','activation_id','state','uid','gid','directory','files_sha256','destination_artifacts_retained'}
        or result['format']!='ascend-lab-owner-delivery-receipt/v1' or result['plan_id']!=value['id']
        or result['owner']!=value['request']['owner'] or result['activation_id']!=value['source']['activation_id']
        or result['state'] not in {'delivered','aborted'} or result['uid']!=value['request']['uid'] or result['gid']!=value['request']['gid']
        or result['files_sha256']!={n:r['sha256'] for n,r in value['source']['files'].items()}
        or type(result['destination_artifacts_retained']) is not bool):raise DeployError('Delivery receipt differs')


def apply(prefix,value,abort=False,finish=False):
    deploy.require_root();_plan_check(value);prefix,meta,state,control=_context(prefix,recovery=True,finish=finish)
    if value['binding']['prefix']!=str(prefix):raise DeployError('Credential delivery plan belongs to another installation')
    if value['request']['uid']==meta['uid'] or value['request']['gid']==meta['gid']:raise DeployError('Personal delivery cannot use the service identity')
    with state_lock(prefix,exclusive=True),state_lock(state):
        journal=prefix/JOURNAL;history=prefix/HISTORY;receipt=history/(value['id']+'.json')
        pending=_root_record(journal) if journal.exists() or journal.is_symlink() else None
        if pending is not None and (not isinstance(pending,dict) or set(pending)!={'plan','phase','directory'} or pending['plan']!=value
            or pending['phase'] not in {'preparing','handoff'}):raise DeployError('Resume only the original credential delivery')
        if history.exists() or history.is_symlink():protected._directory(history,private=True)
        finished=_root_record(receipt) if receipt.exists() or receipt.is_symlink() else None
        if finished is not None:
            _receipt(value,finished)
            if pending is not None:journal.unlink();sync(prefix);_checkpoint('published')
            return {**finished,'historical_receipt_only':True}
        if abort:
            if pending is None:raise DeployError('No pending credential delivery to abort')
            result={'format':'ascend-lab-owner-delivery-receipt/v1','plan_id':value['id'],'owner':value['request']['owner'],
                'activation_id':value['source']['activation_id'],'state':'aborted','uid':value['request']['uid'],'gid':value['request']['gid'],
                'directory':pending['directory'],'files_sha256':{n:r['sha256'] for n,r in value['source']['files'].items()},'destination_artifacts_retained':True}
            write(receipt,result);journal.unlink();sync(prefix);return result
        binding=value['binding']
        if binding!={'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],'installation_sha256':fingerprint(meta),
            'configuration_sha256':fingerprint(deploy.regular_json(state/'config.json',private=True)),'control_identity':control}:
            raise DeployError('Credential delivery installation or configuration changed')
        spec=value['request'];parent,spool,directories=_directories(spec);stage='.delivery-'+value['nonce']
        try:
            if directories!=value['directories']:raise DeployError('Personal parent or Root spool changed since planning')
            if pending is None:
                if _exists(parent,spec['name']) or _exists(spool,stage):raise DeployError('Credential destination or staging name already exists')
                with material(state,meta,spec['owner']) as (source,content):
                    if source!=value['source']:raise DeployError('Activated credential changed before delivery began')
                if not history.exists():history.mkdir(mode=0o700);sync(prefix)
                if len(list(history.glob('*.json')))>=1024:raise DeployError('Archive delivery receipts before more operations')
                pending={'plan':value,'phase':'preparing','directory':None};write(journal,pending);_checkpoint('journal')
            target=_exists(parent,spec['name']);staged=_exists(spool,stage)
            if target is not None and staged is not None:raise DeployError('Both staged and final credential directories exist; reconcile without overwriting')
            if pending['phase']=='handoff' and target is not None and target.st_uid==spec['uid']:
                # Ownership is the irreversible handoff. Personal edits/revocation
                # after that point must never cause old credentials to be recopied.
                directory=os.open(spec['name'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    if fd_identity(directory)!=pending['directory'] or stat.S_IMODE(os.fstat(directory).st_mode)!=0o700:raise DeployError('Handed-off credential directory changed identity/permissions')
                    os.fsync(directory);os.fsync(parent)
                finally:os.close(directory)
            else:
                with material(state,meta,spec['owner']) as (source,content):
                    if source!=value['source']:raise DeployError('Activated credential or source identity changed; no profile was overwritten')
                    if target is None:
                        if staged is None:os.mkdir(stage,mode=0o700,dir_fd=spool);os.fsync(spool)
                        directory=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=spool)
                    else:directory=os.open(spec['name'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                    try:
                        info=os.fstat(directory)
                        if info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700:raise DeployError('Unpublished credential directory must remain Root-private')
                        if pending['directory'] is not None and fd_identity(directory)!=pending['directory']:raise DeployError('Credential directory identity changed')
                        if target is not None and pending['directory'] is None:raise DeployError('Unknown final destination cannot be adopted')
                        pending={**pending,'directory':fd_identity(directory)};write(journal,pending);_checkpoint('directory')
                        marker=(json.dumps({'plan_id':value['id'],'owner':spec['owner'],'activation_id':source['activation_id']},sort_keys=True)+'\n').encode()
                        allowed={**content,'delivery.json':marker}
                        if set(os.listdir(directory))-set(allowed):raise DeployError('Unknown files in credential staging directory')
                        for name,data in allowed.items():
                            if _exists(directory,name):
                                raw,snapshot=_file(directory,name,spec['uid'],allow_root=True)
                                if raw!=data:
                                    if snapshot['uid']!=0 or not data.startswith(raw):raise DeployError('Original delivery file changed; no credentials overwritten')
                                    fd=os.open(name,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                                    with os.fdopen(fd,'ab') as file:
                                        current=os.fstat(file.fileno())
                                        if (current.st_dev,current.st_ino,current.st_uid,current.st_nlink,current.st_size)!=(snapshot['device'],snapshot['inode'],0,1,len(raw)):
                                            raise DeployError('Partial delivery file identity changed')
                                        file.write(data[len(raw):]);file.flush();os.fsync(file.fileno())
                            else:
                                fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                                with os.fdopen(fd,'wb') as file:file.write(data);file.flush();os.fsync(file.fileno())
                        os.fsync(directory);_checkpoint('files')
                        if target is None:
                            _rename(spool,stage,parent,spec['name']);os.fsync(spool);os.fsync(parent)
                        _checkpoint('renamed')
                        pending={**pending,'phase':'handoff'};write(journal,pending);_checkpoint('handoff')
                        if source['valid_until_epoch']<=time.time():raise DeployError('Credential expired before ownership handoff; resume or explicitly abort without issuing another credential')
                        for name in allowed:
                            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
                            try:os.fchown(fd,spec['uid'],spec['gid']);os.fsync(fd)
                            finally:os.close(fd)
                        os.fsync(directory)
                        if source['valid_until_epoch']<=time.time():raise DeployError('Credential expired before final ownership handoff')
                        os.fchown(directory,spec['uid'],spec['gid']);os.fsync(directory);os.fsync(parent)
                        _checkpoint('ownership')
                    finally:os.close(directory)
            result={'format':'ascend-lab-owner-delivery-receipt/v1','plan_id':value['id'],'owner':spec['owner'],'activation_id':value['source']['activation_id'],
                'state':'delivered','uid':spec['uid'],'gid':spec['gid'],'directory':pending['directory'],
                'files_sha256':{n:r['sha256'] for n,r in value['source']['files'].items()},'destination_artifacts_retained':False}
            write(receipt,result);_checkpoint('receipt');journal.unlink();sync(prefix);_checkpoint('published')
            return {**result,'historical_receipt_only':False}
        finally:os.close(parent);os.close(spool)

"""Explicit local abort of an uncommitted activation; never revokes active users.

Plans/journals/receipts contain fingerprints, not plaintext credentials. The
original intent stays private until cleanup and the durable spent-ID record.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import owner_identity
from jobs import JobError,identity_alive,read_json,now
from owner_activation import Activation,FORMAT as ACTIVATION,_read,_save,_sync
from storage_files import private_tree,lock

CAPABILITY='owner-activation-abort-v1'
DIRECTORY='owner-activation-aborts'
FORMAT='ascend-lab-owner-activation-abort-plan/v1'


def _checkpoint(stage):pass


def _sha(value):return owner_identity.digest(value)


def _ids(owner,identifier):
    from auth import IDENTIFIER
    if (not isinstance(owner,str) or not IDENTIFIER.fullmatch(owner) or not isinstance(identifier,str)
        or not re.fullmatch('[0-9a-f]{32}',identifier)):raise JobError('Choose the exact owner and pending activation ID',400)


def guard(runtime,owner):
    directory=Path(runtime)/DIRECTORY
    if directory.exists() or directory.is_symlink():_directory(directory)
    path=Path(runtime)/DIRECTORY/(owner+'.pending.json')
    if path.exists() or path.is_symlink():raise JobError('Resume the original activation abort before activating this owner',409)


def spent(db,identifier):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_activation_aborts'").fetchone()
        and db.execute('SELECT 1 FROM owner_activation_aborts WHERE id=?',(identifier,)).fetchone())


def _snapshot(path):
    if not path.exists() and not path.is_symlink():return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077
            or info.st_nlink!=1 or info.st_size>512*1024):raise JobError('Abort refuses non-private, linked, foreign or oversized files',409)
        data=file.read(512*1024+1);after=os.fstat(file.fileno())
    if (len(data)!=info.st_size or (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
        raise JobError('Activation file changed during abort planning',409)
    return {'device':info.st_dev,'inode':info.st_ino,'size':info.st_size,'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns,
            'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode),'sha256':hashlib.sha256(data).hexdigest()}


def _directory(path):
    item=path.lstat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid!=os.geteuid() or item.st_mode&0o077:
        raise JobError('Abort requires service-owned private control directories',409)
    return {'device':item.st_dev,'inode':item.st_ino,'uid':item.st_uid}


def _plan_check(value):
    fields={'format','id','owner','activation_id','stage_id','runtime','directories','files','preserve_trust'}
    if not isinstance(value,dict) or set(value)!=fields or value['format']!=FORMAT:raise JobError('Invalid activation abort plan',400)
    _ids(value['owner'],value['activation_id'])
    if (not isinstance(value['id'],str) or not re.fullmatch('[0-9a-f]{64}',value['id'])
        or value['id']!=_sha({k:v for k,v in value.items() if k!='id'})
        or not isinstance(value['stage_id'],str) or not re.fullmatch('[0-9a-f]{64}',value['stage_id'])
        or type(value['preserve_trust']) is not bool or not isinstance(value['runtime'],str)):
        raise JobError('Invalid activation abort binding/digest',400)
    directories=value['directories'];files=value['files']
    if (not isinstance(directories,dict) or set(directories)!={'runtime','activations','personal','aborts'}
        or not isinstance(files,dict) or set(files)!={'intent','client','trust'} or files['intent'] is None):
        raise JobError('Invalid activation abort file contract',400)
    for item in directories.values():
        if not isinstance(item,dict) or set(item)!={'device','inode','uid'} or any(type(v) is not int or v<0 for v in item.values()):
            raise JobError('Invalid activation abort directory identity',400)
    for item in files.values():
        if item is None:continue
        if (not isinstance(item,dict) or set(item)!={'device','inode','size','mtime_ns','ctime_ns','uid','gid','mode','sha256'}
            or any(type(item[k]) is not int or item[k]<0 for k in item if k!='sha256')
            or not isinstance(item['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',item['sha256'])):
            raise JobError('Invalid activation abort file identity',400)


class Abort:
    def __init__(self,jobs):
        self.jobs=jobs;self.runtime=jobs.runtime;self.access=jobs.access
        Activation(jobs)
        with self.access.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS owner_activation_aborts(id TEXT PRIMARY KEY,owner TEXT NOT NULL,record TEXT NOT NULL)')

    def _paths(self,owner):
        private_tree(self.runtime,['owner-activations']);private_tree(self.runtime,['personas',owner]);private_tree(self.runtime,[DIRECTORY])
        activations=self.runtime/'owner-activations';personal=self.runtime/'personas'/owner;aborts=self.runtime/DIRECTORY
        directories={'runtime':_directory(self.runtime),'activations':_directory(activations),'personal':_directory(personal),'aborts':_directory(aborts)}
        return {'intent':activations/(owner+'.pending.json'),'client':personal/'client.json','trust':personal/'controller-ca.pem',
                'activation_receipt':activations/(owner+'.json'),'journal':aborts/(owner+'.pending.json'),'history':aborts},directories

    def _stage(self,db,owner,stage_id):
        user=db.execute('SELECT disabled,password_hash,profile FROM users WHERE id=?',(owner,)).fetchone()
        if not user:raise JobError('Unknown staged activation owner',409)
        profile=json.loads(user[2])
        if (user[:2]!=(1,'!') or not owner_identity.staged(owner,profile) or profile['provisioning']['id']!=stage_id
            or db.execute('SELECT 1 FROM tokens WHERE user=?',(owner,)).fetchone()
            or db.execute('SELECT 1 FROM owner_activations WHERE owner=?',(owner,)).fetchone()):
            raise JobError('Abort requires the original disabled, uncommitted identity with no issued credentials',409)

    def _intent(self,paths,owner,identifier):
        value=_read(paths['intent'])
        if (not isinstance(value,dict) or value.get('format')!=ACTIVATION or value.get('id')!=identifier
            or not isinstance(value.get('request'),dict) or value['request'].get('owner')!=owner
            or not isinstance(value.get('client'),dict) or value['client'].get('username')!=owner
            or not isinstance(value.get('marker'),dict) or value['marker'].get('id')!=identifier
            or value['marker'].get('stage_id')!=value['request'].get('stage_id')):
            raise JobError('No matching original pending activation intent',409)
        from owner_activation import request
        normalized=request(value['request'])
        if value['marker'].get('format')!=ACTIVATION or value['marker'].get('required_nodes')!=normalized['required_nodes']:
            raise JobError('Pending activation marker differs from its original request',409)
        return value

    def _live(self):
        ready=read_json(self.runtime/'server-ready.json') or {}
        if identity_alive(ready) and CAPABILITY not in ready.get('capabilities',[]):
            raise JobError('Reload the controller with activation abort recovery support first',409)

    def status(self,owner):
        from auth import IDENTIFIER
        if not isinstance(owner,str) or not IDENTIFIER.fullmatch(owner):raise JobError('Invalid owner',400)
        registered=self.access.registration(owner);paths,_=self._paths(owner)
        pending=_read(paths['intent']);journal=_read(paths['journal'])
        with self.access.connect() as db:row=db.execute('SELECT id FROM owner_activations WHERE owner=?',(owner,)).fetchone()
        return {'owner':owner,'state':'aborting' if journal else 'pending' if pending else 'disabled' if row and registered['disabled'] else 'active' if row else registered['state'],
            'activation_id':journal['activation_id'] if journal else pending['id'] if pending else row[0] if row else None,
            'stage_id':registered.get('stage_id'),'intent_expired':bool(pending and pending.get('expires_at',0)<=time.time()),
            'disabled':registered['disabled']}

    def plan(self,owner,identifier):
        _ids(owner,identifier);self._live()
        with lock(self.runtime,'owner-activation',exclusive=True),self.jobs.transaction(),self.access.connect() as db:
            db.execute('BEGIN IMMEDIATE');paths,directories=self._paths(owner);guard(self.runtime,owner)
            intent=self._intent(paths,owner,identifier);stage_id=intent['request']['stage_id'];self._stage(db,owner,stage_id)
            if spent(db,identifier) or _read(paths['activation_receipt']) is not None:raise JobError('Activation already has a final decision',409)
            client=_read(paths['client'])
            if client is not None and client!=intent['client']:raise JobError('Private client file differs from the original intent; nothing removed',409)
            trust_owned=intent.get('trust_created_by_activation') is True
            if trust_owned and paths['trust'].exists():
                _snapshot(paths['trust'])
                if paths['trust'].read_bytes()!=intent.get('ca_pem','').encode():raise JobError('Private trust file differs from the original intent',409)
            value={'format':FORMAT,'owner':owner,'activation_id':identifier,'stage_id':stage_id,'runtime':str(self.runtime),
                'directories':directories,'files':{'intent':_snapshot(paths['intent']),'client':_snapshot(paths['client']),
                    'trust':_snapshot(paths['trust']) if trust_owned else None},
                'preserve_trust':not trust_owned and (paths['trust'].exists() or paths['trust'].is_symlink())}
            value['id']=_sha(value)
            return {'read_only':True,'plan':value,'credentials_issued':False}

    def apply(self,value):
        _plan_check(value);self._live();owner=value['owner'];identifier=value['activation_id']
        if value['runtime']!=str(self.runtime):raise JobError('Abort plan belongs to another controller state',409)
        with lock(self.runtime,'owner-activation',exclusive=True),self.jobs.transaction(),self.access.connect() as db:
            db.execute('BEGIN IMMEDIATE');paths,directories=self._paths(owner)
            if directories!=value['directories']:raise JobError('Abort control/personal directory identity changed',409)
            journal=_read(paths['journal']);history=paths['history']/(identifier+'.json');receipt=_read(history)
            row=db.execute('SELECT record FROM owner_activation_aborts WHERE id=?',(identifier,)).fetchone()
            expected={'format':'ascend-lab-owner-activation-abort-receipt/v1','owner':owner,'activation_id':identifier,
                'stage_id':value['stage_id'],'plan_id':value['id'],'state':'aborted','credentials_issued':False,
                'preserved_trust':value['preserve_trust'],'original_intent_sha256':value['files']['intent']['sha256']}
            if row and journal is None:
                if json.loads(row[0])!=expected or receipt!=expected:raise JobError('Completed abort receipt differs; do not issue replacement credentials',409)
                # Another activation may now exist. An old replay must not even
                # inspect, delete or revoke its personal files or credentials.
                return {**expected,'reused':True}
            if journal is not None and journal!=value:raise JobError('Resume only the original pending activation abort',409)
            self._stage(db,owner,value['stage_id'])
            if _read(paths['activation_receipt']) is not None:raise JobError('Activation already committed; use disable/revoke instead',409)
            if journal is None:
                self._intent(paths,owner,identifier)
                for name,snapshot in value['files'].items():
                    if name=='trust' and snapshot is None:continue
                    if _snapshot(paths[name])!=snapshot:raise JobError('Activation file identity changed since abort plan',409)
                if len(list(paths['history'].glob('*.json')))>=1024:raise JobError('Archive completed abort receipts before further operations',409)
                _save(paths['journal'],value);_checkpoint('journal')
            for name in ['client','trust']:
                snapshot=value['files'][name]
                if not (name=='trust' and snapshot is None):
                    actual=_snapshot(paths[name])
                    if actual is not None:
                        if actual!=snapshot:raise JobError('Private file changed during abort; unknown data retained',409)
                        paths[name].unlink();_sync(paths[name].parent)
                _checkpoint(name)
            if row:
                if json.loads(row[0])!=expected:raise JobError('Stored abort decision differs',409)
            else:
                db.execute('INSERT INTO owner_activation_aborts VALUES(?,?,?)',(identifier,owner,json.dumps(expected)))
                db.execute('INSERT INTO audit(at,actor,action,target,outcome) VALUES(?,?,?,?,?)',(now(),self.jobs.owner,'owner.activation-abort',owner,'allowed'))
                _checkpoint('before_commit');db.commit();_checkpoint('committed')
            if receipt is not None and receipt!=expected:raise JobError('Private abort receipt differs',409)
            if receipt is None:_save(history,expected)
            _checkpoint('receipt')
            remaining=_snapshot(paths['intent'])
            if remaining is not None:
                if remaining!=value['files']['intent']:raise JobError('Original intent changed during abort',409)
                paths['intent'].unlink();_sync(paths['intent'].parent)
            _checkpoint('intent_removed')
            paths['journal'].unlink();_sync(paths['journal'].parent);_checkpoint('published')
            return {**expected,'reused':bool(row)}

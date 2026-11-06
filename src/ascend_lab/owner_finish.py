"""Resume one final activation + local UID delivery from a Root-private spec.

Earlier identity/storage/Node handoff stages remain explicit prerequisites.
No remote Root shell, OS account creation or implicit resource provisioning.
"""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import resource
import sqlite3
import stat
import subprocess

import deployment as deploy
from deployment import DeployError
from payload_quota_admin import fingerprint,sync,write
from state_lock import state_lock
import owner_delivery as delivery
import unit_refresh as protected

JOURNAL='owner-finish-change.json'
HISTORY='owner-finish-history'
FORMAT='ascend-lab-owner-finish/v1'


def _checkpoint(stage):pass


def request(value):
    from owner_activation import request as activation_request
    if (not isinstance(value,dict) or set(value)!={'operation_id','activation','delivery'}
        or not isinstance(value['operation_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['operation_id'])):
        raise DeployError('Finish requires one explicit 32-hex operation_id, activation and personal delivery definitions')
    result={'operation_id':value['operation_id'],'activation':activation_request(value['activation']),'delivery':delivery.request(value['delivery'])}
    if result['activation']['owner']!=result['delivery']['owner']:raise DeployError('Activation and delivery must select the same logical owner')
    return result


def _binding(prefix,meta,state,control):
    return {'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],'installation_sha256':fingerprint(meta),
        'configuration_sha256':fingerprint(deploy.regular_json(state/'config.json',private=True)),'control_identity':control}


def _private_spec(state,meta,identifier,spec):
    directory=state/'owner-finish'
    if not directory.exists():directory.mkdir(mode=0o700);os.chown(directory,meta['uid'],meta['gid']);sync(state)
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=meta['uid'] or info.st_mode&0o077:raise DeployError('Unsafe final activation request directory')
    path=directory/(identifier+'.json')
    if path.exists() or path.is_symlink():
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:data,_=delivery._file(fd,path.name,meta['uid'])
        finally:os.close(fd)
        if json.loads(data)!=spec:raise DeployError('Final activation request changed')
    else:write(path,spec,uid=meta['uid'],gid=meta['gid'])
    return path


def _service(release,state,meta,path,aborting=False):
    # Fixed installed entry only. No user command/credential in argv or output.
    code='''import sys,json,sqlite3
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from jobs import JobStore
from lab_client import read_json
from owner_activation import Activation
s=Path(sys.argv[2]);spec=read_json(sys.argv[3]);j=JobStore(s,read_json(s/'config.json'))
if sys.argv[4]=='activate':Activation(j).activate(spec)
else:
 p=s/'owner-activations'/(spec['owner']+'.pending.json');intent=read_json(p)
 if intent:
  if intent.get('request')!=spec:raise RuntimeError('Another activation intent is pending')
  a=Activation(j)
  with j.access.connect() as db:row=db.execute('SELECT id FROM owner_activations WHERE owner=?',(spec['owner'],)).fetchone()
  if row:a.activate(spec)
  else:
   from owner_activation_abort import Abort
   b=Abort(j);value=b.plan(spec['owner'],intent['id']);b.apply(value['plan'])
'''
    def drop():
        from payload_groups import gids
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        os.setgroups(gids(meta.get('payload_storage')));os.setgid(meta['gid']);os.setuid(meta['uid']);os.umask(0o077)
    # No captured output or process-wide FSIZE limit: that limit would also
    # restrict legitimate SQLite writes once a multi-user DB exceeds 256 KiB.
    try:r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(release),str(state),str(path),'abort' if aborting else 'activate'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,preexec_fn=drop,timeout=30,
        env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    except subprocess.TimeoutExpired:raise DeployError('Activation response is unknown; retain and resume the original finish request') from None
    if r.returncode:raise DeployError('Activation/abort did not finish; retain the original coordinator and inspect service-private state without printing credentials')


def _preflight(state,spec):
    with sqlite3.connect('file:'+str(state/'identities.sqlite3')+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT disabled,password_hash,profile FROM users WHERE id=?',(spec['activation']['owner'],)).fetchone()
    if not row:raise DeployError('Stage the owner and complete storage/Node handoff before finishing')
    profile=json.loads(row[2]);marker=profile.get('provisioning') or profile.get('provisioned',{})
    if marker.get('stage_id',marker.get('id'))!=spec['activation']['stage_id']:raise DeployError('Finish stage definition differs from the registered identity')
    parent,spool,directories=delivery._directories(spec['delivery'])
    try:
        if delivery._exists(parent,spec['delivery']['name']):raise DeployError('Finish needs a new personal profile directory')
        return directories
    finally:os.close(parent);os.close(spool)


def finish(prefix,value,preview=False,abort=False):
    deploy.require_root();spec=request(value);prefix,meta,state,control=delivery._context(prefix,recovery=True,finish=True)
    release=prefix/'current'
    if hashlib.sha256((release/Path(__file__).name).read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():raise DeployError('Use matching installed finish coordinator code')
    if spec['delivery']['uid']==meta['uid'] or spec['delivery']['gid']==meta['gid']:raise DeployError('Personal finish cannot select the service identity')
    identifier=fingerprint({'prefix':str(prefix),'request':spec});journal=prefix/JOURNAL;history=prefix/HISTORY;receipt=history/(identifier+'.json')
    fd=os.open(prefix/'.owner-finish.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=0 or info.st_nlink!=1 or not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise DeployError('Unsafe Root finish lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DeployError('Another finish coordinator is running') from None
        with state_lock(state):
            pending=delivery._root_record(journal) if journal.exists() or journal.is_symlink() else None
            if pending is not None and (not isinstance(pending,dict) or set(pending)!={'format','id','request','binding','directories','delivery_plan'}
                or pending['format']!=FORMAT or pending['id']!=identifier or pending['request']!=spec):raise DeployError('Resume only the original owner finish request')
            if history.exists() or history.is_symlink():protected._directory(history,private=True)
            if receipt.exists() or receipt.is_symlink():
                result=delivery._root_record(receipt)
                if (set(result)!={'format','id','owner','state','delivery_plan_id','activation_id','credentials_in_output','prior_state_retained'} or result['format']!=FORMAT
                    or result['id']!=identifier or result['owner']!=spec['activation']['owner'] or result['state'] not in {'delivered','aborted'}
                    or result['credentials_in_output'] is not False or type(result['prior_state_retained']) is not bool):raise DeployError('Finish receipt differs')
                if pending is not None:journal.unlink();sync(prefix);_checkpoint('published')
                return {**result,'historical_receipt_only':True}
            child=prefix/delivery.JOURNAL
            if child.exists() or child.is_symlink():
                existing=delivery._root_record(child)
                if pending is None or pending['delivery_plan'] is None or existing.get('plan')!=pending['delivery_plan']:
                    raise DeployError('Another credential delivery is pending')
            if preview:
                if pending is not None:return {'id':identifier,'read_only':True,'pending':True,'credentials_issued':False}
                directories=_preflight(state,spec)
                return {'id':identifier,'read_only':True,'request':spec,'directories':directories,'credentials_issued':False}
            if pending is None:
                if abort:raise DeployError('No pending owner finish to abort')
                directories=_preflight(state,spec)
                if not history.exists():history.mkdir(mode=0o700);sync(prefix)
                if len(list(history.glob('*.json')))>=1024:raise DeployError('Archive finish receipts before more operations')
                pending={'format':FORMAT,'id':identifier,'request':spec,'binding':_binding(prefix,meta,state,control),'directories':directories,'delivery_plan':None}
                write(journal,pending);_checkpoint('journal')
            if pending['binding']!=_binding(prefix,meta,state,control):raise DeployError('Finish installation or configuration changed')
            path=_private_spec(state,meta,identifier,spec['activation'])
            if abort:
                if child.exists():delivery.apply(prefix,pending['delivery_plan'],abort=True,finish=True)
                _service(release,state,meta,path,aborting=True)
                result={'format':FORMAT,'id':identifier,'owner':spec['activation']['owner'],'state':'aborted',
                    'delivery_plan_id':pending['delivery_plan']['id'] if pending['delivery_plan'] else None,
                    'activation_id':None,'credentials_in_output':False,'prior_state_retained':True}
            else:
                if pending['delivery_plan'] is None:
                    parent,spool,directories=delivery._directories(spec['delivery'])
                    try:
                        if directories!=pending['directories'] or delivery._exists(parent,spec['delivery']['name']):raise DeployError('Finish personal destination changed')
                    finally:os.close(parent);os.close(spool)
                    _service(release,state,meta,path);_checkpoint('activation')
                    value=delivery.plan(prefix,spec['delivery'],finish=True)['plan'];pending={**pending,'delivery_plan':value}
                    write(journal,pending);_checkpoint('planned')
                result=delivery.apply(prefix,pending['delivery_plan'],finish=True)
                if result['state']!='delivered':raise DeployError('This finish delivery was aborted; explicitly abort the finish operation')
                _checkpoint('delivered')
                result={'format':FORMAT,'id':identifier,'owner':spec['activation']['owner'],'state':'delivered','delivery_plan_id':result['plan_id'],
                    'activation_id':result['activation_id'],'credentials_in_output':False,'prior_state_retained':False}
            write(receipt,result);_checkpoint('receipt');journal.unlink();sync(prefix);_checkpoint('published')
            return {**result,'historical_receipt_only':False}
    finally:os.close(fd)

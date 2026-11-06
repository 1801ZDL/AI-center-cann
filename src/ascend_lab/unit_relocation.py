"""Root-recorded predecessor for an offline unit refresh after state restore.

The proof lives in protected installation metadata, never in restored user
state. It authorizes one exact before-image for one exact new installation.
It does not write system units or contact a service manager.
"""
import hashlib
from pathlib import Path
import re

import deployment as deploy
from deployment import DeployError
from payload_quota_admin import fingerprint

FIELD='unit_relocation'
FORMAT='ascend-lab-unit-relocation/v1'


def sha(text):return hashlib.sha256(text.encode()).hexdigest()


def validate(value):
    keys={'format','prefix','role','old_state','new_state','before_unit','before_sha256','desired_sha256','binding_sha256','id'}
    if (not isinstance(value,dict) or set(value)!=keys or value['format']!=FORMAT
        or not isinstance(value['role'],str) or value['role'] not in {'controller','node'}):
        raise DeployError('Invalid unit relocation record')
    for key in ['prefix','old_state','new_state']:
        path=value[key]
        if (not isinstance(path,str) or not re.fullmatch('/[A-Za-z0-9_./-]+',path)
            or str(Path(path))!=path or '..' in Path(path).parts):raise DeployError('Invalid unit relocation path')
    if value['old_state']==value['new_state']:raise DeployError('Unit relocation needs distinct state directories')
    if not isinstance(value['before_unit'],str) or len(value['before_unit'].encode())>65536:
        raise DeployError('Unit relocation before-image exceeds its contract')
    for key in ['before_sha256','desired_sha256','binding_sha256','id']:
        if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise DeployError('Invalid unit relocation digest')
    if value['before_sha256']!=sha(value['before_unit']) or value['id']!=fingerprint({k:v for k,v in value.items() if k!='id'}):
        raise DeployError('Unit relocation record changed')


def binding(meta):return fingerprint({k:v for k,v in meta.items() if k!=FIELD})


def capture(prefix,meta):
    deploy.require_root()
    from unit_refresh import _directory,_file,_managed
    _directory(Path(prefix)/'units')
    original=_file(Path(prefix)/'units'/('ascend-lab-'+meta['role']+'.service'),0o444)
    _managed(original['text'],prefix,meta,deploy.unit_text(prefix,meta))
    return original


def prepare(prefix,before,after,original):
    deploy.require_root()
    from unit_refresh import _file,_managed
    if (before['role']!=after['role'] or before['uid']!=after['uid'] or before['gid']!=after['gid']
        or after.get('previous_state_dir')!=before['state_dir']):raise DeployError('Unit relocation identity differs from the restore')
    current=_file(Path(prefix)/'units'/('ascend-lab-'+before['role']+'.service'),0o444)
    if current!=original:raise DeployError('Unit template changed during restore; preserve both state directories')
    _managed(current['text'],prefix,before,deploy.unit_text(prefix,before))
    value={'format':FORMAT,'prefix':str(prefix),'role':after['role'],'old_state':before['state_dir'],
        'new_state':after['state_dir'],'before_unit':current['text'],'before_sha256':sha(current['text']),
        'desired_sha256':sha(deploy.unit_text(prefix,after)),'binding_sha256':binding(after)}
    value['id']=fingerprint(value);validate(value)
    return value


def accepts(text,prefix,meta,desired):
    if FIELD not in meta:return False
    value=meta[FIELD];validate(value)
    return (value['prefix']==str(prefix) and value['role']==meta['role'] and value['old_state']==meta.get('previous_state_dir')
        and value['new_state']==meta['state_dir'] and value['binding_sha256']==binding(meta)
        and value['desired_sha256']==sha(desired) and text==value['before_unit'])

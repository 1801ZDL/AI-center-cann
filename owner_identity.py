"""Credential-free staged owner definitions. Authentication stays closed until commit.

This module deliberately has no activation/token export operation. Those require
the later multi-layer owner binding receipts, not a generic account enable flag.
"""
import hashlib
import json

FORMAT='ascend-lab-owner-stage/v1'
DEFAULT_QUOTA={'max_running':1,'max_queued':10,'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0,
               'input_storage_mib':512,'archive_storage_mib':2048}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def definition(user,workspace,environments,quota,simulation_uid):
    if not isinstance(environments,(list,tuple)) or list(environments)!=['worker-python']:
        raise ValueError('Staged owners require the managed worker environment')
    if quota is not None and (not isinstance(quota,dict) or set(quota)-set(DEFAULT_QUOTA)
        or any(type(v) is not int or not 0<=v<=10**6 for v in quota.values())):raise ValueError('Invalid staged owner quota')
    profile={'role':'user','workspace':str(workspace),'environments':['worker-python'],'mode':'templates',
             'quota':{**DEFAULT_QUOTA,**(quota or {})},'simulation_uid':simulation_uid}
    return profile,{'format':FORMAT,'state':'staged','id':digest({'owner':user,'profile':profile})}


def staged(user,profile):
    if not isinstance(profile,dict):return False
    marker=profile.get('provisioning')
    if not isinstance(marker,dict) or set(marker)!={'format','state','id'} or marker.get('format')!=FORMAT or marker.get('state')!='staged':return False
    base={k:v for k,v in profile.items() if k!='provisioning'}
    return marker['id']==digest({'owner':user,'profile':base})


def login_allowed(profile):
    # Unknown/incomplete provisioning metadata must never silently become an
    # ordinary legacy account. Activation is intentionally not implemented here.
    return isinstance(profile,dict) and 'provisioning' not in profile


def relocate(user,profile,workspace):
    """Offline restore rebases a disabled stage; old plans become stale."""
    if 'provisioning' not in profile:return {**profile,'workspace':workspace}
    if not staged(user,profile):raise ValueError('Invalid staged owner definition in snapshot')
    base={**profile,'workspace':workspace};base.pop('provisioning')
    return {**base,'provisioning':{'format':FORMAT,'state':'staged','id':digest({'owner':user,'profile':base})}}

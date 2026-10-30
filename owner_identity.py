"""Credential-free staged owner definitions. Authentication stays closed until commit.

Activation lives in owner_activation and requires multi-layer binding evidence.
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
    if not isinstance(profile,dict) or 'provisioning' in profile:return False
    if 'provisioned' not in profile:return True
    import re
    value=profile['provisioned']
    valid=(isinstance(value,dict) and set(value)=={'format','id','stage_id','required_nodes'}
        and value['format']=='ascend-lab-owner-activation/v1'
        and isinstance(value['id'],str) and re.fullmatch('[0-9a-f]{32}',value['id']) is not None
        and isinstance(value['stage_id'],str) and re.fullmatch('[0-9a-f]{64}',value['stage_id']) is not None
        and isinstance(value['required_nodes'],list) and 1<=len(value['required_nodes'])<=32)
    if not valid:return False
    seen=set()
    for item in value['required_nodes']:
        if (not isinstance(item,dict) or set(item)!={'node_id','plan_id'}
            or not isinstance(item['node_id'],str) or re.fullmatch('worker-[0-9a-f]{12}',item['node_id']) is None
            or not isinstance(item['plan_id'],str) or re.fullmatch('[0-9a-f]{64}',item['plan_id']) is None
            or item['node_id'] in seen):return False
        seen.add(item['node_id'])
    return True


def relocate(user,profile,workspace):
    """Offline restore rebases a disabled stage; old plans become stale."""
    if 'provisioning' not in profile:return {**profile,'workspace':workspace}
    if not staged(user,profile):raise ValueError('Invalid staged owner definition in snapshot')
    base={**profile,'workspace':workspace};base.pop('provisioning')
    return {**base,'provisioning':{'format':FORMAT,'state':'staged','id':digest({'owner':user,'profile':base})}}

"""Administrator-pinned device-node exposure and trusted management observations."""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from jobs import now
from npu_admission import query,selected_reading

CAPABILITY='npu-device-whitelist-v1'
MANAGEMENT={'/dev/davinci_manager','/dev/hisi_hdc','/dev/devmm_svm','/dev/svm0'}
DRIVER_PATHS={'/usr/local/Ascend/driver','/usr/local/Ascend/firmware','/usr/local/dcmi',
              '/etc/ascend_install.info','/etc/vnpu.cfg'}
KEYS={'device_ids','visible_ids','device_gid','management_devices','driver_paths','query_tool','management_env'}


def validate(policy,host=False):
    if not isinstance(policy,dict) or set(policy)!=KEYS:raise ValueError('NPU profile requires the complete administrator device contract')
    ids=policy['device_ids'];mapping=policy['visible_ids']
    if (not isinstance(ids,list) or not 1<=len(ids)<=256 or any(type(value) is not int or not 0<=value<256 for value in ids)
        or len(ids)!=len(set(ids)) or not isinstance(mapping,dict) or set(mapping)!={str(value) for value in ids}
        or any(type(value) is not int or not 0<=value<256 for value in mapping.values())):
        raise ValueError('NPU physical/visible mapping must be explicit')
    if type(policy['device_gid']) is not int or not 1<=policy['device_gid']<2**31:raise ValueError('NPU device group must be non-root')
    for key,allowed in [('management_devices',MANAGEMENT),('driver_paths',DRIVER_PATHS)]:
        values=policy[key]
        if not isinstance(values,list) or not values or any(not isinstance(value,str) or value not in allowed for value in values) or len(values)!=len(set(values)):
            raise ValueError('Only registered NPU management devices and read-only driver paths are supported')
    if '/dev/davinci_manager' not in policy['management_devices'] or '/usr/local/Ascend/driver' not in policy['driver_paths']:
        raise ValueError('NPU profile must include driver and manager')
    for key in ['query_tool','management_env']:
        path=policy[key]
        if not isinstance(path,str) or not path.startswith('/') or len(path)>1024 or any(char in path for char in ['\0','\n']):raise ValueError('Invalid administrator management tool path')
    if host:
        for device in ['/dev/davinci'+str(value) for value in ids]+policy['management_devices']:
            info=Path(device).lstat()
            if not stat.S_ISCHR(info.st_mode):raise ValueError('NPU source is not a character device')
            if device.startswith('/dev/davinci') and device[12:].isdigit() and info.st_gid!=policy['device_gid']:
                raise ValueError('NPU group differs from physical device ownership')
        for name in policy['driver_paths']:
            path=Path(name);info=path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or info.st_uid!=0 or info.st_mode&0o022:
                raise ValueError('Driver sources must be root-owned and protected against other writers')
        for key in ['query_tool','management_env']:
            if not Path(policy[key]).is_file():raise ValueError('NPU management prerequisite is missing')
    return policy


def fingerprint(policy):
    validate(policy)
    return hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def observe(policy):
    validate(policy,host=True)
    devices,processes=query(policy['management_env'],policy['query_tool'])
    selected=[]
    for physical in policy['device_ids']:
        device=next((item for item in devices if item['id']==physical),None)
        if device is None:raise ValueError('Registered physical NPU is missing from management inventory')
        selected.append({'id':physical,'name':device.get('name','UNKNOWN'),'health':device['health'],'utilization_pct':device['utilization_pct'],
                         'process_count':sum(item['device_id']==physical for item in processes),
                         **{key:device[key] for key in ['temperature_c','power_w'] if device.get(key) is not None}})
    return {'status':'ok','sampled_at':now(),'devices':selected,'policy_sha256':fingerprint(policy)}


def observation(body,policy):
    """Normalize a node observation; no PIDs, commands, or node-selected paths cross HTTP."""
    if not isinstance(body,dict) or body.get('policy_sha256')!=fingerprint(policy):raise ValueError('NPU observation contract differs')
    if body.get('status')!='ok':return {'status':'error','devices':[],'sampled_at':None,'policy_sha256':fingerprint(policy)}
    sampled=body.get('sampled_at')
    try:age=time.time()-dt.datetime.fromisoformat(sampled).timestamp()
    except (TypeError,ValueError):raise ValueError('NPU observation time is invalid') from None
    if not 0<=age<=15:raise ValueError('NPU observation is stale')
    rows=body.get('devices')
    if not isinstance(rows,list) or len(rows)!=len(policy['device_ids']):raise ValueError('NPU inventory differs')
    result=[]
    for item in rows:
        if (not isinstance(item,dict) or type(item.get('id')) is not int or item['id'] not in policy['device_ids']
            or item.get('health') not in {'OK','Warning','Alarm','UNKNOWN'}
            or type(item.get('process_count')) is not int or item['process_count']<0
            or type(item.get('utilization_pct')) not in {float,int} or not math.isfinite(item['utilization_pct'])
            or not 0<=item['utilization_pct']<=100):raise ValueError('NPU observation contains an invalid device')
        name=item.get('name','UNKNOWN')
        if not isinstance(name,str) or not 1<=len(name)<=80 or any(ord(c)<32 for c in name):raise ValueError('Invalid NPU management name')
        result.append({**{key:item[key] for key in ['id','health','utilization_pct','process_count']},'name':name})
        for key,lower,upper in [('temperature_c',-100,250),('power_w',0,100000)]:
            value=item.get(key)
            if type(value) in {int,float} and math.isfinite(value) and lower<=value<=upper:result[-1][key]=value
    if {item['id'] for item in result}!=set(policy['device_ids']):raise ValueError('NPU observation has repeated devices')
    return {'status':'ok','devices':result,'sampled_at':sampled,'policy_sha256':fingerprint(policy)}


def check(grant,policy):
    validate(policy,host=True)
    ids=grant['allocation'].get('npu_ids',[])
    if (grant['resources']['npu']!=1 or len(ids)!=1 or ids[0] not in policy['device_ids']
        or grant.get('npu_policy_sha256')!=fingerprint(policy)):
        raise ValueError('NPU grant does not match the administrator device contract')
    reading=observe(policy)
    wanted=grant['allocation'].get('npu_name')
    if wanted and any(item.get('name','UNKNOWN')!=wanted for item in reading['devices'] if item['id'] in ids):raise ValueError('NPU management name changed after allocation')
    selected_reading(ids,(reading['devices'],[]))
    return {'sampled_at':reading['sampled_at'],'devices':[item for item in reading['devices'] if item['id'] in ids],
            'policy_sha256':reading['policy_sha256']}


def docker_arguments(grant,policy):
    validate(policy,host=True)
    ids=grant['allocation']['npu_ids']
    if len(ids)!=1 or ids[0] not in policy['device_ids']:raise ValueError('Only one approved NPU per task is supported')
    args=[]
    for path in ['/dev/davinci'+str(value) for value in ids]+policy['management_devices']:
        args+=['--device',path+':'+path+':rwm']
    for path in policy['driver_paths']:args+=['--mount','type=bind,src='+path+',dst='+path+',readonly']
    return args

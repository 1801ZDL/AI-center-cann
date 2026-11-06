"""Root onboarding drain: reject new submissions, preserve existing execution.

Only explicitly selected installed process-mode local Nodes can be stopped.
Remote/managed services retain their existing explicit administrator protocol.
"""
import hashlib
from pathlib import Path
import re

import deployment as deploy
from deployment import DeployError
from lab_client import read_json

CAPABILITY='owner-maintenance-drain-v1'
FORMAT='ascend-lab-owner-maintenance-drain/v1'


def capture(prefix,meta,config,spec,local_nodes,registered,running):
    if spec['service']['mode']=='process':
        process=read_json(Path(meta['state_dir'])/'service-process.json') or {}
        if process.get('manager_invocation_id'):raise DeployError('Use the stopped managed lifecycle for a controller previously launched by its service manager')
    from owner_provision import _authority
    result={'format':FORMAT,'controller_resume':bool(running),'nodes':{}}
    for node,selected in local_nodes.items():
        target,nmeta,release=deploy.verify(selected)
        current=deploy.regular_json(Path(nmeta['state_dir'])/'config.json',private=True)
        if (nmeta['role']!='node' or current['node_id']!=node or current['physical_id']!=registered[node]['physical_id']
            or _authority(current)!=_authority(config)):
            raise DeployError('Local drain mapping selects a different Node, physical host or controller authority')
        helper=release/'owner_provision.py'
        if not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256((Path(__file__).parent/'owner_provision.py').read_bytes()).digest():
            raise DeployError('Upgrade the selected local Node participant before beginning onboarding drain')
        saved=read_json(Path(nmeta['state_dir'])/'service-process.json') or {}
        if saved.get('manager_invocation_id'):raise DeployError('Managed local Node lifecycle requires explicit administrator stop/start; omit its local process mapping')
        result['nodes'][node]={'prefix':str(target),'state_dir':nmeta['state_dir'],'release_id':nmeta['release_id'],
            'configuration_sha256':hashlib.sha256((Path(nmeta['state_dir'])/'config.json').read_bytes()).hexdigest(),
            'resume':bool(deploy.process_status(target)['running'])}
    return result


def check(value,record):
    if (not isinstance(value,dict) or set(value)!={'format','controller_resume','nodes'} or value['format']!=FORMAT
        or type(value['controller_resume']) is not bool or not isinstance(value['nodes'],dict)
        or set(value['nodes'])-set(record['nodes'])):raise DeployError('Invalid original maintenance drain contract')
    seen=set()
    for node,item in value['nodes'].items():
        if (not isinstance(item,dict) or set(item)!={'prefix','state_dir','release_id','configuration_sha256','resume'}
            or type(item['resume']) is not bool or any(not isinstance(item[k],str) or not re.fullmatch('[0-9a-f]{64}',item[k]) for k in ['release_id','configuration_sha256'])
            or any(not isinstance(item[k],str) or not item[k].startswith('/') or str(Path(item[k]))!=item[k] or '..' in Path(item[k]).parts for k in ['prefix','state_dir'])
            or item['prefix'] in seen):raise DeployError('Invalid or aliased local Node drain snapshot')
        seen.add(item['prefix'])


def mapping(record,supplied):
    result=dict(supplied)
    for node,item in record.get('drain',{}).get('nodes',{}).items():
        if node in result and str(Path(result[node]).absolute())!=item['prefix']:raise DeployError('Original local drain mapping changed')
        result[node]=item['prefix']
    return result


def _node(record,node):
    item=record['drain']['nodes'][node];prefix,meta,_=deploy.verify(item['prefix'])
    raw=Path(meta['state_dir'])/'config.json';config=deploy.regular_json(raw,private=True)
    if (meta['role']!='node' or meta['state_dir']!=item['state_dir'] or meta['release_id']!=item['release_id']
        or config['node_id']!=node or config['physical_id']!=record['nodes'][node]['physical_id']
        or hashlib.sha256(raw.read_bytes()).hexdigest()!=item['configuration_sha256']):
        raise DeployError('Original local Node installation/configuration changed during drain')
    saved=read_json(raw.parent/'service-process.json') or {}
    if saved.get('manager_invocation_id'):raise DeployError('A local Node became managed; no process signal was sent')
    return prefix,meta,deploy.process_status(prefix)


def readiness(jobs):
    """The jobs write reservation is the cutoff proof for pre-gate submissions."""
    from jobs import ACTIVE
    from maintenance import assert_quiescent
    with jobs.transaction() as db:
        rows=jobs.all(db);queued=sum(j['state']=='queued' for j in rows)
        active=sum(j['state'] in ACTIVE for j in rows);unknown=sum(j['state']=='needs_attention' for j in rows)
        holds=len(jobs.inventory.holds(db=db));reason=None
        if queued or active or holds:reason='Waiting for existing queued/active work and explicit inventory reconciliation; no task was cancelled'
        else:
            try:assert_quiescent(jobs.runtime,'controller')
            except (OSError,ValueError) as error:reason=str(error)[:512]
        return {'quiescent':reason is None,'queued':queued,'active':active,'unknown':unknown,'inventory_holds':holds,'reason':reason}



def status(value):
    if (not isinstance(value,dict) or set(value)!={'quiescent','queued','active','unknown','inventory_holds','reason'}
        or type(value['quiescent']) is not bool or any(type(value[k]) is not int or value[k]<0 for k in ['queued','active','unknown','inventory_holds'])
        or value['unknown']>value['active'] or value['reason'] is not None and (not isinstance(value['reason'],str) or len(value['reason'])>512)
        or value['quiescent'] and (any(value[k] for k in ['queued','active','unknown','inventory_holds']) or value['reason'] is not None)):
        raise DeployError('Invalid fixed drain readiness response')
    return value


def resume_running(prefix,record,save):
    """Restore only processes already observed running in this original flow."""
    status=deploy.process_status(prefix);changed=False
    if status['running'] and not record['drain']['controller_resume']:
        record['drain']['controller_resume']=True;changed=True
    if changed:save()
    if record['request']['service']['mode']=='process' and record['drain']['controller_resume'] and not status['running']:
        status=deploy.start(prefix)
    for node,item in record['drain']['nodes'].items():
        selected,_,current=_node(record,node)
        if current['running'] and not item['resume']:item['resume']=True;save()
        if status['running'] and item['resume'] and not current['running']:deploy.start(selected)
    return status


def local_quiescent(record):
    from maintenance import assert_quiescent
    for node in record['drain']['nodes']:
        _,meta,_=_node(record,node)
        try:assert_quiescent(Path(meta['state_dir']),'node')
        except (OSError,ValueError) as error:return {'ready':False,'reason':'Waiting for local Node cleanup/acknowledgement: '+str(error)[:384]}
    return {'ready':True,'reason':None}


def stop_local(record,save,checkpoint):
    for node,item in record['drain']['nodes'].items():
        selected,_,status=_node(record,node)
        if status['running'] and not item['resume']:item['resume']=True;save()
        deploy.stop(selected);checkpoint('local_drained')
    # Detect late local cleanup rather than entering irreversible user staging.
    return local_quiescent(record)


def restore(prefix,record,*,nodes=False):
    if record['request']['service']['mode']=='process' and record['drain']['controller_resume']:
        if not deploy.start(prefix)['ready']:return False
    if nodes or record['phase']=='drained':
        for node,item in record['drain']['nodes'].items():
            if item['resume']:
                selected,_,_=_node(record,node)
                current=deploy.start(selected)
                if not current['running'] or record['drain']['controller_resume'] and not current['ready']:return False
    return True


def reopen(prefix,record,save,publish):
    """Retain the stopped-participant recovery obligation until restoration.

    A crash or partial restart must leave phase=drained so an explicit abort
    still restores original Nodes. Only completed restoration returns to held.
    """
    if record['phase']!='drained':raise DeployError('Only a drained operation can reopen its retained work')
    publish('draining',True)
    if not restore(prefix,record,nodes=True):return False
    record['phase']='held';save()
    return True

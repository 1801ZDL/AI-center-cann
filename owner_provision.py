"""Root controller participant coordinating staged owner onboarding.

Only fixed installed operations run as the service UID. Node packets contain
no credentials; remote Root execution/transport is deliberately absent. An
explicit local Node map can drive those same packets on this physical host.
"""
from contextvars import ContextVar
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import sqlite3
import stat
import subprocess

import deployment as deploy
from deployment import DeployError
import payload_quota_admin as files
import payload_owner_admin as storage
import owner_delivery as delivery
from node_owner_admin import sha,_plan_check
from node_owner_provision import request as node_request
from state_lock import state_lock

FORMAT='ascend-lab-owner-provision/v1'
CAPABILITY='owner-provision-v1'
PACKET='ascend-lab-owner-node-request/v1'
JOURNAL='owner-provision-change.json'
GATE='owner-provision-admission.json'
HISTORY='owner-provision-history'
MAX_RECORD=12*1024**2
MAX_HISTORY_BYTES=128*1024**2
_ROOT=ContextVar('ascend_lab_controller_owner_participant',default=None)


def _checkpoint(stage):pass


def _path(value):
    return isinstance(value,str) and value.startswith('/') and value==str(Path(value)) and '..' not in Path(value).parts


def request(value):
    from auth import IDENTIFIER
    import owner_identity
    if (not isinstance(value,dict) or set(value)!={'operation_id','owner','quota','storage','nodes','service','delivery'}
        or not isinstance(value['operation_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['operation_id'])
        or not isinstance(value['owner'],str) or not IDENTIFIER.fullmatch(value['owner'])):raise DeployError('Provision requires one explicit operation, owner, quota, storage, Nodes, service and delivery')
    profile,_=owner_identity.definition(value['owner'],'unused',['worker-python'],value['quota'],1000)
    item=value['storage']
    if (not isinstance(item,dict) or set(item)!={'gid','bytes','inodes'} or type(item['gid']) is not int or not 1000<=item['gid']<2**31
        or type(item['bytes']) is not int or not 1024**2<=item['bytes']<=1024**4
        or type(item['inodes']) is not int or not 128<=item['inodes']<=10**7):raise DeployError('Invalid finite central owner storage request')
    if sum(profile['quota'][k] for k in ['input_storage_mib','archive_storage_mib'])*1024**2+65536>item['bytes']:
        raise DeployError('Central native storage cannot cover the owner budgets and metadata')
    if (profile['quota']['npu'] or profile['quota']['memory_mib']<256 or profile['quota']['archive_storage_mib']<10
        or any(profile['quota'][k]<1 for k in ['cpu','max_running','max_queued','input_storage_mib'])):
        raise DeployError('This onboarding requires runnable CPU quotas; NPU pre-activation qualification remains unavailable')
    service=value['service']
    if (not isinstance(service,dict) or set(service)!={'mode','unit_dir'} or service['mode'] not in {'process','systemd'}
        or service['unit_dir'] is not None and not _path(service['unit_dir'])
        or service['mode']=='systemd' and service['unit_dir'] is None):raise DeployError('Select process or stopped-systemd lifecycle with an explicit unit directory')
    nodes=value['nodes']
    if not isinstance(nodes,list) or not 1<=len(nodes)<=32:raise DeployError('Select 1..32 explicitly registered Nodes')
    seen=set()
    for node in nodes:
        if (not isinstance(node,dict) or set(node)!={'node_id','uid','gid','workspace','cache'} or not isinstance(node['node_id'],str)
            or not re.fullmatch('worker-[0-9a-f]{12}',node['node_id']) or node['node_id'] in seen):raise DeployError('Required Node IDs must be unique and explicit')
        node_request({'operation_id':'0'*32,'owner':value['owner'],'stage_id':'0'*64,**{k:node[k] for k in ['uid','gid','workspace','cache']}})
        seen.add(node['node_id'])
    if delivery.request(value['delivery'])['owner']!=value['owner']:raise DeployError('Personal delivery must select the same logical owner')
    return copy.deepcopy(value)


def _read(path):
    value=deploy.regular_json(path,max_bytes=MAX_RECORD,private=True);info=Path(path).lstat()
    if info.st_uid!=0:raise DeployError('Provision records must remain Root-owned')
    return value


def _write(path,value,mode=0o600):
    size=len((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
    if size>MAX_RECORD:raise DeployError('Provision metadata exceeds its finite record limit')
    path=Path(path)
    if path.parent.parent.name==HISTORY:
        total=_budget(path.parent.parent)
        if total+size>MAX_HISTORY_BYTES:raise DeployError('Provision history lacks space for this original record')
    files.write(path,value,mode=mode)


def guard(prefix):
    prefix=Path(prefix);pending=prefix/JOURNAL;gate=prefix/GATE
    if pending.exists() or pending.is_symlink() or gate.exists() or gate.is_symlink():
        if os.geteuid()==0 and _ROOT.get()==str(prefix):return
        raise DeployError('Resume the original central owner provisioning operation before offline maintenance')


def _gate(prefix,state):
    path=Path(prefix)/GATE
    if not path.exists() and not path.is_symlink():return None
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o777!=0o644 or info.st_nlink!=1:
        raise DeployError('Unsafe central provisioning admission gate')
    value=deploy.regular_json(path,max_bytes=65536)
    if (set(value)!={'format','id','prefix','state_dir','phase','service_allowed'} or value['format']!=FORMAT
        or value['prefix']!=str(prefix) or value['state_dir']!=str(state) or value['phase'] not in {'maintenance','open'}
        or not isinstance(value['id'],str) or not re.fullmatch('[0-9a-f]{64}',value['id']) or type(value['service_allowed']) is not bool
        or value['phase']=='open' and value['service_allowed'] is not True):raise DeployError('Invalid central provisioning admission binding')
    return value


def admission(prefix,state):
    pending=Path(prefix)/JOURNAL;value=_gate(prefix,state)
    if value is None:return bool(pending.exists() or pending.is_symlink())
    if not pending.exists() or pending.is_symlink():return True
    return value['phase']!='open'


def service_guard(prefix,meta,stopping=False):
    pending=Path(prefix)/JOURNAL;path=Path(prefix)/GATE
    if not any(p.exists() or p.is_symlink() for p in [pending,path]):return
    if meta['role']!='controller':raise DeployError('Central owner coordinator records do not belong to a Node')
    if stopping:return
    value=_gate(prefix,meta['state_dir'])
    if not pending.exists() or value is None or not value['service_allowed']:
        raise DeployError('Owner provisioning has not enabled this controller startup')


def _publish_gate(prefix,record,phase,allowed):
    value={'format':FORMAT,'id':record['id'],'prefix':str(prefix),'state_dir':record['binding']['state_dir'],
        'phase':phase,'service_allowed':allowed}
    _write(prefix/GATE,value,mode=0o644)


def _budget(history,creating=False):
    if not history.exists():
        if not creating:return 0
        history.mkdir(mode=0o700);files.sync(history.parent)
    from unit_refresh import _directory
    _directory(history,private=True);count=directories=total=0
    for directory in history.iterdir():
        count+=1;directories+=1
        if directories>128 or not re.fullmatch('[0-9a-f]{64}',directory.name):raise DeployError('Archive owner provisioning history before continuing')
        _directory(directory,private=True)
        for path in directory.iterdir():
            count+=1;info=path.lstat()
            if (count>4096 or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_nlink!=1
                or info.st_size>MAX_RECORD):raise DeployError('Unsafe or excessive owner provisioning history')
            total+=info.st_size
    if total+(3*MAX_RECORD if creating else 0)>MAX_HISTORY_BYTES:raise DeployError('Owner provisioning history exceeds its finite byte budget')
    return total


def _binding(prefix,meta,state,config):
    from backends import physical_identity
    return {'prefix':str(prefix),'state_dir':str(state),'release_id':meta['release_id'],'uid':meta['uid'],'gid':meta['gid'],
        'physical_id':physical_identity(),'control_identity':storage._control(state),
        'base_configuration_sha256':files._base_config(config),'base_installation_sha256':files._base_meta(meta)}


def _params(state,meta,identifier,action,value):
    directory=state/'owner-provision'
    if not directory.exists():directory.mkdir(mode=0o700);os.chown(directory,meta['uid'],meta['gid']);files.sync(state)
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=meta['uid'] or info.st_mode&0o077:raise DeployError('Unsafe service participant directory')
    digest=sha(value);path=directory/(identifier+'-'+action+'-'+digest+'.json')
    count=total=0
    for entry in directory.iterdir():
        count+=1;info=entry.lstat();total+=info.st_size
        if count>4096 or not stat.S_ISREG(info.st_mode) or info.st_uid!=meta['uid'] or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>MAX_RECORD:
            raise DeployError('Unsafe or excessive service participant metadata')
    if total+len(json.dumps(value).encode())>MAX_HISTORY_BYTES:raise DeployError('Archive service participant metadata before continuing')
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if info.st_uid!=meta['uid'] or deploy.regular_json(path,max_bytes=MAX_RECORD,private=True)!=value:raise DeployError('Service participant request changed')
    else:files.write(path,value,meta['uid'],meta['gid'])
    return path


def _service(prefix,meta,identifier,action,value):
    if action not in {'stage','quiescent','approve','ready','groups'}:raise DeployError('Unknown fixed service participant operation')
    state=Path(meta['state_dir']);path=_params(state,meta,identifier,action,value)
    code='''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from jobs import JobStore,ACTIVE,JobError
from lab_client import read_json
s=Path(sys.argv[2]);action=sys.argv[3];v=read_json(sys.argv[4]);j=JobStore(s,read_json(s/'config.json'))
if action=='stage':
 from storage_files import private_tree
 base=Path(j.access.config.get('identity_workspace_root',s/'personas'))
 if base==s/'personas':private_tree(s,['personas'])
 private_tree(base,[v['owner'],'workspace'])
 result=j.access.stage_user(v['owner'],base/v['owner']/'workspace',['worker-python'],v['quota'])
elif action=='quiescent':
 with j.transaction() as db:
  if any(x['state'] in ACTIVE or x['state']=='queued' for x in j.all(db)) or j.inventory.holds(db=db):raise JobError('Existing jobs/inventories must finish or be reconciled before onboarding maintenance',409)
  from maintenance import assert_quiescent
  assert_quiescent(s,'controller')
 result={'quiescent':True}
elif action=='approve':result=j.nodes.handoff.approve(v['node_id'],v['proposal'],v['expected_before_sha256'])
elif action=='groups':
 from owner_activation import Activation
 groups=Activation(j).controller_groups();result={'groups':sorted(groups)}
else:
 from owner_activation import Activation
 try:
  profile=j.nodes.handoff._stage(v['owner'],v['stage_id'])
  with j.transaction() as db:Activation(j).readiness(v,db,profile)
  result={'ready':True}
 except (ValueError,OSError) as error:result={'ready':False,'reason':str(error)[:512]}
print(json.dumps(result))
'''
    def drop():
        from payload_groups import gids
        resource.setrlimit(resource.RLIMIT_CORE,(0,0));os.setgroups(gids(meta.get('payload_storage')))
        os.setgid(meta['gid']);os.setuid(meta['uid']);os.umask(0o077)
    try:r=subprocess.run(['/usr/bin/python3','-I','-B','-c',code,str(prefix/'current'),str(state),action,str(path)],
        preexec_fn=drop,stdin=subprocess.DEVNULL,capture_output=True,timeout=30,
        env={'PATH':'/usr/bin:/bin','HOME':str(state),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1',
             'ASCEND_LAB_SERVICE_PREFIX':str(prefix)})
    except subprocess.TimeoutExpired:raise DeployError('Service participant response is unknown; resume the original operation') from None
    if r.returncode or len(r.stdout)>65536:raise DeployError('Fixed service participant operation failed; preserve the request and inspect private service state')
    try:return json.loads(r.stdout)
    except ValueError:raise DeployError('Service participant returned no valid bounded response') from None


def _authority(config):
    from lab_client import controller_url,authority_bytes
    return {'url':controller_url(config),'authority_sha256':hashlib.sha256(authority_bytes(config['ca_file'])).hexdigest() if config.get('ca_file') else None}


def packet_check(value):
    if (not isinstance(value,dict) or set(value)!={'format','flow_id','node_id','physical_id','controller','request','id'}
        or value['format']!=PACKET or value['id']!=sha({k:v for k,v in value.items() if k!='id'})
        or any(not isinstance(value[k],str) or not re.fullmatch(pattern,value[k]) for k,pattern in
               [('id','[0-9a-f]{64}'),('flow_id','[0-9a-f]{64}'),('node_id','worker-[0-9a-f]{12}'),('physical_id','[0-9a-f]{64}')])
        or not isinstance(value['controller'],dict) or set(value['controller'])!={'url','authority_sha256'}):raise DeployError('Invalid protected Node participant packet')
    from lab_client import controller_url
    controller=value['controller']
    if (not isinstance(controller['url'],str) or controller_url({'controller':controller['url']})!=controller['url']
        or controller['authority_sha256'] is not None and (not isinstance(controller['authority_sha256'],str)
            or not re.fullmatch('[0-9a-f]{64}',controller['authority_sha256']))):raise DeployError('Invalid packet controller authority')
    node_request(value['request']);return value


def node_participate(prefix,value,action='prepare'):
    deploy.require_root();packet_check(value);prefix,meta,release=deploy.load_installation(prefix)
    helper=release/Path(__file__).name
    if meta['role']!='node' or not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():raise DeployError('Use matching installed Node packet code')
    config=deploy.regular_json(Path(meta['state_dir'])/'config.json',private=True)
    if (config['node_id']!=value['node_id'] or config['physical_id']!=value['physical_id']
        or _authority(config)!=value['controller']):raise DeployError('Packet selects a different Node, physical host or controller authority')
    from node_owner_provision import provision
    return provision(prefix,value['request'],action)


def _nodes(state,spec):
    with sqlite3.connect('file:'+str(state/'jobs.sqlite3')+'?mode=ro',uri=True) as db:
        result={}
        for item in spec['nodes']:
            row=db.execute('SELECT profile,disabled FROM worker_nodes WHERE id=?',(item['node_id'],)).fetchone()
            if not row or row[1]:raise DeployError('Every requested Node must already be registered and enabled')
            profile=json.loads(row[0])
            if spec['owner'] in profile['users'] or any(x['uid']==item['uid'] for x in profile['users'].values()):
                raise DeployError('Requested owner/UID is already mapped on a selected Node')
            for role,key in [('workspace','workspace'),('cache','persistent_cache')]:
                slots=profile.get(key,{}).get('slots',[])
                if not any(s.get('filesystem')=='ext4-userquota' and str(Path(s['path']).parent)==item[role]['root'] for s in slots):
                    raise DeployError('Requested native root is not registered for that Node resource role')
            result[item['node_id']]={'physical_id':profile['physical_id'],'before_profile_sha256':sha(profile)}
        return result


def _preflight_storage(meta,state,config,spec):
    from payload_storage import validate,root_fd
    import group_quota
    if spec['owner']==config['username']:raise DeployError('Logical owner cannot alias the controller identity')
    before=meta.get('payload_storage');after=copy.deepcopy(before)
    if not isinstance(after,dict) or not after.get('owner_groups'):raise DeployError('Explicit native owner group storage is required')
    after['owner_groups'][spec['owner']]=spec['storage'];validate(after)
    with root_fd(before,meta['uid']) as fd:
        storage._unassigned(group_quota.read(fd,spec['storage']['gid']));storage._capacity(fd,spec['storage'])
    with sqlite3.connect('file:'+str(state/'identities.sqlite3')+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT disabled,password_hash,profile FROM users WHERE id=?',(spec['owner'],)).fetchone()
    if row:
        import owner_identity
        base=Path(config.get('identity_workspace_root',state/'personas'))/spec['owner']/'workspace'
        profile,marker=owner_identity.definition(spec['owner'],base,['worker-python'],spec['quota'],meta['uid']);profile['provisioning']=marker
        if row[:2]!=(1,'!') or json.loads(row[2])!=profile:raise DeployError('Existing owner differs from the requested credential-free stage')
    if any(p.exists() or p.is_symlink() for p in [state/'personas'/spec['owner']/'client.json',
        state/'owner-activations'/(spec['owner']+'.pending.json')]):raise DeployError('Existing activation material requires explicit reconciliation before onboarding')


def _packets(prefix,meta,record,directory):
    config=deploy.regular_json(Path(meta['state_dir'])/'config.json',private=True);result=[]
    for item in record['request']['nodes']:
        node=item['node_id'];body={'operation_id':sha({'flow':record['id'],'node':node})[:32],
            'owner':record['request']['owner'],'stage_id':record['stage_id'],**{k:item[k] for k in ['uid','gid','workspace','cache']}}
        packet={'format':PACKET,'flow_id':record['id'],'node_id':node,'physical_id':record['nodes'][node]['physical_id'],
            'controller':_authority(config),'request':body};packet['id']=sha(packet);path=directory/(node+'.request.json')
        if path.exists():
            if _read(path)!=packet:raise DeployError('Original Node request packet changed')
        else:_write(path,packet)
        result.append({'node_id':node,'packet_id':packet['id'],'request_file':str(path),
            'proposal_id':record['proposals'].get(node)})
    return result


def _proposal(prefix,meta,record,directory,value):
    _plan_check(value);node=value['node_id'];wanted=next((x for x in record['request']['nodes'] if x['node_id']==node),None)
    if wanted is None or value['physical_id']!=record['nodes'][node]['physical_id']:raise DeployError('Proposal is not from a requested physical Node')
    if any(value['request'][k]!=expected for k,expected in {'owner':record['request']['owner'],'stage_id':record['stage_id'],'uid':wanted['uid'],'gid':wanted['gid']}.items()):raise DeployError('Node proposal identity differs from the original request')
    for role,key in [('workspace','workspace'),('cache','persistent_cache')]:
        slot=value['after'][key]['slots'][-1]
        if (str(Path(slot['path']).parent)!=wanted[role]['root'] or slot['quota_uid']!=wanted['uid']
            or any(slot[k]!=wanted[role][k] for k in ['bytes','inodes'])):raise DeployError('Node proposal allocation differs from requested native resources')
    previous=record['proposals'].get(node);path=directory/(node+'.proposal.json')
    if previous and previous!=value['id']:raise DeployError('Only the original Node proposal may be resumed')
    if path.exists():
        if _read(path)!=value:raise DeployError('Original Node proposal file differs')
    else:_write(path,value);_checkpoint('proposal')
    _service(prefix,meta,record['id'],'approve',{'node_id':node,'proposal':value,
        'expected_before_sha256':record['nodes'][node]['before_profile_sha256']});_checkpoint('approval')
    record['proposals'][node]=value['id'];_write(prefix/JOURNAL,record);_checkpoint('approved')


def _children(prefix,record,directory,meta,config):
    storage_path=directory/'storage.json'
    plan=_read(storage_path) if storage_path.exists() else None
    if plan is not None:
        storage._validate_plan(plan)
        wanted=record['request']['storage']
        if (plan['owner']!=record['request']['owner'] or plan['stage_id']!=record['stage_id']
            or plan['after']['owner_groups'][plan['owner']]!=wanted):raise DeployError('Original storage plan differs from the participant request')
    if record['phase'] in {'held','staging','staged'}:
        if sha(meta.get('payload_storage'))!=record['before_payload_sha256'] or config['scheduler'].get('payload_storage')!=meta.get('payload_storage'):
            raise DeployError('Central storage changed before this operation')
    elif record['phase']=='storage':
        if plan is None or any(policy not in [plan['before'],plan['after']] for policy in [meta.get('payload_storage'),config['scheduler'].get('payload_storage')]):
            raise DeployError('Pending storage policy is not this original operation')
    elif plan is None or meta.get('payload_storage')!=plan['after'] or config['scheduler'].get('payload_storage')!=plan['after']:
        raise DeployError('Provisioned central storage binding changed')
    if (prefix/'payload-quota-change.json').exists():raise DeployError('An unrelated quota resize is pending')
    for journal,name in [('payload-owner-change.json','storage'),('unit-refresh-change.json','unit'),
                         ('unit-manager-change.json','manager'),('unit-start-change.json','startup')]:
        path=prefix/journal
        if path.exists() or path.is_symlink():
            expected=directory/(name+'.json')
            if not expected.exists():raise DeployError('An unrelated child provisioning transaction is pending')
            child=_read(path);actual=child.get('plan') if name=='startup' else child
            if actual!=_read(expected):raise DeployError('A child provisioning transaction differs from its original plan')
    finish_path=prefix/'owner-finish-change.json';delivery_path=prefix/'owner-delivery-change.json'
    if finish_path.exists() or finish_path.is_symlink():
        if record['phase']!='finishing' or _read(finish_path).get('request')!=_read(directory/'finish.json'):
            raise DeployError('An unrelated personal activation/delivery is pending')
    if delivery_path.exists() or delivery_path.is_symlink():
        if not finish_path.exists() or _read(delivery_path).get('plan')!=_read(finish_path).get('delivery_plan'):
            raise DeployError('An unrelated private credential delivery is pending')


def _lifecycle(prefix,meta,record,directory,bus):
    service=record['request']['service'];unit_dir=service['unit_dir']
    import unit_refresh,unit_manager,unit_start
    for name,make,apply in [('unit',lambda:unit_refresh.plan(prefix,unit_dir)['plan'],lambda p:unit_refresh.apply(prefix,p)),
                          ('manager',lambda:unit_manager.plan(prefix,unit_dir,bus=bus)['plan'],lambda p:unit_manager.apply(prefix,p,bus=bus))]:
        if unit_dir is None or name=='manager' and service['mode']!='systemd':continue
        if not record['lifecycle'].get(name):
            path=directory/(name+'.json')
            plan=_read(path) if path.exists() else make()
            if not path.exists():_write(path,plan);_checkpoint(name+'_plan')
            apply(plan);_checkpoint(name+'_applied');record['lifecycle'][name]=plan['id'];_write(prefix/JOURNAL,record)
    _publish_gate(prefix,record,'maintenance',True);_checkpoint('service_allowed')
    if service['mode']=='process':
        status=deploy.start(prefix)
        if not status['ready']:return False
    else:
        path=directory/'startup.json';plan=_read(path) if path.exists() else unit_start.plan(prefix,unit_dir,bus=bus)['plan']
        if not path.exists():_write(path,plan);_checkpoint('startup_plan')
        if unit_start.apply(prefix,plan,bus=bus)['state']!='ready':return False
    _checkpoint('service_response')
    groups=_service(prefix,meta,record['id'],'groups',{})['groups']
    if record['request']['storage']['gid'] not in groups:raise DeployError('Live controller has not loaded the new owner storage group')
    return True


def _result(record,directory,packets=None,state=None,reason=None):
    return {'format':FORMAT,'id':record['id'],'operation_id':record['request']['operation_id'],'owner':record['request']['owner'],
        'state':state or record['phase'],'stage_id':record['stage_id'],'node_requests':packets or [],'reason':reason,
        'credentials_in_output':False,'historical_receipt_only':False}


def _record_check(record,identifier,spec,context):
    fields={'format','id','request','binding','directories','nodes','phase','stage_id','storage_plan_id','lifecycle',
        'proposals','before_payload_sha256','service_was_running'}
    if (set(record)!=fields or record['format']!=FORMAT or record['id']!=identifier or record['request']!=spec or record['binding']!=context
        or record['phase'] not in {'held','staging','staged','storage','lifecycle','nodes','finishing'}
        or type(record['service_was_running']) is not bool or not isinstance(record['nodes'],dict)
        or set(record['nodes'])!={x['node_id'] for x in spec['nodes']} or not isinstance(record['proposals'],dict)
        or set(record['proposals'])-set(record['nodes']) or not isinstance(record['lifecycle'],dict)
        or set(record['lifecycle'])-{'unit','manager'}):raise DeployError('Central provisioning installation or phase changed')
    for value in [record['before_payload_sha256'],*record['proposals'].values(),*record['lifecycle'].values()]:
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise DeployError('Invalid durable provisioning digest')
    for item in record['nodes'].values():
        if (not isinstance(item,dict) or set(item)!={'physical_id','before_profile_sha256'}
            or any(not isinstance(x,str) or not re.fullmatch('[0-9a-f]{64}',x) for x in item.values())):raise DeployError('Invalid requested Node snapshot')
    for key,needed in [('stage_id',record['phase'] not in {'held','staging'}),('storage_plan_id',record['phase'] in {'storage','lifecycle','nodes','finishing'})]:
        if needed:
            if not isinstance(record[key],str) or not re.fullmatch('[0-9a-f]{64}',record[key]):raise DeployError('Missing original provisioning stage/resource plan')
        elif record[key] is not None:raise DeployError('Unexpected provisioning stage/resource plan')


def _receipt(saved,identifier,spec):
    if (not isinstance(saved,dict) or set(saved)!={'id','request','result'} or saved['id']!=identifier or saved['request']!=spec
        or not isinstance(saved['result'],dict)):raise DeployError('Central historical request differs')
    result=saved['result'];base={'format','id','operation_id','owner','state','stage_id','node_requests','reason','credentials_in_output','historical_receipt_only'}
    expected=base|({'activation_id','finish_id'} if result.get('state')=='delivered' else set())
    if (set(result)!=expected or result['format']!=FORMAT or result['id']!=identifier or result['operation_id']!=spec['operation_id']
        or result['owner']!=spec['owner'] or result['state'] not in {'delivered','aborted'} or result['credentials_in_output'] is not False
        or result['historical_receipt_only'] is not False or result['node_requests']!=[] or result['reason'] is not None):raise DeployError('Central historical result differs')
    if result['state']=='delivered':
        for key,size in [('stage_id',64),('activation_id',32),('finish_id',64)]:
            if not isinstance(result[key],str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}',result[key]):raise DeployError('Historical activation identity differs')
    elif result['stage_id'] is not None:raise DeployError('Aborted initial operation unexpectedly staged an owner')
    return result


def provision(prefix,value,proposals=(),local_nodes=None,abort=False,bus=None):
    deploy.require_root();spec=request(value);prefix,meta,release=deploy.load_installation(prefix);state=Path(meta['state_dir'])
    helper=release/Path(__file__).name
    if meta['role']!='controller' or not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():raise DeployError('Use matching installed controller provisioning code')
    identifier=sha({'prefix':str(prefix),'operation_id':spec['operation_id']});history=prefix/HISTORY;directory=history/identifier
    journal=prefix/JOURNAL;receipt=directory/'receipt.json';local_nodes={} if local_nodes is None else dict(local_nodes)
    if set(local_nodes)-{x['node_id'] for x in spec['nodes']}:raise DeployError('Local mapping contains an unrequested Node')
    fd=os.open(prefix/'.owner-provision.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);token=None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_nlink!=1:raise DeployError('Unsafe Root owner provisioning lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DeployError('Another central owner participant is running') from None
        _budget(history);record=_read(journal) if journal.exists() or journal.is_symlink() else None
        if record is not None and (record.get('id')!=identifier or record.get('request')!=spec):raise DeployError('Resume only the original central provisioning request')
        existing_gate=_gate(prefix,state)
        if existing_gate is not None and (record is None or existing_gate['id']!=record['id']):raise DeployError('An orphan or unrelated admission gate exists')
        token=_ROOT.set(str(prefix))
        prefix,meta,release=deploy.verify(prefix,quota_recovery=record is not None,unit_recovery=record is not None,
            manager_recovery=record is not None,startup_recovery=record is not None,delivery_recovery=record is not None,finish_recovery=record is not None)
        config=deploy.regular_json(state/'config.json',private=True);context=_binding(prefix,meta,state,config)
        if receipt.exists():
            saved=_read(receipt)
            historical=_receipt(saved,identifier,spec)
            if record is not None:
                _record_check(record,identifier,spec,context);_children(prefix,record,directory,meta,config)
                (prefix/GATE).unlink(missing_ok=True);files.sync(prefix);_checkpoint('gate_removed')
                journal.unlink();files.sync(prefix);_checkpoint('published')
            return {**historical,'historical_receipt_only':True}
        if record is None:
            if abort:raise DeployError('No pending central onboarding to abort')
            if spec['storage']['gid']==meta['gid'] or spec['delivery']['uid']==meta['uid'] or spec['delivery']['gid']==meta['gid']:raise DeployError('Owner storage and personal delivery must not alias the service identity')
            if spec['owner'] in meta.get('payload_storage',{}).get('owner_groups',{}):raise DeployError('A fresh onboarding cannot adopt existing owner storage')
            _preflight_storage(meta,state,config,spec)
            parent,spool,dirs=delivery._directories(spec['delivery'])
            try:
                if delivery._exists(parent,spec['delivery']['name']):raise DeployError('Personal onboarding destination must be new')
            finally:os.close(parent);os.close(spool)
            nodes=_nodes(state,spec);_budget(history,creating=True)
            from jobs import read_json,identity_alive
            running=deploy.process_status(prefix)['running'];ready=read_json(state/'server-ready.json') or {}
            if running and (not identity_alive(ready) or CAPABILITY not in ready.get('capabilities',[])):
                raise DeployError('Reload the existing controller with owner provisioning admission support before maintenance')
            if not directory.exists():directory.mkdir(mode=0o700);files.sync(history)
            record={'format':FORMAT,'id':identifier,'request':spec,'binding':context,'directories':dirs,'nodes':nodes,
                'phase':'held','stage_id':None,'storage_plan_id':None,'lifecycle':{},'proposals':{},
                'before_payload_sha256':sha(meta.get('payload_storage')),'service_was_running':running}
            _write(journal,record);_checkpoint('journal')
        _record_check(record,identifier,spec,context)
        _children(prefix,record,directory,meta,config)
        if abort:
            if record['phase']!='held':raise DeployError('This onboarding has durable identity/resources; retain it and reconcile the original operation')
            if record['service_was_running'] and spec['service']['mode']=='process':
                _publish_gate(prefix,record,'maintenance',True)
                if not deploy.start(prefix)['ready']:return _result(record,directory,reason='Waiting for original controller restoration before abort')
            result=_result(record,directory,state='aborted');_write(receipt,{'id':identifier,'request':spec,'result':result})
            (prefix/GATE).unlink(missing_ok=True);files.sync(prefix);journal.unlink();files.sync(prefix)
            return result
        if record['phase'] in {'held','staging','staged','storage','lifecycle'}:
            _publish_gate(prefix,record,'maintenance',record['phase']=='lifecycle');_checkpoint('held')
        else:_publish_gate(prefix,record,'open',True)
        if record['phase']=='held':
            _service(prefix,meta,identifier,'quiescent',{})
            if spec['service']['mode']=='process':
                saved=deploy.regular_json(state/'service-process.json',private=True) if (state/'service-process.json').exists() else {}
                if saved.get('manager_invocation_id'):raise DeployError('Use the systemd lifecycle for this managed service')
                deploy.stop(prefix)
            else:
                import unit_manager
                bus=bus or __import__('unit_start').Bus();unit_manager.inactive(bus.unit('ascend-lab-controller.service'))
                if deploy.process_status(prefix)['running']:raise DeployError('Systemd onboarding requires an initially stopped managed service')
            _checkpoint('stopped');record['phase']='staging';_write(journal,record);_checkpoint('stage_intent')
        if record['phase']=='staging':
            stage=_service(prefix,meta,identifier,'stage',{'owner':spec['owner'],'quota':spec['quota']})
            record.update(stage_id=stage['stage_id'],phase='staged');_write(journal,record);_checkpoint('staged')
        if record['phase']=='staged':
            path=directory/'storage.json';plan=_read(path) if path.exists() else storage.plan(prefix,spec['owner'],**{'gid':spec['storage']['gid'],'byte_hard':spec['storage']['bytes'],'inode_hard':spec['storage']['inodes']})['plan']
            if not path.exists():_write(path,plan);_checkpoint('storage_plan')
            record.update(storage_plan_id=plan['id'],phase='storage');_write(journal,record)
        if record['phase']=='storage':
            plan=_read(directory/'storage.json')
            if plan['id']!=record['storage_plan_id']:raise DeployError('Original central storage plan changed')
            storage.apply(prefix,plan);_checkpoint('storage_applied')
            record['phase']='lifecycle';_write(journal,record);_checkpoint('storage_saved')
        prefix,meta,release=deploy.verify(prefix,owner_recovery=True,unit_recovery=True,manager_recovery=True,
            startup_recovery=True,delivery_recovery=True,finish_recovery=True)
        config=deploy.regular_json(state/'config.json',private=True)
        if record['phase'] in {'nodes','finishing'} and spec['service']['mode']=='process':
            status=deploy.process_status(prefix)
            if not status['running']:
                status=deploy.start(prefix);_checkpoint('service_recovered')
            if not status['ready']:return _result(record,directory,reason='Waiting for original controller process readiness')
        if record['phase']=='lifecycle':
            if not _lifecycle(prefix,meta,record,directory,bus):return _result(record,directory,reason='Waiting for original controller startup and required Node observations')
            record['phase']='nodes';_write(journal,record);_publish_gate(prefix,record,'open',True);_checkpoint('service_ready')
        if record['phase']=='nodes':
            packets=_packets(prefix,meta,record,directory);_checkpoint('packets')
            for proposal in proposals:_proposal(prefix,meta,record,directory,proposal)
            for node,node_prefix in local_nodes.items():
                packet=_read(directory/(node+'.request.json'));prepared=node_participate(node_prefix,packet)
                if prepared['proposal_path'] is None:raise DeployError('Local Node participant did not prepare a proposal')
                proposal=_read(prepared['proposal_path']);_proposal(prefix,meta,record,directory,proposal)
                node_participate(node_prefix,packet,'complete');deploy.start(node_prefix);_checkpoint('local_node')
            packets=_packets(prefix,meta,record,directory)
            if len(record['proposals'])!=len(spec['nodes']):return _result(record,directory,packets,reason='Waiting for protected Node proposals and independent central approval')
            activation={'owner':spec['owner'],'stage_id':record['stage_id'],
                'required_nodes':[{'node_id':item['node_id'],'plan_id':record['proposals'][item['node_id']]} for item in spec['nodes']]}
            ready=_service(prefix,meta,identifier,'ready',activation)
            if not ready['ready']:return _result(record,directory,packets,reason=ready['reason'])
            parent,spool,dirs=delivery._directories(spec['delivery'])
            try:
                if dirs!=record['directories']:raise DeployError('Original personal delivery directories changed')
            finally:os.close(parent);os.close(spool)
            finish_spec={'operation_id':sha({'flow':identifier,'finish':True})[:32],'activation':activation,'delivery':spec['delivery']}
            _write(directory/'finish.json',finish_spec);record['phase']='finishing';_write(journal,record);_checkpoint('finish_intent')
        from owner_finish import finish
        result=finish(prefix,_read(directory/'finish.json'));_checkpoint('finished')
        if result['state']!='delivered':raise DeployError('Original final delivery did not complete')
        result={**_result(record,directory,state='delivered'),'activation_id':result['activation_id'],'finish_id':result['id']}
        _write(receipt,{'id':identifier,'request':spec,'result':result});_checkpoint('receipt')
        (prefix/GATE).unlink(missing_ok=True);files.sync(prefix);_checkpoint('gate_removed');journal.unlink();files.sync(prefix);_checkpoint('published')
        return result
    finally:
        if token is not None:_ROOT.reset(token)
        os.close(fd)

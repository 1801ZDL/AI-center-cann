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
    if (not isinstance(value,dict) or set(value)-{'device_probes'}!={'operation_id','owner','quota','storage','nodes','service','delivery'}
        or not isinstance(value['operation_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['operation_id'])
        or not isinstance(value['owner'],str) or not IDENTIFIER.fullmatch(value['owner'])):raise DeployError('Provision requires one explicit operation, owner, quota, storage, Nodes, service and delivery')
    profile,_=owner_identity.definition(value['owner'],'unused',['worker-python'],value['quota'],1000)
    item=value['storage']
    if (not isinstance(item,dict) or set(item)!={'gid','bytes','inodes'} or type(item['gid']) is not int or not 1000<=item['gid']<2**31
        or type(item['bytes']) is not int or not 1024**2<=item['bytes']<=1024**4
        or type(item['inodes']) is not int or not 128<=item['inodes']<=10**7):raise DeployError('Invalid finite central owner storage request')
    if sum(profile['quota'][k] for k in ['input_storage_mib','archive_storage_mib'])*1024**2+65536>item['bytes']:
        raise DeployError('Central native storage cannot cover the owner budgets and metadata')
    if (profile['quota']['npu']>2 or profile['quota']['memory_mib']<256 or profile['quota']['archive_storage_mib']<10
        or any(profile['quota'][k]<1 for k in ['cpu','max_running','max_queued','input_storage_mib'])):
        raise DeployError('This onboarding requires runnable managed quotas and at most two NPU slots per owner')
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
    if 'device_probes' in value:
        plans=value['device_probes'];selected=set();count=0
        if not profile['quota']['npu'] or not isinstance(plans,list) or len(plans)!=len(nodes):
            raise DeployError('Automatic probes require explicit NPU quotas and one selection per required Node')
        for plan in plans:
            if (not isinstance(plan,dict) or set(plan)!={'node_id','device_ids','memory_mib'}
                or not isinstance(plan['node_id'],str) or plan['node_id'] not in seen or plan['node_id'] in selected or not isinstance(plan['device_ids'],list)
                or not profile['quota']['npu']<=len(plan['device_ids'])<=64
                or any(type(d) is not int or not 0<=d<256 for d in plan['device_ids'])
                or len(set(plan['device_ids']))!=len(plan['device_ids'])
                or type(plan['memory_mib']) is not int or not 256<=plan['memory_mib']<=min(65536,profile['quota']['memory_mib'])):
                raise DeployError('Select each required Node and at least its NPU quota of unique physical cards with finite memory')
            selected.add(plan['node_id']);count+=len(plan['device_ids'])
        if count>64:raise DeployError('An automatic onboarding plan supports at most 64 explicit card checks')
        from io_policy import MIB,META
        if profile['quota']['archive_storage_mib']*MIB<(count+1)*(2*MIB+META):
            raise DeployError('Archive budget must cover all fixed probes and one subsequent minimum managed task')
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
    if action not in {'stage','quiescent','approve','ready','groups','probe','probe_adoption_status','probe_adopt'}:raise DeployError('Unknown fixed service participant operation')
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
elif action=='probe':
 from owner_device_probe import advance
 result=advance(j,v)
elif action in {'probe_adoption_status','probe_adopt'}:
 from owner_probe_adoption import adoption_status,adopt
 result=adoption_status(j,v) if action=='probe_adoption_status' else adopt(j,v)
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
            if spec['quota'].get('npu',0):
                if (len(profile.get('npu',{}).get('device_ids',[]))<spec['quota']['npu']
                    or 'npu_access_probe' not in profile['templates']):
                    raise DeployError('NPU onboarding requires explicitly enrolled devices and the fixed access probe on each selected Node')
                selected=next((p for p in spec.get('device_probes',[]) if p['node_id']==item['node_id']),None)
                if selected and not set(selected['device_ids'])<=set(profile['npu']['device_ids']):
                    raise DeployError('Automatic probe selection contains a device outside the registered Node contract')
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
    if 'device_probes' in record['request'] and record['phase']=='finishing':_probe_snapshot(record,directory)
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



def _base_probe_specs(record):
    for plan in sorted(record['request']['device_probes'],key=lambda v:v['node_id']):
        for device in sorted(plan['device_ids']):
            node=plan['node_id']
            yield {'operation_id':sha({'flow':record['id'],'node':node,'device':device})[:32],
                'owner':record['request']['owner'],'stage_id':record['stage_id'],'node_id':node,
                'plan_id':record['proposals'][node],'device_id':device,'memory_mib':plan['memory_mib']}



def _check_adoptions(record,base,rows):
    if not isinstance(rows,list) or len(rows)>8:raise DeployError('Invalid bounded explicit probe adoption history')
    current=base;jobs=set()
    from owner_device_probe import request as probe_request
    for revision,item in enumerate(rows,1):
        if (not isinstance(item,dict) or set(item)!={'flow_id','parent_sha256','revision','previous_request','request','job_id','id'}
            or item['flow_id']!=record['id'] or item['parent_sha256']!=sha(record['request']) or type(item['revision']) is not int
            or item['revision']!=revision or item['previous_request']!=current
            or item['id']!=sha({k:v for k,v in item.items() if k!='id'})
            or not isinstance(item['job_id'],str) or not re.fullmatch('job-[0-9a-f]{12}',item['job_id']) or item['job_id'] in jobs):
            raise DeployError('Original probe adoption history changed')
        try:new=probe_request(item['request'])
        except ValueError as error:raise DeployError(str(error)) from None
        if new['operation_id']==current['operation_id'] or any(new[k]!=current[k] for k in current if k!='operation_id'):
            raise DeployError('An adopted probe cannot change its original resource or identity contract')
        current=new;jobs.add(item['job_id'])
    return current


def _adoption_files(directory,base):
    prefix='probe-adopt-'+base['node_id']+'-'+str(base['device_id'])+'-'
    paths=list(directory.glob(prefix+'*.json'));ordered={}
    for path in paths:
        suffix=path.name[len(prefix):-5]
        if not re.fullmatch('[1-8]',suffix):raise DeployError('Unknown or excessive original probe adoption cache entry')
        ordered[int(suffix)]=path
    if set(ordered)!=set(range(1,len(ordered)+1)):raise DeployError('An original probe adoption cache entry is missing')
    return [ordered[i] for i in sorted(ordered)]


def _probe_specs(record,directory=None):
    for base in _base_probe_specs(record):
        rows=[_read(p) for p in _adoption_files(directory,base)] if directory is not None else []
        yield _check_adoptions(record,base,rows)


def _sync_adoptions(prefix,meta,record,directory,requested):
    bases=list(_base_probe_specs(record));pairs={(b['node_id'],b['device_id']) for b in bases}
    if set(requested)-pairs:raise DeployError('Adoption selected a card outside the original plan')
    for base in bases:
        scope={'flow_id':record['id'],'parent_sha256':sha(record['request']),'base_request':base}
        response=_service(prefix,meta,record['id'],'probe_adoption_status',scope)
        if not isinstance(response,dict) or set(response)!={'records'}:raise DeployError('Invalid adoption status response')
        rows=response['records'];current=_check_adoptions(record,base,rows)
        old_paths=_adoption_files(directory,base);cached=[_read(p) for p in old_paths]
        _check_adoptions(record,base,cached)
        if len(cached)>len(rows) or cached!=rows[:len(cached)]:raise DeployError('Central adoption history is older than the original Root cache')
        selected=requested.get((base['node_id'],base['device_id']))
        if selected is not None:
            response=_service(prefix,meta,record['id'],'probe_adopt',{**scope,'expected_authorization_id':sha({'request':current}),'job_id':selected})
            _checkpoint('probe_adoption_response')
            if (not isinstance(response,dict) or set(response)!={'records','historical_receipt_only'}
                or type(response['historical_receipt_only']) is not bool):raise DeployError('Invalid completed-probe adoption response')
            rows=response['records'];_check_adoptions(record,base,rows)
            if len(cached)>len(rows) or cached!=rows[:len(cached)]:raise DeployError('An adoption response changed the original Root history')
        for item in rows[len(cached):]:
            path=directory/('probe-adopt-'+base['node_id']+'-'+str(base['device_id'])+'-'+str(item['revision'])+'.json')
            _write(path,item);_checkpoint('probe_adoption_saved')


def _snapshot_path(record,directory):
    current=[sha({'request':v}) for v in _probe_specs(record,directory)]
    original=[sha({'request':v}) for v in _base_probe_specs(record)]
    return directory/('probes.json' if current==original else 'probes-'+sha(current)+'.json')


def _probe_entries(spec,entries,complete=True):
    pairs=sorted((p['node_id'],d) for p in spec['device_probes'] for d in p['device_ids'])
    if not isinstance(entries,list) or len(entries)!=(len(pairs) if complete else 0):raise DeployError('Incomplete original probe evidence')
    jobs=set()
    for entry,pair in zip(entries,pairs):
        if (not isinstance(entry,dict) or set(entry)!={'authorization_id','node_id','device_id','job_id','state','status','reason'}
            or (entry['node_id'],entry['device_id'])!=pair or entry['status']!='verified' or entry['state']!='succeeded'
            or entry['reason'] is not None or not isinstance(entry['authorization_id'],str)
            or not re.fullmatch('[0-9a-f]{64}',entry['authorization_id']) or not isinstance(entry['job_id'],str)
            or not re.fullmatch('job-[0-9a-f]{12}',entry['job_id']) or entry['job_id'] in jobs):
            raise DeployError('Invalid or duplicate original fixed-probe evidence')
        jobs.add(entry['job_id'])


def _probe_paths(directory,spec):
    name='probe-'+spec['node_id']+'-'+str(spec['device_id'])+'-'+spec['operation_id']
    return directory/(name+'.request.json'),directory/(name+'.binding.json')


def _probe_snapshot(record,directory):
    value=_read(_snapshot_path(record,directory))
    if (set(value)!={'format','flow_id','request_sha256','probes','id'} or value['format']!='ascend-lab-owner-probes/v1'
        or value['flow_id']!=record['id'] or value['request_sha256']!=sha(record['request'])
        or value['id']!=sha({k:v for k,v in value.items() if k!='id'})):
        raise DeployError('Original probe completion snapshot changed')
    _probe_entries(record['request'],value['probes'])
    for spec,result in zip(_probe_specs(record,directory),value['probes']):
        intent,binding=_probe_paths(directory,spec)
        expected={'authorization_id':sha({'request':spec}),'node_id':spec['node_id'],
                  'device_id':spec['device_id'],'job_id':result['job_id']}
        if _read(intent)!=spec or _read(binding)!=expected or result['authorization_id']!=expected['authorization_id']:
            raise DeployError('Original probe snapshot does not match its durable request/job binding')
    return value


def _drive_probes(prefix,meta,record,directory,adoptions=None):
    """Submit at most one unfinished fixed probe; all retries keep its operation."""
    _sync_adoptions(prefix,meta,record,directory,adoptions or {})
    results=[]
    for spec in _probe_specs(record,directory):
        intent,binding=_probe_paths(directory,spec)
        if intent.exists() or intent.is_symlink():
            if _read(intent)!=spec:raise DeployError('Original fixed-probe request changed')
        else:_write(intent,spec);_checkpoint('probe_intent')
        result=_service(prefix,meta,record['id'],'probe',spec);_checkpoint('probe_response')
        from jobs import ACTIVE,TERMINAL
        if (not isinstance(result,dict) or set(result)!={'authorization_id','node_id','device_id','job_id','state','status','reason'}
            or result['authorization_id']!=sha({'request':spec}) or result['node_id']!=spec['node_id']
            or type(result['device_id']) is not int or result['device_id']!=spec['device_id']
            or not isinstance(result['status'],str) or result['status'] not in {'waiting','verified','attention'}
            or result['state'] is not None and (not isinstance(result['state'],str) or result['state'] not in ACTIVE|TERMINAL|{'queued'})
            or result['reason'] is not None and (not isinstance(result['reason'],str) or len(result['reason'])>512)
            or result['job_id'] is not None and (not isinstance(result['job_id'],str) or not re.fullmatch('job-[0-9a-f]{12}',result['job_id']))
            or result['status']=='verified' and (result['state']!='succeeded' or result['job_id'] is None or result['reason'] is not None)
            or result['status']=='waiting' and (result['job_id'] is None or result['state'] not in {'queued','reserved','running','cancelling'})):
            raise DeployError('Fixed service probe returned inconsistent bounded progress')
        if binding.exists() or binding.is_symlink():
            original=_read(binding)
            if original!={k:result[k] for k in ['authorization_id','node_id','device_id','job_id']}:
                raise DeployError('Original fixed-probe job binding differs; never replace an uncertain job')
        elif result['job_id'] is not None:
            _write(binding,{k:result[k] for k in ['authorization_id','node_id','device_id','job_id']});_checkpoint('probe_bound')
        results.append(result)
        if result['status']!='verified':return {'ready':False,'probes':results,'reason':result['reason'] or 'Waiting for the original fixed probe'}
        _checkpoint('probe_verified')
    _probe_entries(record['request'],results)
    snapshot={'format':'ascend-lab-owner-probes/v1','flow_id':record['id'],'request_sha256':sha(record['request']),'probes':results}
    snapshot['id']=sha(snapshot);path=_snapshot_path(record,directory)
    if path.exists() or path.is_symlink():
        if _probe_snapshot(record,directory)!=snapshot:raise DeployError('Original probe completion snapshot differs')
    else:_write(path,snapshot);_checkpoint('probes_ready')
    return {'ready':True,'probes':results,'reason':None}


def watch(operation,seconds):
    """Bounded CLI observation of one original operation; never changes its spec."""
    import time
    if type(seconds) is not int or not 1<=seconds<=900:raise DeployError('Wait must be 1..900 seconds')
    deadline=time.monotonic()+seconds
    while True:
        result=operation()
        if result['state'] in {'delivered','aborted'} or any(p['status']=='attention' for p in result.get('device_probes',[])):
            return {**result,'wait':{'seconds':seconds,'expired':False}}
        remaining=deadline-time.monotonic()
        if remaining<=0:return {**result,'wait':{'seconds':seconds,'expired':True}}
        time.sleep(min(2,remaining))


def _result(record,directory,packets=None,state=None,reason=None):
    return {'format':FORMAT,'id':record['id'],'operation_id':record['request']['operation_id'],'owner':record['request']['owner'],
        'state':state or record['phase'],'stage_id':record['stage_id'],'node_requests':packets or [],'reason':reason,
        'credentials_in_output':False,'historical_receipt_only':False,
        **({'device_probes':[]} if 'device_probes' in record['request'] else {})}


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
    expected=base|({'activation_id','finish_id'} if result.get('state')=='delivered' else set())|({'device_probes'} if 'device_probes' in spec else set())
    if (set(result)!=expected or result['format']!=FORMAT or result['id']!=identifier or result['operation_id']!=spec['operation_id']
        or result['owner']!=spec['owner'] or result['state'] not in {'delivered','aborted'} or result['credentials_in_output'] is not False
        or result['historical_receipt_only'] is not False or result['node_requests']!=[] or result['reason'] is not None):raise DeployError('Central historical result differs')
    if result['state']=='delivered':
        for key,size in [('stage_id',64),('activation_id',32),('finish_id',64)]:
            if not isinstance(result[key],str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}',result[key]):raise DeployError('Historical activation identity differs')
    elif result['stage_id'] is not None:raise DeployError('Aborted initial operation unexpectedly staged an owner')
    if 'device_probes' in spec:
        _probe_entries(spec,result['device_probes'],complete=result['state']=='delivered')
    return result


def provision(prefix,value,proposals=(),local_nodes=None,abort=False,bus=None,probe_adoptions=None):
    deploy.require_root();spec=request(value);prefix,meta,release=deploy.load_installation(prefix);state=Path(meta['state_dir'])
    helper=release/Path(__file__).name
    if meta['role']!='controller' or not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():raise DeployError('Use matching installed controller provisioning code')
    identifier=sha({'prefix':str(prefix),'operation_id':spec['operation_id']});history=prefix/HISTORY;directory=history/identifier
    journal=prefix/JOURNAL;receipt=directory/'receipt.json';local_nodes={} if local_nodes is None else dict(local_nodes)
    if set(local_nodes)-{x['node_id'] for x in spec['nodes']}:raise DeployError('Local mapping contains an unrequested Node')
    probe_adoptions={} if probe_adoptions is None else dict(probe_adoptions)
    selected={(p['node_id'],d) for p in spec.get('device_probes',[]) for d in p['device_ids']}
    if (set(probe_adoptions)-selected or any(not isinstance(j,str) or not re.fullmatch('job-[0-9a-f]{12}',j) for j in probe_adoptions.values())
        or abort and probe_adoptions):raise DeployError('Adopt only explicit successful jobs for selected cards, separately from abort')
    fd=os.open(prefix/'.owner-provision.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);token=None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_nlink!=1:raise DeployError('Unsafe Root owner provisioning lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DeployError('Another central owner participant is running') from None
        _budget(history);record=_read(journal) if journal.exists() or journal.is_symlink() else None
        if record is not None and (record.get('id')!=identifier or record.get('request')!=spec):raise DeployError('Resume only the original central provisioning request')
        if probe_adoptions and (record is None or record.get('phase')!='nodes'):
            raise DeployError('Completed probe adoption is available only while the original flow waits in its Node phase')
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
            if 'device_probes' in spec:
                progress=_drive_probes(prefix,meta,record,directory,probe_adoptions)
                if not progress['ready']:
                    return {**_result(record,directory,packets,reason=progress['reason']),'device_probes':progress['probes']}
            ready=_service(prefix,meta,identifier,'ready',activation)
            if not ready['ready']:
                return {**_result(record,directory,packets,reason=ready['reason']),
                    **({'device_probes':progress['probes']} if 'device_probes' in spec else {})}
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
        if 'device_probes' in spec:result['device_probes']=_probe_snapshot(record,directory)['probes']
        _write(receipt,{'id':identifier,'request':spec,'result':result});_checkpoint('receipt')
        (prefix/GATE).unlink(missing_ok=True);files.sync(prefix);_checkpoint('gate_removed');journal.unlink();files.sync(prefix);_checkpoint('published')
        return result
    finally:
        if token is not None:_ROOT.reset(token)
        os.close(fd)

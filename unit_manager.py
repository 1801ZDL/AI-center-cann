"""Typed system-bus readback and offline reload/load transactions.

Reload/LoadUnit do not start services. There is deliberately no StartUnit,
RestartUnit, enable, process signalling, shell, remote bus or user bus here.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shlex
import stat
import subprocess
import tempfile
import uuid

import deployment as deploy
from deployment import DeployError
from jobs import identity,identity_alive,read_json
from payload_quota_admin import fingerprint,sync,write
from state_lock import state_lock
import unit_refresh as files

BUS='org.freedesktop.systemd1'
MANAGER='/org/freedesktop/systemd1'
INTERFACE=BUS+'.Manager'
JOURNAL='unit-manager-change.json'
HISTORY='unit-manager-history'
FORMAT='ascend-lab-unit-manager-plan/v1'
UNIT={'Id':'s','Names':'as','LoadState':'s','ActiveState':'s','SubState':'s','FragmentPath':'s','SourcePath':'s',
      'DropInPaths':'as','NeedDaemonReload':'b','Job':'(uo)','RequiresMountsFor':'as','Transient':'b','ControlGroup':'s','InvocationID':'ay'}
SERVICE={'MainPID':'u','ControlPID':'u','Type':'s','User':'s','Group':'s','SupplementaryGroups':'as','WorkingDirectory':'s',
    'Environment':'as','EnvironmentFiles':'a(sb)','PassEnvironment':'as','UnsetEnvironment':'as',
    'ExecStart':'a(sasbttttuii)','ExecStartPre':'a(sasbttttuii)','ExecStartPost':'a(sasbttttuii)',
    'ExecStop':'a(sasbttttuii)','ExecStopPost':'a(sasbttttuii)','ExecReload':'a(sasbttttuii)',
    'ReadWritePaths':'as','NoNewPrivileges':'b','ProtectSystem':'s','ProtectHome':'s','PrivateTmp':'b',
    'ProtectKernelTunables':'b','ProtectKernelModules':'b','ProtectControlGroups':'b','RestrictSUIDSGID':'b',
    'LockPersonality':'b','CapabilityBoundingSet':'t','RestrictAddressFamilies':'(bas)','UMask':'u',
    'RootDirectory':'s','RootImage':'s','DynamicUser':'b','PrivateUsers':'b','Restart':'s','RestartUSec':'t','TimeoutStopUSec':'t'}


class MissingUnit(DeployError):pass


def _checkpoint(stage):pass


def _typed(signature,value):
    if signature=='s':return isinstance(value,str)
    if signature=='o':return isinstance(value,str) and re.fullmatch(r'/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?',value) is not None
    if signature=='b':return type(value) is bool
    if signature in {'u','t','i'}:
        lower,upper=(-(2**31),2**31) if signature=='i' else (0,2**(32 if signature=='u' else 64))
        return type(value) is int and lower<=value<upper
    if not isinstance(value,list):return False
    if signature=='as':return all(isinstance(x,str) for x in value)
    if signature=='ay':return all(type(x) is int and 0<=x<=255 for x in value)
    if signature in {'(uo)','(bas)'}:
        fields=['u','o'] if signature=='(uo)' else ['b','as']
        return len(value)==2 and all(_typed(kind,x) for kind,x in zip(fields,value))
    fields={'a(sb)':['s','b'],'a(sasbttttuii)':['s','as','b','t','t','t','t','u','i','i']}.get(signature)
    return fields is not None and all(isinstance(row,list) and len(row)==len(fields) and all(_typed(kind,x) for kind,x in zip(fields,row)) for row in value)


def _bounded(argv):
    """No inherited DBUS address/environment; cap output while the child writes."""
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_FSIZE,(256*1024,256*1024))
    with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
        try:
            result=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=out,stderr=err,timeout=10,preexec_fn=limits,
                env={'PATH':'/usr/bin:/bin','LANG':'C','SYSTEMD_PAGER':'','SYSTEMD_COLORS':'0'})
        except subprocess.TimeoutExpired:raise DeployError('System-bus response timed out; preserve the original operation for reconciliation') from None
        if out.tell()>=256*1024 or err.tell()>=256*1024:raise DeployError('System-bus response exceeds its bounded output contract')
        out.seek(0);err.seek(0);stdout=out.read();stderr=err.read()
    if result.returncode:
        if any(word in stderr for word in [b'NoSuchUnit',b'not loaded',b'No such unit']):raise MissingUnit('Selected unit is not loaded in this manager')
        raise DeployError('System-bus operation rejected; raw properties/output are not logged')
    try:return [json.loads(line) for line in stdout.decode().splitlines() if line]
    except (ValueError,UnicodeError):raise DeployError('System-bus returned malformed typed JSON') from None


class Bus:
    def __init__(self):self.owner=None

    def invoke(self,args):
        path=Path('/usr/bin/busctl');files._directory(path.parent);info=path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise DeployError('busctl must be a protected Root-owned system binary')
        return _bounded([str(path),'--system','--json=short','--auto-start=no','--allow-interactive-authorization=no','--timeout=8s',*args])

    def call(self,destination,path,interface,method,signature='',args=(),reply=''):
        result=self.invoke(['call',destination,path,interface,method,*([signature,*args] if signature else [])])
        if not reply:
            if result and result!=[{'type':'','data':[]}]:raise DeployError('Unexpected system-bus method return')
            return None
        if len(result)!=1 or not isinstance(result[0],dict) or set(result[0])!={'type','data'} or result[0]['type']!=reply or not isinstance(result[0]['data'],list) or len(result[0]['data'])!=1:
            raise DeployError('System-bus method return signature differs')
        value=result[0]['data'][0]
        if not _typed(reply,value):raise DeployError('Malformed system-bus method value')
        return value

    def properties(self,path,interface,schema):
        result=self.invoke(['get-property',self.owner or BUS,path,interface,*schema])
        if len(result)!=len(schema):raise DeployError('System-bus property set is incomplete')
        values={}
        for (key,signature),item in zip(schema.items(),result):
            if not isinstance(item,dict) or set(item)!={'type','data'} or item['type']!=signature:raise DeployError('System-bus property signature differs: '+key)
            value=item['data']
            if not _typed(signature,value):raise DeployError('System-bus property value is malformed: '+key)
            values[key]=value
        return values

    def anchor(self):
        dbus='org.freedesktop.DBus';obj='/org/freedesktop/DBus'
        owner=self.call(dbus,obj,dbus,'GetNameOwner','s',[BUS],'s')
        if not isinstance(owner,str) or not re.fullmatch(r':[0-9]+\.[0-9]+',owner):raise DeployError('Manager has no unique system-bus owner')
        if self.owner and self.owner!=owner:raise DeployError('Systemd manager owner changed; do not continue an old transaction')
        self.owner=owner
        uid=self.call(dbus,obj,dbus,'GetConnectionUnixUser','s',[owner],'u');pid=self.call(dbus,obj,dbus,'GetConnectionUnixProcessID','s',[owner],'u')
        process=identity(1)
        if type(uid) is not int or uid!=0 or type(pid) is not int or pid!=1 or not process or process['uid']!=0:
            raise DeployError('Expected the Root system manager running as PID1')
        guid=self.call(dbus,obj,dbus,'GetId',reply='s')
        version=self.properties(MANAGER,INTERFACE,{'Version':'s'})['Version']
        if not isinstance(guid,str) or not re.fullmatch('[0-9a-f]{32}',guid):raise DeployError('Invalid system-bus identity')
        return {'bus_id':guid,'owner':owner,'pid1':{k:process[k] for k in ['pid','uid','start_ticks','boot_id']},'version':version}

    def unit(self,name,missing=False):
        try:path=self.call(self.owner or BUS,MANAGER,INTERFACE,'GetUnit','s',[name],'o')
        except MissingUnit:
            if missing:return None
            raise
        if not isinstance(path,str) or not re.fullmatch('/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+',path):raise DeployError('Unexpected selected-unit object path')
        return {**self.properties(path,BUS+'.Unit',UNIT),**self.properties(path,BUS+'.Service',SERVICE)}

    def reload(self):self.call(self.owner or BUS,MANAGER,INTERFACE,'Reload')

    def load(self,name):self.call(self.owner or BUS,MANAGER,INTERFACE,'LoadUnit','s',[name],'o')


def expected(prefix,meta,text):
    lines={line.split('=',1)[0]:line.split('=',1)[1] for line in text.splitlines() if '=' in line}
    env=[line.split('=',1)[1] for line in text.splitlines() if line.startswith('Environment=')]
    return {'Type':'simple','User':str(meta['uid']),'Group':str(meta['gid']),
        'SupplementaryGroups':lines.get('SupplementaryGroups','').split(),'WorkingDirectory':meta['state_dir'],
        'Environment':env,'EnvironmentFiles':[],'PassEnvironment':[],'UnsetEnvironment':[],
        'ExecStart':[['/usr/bin/python3',shlex.split(lines['ExecStart']),False]],
        **{key:[] for key in ['ExecStartPre','ExecStartPost','ExecStop','ExecStopPost','ExecReload']},
        'ReadWritePaths':shlex.split(lines['ReadWritePaths']),'RequiresMountsFor':shlex.split(lines.get('RequiresMountsFor','')),
        **{key:True for key in ['NoNewPrivileges','PrivateTmp','ProtectKernelTunables','ProtectKernelModules','ProtectControlGroups','RestrictSUIDSGID','LockPersonality']},
        'ProtectSystem':'strict','ProtectHome':'read-only','CapabilityBoundingSet':(1<<21) if lines['CapabilityBoundingSet']=='CAP_SYS_ADMIN' else 0,
        'RestrictAddressFamilies':[True,['AF_UNIX','AF_INET','AF_INET6']],'UMask':0o77,
        'RootDirectory':'','RootImage':'','DynamicUser':False,'PrivateUsers':False,'Restart':'on-failure','RestartUSec':5000000,'TimeoutStopUSec':20000000}


def provenance(value,name,fragment):
    if (not isinstance(value,dict) or set(value)!=set(UNIT)|set(SERVICE)
        or value['Id']!=name or value['Names']!=[name] or value['LoadState']!='loaded'
        or value['FragmentPath']!=str(fragment) or value['SourcePath'] or value['DropInPaths'] or value['Transient'] is not False):
        raise DeployError('Manager selected a different, aliased, generated, transient or overridden unit')
    for key,signature in {**UNIT,**SERVICE}.items():
        if not _typed(signature,value[key]):raise DeployError('Malformed loaded property: '+key)


def effective(value,wanted,name,fragment,fresh=True):
    provenance(value,name,fragment)
    if fresh and value['NeedDaemonReload'] is not False:raise DeployError('Manager still needs daemon-reload')
    normalized={k:value[k] for k in wanted}
    for key in ['ExecStart','ExecStartPre','ExecStartPost','ExecStop','ExecStopPost','ExecReload']:
        rows=normalized[key]
        if not isinstance(rows,list) or any(not isinstance(row,list) or len(row)!=10 for row in rows):raise DeployError('Malformed executable property: '+key)
        normalized[key]=[row[:3] for row in rows]
    mounts=normalized['RequiresMountsFor'];declared=wanted['RequiresMountsFor']
    implicit={wanted['WorkingDirectory'],'/tmp','/var/tmp'}
    if not set(declared)<=set(mounts) or not set(mounts)<=set(declared)|implicit:
        raise DeployError('Loaded managed property differs: RequiresMountsFor')
    normalized['RequiresMountsFor']=declared
    for key in ['SupplementaryGroups','Environment','ReadWritePaths']:
        if not isinstance(normalized[key],list) or any(not isinstance(x,str) for x in normalized[key]):raise DeployError('Malformed loaded property: '+key)
        if sorted(normalized[key])!=sorted(wanted[key]):raise DeployError('Loaded managed property differs: '+key)
        normalized[key]=wanted[key]
    af=normalized['RestrictAddressFamilies']
    if not isinstance(af,list) or len(af)!=2 or af[0] is not True or not isinstance(af[1],list) or any(not isinstance(x,str) for x in af[1]) or sorted(af[1])!=sorted(wanted['RestrictAddressFamilies'][1]):
        raise DeployError('Loaded managed property differs: RestrictAddressFamilies')
    normalized['RestrictAddressFamilies']=wanted['RestrictAddressFamilies']
    if normalized!=wanted:
        mismatch=next(k for k in wanted if normalized[k]!=wanted[k]);raise DeployError('Loaded managed property differs: '+mismatch)
    return fingerprint(normalized)


def inactive(value):
    if value is not None and (value.get('ActiveState')!='inactive' or value.get('SubState')!='dead'
        or type(value.get('MainPID')) is not int or value['MainPID']!=0 or type(value.get('ControlPID')) is not int or value['ControlPID']!=0
        or value.get('Job')!=[0,'/'] or type(value['Job'][0]) is not int):
        raise DeployError('Stop and reconcile the selected unit; manager reports a live or pending operation')


def _context(prefix,startup_recovery=False,credential_recovery=False,owner_recovery=False):
    if credential_recovery:
        _,selected,_=deploy.load_installation(prefix);credential_recovery=selected['role']=='controller'
    prefix,meta,release=deploy.verify(prefix,manager_recovery=True,startup_recovery=startup_recovery,
        delivery_recovery=credential_recovery,finish_recovery=credential_recovery,owner_recovery=owner_recovery or credential_recovery)
    if owner_recovery or credential_recovery:
        from owner_provision import service_guard
        service_guard(prefix,meta)
    if os.geteuid() not in {0,meta['uid']}:raise DeployError('Root or the selected service identity is required for readback')
    helper=release/Path(__file__).name
    if not helper.is_file() or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest():
        raise DeployError('Activate matching manager readback and recovery code first')
    files._directory(prefix);state=Path(meta['state_dir']);control=state.stat()
    if state.is_symlink() or control.st_uid!=meta['uid'] or control.st_mode&0o077:raise DeployError('Unsafe managed control state')
    return prefix,meta,state,{'device':control.st_dev,'inode':control.st_ino}


def _contract(prefix,meta,state,control,directory):
    directory=Path(directory);name='ascend-lab-'+meta['role']+'.service';files._directory(directory);files._directory(prefix/'units');files._overrides(directory,name)
    installed=files._file(directory/name,0o644);template=files._file(prefix/'units'/name,0o444);text=deploy.unit_text(prefix,meta)
    if installed['text']!=text or template['text']!=text:raise DeployError('Complete the current offline unit refresh before manager operations')
    return {'binding':files._binding(prefix,meta,state,control),'unit_dir':str(directory),'unit_name':name,
        'unit_directory_identity':files._directory(directory),'template_directory_identity':files._directory(prefix/'units'),
        'installed':installed,'template':template,'expected':expected(prefix,meta,text)}


def check(prefix,unit_dir='/etc/systemd/system',running=False,entry=False,bus=None,startup_recovery=False,credential_recovery=False):
    prefix,meta,state,control=_context(prefix,startup_recovery=startup_recovery,credential_recovery=credential_recovery,owner_recovery=entry);contract=_contract(prefix,meta,state,control,unit_dir);bus=bus or Bus();anchor=bus.anchor();value=bus.unit(contract['unit_name'])
    sha=effective(value,contract['expected'],contract['unit_name'],Path(unit_dir)/contract['unit_name'])
    if running or entry:
        pid=value['MainPID'];saved=read_json(state/'service-process.json') or {}
        if (value['ActiveState']!='active' or value['SubState']!='running' or type(pid) is not int or pid<=0
            or value['ControlPID']!=0 or value['Job']!=[0,'/']):raise DeployError('Selected manager unit is not running and settled')
        if entry:
            if pid!=os.getpid() or os.geteuid()!=meta['uid'] or os.getegid()!=meta['gid']:raise DeployError('Startup process is not this manager unit MainPID/identity')
            invocation=value['InvocationID']
            if (not isinstance(invocation,list) or len(invocation)!=16 or any(type(x) is not int or not 0<=x<=255 for x in invocation)
                or bytes(invocation).hex()!=os.environ.get('INVOCATION_ID')):raise DeployError('Startup invocation differs from this manager unit')
        elif not identity_alive(saved) or saved['pid']!=pid or not deploy.process_status(prefix)['ready']:
            raise DeployError('Manager MainPID and ready managed service identity differ')
        actual=identity(pid)
        if not actual or actual['uid']!=meta['uid']:raise DeployError('Running service UID differs')
        status={k:v.split() for k,sep,v in (line.partition(':') for line in Path('/proc',str(pid),'status').read_text().splitlines()) if sep}
        from payload_groups import gids
        if int(status['Gid'][1])!=meta['gid'] or not set(gids(meta.get('payload_storage')) if meta['role']=='controller' else [])<=set(map(int,status['Groups'])):
            raise DeployError('Running service storage groups differ')
        group=value['ControlGroup'];cgroups=Path('/proc',str(pid),'cgroup').read_text().splitlines()
        if not group or not any(line.split(':',2)[-1]==group for line in cgroups):raise DeployError('Running process does not belong to the manager ControlGroup')
        if not identity_alive(actual) or bus.unit(contract['unit_name'])!=value:raise DeployError('Selected service changed during process readback')
    if bus.anchor()!=anchor:raise DeployError('System manager changed during readback')
    return {'verified':True,'read_only':True,'role':meta['role'],'unit':contract['unit_name'],'running_verified':bool(running or entry),
        'effective_sha256':sha,'manager_sha256':fingerprint(anchor),'raw_properties_exported':False}


def service_entry(prefix):
    invocation=os.environ.get('INVOCATION_ID')
    if invocation is None:return
    if not re.fullmatch('[0-9a-f]{32}',invocation):raise DeployError('Invalid systemd invocation identity')
    from unit_start import entry
    entry(prefix)


def plan(prefix,unit_dir='/etc/systemd/system',bus=None):
    deploy.require_root();prefix,meta,state,control=_context(prefix);bus=bus or Bus()
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,meta['role']);contract=_contract(prefix,meta,state,control,unit_dir)
        if deploy.process_status(prefix)['running']:raise DeployError('Stop the managed process before loading a new manager configuration')
        anchor=bus.anchor();before=bus.unit(contract['unit_name'],missing=True);inactive(before)
        if before is not None:
            # The old manager may still hold previous storage groups. Only
            # provenance is trusted here; full comparison follows Reload/LoadUnit.
            provenance(before,contract['unit_name'],Path(unit_dir)/contract['unit_name'])
        if bus.anchor()!=anchor:raise DeployError('System manager changed while planning')
        value={'format':FORMAT,'contract':contract,'manager':anchor,'nonce':uuid.uuid4().hex};value['id']=fingerprint(value)
        return {'read_only':True,'plan':value,'service_start_still_required':True}


def _plan_shape(value):
    if (not isinstance(value,dict) or set(value)!={'format','contract','manager','nonce','id'} or value['format']!=FORMAT
        or not isinstance(value['id'],str) or not re.fullmatch('[0-9a-f]{64}',value['id'])
        or not isinstance(value['nonce'],str) or not re.fullmatch('[0-9a-f]{32}',value['nonce'])
        or fingerprint({k:v for k,v in value.items() if k!='id'})!=value['id'] or not isinstance(value['contract'],dict)
        or not isinstance(value['manager'],dict)):raise DeployError('Invalid manager reload plan')
    contract=value['contract'];fields={'binding','unit_dir','unit_name','unit_directory_identity','template_directory_identity','installed','template','expected'}
    if set(contract)!=fields or not isinstance(contract['expected'],dict):raise DeployError('Invalid manager plan contract')
    # Reuse the bounded, canonical path/binding/snapshot grammar; the contract
    # is compared again with live Root files before any bus mutation.
    offline={k:v for k,v in contract.items() if k!='expected'}
    offline.update(format=files.FORMAT,desired=contract['installed'].get('text') if isinstance(contract['installed'],dict) else None)
    offline['id']=fingerprint(offline);files._plan_check(offline)
    anchor=value['manager']
    if (set(anchor)!={'bus_id','owner','pid1','version'} or not isinstance(anchor['bus_id'],str) or not re.fullmatch('[0-9a-f]{32}',anchor['bus_id'])
        or not isinstance(anchor['owner'],str) or not re.fullmatch(r':[0-9]+\.[0-9]+',anchor['owner']) or not isinstance(anchor['version'],str)
        or not isinstance(anchor['pid1'],dict) or set(anchor['pid1'])!={'pid','uid','start_ticks','boot_id'}):raise DeployError('Invalid system manager binding')
    process=anchor['pid1']
    if (type(process['pid']) is not int or process['pid']!=1 or type(process['uid']) is not int or process['uid']!=0
        or type(process['start_ticks']) is not int or process['start_ticks']<=0 or not isinstance(process['boot_id'],str)
        or not re.fullmatch('[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}',process['boot_id'])):raise DeployError('Invalid PID1 identity')


def _plan_check(value):
    try:_plan_shape(value)
    except (KeyError,TypeError,ValueError):raise DeployError('Malformed manager plan structure') from None


def _receipt(value,result):
    contract=value['contract']
    if (not isinstance(result,dict) or set(result)!={'format','plan_id','state','role','unit','service_started','service_start_still_required','effective_sha256','manager_sha256'}
        or result['format']!='ascend-lab-unit-manager-receipt/v1' or result['plan_id']!=value['id'] or result['state'] not in {'loaded','aborted'}
        or result['role']!=contract['binding']['role'] or result['unit']!=contract['unit_name']
        or result['service_started'] is not False or result['service_start_still_required'] is not True
        or result['effective_sha256']!=(fingerprint(contract['expected']) if result['state']=='loaded' else None)
        or not isinstance(result['manager_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',result['manager_sha256'])
        or result['state']=='loaded' and result['manager_sha256']!=fingerprint(value['manager'])):raise DeployError('Manager operation receipt differs')


def apply(prefix,value,bus=None,abort=False):
    deploy.require_root();_plan_check(value);prefix,meta,state,control=_context(prefix);bus=bus or Bus()
    with state_lock(state,exclusive=True):
        from maintenance import assert_quiescent
        assert_quiescent(state,meta['role']);contract=_contract(prefix,meta,state,control,value['contract'].get('unit_dir',''))
        if contract!=value['contract']:raise DeployError('Manager plan files, installation or configuration changed')
        journal=prefix/JOURNAL;history=prefix/HISTORY;receipt=history/(value['id']+'.json')
        pending=deploy.regular_json(journal,private=True) if journal.exists() or journal.is_symlink() else None
        if pending is not None and pending!=value:raise DeployError('Resume only the original manager reload plan')
        if history.exists() or history.is_symlink():files._directory(history,private=True)
        if receipt.exists() or receipt.is_symlink():
            result=deploy.regular_json(receipt,private=True)
            _receipt(value,result)
            if pending is None:return {**result,'reused':True,'historical_receipt_only':True}
        else:
            if deploy.process_status(prefix)['running']:raise DeployError('Managed process is live; no manager mutation sent')
            anchor=bus.anchor();current=bus.unit(contract['unit_name'],missing=True);inactive(current)
            if current is not None:provenance(current,contract['unit_name'],Path(contract['unit_dir'])/contract['unit_name'])
            if not abort and anchor!=value['manager']:raise DeployError('System manager identity changed; reconcile or explicitly abort the old operation')
            if pending is None:
                if abort:raise DeployError('No pending manager operation to abort')
                if not history.exists():history.mkdir(mode=0o700);sync(prefix)
                if len(list(history.glob('*.json')))>=1024:raise DeployError('Archive manager receipts before more operations')
                write(journal,value);_checkpoint('journal')
            if not abort:
                bus.reload();_checkpoint('reload_response')
                if bus.anchor()!=value['manager']:raise DeployError('System manager changed during reload')
                bus.load(contract['unit_name']);_checkpoint('load_response')
                current=bus.unit(contract['unit_name']);inactive(current)
                effective(current,contract['expected'],contract['unit_name'],Path(contract['unit_dir'])/contract['unit_name'])
                if bus.anchor()!=value['manager']:raise DeployError('System manager changed during configuration verification')
                _checkpoint('verified')
            result={'format':'ascend-lab-unit-manager-receipt/v1','plan_id':value['id'],'state':'aborted' if abort else 'loaded',
                'role':meta['role'],'unit':contract['unit_name'],'service_started':False,'service_start_still_required':True,
                'effective_sha256':None if abort else fingerprint(contract['expected']),'manager_sha256':fingerprint(anchor)}
            write(receipt,result);_checkpoint('receipt')
        if journal.exists():journal.unlink();sync(prefix)
        _checkpoint('published')
        return {**result,'reused':False,'historical_receipt_only':False}

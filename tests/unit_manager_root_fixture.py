"""Fixed protocol adapter, never a systemd PID1 emulator or production CLI option."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess


def anchor():
    return {'bus_id':'1'*32,'owner':':1.42','pid1':{'pid':1,'uid':0,'start_ticks':123,
        'boot_id':'12345678-1234-1234-1234-123456789abc'},'version':'255-fixture'}


def properties(prefix,meta,unit_dir):
    """Independent fixed wire fixture for the generated controller unit."""
    assert meta['role']=='controller'
    from payload_groups import gids
    name='ascend-lab-controller.service';state=meta['state_dir'];payload=meta.get('payload_storage')
    return {'Id':name,'Names':[name],'LoadState':'loaded','ActiveState':'inactive','SubState':'dead',
        'FragmentPath':str(Path(unit_dir)/name),'SourcePath':'','DropInPaths':[],'NeedDaemonReload':False,'Job':[0,'/'],
        'RequiresMountsFor':[payload['path']] if payload else [],'Transient':False,'ControlGroup':'','InvocationID':[],
        'MainPID':0,'ControlPID':0,'Type':'simple','User':str(meta['uid']),'Group':str(meta['gid']),
        'SupplementaryGroups':[str(x) for x in gids(payload)],'WorkingDirectory':state,
        'Environment':['PYTHONDONTWRITEBYTECODE=1','HOME='+state],'EnvironmentFiles':[],'PassEnvironment':[],'UnsetEnvironment':[],
        'ExecStart':[['/usr/bin/python3',['/usr/bin/python3','-I','-B',str(prefix)+'/current/servicectl','run','--prefix',str(prefix)],False,0,0,0,0,0,0,0]],
        'ExecStartPre':[],'ExecStartPost':[],'ExecStop':[],'ExecStopPost':[],'ExecReload':[],
        'ReadWritePaths':[state,payload['path']] if payload else [state],
        'NoNewPrivileges':True,'ProtectSystem':'strict','ProtectHome':'read-only','PrivateTmp':True,
        'ProtectKernelTunables':True,'ProtectKernelModules':True,'ProtectControlGroups':True,'RestrictSUIDSGID':True,
        'LockPersonality':True,'CapabilityBoundingSet':0,'RestrictAddressFamilies':[True,['AF_UNIX','AF_INET','AF_INET6']],
        'UMask':0o77,'RootDirectory':'','RootImage':'','DynamicUser':False,'PrivateUsers':False,
        'Restart':'on-failure','RestartUSec':5000000,'TimeoutStopUSec':20000000}


class Protocol:
    def __init__(self,path):self.path=Path(path)
    def read(self):return json.loads(self.path.read_text())
    def save(self,value):
        from payload_quota_admin import write
        write(self.path,value)
    def anchor(self):return copy.deepcopy(self.read()['anchor'])
    def unit(self,name,missing=False):
        value=self.read()['current']
        if value is None and not missing:
            from unit_manager import MissingUnit
            raise MissingUnit('Fixture unit missing')
        return copy.deepcopy(value)
    def reload(self):
        value=self.read();value['calls'].append('Reload');self.save(value)
    def load(self,name):
        value=self.read();value['calls'].append('LoadUnit');value['current']=copy.deepcopy(value['desired']);self.save(value)


def run(session):
    import deployment as deploy
    import unit_manager as unit
    from lab_client import read_json
    from payload_quota_admin import write
    from state_lock import state_lock
    prefix=Path(session.prefix);directory=session.unit_dir;meta=deploy.verify(prefix)[1]
    path=Path('/state/manager-protocol.json');desired=properties(prefix,meta,directory)
    write(path,{'anchor':anchor(),'current':None,'desired':desired,'calls':[]});bus=Protocol(path)
    with state_lock(session.state):
        try:unit.plan(prefix,directory,bus=bus)
        except RuntimeError:pass
        else:raise AssertionError('Shared service lock permitted manager plan')
    value=unit.plan(prefix,directory,bus=bus)['plan'];unit._plan_check(value)
    assert unit.plan(prefix,directory,bus=bus)['plan']['id']!=value['id']
    output=Path('/state/manager-plan.json');write(output,value)
    bad=bus.read();bad['current']={**desired,'SourcePath':'/generated'};bus.save(bad)
    try:unit.apply(prefix,value,bus=bus)
    except deploy.DeployError:pass
    else:raise AssertionError('Generated manager unit was adopted')
    assert not (prefix/unit.JOURNAL).exists() and not bus.read()['calls']
    bad['current']=None;bus.save(bad)
    faults=[]
    child='''import sys,os,signal,json
sys.path.insert(0,"/opt/handoff-controller/current");sys.path.insert(0,"/payload")
import unit_manager as u
from unit_manager_fixture import Protocol
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
u._checkpoint=crash
u.apply("/opt/handoff-controller",json.load(open("/state/manager-plan.json")),bus=Protocol("/state/manager-protocol.json"))
'''
    for boundary in ['journal','reload_response','load_response','verified','receipt','published']:
        killed=subprocess.run(['/usr/bin/python3','-I','-B','-c',child,boundary],capture_output=True,text=True,timeout=30)
        assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-1200:])
        if boundary!='published':
            for recovery in [{},{'quota_recovery':True},{'unit_recovery':True}]:
                try:deploy.verify(prefix,**recovery)
                except deploy.DeployError:pass
                else:raise AssertionError('Pending manager journal bypassed verify')
            from maintenance import snapshot,activate
            for operation in [lambda:deploy.start(prefix),lambda:snapshot(prefix,'/opt/manager-blocked.tar.gz'),
                              lambda:activate(prefix,release_id=meta['release_id'])]:
                try:operation()
                except deploy.DeployError:pass
                else:raise AssertionError('Pending manager transaction permitted maintenance/start')
        faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_resumed':True})
    replay=unit.apply(prefix,value,bus=bus)
    assert replay['reused'] and replay['historical_receipt_only'] and replay['service_started'] is False
    assert unit.check(prefix,directory,bus=bus)['verified']
    calls_before=list(bus.read()['calls'])
    fresh=unit.plan(prefix,directory,bus=bus)['plan'];write(output,fresh)
    killed=subprocess.run(['/usr/bin/python3','-I','-B','-c',child,'journal'],capture_output=True,text=True,timeout=30)
    assert killed.returncode==-signal.SIGKILL
    changed=bus.read();changed['anchor']['owner']=':1.99';bus.save(changed)
    try:unit.apply(prefix,fresh,bus=bus)
    except deploy.DeployError:pass
    else:raise AssertionError('Changed manager silently resumed pending operation')
    assert bus.read()['calls']==calls_before
    aborted=unit.apply(prefix,fresh,bus=bus,abort=True);assert aborted['state']=='aborted'
    assert unit.apply(prefix,fresh,bus=bus)['state']=='aborted' and bus.read()['calls']==calls_before
    final_plan=unit.plan(prefix,directory,bus=bus)['plan'];assert final_plan['id']!=fresh['id']
    final=unit.apply(prefix,final_plan,bus=bus);assert final['state']=='loaded'
    deploy.verify(prefix);assert not deploy.process_status(prefix)['running'] and not (prefix/unit.JOURNAL).exists()
    proof=Path('/state/unit-manager-proof');proof.mkdir(mode=0o700)
    write(proof/'plan.json',value);write(proof/'receipt.json',read_json(prefix/unit.HISTORY/(value['id']+'.json')))
    write(proof/'aborted.json',aborted)
    return {'passed':True,'fault_chain':faults,'manager_change_rejected':True,'explicit_abort_no_bus_mutation':True,
        'fresh_plan_after_abort':True,'pending_start_backup_activation_blocked':True,'shared_service_lock_rejected':True,
        'generated_unit_rejected_before_journal':True,'historical_replay_is_not_readiness':True,
        'mutating_protocol_calls':bus.read()['calls'],'service_started_by_manager':False,'systemd_pid1_tested':False,
        'scope':'Real installed Root files/journal and six SIGKILL boundaries with a fixed protocol adapter; no actual systemd manager Reload/LoadUnit/start.'}

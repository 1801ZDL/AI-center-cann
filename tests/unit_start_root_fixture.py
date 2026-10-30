"""Owned Root startup protocol adapter launching the real installed controller.

No host system bus/cgroups/systemd writes. PID1 provenance is represented by
the fixed adapter; actual service UID, locks, nonce, HTTP and ready are real.
"""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from unit_manager_fixture import Protocol


class Wire(Protocol):
    def save(self,value):
        from payload_quota_admin import write
        write(self.path,value,mode=0o644)
    def start(self,name):
        import deployment as deploy
        import unit_start as start
        from payload_groups import gids
        value=self.read();assert name==value['desired']['Id'];value['calls'].append('StartUnit');self.save(value)
        prefix=value['prefix'];meta=deploy.load_installation(prefix)[1]
        gate=json.loads(Path(prefix,start.GATE).read_text());assert gate['phase']=='issued'
        def drop():os.setgroups(gids(meta.get('payload_storage')));os.setgid(meta['gid']);os.setuid(meta['uid']);os.umask(0o077)
        code='''import sys
sys.path.insert(0,sys.argv[1]+"/current");sys.path.insert(0,"/payload")
import deployment as d,unit_manager as m,unit_start
from unit_start_fixture import ReadWire
m.Bus=lambda:ReadWire(sys.argv[2])
d.run_service(sys.argv[1])
'''
        log=Path(meta['state_dir'])/'startup-fixture.log';fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.fchown(fd,meta['uid'],meta['gid'])
        invocation='c'*32
        with os.fdopen(fd,'ab') as out:
            child=subprocess.Popen(['/usr/bin/python3','-I','-B','-c',code,prefix,str(self.path)],preexec_fn=drop,
                env={'PATH':'/usr/bin:/bin','INVOCATION_ID':invocation},stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True)
        current=copy.deepcopy(value['desired']);current.update(MainPID=child.pid,ActiveState='active',SubState='running',
            InvocationID=list(bytes.fromhex(invocation)),ControlGroup=Path('/proc',str(child.pid),'cgroup').read_text().splitlines()[0].split(':',2)[-1])
        value=self.read();value['current']=current;value['service_pid']=child.pid;self.save(value)
        start._checkpoint('accepted_without_response')
        return '/org/freedesktop/systemd1/job/42'


class ReadWire(Wire):
    def unit(self,name,missing=False):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            value=self.read()['current']
            if value is not None and value.get('MainPID')==os.getpid():return value
            time.sleep(.01)
        raise RuntimeError('Fixture MainPID was not published')


def run_node(prefix,directory):
    import deployment as deploy
    import unit_start as start
    from lab_client import read_json
    from payload_quota_admin import write
    from quota_workspace import is_quota
    from unit_manager_fixture import properties,anchor
    prefix=Path(prefix);meta=deploy.verify(prefix)[1];state=Path(meta['state_dir']);config=read_json(state/'config.json')
    desired=properties(prefix,{**meta,'role':'controller'},directory)
    mounts=set()
    for slot in config['workspace']['slots']:mounts.add(str(Path(slot['path']).parent) if is_quota(slot) else slot['path'])
    for slot in config['persistent_cache']['slots']:mounts.add(str(Path(slot['path']).parent))
    mounts.add(config['spool_storage']['path'])
    desired.update(Id='ascend-lab-node.service',Names=['ascend-lab-node.service'],FragmentPath=str(Path(directory)/'ascend-lab-node.service'),
        ReadWritePaths=[str(state),*sorted(mounts)],RequiresMountsFor=sorted(mounts),CapabilityBoundingSet=1<<21)
    path=Path('/state/node-start-protocol.json');wire=Wire(path);wire.save({'prefix':str(prefix),'anchor':anchor(),'desired':desired,'current':desired,'calls':[]})
    value=start.plan(prefix,directory,bus=wire)['plan'];output=Path('/state/node-start-plan.json');write(output,value)
    child='''import sys,os,signal,json
sys.path.insert(0,"/opt/owner-node/current");sys.path.insert(0,"/payload")
import unit_start as s
from unit_start_fixture import Wire
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
s._checkpoint=crash;s.apply("/opt/owner-node",json.load(open("/state/node-start-plan.json")),bus=Wire("/state/node-start-protocol.json"))
'''
    def killed(boundary):
        r=subprocess.run(['/usr/bin/python3','-I','-B','-c',child,boundary],capture_output=True,text=True,timeout=30)
        assert r.returncode==-signal.SIGKILL,(boundary,r.returncode,r.stderr[-1600:])
    killed('response');deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        status=deploy.process_status(prefix)
        if status['ready']:break
        time.sleep(.1)
    assert status['running'] and status['ready']
    pid=status['pid'];ack=read_json(state/'heartbeat-receipt.json')
    assert ack['node_id']==config['node_id'] and ack['service_start_id']==value['nonce'] and ack['pid']==pid
    faults=[]
    for boundary in ['ready','receipt','gate_removed','published']:
        killed(boundary);assert wire.read()['calls']==['StartUnit'] and deploy.process_status(prefix)['pid']==pid
        faults.append({'boundary':boundary,'sigkill_observed':True,'same_process':True,'no_second_start':True})
    result=start.apply(prefix,value,bus=wire);assert result['historical_receipt_only'] and result['state']=='ready'
    deploy.stop(prefix);assert not deploy.process_status(prefix)['running']
    return {'passed':True,'response_sigkill_observed':True,'ready_fault_chain':faults,'start_unit_calls':1,
        'actual_root_worker_heartbeat':True,'nonce_pid_node_id_matched':True,'ordinary_stop_after_publication':True,
        'systemd_pid1_tested':False,'scope':'Fixed manager adapter plus actual installed Root Node and authenticated heartbeat to the running non-root controller; not actual PID1.'}


def run(session):
    import deployment as deploy
    import unit_start as start
    from lab_client import Client,ClientError,read_json
    from payload_quota_admin import write
    from unit_manager_fixture import properties,anchor
    prefix=Path(session.prefix);meta=deploy.verify(prefix)[1];path=Path('/state/start-protocol.json')
    wire=Wire(path);desired=properties(prefix,meta,session.unit_dir)
    wire.save({'prefix':str(prefix),'anchor':anchor(),'desired':desired,'current':desired,'calls':[]})
    value=start.plan(prefix,session.unit_dir,bus=wire)['plan'];output=Path('/state/start-plan.json');write(output,value)
    child='''import sys,os,signal,json
sys.path.insert(0,"/opt/handoff-controller/current");sys.path.insert(0,"/payload")
import unit_start as s
from unit_start_fixture import Wire
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
s._checkpoint=crash;s.apply("/opt/handoff-controller",json.load(open("/state/start-plan.json")),bus=Wire("/state/start-protocol.json"))
'''
    def killed(boundary):
        result=subprocess.run(['/usr/bin/python3','-I','-B','-c',child,boundary],capture_output=True,text=True,timeout=30)
        assert result.returncode==-signal.SIGKILL,(boundary,result.returncode,result.stderr[-1600:])
    uncertain=[]
    for boundary in ['journal','prepared','issued']:
        killed(boundary);uncertain.append({'boundary':boundary,'sigkill_observed':True})
        for flags in [{},{'manager_recovery':True},{'unit_recovery':True},{'quota_recovery':True}]:
            try:deploy.verify(prefix,**flags)
            except deploy.DeployError:pass
            else:raise AssertionError('Pending startup bypassed verify')
    # Issued is durable but the RPC was never entered. Recovery MUST NOT send.
    pending=start.apply(prefix,value,bus=wire);assert pending['state']=='pending' and wire.read()['calls']==[]
    aborted=start.apply(prefix,value,bus=wire,abort=True);assert aborted['state']=='aborted' and not wire.read()['calls']
    replay=start.apply(prefix,value,bus=wire);assert replay['historical_receipt_only'] and replay['state']=='aborted'
    value=start.plan(prefix,session.unit_dir,bus=wire)['plan'];write(output,value)
    killed('accepted_without_response')
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        status=deploy.process_status(prefix)
        if status['ready']:break
        time.sleep(.1)
    assert status['running'] and status['ready']
    pid=status['pid'];assert wire.read()['calls']==['StartUnit']
    # Trusted Root fixture reads the central file; personal clients below in
    # the onboarding acceptance still use from_profile under the actual owner.
    client=Client(deploy.regular_json(session.state/'personas/old_owner/client.json',private=True))
    def held():
        assert client.request('/api/me')['id']=='old_owner'
        try:client.request('/api/jobs',{})
        except ClientError as error:assert '503' in str(error),str(error)
        else:raise AssertionError('Held startup admitted a new job')
    held()
    try:start.apply(prefix,value,bus=wire,abort=True)
    except RuntimeError:pass
    else:raise AssertionError('Live service startup was aborted without stopping it')
    changed=wire.read();original=copy.deepcopy(changed);changed['current']['InvocationID']=[187]*16;wire.save(changed)
    try:start.apply(prefix,value,bus=wire)
    except deploy.DeployError:pass
    else:raise AssertionError('Changed invocation adopted an old startup claim')
    wire.save(original);faults=[]
    for boundary in ['ready','receipt','gate_removed','published']:
        killed(boundary);assert wire.read()['calls']==['StartUnit'] and deploy.process_status(prefix)['pid']==pid
        if boundary!='published':held()
        faults.append({'boundary':boundary,'sigkill_observed':True,'same_process':True,'no_second_start':True})
    again=start.apply(prefix,value,bus=wire);assert again['historical_receipt_only'] and again['state']=='ready'
    assert not (prefix/start.JOURNAL).exists() and not (prefix/start.GATE).exists()
    try:client.request('/api/jobs',{})
    except ClientError as error:assert '400' in str(error),str(error)
    else:raise AssertionError('Invalid request unexpectedly accepted after opening admission')
    assert wire.read()['calls']==['StartUnit'] and deploy.verify(prefix)[1]['uid']==23820
    proof=Path('/state/unit-start-proof');proof.mkdir(mode=0o700);write(proof/'plan.json',value);write(proof/'receipt.json',read_json(prefix/start.HISTORY/(value['id']+'.json')))
    return {'passed':True,'uncertain_before_send_faults':uncertain,'issued_before_rpc_not_retried':True,'aborted_without_start':True,
        'accepted_without_response_sigkill':True,'live_invocation_mismatch_rejected':True,'live_abort_rejected':True,
        'ready_fault_chain':faults,'start_unit_calls':1,'actual_service_pid_preserved':True,'controller_uid':23820,
        'http503_until_final_publication':True,'me_available_while_held':True,'maintenance_guard_independent':True,
        'actual_service_shared_lock_acquired':True,'systemd_pid1_tested':False,
        'scope':'Fixed manager protocol adapter launches a real installed non-root controller; real nonce/locks/HTTP/ready and coordinator SIGKILL recovery. Not actual PID1 StartUnit or Node startup/physical-host acceptance.'}

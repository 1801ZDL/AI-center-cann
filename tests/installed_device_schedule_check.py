"""Actual installed admin/restore guards with explicitly synthetic budget history.

The installed CPU Node has no NPU contract. Never enable a device plan, register
a fake execution Node, open a device, or claim a real self-check success here.
"""
import json
import base64
from pathlib import Path


def run(ctx):
    controller=ctx['controller'];state=ctx['controller_state'];uid=ctx['installed']['uid'];gid=ctx['installed']['gid']
    docker=ctx['docker'];ctl=ctx['ctl'];spec_path=state/'device-schedule-spec.json'
    # This minimal owned image omits tzdata. Supply only UTC to the disposable
    # controller; production deliberately rejects unavailable IANA zones.
    utc=Path('/usr/share/zoneinfo/UTC').read_bytes()
    install_utc='import base64,sys;from pathlib import Path;p=Path("/usr/share/zoneinfo/UTC");p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(base64.b64decode(sys.argv[1]))'
    docker(['exec',controller,'/usr/bin/python3','-I','-c',install_utc,base64.b64encode(utc).decode()])
    spec={'name':'restore-fixture','owner':'proof_user','project':'personal:proof_user','node_id':ctx['configuration']['node_id'],
        'device_ids':[0],'timezone':'UTC','at':'02:00','window_minutes':240,'memory_mib':512,
        'max_probes_per_card':2,'daily_npu_seconds':300,'total_npu_seconds':600}
    write='import sys,os;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600);os.chown(p,int(sys.argv[2]),int(sys.argv[3]))'
    docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',write,str(spec_path),str(uid),str(gid)],input=json.dumps(spec))
    before=ctx['api']('/api/jobs')[1]['counts']
    refused=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/opt/ascend-lab-controller/current/workerctl',
        '--controller-state',str(state),'device-schedule-add','--spec',str(spec_path)],check=False)
    assert refused.returncode!=0 and 'Controller has not enabled managed NPU jobs' in refused.stderr
    assert ctx['api']('/api/jobs')[1]['counts']==before
    assert ctx['api']('/api/device-schedules')[1]['plans']==[]
    status,_=ctx['api']('/api/device-schedules',{});assert status in {403,404}
    ctl(controller,'controller','stop')
    ctl(controller,'controller','backup','--output','/root/backups/device-before.tar.gz')
    # Fault-injected retained accounting, not a plan approved by create/enable.
    # It is paused forever in this fixture and has no executable run/action.
    code='''import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
from jobs import JobStore,read_json,now
from npu_selfcheck import DRIVER,source_sha
from device_schedule import specification
import base64
p=Path(sys.argv[1]);j=JobStore(p,read_json(p/'config.json'));spec=specification(json.loads(sys.argv[2]));owner=j.access.user('proof_user')
raw=(Path('/opt/ascend-lab-controller/current')/DRIVER).read_bytes()
b,_=j.inputs.create(owner,{'files':[{'name':DRIVER,'size':len(raw),'sha256':source_sha()}]})
if not b['sealed']:
 j.inputs.upload(b['id'],owner,{'name':DRIVER,'offset':0,'data':base64.b64encode(raw).decode()});b=j.inputs.seal(b['id'],owner)
record={'id':'schedule-'+'d'*24,'spec':spec,'context':{'driver_sha256':source_sha()},'input_id':b['id'],
 'input_manifest_sha256':b['manifest_sha256'],'state':'paused','authorization':None,'generation':1,'total_seconds':120,
 'watermark':'2026-10-31','reason':'Synthetic retained-budget restore fixture; never approved or executed','created_at':now()}
with j.connect() as db:
 db.execute('INSERT INTO device_schedule_plans VALUES(?,?,?)',(record['id'],spec['name'],json.dumps(record)))
 db.execute('INSERT INTO device_schedule_days VALUES(?,?,?)',(record['id'],'2026-10-31',120))
print(json.dumps({'plan_id':record['id'],'total_seconds':120,'synthetic_history':True,'input_id':b['id']}))
'''
    result=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-I','-c',code,str(state),json.dumps(spec)])
    history=json.loads(result.stdout)
    target=ctx['base']/'device-older-rejected'
    configuration_hash='import hashlib,sys;from pathlib import Path;print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())'
    original=docker(['exec',controller,'/usr/bin/python3','-I','-c',configuration_hash,str(state/'config.json')]).stdout.strip()
    rejected=ctl(controller,'controller','restore','--archive','/root/backups/device-before.tar.gz','--new-state-dir',str(target),check=False)
    assert rejected.returncode!=0 and 'daily schedule authority or charges' in rejected.stderr,rejected.stderr[-1500:]
    assert not target.exists()
    assert docker(['exec',controller,'/usr/bin/python3','-I','-c',configuration_hash,str(state/'config.json')]).stdout.strip()==original
    ctl(controller,'controller','start')
    data=ctx['api']('/api/device-schedules')[1]
    assert data['plans'][0]['reserved_npu_seconds']==120 and data['plans'][0]['state']=='paused'
    return {'passed':True,'plan_id':history['plan_id'],'synthetic_history':True,'prepared_device_plan_rejected':True,'fixture_utc_zone_prepared':True,
        'no_new_device_jobs':True,'no_http_write_authority':True,'older_snapshot_rejected_before_rebind':True,
        'configuration_unchanged_after_rejection':True,'retained_total_seconds':120,'original_context_not_hardware_approved':True}


def after_restore(ctx,proof):
    data=ctx['api']('/api/device-schedules')[1]
    plan=next(p for p in data['plans'] if p['id']==proof['plan_id'])
    assert plan['state']=='restore_paused' and plan['reserved_npu_seconds']==120
    controller=ctx['controller'];uid=ctx['installed']['uid'];gid=ctx['installed']['gid'];state=ctx['restored_state']
    refused=ctx['docker'](['exec','--user',str(uid)+':'+str(gid),controller,'/opt/ascend-lab-controller/current/workerctl',
        '--controller-state',str(state),'device-schedule-enable',proof['plan_id']],check=False)
    assert refused.returncode!=0 and 'historical only' in refused.stderr
    return {'passed':True,'state':'restore_paused','reserved_npu_seconds':120,'old_approval_cannot_reenable':True,'new_device_jobs':0}

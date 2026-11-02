"""Installed guards with explicit synthetic policy history, never NPU approval."""
import json


def run(ctx):
    docker=ctx['docker'];ctl=ctx['ctl'];controller=ctx['controller'];state=ctx['controller_state']
    uid=ctx['installed']['uid'];gid=ctx['installed']['gid'];node=ctx['configuration']['node_id']
    spec={'name':'synthetic-guard','source_plan_id':ctx['device_schedule_proof']['plan_id'],
          'target_node_ids':[node],'projects':['personal:proof_user'],'max_age_seconds':3600,'mode':'require'}
    path=state/'device-evidence-spec.json'
    write='import os,sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600);os.chown(p,int(sys.argv[2]),int(sys.argv[3]))'
    docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',write,str(path),str(uid),str(gid)],input=json.dumps(spec))
    refused=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/opt/ascend-lab-controller/current/workerctl',
        '--controller-state',str(state),'device-evidence-add','--spec',str(path)],check=False)
    assert refused.returncode and 'activate a prepared golden daily source plan' in refused.stderr,refused.stderr[-1200:]
    assert ctx['api']('/api/device-evidence')[1]['total']==0
    status,_=ctx['api']('/api/device-evidence',{});assert status in {403,404}
    ctl(controller,'controller','stop');ctl(controller,'controller','backup','--output','/root/backups/device-evidence-before.tar.gz')
    # This is a fault-injected retained requirement, not a policy accepted by
    # create/enable. Its source is paused and no NPU contract/check exists.
    code='''import sys,json
from pathlib import Path
sys.path.insert(0,"/opt/ascend-lab-controller/current")
from jobs import JobStore,read_json,now
p=Path(sys.argv[1]);j=JobStore(p,read_json(p/'config.json'));spec=json.loads(sys.argv[2])
value={'id':'evidence-'+'e'*24,'spec':spec,'anchor':{'targets':{spec['target_node_ids'][0]:{'environment_sha256':'0'*64,'cards':{'0':'0'*64}}},
 'source_context_sha256':'0'*64,'source_authorization':None},'state':'active','approval':'synthetic-never-hardware-approved',
 'revision':1,'generation':1,'created_at':now(),'synthetic_history':True}
with j.connect() as db:
 db.execute('INSERT INTO device_evidence_policies VALUES(?,?,?)',(value['id'],spec['name'],json.dumps(value)))
 db.execute('INSERT INTO device_evidence_targets VALUES(?,?,?)',(value['id'],spec['target_node_ids'][0],spec['projects'][0]))
print(json.dumps({'policy_id':value['id'],'synthetic_history':True}))
'''
    created=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-I','-c',code,str(state),json.dumps(spec)])
    identifier=json.loads(created.stdout)['policy_id'];target='/var/lib/ascend-lab-evidence-rejected'
    before=ctx['secret_json'](controller,state/'config.json')
    rejected=ctl(controller,'controller','restore','--archive','/root/backups/device-evidence-before.tar.gz','--new-state-dir',target,check=False)
    assert rejected.returncode and 'Self-check policy/check history' in rejected.stderr,rejected.stderr[-1200:]
    exists=docker(['exec',controller,'/usr/bin/python3','-I','-c','import sys;from pathlib import Path;assert not Path(sys.argv[1]).exists()',target])
    assert exists.returncode==0 and ctx['secret_json'](controller,state/'config.json')==before
    ctl(controller,'controller','start')
    public=ctx['api']('/api/device-evidence')[1];policy=next(p for p in public['policies'] if p['id']==identifier)
    assert policy['mode']=='require' and policy['cards'][0]['evidence']['status']!='recent_microchecks'
    return {'passed':True,'policy_id':identifier,'synthetic_policy_history':True,'no_hardware_approval':True,
        'unprepared_golden_source_rejected':True,'no_http_write_authority':True,'older_snapshot_guard_loss_rejected':True,
        'rejected_target_absent_inside_original_container':True,'current_configuration_unchanged':True,'new_npu_jobs':0}


def after_restore(ctx,proof):
    data=ctx['api']('/api/device-evidence')[1];policy=next(p for p in data['policies'] if p['id']==proof['policy_id'])
    assert policy['state']=='restore_paused' and policy['mode']=='require'
    assert policy['cards'][0]['evidence']['required'] and policy['cards'][0]['evidence']['status']=='approval_required'
    refused=ctx['docker'](['exec','--user',str(ctx['installed']['uid'])+':'+str(ctx['installed']['gid']),ctx['controller'],
        '/opt/ascend-lab-controller/current/workerctl','--controller-state',str(ctx['restored_state']),
        'device-evidence-enable',proof['policy_id']],check=False)
    assert refused.returncode and 'historical' in refused.stderr
    return {'passed':True,'required_guard_retained':True,'state':'restore_paused','old_policy_enable_rejected':True,
            'new_hardware_qualification':False,'new_npu_jobs':0}

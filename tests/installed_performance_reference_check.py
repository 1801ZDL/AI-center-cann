"""Installed source rejection and restore fencing, with explicit injected history.

This CPU fixture never creates/approves a real NPU performance reference.
"""
import json


def run(ctx):
    docker=ctx['docker'];ctl=ctx['ctl'];controller=ctx['controller'];state=ctx['controller_state']
    uid=ctx['installed']['uid'];gid=ctx['installed']['gid'];node=ctx['configuration']['node_id']
    spec={'name':'cpu-is-not-npu-reference','source_job_ids':[ctx['first']['id'],ctx['probe_job']['id'],ctx['measurement_proof']['job_ids'][0]],
        'ratio_bounds':[.8,1.2],'max_round_ratio':1.1,'max_age_seconds':3600}
    path=state/'performance-reference-spec.json'
    write='import sys,os;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600);os.chown(p,int(sys.argv[2]),int(sys.argv[3]))'
    docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',write,str(path),str(uid),str(gid)],input=json.dumps(spec))
    before=ctx['api']('/api/jobs')[1]['counts']
    refused=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/opt/ascend-lab-controller/current/workerctl',
        '--controller-state',str(state),'performance-reference-add','--spec',str(path)],check=False)
    assert refused.returncode and 'original completed fixed managed NPU microchecks' in refused.stderr,refused.stderr[-1500:]
    assert ctx['api']('/api/performance-references')[1]['total']==0 and ctx['api']('/api/jobs')[1]['counts']==before
    assert ctx['api']('/api/performance-references',{})[0] in {403,404}
    ctl(controller,'controller','stop');ctl(controller,'controller','backup','--output','/root/backups/reference-before.tar.gz')
    # Retained policy history is explicitly fault-injected. Nonexistent source
    # IDs and a synthetic approval must never be mistaken for measured evidence.
    code='''import json,sys,time
from pathlib import Path
sys.path.insert(0,'/opt/ascend-lab-controller/current')
from jobs import JobStore,read_json,now
p=Path(sys.argv[1]);j=JobStore(p,read_json(p/'config.json'));node=sys.argv[2]
sources=['job-'+c*12 for c in ['d','e','f']]
with j.connect() as db:
 assert all(not db.execute('SELECT 1 FROM jobs WHERE id=?',(i,)).fetchone() for i in sources)
 spec={'name':'synthetic-reference-history','source_job_ids':sources,'ratio_bounds':[.8,1.2],'max_round_ratio':1.1,'max_age_seconds':3600}
 value={'id':'perfref-'+'b'*24,'spec':spec,'sources':[{'job_id':i} for i in sources],
  'context':{'node_id':node,'device_id':0,'project':'personal:proof_user'},'center_us':1.,'limits_us':[.8,1.2],
  'expires_at':time.time()+3600,'state':'active','revision':1,'approval':'fault-injected-not-a-real-approval','approved_at':now(),'created_at':now(),
  'synthetic_history':True}
 db.execute('INSERT INTO performance_references VALUES(?,?,?)',(value['id'],spec['name'],json.dumps(value)))
print(value['id'])
'''
    result=docker(['exec','--user',str(uid)+':'+str(gid),controller,'/usr/bin/python3','-I','-c',code,str(state),node])
    identifier=result.stdout.strip();target='/var/lib/ascend-lab-reference-rejected'
    original=ctx['secret_json'](controller,state/'config.json')
    rejected=ctl(controller,'controller','restore','--archive','/root/backups/reference-before.tar.gz','--new-state-dir',target,check=False)
    assert rejected.returncode and 'performance reference history' in rejected.stderr,rejected.stderr[-1500:]
    docker(['exec',controller,'/usr/bin/python3','-I','-c','import sys;from pathlib import Path;assert not Path(sys.argv[1]).exists()',target])
    assert ctx['secret_json'](controller,state/'config.json')==original
    ctl(controller,'controller','start')
    value=ctx['api']('/api/performance-references')[1]['references'][0]
    assert value['id']==identifier and value['validity']!='active' and value['performance_qualified'] is False
    return {'passed':True,'reference_id':identifier,'synthetic_history_only':True,'actual_cpu_source_rejected':True,
        'no_new_jobs_or_http_write_authority':True,'older_snapshot_rejected':True,'rejected_target_absent_inside_controller':True,
        'no_real_npu_approval':True,'npu_kernel_launches':0}


def after_restore(ctx,proof):
    value=next(v for v in ctx['api']('/api/performance-references')[1]['references'] if v['id']==proof['reference_id'])
    assert value['state']=='restore_paused' and value['validity']=='approval_required'
    refused=ctx['docker'](['exec','--user',str(ctx['installed']['uid'])+':'+str(ctx['installed']['gid']),ctx['controller'],
        '/opt/ascend-lab-controller/current/workerctl','--controller-state',str(ctx['restored_state']),
        'performance-reference-approve',proof['reference_id']],check=False)
    assert refused.returncode and 'historical' in refused.stderr
    return {'passed':True,'old_reference_frozen':True,'reapproval_rejected':True,'no_new_npu_qualification':True}

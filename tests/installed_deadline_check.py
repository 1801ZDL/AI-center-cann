"""Real node-bound waiting and resumption for a declared long CPU workflow."""
import copy
import json
import time


def run(ctx):
    api=ctx['api'];ctl=ctx['ctl'];docker=ctx['docker'];controller=ctx['controller'];node=ctx['node']
    request=copy.deepcopy(ctx['request']);request['parameters']['timeout_seconds']=7200;request['idempotency_key']='installed-long-deadline-same-job'
    status,_=api('/api/jobs',request);assert status==400,'Default administrator ceiling must still reject long submissions'
    assert not ctl(controller,'controller','stop')['running']
    edit='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["scheduler"]["max_timeout_seconds"]=7200;p.write_text(json.dumps(v));p.chmod(0o600)'
    docker(['exec',controller,'/usr/bin/python3','-I','-c',edit,str(ctx['controller_state']/'config.json')])
    assert ctl(controller,'controller','start')['ready']
    status,value=api('/api/jobs',request);assert status==201,value
    identifier=value['job']['id'];deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        detail=api('/api/jobs/'+identifier)[1]
        if '执行时限不足' in detail['job'].get('reason',''):break
        time.sleep(.2)
    assert detail['job']['state']=='queued' and not detail['attempts'] and detail['job']['allocation']=={},detail
    assert '执行时限不足' in detail['job']['reason']
    before=next(n for n in api('/api/nodes')[1]['nodes'] if n['id']==ctx['enrollment']['node_id'])
    assert before['max_timeout_seconds']==300
    ctl(node,'node','stop')
    change='import json,sys;from pathlib import Path;p=Path(sys.argv[1]);v=json.loads(p.read_text());v["max_timeout_seconds"]=7200;p.write_text(json.dumps(v));p.chmod(0o600)'
    docker(['exec',node,'/usr/bin/python3','-I','-c',change,str(ctx['node_state']/'config.json')])
    assert ctl(node,'node','start')['ready']
    job=ctx['wait'](identifier);assert job['state']=='succeeded' and job['timeout_seconds']==7200
    assert len(api('/api/jobs/'+identifier)[1]['attempts'])==1
    after=next(n for n in api('/api/nodes')[1]['nodes'] if n['id']==ctx['enrollment']['node_id'])
    assert after['max_timeout_seconds']==7200
    receipt=api('/api/jobs/'+identifier+'/artifact?name=results%2F_lab%2Fenvironment.json')[1]
    assert receipt['locked'] and receipt['workload_exit_code']==0
    return {'passed':True,'job_id':identifier,'default_controller_rejected':True,'waiting_attempts':0,'waiting_allocation':{},
        'before_node_max_seconds':300,'after_node_max_seconds':7200,'declared_task_seconds':7200,'attempts':1,
        'actual_ir_succeeded':True,'scope':'Actual long declared deadline admission and short IR execution; not a two-hour elapsed timeout test or full compiler build'}

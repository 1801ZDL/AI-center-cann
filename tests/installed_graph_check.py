"""Actual ordinary graph across the existing full Root backup/restore fixture."""
import copy
import json
from pathlib import Path

from installed_operator_check import upload


def run(ctx):
    assert not ctx['options'].central_payload
    ctl=ctx['ctl'];controller=ctx['controller'];api=ctx['api'];base=ctx['base']
    assert not any(api('/api/jobs')[1]['counts'][key] for key in ['queued','reserved','running','cancelling','needs_attention'])
    ctl(controller,'controller','stop')
    ctl(controller,'controller','backup','--output','/root/backups/pre-graph.tar.gz')
    assert ctl(controller,'controller','start')['ready']
    source=base/'graph-input';source.mkdir(mode=0o700)
    original=(ctx['ROOT']/'examples/workflows/ir.py').read_text()
    (source/'graph_ir.py').write_text('import sys\n'+original.replace("'input.mlir'",'sys.argv[1]'))
    (source/'input.mlir').write_bytes((ctx['ROOT']/'examples/workflows/input.mlir').read_bytes())
    bundle=upload(ctx,source);nodes=[]
    for i in range(3):
        job=copy.deepcopy(ctx['request']);job.pop('idempotency_key',None)
        job['parameters'].update(input_id=bundle,argv=['python','graph_ir.py','input.mlir' if i==0 else 'upstream.mlir'],
            resources={'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},artifacts={'file_mib':1,'total_mib':2})
        node={'key':'stage'+str(i),'job':job}
        if i:node.update(after=['stage'+str(i-1)],artifact_inputs=[{'node':'stage'+str(i-1),'name':'ir/canonical.mlir','target':'upstream.mlir'}])
        nodes.append(node)
    request={'name':'Installed graph restore lineage','idempotency_key':'installed-graph-restore','nodes':nodes}
    status,reply=api('/api/job-graphs',request);assert status==201,reply
    graph=reply['graph'];tasks=[]
    for node in graph['nodes']:
        job=ctx['wait'](node['job_id']);assert job['state']=='succeeded',job
        detail=api('/api/jobs/'+job['id'])[1];assert len(detail['attempts'])==1
        code,data=api('/api/jobs/'+job['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)
        assert code==200 and b'arith.constant 42' in data
        tasks.append({'job_id':job['id'],'attempt_id':job['attempt_id']})
    metadata=ctx['docker'](['exec',controller,'/usr/bin/python3','-I','-c','from pathlib import Path;print(Path("/opt/ascend-lab-controller/installation.json").read_text())']).stdout
    ctl(controller,'controller','stop')
    denied=ctl(controller,'controller','restore','--archive','/root/backups/pre-graph.tar.gz',
        '--new-state-dir',str(base/'graph-old-snapshot-refused'),check=False)
    assert denied.returncode!=0 and 'graph admission' in denied.stderr
    same=ctx['docker'](['exec',controller,'/usr/bin/python3','-I','-c','from pathlib import Path;print(Path("/opt/ascend-lab-controller/installation.json").read_text())']).stdout
    assert metadata==same and ctl(controller,'controller','start')['ready']
    status,old=api('/api/job-graphs/'+graph['id']);assert status==200 and old['graph']['id']==graph['id']
    return {'passed':True,'graph_id':graph['id'],'request':request,'tasks':tasks,
        'older_snapshot_without_graph_refused':True,'installation_metadata_unchanged_after_refusal':True}


def after_restore(ctx,proof):
    api=ctx['api'];status,value=api('/api/job-graphs/'+proof['graph_id'])
    assert status==200 and value['graph']['state']=='submitted'
    assert {n['job_id'] for n in value['graph']['nodes']}=={j['job_id'] for j in proof['tasks']}
    for task in proof['tasks']:
        status,detail=api('/api/jobs/'+task['job_id']);assert status==200
        assert detail['job']['state']=='succeeded' and detail['job']['attempt_id']==task['attempt_id'] and len(detail['attempts'])==1
    status,replay=api('/api/job-graphs',proof['request'])
    assert status==200 and replay['reused'] and replay['graph']['id']==proof['graph_id']
    return {'same_graph_and_original_stage_ids':True,'same_single_attempts':True,'original_request_replay_free':True,'graph_resubmitted_for_execution':False}

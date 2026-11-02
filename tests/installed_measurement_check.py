"""Actual installed CPU markers, task UID socket isolation and Worker restart.

No synthetic NPU execution Node or device is registered. All observations are
not_applicable; only the real IPC/lifecycle/IR paths are accepted here.
"""
import base64
import hashlib
import json
import time


def run(ctx):
    api=ctx['api'];wait=ctx['wait'];docker=ctx['docker'];node=ctx['node'];ctl=ctx['ctl'];node_state=ctx['node_state']
    def submit(hold):
        wrapper='''import json,runpy,time
from pathlib import Path
marker=runpy.run_path('/payload/npu_measurement.py')['request']
first=marker('begin','cpu_ir')
assert first['ok']
assert marker('begin','cpu_ir')==first
time.sleep(HOLD)
runpy.run_path('ir.py')
last=marker('end','cpu_ir')
Path('/work/results/window-client.json').write_text(json.dumps({'begin':first,'end':last}))
'''.replace('HOLD',str(hold)).encode()
        source={'ir.py':ctx['source']['ir.py'],'input.mlir':ctx['source']['input.mlir'],'window.py':wrapper}
        status,response=api('/api/inputs',{'files':[{'name':n,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for n,raw in source.items()]})
        assert status in {200,201},response
        identifier=response['input']['id']
        for name,raw in source.items():assert api('/api/inputs/'+identifier+'/blob',{'name':name,'offset':0,'data':base64.b64encode(raw).decode()})[0]==200
        assert api('/api/inputs/'+identifier+'/seal',{})[1]['input']['sealed']
        request={'template':'ir_v1','environment':'worker-python','name':'CPU measurement protocol '+str(hold),
            'parameters':{'input_id':identifier,'argv':['python','window.py'],
                'lock':{'python':ctx['task_version'],'image_sha256':ctx['task_image']},'timeout_seconds':60,
                'artifacts':{'file_mib':2,'total_mib':4}},'idempotency_key':'installed-measurement-'+str(hold)}
        status,response=api('/api/jobs',request);assert status==201,response
        return response['job']['id']
    first=wait(submit(0));assert first['state']=='succeeded',first
    normalized=first['device_conditions']['measurement'];assert normalized['status']=='recorded' and normalized['queries']==0
    assert normalized['windows'][0]['status']=='not_applicable' and not normalized['performance_qualified']
    control=node_state/'attempts'/first['attempt_id']/'control'
    journal=ctx['secret_json'](node,control/'measurement.json')
    assert journal['connections']==3 and journal['queries']==0 and journal['windows'][0]['state']=='closed'
    assert b'arith.constant 42' in api('/api/jobs/'+first['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    second_id=submit(18);deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        second=api('/api/jobs/'+second_id)[1]['job']
        if second.get('attempt_id'):
            control=node_state/'attempts'/second['attempt_id']/'control'
            try:journal=ctx['secret_json'](node,control/'measurement.json')
            except (RuntimeError,ValueError):journal={}
            if any(w['state']=='open' for w in journal.get('windows',[])):break
        time.sleep(.2)
    else:raise AssertionError('Actual CPU task did not open its measurement window')
    name='ascend-lab-'+second['attempt_id'][8:]
    identity=docker(['exec',node,'docker','inspect','--format','{{.Id}} {{.State.StartedAt}} {{index .Config.Labels "io.ascend-lab.attempt"}}',name]).stdout.strip()
    assert identity.endswith(second['attempt_id'])
    # Only this fixture's socket is temporarily made connectable to demonstrate
    # the actual SO_PEERCRED check even when DAC is deliberately permissive.
    endpoint=control/'measurement-endpoint/endpoint.sock'
    chmod='import os,sys;os.chmod(sys.argv[1],int(sys.argv[2],8))'
    docker(['exec',node,'/usr/bin/python3','-I','-c',chmod,str(endpoint),'666'])
    foreign='''import json,socket
from pathlib import Path
c=json.loads(Path('/payload/run.json').read_text());b=c['measurement']
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
 s.connect(b['socket']);s.sendall(json.dumps({'operation':'begin','window_id':'foreign','token':b['token'],'job_id':c['job_id'],'attempt_id':b['attempt_id']}).encode()+b'\\n')
 r=json.loads(s.recv(8192));assert r['ok'] is False
print('Foreign UID refused')
'''
    try:
        rejected=docker(['exec',node,'docker','exec','--user','23004:23004',name,'/usr/bin/python3','-I','-c',foreign])
        assert rejected.stdout.strip()=='Foreign UID refused'
    finally:docker(['exec',node,'/usr/bin/python3','-I','-c',chmod,str(endpoint),'600'])
    before=ctx['secret_json'](node,control/'measurement.json');assert len(before['windows'])==1 and before['queries']==0
    ctl(node,'node','stop');ctl(node,'node','start')
    assert docker(['exec',node,'docker','inspect','--format','{{.Id}} {{.State.StartedAt}} {{index .Config.Labels "io.ascend-lab.attempt"}}',name]).stdout.strip()==identity
    second=wait(second_id);assert second['state']=='succeeded',second
    normalized_second=second['device_conditions']['measurement']
    assert normalized_second['status']=='unconfirmed' and normalized_second['windows'][0]['status']=='interrupted'
    client=api('/api/jobs/'+second_id+'/artifact?name=results%2Fwindow-client.json')[1]
    assert client['begin']['ok'] and client['end']['status']=='interrupted'
    assert b'arith.constant 42' in api('/api/jobs/'+second_id+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    return {'passed':True,'job_ids':[first['id'],second_id],'actual_cpu_ir_constant':42,'task_uid':23003,
        'foreign_uid':23004,'foreign_peer_credentials_rejected':True,'idempotent_begin_no_extra_query':True,
        'same_container_started_at_after_node_restart':True,'original_task_result_preserved':True,
        'npu_queries':0,'npu_kernel_launches':0,'performance_qualified':False,
        'snapshots':{first['id']:normalized,second_id:normalized_second}}


def after_restore(ctx,proof):
    for identifier,expected in proof['snapshots'].items():
        job=ctx['api']('/api/jobs/'+identifier)[1]['job'];assert job['state']=='succeeded'
        assert job['device_conditions']['measurement']==expected
        assert not job['resources']['npu']
        assert b'arith.constant 42' in ctx['api']('/api/jobs/'+identifier+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    return {'passed':True,'original_window_evidence_preserved':True,'no_new_execution_or_measurement':True,'npu_kernel_launches':0}

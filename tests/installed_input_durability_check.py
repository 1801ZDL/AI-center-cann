"""Real HTTP acknowledgements across SIGKILL of the owned installed controller."""
import base64
import copy
import hashlib
import json
import time


def run(ctx):
    api=ctx['api'];ctl=ctx['ctl'];controller=ctx['controller'];docker=ctx['docker']
    data=b'v'*(2*1024**2);correct=b'correct-reset-bytes';wrong=b'x'*len(correct)
    script=b'''import hashlib,json,os,shutil,subprocess
from pathlib import Path
assert Path('data/nested/payload.bin').read_bytes()==b'v'*(2*1024**2)
assert Path('reset/data.bin').read_bytes()==b'correct-reset-bytes'
assert Path('empty/zero').read_bytes()==b''
out=Path(os.environ['LAB_JOB_DIR'])/'ir/canonical.mlir'
subprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir']}))
'''
    sources={'verify.py':script,'input.mlir':(ctx['ROOT']/'examples/workflows/input.mlir').read_bytes(),
             'data/nested/payload.bin':data,'reset/data.bin':correct,'empty/zero':b''}
    status,value=api('/api/inputs',{'files':[{'name':n,'size':len(v),'sha256':hashlib.sha256(v).hexdigest()} for n,v in sources.items()]})
    assert status==201,value
    identifier=value['input']['id'];route='/api/inputs/'+identifier
    def send(name,offset,part):
        status,result=api(route+'/blob',{'name':name,'offset':offset,'data':base64.b64encode(part).decode()})
        assert status==200,result
        return result
    chunk=256*1024;assert send('data/nested/payload.bin',0,data[:chunk])['size']==chunk
    crashes=[]
    def crash():
        before=ctl(controller,'controller','status');assert before['running'] and before['ready']
        # Metadata + live identity + pidfd bind the signal to this fixture's
        # actual service process. No name-based kill or global host action.
        code='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,'/opt/ascend-lab-controller/current')
from jobs import identity_alive
m=json.loads(Path('/opt/ascend-lab-controller/installation.json').read_text())
s=json.loads((Path(m['state_dir'])/'service-process.json').read_text())
assert s['pid']==int(sys.argv[1]) and s['uid']==m['uid'] and identity_alive(s)
fd=os.pidfd_open(s['pid'])
try:
 assert identity_alive(s)
 signal.pidfd_send_signal(fd,signal.SIGKILL)
finally:os.close(fd)
print(json.dumps({'pid':s['pid'],'uid':s['uid'],'start_ticks':s['start_ticks'],'signal':'SIGKILL'}))
'''
        identity=str(ctx['installed']['uid'])+':'+str(ctx['installed']['gid'])
        saved=json.loads(docker(['exec','--user',identity,controller,'/usr/bin/python3','-I','-c',code,str(before['pid'])]).stdout)
        deadline=time.monotonic()+8
        while ctl(controller,'controller','status')['running']:
            assert time.monotonic()<deadline,'Killed owned service still reports live'
            time.sleep(.1)
        after=ctl(controller,'controller','start');assert after['ready'] and after['pid']!=before['pid']
        saved['new_pid']=after['pid'];crashes.append(saved)
    crash()
    assert api(route)[1]['uploaded']['data/nested/payload.bin']==chunk
    assert send('data/nested/payload.bin',0,data[:chunk])['reused']
    offset=chunk
    while offset<len(data):
        part=data[offset:offset+chunk];send('data/nested/payload.bin',offset,part);offset+=len(part)
    for name in ['verify.py','input.mlir']:send(name,0,sources[name])
    send('reset/data.bin',0,wrong)
    assert api(route+'/seal',{})[0]==409
    assert api(route+'/reset-file',{'name':'reset/data.bin'})[1]['reset']
    crash()
    progress=api(route)[1];assert not progress['sealed'] and 'reset/data.bin' not in progress['uploaded']
    assert progress['uploaded']['data/nested/payload.bin']==len(data)
    send('reset/data.bin',0,correct);assert api(route+'/seal',{})[1]['input']['sealed']
    value=copy.deepcopy(ctx['request']);value.update(idempotency_key='installed-input-sigkill-recovery',name='Input SIGKILL recovery IR')
    value['parameters'].update(input_id=identifier,argv=['python','verify.py'],artifacts={'file_mib':8,'total_mib':16})
    status,response=api('/api/jobs',value);assert status==201,response
    job=ctx['wait'](response['job']['id']);assert job['state']=='succeeded',job
    assert len(api('/api/jobs/'+job['id'])[1]['attempts'])==1 and not job['allocation']['npu_ids']
    assert b'arith.constant 42' in api('/api/jobs/'+job['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    return {'passed':True,'input_id':identifier,'data_bytes':len(data),'data_sha256':hashlib.sha256(data).hexdigest(),
        'acknowledged_prefix_survived_sigkill':True,'identical_chunk_retry_reused':True,
        'reset_ack_survived_sigkill':True,'other_file_offset_preserved':True,'sealed_after_resume':True,
        'actual_ir_job':job['id'],'consumer_attempts':1,'npu_used':False,'process_crashes':crashes,
        'scope':'Owned installed non-root controller SIGKILL and HTTP recovery on dedicated ext4 with owner quotas; not host power loss or hardware crash acceptance'}

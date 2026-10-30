"""Actual installed controller/Node/cache workflow checks; own fixtures only."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import time


def upload(ctx,headers=None):
    api=ctx['api'];source={n:(ctx['ROOT']/'examples/workflows'/n).read_bytes() for n in ['cache_ir.py','input.mlir']}
    status,value=api('/api/inputs',{'files':[{'name':n,'size':len(v),'sha256':hashlib.sha256(v).hexdigest()} for n,v in source.items()]},extra_headers=headers)
    assert status in {200,201},value
    bundle=value['input']['id']
    for name,data in source.items():assert api('/api/inputs/'+bundle+'/blob',{'name':name,'offset':0,'data':base64.b64encode(data).decode()},extra_headers=headers)[0]==200
    assert api('/api/inputs/'+bundle+'/seal',{},extra_headers=headers)[1]['input']['sealed']
    return bundle


def submit(ctx,request,key,headers=None,args=(),environment=None):
    value=copy.deepcopy(request);value['idempotency_key']=key
    value['parameters']['argv']=['python','cache_ir.py',*args]
    if environment is not None:value['parameters']['environment']=environment
    status,result=ctx['api']('/api/jobs',value,extra_headers=headers);assert status==201,result
    return result['job']['id']


def result(ctx,job_id,headers=None):
    job=ctx['wait'](job_id,headers=headers);assert job['state']=='succeeded',job
    details=ctx['api']('/api/jobs/'+job_id,extra_headers=headers)[1];assert len(details['attempts'])==1
    artifact=lambda name:ctx['api']('/api/jobs/'+job_id+'/artifact?name='+name,extra_headers=headers)[1]
    record=artifact('results%2Fworkload-result.json');metadata=artifact('environment.json')
    assert metadata['persistent_cache']['enforced'] and metadata['workspace']['cache_mode']=='persistent-private-uid-quota'
    assert metadata['persistent_cache']['key']==job['allocation']['cache']['key']
    assert metadata['persistent_cache']['verified_identity']['uid']==metadata['task_uid']
    assert any(m['destination']=='/cache' and m['writable'] for m in metadata['mounts'])
    assert job['result']['cache_release_confirmed'] and record['calls']==1
    return job,record,metadata


def cache_node(ctx):return next(n for n in ctx['api']('/api/nodes')[1]['nodes'] if n['id']==ctx['enrollment']['node_id'])


def reading(ctx,uid):
    slot=next(s for s in ctx['cache_policy']['slots'] if s['quota_uid']==uid)
    return next(r for r in cache_node(ctx)['persistent_cache']['slots'] if r['slot_id']==slot['id'])


def run(ctx):
    api=ctx['api'];ctl=ctx['ctl'];docker=ctx['docker'];node=ctx['node'];request=copy.deepcopy(ctx['request'])
    def cache_ctl(action,*args,check=True):
        deadline=time.monotonic()+5
        while True:
            value=ctl(node,'node',action,*args,check=False)
            if value.returncode and 'Cache control transaction is in use' in value.stderr and time.monotonic()<deadline:
                time.sleep(.05);continue
            if not check:return value
            if value.returncode:raise RuntimeError('Cache administrative check failed: '+value.stderr[-1200:])
            return json.loads(value.stdout)
    request['parameters'].update(input_id=upload(ctx),cache={'mode':'persistent'},artifacts={'file_mib':8,'total_mib':16})
    cold_id=submit(ctx,request,'installed-cache-cold');cold,cold_result,metadata=result(ctx,cold_id);assert cold_result['cache_hit'] is False
    warm_id=submit(ctx,request,'installed-cache-warm');warm,warm_result,_=result(ctx,warm_id);assert warm_result['cache_hit'] is True
    assert cold['allocation']['cache']==warm['allocation']['cache'] and warm_result['ir_sha256']==cold_result['ir_sha256']
    allocation=cold['allocation']['cache'];slot_id=allocation['slot_id'];key=allocation['key']
    old_plan=cache_ctl('cache-plan','--slot-id',slot_id,'--key',key)['plan']
    hold_id=submit(ctx,request,'installed-cache-reconnect',args=['--hold']);deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        active=api('/api/jobs/'+hold_id)[1]['job']
        if active.get('attempt_id') and active['state']=='running':break
        time.sleep(.2)
    assert active['state']=='running',active
    container='ascend-lab-'+active['attempt_id'][8:]
    before=json.loads(docker(['inspect',container]).stdout)[0];assert before['State']['Running']
    denied=cache_ctl('cache-plan','--slot-id',slot_id,'--key',key,check=False)
    assert denied.returncode and 'lease' in denied.stderr.lower()
    ctl(node,'node','stop');assert ctl(node,'node','start')['ready']
    after=json.loads(docker(['inspect',container]).stdout)[0]
    assert after['State']['Running'] and after['State']['StartedAt']==before['State']['StartedAt']
    held,held_result,_=result(ctx,hold_id);assert held_result['cache_hit'] and held['attempt_id']==active['attempt_id']
    save='import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600)'
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save,'/provision/cache-stale-plan.json'],input=json.dumps(old_plan))
    denied=cache_ctl('cache-prune','--plan','/provision/cache-stale-plan.json',check=False)
    assert denied.returncode and 'stale' in denied.stderr.lower()
    peer_request=copy.deepcopy(request);peer_request['parameters']['input_id']=upload(ctx,ctx['peer_headers'])
    pressure=[]
    for kind,flag,free_key in [('bytes','--fill-bytes','free_bytes'),('inodes','--fill-inodes','free_inodes')]:
        fill_id=submit(ctx,request,'installed-cache-pressure-'+kind,args=[flag]);fill_job,filled,_=result(ctx,fill_id)
        inventory=filled['pressure'];assert inventory['errno']==122 and inventory['kind']==kind and inventory['files']
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=reading(ctx,23003)
            if state.get(free_key)==0 and state.get('leased') is False:break
            time.sleep(.2)
        assert state[free_key]==0 and state['leased'] is False,state
        waiting_id=submit(ctx,request,'installed-cache-waiting-'+kind);time.sleep(.8)
        waiting=api('/api/jobs/'+waiting_id)[1];assert waiting['job']['state']=='queued' and not waiting['attempts']
        peer_id=submit(ctx,peer_request,'installed-cache-peer-'+kind,headers=ctx['peer_headers'])
        peer,peer_result,peer_metadata=result(ctx,peer_id,ctx['peer_headers']);assert peer_metadata['task_uid']==23004
        assert peer['allocation']['cache']['key']!=key and peer['allocation']['cache']['storage_id']!=allocation['storage_id']
        from lab_client import atomic_json
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/cache-ui.json',{'source_parent':Path(os.environ['LAB_JOB_DIR']).name,
            'scope':'Archived real installed owner cache full; not live Main enrollment','node':cache_node(ctx),'job':waiting['job']})
        slot=next(s for s in ctx['cache_policy']['slots'] if s['id']==slot_id)
        target=str(Path(slot['path'])/key/'triton')
        cleanup='import json,os,sys;from pathlib import Path;p=Path(sys.argv[1]);items=json.load(sys.stdin)["files"]\nfor item in items:\n name=item["name"];assert name=="pressure.bin" or name.startswith("pressure-inode-");assert "/" not in name;s=(p/name).lstat();assert (s.st_dev,s.st_ino,s.st_size,s.st_uid,s.st_nlink)==(item["device"],item["inode"],item["bytes"],23003,1)\nfor item in items:(p/item["name"]).unlink()'
        docker(['exec','-i',node,'/usr/bin/python3','-I','-c',cleanup,target],input=json.dumps(inventory))
        resumed,resumed_result,_=result(ctx,waiting_id);assert resumed_result['cache_hit'] and resumed['id']==waiting_id
        pressure.append({'kind':kind,'native_errno':122,'pressure_job':fill_id,'waiting_job':waiting_id,
            'waiting_attempts_before_cleanup':0,'peer_job':peer_id,'peer_cache_hit':peer_result['cache_hit'],'same_waiting_job_resumed':True})
    ctl(node,'node','stop');docker(['exec',node,'umount',ctx['cache_path']])
    assert ctl(node,'node','start')['ready'];deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        failed=reading(ctx,23003)
        if failed.get('status')=='error':break
        time.sleep(.2)
    assert failed['status']=='error' and failed['free_bytes']==failed['free_inodes']==0
    blocked_id=submit(ctx,request,'installed-cache-missing-native-mount');time.sleep(.8)
    blocked=api('/api/jobs/'+blocked_id)[1];assert blocked['job']['state']=='queued' and not blocked['attempts']
    normal={**ctx['request'],'idempotency_key':'installed-cache-missing-ephemeral-progress'}
    status,value=api('/api/jobs',normal);assert status==201,value
    normal_job=ctx['wait'](value['job']['id']);assert normal_job['state']=='succeeded' and 'cache' not in normal_job['allocation']
    check_empty='from pathlib import Path;import sys;assert not list(Path(sys.argv[1]).iterdir())'
    docker(['exec',node,'/usr/bin/python3','-I','-c',check_empty,ctx['cache_path']])
    ctl(node,'node','stop');docker(['exec',node,'mount','--bind','/cache-proof-alias',ctx['cache_path']]);assert ctl(node,'node','start')['ready']
    resumed,resumed_result,_=result(ctx,blocked_id);assert resumed_result['cache_hit']
    changed_id=submit(ctx,request,'installed-cache-new-environment',environment={'TRITON_DEBUG':'1'})
    changed,changed_result,_=result(ctx,changed_id);assert not changed_result['cache_hit'] and changed['allocation']['cache']['key']!=key
    plan=cache_ctl('cache-plan','--slot-id',slot_id,'--key',changed['allocation']['cache']['key'])['plan']
    docker(['exec','-i',node,'/usr/bin/python3','-I','-c',save,'/provision/cache-gc-plan.json'],input=json.dumps(plan))
    assert cache_ctl('cache-prune','--plan','/provision/cache-gc-plan.json')['removed']
    listing=cache_ctl('cache-list');keys=next(s for s in listing['slots'] if s['slot_id']==slot_id)['keys']
    assert [v['key'] for v in keys]==[key]
    return {'passed':True,'request':request,'cold_job':cold_id,'warm_job':warm_id,'cache_key':key,'cache_slot_id':slot_id,
        'cold_invocations':cold_result['calls'],'warm_invocations':warm_result['calls'],'ir_sha256':cold_result['ir_sha256'],
        'active_gc_rejected':True,'stale_gc_rejected':True,'active_node_restart_same_attempt':True,
        'same_container_started_at':before['State']['StartedAt'],'reconnect_job':hold_id,'native_quota_pressure':pressure,
        'lost_mount_queued_job':blocked_id,'lost_mount_zero_attempts':True,'lost_mount_no_directory_fallback':True,
        'ephemeral_progress_while_cache_lost':normal_job['id'],'original_mount_cache_reused':True,
        'different_environment_key':changed['allocation']['cache']['key'],'idle_namespace_pruned':True,
        'task_uid':metadata['task_uid'],'scope':'Actual installed UID-quota cache and protected Docker /cache bootstrap; fixed CPU IR cache example, not Triton JIT/ccache/NPU acceptance'}


def after_restore(ctx,proof):
    request=copy.deepcopy(proof['request'])
    job_id=submit(ctx,request,'installed-cache-warm-after-node-state-restore')
    job,record,_=result(ctx,job_id)
    assert record['cache_hit'] and record['calls']==1 and record['ir_sha256']==proof['ir_sha256']
    assert job['allocation']['cache']['key']==proof['cache_key']
    proof['same_cache_retained_after_node_control_restore']=True
    return job_id

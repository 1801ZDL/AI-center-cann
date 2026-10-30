"""Supplemental actual unsealed HTTP upload during installed quota resize."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def actor():
    import urllib.request
    sys.path.insert(0,'/bootstrap')
    from lab_client import atomic_json,read_json
    from payload_groups import prepare
    from payload_quota_admin import plan,apply
    from payload_storage import root_fd
    import group_quota
    prefix='/opt/lab';state='/var/lib/lab';path=Path('/srv/payload')
    assert sorted(p.name for p in path.iterdir())==['lost+found'];(path/'lost+found').rmdir();path.chmod(0o700)
    stat=path.stat();space=os.statvfs(path)
    policy={'path':str(path),'filesystem':'ext4','storage_id':uuid.uuid4().hex,'device':stat.st_dev,'root_inode':stat.st_ino,
        'bytes':space.f_blocks*space.f_frsize,'inodes':space.f_files,'owner_groups':{
        'alice':{'gid':23841,'bytes':32*1024**2,'inodes':512},'bob':{'gid':23842,'bytes':32*1024**2,'inodes':512}}}
    prepare(policy);atomic_json('/root/payload.json',policy)
    def ctl(*args):
        r=subprocess.run(['/usr/bin/python3','-B','/bootstrap/servicectl',*args],capture_output=True,text=True,timeout=25)
        if r.returncode:raise RuntimeError('Fixed pending-upload administration failed: '+r.stderr[-900:])
        return r.stdout
    ctl('install','--archive','/provision/server.tar.gz','--prefix',prefix,'--state-dir',state,'--role','controller',
        '--uid','23840','--gid','23840','--payload-config','/root/payload.json','--port','18766')
    for owner in ['alice','bob']:ctl('admin','--prefix',prefix,'--','accessctl','user-add',owner)
    profiles={owner:read_json(Path(state)/'personas'/owner/'client.json') for owner in ['alice','bob']}
    ctl('start','--prefix',prefix)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(owner,route,body=None):
        data=None if body is None else json.dumps(body).encode()
        request=urllib.request.Request('http://127.0.0.1:18766'+route,data=data,headers={'Authorization':'Bearer '+profiles[owner]['token'],'Content-Type':'application/json'})
        with opener.open(request,timeout=10) as r:return json.load(r)
    data=b'p'*(20*1024**2);sha=hashlib.sha256(data).hexdigest()
    bundle=api('alice','/api/inputs',{'files':[{'name':'pending.bin','size':len(data),'sha256':sha}]})['input']['id']
    chunk=256*1024
    api('alice','/api/inputs/'+bundle+'/blob',{'name':'pending.bin','offset':0,'data':base64.b64encode(data[:chunk]).decode()})
    before=api('alice','/api/inputs/'+bundle);assert not before['sealed'] and before['uploaded']['pending.bin']==chunk
    peer=b'peer remains intact';peer_id=api('bob','/api/inputs',{'files':[{'name':'peer.txt','size':len(peer),'sha256':hashlib.sha256(peer).hexdigest()}]})['input']['id']
    api('bob','/api/inputs/'+peer_id+'/blob',{'name':'peer.txt','offset':0,'data':base64.b64encode(peer).decode()})
    assert api('bob','/api/inputs/'+peer_id+'/seal',{})['input']['sealed']
    ctl('stop','--prefix',prefix)
    with root_fd(policy,23840) as fd:
        usage=group_quota.read(fd,23841);peer_before=group_quota.read(fd,23842)
    # This target fits physical usage plus headroom, so only outstanding upload
    # reservations can make it unsafe. Inode limit remains unchanged.
    assert usage['bytes_used']+65536<16*1024**2 and usage['inodes_used']+16<512
    try:plan(prefix,'alice',16*1024**2,512)
    except ValueError as error:assert 'pending uploads' in str(error)
    else:raise AssertionError('Pending declared bytes were ignored during shrink')
    preview=plan(prefix,'alice',40*1024**2,512)['plan'];assert preview['pending_bytes']==len(data)
    changed=apply(prefix,preview);assert changed['receipt']['pending_upload_bytes_preserved']==len(data)
    with root_fd(preview['after'],23840) as fd:assert group_quota.read(fd,23842)==peer_before
    ctl('start','--prefix',prefix)
    resumed=api('alice','/api/inputs/'+bundle);assert resumed==before
    for offset in range(chunk,len(data),chunk):
        api('alice','/api/inputs/'+bundle+'/blob',{'name':'pending.bin','offset':offset,'data':base64.b64encode(data[offset:offset+chunk]).decode()})
    sealed=api('alice','/api/inputs/'+bundle+'/seal',{})['input'];assert sealed['sealed'] and sealed['uploaded']['pending.bin']==len(data)
    assert api('bob','/api/inputs/'+peer_id)['sealed']
    ctl('stop','--prefix',prefix)
    retained=path/'inputs'/bundle/'pending.bin';assert retained.stat().st_size==len(data) and hashlib.sha256(retained.read_bytes()).hexdigest()==sha
    print(json.dumps({'passed':True,'input_id':bundle,'peer_input_id':peer_id,'uploaded_prefix_bytes':chunk,'declared_bytes':len(data),
        'declared_sha256':sha,'pending_reservation_bytes':preview['pending_bytes'],'physical_owner_bytes_before':usage['bytes_used'],
        'unsafe_target_bytes':16*1024**2,'physical_usage_fits_unsafe_target':True,'pending_only_shrink_rejected':True,
        'upload_offset_preserved':True,'upload_completed_and_sealed':True,'peer_unchanged':True,'quota_receipt':changed['receipt'],
        'controller_uid':23840,'host_accounts_created':False,'kernel_launches':0,
        'scope':'Supplemental positive pending upload coverage on the exact v035 candidate; one installed isolated controller, no compute node or NPU'}),flush=True)


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
    from lab_client import read_json,atomic_json
    from disk_workspace_fixture import OwnedDisk
    from docker_runtime import discover,command
    from deployment import unpack_release,digest
    directory=Path(os.environ['LAB_JOB_DIR']);dispatch=read_json(directory/'dispatch.json');assert dispatch['resources']['npu']==0
    cpus=dispatch['allocation']['cpu_ids'];base=directory/'tmp/quota-pending';base.mkdir(mode=0o700)
    archive=root/'evidence/ascend-lab-server-v035.tar.gz';bootstrap,manifest=unpack_release(archive,base)
    binding,_=discover()
    def docker(args,check=True,input=None,timeout=30):
        argv,env=command(binding,['docker',*args]);r=subprocess.run(argv,env=env,capture_output=True,text=True,input=input,timeout=timeout)
        if check and r.returncode:raise RuntimeError('Fixed quota pending fixture failed: '+r.stderr[-1800:])
        return r
    prefix='ascend-lab-quota-pending-'+uuid.uuid4().hex[:8];name=prefix+'-controller';volume=prefix+'-payload'
    image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    disk=OwnedDisk(base,prefix,image,cpus,docker,folder_name='quota-workspace',image_mib=128,inodes=4096,quota_type='group')
    volume_created=False;container_created=False;result=None;report=None
    try:
        device=disk.create();docker(['volume','create','--label','io.ascend-lab.quota-pending='+prefix,'--driver','local',
            '--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev,grpquota',volume]);volume_created=True
        docker(['create','--name',name,'--label','io.ascend-lab.quota-pending='+prefix,'--user','0:0','--network','none','--read-only',
            '--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER','--cap-add','SETUID','--cap-add','SETGID',
            '--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined','--security-opt','apparmor=unconfined',
            '--memory','512m','--memory-swap','512m','--cpus','1.25','--cpuset-cpus',','.join(map(str,cpus)),'--pids-limit','64',
            '--tmpfs','/opt:rw,nosuid,nodev,size=32m,mode=755','--tmpfs','/var/lib:rw,nosuid,nodev,size=128m,mode=755',
            '--tmpfs','/root:rw,nosuid,nodev,size=16m,mode=700','--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777',
            '--log-opt','max-size=1m','--log-opt','max-file=1','--mount','type=volume,src='+volume+',dst=/srv/payload',
            '--mount','type=bind,src='+str(bootstrap)+',dst=/bootstrap,readonly',
            '--mount','type=bind,src='+str(archive)+',dst=/provision/server.tar.gz,readonly',
            '--mount','type=bind,src='+str(Path(__file__))+',dst=/probe.py,readonly',
            '--entrypoint','/usr/bin/timeout',image,'--kill-after=5s','180s','/usr/bin/python3','-I','/probe.py','--actor'])
        container_created=True;result=docker(['start','--attach',name],check=False,timeout=190)
        (base/'actor-stderr.log').write_text(result.stderr)
        detail=json.loads(docker(['inspect',name]).stdout)[0];assert not detail['State']['Running'] and detail['State']['Pid']==0
        assert result.returncode==detail['State']['ExitCode']==0,result.stderr[-1800:]
        report=json.loads(result.stdout);assert report['passed'];report.update(parent_job=directory.name,server_release=manifest['id'],server_archive_sha256=digest(archive))
    finally:
        if container_created:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels'].get('io.ascend-lab.quota-pending')==prefix
            docker(['rm','-f',name])
        if volume_created:docker(['volume','rm',volume])
        disk.cleanup()
        if report:
            report['disk']=disk.report;report['cleanup_errors']=[]
            atomic_json(directory/'results/quota-pending-validation.json',report)
    print(json.dumps({'passed':True,'parent_job':directory.name,'pending_reservation_bytes':report['pending_reservation_bytes'],'disk_released':report['disk']['loop_association_released']}))


if __name__=='__main__':
    if sys.argv[1:]==['--actor']:actor()
    else:parent()

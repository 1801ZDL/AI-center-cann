"""Actual nonzero byte/inode pressure in the owned controller test filesystem."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import tarfile
import sys

from control_storage import HOLD
from lab_client import Client,atomic_json


def run(ctx,node_state):
    api=ctx['api'];docker=ctx['docker'];ctl=ctx['ctl'];secret=ctx['secret_json'];check=ctx['check']
    controller=ctx['controller'];node=ctx['node']
    status,initial=api('/api/control-storage');assert status==200 and initial['status']=='ready'
    assert initial['minimum_free_bytes']==64*1024**2 and initial['minimum_free_inodes']==256
    ctl(node,'node','stop')
    ctl(controller,'controller','stop')
    # A real, previously published release, not current source with a module
    # deleted. Old intact installations stay valid, but cannot run enabled state.
    baseline='497cbba96256576f1c2a8a2da51c43a90fc2445d'
    archive=ctx['base']/'pre-headroom-source.tar';legacy_source=ctx['base']/'pre-headroom-source';legacy_source.mkdir(mode=0o700)
    subprocess.run(['git','archive','--format=tar','--output',str(archive),baseline],cwd=ctx['ROOT'],check=True,capture_output=True,timeout=20)
    from deployment import safe_relative
    with tarfile.open(archive) as source_archive:
        entries=source_archive.getmembers();assert len(entries)<500 and sum(e.size for e in entries)<8*1024**2
        for entry in entries:
            name=entry.name.rstrip('/') if entry.isdir() else entry.name
            assert safe_relative(name) and entry.name in {name,name+'/'}
            target=legacy_source/name
            if entry.isdir():target.mkdir(mode=0o755,parents=True,exist_ok=True);continue
            assert entry.isfile();target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            with source_archive.extractfile(entry) as source,target.open('xb') as destination:shutil.copyfileobj(source,destination)
            target.chmod(0o555 if entry.mode&0o111 else 0o444)
    assert not (legacy_source/'control_storage.py').exists()
    old_package=ctx['base']/'pre-headroom-server.tar.gz'
    packaged=subprocess.run([sys.executable,str(legacy_source/'servicectl'),'bundle','--output',str(old_package)],
        cwd=legacy_source,check=True,capture_output=True,text=True,timeout=20)
    old_release=json.loads(packaged.stdout)
    docker(['cp',str(old_package),controller+':/provision/pre-headroom-server.tar.gz'])
    installation=secret(controller,Path('/opt/ascend-lab-controller/installation.json'))
    refused=ctl(controller,'controller','activate','--archive','/provision/pre-headroom-server.tar.gz',check=False)
    assert refused.returncode and 'Control storage admission' in refused.stdout+refused.stderr
    assert secret(controller,Path('/opt/ascend-lab-controller/installation.json'))==installation
    old_plan=docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','doctor','--role','controller',
        '--archive','/provision/pre-headroom-server.tar.gz','--prefix','/opt/ascend-lab-old-plan','--state-dir','/central-pressure/old-state',
        '--uid',str(ctx['installed']['uid']),'--gid',str(ctx['installed']['gid'])],check=False)
    assert old_plan.returncode==2 and 'release_archive' in json.loads(old_plan.stdout)['blockers']
    check('Actual published pre-headroom code is refused for enabled-state activation and new installation; current release binding stays unchanged')
    # Exercise the restore gate too: create a stopped installation using the
    # actual old installer, then ask current maintenance to restore enabled
    # state into that old runtime. No old service is started or account added.
    docker(['cp',str(legacy_source),controller+':/provision/pre-headroom-bootstrap'])
    free_port=docker(['exec',controller,'/usr/bin/python3','-I','-c',
        "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()"] ).stdout.strip()
    legacy_prefix='/opt/ascend-lab-pre-headroom';legacy_state='/central-pressure/legacy-state'
    docker(['exec',controller,'/usr/bin/python3','-B','/provision/pre-headroom-bootstrap/servicectl','install',
        '--archive','/provision/pre-headroom-server.tar.gz','--prefix',legacy_prefix,'--state-dir',legacy_state,
        '--role','controller','--uid',str(ctx['installed']['uid']),'--gid',str(ctx['installed']['gid']),'--port',free_port])
    old_meta=secret(controller,Path(legacy_prefix)/'installation.json')
    restoration=docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','restore',
        '--prefix',legacy_prefix,'--archive','/root/backups/controller.tar.gz','--new-state-dir','/central-pressure/rejected-restore'],check=False)
    assert restoration.returncode and 'Control storage admission' in restoration.stdout+restoration.stderr
    assert secret(controller,Path(legacy_prefix)/'installation.json')==old_meta
    docker(['exec',controller,'/usr/bin/python3','-I','-c',"import os;assert not os.path.lexists('/central-pressure/rejected-restore')"])
    check('Enabled snapshot restore is refused against a real stopped pre-headroom runtime before publishing state or changing its installation binding')
    assert ctl(controller,'controller','start')['ready'] and ctl(node,'node','start')['ready']
    ctl(node,'node','stop')
    client=Client({'controller':ctx['controller_url'],'token':ctx['token']})
    source=ctx['base']/'headroom-source';source.mkdir(mode=0o700)
    script="""import json,os,uuid
from pathlib import Path
p=Path(os.environ['LAB_JOB_DIR']);(p/'ir/marker.json').write_text(json.dumps({'marker':uuid.uuid4().hex}))
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/marker.json']}))
"""
    (source/'task.py').write_text(script);bundle=client.upload_input(source)
    from environment_contract import lock_for
    request={'template':'ir_v1','environment':'worker-python','idempotency_key':'headroom-original',
             'parameters':{'input_id':bundle['id'],'argv':['python','task.py'],'lock':lock_for(ctx['configuration']['environment_manifest']),'timeout_seconds':45}}
    status,value=api('/api/jobs',request);assert status==201,value
    identifier=value['job']['id']
    def cpu(key):return {'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':key}
    following=[]
    for key in ['headroom-follower','headroom-cancelled']:
        status,value=api('/api/jobs',cpu(key));assert status==201,value
        following.append(value['job']['id'])
    node_auth={'Authorization':'Bearer '+ctx['configuration']['node_key']}
    status,value=api('/api/worker/claim',{},extra_headers=node_auth);assert status==200 and value['grant']['job_id']==identifier
    grant=value['grant'];attempt=grant['attempt_id'];auth={k:grant[k] for k in ['job_id','attempt_id','lease_token']}
    data=b'abcdef';files=[{'name':'partial.bin','size':6,'sha256':hashlib.sha256(data).hexdigest()}]
    status,value=api('/api/inputs',{'files':files});assert status==201,value
    partial=value['input']['id']
    def chunk(offset,part):return {'name':'partial.bin','offset':offset,'data':base64.b64encode(part).decode()}
    assert api('/api/inputs/'+partial+'/blob',chunk(0,data[:3]))[0]==200
    fill=r'''import json,os,sys
from pathlib import Path
p=Path('/central-pressure/headroom-filler');fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
target=int(sys.argv[1])-8*1024**2
try:
 while os.statvfs(p.parent).f_bavail*os.statvfs(p.parent).f_frsize>target:os.write(fd,b'x'*65536)
 os.fsync(fd)
finally:os.close(fd)
s=p.stat();v=os.statvfs(p.parent)
assert 0<v.f_bavail*v.f_frsize<int(sys.argv[1])
print(json.dumps({'device':s.st_dev,'inode':s.st_ino,'bytes':s.st_size,'free_bytes':v.f_bavail*v.f_frsize}))
'''
    filler=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',fill,str(initial['minimum_free_bytes'])]).stdout)
    status,low=api('/api/control-storage');assert status==200 and low['reason_code']=='control_storage_bytes'
    assert low['blocks_new_work'] and low['free_bytes']>0
    doctor=docker(['exec',controller,'/usr/bin/python3','-B','/bootstrap/servicectl','doctor','--role','controller',
        '--archive','/provision/server-v1.tar.gz','--prefix','/opt/ascend-lab-headroom-plan','--state-dir','/central-pressure/new-state',
        '--uid',str(ctx['installed']['uid']),'--gid',str(ctx['installed']['gid'])],check=False)
    plan=json.loads(doctor.stdout);assert doctor.returncode==2 and 'control_storage_admission' in plan['blockers'] and plan['read_only']
    untouched=docker(['exec',controller,'/usr/bin/python3','-I','-c',
        "import os;assert not os.path.lexists('/opt/ascend-lab-headroom-plan') and not os.path.lexists('/central-pressure/new-state')"])
    check('Actual Root doctor rejects the low-headroom control filesystem before creating prefix, state or service identity')
    for _ in range(2):
        status,ticket=api('/api/worker/start',auth,extra_headers=node_auth)
        assert status==200 and ticket['wait'] and not ticket['start_permitted'] and not ticket['already_authorized']
    status,recovered=api('/api/worker/claim',{},extra_headers=node_auth);assert status==200 and recovered['grant']==grant
    status,no_new=api('/api/worker/claim',{'exclude_attempts':[attempt]},extra_headers=node_auth)
    assert status==200 and no_new['grant'] is None and no_new['reason']==HOLD
    status,reused=api('/api/jobs',request);assert status==200 and reused['reused'] and reused['job']['id']==identifier
    status,error=api('/api/jobs',cpu('headroom-rejected-bytes'));assert status==503 and error['error']=='control_storage_pressure'
    other=[{'name':'new.bin','size':1,'sha256':hashlib.sha256(b'x').hexdigest()}]
    assert api('/api/inputs',{'files':other})[0]==503
    assert api('/api/inputs',{'files':files})[1]['reused']
    assert api('/api/inputs/'+partial+'/blob',chunk(0,data[:3]))[1]['reused']
    assert api('/api/inputs/'+partial+'/blob',chunk(3,data[3:]))[0]==503
    assert api('/api/inputs/'+partial)[1]['uploaded']['partial.bin']==3
    assert api('/api/jobs/'+following[1]+'/cancel',{})[0]==200
    assert api('/api/jobs/'+following[1])[1]['job']['state']=='cancelled'
    assert not api('/api/jobs/'+following[0])[1]['attempts']
    # A real Node must recover the original grant and wait on its unconsumed
    # start ticket, without a task container or replacement attempt.
    assert ctl(node,'node','start')['ready']
    detail=api('/api/jobs/'+identifier)[1];assert detail['job']['attempt_id']==attempt and len(detail['attempts'])==1
    name='ascend-lab-'+attempt[8:]
    absent=docker(['inspect',name],check=False);assert absent.returncode and 'No such object' in absent.stderr
    assert api('/api/jobs')[1]['reserved']['cpu']==1
    check('Nonzero byte pressure rejects growth, preserves input retry/offset and cancellation, and lets a real Node wait on the original unconsumed start ticket')
    profile=ctx['base']/'control-headroom-client.json'
    atomic_json(profile,{'controller':ctx['controller_url'],'token':ctx['token']})
    browser_result=ctx['base']/'control-headroom-browser.json'
    observed=subprocess.run(['/bin/bash',str(ctx['ROOT']/'check-browser.sh'),'tests/browser_control_headroom_check.py',
        '--profile',str(profile),'--output',str(browser_result)],cwd=ctx['ROOT'],capture_output=True,text=True,timeout=45)
    (ctx['base']/'control-headroom-browser.log').write_text(observed.stdout+observed.stderr)
    assert observed.returncode==0,'Inspect the private headroom browser log; no repeated task submission'
    browser=json.loads(browser_result.read_text());assert browser['passed'] and browser['created_jobs']==0 and not browser['page_errors']
    check('Packaged authenticated dashboard renders the actual control-pressure warning and preview blocker without submitting a job')
    # Install the portable client separately from private controller state. The
    # harness runs with a different real numeric UID and sees only its API token.
    home='/home/headroom-client';public='/opt/ascend-lab-headroom-client'
    setup=r'''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642)
f=p/'client.json';f.write_text(sys.stdin.read());f.chmod(0o600);os.chown(f,23642,23642)
'''
    docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',setup,home],input=json.dumps({'controller':ctx['controller_url'],'token':ctx['token']}))
    docker(['exec',controller,'/opt/ascend-lab-controller/current/install-client','--prefix',public])
    no_state='import os,sys;assert os.getuid()==23642 and not os.access(sys.argv[1],os.R_OK) and not os.path.exists("/var/run/docker.sock")'
    docker(['exec','--user','23642:23642',controller,'/usr/bin/python3','-I','-c',no_state,str(ctx['restored_state'])])
    peer='import os,sys;assert os.getuid()==23004 and not os.access(sys.argv[1],os.R_OK)'
    docker(['exec','--user','23004:23004',controller,'/usr/bin/python3','-I','-c',peer,home+'/client.json'])
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'queue_status','arguments':{}}},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'queue_status','arguments':{'path':'/etc/passwd'}}}]
    mcp=docker(['exec','-i','--user','23642:23642',controller,'/usr/bin/python3','-B',public+'/mcp_bridge.py',
        '--profile',home+'/client.json','--session-dir',home+'/session','--environment','worker-python'],
        input=''.join(json.dumps(v)+'\n' for v in messages),timeout=25)
    responses=[json.loads(line)['result'] for line in mcp.stdout.splitlines()]
    assert len(responses)==3 and not responses[1]['isError'] and responses[2]['isError']
    queue=json.loads(responses[1]['content'][0]['text'])
    assert queue['control_storage']['blocks_new_work'] and queue['control_storage']['reason_code']=='control_storage_bytes'
    assert 'jobs' not in queue and queue['session_jobs']==[]
    assert api('/api/jobs/'+identifier)[1]['job']['attempt_id']==attempt and not api('/api/jobs/'+following[0])[1]['attempts']
    check('Independent UID23642 installed MCP reads actual headroom pressure, cannot read controller state/socket, and rejects path overrides without submitting work')
    release=r'''import os,sys
from pathlib import Path
p=Path('/central-pressure/headroom-filler');s=p.stat();assert [s.st_dev,s.st_ino]==list(map(int,sys.argv[1:]))
p.unlink();fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
'''
    docker(['exec',controller,'/usr/bin/python3','-I','-c',release,str(filler['device']),str(filler['inode'])])
    done=ctx['wait'](identifier);later=ctx['wait'](following[0])
    assert done['state']==later['state']=='succeeded' and done['attempt_id']==attempt
    assert len(api('/api/jobs/'+identifier)[1]['attempts'])==len(api('/api/jobs/'+following[0])[1]['attempts'])==1
    assert api('/api/inputs/'+partial+'/blob',chunk(3,data[3:]))[0]==200
    assert api('/api/inputs/'+partial+'/seal',{})[1]['input']['sealed']
    assert api('/api/control-storage')[1]['status']=='ready'
    check('Restoring headroom consumes the same saved ticket once, executes each original task once and resumes the unchanged partial input')
    # Inode pressure is independent: plenty of bytes remain. Bound every file
    # to a private newly created directory and retain exact inode identities.
    ctl(node,'node','stop')
    status,value=api('/api/jobs',cpu('headroom-inode-original'));assert status==201,value
    inode_job=value['job']['id']
    fill_inodes=r'''import json,os
from pathlib import Path
p=Path('/central-pressure/headroom-inodes');p.mkdir(mode=0o700);s=p.stat();items=[]
while os.statvfs(p).f_favail>=128:
 assert len(items)<8192
 q=p/str(len(items));fd=os.open(q,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
 item=q.stat();items.append([q.name,item.st_ino])
v=os.statvfs(p);assert 0<v.f_favail<256 and v.f_bavail*v.f_frsize>64*1024**2
print(json.dumps({'device':s.st_dev,'inode':s.st_ino,'files':items,'free_bytes':v.f_bavail*v.f_frsize,'free_inodes':v.f_favail}))
'''
    inodes=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',fill_inodes],timeout=45).stdout)
    low_inodes=api('/api/control-storage')[1];assert low_inodes['blocks_new_work'] and low_inodes['reason_code']=='control_storage_inodes'
    assert api('/api/jobs',cpu('headroom-rejected-inodes'))[0]==503
    assert not api('/api/jobs/'+inode_job)[1]['attempts']
    cleanup=r'''import json,os,stat,sys
from pathlib import Path
v=json.loads(sys.stdin.read());p=Path('/central-pressure/headroom-inodes');s=p.lstat()
assert stat.S_ISDIR(s.st_mode) and [s.st_dev,s.st_ino]==[v['device'],v['inode']]
assert sorted(x.name for x in p.iterdir())==sorted(x[0] for x in v['files'])
for name,ino in v['files']:
 q=p/name;t=q.lstat();assert stat.S_ISREG(t.st_mode) and t.st_nlink==1 and t.st_size==0 and [t.st_dev,t.st_ino]==[s.st_dev,ino]
for name,ino in v['files']:(p/name).unlink()
fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd);p.rmdir()
fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
'''
    docker(['exec','-i',controller,'/usr/bin/python3','-I','-c',cleanup],input=json.dumps(inodes),timeout=45)
    assert api('/api/control-storage')[1]['status']=='ready'
    assert ctl(node,'node','start')['ready']
    assert ctx['wait'](inode_job)['state']=='succeeded' and len(api('/api/jobs/'+inode_job)[1]['attempts'])==1
    check('Actual inode headroom stops new work despite free bytes; removing only exact owned empty files restores the original queued task')
    return {'passed':True,'original_job_id':identifier,'original_attempt_id':attempt,'follower_job_id':following[0],
            'cancelled_zero_attempt_job':following[1],'inode_original_job_id':inode_job,'input_id':partial,
            'nonzero_byte_pressure':low,'inode_pressure':low_inodes,'byte_filler':filler,
            'inode_filler_files':len(inodes['files']),'start_ticket_waited_and_reused':True,
            'unchanged_input_prefix_resumed':True,'denials_http_status':503,'byte_and_inode_rejected_jobs_absent':True,
            'old_release_refused':{'baseline':baseline,'release':old_release['release_id'],'activation_binding_unchanged':True,'new_install_preflight_refused':True,'restore_to_old_runtime_refused':True,'restore_binding_unchanged':True},
            'installed_mcp':{'passed':True,'actual_uid':23642,'peer_uid':23004,'controller_state_unreadable':True,'profile_unreadable_by_peer':True,'queue_status_pressure_visible':True,'path_override_rejected':True,'submitted_jobs':0},
            'browser':browser,'scope':'Actual owned finite ext4 nonzero byte/inode pressure; not an exclusive reservation, hard quota, power-loss or production filesystem migration'}

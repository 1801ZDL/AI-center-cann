"""Installed root CLI quota resize and two actual administrative SIGKILLs."""
import copy
import json


def run(ctx):
    docker=ctx['docker'];controller=ctx['controller'];ctl=ctx['ctl'];api=ctx['api'];prefix='/opt/ascend-lab-controller'
    binary=prefix+'/current/servicectl'
    def command(*args,check=True):return docker(['exec',controller,binary,*args,'--prefix',prefix],check=check)
    def inspect():
        code='import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]+"/current");from deployment import load_installation,regular_json;from payload_storage import root_fd;import group_quota as q;p,m,r=load_installation(sys.argv[1]);s=Path(m["state_dir"]);c=regular_json(s/"config.json");policy=m["payload_storage"]\nwith root_fd(policy,m["uid"]) as fd: values={n:q.read(fd,a["gid"]) for n,a in policy["owner_groups"].items()}\nprint(json.dumps({"quotas":values,"revision":m.get("payload_quota_revision",0),"config_sha256":__import__("hashlib").sha256((s/"config.json").read_bytes()).hexdigest(),"config_uid":(s/"config.json").stat().st_uid,"journal":(p/"payload-quota-change.json").exists()}))'
        return json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',code,prefix]).stdout)
    assert ctx['api']('/api/jobs')[1]['counts']['running']==0
    before_ir=api('/api/jobs/'+ctx['first']['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]
    assert not ctl(controller,'controller','stop')['running']
    before=inspect();old=before['quotas']['proof_user'];peer=before['quotas']['proof_peer']
    def plan(name,mib,inodes):
        path='/root/'+name+'.json';r=command('payload-quota-plan','--owner','proof_user','--mib',str(mib),'--inodes',str(inodes),'--output',path)
        return path,json.loads(r.stdout)
    # Actual filesystem has enough aggregate spare capacity for an 8MiB grow.
    initial_mib=old['byte_hard']//1024**2
    stale_path,stale=plan('quota-stale',initial_mib+4,old['inode_hard']+128)
    grow_path,grow=plan('quota-grow',initial_mib+8,old['inode_hard']+256)
    assert inspect()==before,'Read-only plans changed quota, installation or private config'
    denied=docker(['exec','--user','23642:23642',controller,binary,'payload-quota-plan','--prefix',prefix,
        '--owner','proof_user','--mib',str(initial_mib+8),'--inodes',str(old['inode_hard']+256)],check=False)
    assert denied.returncode and 'requires root' in denied.stderr and inspect()==before
    invalid=command('payload-quota-plan','--owner','proof_user','--mib','16','--inodes','128',check=False)
    assert invalid.returncode and 'retained data' in invalid.stderr and inspect()==before
    crashes=[]
    def crash_apply(path,stage):
        code='import json,os,signal,sys;sys.path.insert(0,sys.argv[1]+"/current");import payload_quota_admin as a;from deployment import regular_json\ndef boundary(name):\n if name==sys.argv[3]:\n  print(json.dumps({"stage":name,"pid":os.getpid(),"uid":os.getuid(),"signal":"SIGKILL"}),flush=True);os.kill(os.getpid(),signal.SIGKILL)\na._checkpoint=boundary;a.apply(sys.argv[1],regular_json(sys.argv[2],private=True))'
        process=docker(['exec',controller,'/usr/bin/python3','-I','-c',code,prefix,path,stage],check=False)
        assert process.returncode==137,(process.returncode,process.stderr)
        identity=json.loads(process.stdout);assert identity['stage']==stage and identity['uid']==0
        crashes.append(identity)
        assert inspect()['journal']
        denied=command('verify',check=False);assert denied.returncode and 'pending payload quota' in denied.stderr
        denied=command('start',check=False);assert denied.returncode and 'pending payload quota' in denied.stderr
        denied=command('backup','--output','/root/forbidden-quota-backup.tar.gz',check=False);assert denied.returncode
    crash_apply(grow_path,'quota')
    grown=json.loads(command('payload-quota-apply','--plan',grow_path).stdout)
    assert grown['resumed'] and grown['applied'] and not inspect()['journal']
    assert inspect()['quotas']['proof_peer']==peer and inspect()['config_uid']==ctx['installed']['uid']
    repeat=json.loads(command('payload-quota-apply','--plan',grow_path).stdout);assert repeat['already_applied']
    denied=command('payload-quota-apply','--plan',stale_path,check=False);assert denied.returncode
    assert ctl(controller,'controller','start')['ready']
    assert api('/api/me')[1]['storage']['filesystem_quota']['byte_hard']==old['byte_hard']+8*1024**2
    grow_request=copy.deepcopy(ctx['request']);grow_request['idempotency_key']='actual-ir-after-quota-grow'
    status,value=api('/api/jobs',grow_request);assert status==201,value
    grow_job=ctx['wait'](value['job']['id']);assert grow_job['state']=='succeeded'
    assert not ctl(controller,'controller','stop')['running']
    shrink_path,shrink=plan('quota-shrink',initial_mib,old['inode_hard'])
    crash_apply(shrink_path,'config')
    shrunk=json.loads(command('payload-quota-apply','--plan',shrink_path).stdout);assert shrunk['resumed']
    after=inspect();assert after['revision']==before['revision']+2 and not after['journal']
    assert after['quotas']['proof_user']['byte_hard']==old['byte_hard'] and after['quotas']['proof_user']['inode_hard']==old['inode_hard']
    assert after['quotas']['proof_peer']==peer
    export='import base64,hashlib,json,sys;from pathlib import Path;p=Path(sys.argv[1])/"payload-quota-history";rows=[]\nfor f in sorted(p.glob("*.json")):\n s=f.stat();assert s.st_uid==0 and s.st_mode&0o777==0o600;b=f.read_bytes();rows.append({"name":f.name,"sha256":hashlib.sha256(b).hexdigest(),"bytes_base64":base64.b64encode(b).decode(),"uid":s.st_uid,"mode":s.st_mode&0o777})\nprint(json.dumps(rows))'
    receipts=json.loads(docker(['exec',controller,'/usr/bin/python3','-I','-c',export,prefix]).stdout)
    assert {r['name'] for r in receipts}=={grow['plan_id']+'.json',shrink['plan_id']+'.json'}
    from lab_client import atomic_json
    from pathlib import Path
    import os
    atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/quota-audit.json',{'source_parent':Path(os.environ['LAB_JOB_DIR']).name,'receipts':receipts})
    assert ctl(controller,'controller','start')['ready']
    assert api('/api/jobs/'+ctx['first']['id']+'/artifact?name=ir%2Fcanonical.mlir',raw=True)[1]==before_ir
    assert api('/api/me')[1]['storage']['filesystem_quota']['byte_hard']==old['byte_hard']
    peer_request=copy.deepcopy(ctx['request']);peer_request['parameters']['input_id']=ctx['peer_request']['parameters']['input_id']
    peer_request['idempotency_key']='actual-peer-ir-after-quota-shrink'
    status,value=api('/api/jobs',peer_request,extra_headers=ctx['peer_headers']);assert status==201,value
    peer_job=ctx['wait'](value['job']['id'],headers=ctx['peer_headers']);assert peer_job['state']=='succeeded'
    return {'passed':True,'before':before,'after':after,'grow':grown,'shrink':shrunk,'process_crashes':crashes,
        'plan_readonly':True,'unsafe_shrink_rejected':True,'stale_plan_rejected':True,'idempotent_apply':True,
        'pending_start_verify_backup_blocked':True,'peer_limits_and_usage_preserved':True,'old_ir_preserved':True,
        'grow_actual_ir_job':grow_job['id'],'peer_actual_ir_job':peer_job['id'],
        'audit_files_exported':2,
        'nonroot_resize_denied':True,
        'scope':'Existing central owner quota resize only; real offline installed root processes and group-quota ext4, not online resize, new-owner provisioning or host power-loss'}

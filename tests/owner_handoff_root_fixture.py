"""Inside an owned Root role: real non-root central service for Node ACK cases."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid


class CentralSession:
    def __init__(self,config,context):
        import deployment as deploy
        from lab_client import atomic_json,read_json
        from payload_groups import prepare
        from payload_owner_admin import plan,apply
        self.prefix='/opt/handoff-controller';self.state=Path('/state/controller');self.node_prefix='/opt/owner-node';self.context=context
        Path('/state').chmod(0o755)
        root=Path('/central-quota');root.chmod(0o700)
        if (root/'lost+found').exists():(root/'lost+found').rmdir()
        info=root.stat();space=os.statvfs(root)
        policy={'path':str(root),'filesystem':'ext4','device':info.st_dev,'root_inode':info.st_ino,
            'bytes':space.f_blocks*space.f_frsize,'inodes':space.f_files,'storage_id':uuid.uuid4().hex,
            'owner_groups':{'old_owner':{'gid':23831,'bytes':16*1024**2,'inodes':128}}}
        prepare(policy);atomic_json(Path('/state/central-payload.json'),policy)
        deploy.install('/payload/server.tar.gz',self.prefix,str(self.state),'controller',uid=23820,gid=23820,
                       payload_config='/state/central-payload.json',port=18769)
        self.command('accessctl','user-add','old_owner')
        self.command('accessctl','environment-grant','old_owner','worker-python',json_output=False)
        spec=self.state/'new-owner.json';atomic_json(spec,{'user':'new_owner','environments':['worker-python'],
            'quota':{'input_storage_mib':4,'archive_storage_mib':32}});os.chown(spec,23820,23820)
        staged=self.command('accessctl','user-stage','--spec',str(spec));self.stage_id=staged['stage_id']
        value=plan(self.prefix,'new_owner',23832,64*1024**2,512)['plan'];apply(self.prefix,value)
        node_profile={k:copy.deepcopy(v) for k,v in config.items() if k not in {'node_id','node_key','controller','docker_runtime','state_dir'}}
        node_profile['name']='handoff_node';path=self.state/'node-profile.json';atomic_json(path,node_profile);os.chown(path,23820,23820)
        created=self.service_code('''import json,sys
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
state=Path(sys.argv[1]);j=JobStore(state,read_json(state/"config.json"));node,key=j.nodes.enroll(read_json(state/"node-profile.json"))
print(json.dumps({"node_id":node,"node_key":key}))
''')
        self.node_id=created['node_id'];self.config={**config,**created,'controller':'http://127.0.0.1:18769'}
        self.server=deploy.start(self.prefix);assert self.server['running']

    def command(self,*args,json_output=True):
        r=subprocess.run(['/usr/bin/python3','-I','-B',self.prefix+'/current/servicectl','admin','--prefix',self.prefix,'--',*args],
                         capture_output=True,text=True,timeout=30)
        if r.returncode:raise RuntimeError('Installed central management command rejected fixture: '+r.stderr[-1200:])
        return json.loads(r.stdout) if json_output else None

    def service_code(self,code):
        def drop():
            import deployment as deploy
            from payload_groups import gids
            os.setgroups(gids(deploy.verify(self.prefix)[1]['payload_storage']));os.setgid(23820);os.setuid(23820)
        wrapper='import sys;sys.path.insert(0,"'+self.prefix+'/current");'+code
        r=subprocess.run(['/usr/bin/python3','-I','-B','-c',wrapper,str(self.state)],capture_output=True,text=True,preexec_fn=drop,
                         env={'PATH':'/usr/bin:/bin','HOME':str(self.state),'LANG':'C.UTF-8'},timeout=30)
        if r.returncode:raise RuntimeError('Fixed central service operation failed: '+r.stderr[-1200:])
        return json.loads(r.stdout)

    def approve(self,path):
        from lab_client import read_json,atomic_json
        target=self.state/'owner-plan.json';atomic_json(target,read_json(path));os.chown(target,23820,23820)
        first=self.command('workerctl','owner-approve',self.node_id,'--plan',str(target))
        again=self.command('workerctl','owner-approve',self.node_id,'--plan',str(target))
        assert not first['reused'] and again['reused'] and first['approval']==again['approval']
        assert first['approval']['stage_id']==self.stage_id

    def finish(self,prefix,value,current):
        import deployment as deploy
        from lab_client import read_json
        from worker import Worker
        from node_owner_admin import AWAIT,projection,sha
        from node_owner_ack import PROGRESS
        faults=[]
        for boundary in ['intent','commit_response','committed','config_ready','ready_response','ready_saved','receipt','awaiting_removed','published']:
            killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,os,signal
sys.path.insert(0,"/payload/code");import node_owner_ack as a
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.complete("/opt/owner-node",sys.argv[2])
''',boundary,value['id']],capture_output=True,text=True,timeout=30)
            assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-1600:])
            if boundary!='published':
                try:deploy.verify(prefix)
                except deploy.DeployError:pass
                else:raise AssertionError('Partially persisted ACK unblocked Node startup')
            faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_replayed':True})
        final=subprocess.run(['/usr/bin/python3','-I','-B',prefix+'/current/servicectl','node-owner-ack','--prefix',prefix,'--plan-id',value['id']],
                             capture_output=True,text=True,check=True,timeout=30)
        acknowledged=json.loads(final.stdout);assert acknowledged['acknowledged'] and acknowledged['already_acknowledged']
        state=Path('/state/node');assert not (state/AWAIT).exists() and not (state/PROGRESS).exists()
        deploy.verify(prefix);config=read_json(state/'config.json');assert config['owner_bindings']['new_owner']['state']=='ready'
        before_beat=self.command('workerctl','owner-status',self.node_id)[0]
        assert before_beat['state']=='ready' and not before_beat['node_online_seen']
        worker=Worker(config)
        try:worker.heartbeat()
        finally:worker.lock.close();worker.state_lock.close()
        after_beat=self.command('workerctl','owner-status',self.node_id)[0]
        assert after_beat['state']=='ready' and after_beat['node_online_seen']
        status=self.command('accessctl','user-status','new_owner');assert status['disabled'] and status['state']=='staged'
        verified=self.service_code('''import json,sys
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
from node_owner_admin import projection,sha
s=Path(sys.argv[1]);j=JobStore(s,read_json(s/"config.json"));r=j.nodes.handoff.status("'''+self.node_id+'''")[0]
with j.access.connect() as db:n=db.execute("SELECT COUNT(*) FROM tokens WHERE user='new_owner'").fetchone()[0]
print(json.dumps({"profile_sha256":sha(projection(j.nodes.node(r["node_id"])["profile"])),"tokens":n,"user_auth_closed":j.access.user("new_owner") is None}))
''')
        assert verified['profile_sha256']==sha(projection(config)) and verified['tokens']==0 and verified['user_auth_closed']
        deploy.stop(self.prefix)
        return {'actual_central_contact':True,'fixture_stage_identity_only':False,'real_central_stage_definition_matched':True,
            'central_native_gid_appended':True,'central_approval_replay_stable':True,'node_ack_fault_chain':faults,
            'registered_and_local_projections_match':True,'node_unblocked_after_durable_ready_ack':True,
            'healthy_real_worker_heartbeat_observed':True,'identity_still_staged_and_token_count_zero':True,
            'central_nonroot_uid':23820,'accounts_enabled':False,'full_owner_onboarding_ready':False,
            'scope':'Same-host installed non-root controller plus Root Node, real stage/GID approval and authenticated HTTP commit/ready ACK, no user activation or queued new-owner workload. Environment enrolled by trusted fixture administrator; not a new protected environment-probe acceptance.'}

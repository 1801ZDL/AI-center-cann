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
        root=Path(context.get('central_path','/central-quota'));root.chmod(0o700)
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
        self.unit_proof={}
        if context.get('unit_refresh'):
            self.unit_dir=Path('/opt/fixture-systemd');self.unit_dir.mkdir(mode=0o755)
            self.root_command('unit-install','--prefix',self.prefix,'--unit-dir',str(self.unit_dir))
        value=plan(self.prefix,'new_owner',23832,64*1024**2,512)['plan'];apply(self.prefix,value)
        if context.get('unit_refresh'):self.unit_proof=self.refresh_unit()
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

    def root_command(self,*args):
        r=subprocess.run(['/usr/bin/python3','-I','-B',self.prefix+'/current/servicectl',*args],capture_output=True,text=True,timeout=30)
        if r.returncode:raise RuntimeError('Installed Root unit command rejected fixture: '+r.stderr[-1200:])
        return json.loads(r.stdout)

    def refresh_unit(self):
        import hashlib
        import deployment as deploy
        import unit_refresh as unit
        from state_lock import state_lock
        from lab_client import read_json,atomic_json
        installed=self.unit_dir/'ascend-lab-controller.service';original=installed.read_text();old_sha=hashlib.sha256(installed.read_bytes()).hexdigest()
        assert 'SupplementaryGroups=23831\n' in original
        with state_lock(self.state):
            try:unit.plan(self.prefix,str(self.unit_dir))
            except RuntimeError:pass
            else:raise AssertionError('Live controller lock allowed offline unit refresh')
        drop=self.unit_dir/'ascend-.service.d';drop.mkdir(mode=0o755)
        try:
            try:unit.plan(self.prefix,str(self.unit_dir))
            except deploy.DeployError:pass
            else:raise AssertionError('Prefix drop-in allowed automatic unit rewrite')
        finally:drop.rmdir()
        # Metadata identity, not just equal text, makes a pre-journal plan stale.
        stale=unit.plan(self.prefix,str(self.unit_dir))['plan'];unit._publish(installed,original,0o644)
        try:unit.apply(self.prefix,stale)
        except deploy.DeployError:pass
        else:raise AssertionError('Recreated unit bypassed CAS')
        assert not Path(self.prefix,unit.JOURNAL).exists()
        installed.write_text(original+'\n# custom setting\n');installed.chmod(0o644)
        try:unit.plan(self.prefix,str(self.unit_dir))
        except deploy.DeployError:pass
        else:raise AssertionError('Customized unit was adopted')
        installed.write_text(original);installed.chmod(0o644)
        output=Path('/state/unit-plan.json')
        preview=self.root_command('unit-refresh-plan','--prefix',self.prefix,'--unit-dir',str(self.unit_dir),'--output',str(output))
        value=read_json(output);assert preview['read_only'] and preview['unit_changed'] and installed.read_text()==original
        unrelated=self.unit_dir/'unrelated.service';unrelated.write_text('untouched fixture');sentinel=unrelated.read_bytes()
        faults=[]
        for boundary in ['journal','backup','installed','template','receipt','published']:
            killed=subprocess.run(['/usr/bin/python3','-I','-B','-c','''import sys,os,signal,json
sys.path.insert(0,"/opt/handoff-controller/current");import unit_refresh as u
def crash(stage):
 if stage==sys.argv[1]:os.kill(os.getpid(),signal.SIGKILL)
u._checkpoint=crash;u.apply("/opt/handoff-controller",json.load(open("/state/unit-plan.json")))
''',boundary],capture_output=True,text=True,timeout=30)
            assert killed.returncode==-signal.SIGKILL,(boundary,killed.stderr[-1200:])
            if boundary!='published':
                for recover in [False,True]:
                    try:deploy.verify(self.prefix,quota_recovery=recover)
                    except deploy.DeployError:pass
                    else:raise AssertionError('Pending unit refresh allowed normal verify/start/other quota maintenance')
                from maintenance import snapshot,activate
                for operation in [lambda:snapshot(self.prefix,'/opt/should-not-exist.tar.gz'),
                                  lambda:activate(self.prefix,release_id=value['binding']['release_id'])]:
                    try:operation()
                    except deploy.DeployError:pass
                    else:raise AssertionError('Pending unit refresh allowed backup or code activation')
            faults.append({'boundary':boundary,'sigkill_observed':True,'same_plan_resumed':True})
        replay=self.root_command('unit-refresh-apply','--prefix',self.prefix,'--plan',str(output));assert replay['already_applied']
        assert replay['daemon_reload_required'] and replay['service_restart_required'] and replay['systemd_manager_contacted'] is False
        expected=deploy.unit_text(self.prefix,deploy.verify(self.prefix)[1]);assert installed.read_text()==expected and 'SupplementaryGroups=23831 23832\n' in expected
        assert Path(self.prefix,'units',installed.name).read_text()==expected and installed.stat().st_mode&0o777==0o644
        history=Path(self.prefix,unit.HISTORY,value['id']);assert (history/'installed-before.service').read_text()==original
        assert (history/'template-before.service').read_text()==original and history.stat().st_mode&0o777==0o700
        assert (history/'installed-before.service').stat().st_mode&0o777==0o600 and unrelated.read_bytes()==sentinel
        try:self.root_command('unit-install','--prefix',self.prefix,'--unit-dir',str(self.unit_dir))
        except RuntimeError:pass
        else:raise AssertionError('Initial unit install overwrote an existing unit')
        import shutil
        proof=Path('/state/unit-refresh-proof');shutil.copytree(history,proof)
        (proof/'desired.service').write_text(expected);(proof/'desired.service').chmod(0o600)
        return {'passed':True,'fault_chain':faults,'old_groups':[23831],'new_groups':[23831,23832],
            'old_unit_sha256':old_sha,'desired_sha256':hashlib.sha256(expected.encode()).hexdigest(),
            'installed_cli_plan_apply':True,'shared_service_lock_rejected':True,'drop_in_rejected':True,'stale_inode_plan_rejected':True,
            'custom_unit_rejected':True,'old_unit_backups_exact':True,'backup_and_code_activation_blocked_while_pending':True,
            'unrelated_unit_unchanged':True,'systemd_manager_contacted':False,'systemd_pid1_tested':False,
            'scope':'Installed Root offline unit files only; real central process starts separately afterward, not through systemd PID1.'}

    def command(self,*args,json_output=True):
        r=subprocess.run(['/usr/bin/python3','-I','-B',self.prefix+'/current/servicectl','admin','--prefix',self.prefix,'--',*args],
                         capture_output=True,text=True,timeout=30)
        if r.returncode:raise RuntimeError('Installed central management command rejected fixture: '+r.stderr[-1200:])
        return json.loads(r.stdout) if json_output else None

    def service_run(self,code):
        def drop():
            import deployment as deploy
            from payload_groups import gids
            os.setgroups(gids(deploy.verify(self.prefix)[1]['payload_storage']));os.setgid(23820);os.setuid(23820)
        wrapper='import sys;sys.path.insert(0,"'+self.prefix+'/current");'+code
        r=subprocess.run(['/usr/bin/python3','-I','-B','-c',wrapper,str(self.state)],capture_output=True,text=True,preexec_fn=drop,
                         env={'PATH':'/usr/bin:/bin','HOME':str(self.state),'LANG':'C.UTF-8'},timeout=30)
        return r

    def service_code(self,code):
        r=self.service_run(code)
        if r.returncode:raise RuntimeError('Fixed central service operation failed: '+r.stderr[-1200:])
        return json.loads(r.stdout)

    def approve(self,path):
        from lab_client import read_json,atomic_json
        target=self.state/'owner-plan.json';atomic_json(target,read_json(path));os.chown(target,23820,23820)
        first=self.command('workerctl','owner-approve',self.node_id,'--plan',str(target))
        again=self.command('workerctl','owner-approve',self.node_id,'--plan',str(target))
        assert not first['reused'] and again['reused'] and first['approval']==again['approval']
        assert first['approval']['stage_id']==self.stage_id

    def personal_code(self,code,uid=23802):
        def drop():os.setgroups([]);os.setgid(uid);os.setuid(uid)
        wrapper='import sys;sys.path.insert(0,"'+self.prefix+'/current");'+code
        r=subprocess.run(['/usr/bin/python3','-I','-B','-c',wrapper,'/personal/new_owner'],capture_output=True,text=True,
            preexec_fn=drop,env={'PATH':'/usr/bin:/bin','HOME':'/personal/new_owner','LANG':'C.UTF-8'},timeout=30)
        if r.returncode:raise RuntimeError('Personal UID client operation failed: '+r.stderr[-1200:])
        return json.loads(r.stdout)

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
        state=Path(current.get('state_dir','/state/node'));assert not (state/AWAIT).exists() and not (state/PROGRESS).exists()
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
        activation=self.activate(prefix,value,config) if self.context.get('activate_owner') else {}
        if self.context.get('unit_refresh'):
            import unit_refresh as unit
            target=Path('/opt/node-fixture-systemd');target.mkdir(mode=0o755)
            cli=['/usr/bin/python3','-I','-B',prefix+'/current/servicectl']
            subprocess.run([*cli,'unit-install','--prefix',prefix,'--unit-dir',str(target)],capture_output=True,text=True,check=True,timeout=30)
            preview=unit.plan(prefix,str(target));assert not preview['unit_changed']
            original=(target/'ascend-lab-node.service').stat()
            first=unit.apply(prefix,preview['plan']);again=unit.apply(prefix,preview['plan'])
            assert not first['already_applied'] and again['already_applied'] and not first['unit_changed']
            assert (target/'ascend-lab-node.service').stat().st_ino==original.st_ino
            deploy.verify(prefix)
            self.unit_proof['node_unchanged_unit_replay']={'passed':True,'installed_inode_preserved':True,'systemd_manager_contacted':False}
        deploy.stop(self.prefix)
        return {'actual_central_contact':True,'fixture_stage_identity_only':False,'real_central_stage_definition_matched':True,
            'central_native_gid_appended':True,'central_approval_replay_stable':True,'node_ack_fault_chain':faults,
            'registered_and_local_projections_match':True,'node_unblocked_after_durable_ready_ack':True,
            'healthy_real_worker_heartbeat_observed':True,'identity_still_staged_and_token_count_zero':True,
            'central_nonroot_uid':23820,'accounts_enabled':False,'full_owner_onboarding_ready':False,
            'scope':'Same-host installed non-root controller plus Root Node, real stage/GID approval and authenticated HTTP commit/ready ACK. Environment enrolled by trusted fixture administrator; not a new protected environment-probe acceptance.',**activation,
            **({'unit_refresh':self.unit_proof} if self.unit_proof else {})}

    def abort_uncommitted(self,spec_path):
        from lab_client import read_json
        import hashlib
        previous=(self.state/'personas/old_owner/client.json').read_bytes()
        for option in ['--state-dir=/state/unrelated','--state-d=/state/unrelated']:
            try:self.command('accessctl',option,'user-status','new_owner')
            except RuntimeError as error:assert 'without overriding' in str(error)
            else:raise AssertionError('Installed admin accepted another state directory')
        killed=self.service_run('''import json,sys,os,signal
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
import owner_activation as a
s=Path(sys.argv[1])
def crash(stage):
 if stage=='client':os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.Activation(JobStore(s,read_json(s/'config.json'))).activate(read_json(s/'activation-spec.json'))
''');assert killed.returncode==-signal.SIGKILL,killed.stderr[-1000:]
        state=self.command('accessctl','user-activation-status','new_owner');identifier=state['activation_id'];assert state['state']=='pending'
        pending=self.state/'owner-activations/new_owner.pending.json';self.aborted_client=read_json(pending)['client'];self.aborted_id=identifier
        self.service_code('''import json,sys
from pathlib import Path
from owner_activation import _save,_read
s=Path(sys.argv[1]);p=s/'owner-activations/new_owner.pending.json';v=_read(p);v['expires_at']=-1;_save(p,v)
print(json.dumps({'expired':True}))
''')
        refused=subprocess.run(['/usr/bin/python3','-I','-B',self.prefix+'/current/servicectl','admin','--prefix',self.prefix,'--',
            'accessctl','user-activate','--spec',str(spec_path)],capture_output=True,text=True,timeout=30)
        assert refused.returncode and 'expired' in refused.stderr
        plan=self.state/'abort-plan.json';preview=self.command('accessctl','user-activation-abort-plan','new_owner','--activation-id',identifier,'--output',str(plan))
        text=plan.read_text();assert self.aborted_client['token'] not in text and self.aborted_client['password'] not in text
        assert preview['read_only'] and not preview['credentials_issued'] and pending.exists()
        faults=[]
        for boundary in ['journal','client','trust','before_commit','committed','receipt','intent_removed','published']:
            stopped=self.service_run('''import json,sys,os,signal
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
import owner_activation_abort as a
s=Path(sys.argv[1])
def crash(stage):
 if stage=="'''+boundary+'''":os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.Abort(JobStore(s,read_json(s/'config.json'))).apply(read_json(s/'abort-plan.json'))
''');assert stopped.returncode==-signal.SIGKILL,(boundary,stopped.stderr[-1200:])
            if boundary!='published':
                refused=subprocess.run(['/usr/bin/python3','-I','-B',self.prefix+'/current/servicectl','admin','--prefix',self.prefix,'--',
                    'accessctl','user-activate','--spec',str(spec_path)],capture_output=True,text=True,timeout=30)
                assert refused.returncode and 'activation abort' in refused.stderr
            assert self.command('accessctl','user-status','new_owner')['disabled']
            faults.append({'boundary':boundary,'sigkill_observed':True,'same_abort_plan':True})
        replay=self.command('accessctl','user-activation-abort-apply','--plan',str(plan));assert replay['reused'] and replay['state']=='aborted'
        assert not pending.exists() and not (self.state/'personas/new_owner/client.json').exists()
        assert self.command('accessctl','user-activation-status','new_owner')['state']=='staged'
        verified=self.service_code('''import sqlite3,json,sys
from pathlib import Path
s=Path(sys.argv[1])
with sqlite3.connect(s/'identities.sqlite3') as d:
 assert d.execute("SELECT disabled,password_hash FROM users WHERE id='new_owner'").fetchone()==(1,'!')
 assert d.execute("SELECT COUNT(*) FROM tokens WHERE user='new_owner'").fetchone()[0]==0
 assert d.execute("SELECT COUNT(*) FROM owner_activations WHERE owner='new_owner'").fetchone()[0]==0
 assert d.execute("SELECT COUNT(*) FROM owner_activation_aborts WHERE owner='new_owner'").fetchone()[0]==1
print(json.dumps({'disabled_and_no_credentials':True}))
''');assert verified['disabled_and_no_credentials'] and (self.state/'personas/old_owner/client.json').read_bytes()==previous
        self.abort_plan=plan
        self.abort_proof={'passed':True,'fault_chain':faults,'expired_intent_explicitly_aborted':True,'private_plan_contains_no_credentials':True,
            'original_private_files_removed':True,'old_owner_profile_unchanged':True,'zero_tokens_before_reactivation':True,
            'equal_form_admin_state_override_refused':True,
            'scope':'Installed service-UID CLI aborts a real uncommitted expired activation, eight consecutive SIGKILL boundaries; no resource or user deletion.'}

    def activate(self,prefix,plan,config):
        from lab_client import atomic_json,read_json
        from worker import Worker
        import deployment as deploy
        spec={'owner':'new_owner','stage_id':self.stage_id,'required_nodes':[{'node_id':self.node_id,'plan_id':plan['id']}]}
        path=self.state/'activation-spec.json';atomic_json(path,spec);os.chown(path,23820,23820)
        # A current heartbeat is mandatory even with a historical online flag.
        self.service_code('''import sqlite3,json,sys
from pathlib import Path
s=Path(sys.argv[1])
with sqlite3.connect(s/'jobs.sqlite3') as d:d.execute("UPDATE worker_nodes SET last_seen=0")
print(json.dumps({"stale":True}))
''')
        refused=subprocess.run(['/usr/bin/python3','-I','-B',self.prefix+'/current/servicectl','admin','--prefix',self.prefix,'--',
            'accessctl','user-activate','--spec',str(path)],capture_output=True,text=True,timeout=30)
        assert refused.returncode and 'stale' in refused.stderr
        assert not (self.state/'personas/new_owner/client.json').exists()
        worker=Worker(config)
        try:worker.heartbeat()
        finally:worker.lock.close();worker.state_lock.close()
        if self.context.get('activation_abort'):self.abort_uncommitted(path)
        faults=[];identifier=None
        for boundary in ['intent','trust','client','before_commit','committed','receipt','published']:
            crashed=self.service_run('''import sys,os,signal,json
from pathlib import Path
from jobs import JobStore
from lab_client import read_json
import owner_activation as a
s=Path(sys.argv[1])
def crash(stage):
 if stage=="'''+boundary+'''":os.kill(os.getpid(),signal.SIGKILL)
a._checkpoint=crash;a.Activation(JobStore(s,read_json(s/'config.json'))).activate(read_json(s/'activation-spec.json'))
''')
            assert crashed.returncode==-signal.SIGKILL,(boundary,crashed.stderr[-1200:])
            pending=self.state/'owner-activations/new_owner.pending.json'
            if pending.exists():
                value=read_json(pending)
                if identifier:assert identifier==value['id']
                identifier=value['id']
            status=self.command('accessctl','user-status','new_owner')
            if boundary in ['intent','trust','client','before_commit']:assert status['disabled'] and status['state']=='staged'
            faults.append({'boundary':boundary,'sigkill_observed':True,'same_activation_id':True})
        first=self.command('accessctl','user-activate','--spec',str(path));again=self.command('accessctl','user-activate','--spec',str(path))
        assert first['id']==again['id']==identifier and first['reused'] and again['reused']
        assert not (self.state/'owner-activations/new_owner.pending.json').exists()
        published=self.state/'personas/new_owner/client.json';assert published.stat().st_uid==23820 and published.stat().st_mode&0o777==0o600
        if self.context.get('activation_abort'):
            import hashlib
            assert identifier!=self.aborted_id
            fingerprint=hashlib.sha256(published.read_bytes()).hexdigest()
            replay=self.command('accessctl','user-activation-abort-apply','--plan',str(self.abort_plan));assert replay['reused']
            assert hashlib.sha256(published.read_bytes()).hexdigest()==fingerprint and not self.command('accessctl','user-status','new_owner')['disabled']
            from lab_client import Client,ClientError
            for credentials in [self.aborted_client,{k:v for k,v in self.aborted_client.items() if k!='token'}]:
                try:Client(credentials).request('/api/me')
                except ClientError as error:assert '401' in str(error)
                else:raise AssertionError('Aborted password/token became usable')
            self.abort_proof.update(new_activation_id_differs=True,old_abort_replay_keeps_new_active_credentials=True,old_password_and_token_http_denied=True)
        # Explicit trusted-administrator delivery, without putting credentials
        # in command arguments, stdout, the task payload or central web routes.
        import shutil
        personal=Path('/personal/new_owner');personal.mkdir(mode=0o700);os.chown(personal,23802,23802)
        shutil.copyfile(published,personal/'client.json');(personal/'client.json').chmod(0o600);os.chown(personal/'client.json',23802,23802)
        atomic_json(personal/'environment.json',config['environment_manifest']);os.chown(personal/'environment.json',23802,23802)
        denied=self.personal_code('''import json,sys
from pathlib import Path
try:(Path(sys.argv[1])/'client.json').read_bytes()
except PermissionError:print(json.dumps({"peer_private_profile_denied":True}))
else:raise AssertionError('Peer UID read personal credential file')
''',uid=23801)
        assert denied['peer_private_profile_denied']
        created=self.personal_code('''import json,sys,hashlib,os
from pathlib import Path
from lab_client import Client,read_json
from environment_contract import lock_for
s=Path(sys.argv[1]);assert os.getuid()==23802;c=Client.from_profile(s/'client.json');me=c.request('/api/me');assert me['id']=='new_owner'
source=s/'new-owner-source';source.mkdir(mode=0o700)
(source/'input.mlir').write_text('module { func.func @add() -> i32 { %a = arith.constant 19 : i32\\n %b = arith.constant 23 : i32\\n %r = arith.addi %a, %b : i32\\n return %r : i32 } }\\n')
(source/'run.py').write_text("import os,json,subprocess,shutil\\nfrom pathlib import Path\\nassert os.getuid()==23802\\ns=Path('/proc/self/status').read_text();assert 'CapEff:\\t0000000000000000' in s and 'NoNewPrivs:\\t1' in s\\nr=Path(os.environ['LAB_JOB_DIR']);out=r/'ir/canonical.mlir'\\nsubprocess.run([shutil.which('bishengir-opt'),'--canonicalize','--cse','input.mlir','-o',str(out)],check=True)\\nassert 'arith.constant 42' in out.read_text()\\nPath(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir','passed':True,'artifacts':['ir/canonical.mlir'],'uid':os.getuid(),'caps_zero':True}))\\n")
bundle=c.upload_input(source)
request={'environment':'worker-python','template':'ir_v1','idempotency_key':'activated-owner-ir-once',
 'parameters':{'input_id':bundle['id'],'argv':['python','run.py'],'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},
 'timeout_seconds':30,'lock':lock_for(read_json(s/'environment.json')),'workspace':{'min_mib':1,'min_inodes':16,'durable':True},'artifacts':{'file_mib':1,'total_mib':2}}}
first=c.request('/api/jobs',request);again=c.request('/api/jobs',request);assert first['job']['id']==again['job']['id'] and again['reused']
print(json.dumps({'job_id':first['job']['id'],'input_id':bundle['id'],'me_verified':True,'replay_same_job':True}))
''')
        worker=Worker(config)
        try:
            worker.heartbeat();grant=worker.client.call('claim',{})['grant']
            assert grant['job_id']==created['job_id'] and grant['owner']=='new_owner'
            from contextlib import redirect_stdout
            with (self.state/'new-owner-worker.log').open('x') as output,redirect_stdout(output):worker.attempt(grant)
        finally:worker.lock.close();worker.state_lock.close()
        checked=self.personal_code('''import json,sys,hashlib
from pathlib import Path
from lab_client import Client,ClientError
s=Path(sys.argv[1]);c=Client.from_profile(s/'client.json');identifier="'''+created['job_id']+'''"
value=c.request('/api/jobs/'+identifier);assert value['job']['state']=='succeeded' and len(value['attempts'])==1,value
artifact=c.request('/api/jobs/'+identifier+'/artifact?name=ir%2Fcanonical.mlir',raw=True);assert b'arith.constant 42' in artifact
try:c.request('/api/worker/owner-ready',{'plan_id':'0'*64,'commit_ack_id':'0'*32})
except ClientError as error:assert '401' in str(error)
else:raise AssertionError('Personal token used as Node credential')
print(json.dumps({'job_id':identifier,'state':'succeeded','attempts':1,'ir_sha256':hashlib.sha256(artifact).hexdigest(),'personal_node_api_denied':True,'client_uid':__import__('os').getuid()}))
''')
        peer=self.service_code('''import json,sys,sqlite3
from pathlib import Path
from lab_client import Client,ClientError
s=Path(sys.argv[1]);peer=Client.from_profile(s/'personas/old_owner/client.json')
try:peer.request('/api/jobs/'''+created['job_id']+'''')
except ClientError as error:assert '404' in str(error)
else:raise AssertionError('Peer read new owner job')
with sqlite3.connect(s/'identities.sqlite3') as d:n=d.execute("SELECT COUNT(*) FROM tokens WHERE user='new_owner'").fetchone()[0]
assert n==1
print(json.dumps({'peer_read_denied':True,'tokens':n}))
''');checked.update(peer)
        # Completion and profile replay must never reverse a later admin disable.
        self.command('accessctl','user-disable','new_owner',json_output=False)
        replay=self.command('accessctl','user-activate','--spec',str(path));assert replay['reused']
        assert self.command('accessctl','user-status','new_owner')['disabled']
        return {'owner_activation':{'passed':True,'fault_chain':faults,'stale_node_refused':True,'private_profile_mode':'0600',
            'service_uid':23820,'task_uid':23802,'caps_zero':True,'actual_cpu_ir':checked,'later_disable_preserved':True,
            'personal_client_uid':23802,'peer_uid23801_private_profile_denied':True,'administrator_private_copy_delivery':True,
            'scope':'Installed same-host controller/Node activation plus one real queued non-root CPU BishengIR workflow. No NPU or real systemd PID1.'},
            'accounts_enabled':1,'account_disabled_after_acceptance':True,'full_owner_onboarding_ready':False,
            'identity_still_staged_and_token_count_zero':False,'credential_free_stage_verified_before_activation':True,
            **({'activation_abort':self.abort_proof} if self.context.get('activation_abort') else {})}

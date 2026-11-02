"""Owned installed CPU graph admission and real SIGKILL preparation recovery."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from large_mixed_compile_check import Acceptance
from lab_client import Client,ClientHTTPError,atomic_json,now

FAULT_CODE='''import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,'/opt/ascend-lab-controller/current')
from state_lock import state_lock
from jobs import JobStore
from jobs import atomic_json
import job_graphs
p=Path(sys.argv[1]);body=json.loads(Path(sys.argv[2]).read_text());stage=sys.argv[3]
authority=json.loads(Path(sys.argv[4]).read_text())
lock=state_lock(p)
from payload_groups import require_membership
require_membership(json.loads((p/'config.json').read_text())['scheduler']['payload_storage'])
jobs=JobStore(p,json.loads((p/'config.json').read_text()))
principal=jobs.access.authenticate('Bearer '+authority['token']);assert principal is not None
def fault(where):
 if where==stage:
  atomic_json(Path(sys.argv[4]).with_suffix('.fault.json'),{'boundary':where,'self_sigkill':True})
  print(json.dumps({'fault_boundary':where}),flush=True)
  os.kill(os.getpid(),signal.SIGKILL)
job_graphs._checkpoint=fault
jobs.graphs.submit(body,principal)
raise AssertionError('Expected original fault boundary was not reached')
'''


class GraphAcceptance(Acceptance):
    def __init__(self):
        super().__init__(small=True);self.graph_proofs=[];self.faults=[]

    def svc(self,code,*args,check=True,input=None):
        # Match the installed service's real supplementary groups; docker exec
        # --user or initgroups alone omits the explicit numeric storage GIDs.
        launch='import os,json,sys;from pathlib import Path;sys.path.insert(0,"/opt/ascend-lab-controller/current");from payload_groups import gids;m=json.loads(Path("/opt/ascend-lab-controller/installation.json").read_text());uid=int(sys.argv[1]);gid=int(sys.argv[2]);assert (uid,gid)==(m["uid"],m["gid"]);os.setgroups(gids(m.get("payload_storage")));os.setgid(gid);os.setuid(uid);os.umask(0o077);os.execv("/usr/bin/python3",["/usr/bin/python3","-I","-c",sys.argv[3],*sys.argv[4:]])'
        return self.docker(['exec',*(['-i'] if input is not None else []),self.controller,'/usr/bin/python3','-I','-c',launch,
            str(self.service_uid),str(self.service_gid),code,*map(str,args)],check=check,input=input)

    def private(self,path,value):
        self.svc('import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600)',path,input=json.dumps(value))

    def counts(self):
        return json.loads(self.svc('''import json,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1])
with sqlite3.connect(p/'jobs.sqlite3') as db:
 print(json.dumps({t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['jobs','job_graphs','experiment_charges','storage_allocations']}))
''',self.controller_state).stdout)

    def plan(self,owner,label):
        source=self.base/('graph-input-'+str(owner));source.mkdir(mode=0o700)
        raw=(ROOT/'tests/workloads/graph_pipeline.py').read_bytes();(source/'pipeline.py').write_bytes(raw)
        bundle=self.clients[owner].upload_input(source)
        budget=self.clients[owner].request('/api/experiment-budgets',{'name':'Graph '+label,'idempotency_key':self.prefix+'-budget-'+label,
            'limits':{'jobs':3,'cpu_seconds':180,'npu_seconds':0},'templates':['build_v1','ir_v1'],'lifetime_seconds':3600})['budget']
        token=self.clients[owner].request('/api/experiment-budgets/'+budget['id']+'/token',{})['token']
        client=Client({'controller':self.url,'token':token})
        nodes=[]
        for i,stage in enumerate(['build','verify','report']):
            job={'template':'build_v1' if i==0 else 'ir_v1','environment':'worker-python',
                'parameters':{'input_id':bundle['id'],'argv':['python','pipeline.py',stage],'lock':self.lock,
                    'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},'timeout_seconds':60,
                    'workspace':{'min_mib':16,'min_inodes':128,'durable':True},'artifacts':{'file_mib':1,'total_mib':2}}}
            node={'key':'stage'+str(i),'job':job}
            if i:node.update(after=['stage'+str(i-1)],artifact_inputs=[{'node':'stage'+str(i-1),'name':'ir/output.mlir',
                'target':'upstream.mlir' if i==1 else 'verified.mlir'}])
            nodes.append(node)
        request={'name':'Actual CPU pipeline '+label,'idempotency_key':self.prefix+'-graph-'+label,'nodes':nodes}
        atomic_json(self.base/('graph-request-'+label+'.json'),request)
        atomic_json(self.base/('graph-authority-'+label+'.json'),{'controller':self.url,'token':token})
        return request,client,budget

    def remember(self,graph,owner):
        self.safe_cleanup=False
        for node in graph['nodes']:
            if not any(h['id']==node['job_id'] for h in self.jobs):self.jobs.append({'id':node['job_id'],'owner':owner,'phase':'graph-'+node['key']})
        self.save()

    def finish(self,graph,owner,client,budget,label):
        self.remember(graph,owner);handles=[h for h in self.jobs if h['id'] in {n['job_id'] for n in graph['nodes']}]
        details=self.wait_all(handles,240);stages={}
        for h,detail in zip(handles,details):
            job=detail['job'];assert job['state']=='succeeded' and len(detail['attempts'])==1,(label,job['state'],job['reason'])
            report=self.artifact(h,'results/graph-stage.json');assert report['answer']==42 and report['uid']==23900+owner
            assert report['cap_effective']=='0000000000000000' and report['npu_kernel_launches']==0
            stages[report['stage']]={'job_id':h['id'],'attempt_id':job['attempt_id'],'report':report,
                'input_provenance':job.get('input_provenance'),'execution_cleanup_confirmed':job['result']['execution_cleanup_confirmed']}
        assert stages['verify']['report']['input_sha256']==stages['build']['report']['output_sha256']
        assert stages['report']['report']['input_sha256']==stages['verify']['report']['output_sha256']
        value=client.request('/api/experiment-budgets/'+budget['id'])['budget']
        assert value['charged']=={'jobs':3,'cpu_seconds':180,'npu_seconds':0}
        self.settle(cancel=False)
        self.graph_proofs.append({'label':label,'graph_id':graph['id'],'owner':owner,'uid':23900+owner,'budget_id':budget['id'],
            'charged':value['charged'],'stages':stages})
        atomic_json(self.base/'graph-proofs.json',self.graph_proofs)

    def crash(self,owner,label,stage):
        request,client,budget=self.plan(owner,label)
        self.ctl('node','stop');self.ctl('controller','stop')
        path=self.controller_state/('graph-request-'+label+'.json');authority=self.controller_state/('graph-authority-'+label+'.json')
        self.private(path,request);self.private(authority,{'token':client.config['token']})
        before=self.counts()
        self.safe_cleanup=False
        done=self.svc(FAULT_CODE,self.controller_state,path,stage,authority,check=False)
        inspect='''import json,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]);key=sys.argv[2]
with sqlite3.connect(p/'jobs.sqlite3') as db:
 row=db.execute('SELECT record FROM job_graphs WHERE idem_key=?',(key,)).fetchone()
 if row:
  value=json.loads(row[0]);jobs=[]
  for label,ident in value['jobs'].items():
   raw=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(ident,)).fetchone()[0]);jobs.append({'key':label,'id':ident,'attempts':db.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(ident,)).fetchone()[0],'directory':Path(raw['archive_dir']).exists()})
  print(json.dumps({'graph_id':value['id'],'state':value['state'],'jobs':jobs}))
 else:print(json.dumps({'graph_id':None,'jobs':[]}))
'''
        try:
            pending=json.loads(self.svc(inspect,self.controller_state,request['idempotency_key']).stdout)
            if pending['graph_id']:self.remember({'nodes':[{'key':v['key'],'job_id':v['id']} for v in pending['jobs']]},owner)
            assert done.returncode in {-signal.SIGKILL,128+signal.SIGKILL},(stage,done.returncode,done.stderr[-500:])
            assert json.loads(done.stdout.splitlines()[-1])=={'fault_boundary':stage}
            assert self.read(self.controller,authority.with_suffix('.fault.json'))=={'boundary':stage,'self_sigkill':True}
            if stage.startswith('admitted:'):
                assert pending['graph_id'] is None and self.counts()==before
            else:
                assert pending['state']=='preparing' and len(pending['jobs'])==3 and all(v['attempts']==0 for v in pending['jobs'])
                if stage=='admission_committed':assert not any(v['directory'] for v in pending['jobs'])
                if stage=='archive:stage0':assert sum(v['directory'] for v in pending['jobs'])==1
        finally:
            # The original exec has returned, so its actor is known terminal.
            # Restore only this service so original cleanup can observe state.
            assert self.ctl('controller','start')['ready']
        reply=client.request('/api/job-graphs',request);graph=reply['graph']
        if pending['graph_id']:
            assert reply['reused'] and graph['id']==pending['graph_id']
            assert {v['job_id'] for v in graph['nodes']}=={v['id'] for v in pending['jobs']}
        self.remember(graph,owner)
        assert self.ctl('node','start')['ready']
        self.finish(graph,owner,client,budget,label)
        self.faults.append({'label':label,'stage':stage,'actual_sigkill':True,'committed_graph_id':pending['graph_id'],
            'fault_boundary_marker_confirmed':True,'zero_attempts_before_recovery':True,'post_commit_ids_preserved':bool(pending['graph_id']),'before_commit_atomic_rollback':stage.startswith('admitted:')})

    def portable(self,request,client,graph):
        self.docker(['exec',self.controller,'/usr/bin/python3','-B','/bootstrap/install-client','--prefix','/opt/ascend-lab-personal'])
        path='/tmp/graph-personal';code='''import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]);p.mkdir(mode=0o700);os.chown(p,23642,23642)
v=json.loads(sys.stdin.read())
for name,value in v.items():
 q=p/name;q.write_text(json.dumps(value));q.chmod(0o600);os.chown(q,23642,23642)
'''
        self.py(self.controller,code,path,input=json.dumps({'client.json':client.config,'approved.json':request}))
        argv=['exec','--user','23642:23642',self.controller,'/usr/bin/python3','-B','/opt/ascend-lab-personal/graph_client.py',
              '--profile',path+'/client.json','--request',path+'/approved.json','--state',path+'/conversation.json']
        reply=json.loads(self.docker([*argv,'submit']).stdout);assert reply['reused'] and reply['graph']['id']==graph['id']
        messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'graph_status','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_graph','arguments':{}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'submit_graph','arguments':{'argv':['sh']}}}]
        done=self.docker([argv[0],'-i',*argv[1:],'stdio'],input=''.join(json.dumps(v)+'\n' for v in messages))
        replies=[json.loads(v)['result'] for v in done.stdout.splitlines()]
        assert {v['name'] for v in replies[0]['tools']}=={'submit_graph','graph_status','resume_graph'}
        assert not replies[1]['isError'] and json.loads(replies[1]['content'][0]['text'])['graph']['id']==graph['id']
        assert not replies[2]['isError'] and json.loads(replies[2]['content'][0]['text'])['reused']
        assert replies[3]['isError']
        denied=self.docker(['exec','--user','23642:23642',self.controller,'/usr/bin/python3','-I','-c',
            'from pathlib import Path;import sys;Path(sys.argv[1]).read_bytes()',str(self.controller_state/'config.json')],check=False)
        assert denied.returncode!=0
        return {'actual_personal_uid':23642,'installed_cli_and_stdio_mcp':True,'same_approved_graph_reused':True,
            'model_command_override_refused':True,'service_private_configuration_unreadable':True}

    def workloads(self):
        metadata=self.read(self.controller,'/opt/ascend-lab-controller/installation.json')
        self.service_uid=metadata['uid'];self.service_gid=metadata['gid']
        self.phase('atomic-graph-rejection-and-real-three-stage-pipeline')
        request,client,budget=self.plan(1,'normal');bad=copy.deepcopy(request)
        bad['idempotency_key']+='-invalid';bad['nodes'][-1]['job']['parameters']['input_id']='input-'+'f'*24
        before=self.counts()
        try:client.request('/api/job-graphs',bad)
        except ClientHTTPError as error:assert error.status in {400,404}
        else:raise AssertionError('Partial graph was admitted')
        assert self.counts()==before
        reply=client.request('/api/job-graphs',request);graph=reply['graph'];self.remember(graph,1)
        replay=client.request('/api/job-graphs',request);assert replay['reused'] and replay['graph']['id']==graph['id']
        try:self.clients[2].request('/api/job-graphs/'+graph['id'])
        except ClientHTTPError as error:assert error.status==404
        else:raise AssertionError('Foreign graph visibility')
        self.finish(graph,1,client,budget,'normal')
        portable=self.portable(request,client,graph)
        for owner,label,stage in [(2,'before-commit','admitted:stage1'),(3,'after-commit','admission_committed'),(4,'partial-files','archive:stage0')]:
            self.phase('actual-sigkill-'+label);self.crash(owner,label,stage)
        self.phase('completed-graph-evidence')
        record={'passed':True,'recorded_at':now(),'graphs':self.graph_proofs,'faults':self.faults,
            'actual_workload_jobs':12,'actual_graphs':4,'actual_sigkills':3,'all_workload_jobs_single_attempt':True,
            'three_stage_artifact_sha_chain_preserved':True,'late_invalid_stage_atomic_rollback':True,
            'foreign_graph_read_404':True,'task_uids':[23901,23902,23903,23904],
            'portable_client':portable,
            'npu':0,'model_calls':0,'scope':'Same-host owned installed controller/Root Node,4 owner-budget CPU pipelines and actual SIGKILL during admission/preparation; not hardware numerics, cross-host fencing or long-term load.'}
        atomic_json(self.base/'graph-validation.json',record);atomic_json(self.outer/'results/graph-validation.json',record)
        print(json.dumps({'passed':True,'graphs':4,'workload_jobs':12,'actual_sigkills':3}),flush=True)


def main():
    os.umask(0o077);run=GraphAcceptance()
    try:run.prepare();run.workloads()
    finally:run.cleanup()
    assert run.state['cleaned'] and not run.state['cleanup_errors']
    path=run.outer/'results/graph-validation.json';value=json.loads(path.read_text());value['cleanup_confirmed']=True;atomic_json(path,value)


if __name__=='__main__':main()

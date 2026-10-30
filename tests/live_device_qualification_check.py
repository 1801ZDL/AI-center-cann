"""One queued NPU reservation contains an isolated HTTP controller and actual worker.

No ACL or numerical workload. A real fixed probe may open only the reserved card;
ordinary accuracy requests are inspected/cancelled, never executed. Legacy-sized
directory storage is deliberate for this trusted bounded fixture, not quota proof.
"""
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import JobStore
from lab_client import Client,read_json,atomic_json,now
from server import Store,handler_class
from listener import HTTPServer
from worker import Worker
from device_qualification import PROBE,decision
from npu_admission import query,selected_reading


def main():
    directory=Path(os.environ['LAB_JOB_DIR']);parent=read_json(directory/'dispatch.json')
    assert parent['resources']['npu']==1 and len(parent['allocation']['npu_ids'])==1
    physical=parent['allocation']['npu_ids'][0];cpu=parent['allocation']['cpu_ids'][0]
    def idle():
        devices,processes=query();selected_reading([physical],(devices,processes))
        return [{k:row.get(k) for k in ['id','health','utilization_pct']} for row in devices if row['id']==physical]
    before=idle();base=directory/'tmp/qualification';base.mkdir(mode=0o700)
    state=base/'controller';state.mkdir(mode=0o700)
    config={'username':'qual_operator','password':secrets.token_urlsafe(30),'container':'zdl','host':'127.0.0.1','port':0,
        'monitor_local':False,'scheduler':{'workspace_root':str(base),'local_enabled':False,'cpu_ids':[cpu],'memory_mib':768,'shm_mib':0,'max_running':1}}
    atomic_json(state/'config.json',config);jobs=JobStore(state,config);jobs.controller_instance=uuid.uuid4().hex
    server=HTTPServer(('127.0.0.1',0),handler_class(Store(state/'observations.sqlite3'),config,jobs));server.daemon_threads=True
    config['port']=server.server_port;atomic_json(state/'config.json',config)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();worker=None;owned=[]
    try:
        owner='qual_alice';workspace=base/'alice';workspace.mkdir(mode=0o700)
        jobs.access.create_user(owner,workspace,environments=['worker-python'],quota={'npu':1,'memory_mib':768})
        token=jobs.access.issue_token(owner,{'jobs:submit','jobs:read','jobs:cancel'})
        client=Client({'host':'127.0.0.1','port':server.server_port,'token':token})
        policy={'device_ids':[physical],'visible_ids':{str(physical):0},'device_gid':1001,
           'management_devices':['/dev/davinci_manager',*(['/dev/hisi_hdc'] if Path('/dev/hisi_hdc').exists() else [])],
           'driver_paths':['/usr/local/Ascend/driver'],'query_tool':'/usr/local/sbin/npu-smi','management_env':'/data/pri/cann/set_env.sh'}
        profile={'name':'qualification-fixture','physical_id':jobs.backend.physical_id,
           'image':'sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273',
           'users':{owner:{'uid':23901,'gid':23901}},'npu':policy,'templates':[PROBE,'accuracy_v1','cpu_demo'],
           'policy':{'cpu_ids':[cpu],'memory_mib':512,'shm_mib':0,'max_running':1}}
        node,key=jobs.nodes.enroll(profile)
        node_config={**profile,'node_id':node,'node_key':key,'state_dir':str(base/'node'),
             'controller':'http://127.0.0.1:'+str(server.server_port),'policy_revision':1,'python':'/usr/bin/python3'}
        atomic_json(base/'node-config.json',node_config);worker=Worker(node_config);worker.heartbeat()
        source=workspace/'sentinel.py';source.write_text('raise RuntimeError("No numerical workload is authorized by this fixture")\n')
        bundle=client.upload_input(source)
        ordinary=client.request('/api/jobs',{'template':'accuracy_v1','environment':'worker-python','name':'Must wait for access qualification',
             'parameters':{'input_id':bundle['id'],'argv':['/usr/bin/python3','/inputs/sentinel.py'],
                           'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':1},'lock':{'python':'3.10.12'}}})['job']
        assert worker.client.call('claim',{})['grant'] is None
        assert client.request('/api/jobs/'+ordinary['id'])['attempts']==[]
        assert jobs.summary()['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
        probe=client.request('/api/jobs',{'template':PROBE,'environment':'worker-python',
             'parameters':{'node_id':node,'device_id':physical}})['job']
        grant=worker.client.call('claim',{})['grant'];assert grant['job_id']==probe['id'];owned.append(grant['attempt_id'])
        worker.attempt(grant);worker.heartbeat()
        actual=client.request('/api/jobs/'+probe['id'])['job'];qualification=decision(jobs.nodes.node(node),owner,physical)
        assert actual['state'] in {'succeeded','failed'}
        assert qualification['status'] in {'blocked','verified'}
        receipt=client.request('/api/jobs/'+probe['id']+'/artifact?name=results%2F_lab%2Fenvironment.json')
        assert receipt['workload_started'] is False
        if qualification['status']=='blocked':
            assert actual['state']=='failed' and actual['result']['failure_stage']=='npu_device_access'
            assert worker.client.call('claim',{})['grant'] is None
            assert client.request('/api/jobs/'+ordinary['id'])['attempts']==[]
        # The ordinary request is never started, including if hardware unexpectedly permits access.
        client.request('/api/jobs/'+ordinary['id']+'/cancel',{})
        cpu_job=client.request('/api/jobs',{'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}})['job']
        cpu_grant=worker.client.call('claim',{})['grant'];assert cpu_grant['job_id']==cpu_job['id'];owned.append(cpu_grant['attempt_id'])
        worker.attempt(cpu_grant);assert client.request('/api/jobs/'+cpu_job['id'])['job']['state']=='succeeded'
        worker.heartbeat();summary=client.request('/api/jobs')
        assert summary['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
        from nodes import NodeRegistry
        jobs.nodes=NodeRegistry(jobs)
        assert decision(jobs.nodes.node(node),owner,physical)['status']==qualification['status']
        public=client.request('/api/nodes')['nodes'][0]
        assert public['device_access']['counts'][qualification['status']]==1
        assert probe['id'] not in json.dumps(public) and owner not in json.dumps(public)
        after=idle()
        report={'passed':True,'verified_at':now(),'parent_job':parent['id'],'probe_job':probe['id'],'probe_state':actual['state'],
             'qualification':qualification['status'],'negative_gate_verified':qualification['status']=='blocked',
             'protected_receipt':receipt,'ordinary_job':ordinary['id'],'ordinary_attempts':0,'cpu_job':cpu_job['id'],
             'cpu_progress_after_device_result':True,'registry_restart_preserved_status':True,'public_counts_only':True,
             'public_node_snapshot':public,'runtime_files':grant['runtime_files'],
             'node_id':node,'owned_attempts':owned,'reserved_after':summary['reserved'],'before':before,'after':after,
             'npu_kernel_launches':0,'acl_initialized':False,'existing_containers_modified':False,
             'scope':'Actual authenticated HTTP node protocol and managed Docker in one queue-owned allocation; own logical account, ordinary directory storage, not host root/systemd or numerical acceptance'}
        atomic_json(directory/'results/device-qualification.json',report);print(json.dumps(report,indent=2))
    finally:
        if worker:
            from container_engine import DockerAttempt,run
            for grant in worker.recover():
                engine=DockerAttempt(worker.root,grant,worker.config);info=engine.inspect()
                if info and info['State']['Running']:run(['docker','stop','--time','2',engine.name],binding=engine.binding())
                info=engine.inspect()
                if info and (info['State']['Running'] or info['State']['Pid']):raise RuntimeError('Owned attempt termination is unconfirmed; reconcile original allocation')
                engine.cleanup()
            worker.lock.close();worker.state_lock.close()
        server.shutdown();server.server_close();thread.join(timeout=3);jobs.access.close()


if __name__=='__main__':main()

"""NPU policy boundaries, physical leases, freshness and device release gates."""
import copy
import datetime as dt
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,Scheduler,now
from npu_device import validate,fingerprint,observation,CAPABILITY,check
from container_engine import DockerAttempt,SCRIPTS
from worker import Worker


def policy():
    return {'device_ids':[0],'visible_ids':{'0':0},'device_gid':1001,
            'management_devices':['/dev/davinci_manager','/dev/hisi_hdc'],
            'driver_paths':['/usr/local/Ascend/driver'],'query_tool':'/usr/local/sbin/npu-smi',
            'management_env':'/data/pri/cann/set_env.sh'}


class NpuDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-npu-device-');self.root=Path(self.temp.name)
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        for user in ['alice','bob']:
            directory=self.root/user;directory.mkdir()
            self.jobs.access.create_user(user,directory,environments=['worker-python'],quota={'npu':1})
        domain=self.jobs.resources.get(self.jobs.backend.physical_id)
        self.jobs.resources.configure(domain['physical_id'],{**domain['policy'],'max_running':2},domain['revision'])
        self.npu=policy();self.nodes=[]
        for index in range(2):
            profile={'name':'npu-node-'+str(index),'physical_id':domain['physical_id'],
              'users':{name:{'uid':23001+i,'gid':23001+i} for i,name in enumerate(['alice','bob'])},
              'templates':['cpu_demo','npu_probe'],'npu':self.npu,
              'policy':{'cpu_ids':[domain['policy']['cpu_ids'][index]],'memory_mib':512,'shm_mib':0,'max_running':1}}
            node,key=self.jobs.nodes.enroll(profile);self.nodes.append(node);self.beat(node)

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def reading(self,**change):
        return {'status':'ok','sampled_at':now(),'policy_sha256':fingerprint(self.npu),
                'devices':[{'id':0,'health':'OK','utilization_pct':0,'process_count':0}],**change}

    def beat(self,node,reading=None,caps=None):
        return self.jobs.nodes.heartbeat(node,{'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
          'capabilities':caps or ['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY],
          'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
          'npu_observation':reading or self.reading()})

    def submit(self,user='alice',template='npu_probe'):
        return self.jobs.submit({'template':template,'environment':'worker-python'},self.jobs.access.user(user))[0]

    def test_policy_rejects_root_group_arbitrary_mounts_and_ambiguous_ids(self):
        for change in [{'device_gid':0},{'driver_paths':['/']},{'driver_paths':['/var/run/docker.sock']},
                       {'management_devices':['/dev/sda']},{'device_ids':[True]}, {'visible_ids':{}},
                       {'device_ids':[0,0]},{'visible_ids':{'0':True}}]:
            with self.subTest(change=change),self.assertRaises(ValueError):validate({**self.npu,**change})

    def test_heartbeat_requires_matching_contract_freshness_and_no_private_process_fields(self):
        for reading in [self.reading(policy_sha256='0'*64),self.reading(sampled_at='2000-01-01T00:00:00+00:00'),
                        self.reading(devices=[{'id':0,'health':'OK','utilization_pct':float('nan'),'process_count':0}])]:
            with self.assertRaises(JobError):self.beat(self.nodes[0],reading)
        with self.assertRaises(JobError):self.beat(self.nodes[0],caps=['start-ticket-v1','cleanup-before-complete-v1'])
        reading=self.reading();reading['devices'][0].update(pid=123,command='private')
        self.beat(self.nodes[0],reading)
        self.assertNotIn('command',str(self.jobs.nodes.public_nodes()))
        self.assertNotIn('pid',str(self.jobs.nodes.node(self.nodes[0])['report']['npu']))

    def test_two_entries_cannot_allocate_same_physical_card_and_cpu_work_can_progress(self):
        first=self.submit();self.submit('bob');self.submit('bob','cpu_demo')
        grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        self.assertEqual(grant['job_id'],first['id']);self.assertEqual(grant['allocation']['npu_ids'],[0])
        self.assertEqual(grant['npu_policy_sha256'],fingerprint(self.npu))
        other=self.jobs.nodes.claim(self.nodes[1])['grant']
        self.assertEqual(other['template'],'cpu_demo');self.assertEqual(other['allocation']['npu_ids'],[])

    def test_busy_or_unavailable_observation_never_grants_device(self):
        job=self.submit()
        for reading in [self.reading(devices=[{'id':0,'health':'OK','utilization_pct':0,'process_count':1}]),
                        self.reading(status='error',devices=[])]:
            self.beat(self.nodes[0],reading);self.assertIsNone(self.jobs.nodes.claim(self.nodes[0])['grant'])
        self.assertEqual(self.jobs.get(job['id'])['allocation'],{})

    def test_stale_npu_reading_is_rejected_even_if_generic_heartbeat_is_fresh(self):
        self.submit()
        with self.jobs.connect() as db:
            import json
            node=self.jobs.nodes.node(self.nodes[0],db);node['report']['npu']['sampled_at']='2000-01-01T00:00:00+00:00'
            db.execute('UPDATE worker_nodes SET last_seen=?,report=? WHERE id=?',(time.time(),json.dumps(node['report']),self.nodes[0]))
        self.assertIsNone(self.jobs.nodes.claim(self.nodes[0])['grant'])

    def test_missing_npu_release_confirmation_keeps_reservation(self):
        self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        body={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
        result={'status':'cancelled','exit_code':None,'termination_confirmed':True,'execution_cleanup_confirmed':True}
        with self.assertRaises(JobError):self.jobs.nodes.complete(self.nodes[0],{**body,'result':result,'artifacts':[]})
        self.assertEqual(self.jobs.summary()['reserved']['npu_ids'],[0])
        self.jobs.nodes.complete(self.nodes[0],{**body,'result':{**result,'npu_release_confirmed':True},'artifacts':[]})
        self.assertEqual(self.jobs.summary()['reserved']['npu_ids'],[])

    def test_local_scheduler_excludes_npu_leased_by_managed_node(self):
        self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        local,_=self.jobs.submit({'argv':['python','-c','pass'],'npu':1})
        observations=Mock();observations.latest.return_value={'sampled_at':now(),
          'host':{'memory_available_bytes':8*1024**3,'workspace_disk':{'free_bytes':8*1024**3}},
          'npu':{'status':'ok','sampled_at':now(),'devices':[{'id':i,'health':'OK','utilization_pct':0,
             'scoped_process_count':0,'external_process_count':0} for i in [0,1]]}}
        scheduler=Scheduler(self.jobs,observations,threading.Event())
        try:
            with patch('jobs.subprocess.Popen'):scheduler.tick()
            self.assertEqual(self.jobs.get(local['id'])['allocation']['npu_ids'],[1])
        finally:
            if scheduler.lock:scheduler.lock.close()

    def test_device_state_change_after_create_never_issues_docker_start(self):
        self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        config={'image':'sha256:'+'0'*64,'users':{'alice':{'uid':23001,'gid':23001}},
                'docker_runtime':{'endpoint':'unix:///var/run/docker.sock','daemon_id':'offline-only'},
                'policy':self.jobs.nodes.node(self.nodes[0])['profile']['policy'],'npu':self.npu}
        engine=DockerAttempt(self.root/'engine',grant,config)
        with patch.object(engine,'validate',return_value={'uid':23001,'gid':23001}),patch.object(engine,'inspect',return_value=None),\
             patch('npu_device.docker_arguments',return_value=['--device','/dev/davinci0:/dev/davinci0:rwm']),\
             patch('npu_device.check',side_effect=RuntimeError('device busy')),patch('container_engine.run') as docker:
            with self.assertRaises(RuntimeError):engine.start()
        self.assertEqual(docker.call_count,1);self.assertEqual(docker.call_args.args[0][:2],['docker','create'])
        self.assertEqual(engine.state['stage'],'prestart-rejected');self.assertIsNone(engine.state.get('started_epoch'))

    def test_finished_container_with_uncleared_device_is_not_cleaned_or_completed(self):
        worker=object.__new__(Worker);worker.upload=Mock()
        engine=Mock();engine.grant={'resources':{'npu':1}};engine.state={'started_epoch':1};engine.config={'npu':self.npu}
        with patch('npu_device.check',side_effect=RuntimeError('device not clear')):
            with self.assertRaises(RuntimeError):worker.finish_attempt(engine,{'status':'succeeded'})
        engine.cleanup.assert_not_called();worker.upload.assert_not_called()

    def test_no_kernel_probe_still_requires_device_release_confirmation(self):
        worker=object.__new__(Worker);worker.upload=Mock()
        engine=Mock();engine.grant={'resources':{'npu':1},'template':'npu_probe'}
        engine.state={'started_epoch':1};engine.config={'npu':self.npu}
        with patch('npu_device.check',side_effect=RuntimeError('device state unconfirmed')):
            with self.assertRaises(RuntimeError):worker.finish_attempt(engine,{'status':'failed'})
        engine.cleanup.assert_not_called();worker.upload.assert_not_called()

    def test_created_container_recovery_never_becomes_success_or_repeats_start(self):
        self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        engine=DockerAttempt(self.root/'created',grant,{'image':'sha256:'+'0'*64,'users':{'alice':{'uid':23001,'gid':23001}}})
        engine.state.update(stage='start-intent',started_epoch=time.time())
        created={'State':{'Status':'created','Running':False,'Pid':0,'ExitCode':0}}
        with patch.object(engine,'inspect',return_value=created),patch('container_engine.run') as docker:
            with self.assertRaises(RuntimeError):engine.start()
        docker.assert_not_called()


if __name__=='__main__':unittest.main()

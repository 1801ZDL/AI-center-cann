"""Execution identity fencing and fail-closed service-user checks."""
import os
from pathlib import Path
import pwd
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,Scheduler,atomic_json,now
from backends import LocalExecutionBackend
import executor
import server


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-backend-')
        self.runtime=Path(self.temp.name)
        self.jobs=JobStore(self.runtime,{'username':pwd.getpwuid(os.getuid()).pw_name,'container':'zdl'})

    def tearDown(self):self.temp.cleanup()

    def test_node_identity_survives_restart_and_stale_inventory_is_unknown(self):
        backend=LocalExecutionBackend(self.runtime)
        self.assertEqual(backend.node_id,self.jobs.backend.node_id)
        inventory=backend.inventory({'sampled_at':'2000-01-01T00:00:00+00:00'},self.jobs.policy)
        self.assertEqual(inventory['status'],'unknown')
        self.assertEqual(inventory['enforcement'],'cooperative-reservations')
        with self.assertRaises(ValueError):
            backend.launch(self.runtime,{'node_id':'foreign-node','attempt_id':'a'})

    def test_stale_attempt_or_wrong_node_cannot_release_resources(self):
        job,_=self.jobs.submit({'argv':['python','-c','print(1)']})
        observations=Mock()
        observations.latest.return_value={'sampled_at':now(),'host':{'memory_available_bytes':8*1024**3,
            'workspace_disk':{'free_bytes':8*1024**3}},'npu':{'devices':[]}}
        scheduler=Scheduler(self.jobs,observations,threading.Event())
        with patch('jobs.subprocess.Popen'):scheduler.tick()
        assigned=self.jobs.get(job['id'])
        result={'status':'succeeded','started_at':now(),'finished_at':now()}
        for changes in [{'attempt_id':'stale-attempt'},{'node_id':'foreign-node'}]:
            completion={'job_id':job['id'],'token':assigned['_token'],'attempt_id':assigned['attempt_id'],
                        'node_id':assigned['node_id'],'result':result,**changes}
            atomic_json(self.jobs.directory(job['id'])/'completion.json',completion)
            scheduler.reconcile()
            self.assertEqual(self.jobs.get(job['id'])['state'],'needs_attention')
            self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        atomic_json(self.jobs.directory(job['id'])/'completion.json',
                    {'job_id':job['id'],'token':assigned['_token'],'attempt_id':assigned['attempt_id'],
                     'node_id':assigned['node_id'],'result':result})
        scheduler.reconcile()
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)
        attempts=self.jobs.attempts(job['id'])
        self.assertEqual(len(attempts),1)
        self.assertEqual(attempts[0]['state'],'succeeded')

    def test_current_controller_and_executor_refuse_root_execution(self):
        with patch('os.getuid',return_value=0),patch('sys.argv',['server.py']):
            with self.assertRaises(SystemExit):server.main()
            with self.assertRaises(SystemExit):executor.main()


if __name__=='__main__':unittest.main()

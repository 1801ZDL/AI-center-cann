"""Lost controller records must quarantine every entry in their physical domain."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from attempt_inventory import CAPABILITY,HOLD_REASON,WAIT_REASON,local,validate
from jobs import ROOT,JobStore,JobError,atomic_json

class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-inventory-');self.runtime=Path(self.temp.name)
        self.config={'username':'inventory-admin','password':'test-only','container':'zdl'}
        self.jobs=JobStore(self.runtime,self.config);self.jobs.controller_instance='instance-one';self.jobs.policy['require_attempt_inventory']=True
        workspace=self.runtime/'alice';workspace.mkdir();self.jobs.access.create_user('alice',workspace,environments=['worker-python'])
        self.alice=self.jobs.access.user('alice');self.nodes=[]
        for index in range(2):
            profile={'name':'inventory-node'+str(index),'physical_id':self.jobs.backend.physical_id,
              'users':{'alice':{'uid':23651,'gid':23651}},'templates':['cpu_demo'],
              'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][index]],'memory_mib':512,'shm_mib':0,'max_running':1}}
            self.nodes.append(self.jobs.nodes.enroll(profile)[0])
        for node in self.nodes:self.beat(node)
    def tearDown(self):self.temp.cleanup()
    def beat(self,node,items=None,caps=None):
        return self.jobs.nodes.heartbeat(node,{'protocol_version':1,'physical_id':self.jobs.backend.physical_id,
          'capabilities':caps if caps is not None else ['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY],
          'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
          'attempt_inventory':[] if items is None else items})
    def submit(self):return self.jobs.submit({'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}},self.alice)[0]
    def orphan(self):return {'job_id':'job-'+'a'*12,'attempt_id':'attempt-'+'b'*32,'lease_sha256':'c'*64,'cleaned':False}
    def grant_item(self,grant,cleaned=False):return {'job_id':grant['job_id'],'attempt_id':grant['attempt_id'],
      'lease_sha256':hashlib.sha256(grant['lease_token'].encode()).hexdigest(),'cleaned':cleaned}
    def test_inventory_validation_is_bounded_unique_and_private_token_free(self):
        item=self.orphan();self.assertEqual(validate([item]),[item])
        for data in [None,[{**item,'lease_token':'private'}],[{**item,'cleaned':1}], [item,item],[item]*65]:
            with self.assertRaises(ValueError):validate(data)
    def test_unknown_local_attempt_blocks_other_node_without_reserving_anything(self):
        self.beat(self.nodes[0],[self.orphan()]);j=self.submit()
        self.assertEqual(self.jobs.inventory.reason(self.jobs.backend.physical_id),HOLD_REASON)
        self.assertIsNone(self.jobs.nodes.claim(self.nodes[1])['grant'])
        self.assertFalse(self.jobs.attempts(j['id']));self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)
    def test_disabling_node_or_restarting_store_does_not_clear_hold(self):
        self.beat(self.nodes[0],[self.orphan()]);self.jobs.nodes.disable(self.nodes[0])
        other=JobStore(self.runtime,self.config)
        self.assertEqual(other.inventory.reason(self.jobs.backend.physical_id),HOLD_REASON)
        with self.assertRaises(JobError):other.inventory.release(self.nodes[0],1)
    def test_reconciled_report_requires_explicit_revision_checked_release(self):
        self.beat(self.nodes[0],[self.orphan()]);self.beat(self.nodes[0])
        self.assertEqual(self.jobs.inventory.reason(self.jobs.backend.physical_id),HOLD_REASON)
        with self.assertRaises(JobError):self.jobs.inventory.release(self.nodes[0],0)
        result=self.jobs.inventory.release(self.nodes[0],1);self.assertFalse(result['execution_leases_released'])
        self.assertIsNone(self.jobs.inventory.reason(self.jobs.backend.physical_id))
        j=self.submit();self.assertEqual(self.jobs.nodes.claim(self.nodes[1])['grant']['job_id'],j['id'])
    def test_unresolved_or_stale_inventory_cannot_release_hold(self):
        self.beat(self.nodes[0],[self.orphan()])
        with self.assertRaises(JobError):self.jobs.inventory.release(self.nodes[0],1)
        self.beat(self.nodes[0])
        with self.jobs.connect() as db:db.execute('UPDATE worker_nodes SET last_seen=0 WHERE id=?',(self.nodes[0],))
        with self.assertRaises(JobError):self.jobs.inventory.release(self.nodes[0],1)
    def test_changed_mismatch_increments_hold_revision(self):
        item=self.orphan();self.beat(self.nodes[0],[item]);self.beat(self.nodes[0],[item])
        self.assertEqual(self.jobs.inventory.holds()[0]['revision'],1)
        self.beat(self.nodes[0],[{**item,'job_id':'job-'+'d'*12}])
        self.assertEqual(self.jobs.inventory.holds()[0]['revision'],2)
    def test_new_controller_instance_waits_for_each_enabled_entry(self):
        j=self.submit();self.jobs.controller_instance='instance-two'
        self.assertEqual(self.jobs.inventory.reason(self.jobs.backend.physical_id),WAIT_REASON)
        self.beat(self.nodes[0]);self.assertIsNone(self.jobs.nodes.claim(self.nodes[0])['grant'])
        self.beat(self.nodes[1]);self.assertEqual(self.jobs.nodes.claim(self.nodes[0])['grant']['job_id'],j['id'])
    def test_known_active_attempt_is_reconciled_and_recovery_is_not_blocked(self):
        j=self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        self.beat(self.nodes[0],[self.grant_item(grant)]);self.assertFalse(self.jobs.inventory.holds())
        self.beat(self.nodes[1],[self.orphan()])
        self.assertEqual(self.jobs.nodes.claim(self.nodes[0])['grant'],grant)
        self.assertEqual(self.jobs.get(j['id'])['state'],'reserved')
    def test_wrong_lease_and_dirty_terminal_attempt_are_not_reconciled(self):
        j=self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant'];item=self.grant_item(grant)
        self.beat(self.nodes[0],[{**item,'lease_sha256':'f'*64}]);self.assertTrue(self.jobs.inventory.holds())
        with self.jobs.transaction() as db:
            job=self.jobs.get(j['id'],db);job['state']='succeeded';self.jobs.write(db,job)
        self.beat(self.nodes[0],[item]);self.assertFalse(self.jobs.nodes.node(self.nodes[0])['report']['attempt_inventory']['reconciled'])
        self.beat(self.nodes[0],[{**item,'cleaned':True}]);self.assertTrue(self.jobs.nodes.node(self.nodes[0])['report']['attempt_inventory']['reconciled'])
    def test_old_worker_cannot_confirm_availability_in_strict_mode(self):
        with self.assertRaises(JobError):self.beat(self.nodes[0],caps=['start-ticket-v1','cleanup-before-complete-v1'])
    def test_cleaned_acknowledged_local_history_is_not_pending(self):
        directory=self.runtime/'node/attempts/fixture/control';directory.mkdir(parents=True)
        atomic_json(directory/'grant.json',{'job_id':'job-'+'a'*12,'attempt_id':'attempt-'+'b'*32,'lease_token':'private-only'})
        atomic_json(directory/'state.json',{'stage':'cleaned','controller_acknowledged':False})
        observed=local(self.runtime/'node');self.assertEqual(len(observed),1);self.assertNotIn('private-only',json.dumps(observed))
        atomic_json(directory/'state.json',{'stage':'cleaned','controller_acknowledged':True});self.assertEqual(local(self.runtime/'node'),[])

    def test_concurrent_heartbeat_snapshot_cannot_arrive_after_its_attempt_completion(self):
        # Real local inventory + real controller reconciliation, with only the
        # HTTP transport and the accepted completion commit held at barriers.
        # A valid old "running" snapshot must arrive before that attempt can
        # become terminal; otherwise normal concurrent tasks quarantine a host.
        from worker import Worker
        job=self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        node_root=self.runtime/'concurrent-node';control=node_root/'attempts'/grant['attempt_id']/'control'
        control.mkdir(parents=True);atomic_json(control/'grant.json',grant)
        atomic_json(control/'state.json',{'stage':'running'})
        artifacts=node_root/'fixture-artifacts';artifacts.mkdir();(artifacts/'environment.json').write_text('{}')
        arrived=threading.Event();release=threading.Event();blob=threading.Event();complete=threading.Event();errors=[]
        reply=threading.Event();fresh=threading.Event();heartbeats=[]
        state={'stage':'cleaned'}
        engine=SimpleNamespace(grant=grant,spool=None,control=control,artifacts=artifacts,state=state,
                               save=lambda:atomic_json(control/'state.json',state))
        def call(action,body):
            if action=='heartbeat':
                heartbeats.append(body)
                if len(heartbeats)==1:
                    self.assertFalse(body['attempt_inventory'][0]['cleaned']);arrived.set()
                    if not release.wait(5):raise RuntimeError('Test heartbeat barrier expired')
                else:self.assertTrue(body['attempt_inventory'][0]['cleaned'])
                value=self.jobs.nodes.heartbeat(self.nodes[0],body)
                if len(heartbeats)>1:fresh.set()
                return value
            if action=='blob':blob.set();return {}
            if action=='complete':
                complete.set()
                with self.jobs.transaction() as db:
                    current=self.jobs.get(job['id'],db);current['state']='succeeded';self.jobs.write(db,current)
                if not reply.wait(5):raise RuntimeError('Test completion ACK barrier expired')
                return {'accepted':True,'state':'succeeded'}
            raise AssertionError(action)
        config={'state_dir':str(node_root),'controller':'http://127.0.0.1:18888','node_key':'test-node-key',
                'node_id':self.nodes[0],
                'physical_id':self.jobs.backend.physical_id,'image':'sha256:'+'a'*64,
                'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':512,'shm_mib':0,'max_running':1}}
        def capture(fn):
            try:fn()
            except BaseException as error:errors.append(error)
        with patch('worker.NodeClient',return_value=SimpleNamespace(call=call)),\
             patch('docker_runtime.discover',return_value=({},{})),\
             patch('docker_runtime.image_available'),patch('environment_contract.verify_config',return_value=None):
            worker=Worker(config)
            def upload():
                engine.save()  # Actual worker cleanup precedes upload/complete.
                worker.upload(engine,{'status':'succeeded','execution_cleanup_confirmed':True})
            heart=threading.Thread(target=lambda:capture(worker.heartbeat));finish=threading.Thread(target=lambda:capture(upload))
            later=threading.Thread(target=lambda:capture(worker.heartbeat))
            try:
                heart.start();self.assertTrue(arrived.wait(2));finish.start();self.assertTrue(blob.wait(2))
                # Give the old unguarded implementation time to commit complete;
                # the corrected worker may upload blobs but must serialize it.
                complete.wait(.5);release.set();heart.join(3);self.assertTrue(complete.wait(2))
                # A large completion ACK must not block newer cleaned-state
                # heartbeats or make an otherwise healthy node look offline.
                later.start();self.assertTrue(fresh.wait(2));reply.set();finish.join(3);later.join(3)
                self.assertFalse(heart.is_alive());self.assertFalse(finish.is_alive());self.assertFalse(errors,errors)
                self.assertTrue(complete.is_set());self.assertEqual(self.jobs.get(job['id'])['state'],'succeeded')
                self.assertFalse(self.jobs.inventory.holds(),'A pre-cleanup heartbeat raced with a valid completion')
            finally:
                release.set();reply.set();heart.join(3)
                if finish.ident is not None:finish.join(3)
                if later.ident is not None:later.join(3)
                worker.lock.close();worker.state_lock.close()
    def test_reserved_start_waits_without_consuming_ticket_until_reconciled(self):
        self.submit();grant=self.jobs.nodes.claim(self.nodes[0])['grant']
        self.beat(self.nodes[1],[self.orphan()]);body={key:grant[key] for key in ['job_id','attempt_id','lease_token']}
        reply=self.jobs.nodes.authorize_start(self.nodes[0],body)
        self.assertTrue(reply['wait']);self.assertFalse(reply['start_permitted'])
        self.assertFalse(self.jobs.get(grant['job_id'])['_start_authorized'])
        self.beat(self.nodes[1]);self.jobs.inventory.release(self.nodes[1],1)
        self.assertTrue(self.jobs.nodes.authorize_start(self.nodes[0],body)['start_permitted'])
    def test_quarantine_prevents_offline_maintenance_even_when_job_table_is_empty(self):
        from maintenance import assert_quiescent
        from deployment import DeployError
        self.beat(self.nodes[0],[self.orphan()])
        with self.assertRaisesRegex(DeployError,'inventory hold'):assert_quiescent(self.runtime,'controller')
    def test_strict_controller_cannot_activate_a_release_without_inventory_support(self):
        from maintenance import require_features
        from deployment import DeployError
        atomic_json(self.runtime/'config.json',{'scheduler':{'require_attempt_inventory':True}})
        with self.assertRaisesRegex(DeployError,'downgrade'):require_features(self.runtime,'controller',{'files':{'nodes.py':{}}})
        require_features(self.runtime,'controller',{'files':{'attempt_inventory.py':{}}})

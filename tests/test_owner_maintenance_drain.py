"""Existing work admission during Root-owned drain; no real Root/NPU operations."""
from contextlib import ExitStack
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_nodes as builder
import owner_provision as master
import owner_maintenance_drain as drain
import unit_start
from lab_client import atomic_json
from deployment import DeployError
from jobs import JobError,now


class DrainAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=builder.NodeTests();self.fixture.setUp();self.jobs=self.fixture.jobs;self.nodes=self.fixture.nodes
        self.root=self.fixture.runtime;self.prefix=self.root/'installed';self.prefix.mkdir();self.patches=ExitStack()
        self.patches.enter_context(patch.dict(os.environ,{unit_start.PREFIX_ENV:str(self.prefix)}))
        self.patches.enter_context(patch('unit_start.deploy.load_installation',return_value=(self.prefix,{'state_dir':str(self.root)},self.prefix)))
        self.patches.enter_context(patch('unit_start.files._directory'))
        real=Path.lstat
        def root_gate(path,*args,**kwargs):
            info=real(path,*args,**kwargs)
            if path==self.prefix/master.GATE:
                values={k:getattr(info,k) for k in dir(info) if k.startswith('st_')};values['st_uid']=0
                return SimpleNamespace(**values)
            return info
        self.patches.enter_context(patch.object(Path,'lstat',root_gate))

    def tearDown(self):self.patches.close();self.fixture.tearDown()

    def gate(self,phase='draining',allowed=True):
        (self.prefix/master.JOURNAL).touch()
        atomic_json(self.prefix/master.GATE,{'format':master.FORMAT,'id':'f'*64,'prefix':str(self.prefix),
            'state_dir':str(self.root),'phase':phase,'service_allowed':allowed});(self.prefix/master.GATE).chmod(0o644)

    def finish(self,grant):
        auth=self.fixture.auth(grant);self.assertTrue(self.nodes.authorize_start(self.fixture.a,auth)['start_permitted'])
        raw=b'{}';self.nodes.upload(self.fixture.a,{**auth,'name':'environment.json','offset':0,'data':base64.b64encode(raw).decode()})
        return self.nodes.complete(self.fixture.a,{**auth,'result':{'status':'succeeded','exit_code':0,'termination_confirmed':True,'execution_cleanup_confirmed':True},
            'artifacts':[{'name':'environment.json','size':2,'sha256':hashlib.sha256(raw).hexdigest()}]})

    def test_drain_blocks_new_submissions_but_runs_both_existing_owners(self):
        a=self.fixture.submit(self.fixture.alice);b=self.fixture.submit(self.fixture.bob);self.gate()
        before=self.jobs.summary()['storage'] if 'storage' in self.jobs.summary() else None
        with self.assertRaises(JobError) as caught:self.fixture.submit(self.fixture.alice)
        self.assertEqual(caught.exception.status,503)
        state=drain.readiness(self.jobs);self.assertEqual(state['queued'],2);self.assertFalse(state['quiescent'])
        first=self.nodes.claim(self.fixture.a)['grant'];self.assertIn(first['job_id'],[a['id'],b['id']]);self.finish(first)
        second=self.nodes.claim(self.fixture.a)['grant'];self.assertNotEqual(first['job_id'],second['job_id']);self.finish(second)
        self.assertTrue(drain.readiness(self.jobs)['quiescent']);self.assertEqual(len(self.jobs.all()),2)
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)

    def test_missing_gate_pauses_reserved_start_without_failing_or_releasing_it(self):
        job=self.fixture.submit();grant=self.nodes.claim(self.fixture.a)['grant'];allocation=copy.deepcopy(self.jobs.get(job['id'])['allocation'])
        (self.prefix/master.JOURNAL).touch();auth=self.fixture.auth(grant)
        denied=self.nodes.authorize_start(self.fixture.a,auth);self.assertTrue(denied['wait']);self.assertFalse(denied['start_permitted'])
        self.assertEqual(self.jobs.get(job['id'])['allocation'],allocation);self.assertEqual(self.jobs.get(job['id'])['state'],'reserved')
        self.gate();self.assertTrue(self.nodes.authorize_start(self.fixture.a,auth)['start_permitted'])
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)

    def test_maintenance_still_blocks_claims_and_waits_for_reserved_start(self):
        a=self.fixture.submit();b=self.fixture.submit(self.fixture.bob);grant=self.nodes.claim(self.fixture.a)['grant'];self.gate('maintenance',False)
        denied=self.nodes.authorize_start(self.fixture.a,self.fixture.auth(grant));self.assertTrue(denied['wait'])
        self.assertIsNone(self.nodes.claim(self.fixture.b)['grant']);self.assertEqual(self.jobs.get(b['id'])['allocation'],{})
        self.assertFalse(drain.readiness(self.jobs)['quiescent'])

    def test_inventory_hold_and_unknown_attempt_remain_reserved(self):
        job=self.fixture.submit();grant=self.nodes.claim(self.fixture.a)['grant'];self.gate()
        with self.jobs.transaction() as db:
            j=self.jobs.get(job['id'],db);j['state']='needs_attention';self.jobs.write(db,j)
            db.execute('INSERT INTO worker_inventory_holds VALUES(?,?,?,?,?,?)',(self.fixture.a,self.jobs.backend.physical_id,1,1,'{}',now()))
        state=drain.readiness(self.jobs);self.assertEqual((state['active'],state['unknown'],state['inventory_holds']),(1,1,1));self.assertFalse(state['quiescent'])
        self.assertEqual(self.jobs.get(job['id'])['allocation'],grant['allocation'])
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_startup_hold_has_priority_over_a_draining_owner_gate(self):
        self.fixture.submit();self.gate();(self.prefix/unit_start.JOURNAL).touch()
        self.assertEqual(unit_start.admission_reason(self.root),unit_start.HOLD)
        self.assertEqual(unit_start.admission_reason(self.root,submit=True),unit_start.HOLD)
        self.assertIsNone(self.nodes.claim(self.fixture.a)['grant'])

    def test_abort_style_gate_removal_restores_new_submission_and_original_queue(self):
        old=self.fixture.submit();self.gate()
        (self.prefix/master.GATE).unlink();(self.prefix/master.JOURNAL).unlink()
        new=self.fixture.submit(self.fixture.bob);self.assertNotEqual(old['id'],new['id'])
        self.assertIsNotNone(self.nodes.claim(self.fixture.a)['grant']);self.assertEqual(len(self.jobs.all()),2)

    def test_submit_race_is_rechecked_before_allocating_storage(self):
        request={'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}}
        with patch('unit_start.admission_reason',side_effect=[None,unit_start.OWNER_HOLD]),self.assertRaises(JobError):self.jobs.submit(request,self.fixture.alice)
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM storage_allocations WHERE kind='archive'").fetchone()[0],0)

    def test_invalid_gate_does_not_unblock_old_claims(self):
        self.fixture.submit();self.gate('draining',False)
        self.assertEqual(unit_start.admission_reason(self.root),unit_start.HOLD);self.assertIsNone(self.nodes.claim(self.fixture.a)['grant'])


class DrainSnapshotTests(unittest.TestCase):
    def record(self):
        node='worker-'+'a'*12
        return {'nodes':{node:{'physical_id':'d'*64}},'request':{'service':{'mode':'process'}},'phase':'drained',
            'drain':{'format':drain.FORMAT,'controller_resume':True,'nodes':{node:{'prefix':'/opt/node','state_dir':'/var/lib/node',
                'release_id':'b'*64,'configuration_sha256':'c'*64,'resume':True}}}}

    def test_snapshot_mapping_cannot_redirect_an_original_participant(self):
        r=self.record();drain.check(r['drain'],r);node=next(iter(r['nodes']))
        self.assertEqual(drain.mapping(r,{}),{node:'/opt/node'})
        with self.assertRaises(DeployError):drain.mapping(r,{node:'/opt/other'})

    def test_invalid_snapshot_fields_fail_closed(self):
        for change in [{'controller_resume':1},{'nodes':[]},{'format':'wrong'}]:
            r=self.record();r['drain'].update(change)
            with self.subTest(change=change),self.assertRaises(DeployError):drain.check(r['drain'],r)
        r=self.record();next(iter(r['drain']['nodes'].values()))['state_dir']='/var/../other'
        with self.assertRaises(DeployError):drain.check(r['drain'],r)

    def test_node_cleanup_must_finish_before_stop(self):
        r=self.record()
        with patch('owner_maintenance_drain._node',return_value=(Path('/opt/node'),{'state_dir':'/var/lib/node'},{'running':True})),patch('maintenance.assert_quiescent',side_effect=DeployError('pending GC')):
            self.assertFalse(drain.local_quiescent(r)['ready'])

    def test_stopped_original_controller_is_not_started_implicitly(self):
        r=self.record();r['drain']['controller_resume']=False;r['phase']='held'
        with patch('owner_maintenance_drain.deploy.process_status',return_value={'running':False,'ready':False}),patch('owner_maintenance_drain._node',return_value=(Path('/opt/node'),{}, {'running':False,'ready':False})),patch('owner_maintenance_drain.deploy.start') as start:
            drain.resume_running(Path('/opt/controller'),r,Mock());start.assert_not_called()

    def test_running_snapshot_recovers_only_the_original_process_participants(self):
        r=self.record()
        with patch('owner_maintenance_drain.deploy.process_status',return_value={'running':False,'ready':False}),patch('owner_maintenance_drain._node',return_value=(Path('/opt/node'),{}, {'running':False,'ready':False})),patch('owner_maintenance_drain.deploy.start',return_value={'running':True,'ready':True}) as start:
            drain.resume_running(Path('/opt/controller'),r,Mock())
            self.assertEqual([c.args[0] for c in start.call_args_list],[Path('/opt/controller'),Path('/opt/node')])

    def test_uncertain_process_observation_does_not_launch_a_duplicate(self):
        r=self.record()
        with patch('owner_maintenance_drain.deploy.process_status',return_value={'running':True,'ready':False}),patch('owner_maintenance_drain._node',return_value=(Path('/opt/node'),{}, {'running':True,'ready':False})),patch('owner_maintenance_drain.deploy.start') as start:
            drain.resume_running(Path('/opt/controller'),r,Mock());start.assert_not_called()

    def test_abort_before_stop_does_not_touch_local_nodes(self):
        r=self.record();r['phase']='held'
        with patch('owner_maintenance_drain.deploy.start',return_value={'running':True,'ready':True}) as start,patch('owner_maintenance_drain._node') as node:
            self.assertTrue(drain.restore(Path('/opt/controller'),r));node.assert_not_called();start.assert_called_once_with(Path('/opt/controller'))

    def test_abort_after_stop_restores_controller_then_original_nodes(self):
        r=self.record()
        with patch('owner_maintenance_drain.deploy.start',return_value={'running':True,'ready':True}) as start,patch('owner_maintenance_drain._node',return_value=(Path('/opt/node'),{},{})):
            self.assertTrue(drain.restore(Path('/opt/controller'),r));self.assertEqual([c.args[0] for c in start.call_args_list],[Path('/opt/controller'),Path('/opt/node')])

    def test_reopen_crash_keeps_stopped_nodes_in_abort_recovery(self):
        r=self.record();saved=copy.deepcopy(r);save=Mock();publish=Mock()
        with patch('owner_maintenance_drain.restore',side_effect=RuntimeError('restart response lost')):
            with self.assertRaises(RuntimeError):drain.reopen(Path('/opt/controller'),r,save,publish)
        self.assertEqual(r,saved);save.assert_not_called();publish.assert_called_once_with('draining',True)
        with patch('owner_maintenance_drain.deploy.start',return_value={'running':True,'ready':True}) as start,patch('owner_maintenance_drain._node',return_value=(Path('/opt/node'),{},{})):
            self.assertTrue(drain.restore(Path('/opt/controller'),r))
            self.assertEqual([c.args[0] for c in start.call_args_list],[Path('/opt/controller'),Path('/opt/node')])

    def test_partial_reopen_preserves_durable_stop_obligation(self):
        r=self.record();save=Mock()
        with patch('owner_maintenance_drain.restore',return_value=False):
            self.assertFalse(drain.reopen(Path('/opt/controller'),r,save,Mock()))
        self.assertEqual(r['phase'],'drained');save.assert_not_called()

    def test_completed_reopen_saves_held_only_after_process_restoration(self):
        r=self.record();events=[]
        def restored(*args,**kwargs):
            self.assertEqual(r['phase'],'drained');self.assertTrue(kwargs['nodes']);events.append('restore');return True
        with patch('owner_maintenance_drain.restore',side_effect=restored):
            self.assertTrue(drain.reopen(Path('/opt/controller'),r,lambda:events.append(r['phase']),lambda *args:events.append('gate')))
        self.assertEqual(events,['gate','restore','held'])

    def test_invalid_readiness_cannot_admit_maintenance(self):
        valid={'quiescent':True,'queued':0,'active':0,'unknown':0,'inventory_holds':0,'reason':None};drain.status(valid)
        for change in [{'queued':1},{'active':1},{'inventory_holds':1},{'unknown':1},{'quiescent':1}]:
            with self.subTest(change=change),self.assertRaises(DeployError):drain.status({**valid,**change})


if __name__=='__main__':unittest.main()

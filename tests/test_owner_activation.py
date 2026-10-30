"""Activation transaction/recovery gates; native quota and HTTP use Root fixture."""
import base64
import copy
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_handoff as builder
from test_environment_contract import contract
from environment_contract import fingerprint
from workflow_spec import CAPABILITIES
from owner_activation import Activation,request
from jobs import JobError
from lab_client import private_profile,atomic_json
from maintenance import assert_quiescent,require_features
from deployment import DeployError


class OwnerActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=builder.OwnerHandoffTests();self.fixture.stage_archive_mib=getattr(self,'stage_archive_mib',16);self.fixture.stage_hard_mib=64
        self.fixture.stage_npu=getattr(self,'stage_npu',0)
        self.fixture.setUp();self.jobs=self.fixture.jobs;self.root=self.fixture.root
        self.jobs.access.config.update(host='127.0.0.1',port=18765)
        with self.jobs.transaction() as db:
            profile=self.jobs.nodes.node(self.fixture.node,db)['profile']
            profile.update(templates=['cpu_demo','ir_v1'],environment_manifest=contract(),python='/usr/bin/python3')
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.fixture.node))
        self.fixture.approve();ack=self.fixture.h.commit(self.fixture.node,{'receipt':self.fixture.receipt()})['ack']
        self.fixture.h.ready(self.fixture.node,{'plan_id':self.fixture.plan['id'],'commit_ack_id':ack['ack_id']});self.fixture.beat()
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.fixture.node,db);profile=node['profile'];profile['spool_storage']={'fixture':True}
            report=node['report'];report.update(capabilities=sorted(set(report['capabilities'])|CAPABILITIES|{'node-spool-v1'}),disk_free_inodes=262144,
                environment_sha256=fingerprint(contract()),finite_spool={'status':'ok'},artifact_gc_ready=True)
            record=self.fixture.h.get(self.fixture.plan['id'],db);record.update(after_profile=profile,node_online_seen=True);self.fixture.h.save(db,record)
            db.execute('UPDATE worker_nodes SET profile=?,report=? WHERE id=?',(json.dumps(profile),json.dumps(report),self.fixture.node))
        self.spec={'owner':'bob','stage_id':self.fixture.stage['stage_id'],
                   'required_nodes':[{'node_id':self.fixture.node,'plan_id':self.fixture.plan['id']}]}
        self.activation=Activation(self.jobs);self.group=patch('owner_activation.os.getgroups',return_value=[23832]);self.group.start()
        self.server_groups=patch.object(self.activation,'controller_groups',return_value={23832});self.server_groups.start()

    def tearDown(self):self.server_groups.stop();self.group.stop();self.fixture.tearDown()

    def run_activation(self):return self.activation.activate(self.spec)

    def alter_node(self,change):
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.fixture.node,db);change(node)
            db.execute('UPDATE worker_nodes SET profile=?,report=?,last_seen=? WHERE id=?',
                (json.dumps(node['profile']),json.dumps(node['report']),node['last_seen'],self.fixture.node))

    def test_ready_activation_and_replay_issue_exactly_one_private_credential(self):
        first=self.run_activation();again=self.run_activation()
        self.assertEqual(first['id'],again['id']);self.assertFalse(first['reused']);self.assertTrue(again['reused'])
        self.assertFalse({'password','token','salt','password_hash'}&set(first))
        client=private_profile(first['credentials']);self.assertIsNotNone(self.jobs.access.authenticate('Bearer '+client['token']))
        basic='Basic '+base64.b64encode(('bob:'+client['password']).encode()).decode()
        self.assertIsNotNone(self.jobs.access.authenticate(basic))
        self.assertEqual(Path(first['credentials']).stat().st_mode&0o777,0o600)
        with self.jobs.access.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tokens WHERE user='bob'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM owner_activations').fetchone()[0],1)
        with self.assertRaises(DeployError):require_features(self.root,'controller',{'files':{'owner_handoff.py':{}}})

    def test_sequential_crash_boundaries_resume_original_credentials(self):
        class Crash(Exception):pass
        identifier=None
        for boundary in ['intent','trust','client','before_commit','committed','receipt','published']:
            def crash(name):
                if name==boundary:raise Crash()
            with patch('owner_activation._checkpoint',side_effect=crash),self.assertRaises(Crash):self.run_activation()
            pending=self.root/'owner-activations/bob.pending.json'
            if pending.exists():
                intent=json.loads(pending.read_text())
                if identifier:self.assertEqual(identifier,intent['id'])
                identifier=intent['id']
            if boundary in {'intent','trust','client','before_commit'}:self.assertIsNone(self.jobs.access.user('bob'))
            if boundary!='published':
                with self.assertRaisesRegex(DeployError,'pending owner activation'):assert_quiescent(self.root,'controller')
        done=self.run_activation();self.assertEqual(done['id'],identifier)
        self.assertFalse((self.root/'owner-activations/bob.pending.json').exists())

    def test_stale_or_revoked_node_cannot_activate_from_historical_online_flag(self):
        self.alter_node(lambda n:n.update(last_seen=time.time()-60))
        with self.assertRaisesRegex(JobError,'stale'):self.run_activation()
        self.assertIsNone(self.jobs.access.user('bob'))
        self.jobs.nodes.disable(self.fixture.node)
        with self.assertRaises(JobError):self.run_activation()
        self.assertFalse((self.root/'owner-activations/bob.pending.json').exists())

    def test_profile_change_invalidates_exact_handoff_contract(self):
        self.alter_node(lambda n:n['profile'].update(policy_revision=99))
        with self.assertRaisesRegex(JobError,'unchanged'):self.run_activation()

    def test_missing_storage_groups_or_failed_current_storage_are_rejected(self):
        with patch('owner_activation.os.getgroups',return_value=[]),patch('owner_activation.os.getegid',return_value=999),self.assertRaisesRegex(JobError,'supplementary'):self.run_activation()
        self.alter_node(lambda n:n['report']['workspace']['slots'][-1].update(status='error'))
        with self.assertRaisesRegex(JobError,'slots'):self.run_activation()

    def test_missing_environment_spool_inventory_or_capabilities_reject(self):
        original=self.jobs.nodes.node(self.fixture.node)['report']
        for changes in [{'environment_sha256':'0'*64},{'finite_spool':{'status':'error'}},{'artifact_gc_ready':False},{'capabilities':[]}]:
            self.alter_node(lambda n:n.update(report={**original,**changes}))
            with self.subTest(changes=changes),self.assertRaises(JobError):self.run_activation()
        self.jobs.policy['require_attempt_inventory']=True
        self.alter_node(lambda n:n.update(report=original))
        with self.assertRaisesRegex(JobError,'inventory'):self.run_activation()

    def test_explicit_required_nodes_cannot_silently_skip_missing_peer(self):
        spec=copy.deepcopy(self.spec);spec['required_nodes'].append({'node_id':'worker-'+'f'*12,'plan_id':'e'*64})
        with self.assertRaises(JobError):self.activation.activate(spec)
        self.assertFalse((self.root/'owner-activations/bob.pending.json').exists())
        for value in [[],[self.spec['required_nodes'][0]]*2,[{'node_id':'../x','plan_id':'e'*64}]]:
            with self.subTest(value=value),self.assertRaises(JobError):request({**self.spec,'required_nodes':value})

    def test_preexisting_profile_or_symlink_cannot_be_overwritten(self):
        directory=self.root/'personas/bob';directory.mkdir(parents=True,mode=0o700)
        target=directory/'client.json';atomic_json(target,{'private':'existing'})
        with self.assertRaises(JobError):self.run_activation()
        self.assertEqual(json.loads(target.read_text()),{'private':'existing'})
        target.unlink();outside=self.root/'sentinel';atomic_json(outside,{'private':'target'});target.symlink_to(outside)
        with self.assertRaises(OSError):self.run_activation()
        self.assertEqual(json.loads(outside.read_text()),{'private':'target'})

    def test_disabled_or_revoked_after_commit_is_never_revived_by_recovery(self):
        class Crash(Exception):pass
        def crash(name):
            if name=='committed':raise Crash()
        with patch('owner_activation._checkpoint',side_effect=crash),self.assertRaises(Crash):self.run_activation()
        self.jobs.access.disable_user('bob')
        with self.assertRaisesRegex(JobError,'revive'):self.run_activation()
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_changed_contract_during_pending_does_not_replace_credentials(self):
        class Crash(Exception):pass
        with patch('owner_activation._checkpoint',side_effect=Crash),self.assertRaises(Crash):self.run_activation()
        pending=self.root/'owner-activations/bob.pending.json';original=pending.read_bytes()
        self.alter_node(lambda n:n['profile'].update(policy_revision=44))
        with self.assertRaises(JobError):self.run_activation()
        self.assertEqual(pending.read_bytes(),original)

    def test_npu_quota_cannot_use_cpu_only_readiness(self):
        with self.jobs.access.connect() as db:
            profile=json.loads(db.execute("SELECT profile FROM users WHERE id='bob'").fetchone()[0]);profile['quota']['npu']=1
        from owner_activation import request
        with self.jobs.transaction() as db,self.assertRaisesRegex(JobError,'NPU activation'):self.activation.readiness(request(self.spec),db,profile)

    def test_current_admission_headroom_is_required_before_credentials_exist(self):
        original=self.jobs.nodes.node(self.fixture.node)['report']
        for key,value in [('disk_free_bytes',128*1024**2),('memory_available_bytes',512*1024**2)]:
            self.alter_node(lambda n:n.update(report={**original,key:value}))
            with self.subTest(key=key),self.assertRaisesRegex(JobError,'headroom'):self.run_activation()
        self.assertFalse((self.root/'personas/bob/client.json').exists())

    def test_spool_workflow_inode_requirement_is_shared_with_actual_admission(self):
        self.alter_node(lambda n:n['report'].update(disk_free_inodes=4096))
        with self.assertRaisesRegex(JobError,'inode headroom'):self.run_activation()
        self.assertFalse((self.root/'personas/bob/client.json').exists())

    def test_malformed_provisioned_marker_cannot_open_login(self):
        first=self.run_activation()
        with self.jobs.access.connect() as db:
            p=json.loads(db.execute("SELECT profile FROM users WHERE id='bob'").fetchone()[0])
        for nodes in [[],['not-a-node'],[{'node_id':self.fixture.node,'plan_id':'bad'}],self.spec['required_nodes']*2]:
            current=copy.deepcopy(p);current['provisioned']['required_nodes']=nodes
            with self.jobs.access.connect() as db:db.execute("UPDATE users SET profile=? WHERE id='bob'",(json.dumps(current),))
            self.assertIsNone(self.jobs.access.user('bob'))

    def test_cli_new_groups_cannot_hide_running_controller_old_groups(self):
        with patch.object(self.activation,'controller_groups',return_value={999}),self.assertRaisesRegex(JobError,'supplementary'):self.run_activation()
        self.assertFalse((self.root/'personas/bob/client.json').exists())

    def test_controller_process_identity_capability_and_groups_are_checked(self):
        from jobs import identity
        from owner_activation import CAPABILITY
        actual=identity(os.getpid());atomic_json(self.root/'server-ready.json',{**actual,'capabilities':[CAPABILITY]})
        groups=Activation.controller_groups(self.activation);self.assertIn(os.getegid(),groups)
        for bad in [{**actual,'capabilities':[]},{**actual,'start_ticks':actual['start_ticks']+1,'capabilities':[CAPABILITY]}]:
            atomic_json(self.root/'server-ready.json',bad)
            with self.assertRaises(JobError):Activation.controller_groups(self.activation)

    def test_staged_quotas_must_fit_minimum_task_and_archive_metadata(self):
        with self.jobs.access.connect() as db:original=json.loads(db.execute("SELECT profile FROM users WHERE id='bob'").fetchone()[0])
        for field,value in [('memory_mib',128),('archive_storage_mib',4)]:
            profile=copy.deepcopy(original);profile['quota'][field]=value
            with self.jobs.transaction() as db,self.assertRaisesRegex(JobError,'metadata reservation'):self.activation.readiness(self.spec,db,profile)

    def test_offline_relocation_preserves_issued_marker_and_rebases_delivery_receipt(self):
        from maintenance import relocate
        first=self.run_activation();target=self.root/'restored-state'
        config={**self.jobs.access.config,'monitor_local':False,'identity_workspace_root':str(self.root/'personas'),
                'scheduler':{'workspace_root':str(self.root),'local_enabled':False}}
        atomic_json(self.root/'config.json',config)
        relocate(self.root,self.root,target,'controller')
        with self.jobs.access.connect() as db:
            profile=json.loads(db.execute("SELECT profile FROM users WHERE id='bob'").fetchone()[0])
            record=json.loads(db.execute("SELECT record FROM owner_activations WHERE owner='bob'").fetchone()[0])
        self.assertEqual(profile['workspace'],str(target/'bob'));self.assertEqual(profile['provisioned']['id'],first['id'])
        self.assertEqual(record['credentials'],str(target/'personas/bob/client.json'))
        self.assertEqual(record,json.loads((self.root/'owner-activations/bob.json').read_text()))
        self.assertIsNotNone(self.jobs.access.user('bob'))


if __name__=='__main__':unittest.main()

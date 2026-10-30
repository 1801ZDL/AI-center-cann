"""Active-owner expansion, stale authority and immutable-resource recovery."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_activation as activation_builder
import test_owner_handoff as handoff_builder
from node_owner_admin import sha,_plan_check
from persistent_cache import fingerprint
from owner_node_grant import Expansion,CAPABILITY
from jobs import identity,JobError
from lab_client import atomic_json
from maintenance import require_features
from deployment import DeployError,CORE


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=activation_builder.OwnerActivationTests();self.fixture.setUp();self.jobs=self.fixture.jobs;self.h=self.jobs.nodes.handoff
        self.activation=self.fixture.run_activation();self.credentials=Path(self.activation['credentials'])
        self.credentials_sha=hashlib.sha256(self.credentials.read_bytes()).hexdigest()
        self.old_profile=self.jobs.nodes.node(self.fixture.fixture.node)['profile']
        # The activation unit fixture only needed a presence marker. Peer alias
        # checks need a real identity for that synthetic old spool as well.
        self.old_profile['spool_storage'].update(device=56999,path='/srv/old-spool',storage_id='c'*32)
        with self.jobs.transaction() as db:
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(self.old_profile),self.fixture.fixture.node))
            record=self.h.get(self.fixture.fixture.plan['id'],db);record['after_profile']=self.old_profile;self.h.save(db,record)
        value=copy.deepcopy(self.fixture.fixture.plan)
        def relocate(item):
            if isinstance(item,dict):return {relocate(k):relocate(v) for k,v in item.items()}
            if isinstance(item,list):return [relocate(v) for v in item]
            if isinstance(item,str):
                if item in {str(i)*32 for i in range(1,5)}:return str(int(item[0])+4)*32
                if item=='a'*32:return 'd'*32
                if item=='b'*32:return 'e'*32
                return item.replace('/srv/','/srv/expansion/')
            if type(item) is int and item in {23801,23802,54320,54321}:return item+100
            return item
        value=relocate(value)
        profile={**self.old_profile,**value['before']}
        for field in ['environment_manifest','spool_storage']:profile.pop(field,None)
        self.node,self.key=self.jobs.nodes.enroll(profile)
        value.update(node_id=self.node,prefix='/opt/expansion-node',state_dir=str(self.fixture.root/'expansion-node'))
        for snapshot in value['caches'].values():
            snapshot['state']['binding'].update(node_id=self.node,state_dir=value['state_dir'],
                policy_sha256=fingerprint(value['before']['persistent_cache']))
        value['id']=sha({k:v for k,v in value.items() if k!='id'});_plan_check(value);self.value=value
        self.expansion=Expansion(self.h)
        atomic_json(self.fixture.root/'server-ready.json',{**identity(os.getpid()),'capabilities':[CAPABILITY,'owner-handoff-v1']})

    def tearDown(self):self.fixture.tearDown()

    def grant(self):
        plan=self.expansion.plan(self.node,self.value);self.expansion.apply(plan);return plan

    def receipt(self):
        result=self.fixture.fixture.receipt()
        result.update(plan_id=self.value['id'],node_id=self.node,before_projection_sha256=sha(self.value['before']),after_projection=self.value['after'])
        return result

    def commit(self):return self.h.commit(self.node,{'receipt':self.receipt()})['ack']

    def ready(self,ack):return self.h.ready(self.node,{'plan_id':self.value['id'],'commit_ack_id':ack['ack_id']})['ack']

    def beat(self):
        holder=object.__new__(handoff_builder.OwnerHandoffTests);holder.jobs=self.jobs;holder.node=self.node
        return holder.beat()

    def unchanged(self):
        self.assertEqual(hashlib.sha256(self.credentials.read_bytes()).hexdigest(),self.credentials_sha)
        self.assertEqual(self.jobs.nodes.node(self.fixture.fixture.node)['profile'],self.old_profile)
        with self.jobs.access.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM tokens WHERE user='bob'").fetchone()[0],1)

    def test_separate_grant_completes_without_new_credentials_or_old_node_changes(self):
        with self.assertRaisesRegex(JobError,'credential-free'):self.h.approve(self.node,self.value)
        plan=self.expansion.plan(self.node,self.value)
        self.assertEqual(self.h.status(self.node),[])
        self.assertNotIn('token',json.dumps(plan));self.assertNotIn('password_hash',json.dumps(plan))
        first=self.expansion.apply(plan);again=self.expansion.apply(plan)
        self.assertFalse(first['reused']);self.assertTrue(again['reused']);ack=self.commit();self.ready(ack)
        with self.jobs.connect() as db:self.assertIsNotNone(self.h.pending(self.node,db))
        self.beat();self.assertTrue(self.h.status(self.node)[0]['node_online_seen'])
        with self.jobs.connect() as db:self.assertIsNone(self.h.pending(self.node,db))
        self.assertTrue(self.expansion.apply(plan)['historical_receipt_only']);self.unchanged()

    def test_changed_then_restored_quota_invalidates_unapplied_plan(self):
        plan=self.expansion.plan(self.node,self.value)
        self.jobs.access.set_quota('bob',{'cpu':2});self.jobs.access.set_quota('bob',{'cpu':1})
        with self.assertRaisesRegex(JobError,'stale'):self.expansion.apply(plan)
        self.assertEqual(self.h.status(self.node),[]);self.unchanged()

    def test_explicit_reauthorization_preserves_committed_ack_and_plan(self):
        original=self.grant();ack=self.commit()
        self.jobs.access.set_quota('bob',{'cpu':2});self.jobs.access.set_quota('bob',{'cpu':1})
        for operation in [self.commit,lambda:self.ready(ack),lambda:self.expansion.apply(original)]:
            with self.assertRaisesRegex(JobError,'reauthorize'):operation()
        with self.assertRaises(JobError):self.expansion.plan(self.node,self.value)
        new=self.expansion.plan(self.node,self.value,reauthorize=True);self.expansion.apply(new)
        self.assertEqual(self.commit(),ack);self.ready(ack);self.beat();self.unchanged()
        with self.assertRaisesRegex(JobError,'superseded'):self.expansion.apply(original)
        with self.assertRaises(JobError):self.expansion.plan(self.node,self.value,reauthorize=True)

    def test_reauthorization_before_local_commit_keeps_same_resource_contract(self):
        self.grant();self.jobs.access.set_quota('bob',{'cpu':1})
        new=self.expansion.plan(self.node,self.value,reauthorize=True);self.expansion.apply(new)
        ack=self.commit();self.ready(ack);self.beat();self.unchanged()

    def test_authority_change_after_ready_blocks_first_heartbeat_and_claim(self):
        self.grant();ack=self.commit();self.ready(ack)
        self.jobs.access.add_member('team','bob');self.jobs.access.remove_member('team','bob')
        with self.assertRaisesRegex(JobError,'reauthorize'):self.beat()
        self.assertFalse(self.h.status(self.node)[0]['node_online_seen'])
        new=self.expansion.plan(self.node,self.value,reauthorize=True);self.expansion.apply(new)
        self.assertEqual(self.commit(),ack);self.ready(ack);self.beat();self.unchanged()

    def test_disabled_owner_cannot_be_reauthorized_or_reactivated_by_ack(self):
        self.grant();ack=self.commit();self.jobs.access.disable_user('bob')
        for operation in [lambda:self.ready(ack),self.commit,lambda:self.expansion.plan(self.node,self.value,reauthorize=True)]:
            with self.assertRaisesRegex(JobError,'enabled'):operation()
        self.assertIsNone(self.jobs.access.user('bob'));self.unchanged()

    def test_completed_replays_do_not_revive_disabled_owner(self):
        plan=self.grant();ack=self.commit();ready=self.ready(ack);self.beat();self.jobs.access.disable_user('bob')
        self.assertEqual(self.commit(),ack);self.assertEqual(self.ready(ack),ready)
        self.assertTrue(self.expansion.apply(plan)['historical_receipt_only'])
        self.assertIsNone(self.jobs.access.user('bob'));self.unchanged()

    def test_renewal_revocation_environment_and_direct_sql_are_detected(self):
        plan=self.grant();token=self.jobs.access.issue_token('bob',['jobs:read'])
        with self.assertRaises(JobError):self.commit()
        self.jobs.access.revoke_token(token)
        new=self.expansion.plan(self.node,self.value,reauthorize=True);self.expansion.apply(new)
        self.jobs.access.grant_environment('bob','worker-python')
        with self.assertRaises(JobError):self.commit()
        new=self.expansion.plan(self.node,self.value,reauthorize=True);self.expansion.apply(new)
        with self.jobs.access.connect() as db:
            db.execute("UPDATE users SET disabled=1 WHERE id='bob'");db.execute("UPDATE users SET disabled=0 WHERE id='bob'")
        with self.assertRaises(JobError):self.commit()

    def test_pending_slots_are_reserved_against_another_node_enrollment(self):
        self.grant();profile=self.jobs.nodes.node(self.node)['profile']
        peer={**profile,'name':'conflicting_expansion','users':{'bob':self.value['after']['users']['bob']},
            'workspace':{'slots':self.value['after']['workspace']['slots'][-1:]},
            'persistent_cache':{'slots':self.value['after']['persistent_cache']['slots'][-1:]}}
        with self.assertRaises(JobError):self.jobs.nodes.enroll(peer)
        self.unchanged()

    def test_stale_node_profile_plan_tampering_and_other_node_are_rejected(self):
        plan=self.expansion.plan(self.node,self.value)
        self.jobs.nodes.set_templates(self.node,['cpu_demo'])
        with self.assertRaises(JobError):self.expansion.apply(plan)
        with self.assertRaises(JobError):self.expansion.apply({**plan,'service_uid':True})
        with self.assertRaises(JobError):self.expansion.plan(self.fixture.fixture.node,self.value)

    def test_live_service_capability_is_required_and_downgrade_is_blocked(self):
        ready=self.fixture.root/'server-ready.json'
        atomic_json(ready,{**identity(os.getpid()),'capabilities':[]})
        with self.assertRaisesRegex(JobError,'live'):self.expansion.plan(self.node,self.value)
        atomic_json(ready,{**identity(os.getpid()),'capabilities':[CAPABILITY]});self.grant()
        manifest={'files':{name:{} for name in CORE if name!='owner_node_grant.py'}}
        with self.assertRaisesRegex(DeployError,'expansion'):require_features(self.fixture.root,'controller',manifest)

    def test_identity_writer_lock_is_held_through_jobs_commit(self):
        import sqlite3
        with self.h.transaction() as (db,identities):
            peer=sqlite3.connect(self.jobs.access.path,timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):peer.execute("UPDATE users SET disabled=1 WHERE id='bob'")
                db.commit()
                with self.assertRaises(sqlite3.OperationalError):peer.execute("UPDATE users SET disabled=1 WHERE id='bob'")
            finally:peer.close()


if __name__=='__main__':unittest.main()

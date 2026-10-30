"""Central stage/Node plan authorization, pause, idempotent ACK and HTTP identity."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_node_owner_admin as builder
from jobs import JobStore,JobError,identity
from lab_client import atomic_json,Client,ClientError
from node_owner_admin import projection,sha
from owner_handoff import Handoff,ready_projection,validate_ack
from maintenance import assert_quiescent,require_features
from deployment import DeployError
from server import handler_class,ThreadingHTTPServer


class OwnerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.data=builder.NodeOwnerTests();self.data.setUp();self.root=self.data.state
        self.jobs=JobStore(self.root,{'username':'owner_admin','container':'zdl','password':'handoff-fixture-only',
                                     'scheduler':{'workspace_root':str(self.root)}})
        for owner in ['alice','bob']:(self.root/owner).mkdir(mode=0o700)
        self.jobs.access.create_user('alice',self.root/'alice',environments=['worker-python'])
        self.stage=self.jobs.access.stage_user('bob',self.root/'bob',quota={'npu':getattr(self,'stage_npu',0),'input_storage_mib':4,'archive_storage_mib':getattr(self,'stage_archive_mib',4)})
        profile={'name':'owner_node','physical_id':self.jobs.backend.physical_id,'users':self.data.before['users'],
            'workspace':self.data.before['workspace'],'persistent_cache':self.data.before['persistent_cache'],
            'image':'sha256:'+'a'*64,'templates':['cpu_demo'],
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':512,'shm_mib':0,'max_running':1}}
        self.node,self.key=self.jobs.nodes.enroll(profile);self.h=self.jobs.nodes.handoff
        self.plan=copy.deepcopy(self.data.value);self.plan.update(node_id=self.node,physical_id=profile['physical_id'])
        self.plan['request']['stage_id']=self.stage['stage_id'];self.plan['after']['owner_bindings']['bob']['stage_id']=self.stage['stage_id']
        for snapshot in self.plan['caches'].values():snapshot['state']['binding'].update(node_id=self.node,physical_id=profile['physical_id'])
        self.plan['id']=sha({k:v for k,v in self.plan.items() if k!='id'})
        self.storage=patch.object(self.h,'_storage',return_value={'storage_id':'a'*32,'gid':23832,'bytes':getattr(self,'stage_hard_mib',16)*1024**2,'inodes':128});self.storage.start()

    def tearDown(self):self.storage.stop();self.jobs.access.close();self.data.tearDown()

    def approve(self):return self.h.approve(self.node,self.plan)

    def receipt(self):
        return {'format':'ascend-lab-node-owner-receipt/v1','plan_id':self.plan['id'],'node_id':self.node,
            'physical_id':self.plan['physical_id'],'owner':'bob','stage_id':self.stage['stage_id'],'revision':1,
            'policy_revision':2,'before_projection_sha256':sha(self.plan['before']),'after_projection':self.plan['after'],
            'after_configuration_sha256':'f'*64,'completed_at':'fixture','central_acknowledgement_still_required':True}

    def beat(self):
        from workspace_slots import fingerprint,CAPABILITY
        from persistent_cache import fingerprint as cf,CAPABILITY as CC
        profile=self.jobs.nodes.node(self.node)['profile']
        body={'physical_id':profile['physical_id'],'protocol_version':1,'policy_revision':profile['policy_revision'],
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',CAPABILITY,CC,'uid-quota-workspace-v1'],
            'cpu_ids':profile['policy']['cpu_ids'],'memory_available_bytes':4*1024**3,'disk_free_bytes':4*1024**3,
            'workspace_observation':{'policy_sha256':fingerprint(profile['workspace']),
                'slots':[{'id':s['id'],'status':'ok','clean':True,'free_bytes':s['bytes'],'free_inodes':s['inodes']} for s in profile['workspace']['slots']]},
            'cache_observation':{'policy_sha256':cf(profile['persistent_cache']),
                'slots':[{'id':s['id'],'status':'ok','leased':False,'free_bytes':s['bytes'],'free_inodes':s['inodes'],'namespaces':0} for s in profile['persistent_cache']['slots']]}}
        return self.jobs.nodes.heartbeat(self.node,body)

    def test_real_stage_binding_approval_and_duplicate_are_stable(self):
        first=self.approve();again=self.approve();self.assertFalse(first['reused']);self.assertTrue(again['reused'])
        self.assertEqual(first['approval']['approval_id'],again['approval']['approval_id'])
        self.assertIsNone(self.jobs.access.user('bob'))
        wrong=copy.deepcopy(self.plan);wrong['request']['stage_id']='d'*64;wrong['after']['owner_bindings']['bob']['stage_id']='d'*64
        wrong['id']=sha({k:v for k,v in wrong.items() if k!='id'})
        with self.assertRaises(JobError):self.h.approve(self.node,wrong)

    def test_pending_handoff_pauses_claim_and_blocks_profile_edits_and_backup(self):
        self.approve();self.beat()
        job,_=self.jobs.submit({'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}},self.jobs.access.user('alice'))
        self.assertIsNone(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        for change in [lambda:self.jobs.nodes.set_templates(self.node,['cpu_demo']),
                       lambda:self.jobs.nodes.set_policy(self.node,self.jobs.nodes.node(self.node)['profile']['policy'],1)]:
            with self.assertRaises(JobError):change()
        self.jobs.cancel(job['id'])
        with self.assertRaisesRegex(DeployError,'owner handoffs'):assert_quiescent(self.root,'controller')
        with self.assertRaisesRegex(DeployError,'Owner handoff'):require_features(self.root,'controller',{'files':{'owner_identity.py':{}}})

    def test_commit_ready_replays_and_healthy_heartbeat_do_not_activate_owner(self):
        self.approve();receipt=self.receipt();commit=self.h.commit(self.node,{'receipt':receipt})['ack']
        self.assertEqual(commit,self.h.commit(self.node,{'receipt':receipt})['ack'])
        with self.jobs.connect() as db:self.assertIsNotNone(self.h.pending(self.node,db))
        ready=self.h.ready(self.node,{'plan_id':self.plan['id'],'commit_ack_id':commit['ack_id']})['ack']
        self.assertEqual(ready,self.h.ready(self.node,{'plan_id':self.plan['id'],'commit_ack_id':commit['ack_id']})['ack'])
        self.assertEqual(commit,self.h.commit(self.node,{'receipt':receipt})['ack'])
        with self.assertRaises(JobError):self.jobs.nodes.set_templates(self.node,['cpu_demo'])
        self.beat();self.assertTrue(self.h.status(self.node)[0]['node_online_seen'])
        self.jobs.nodes.set_templates(self.node,['cpu_demo'])
        self.assertEqual(projection(self.jobs.nodes.node(self.node)['profile']),ready_projection(self.plan['after'],'bob'))
        self.assertIsNone(self.jobs.access.user('bob'))

    def test_unapproved_wrong_receipt_or_wrong_ack_never_changes_profile(self):
        before=self.jobs.nodes.node(self.node)['profile']
        with self.assertRaises(JobError):self.h.commit(self.node,{'receipt':self.receipt()})
        self.approve()
        for key,value in [('owner','alice'),('revision',9),('revision',True),('central_acknowledgement_still_required',1),('after_configuration_sha256','bad')]:
            with self.subTest(key=key),self.assertRaises(JobError):self.h.commit(self.node,{'receipt':{**self.receipt(),key:value}})
        self.assertEqual(before,self.jobs.nodes.node(self.node)['profile'])
        commit=self.h.commit(self.node,{'receipt':self.receipt()})['ack']
        with self.assertRaises(JobError):self.h.ready(self.node,{'plan_id':self.plan['id'],'commit_ack_id':'0'*32})
        with self.assertRaises(JobError):self.h.commit(self.node,{'receipt':{**self.receipt(),'completed_at':'changed'}})

    def test_old_live_controller_or_changed_stage_is_rejected(self):
        atomic_json(self.root/'server-ready.json',identity(__import__('os').getpid()))
        with self.assertRaisesRegex(JobError,'Reload'):self.approve()
        (self.root/'server-ready.json').unlink()
        with self.jobs.access.connect() as db:db.execute('UPDATE users SET disabled=0 WHERE id=?',('bob',))
        with self.assertRaises(JobError):self.approve()

    def test_another_node_and_forged_local_ack_are_rejected(self):
        p=self.jobs.nodes.node(self.node)['profile'];other,key=self.jobs.nodes.enroll({k:v for k,v in p.items() if k not in {'workspace','persistent_cache'}})
        self.approve()
        with self.assertRaises(JobError):self.h.commit(other,{'receipt':self.receipt()})
        ack=self.h.commit(self.node,{'receipt':self.receipt()})['ack']
        validate_ack(ack,{'plan_id':self.plan['id']},'committed')
        with self.assertRaises(JobError):validate_ack({**ack,'projection_sha256':'0'*64},{'projection_sha256':ack['projection_sha256']},'committed')

    def test_active_execution_prevents_approval(self):
        job,_=self.jobs.submit({'environment':'worker-python','template':'cpu_demo','parameters':{'seconds':1}},self.jobs.access.user('alice'))
        with self.jobs.transaction() as db:
            row=self.jobs.get(job['id'],db);row.update(node_id=self.node,state='running');self.jobs.write(db,row)
        with self.assertRaisesRegex(JobError,'execution'):self.approve()

    def test_revoked_pending_node_still_holds_storage_allocation(self):
        self.approve();self.jobs.nodes.disable(self.node)
        candidate={'name':'replacement','physical_id':self.plan['physical_id'],'users':{'bob':{'uid':23802,'gid':23802}},
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':512,'shm_mib':0,'max_running':1},'templates':['cpu_demo'],
            'persistent_cache':{'slots':[self.plan['after']['persistent_cache']['slots'][-1]]}}
        with self.assertRaisesRegex(JobError,'aliases'):self.jobs.nodes.enroll(candidate)

    def test_key_rotation_keeps_approval_and_old_key_cannot_commit(self):
        self.approve();self.jobs.nodes.disable(self.node);new=self.jobs.nodes.rotate_key(self.node)
        self.assertIsNone(self.jobs.nodes.authenticate('Bearer '+self.key))
        self.assertEqual(self.jobs.nodes.authenticate('Bearer '+new),self.node)
        self.assertEqual(self.h.commit(self.node,{'receipt':self.receipt()})['ack']['phase'],'committed')

    def test_storage_and_execution_conflicts_refuse_before_approval(self):
        with patch.object(self.h,'_storage',side_effect=JobError('no hard group',409)),self.assertRaises(JobError):self.approve()
        with self.jobs.transaction() as db:
            profile=self.jobs.nodes.node(self.node,db)['profile'];profile['policy_revision']=2
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(profile),self.node))
        with self.assertRaises(JobError):self.approve()
        self.assertEqual(self.h.status(self.node),[])

    def test_http_requires_node_credentials_for_owner_commit(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),self.jobs.access.config,self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)
            token=self.jobs.access.issue_token('alice',{'jobs:read'})
            client=Client({'controller':url,'token':token})
            with self.assertRaisesRegex(ClientError,'401'):client.request('/api/worker/owner-commit',{'receipt':self.receipt()})
            client=Client({'controller':url,'token':self.key});self.approve()
            self.assertEqual(client.request('/api/worker/owner-commit',{'receipt':self.receipt()})['ack']['phase'],'committed')
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()

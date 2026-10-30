"""Protocol-only access evidence across real central owner-append transactions."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_owner_handoff as builder
import test_owner_node_grant as expansion_builder
from device_qualification import CAPABILITY,PROBE,decision
from jobs import now,JobError,JobStore
from workflow_spec import CAPABILITIES,runtime_manifest
import qualification_compatibility as compat
from maintenance import assert_quiescent,require_features
from deployment import DeployError


class QualificationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.OwnerHandoffTests();self.base.setUp();self.jobs=self.base.jobs;self.node=self.base.node
        self.jobs.access.set_quota('alice',{'npu':1})
        self.policy={'device_ids':[0,1],'visible_ids':{'0':0,'1':0},'device_gid':1001,
            'management_devices':['/dev/davinci_manager'],'driver_paths':['/usr/local/Ascend/driver'],
            'query_tool':'/usr/local/sbin/npu-smi','management_env':'/data/pri/cann/set_env.sh'}
        with self.jobs.transaction() as db:
            node=self.jobs.nodes.node(self.node,db);p=node['profile'];p.update(npu=self.policy,templates=[PROBE,'accuracy_v1','cpu_demo'])
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(p),self.node))
        self.beat()

    def tearDown(self):self.base.tearDown()

    def beat(self,**changes):
        from workspace_slots import fingerprint as work_sha,CAPABILITY as WORK
        from persistent_cache import fingerprint as cache_sha,CAPABILITY as CACHE
        from npu_device import fingerprint as npu_sha
        p=self.jobs.nodes.node(self.node)['profile']
        body={'physical_id':p['physical_id'],'protocol_version':1,'policy_revision':p['policy_revision'],
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',WORK,CACHE,'uid-quota-workspace-v1','npu-device-whitelist-v1',CAPABILITY,*CAPABILITIES],
            'cpu_ids':p['policy']['cpu_ids'],'memory_available_bytes':4*1024**3,'disk_free_bytes':4*1024**3,
            'access_host_sha256':'b'*64,'access_runtime_files':runtime_manifest(),
            'npu_observation':{'status':'ok','sampled_at':now(),'policy_sha256':npu_sha(p['npu']),
                'devices':[{'id':i,'name':'protocol-only','health':'OK','utilization_pct':0,'process_count':0} for i in p['npu']['device_ids']]},
            'workspace_observation':{'policy_sha256':work_sha(p['workspace']),
                'slots':[{'id':s['id'],'status':'ok','clean':True,'free_bytes':s['bytes'],'free_inodes':s['inodes']} for s in p['workspace']['slots']]},
            'cache_observation':{'policy_sha256':cache_sha(p['persistent_cache']),
                'slots':[{'id':s['id'],'status':'ok','leased':False,'free_bytes':s['bytes'],'free_inodes':s['inodes'],'namespaces':0} for s in p['persistent_cache']['slots']]},**changes}
        return self.jobs.nodes.heartbeat(self.node,body)

    def qualify(self,passed=True):
        job,_=self.jobs.submit({'environment':'worker-python','template':PROBE,
            'parameters':{'node_id':self.node,'device_id':0}},self.jobs.access.user('alice'))
        g=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(g['job_id'],job['id'])
        auth={k:g[k] for k in ['job_id','attempt_id','lease_token']};self.assertTrue(self.jobs.nodes.authorize_start(self.node,auth)['start_permitted'])
        uid=self.jobs.nodes.node(self.node)['profile']['users']['alice']['uid']
        receipt={'schema':'ascend-lab-environment/v1','locked':passed,'workload_started':False,'probe_only':True,'probe_exit_code':0 if passed else 78,
            'device_access':{'schema':'ascend-lab-device-access/v1','status':'passed' if passed else 'failed','passed':passed,'requested_device_ids':[0],
            'opened_device_ids':[0] if passed else [],'physical_device_files':[0],'errors':[] if passed else [{'errno':16}],
            'uid':uid,'euid':uid,'cap_effective':'0000000000000000','cap_permitted':'0000000000000000','no_new_privileges':'1'}}
        if not passed:receipt['failure_stage']='npu_device_access'
        meta={'runtime':'managed-docker','task_uid':uid,'task_gid':uid,'privileged':False,'read_only_root':True,
            'network_mode':'none','image':'sha256:'+'a'*64,'runtime_files':g['runtime_files']}
        files=[]
        for name,value in [('results/_lab/environment.json',receipt),('environment.json',meta)]:
            raw=json.dumps(value).encode();self.jobs.nodes.upload(self.node,{**auth,'name':name,'offset':0,'data':base64.b64encode(raw).decode()})
            files.append({'name':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        result={'status':'succeeded' if passed else 'failed','exit_code':0 if passed else 78,'termination_confirmed':True,
            'execution_cleanup_confirmed':True,'npu_release_confirmed':True,'workspace_release_confirmed':True,'cache_release_confirmed':True}
        self.jobs.nodes.complete(self.node,{**auth,'result':result,'artifacts':files})
        return self.jobs.nodes.node(self.node)['device_qualifications'][0]

    def approved_ready(self):
        self.base.approve();receipt=self.base.receipt();receipt['policy_revision']=self.base.plan['after']['policy_revision']
        ack=self.base.h.commit(self.node,{'receipt':receipt})['ack']
        self.base.h.ready(self.node,{'plan_id':self.base.plan['id'],'commit_ack_id':ack['ack_id']})

    def append(self,**heartbeat):self.approved_ready();self.beat(**heartbeat)

    def proof(self):
        with self.jobs.connect() as db:
            row=db.execute('SELECT state,record FROM '+compat.TABLE+' WHERE plan_id=?',(self.base.plan['id'],)).fetchone()
        return (row[0],json.loads(row[1])) if row else None

    def test_unrelated_owner_append_preserves_existing_owner_card_and_expiry(self):
        old=self.qualify();self.append()
        node=self.jobs.nodes.node(self.node);self.assertEqual(decision(node,'alice',0)['status'],'verified')
        current=node['device_qualifications'][0]
        for key in ['recorded_at','expires_at','job_id','attempt_id','receipt_sha256']:self.assertEqual(current[key],old[key])
        self.assertEqual(decision(node,'alice',1)['status'],'unverified');self.assertEqual(decision(node,'bob',0)['status'],'unverified')
        self.assertEqual(current['probe_context_sha256'],old['context_sha256'])
        self.assertEqual(self.proof()[0],'complete');self.assertEqual(self.proof()[1]['outcomes'][0]['state'],'carried')

    def test_full_append_proof_rejects_changes_to_old_execution_contracts(self):
        self.qualify();self.base.approve()
        with self.jobs.connect() as db:record=self.base.h.get(self.base.plan['id'],db)
        before=self.proof()[1]['intent']['before_profile'];after=record['after_profile']
        changes=[lambda p:p.update(image='sha256:'+'c'*64),lambda p:p.update(physical_id='d'*64),
            lambda p:p.update(python='/different/python'),lambda p:p.update(environment_setup='/different/setup'),
            lambda p:p.update(templates=[PROBE]),lambda p:p['npu'].update(device_gid=777),
            lambda p:p['users']['alice'].update(gid=777),lambda p:p['workspace']['slots'][0].update(bytes=32*1024**2),
            lambda p:p['persistent_cache']['slots'][0].update(root_inode=999),lambda p:p.update(policy_revision=99)]
        for change in changes:
            candidate=copy.deepcopy(after);change(candidate)
            with self.subTest(change=change):self.assertFalse(compat.compatible(before,candidate,'bob'))

    def test_only_authenticated_ready_heartbeat_can_complete_the_pending_bridge(self):
        old=self.qualify();self.base.approve()
        self.assertEqual(self.proof()[0],'pending')
        with self.assertRaises(JobError):self.beat(policy_revision=2)
        self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'][0],old)
        ack=self.base.h.commit(self.node,{'receipt':self.base.receipt()})['ack'];self.beat()
        self.assertEqual(self.proof()[0],'pending')
        self.base.h.ready(self.node,{'plan_id':self.base.plan['id'],'commit_ack_id':ack['ack_id']});self.beat()
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')

    def test_host_change_seals_rejection_and_later_restore_cannot_rebase_it(self):
        self.qualify();self.append(access_host_sha256='c'*64)
        self.assertEqual(self.proof()[1]['outcomes'][0]['state'],'skipped');self.beat()
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'stale')

    def test_missing_access_observation_does_not_become_a_future_automatic_probe(self):
        self.qualify();self.approved_ready()
        from workspace_slots import CAPABILITY as W
        from persistent_cache import CAPABILITY as C
        self.beat(capabilities=['start-ticket-v1','cleanup-before-complete-v1',W,C,'uid-quota-workspace-v1','npu-device-whitelist-v1',*CAPABILITIES])
        self.assertEqual(self.proof()[1]['outcomes'][0]['state'],'skipped');self.beat()
        self.assertNotEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')

    def test_changed_then_restored_owner_authority_cannot_carry_old_evidence(self):
        self.qualify();self.approved_ready();self.jobs.access.disable_user('alice');self.jobs.access.disable_user('alice',False);self.beat()
        self.assertIn('authority changed',self.proof()[1]['outcomes'][0]['reason'])
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'stale')

    def test_newer_qualification_is_not_overwritten_by_a_pending_bridge(self):
        old=self.qualify();self.approved_ready();replacement={**old,'status':'blocked','expires_at':None,'receipt_sha256':'d'*64}
        with self.jobs.transaction() as db:db.execute('UPDATE device_qualifications SET record=? WHERE node_id=?',(json.dumps(replacement),self.node))
        self.beat();self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'][0],replacement)
        self.assertIn('no replacement was overwritten',self.proof()[1]['outcomes'][0]['reason'])

    def test_expiry_during_handoff_is_preserved_and_never_renewed(self):
        old=self.qualify();self.approved_ready()
        import types
        with patch.object(compat,'time',types.SimpleNamespace(time=lambda:old['expires_at']+1)):self.beat()
        current=self.jobs.nodes.node(self.node)['device_qualifications'][0];self.assertEqual(current,old)
        self.assertIn('expired',self.proof()[1]['outcomes'][0]['reason'])

    def test_failed_and_already_expired_sources_have_no_compatibility_intent(self):
        self.qualify(passed=False);self.append();self.assertIsNone(self.proof())
        self.assertNotEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')

    def test_expired_source_is_not_eligible_at_approval(self):
        self.qualify()
        with self.jobs.transaction() as db:
            raw=self.jobs.nodes.qualifications.records(self.node,db)[0];raw['expires_at']=0
            db.execute('UPDATE device_qualifications SET record=? WHERE node_id=?',(json.dumps(raw),self.node))
        self.append();self.assertIsNone(self.proof())

    def test_prior_policy_change_and_restore_still_requires_an_explicit_probe(self):
        self.qualify();node=self.jobs.nodes.node(self.node);policy=node['profile']['policy']
        self.jobs.nodes.set_policy(self.node,{**policy,'memory_mib':768},1);self.jobs.nodes.set_policy(self.node,policy,2)
        from node_owner_admin import projection,extend,sha
        value=copy.deepcopy(self.base.plan);value['before']=projection(self.jobs.nodes.node(self.node)['profile'])
        value['after']=extend(value['before'],value['request'],value['after']['workspace']['slots'][-1],value['after']['persistent_cache']['slots'][-1])
        value['id']=sha({k:v for k,v in value.items() if k!='id'});self.base.plan=value;self.append()
        self.assertEqual(self.jobs.nodes.node(self.node)['profile']['policy_revision'],4)
        self.assertEqual(self.proof()[1]['outcomes'][0]['state'],'skipped')
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'stale')

    def test_heartbeat_faults_roll_back_all_rebases_and_retry_the_same_intent(self):
        old=self.qualify();self.approved_ready();intent=self.proof()[1]['intent_sha256']
        class Crash(BaseException):pass
        for boundary in ['candidate','complete']:
            def crash(stage):
                if stage==boundary:raise Crash()
            with patch.object(compat,'_checkpoint',side_effect=crash),self.assertRaises(Crash):self.beat()
            self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'][0],old)
            self.assertEqual(self.proof()[0],'pending');self.assertEqual(self.proof()[1]['intent_sha256'],intent)
        self.beat();first=self.jobs.nodes.node(self.node)['device_qualifications'][0];self.beat()
        self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'][0],first)
        with self.jobs.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM worker_node_events WHERE actor='qualification-compatibility'").fetchone()[0],1)

    def test_restart_recovers_from_the_persisted_original_intent(self):
        self.qualify();self.approved_ready();original=self.jobs
        replacement=JobStore(original.runtime,original.access.config)
        try:
            self.jobs=replacement;self.beat();self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')
        finally:self.jobs=original;replacement.access.close()

    def test_pending_audit_blocks_configuration_changes_maintenance_and_downgrade(self):
        self.qualify();self.approved_ready()
        with self.assertRaisesRegex(DeployError,'compatibility'):assert_quiescent(self.jobs.runtime,'controller')
        with self.assertRaisesRegex(DeployError,'compatibility'):require_features(self.jobs.runtime,'controller',{'files':{}})
        # Before the first online heartbeat the normal handoff guard also holds.
        with self.assertRaises(JobError):self.jobs.nodes.set_templates(self.node,[PROBE])
        self.beat()
        with self.assertRaisesRegex(DeployError,'compatibility'):require_features(self.jobs.runtime,'controller',{'files':{}})

    def test_restored_backup_does_not_revive_completed_compatibility(self):
        self.qualify();self.append()
        from device_qualification import invalidate_restored
        invalidate_restored(self.jobs.runtime/'jobs.sqlite3');self.beat()
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'expired')

    def test_audit_budget_failure_rolls_back_the_new_approval_without_touching_old_owner(self):
        old=self.qualify();before=self.jobs.nodes.node(self.node)['profile']
        with patch.object(compat,'MAX_PLANS',0),self.assertRaisesRegex(JobError,'history'):self.base.approve()
        self.assertEqual(self.jobs.nodes.node(self.node)['profile'],before);self.assertIsNone(self.proof())
        self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'][0],old)

    def test_tampered_intent_is_not_used_to_rewrite_a_qualification(self):
        old=self.qualify();self.approved_ready()
        with self.jobs.transaction() as db:
            value=self.proof()[1];value['intent']['before_profile']['image']='sha256:'+'f'*64
            db.execute('UPDATE '+compat.TABLE+' SET record=? WHERE plan_id=?',(json.dumps(value),self.base.plan['id']))
        with self.assertRaisesRegex(JobError,'intent changed'):self.beat()
        self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'][0],old)

    def test_audit_status_is_bounded_readonly_and_contains_no_configuration(self):
        self.qualify();self.append();value=compat.status(self.jobs,self.node,1,0)
        self.assertEqual(value['records'][0]['carried'],1);self.assertFalse(value['more'])
        self.assertNotIn('before_profile',json.dumps(value));self.assertNotIn('/srv/',json.dumps(value))
        for limit,offset in [(0,0),(101,0),(1,-1)]:
            with self.subTest(limit=limit,offset=offset),self.assertRaises(JobError):compat.status(self.jobs,self.node,limit,offset)

    def test_existing_owner_workload_uses_the_new_exact_context_without_a_new_probe(self):
        self.qualify();self.append();user=self.jobs.access.user('alice');raw=b'pass\n'
        bundle,_=self.jobs.inputs.create(user,{'files':[{'name':'main.py','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]})
        self.jobs.inputs.upload(bundle['id'],user,{'name':'main.py','offset':0,'data':base64.b64encode(raw).decode()});self.jobs.inputs.seal(bundle['id'],user)
        job,_=self.jobs.submit({'environment':'worker-python','template':'accuracy_v1','parameters':{
            'input_id':bundle['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'},
            'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':1},'artifacts':{'file_mib':1,'total_mib':2},
            'workspace':{'min_mib':1,'min_inodes':16,'durable':True}}},user)
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertIsNotNone(grant);self.assertEqual(grant['job_id'],job['id'])
        node=self.jobs.nodes.node(self.node)
        self.assertEqual(grant['npu_access_context_sha256'],decision(node,'alice',0)['context_sha256'])
        self.assertEqual(grant['allocation']['npu_ids'],[0])
        self.assertEqual(sum(j.get('template')==PROBE for j in self.jobs.all()),1)

    def test_consecutive_appends_preserve_the_original_probe_and_do_not_renew_expiry(self):
        old=self.qualify();self.append();previous=self.proof()[1]['intent_sha256'];first_plan=self.base.plan['id']
        from node_owner_admin import projection,extend,sha
        from test_node_owner_admin import slot
        from persistent_cache import fingerprint
        value=copy.deepcopy(self.base.plan);value['revision']=1
        value['before']=projection(self.jobs.nodes.node(self.node)['profile'])
        home=self.jobs.runtime/'charlie';home.mkdir()
        stage=self.jobs.access.stage_user('charlie',home,quota={'input_storage_mib':4,'archive_storage_mib':4})
        value['request']={'owner':'charlie','stage_id':stage['stage_id'],'uid':23803,'gid':23803,'workspace_slot':'work5','cache_slot':'cache5'}
        value['after']=extend(value['before'],value['request'],slot('work',23803,5),slot('cache',23803,5))
        template=copy.deepcopy(next(iter(value['caches'].values())));value['caches']={}
        for s in value['before']['persistent_cache']['slots']:
            snap=copy.deepcopy(template);snap['state']['binding'].update(storage_id=s['storage_id'],quota_uid=s['quota_uid'],policy_sha256=fingerprint(value['before']['persistent_cache']))
            value['caches'][s['storage_id']]=snap
        for key,path in [('workspace','/srv/work'),('persistent_cache','/srv/cache')]:value['sources'][path]['slots']=copy.deepcopy(value['after'][key]['slots'])
        value['id']=sha({k:v for k,v in value.items() if k!='id'});self.base.plan=value
        # The fixture receipt includes the request owner/stage and Node-owner
        # revision, while central approval and ACK are the real implementations.
        self.base.approve();receipt=self.base.receipt();receipt.update(owner='charlie',stage_id=stage['stage_id'],revision=2,policy_revision=3)
        ack=self.base.h.commit(self.node,{'receipt':receipt})['ack'];self.base.h.ready(self.node,{'plan_id':value['id'],'commit_ack_id':ack['ack_id']});self.beat()
        current=self.jobs.nodes.node(self.node)['device_qualifications'][0]
        self.assertEqual(current['probe_context_sha256'],old['context_sha256']);self.assertEqual(current['expires_at'],old['expires_at'])
        self.assertEqual(current['job_id'],old['job_id']);self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM '+compat.TABLE).fetchone()[0],2)
            prior=json.loads(db.execute('SELECT record FROM '+compat.TABLE+' WHERE plan_id=?',(first_plan,)).fetchone()[0])
        self.assertEqual(prior['intent_sha256'],previous);self.assertEqual(self.proof()[1]['intent']['candidates'][0]['source']['compatibility']['plan_id'],first_plan)

    def test_identity_write_reservation_is_held_until_the_bridge_transaction_commits(self):
        import sqlite3
        self.qualify();self.approved_ready();observed=[]
        def locked(stage):
            if stage!='candidate':return
            db=sqlite3.connect(self.jobs.access.path,timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):db.execute("UPDATE users SET disabled=1 WHERE id='alice'")
                observed.append(stage)
            finally:db.close()
        with patch.object(compat,'_checkpoint',side_effect=locked):self.beat()
        self.assertEqual(observed,['candidate']);self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')

    def test_peer_inventory_wait_keeps_configuration_and_maintenance_guarded(self):
        self.qualify();self.approved_ready();profile=self.jobs.nodes.node(self.node)['profile']
        peer,_=self.jobs.nodes.enroll({'name':'compatibility_peer','physical_id':profile['physical_id'],
            'users':{'alice':profile['users']['alice']},'image':profile['image'],'templates':['cpu_demo'],'policy':profile['policy']})
        from attempt_inventory import CAPABILITY as INVENTORY
        from nodes import REQUIRED_CAPABILITIES
        from workspace_slots import CAPABILITY as W
        from persistent_cache import CAPABILITY as C
        self.jobs.policy['require_attempt_inventory']=True;self.jobs.controller_instance='c'*32
        caps=['start-ticket-v1','cleanup-before-complete-v1',W,C,'uid-quota-workspace-v1','npu-device-whitelist-v1',CAPABILITY,INVENTORY,*CAPABILITIES]
        self.beat(capabilities=caps,attempt_inventory=[])
        self.assertTrue(self.base.h.status(self.node)[0]['node_online_seen']);self.assertEqual(self.proof()[0],'pending')
        with self.assertRaisesRegex(JobError,'qualification compatibility'):self.jobs.nodes.set_templates(self.node,[PROBE])
        with self.assertRaisesRegex(DeployError,'compatibility'):assert_quiescent(self.jobs.runtime,'controller')
        self.jobs.nodes.heartbeat(peer,{'physical_id':profile['physical_id'],'protocol_version':1,'policy_revision':1,
            'capabilities':sorted(REQUIRED_CAPABILITIES|{INVENTORY}),'attempt_inventory':[],
            'cpu_ids':profile['policy']['cpu_ids'],'memory_available_bytes':4*1024**3,'disk_free_bytes':4*1024**3})
        self.beat(capabilities=caps,attempt_inventory=[]);self.assertEqual(self.proof()[0],'complete')
        self.assertEqual(decision(self.jobs.nodes.node(self.node),'alice',0)['status'],'verified')

    def test_local_audit_cli_reads_the_same_summary_without_modifying_qualification(self):
        from lab_client import atomic_json
        self.qualify();self.append();before=copy.deepcopy(self.jobs.nodes.node(self.node)['device_qualifications'])
        atomic_json(self.jobs.runtime/'config.json',self.jobs.access.config)
        result=subprocess.run([sys.executable,str(ROOT/'workerctl'),'--controller-state',str(self.jobs.runtime),
            'node-qualification-compatibility',self.node,'--limit','1'],capture_output=True,text=True,check=True,timeout=10)
        summary=json.loads(result.stdout);self.assertEqual(summary['records'][0]['carried'],1)
        self.assertEqual(self.jobs.nodes.node(self.node)['device_qualifications'],before)


class ExpansionQualificationCompatibilityTests(unittest.TestCase):
    def test_active_owner_expansion_carries_only_the_existing_other_owner(self):
        base=expansion_builder.ExpansionTests();base.setUp()
        try:
            self.jobs=base.jobs;self.node=base.node;self.policy={'device_ids':[0,1],'visible_ids':{'0':0,'1':0},'device_gid':1001,
                'management_devices':['/dev/davinci_manager'],'driver_paths':['/usr/local/Ascend/driver'],
                'query_tool':'/usr/local/sbin/npu-smi','management_env':'/data/pri/cann/set_env.sh'}
            self.jobs.access.set_quota('alice',{'npu':1})
            with self.jobs.transaction() as db:
                node=self.jobs.nodes.node(self.node,db);p=node['profile'];p.update(npu=self.policy,templates=[PROBE,'accuracy_v1','cpu_demo'])
                db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(p),self.node))
            QualificationCompatibilityTests.beat(self);old=QualificationCompatibilityTests.qualify(self)
            base.grant();ack=base.commit();base.ready(ack);QualificationCompatibilityTests.beat(self)
            node=self.jobs.nodes.node(self.node);self.assertEqual(decision(node,'alice',0)['status'],'verified')
            self.assertEqual(decision(node,'bob',0)['status'],'unverified');self.assertEqual(node['device_qualifications'][0]['expires_at'],old['expires_at'])
            base.unchanged()
        finally:base.tearDown()


if __name__=='__main__':unittest.main()

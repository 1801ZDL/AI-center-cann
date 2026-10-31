"""Central storage CAS, original authority, bounded native plans and guards."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from deployment import DeployError
import filesystem_quota as quota
from jobs import JobError,JobStore
from lab_client import Client,ClientHTTPError,atomic_json
from maintenance import require_features,assert_quiescent
import node_quota_admin as admin
from node_owner_admin import projection
import persistent_cache as cache
from quota_workspace import FORMAT
from server import ThreadingHTTPServer,handler_class
from test_environment_contract import IMAGE,contract
from test_node_owner_admin import slot
from worker import Worker,NodeClient
from workspace_slots import fingerprint as workspace_fingerprint


def signed(value):value['id']=admin.sha({k:v for k,v in value.items() if k!='id'});return value


def planned(profile,node,state,kind='workspace'):
    before=projection(profile);after=copy.deepcopy(before);after[kind]['slots'][0].update(bytes=32*1024**2,inodes=256);after['policy_revision']+=1
    old=before[kind]['slots'][0];root=str(Path(old['path']).parent)
    marker={'format':FORMAT,'filesystem_id':old['filesystem_id'],'device':old['device'],'root_inode':2,
        'bytes':old['filesystem_bytes'],'inodes':old['filesystem_inodes'],'slots':copy.deepcopy(before[kind]['slots'])}
    target=copy.deepcopy(marker);target['slots']=copy.deepcopy(after[kind]['slots'])
    value={'format':admin.FORMAT,'prefix':'/opt/node','release_id':'a'*64,'state_dir':str(state),
        'control_identity':{'device':state.stat().st_dev,'inode':state.stat().st_ino},'config_base_sha256':'b'*64,'meta_base_sha256':'c'*64,
        'revision':0,'node_id':node,'physical_id':profile['physical_id'],'owner':'alice','kind':kind,
        'before':before,'after':after,'root':root,'native_before':marker,'native_after':target,
        'usage':{'bytes_used':4096,'inodes_used':1},'cache_snapshots':{}}
    if kind=='persistent_cache':
        for s in before[kind]['slots']:
            value['cache_snapshots'][s['storage_id']]={'state':{'binding':{'format':'private-persistent-cache/v1','node_id':node,
                'physical_id':profile['physical_id'],'state_dir':str(state),'policy_sha256':cache.fingerprint(before[kind]),
                'storage_id':s['storage_id'],'quota_uid':s['quota_uid']},'generation':4,'lease':None,'keys':{}},
                'directories':[],'usage':{'bytes_used':4096,'inodes_used':1}}
    return signed(value)


class NodeQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='node-quota-unit-');self.state=Path(self.temp.name)
        self.config={'username':'operator','password':'offline-only','container':'zdl'};self.jobs=JobStore(self.state,self.config)
        for name in ['alice','bob']:
            home=self.state/name;home.mkdir();self.jobs.access.create_user(name,home,environments=['worker-python'])
        self.profile={'name':'quota-node','physical_id':self.jobs.backend.physical_id,'image':IMAGE,'python':'/usr/bin/python3',
            'environment_manifest':contract(),'users':{'alice':{'uid':23801,'gid':23801},'bob':{'uid':23802,'gid':23802}},
            'templates':['cpu_demo','ir_v1'],'workspace':{'slots':[slot('work',23801,1),slot('work',23802,2)]},
            'persistent_cache':{'slots':[slot('cache',23801,1),slot('cache',23802,2)]},
            'policy':{'cpu_ids':self.jobs.policy['cpu_ids'],'memory_mib':2048,'shm_mib':0,'max_running':1}}
        self.node,self.key=self.jobs.nodes.enroll(self.profile);self.profile=self.jobs.nodes.node(self.node)['profile']
        self.changes=self.jobs.nodes.quota_changes;self.plan=planned(self.profile,self.node,self.state)

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def beat(self,*,clean=True,cap=True,revision=None):
        from environment_contract import CAPABILITY as ENV,fingerprint
        from workflow_spec import CAPABILITIES
        from quota_workspace import CAPABILITY as QUOTA
        from workspace_slots import CAPABILITY as WORK
        profile=self.jobs.nodes.node(self.node)['profile']
        return self.jobs.nodes.heartbeat(self.node,{'physical_id':profile['physical_id'],'protocol_version':1,
            'policy_revision':profile.get('policy_revision',1) if revision is None else revision,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',ENV,WORK,QUOTA,cache.CAPABILITY,*CAPABILITIES,*([admin.CAPABILITY] if cap else [])],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
            'image_sha256':IMAGE,'environment_setup':None,'python_executable':'/usr/bin/python3','environment_sha256':fingerprint(contract()),
            'workspace_observation':{'policy_sha256':workspace_fingerprint(profile['workspace']),
                'slots':[{'id':s['id'],'status':'ok','clean':clean,'free_bytes':s['bytes'],'free_inodes':s['inodes']} for s in profile['workspace']['slots']]},
            'cache_observation':{'policy_sha256':cache.fingerprint(profile['persistent_cache']),
                'slots':[{'id':s['id'],'status':'ok','leased':False,'free_bytes':s['bytes'],'free_inodes':s['inodes'],'namespaces':0} for s in profile['persistent_cache']['slots']]}})

    def ready(self):
        approval=self.changes.status(self.node,{'plan_id':self.plan['id']})
        return self.changes.ready(self.node,{'plan_id':self.plan['id'],'approval_id':approval['approval_id'],
            'after_sha256':admin.sha(self.plan['after']),'receipt_sha256':'e'*64})

    def test_approval_changes_only_limits_and_revision_and_invalidates_old_heartbeat(self):
        self.beat();before=self.jobs.nodes.node(self.node)['profile'];reply=self.changes.approve(self.node,self.plan)
        node=self.jobs.nodes.node(self.node);self.assertEqual(node['last_seen'],0);self.assertEqual(node['report'],{})
        self.assertEqual(projection(node['profile']),self.plan['after']);self.assertEqual(node['profile']['users'],before['users'])
        self.assertEqual(node['profile']['image'],before['image']);self.assertEqual(node['profile']['persistent_cache'],before['persistent_cache'])
        self.assertEqual(node['profile']['workspace']['slots'][1],before['workspace']['slots'][1])
        with self.assertRaises(JobError):self.beat(revision=self.plan['before']['policy_revision'])
        replay=self.changes.approve(self.node,self.plan);self.assertTrue(replay['reused']);self.assertEqual(replay['approval'],reply['approval'])

    def test_ready_requires_approved_identity_and_healthy_storage_heartbeat(self):
        self.changes.approve(self.node,self.plan);self.assertEqual(self.ready()['state'],'ready')
        self.beat(clean=False);self.assertEqual(self.changes.status(self.node,{'plan_id':self.plan['id']})['state'],'ready')
        self.beat(clean=True);self.assertEqual(self.changes.status(self.node,{'plan_id':self.plan['id']})['state'],'observed')
        self.assertEqual(self.ready()['state'],'observed')

    def test_approved_and_ready_nodes_cannot_allocate_then_original_queue_can_run(self):
        self.changes.approve(self.node,self.plan)
        owner=self.jobs.access.user('alice');job=self.jobs.submit({'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}},owner)[0]
        self.beat();self.assertFalse(self.jobs.nodes.claim(self.node)['grant']);self.assertFalse(self.jobs.attempts(job['id']))
        self.ready();self.beat();grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['allocation']['workspace']['bytes'],32*1024**2)

    def test_live_or_unknown_attempt_blocks_approval_without_changing_profile(self):
        self.beat();job=self.jobs.submit({'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}},self.jobs.access.user('alice'))[0]
        self.jobs.nodes.claim(self.node)
        for status in ['reserved','needs_attention']:
            with self.jobs.transaction() as db:
                actual=self.jobs.get(job['id'],db);actual['state']=status;self.jobs.write(db,actual)
            before=self.jobs.nodes.node(self.node)['profile']
            with self.assertRaises(JobError):self.changes.approve(self.node,self.plan)
            self.assertEqual(self.jobs.nodes.node(self.node)['profile'],before)

    def test_pending_change_blocks_mutations_and_offline_controller_maintenance(self):
        self.changes.approve(self.node,self.plan)
        with self.assertRaises(JobError):self.jobs.nodes.set_policy(self.node,self.profile['policy'],2)
        with self.assertRaises(JobError):self.jobs.nodes.set_templates(self.node,['cpu_demo'])
        with self.assertRaisesRegex(DeployError,'Node quota'):assert_quiescent(self.state,'controller')

    def test_restart_preserves_approval_and_ready_does_not_replace_it(self):
        first=self.changes.approve(self.node,self.plan)['approval'];other=JobStore(self.state,self.config)
        try:
            self.assertEqual(other.nodes.quota_changes.status(self.node,{'plan_id':self.plan['id']}),first)
            self.assertTrue(other.nodes.quota_changes.approve(self.node,self.plan)['reused'])
        finally:other.access.close()
        self.ready();changed={'plan_id':self.plan['id'],'approval_id':first['approval_id'],'after_sha256':first['after_sha256'],'receipt_sha256':'f'*64}
        with self.assertRaises(JobError):self.changes.ready(self.node,changed)

    def test_old_worker_capability_cannot_acknowledge_changed_storage(self):
        self.changes.approve(self.node,self.plan);self.ready()
        with self.assertRaises(JobError):self.beat(cap=False)
        self.assertEqual(self.jobs.nodes.node(self.node)['last_seen'],0)
        self.beat();self.assertEqual(self.changes.status(self.node,{'plan_id':self.plan['id']})['state'],'observed')
        with self.assertRaises(JobError):self.beat(cap=False)

    def test_plan_cannot_change_owner_mapping_slot_identity_or_unselected_quota(self):
        for field in ['users','workspace','persistent_cache']:
            altered=copy.deepcopy(self.plan)
            if field=='users':altered['after']['users']['alice']['uid']=23803
            else:altered['after'][field]['slots'][1]['bytes']+=4096
            with self.subTest(field=field),self.assertRaises((DeployError,ValueError)):admin.validate_plan(signed(altered))
        altered=copy.deepcopy(self.plan);altered['after']['workspace']['slots'][0]['storage_id']='f'*32
        with self.assertRaises(DeployError):admin.validate_plan(signed(altered))

    def test_capacity_geometry_limits_and_unregistered_native_slots_are_rejected(self):
        for patch_value in [{'bytes':128*1024**2},{'bytes':True},{'inodes':4096},{'bytes':17},{'quota_uid':0}]:
            altered=copy.deepcopy(self.plan);altered['after']['workspace']['slots'][0].update(patch_value)
            with self.subTest(value=patch_value),self.assertRaises((DeployError,ValueError)):admin.validate_plan(signed(altered))
        altered=copy.deepcopy(self.plan);altered['native_before']['slots']=altered['native_before']['slots'][:1]
        with self.assertRaises(DeployError):admin.validate_plan(signed(altered))
        altered=copy.deepcopy(self.plan);altered['usage']['bytes_used']=33*1024**2
        with self.assertRaises(DeployError):admin.validate_plan(signed(altered))

    def test_cache_resize_preserves_namespace_key_but_requires_every_idle_snapshot(self):
        plan=planned(self.profile,self.node,self.state,'persistent_cache');admin.validate_plan(plan)
        grant={'owner':'alice','environment_sha256':self.profile['environment_manifest']['fingerprint'] if 'fingerprint' in self.profile['environment_manifest'] else None,
            'runtime_files':{'compiler':'b'*64},'parameters':{'lock':{'python':'3.10.12'}}}
        from environment_contract import fingerprint
        grant['environment_sha256']=fingerprint(self.profile['environment_manifest'])
        self.assertEqual(cache.namespace(self.profile,grant,host=False),cache.namespace({**self.profile,**plan['after']},grant,host=False))
        for change in ['missing','lease','generation','usage']:
            altered=copy.deepcopy(plan);key=next(iter(altered['cache_snapshots']))
            if change=='missing':altered['cache_snapshots'].pop(key)
            elif change=='usage':altered['cache_snapshots'][key]['usage']['bytes_used']=True
            else:altered['cache_snapshots'][key]['state'][change]={'attempt_id':'unknown'} if change=='lease' else True
            with self.subTest(change=change),self.assertRaises(DeployError):admin.validate_plan(signed(altered))

    def test_only_node_key_rotation_is_excluded_from_nonstorage_configuration_digest(self):
        config={**self.profile,'node_key':'original','controller':'https://controller.example','ca_file':'/root/ca.pem'}
        self.assertEqual(admin.base_config(config),admin.base_config({**config,'node_key':'rotated'}))
        for change in [{'controller':'https://other.example'},{'image':'sha256:'+'f'*64},{'ca_file':'/root/other-ca.pem'}]:
            self.assertNotEqual(admin.base_config(config),admin.base_config({**config,**change}))

    def test_history_refuses_unsupported_controller_and_node_downgrade(self):
        self.changes.approve(self.node,self.plan)
        with self.assertRaisesRegex(DeployError,'quota change history'):require_features(self.state,'controller',{'files':{'source_grants.py':{},'experiment_budget.py':{}}})
        history=self.state/admin.HISTORY;history.mkdir()
        with self.assertRaisesRegex(DeployError,'quota change history'):require_features(self.state,'node',{'files':{}})

    def test_installed_revision_blocks_downgrade_even_if_an_old_state_lacks_receipts(self):
        with self.assertRaisesRegex(DeployError,'Installed Node quota'):
            require_features(self.state,'node',{'files':{}},{'node_quota_revision':2})

    def test_controller_restore_cannot_drop_or_change_a_later_quota_approval(self):
        from node_quota_change import require_restore_history
        saved=self.state/'checkpoint';saved.mkdir()
        with self.jobs.connect() as source,sqlite3.connect(saved/'jobs.sqlite3') as target:source.backup(target)
        self.changes.approve(self.node,self.plan);self.ready();self.beat()
        with self.assertRaisesRegex(DeployError,'quota history'):require_restore_history(self.state,saved,'controller',{}, {})
        with self.jobs.connect() as source,sqlite3.connect(saved/'jobs.sqlite3') as target:source.backup(target)
        self.assertIsNone(require_restore_history(self.state,saved,'controller',{},{}))
        with sqlite3.connect(saved/'jobs.sqlite3') as db:db.execute("UPDATE node_quota_changes SET record='{}'")
        with self.assertRaisesRegex(DeployError,'quota history'):require_restore_history(self.state,saved,'controller',{}, {})

    def test_node_restore_preserves_complete_chain_and_metadata_floor(self):
        from node_quota_change import node_history,require_restore_history
        live=self.state/'node-live';saved=self.state/'node-saved'
        for path in [live,saved]:path.mkdir();(path/admin.HISTORY).mkdir(mode=0o700)
        def receipt(path,revision):
            plan_id=str(revision)*64;atomic_json(path/admin.HISTORY/(plan_id+'.json'),{'format':'ascend-lab-node-quota-receipt/v1',
                'plan_id':plan_id,'node_id':self.node,'revision':revision,'preserved_usage':{'bytes_used':4096,'inodes_used':1}})
        for revision in [1,2]:receipt(live,revision)
        receipt(saved,1)
        with self.assertRaisesRegex(DeployError,'quota history'):require_restore_history(live,saved,'node',{'node_quota_revision':2},{})
        receipt(saved,2)
        self.assertEqual(require_restore_history(live,saved,'node',{'node_quota_revision':2},{'node_quota_revision':2}),2)
        with self.assertRaises(DeployError):require_restore_history(live,saved,'node',{'node_quota_revision':2},{'node_quota_revision':0})
        (saved/admin.HISTORY/('1'*64+'.json')).unlink()
        with self.assertRaisesRegex(DeployError,'missing revisions'):node_history(saved)

    def test_local_pending_journal_blocks_worker_before_docker_access(self):
        atomic_json(self.state/admin.JOURNAL,self.plan)
        with patch('docker_runtime.discover') as discover:
            with self.assertRaisesRegex(DeployError,'Node quota'):Worker({'state_dir':str(self.state),'controller':'http://127.0.0.1:1','node_key':'test-only'})
            discover.assert_not_called()

    def test_stale_plan_and_disabled_pending_node_do_not_create_replacement_authority(self):
        self.jobs.nodes.set_policy(self.node,self.profile['policy'],1)
        with self.assertRaises(JobError):self.changes.approve(self.node,self.plan)
        current=self.jobs.nodes.node(self.node)['profile'];plan=planned(current,self.node,self.state)
        self.changes.approve(self.node,plan);self.jobs.nodes.disable(self.node)
        with self.jobs.connect() as db:self.assertIn(self.node,self.jobs.nodes.handoff.held_nodes(db))
        self.assertIsNone(self.jobs.nodes.authenticate('Bearer '+self.key))
        new_key=self.jobs.nodes.rotate_key(self.node)
        self.assertEqual(self.jobs.nodes.authenticate('Bearer '+new_key),self.node)
        self.assertTrue(self.changes.approve(self.node,plan)['reused'])

    def test_a_different_registered_node_cannot_use_this_approval(self):
        self.changes.approve(self.node,self.plan)
        foreign={**copy.deepcopy(self.profile),'name':'other-physical-node','physical_id':'f'*64}
        self.jobs.resources.configure(foreign['physical_id'],foreign['policy'],0)
        identifier,_=self.jobs.nodes.enroll(foreign)
        with self.assertRaises(JobError) as error:self.changes.status(identifier,{'plan_id':self.plan['id']})
        self.assertEqual(error.exception.status,404)

    def test_current_native_capacity_is_checked_beyond_declared_geometry(self):
        usage={'bytes_used':0,'inodes_used':0}
        geometry=Mock(f_bavail=8*1024,f_frsize=4096,f_favail=4096)
        with patch.object(admin.os,'fstatvfs',return_value=geometry),patch.object(quota,'read',return_value=usage):
            with self.assertRaises(DeployError):admin.native_capacity(5,self.plan['native_after'])
        geometry.f_bavail=128*1024**2//4096;geometry.f_favail=100
        with patch.object(admin.os,'fstatvfs',return_value=geometry),patch.object(quota,'read',return_value=usage):
            with self.assertRaises(DeployError):admin.native_capacity(5,self.plan['native_after'])

    def test_real_http_node_credentials_cannot_be_replaced_by_personal_or_other_node_tokens(self):
        self.changes.approve(self.node,self.plan)
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(self.config,Mock(),self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(server.server_port)
        try:
            node=NodeClient({'controller':url,'node_key':self.key});ack=node.call('quota-status',{'plan_id':self.plan['id']})
            self.assertEqual(ack['node_id'],self.node)
            ordinary=Client({'controller':url,'token':self.jobs.access.issue_token('alice',{'jobs:read','jobs:submit'},3600)})
            with self.assertRaises(ClientHTTPError) as error:ordinary.request('/api/worker/quota-status',{'plan_id':self.plan['id']})
            self.assertEqual(error.exception.status,401)
            unknown=Client({'controller':url,'token':'not-a-node-key'})
            with self.assertRaises(ClientHTTPError) as error:unknown.request('/api/worker/quota-status',{'plan_id':self.plan['id']})
            self.assertEqual(error.exception.status,401)
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


class QuotaCASTests(unittest.TestCase):
    def test_cas_changes_only_limits_and_replay_does_not_set_them_again(self):
        current={'byte_hard':16*1024**2,'byte_soft':16*1024**2,'inode_hard':128,'inode_soft':128,'bytes_used':8192,'inodes_used':2}
        sets=[]
        def call(fd,command,uid,value):
            if command==quota.Q_SETQUOTA:
                sets.append(command);current.update(byte_hard=value.block_hard*quota.BLOCK,byte_soft=value.block_soft*quota.BLOCK,
                    inode_hard=value.inode_hard,inode_soft=value.inode_soft)
        before={'bytes':16*1024**2,'inodes':128};after={'bytes':32*1024**2,'inodes':256}
        with patch.object(quota.os,'geteuid',return_value=0),patch.object(quota,'read',side_effect=lambda *_:dict(current)),patch.object(quota,'quota_call',side_effect=call):
            self.assertEqual(quota.resize(5,23801,before,after)['bytes_used'],8192)
            self.assertEqual(quota.resize(5,23801,before,after)['inodes_used'],2)
        self.assertEqual(len(sets),1)

    def test_unprivileged_stale_soft_limit_and_discarding_usage_are_refused(self):
        before={'bytes':16*1024**2,'inodes':128};after={'bytes':32*1024**2,'inodes':256}
        with patch.object(quota.os,'geteuid',return_value=23801),patch.object(quota,'read') as read:
            with self.assertRaises(PermissionError):quota.resize(5,23801,before,after)
            read.assert_not_called()
        good={'byte_hard':before['bytes'],'byte_soft':before['bytes'],'inode_hard':128,'inode_soft':128,'bytes_used':8192,'inodes_used':2}
        for changes in [{'byte_hard':64*1024**2},{'byte_soft':1024},{'bytes_used':33*1024**2},{'inodes_used':300}]:
            with self.subTest(changes=changes),patch.object(quota.os,'geteuid',return_value=0),patch.object(quota,'read',return_value={**good,**changes}),patch.object(quota,'quota_call') as call:
                with self.assertRaises(ValueError):quota.resize(5,23801,before,after)
                call.assert_not_called()


if __name__=='__main__':unittest.main()

"""Node owner/cache append contracts and entry-point gates; native cases separate."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import deployment
from deployment import DeployError,CORE
from lab_client import atomic_json
import node_owner_admin as admin
import persistent_cache as cache
from worker import Worker


def slot(role,uid,index):
    return {'id':role+str(index),'storage_id':str(index+(0 if role=='work' else 2))*32,'path':'/srv/'+role+'/slot'+str(index),
        'filesystem':'ext4-userquota','bytes':16*1024**2,'inodes':128,'device':54320+(role=='cache'),'root_inode':12+index,
        'quota_uid':uid,'filesystem_id':('a' if role=='work' else 'b')*32,'filesystem_bytes':128*1024**2,'filesystem_inodes':4096}


class NodeOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-node-owner-');self.state=Path(self.temp.name)
        self.before={'users':{'alice':{'uid':23801,'gid':23801}},'workspace':{'slots':[slot('work',23801,1)]},
                     'persistent_cache':{'slots':[slot('cache',23801,1)]},'policy_revision':1,'owner_bindings':{}}
        self.request={'owner':'bob','stage_id':'c'*64,'uid':23802,'gid':23802,'workspace_slot':'work2','cache_slot':'cache2'}
        self.after=admin.extend(self.before,self.request,slot('work',23802,2),slot('cache',23802,2))
        self.value={'format':admin.FORMAT,'prefix':'/opt/node','state_dir':str(self.state),'release_id':'a'*64,
            'node_id':'worker-'+'b'*12,'physical_id':'c'*64,'request':self.request,'before':self.before,'after':self.after,
            'config_base_sha256':'d'*64,'meta_base_sha256':'e'*64,'control_identity':{'device':123,'inode':456},'revision':0,
            'sources':{('/srv/'+role):{'format':'ascend-lab-userquota/v1','slots':[slot(role,23801,1),slot(role,23802,2)]} for role in ['work','cache']},
            'caches':{}}
        s=self.before['persistent_cache']['slots'][0]
        self.value['caches'][s['storage_id']]={'state':{'binding':{'format':'private-persistent-cache/v1',
            'node_id':self.value['node_id'],'physical_id':self.value['physical_id'],'state_dir':str(self.state),
            'policy_sha256':cache.fingerprint(self.before['persistent_cache']),'storage_id':s['storage_id'],'quota_uid':23801},
            'generation':7,'lease':None,'keys':{}},'directories':[],'usage':{'bytes_used':0,'inodes_used':0}}
        self.sign(self.value)

    def tearDown(self):self.temp.cleanup()

    def sign(self,value):value['id']=admin.sha({k:v for k,v in value.items() if k!='id'});return value

    def test_only_requested_owner_and_two_prepared_slots_are_appended(self):
        admin._plan_check(self.value)
        self.assertEqual(self.after['users']['alice'],self.before['users']['alice'])
        self.assertEqual(self.after['workspace']['slots'][:-1],self.before['workspace']['slots'])
        self.assertEqual(self.after['persistent_cache']['slots'][:-1],self.before['persistent_cache']['slots'])
        self.assertEqual(self.after['policy_revision'],2)
        for field in ['users','workspace','persistent_cache']:
            value=copy.deepcopy(self.value)
            if field=='users':value['after'][field]['alice']['gid']=23803
            else:value['after'][field]['slots'][0]['bytes']+=4096
            with self.subTest(field=field),self.assertRaises(DeployError):admin._plan_check(self.sign(value))

    def test_existing_owner_uid_or_wrong_role_slot_cannot_be_adopted(self):
        for value in [{**self.request,'owner':'alice'},{**self.request,'uid':23801}]:
            with self.assertRaises(DeployError):admin.extend(self.before,value,slot('work',23802,2),slot('cache',23802,2))
        for work,private in [(slot('work',23801,2),slot('cache',23802,2)),(slot('work',23802,2),slot('cache',23801,2))]:
            with self.assertRaises(DeployError):admin.extend(self.before,self.request,work,private)

    def test_request_and_plan_digest_boundaries_reject_aliases_or_tampering(self):
        for patch in [{'uid':True},{'gid':0},{'stage_id':'wrong'},{'workspace_slot':'../x'},{'owner':'../x'}]:
            with self.subTest(patch=patch),self.assertRaises(DeployError):admin.request({**self.request,**patch})
        with self.assertRaises(DeployError):admin._plan_check({**self.value,'revision':1})
        with self.assertRaises(DeployError):admin._plan_check(self.sign({**self.value,'control_identity':{'device':True,'inode':1}}))

    def test_idle_snapshot_and_original_binding_are_required(self):
        storage=next(iter(self.value['caches']))
        for patch in [{'lease':{'attempt_id':'unknown'}},{'generation':True},{'binding':{}},{'creating':{}}]:
            value=copy.deepcopy(self.value);value['caches'][storage]['state'].update(patch)
            with self.subTest(patch=patch),self.assertRaises(DeployError):admin._plan_check(self.sign(value))
        value=copy.deepcopy(self.value);value['caches']={}
        with self.assertRaises(DeployError):admin._plan_check(self.sign(value))

    def test_cache_namespace_is_stable_across_owner_policy_append(self):
        old={'node_id':self.value['node_id'],'physical_id':self.value['physical_id'],'image':'sha256:'+'a'*64,**self.before}
        new={**old,**self.after}
        grant={'owner':'alice','environment_sha256':'e'*64,'runtime_files':{'workflow_runner.py':'f'*64},
               'parameters':{'lock':{'python':'3.12.3'},'environment':{}}}
        with patch('environment_contract.verify_config',return_value='e'*64):
            self.assertEqual(cache.namespace(old,grant),cache.namespace(new,grant))
        self.assertNotEqual(cache.fingerprint(old['persistent_cache']),cache.fingerprint(new['persistent_cache']))

    def test_pending_and_awaiting_records_block_worker_before_runtime_discovery(self):
        config={'state_dir':str(self.state),'controller':'http://127.0.0.1:1','node_key':'node-owner-unit-fixture'}
        for name in [admin.JOURNAL,admin.AWAIT]:
            path=self.state/name;atomic_json(path,{})
            with patch('worker.discover' if hasattr(__import__('worker'),'discover') else 'docker_runtime.discover') as discover:
                with self.assertRaisesRegex(DeployError,'central acknowledgement'):Worker(config)
                discover.assert_not_called()
            path.unlink()
        path=self.state/admin.AWAIT;path.symlink_to(self.state/'missing')
        with self.assertRaises(DeployError):admin.guard(self.state)

    def test_deployment_blocks_incomplete_owner_binding_even_when_cache_policy_matches(self):
        release=self.state/'release';release.mkdir();atomic_json(release/'release.json',{'id':'a'*64,'state_schema':1,'files':{}})
        config={'persistent_cache':self.before['persistent_cache']};atomic_json(self.state/'config.json',config)
        meta={'role':'node','state_dir':str(self.state),'release_id':'a'*64,'state_schema':1,**config}
        with patch('deployment.load_installation',return_value=(self.state,meta,release)),patch('deployment.checked_manifest'):
            for name in [admin.JOURNAL,admin.AWAIT]:
                path=self.state/name;atomic_json(path,{})
                with self.assertRaises(DeployError):deployment.verify(self.state)
                deployment.verify(self.state,quota_recovery=True);path.unlink()

    def test_nonroot_apply_refuses_before_native_or_control_access(self):
        with patch('node_owner_admin.os.geteuid',return_value=23801),patch('node_owner_admin._context') as context:
            with self.assertRaises(DeployError):admin.apply('/opt/node',self.value)
            context.assert_not_called()
        self.assertIn('node_owner_admin.py',CORE)


if __name__=='__main__':unittest.main()

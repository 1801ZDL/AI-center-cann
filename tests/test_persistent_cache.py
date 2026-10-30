"""Cache identity/capacity contracts; native enforcement has its own Root fixture."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT
from persistent_cache import validate,fingerprint,namespace,Pool,Lease,CacheUnavailable


def slot(uid=23801,index=1):
    return {'id':'cache'+str(index),'storage_id':str(index)*32,'path':'/srv/cache/slot'+str(index),
        'filesystem':'ext4-userquota','bytes':16*1024**2,'inodes':128,'device':54321,'root_inode':12+index,
        'quota_uid':uid,'filesystem_id':'a'*32,'filesystem_bytes':128*1024**2,'filesystem_inodes':4096}


def config():
    return {'node_id':'worker-'+'1'*12,'physical_id':'a'*64,'image':'sha256:'+'b'*64,
        'users':{'alice':{'uid':23801,'gid':23801},'bob':{'uid':23802,'gid':23802}},
        'persistent_cache':{'slots':[slot(),slot(23802,2)]}}


def grant():
    return {'owner':'alice','job_id':'job-'+'c'*12,'attempt_id':'attempt-'+'d'*32,'lease_token':'fixture-test-token',
        'environment_sha256':'e'*64,'runtime_files':{'container_entry.py':'f'*64},
        'parameters':{'lock':{'python':'3.10.12'},'environment':{}},'allocation':{}}


class PersistentCacheTests(unittest.TestCase):
    def test_only_explicit_uid_quota_provider_and_owner_mapping_are_accepted(self):
        c=config();self.assertEqual(validate(c['persistent_cache'],c['users']),c['persistent_cache'])
        for changed in [None,{}, {'slots':[{k:v for k,v in slot().items() if k not in {'quota_uid','filesystem_id','filesystem_bytes','filesystem_inodes'}}]}]:
            with self.assertRaises(ValueError):validate(changed,c['users'])
        for users in [{},{'other':{'uid':23802,'gid':23802}},{**c['users'],'alias':c['users']['alice']}]:
            with self.assertRaises(ValueError):validate(c['persistent_cache'],users)

    def test_one_owner_cannot_have_ambiguous_multiple_cache_slots(self):
        c=config();extra=slot(23801,3);extra.update(device=54322,path='/srv/cache2/slot3',filesystem_id='b'*32)
        with self.assertRaisesRegex(ValueError,'one registered'):validate({'slots':[slot(),extra]},c['users'])

    def test_byte_inode_totals_and_policy_fingerprint_are_fixed(self):
        c=config();policy=c['persistent_cache'];self.assertEqual(fingerprint(policy),fingerprint(copy.deepcopy(policy)))
        for key,value in [('bytes',64*1024**2),('inodes',2048)]:
            with self.assertRaises(ValueError):validate({'slots':[{**s,key:value} for s in policy['slots']]})
        modified=copy.deepcopy(policy);modified['slots'][0]['bytes']+=4096
        self.assertNotEqual(fingerprint(policy),fingerprint(modified))

    def test_namespace_changes_with_owner_environment_runtime_and_lock(self):
        c=config();g=grant()
        with patch('environment_contract.verify_config',return_value='e'*64):
            original=namespace(c,g);self.assertEqual(original,namespace(copy.deepcopy(c),copy.deepcopy(g)))
            for change in [{'owner':'bob'},{'runtime_files':{'container_entry.py':'0'*64}},
                {'parameters':{'lock':{'python':'3.11.0'},'environment':{}}},
                {'parameters':{'lock':g['parameters']['lock'],'environment':{'TRITON_DEBUG':'1'}}},
                {'npu_policy_sha256':'1'*64}]:
                self.assertNotEqual(original,namespace(c,{**g,**change}))
            with self.assertRaises(ValueError):namespace(c,{**g,'environment_sha256':'0'*64})
            with self.assertRaises(ValueError):namespace(c,{**g,'runtime_files':{}})
        with patch('environment_contract.verify_config',return_value=None),self.assertRaises(ValueError):namespace(c,g)

    def test_other_owner_slot_or_key_cannot_be_substituted_in_a_grant(self):
        c=config();g=grant()
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as d,patch('persistent_cache.os.geteuid',return_value=0),\
             patch('environment_contract.verify_config',return_value='e'*64):
            pool=Pool(d,c);s=c['persistent_cache']['slots'][0]
            g.update(cache_policy_sha256=fingerprint(pool.policy),allocation={'cache':{'slot_id':s['id'],'storage_id':s['storage_id'],'key':namespace(c,g)}})
            lease=Lease(pool,g);self.assertEqual(lease.slot['quota_uid'],23801)
            with self.assertRaises(CacheUnavailable):Lease(pool,{**g,'cache_policy_sha256':'0'*64})
            for cache in [{**g['allocation']['cache'],'key':'0'*64},{**g['allocation']['cache'],'path':'/tmp'},
                {'slot_id':'cache2','storage_id':'2'*32,'key':namespace(c,g)}]:
                with self.assertRaises(CacheUnavailable):Lease(pool,{**g,'allocation':{'cache':cache}})
            with self.assertRaises(CacheUnavailable):lease.release()

    def test_control_workspace_and_spool_cannot_share_native_cache_device(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as d,patch('persistent_cache.os.geteuid',return_value=0):
            c=config();Pool(d,c)
            for change in [{'workspace':{'slots':[slot()]}},{'spool_storage':{'device':54321,'path':'/srv/spool'}},
                {'persistent_cache':{'slots':[{**s,'device':Path(d).stat().st_dev} for s in c['persistent_cache']['slots']]}}]:
                with self.assertRaises(CacheUnavailable):Pool(d,{**c,**change})

    def test_unprivileged_pool_is_rejected_before_any_control_creation(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as d,patch('persistent_cache.os.geteuid',return_value=1104):
            with self.assertRaises(CacheUnavailable):Pool(d,config())
            self.assertEqual(list(Path(d).iterdir()),[])


if __name__=='__main__':unittest.main()

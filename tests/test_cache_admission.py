"""Persistent cache placement, owner routing, downgrade and result guards."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,atomic_json
from persistent_cache import CAPABILITY,fingerprint,namespace,observation
from resources import MULTI_SLOT
from workflow_spec import CAPABILITIES,cache_request
from environment_contract import CAPABILITY as ENV,fingerprint as env_fingerprint
from deployment import DeployError,unit_text
from maintenance import require_features
from test_environment_contract import contract,IMAGE
from test_persistent_cache import slot


class CacheAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='cache-admission-test-');self.root=Path(self.temp.name)
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl','scheduler':{'max_running':2}})
        for owner in ['alice','bob']:
            p=self.root/owner;p.mkdir();self.jobs.access.create_user(owner,p,environments=['worker-python'],quota={'max_running':2,'cpu':2,'memory_mib':1024})
        self.policy={'slots':[slot(),slot(23802,2)]}
        self.profile={'name':'cache-node','physical_id':self.jobs.backend.physical_id,'image':IMAGE,'python':'/usr/bin/python3',
            'environment_manifest':contract(),'persistent_cache':self.policy,
            'users':{'alice':{'uid':23801,'gid':23801},'bob':{'uid':23802,'gid':23802}},'templates':['ir_v1'],
            'policy':{'cpu_ids':self.jobs.policy['cpu_ids'],'memory_mib':2048,'shm_mib':0,'max_running':2}}
        self.node,_=self.jobs.nodes.enroll(self.profile);self.beat()

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def reading(self):
        return {'policy_sha256':fingerprint(self.policy),'slots':[{'id':s['id'],'status':'ok','leased':False,
            'free_bytes':s['bytes'],'free_inodes':s['inodes'],'namespaces':0} for s in self.policy['slots']]}

    def beat(self,reading=None,cap=True):
        self.jobs.nodes.heartbeat(self.node,{'physical_id':self.profile['physical_id'],'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1',ENV,MULTI_SLOT,*CAPABILITIES,*([CAPABILITY] if cap else [])],
            'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
            'image_sha256':IMAGE,'environment_setup':None,'python_executable':'/usr/bin/python3',
            'environment_sha256':env_fingerprint(contract()),'cache_observation':reading or self.reading()})

    def workflow(self,owner='alice',cache=None):
        user=self.jobs.access.user(owner);data=b'print(42)';manifest={'files':[{'name':'main.py','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}
        bundle,_=self.jobs.inputs.create(user,manifest)
        if not bundle['sealed']:
            self.jobs.inputs.upload(bundle['id'],user,{'name':'main.py','offset':0,'data':base64.b64encode(data).decode()});self.jobs.inputs.seal(bundle['id'],user)
        return self.jobs.submit({'template':'ir_v1','environment':'worker-python','parameters':{
            'input_id':bundle['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'},**({'cache':cache} if cache is not False else {})}},user)[0]

    def test_explicit_cache_request_rejects_paths_modes_and_unbounded_values(self):
        self.assertEqual(cache_request({'mode':'persistent'}),{'mode':'persistent','min_free_mib':1,'min_free_inodes':8})
        for v in [None,False,{'mode':'shared'},{'mode':'persistent','path':'/tmp'},{'mode':'persistent','min_free_mib':True},
                  {'mode':'persistent','min_free_inodes':0}]:
            with self.assertRaises(ValueError):cache_request(v)

    def test_matching_owner_policy_and_namespace_are_frozen_in_one_grant(self):
        job=self.workflow(cache={'mode':'persistent'});grant=self.jobs.nodes.claim(self.node)['grant']
        self.assertEqual(grant['job_id'],job['id']);self.assertEqual(grant['cache_policy_sha256'],fingerprint(self.policy))
        self.assertEqual(grant['allocation']['cache']['slot_id'],self.policy['slots'][0]['id'])
        self.assertEqual(grant['allocation']['cache']['key'],namespace(self.profile,grant,host=False))
        self.assertEqual(grant,self.jobs.nodes.claim(self.node)['grant'])

    def test_active_reservation_blocks_same_cache_even_with_stale_idle_heartbeat(self):
        first=self.workflow(cache={'mode':'persistent'});self.jobs.nodes.claim(self.node)
        second=self.workflow(cache={'mode':'persistent'});self.beat()
        self.assertIsNone(self.jobs.nodes.claim(self.node,{'exclude_attempts':[self.jobs.get(first['id'])['attempt_id']]})['grant'])
        self.assertFalse(self.jobs.attempts(second['id']));self.assertIn('缓存租约',self.jobs.get(second['id'])['reason'])

    def test_full_owner_waits_while_peer_gets_its_own_quota(self):
        read=self.reading();read['slots'][0]['free_bytes']=0;self.beat(read)
        blocked=self.workflow(cache={'mode':'persistent'});peer=self.workflow('bob',{'mode':'persistent'})
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],peer['id'])
        self.assertEqual(grant['allocation']['cache']['slot_id'],self.policy['slots'][1]['id']);self.assertFalse(self.jobs.attempts(blocked['id']))

    def test_lost_cache_preserves_queued_job_and_legacy_ephemeral_can_progress(self):
        read=self.reading()
        for r in read['slots']:r.update(status='error',leased=True,free_bytes=0,free_inodes=0)
        self.beat(read);blocked=self.workflow(cache={'mode':'persistent'});normal=self.workflow('bob',False)
        grant=self.jobs.nodes.claim(self.node)['grant'];self.assertEqual(grant['job_id'],normal['id'])
        self.assertNotIn('cache',grant['allocation']);self.assertFalse(self.jobs.attempts(blocked['id']))

    def test_capability_and_bounded_observations_are_required(self):
        with self.assertRaises(JobError):self.beat(cap=False)
        for change in [{'free_bytes':128*1024**2},{'free_inodes':True},{'status':'error','leased':False},{'id':'unknown'}]:
            read=self.reading();read['slots'][0].update(change)
            with self.assertRaises(ValueError):observation(read,self.policy)

    def test_cross_node_cache_filesystem_or_nested_workspace_alias_is_rejected(self):
        for field in ['persistent_cache','workspace']:
            peer=copy.deepcopy(self.profile);peer['name']='peer';peer.pop('persistent_cache');peer[field]=self.policy
            with self.assertRaises(JobError):self.jobs.nodes.enroll(peer)
        peer=copy.deepcopy(self.profile);peer['name']='nested'
        peer['persistent_cache']['slots']=[{**slot(),'device':54322,'path':slot()['path']+'/nested'}]
        with self.assertRaises(JobError):self.jobs.nodes.enroll(peer)

    def test_public_cache_state_contains_no_paths_or_uid_mapping(self):
        public=next(n for n in self.jobs.nodes.public_nodes() if n['id']==self.node)['persistent_cache']
        self.assertTrue(public['enforced']);self.assertNotIn('/srv',json.dumps(public));self.assertNotIn('quota_uid',json.dumps(public))

    def test_terminal_result_requires_cache_release_before_artifact_processing(self):
        self.workflow(cache={'mode':'persistent'});g=self.jobs.nodes.claim(self.node)['grant']
        body={'job_id':g['job_id'],'attempt_id':g['attempt_id'],'lease_token':g['lease_token'],
            'result':{'status':'failed','termination_confirmed':True,'execution_cleanup_confirmed':True}}
        with self.assertRaisesRegex(JobError,'cache lease release'):self.jobs.nodes.complete(self.node,body)

    def test_installed_node_unit_and_downgrade_guard_include_cache_policy(self):
        atomic_json(self.root/'config.json',{'persistent_cache':self.policy})
        text=unit_text('/opt/lab',{'uid':0,'gid':0,'role':'node','state_dir':str(self.root)})
        self.assertIn('"/srv/cache"',text);self.assertIn('CAP_SYS_ADMIN',text)
        with self.assertRaisesRegex(DeployError,'cache lifecycle'):require_features(self.root,'node',{'files':{}})
        atomic_json(self.root/'config.json',{})
        with self.assertRaisesRegex(DeployError,'cache policies'):require_features(self.root,'controller',{'files':{}})


if __name__=='__main__':unittest.main()

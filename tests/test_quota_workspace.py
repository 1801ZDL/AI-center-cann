"""UID routing, alias/capacity accounting and privileged service configuration."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,atomic_json
from workspace_slots import validate,fingerprint,selected,allocation
from quota_workspace import CAPABILITY,capacity,separate_control,requested_limits
from deployment import unit_text


def slot(uid=23001,index=1,byte_limit=16*1024**2):
    return {'id':'quota'+str(index),'storage_id':str(index)*32,'path':'/srv/quota/slot'+str(index),
       'filesystem':'ext4-userquota','bytes':byte_limit,'inodes':128,'device':54321,'root_inode':12+index,
       'quota_uid':uid,'filesystem_id':'a'*32,'filesystem_bytes':128*1024**2,'filesystem_inodes':4096}


class QuotaWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='quota-workspace-test-');self.root=Path(self.temp.name)
        self.jobs=JobStore(self.root,{'username':'operator','password':'offline-only','container':'zdl'})
        for owner in ['alice','bob']:
            path=self.root/owner;path.mkdir();self.jobs.access.create_user(owner,path,environments=['worker-python'])
        self.slots=[slot(),slot(23002,2,24*1024**2)]
        self.profile={'name':'quotas','physical_id':self.jobs.backend.physical_id,
            'users':{'alice':{'uid':23001,'gid':23001},'bob':{'uid':23002,'gid':23002}},'templates':['cpu_demo'],
            'workspace':{'slots':self.slots},
            'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':768,'shm_mib':0,'max_running':1}}

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def beat(self,node,capabilities=None,free=None):
        policy=self.jobs.nodes.node(node)['profile']['workspace']
        body={'physical_id':self.jobs.backend.physical_id,'protocol_version':1,
          'capabilities':capabilities or ['start-ticket-v1','cleanup-before-complete-v1','filesystem-workspace-v1',CAPABILITY],
          'cpu_ids':self.jobs.policy['cpu_ids'],'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3,
          'workspace_observation':{'policy_sha256':fingerprint(policy),'slots':[{'id':s['id'],'status':'ok','clean':True,
                   'free_bytes':s['bytes'] if free is None else free,'free_inodes':s['inodes']} for s in policy['slots']]}}
        self.jobs.nodes.heartbeat(node,body)

    def test_same_filesystem_distinct_uids_and_private_allocations(self):
        validate({'slots':self.slots})
        public=allocation(self.slots[0]);self.assertEqual(public['provider'],'uid-quota-v1')
        self.assertNotIn('quota_uid',public);self.assertNotIn('path',public)
        for change in [{'quota_uid':23001},{'filesystem_id':'b'*32},{'filesystem_bytes':256*1024**2}]:
            with self.assertRaises(ValueError):validate({'slots':[self.slots[0],{**self.slots[1],**change}]})

    def test_sum_of_caps_leaves_filesystem_and_metadata_reserve(self):
        for changes in [{'bytes':64*1024**2},{'inodes':2048}]:
            with self.assertRaises(ValueError):validate({'slots':[{**s,**changes} for s in self.slots]})

    def test_owner_uid_selects_its_own_slot_and_node_cannot_substitute_another(self):
        node,_=self.jobs.nodes.enroll(self.profile);self.beat(node)
        job,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('bob'))
        grant=self.jobs.nodes.claim(node)['grant'];self.assertEqual(grant['job_id'],job['id'])
        self.assertEqual(grant['allocation']['workspace']['slot_id'],self.slots[1]['id'])
        self.assertEqual(selected(self.profile['workspace'],grant,23002),self.slots[1])
        with self.assertRaisesRegex(ValueError,'another execution UID'):selected(self.profile['workspace'],grant,23001)
        with self.assertRaises(ValueError):selected(self.profile['workspace'],grant)

    def test_unused_peer_quota_is_not_a_fallback_when_owner_has_no_slot(self):
        profile=copy.deepcopy(self.profile);profile['workspace']['slots']=self.slots[1:]
        node,_=self.jobs.nodes.enroll(profile);self.beat(node)
        self.jobs.submit({'template':'cpu_demo','environment':'worker-python'},self.jobs.access.user('alice'))
        self.assertIsNone(self.jobs.nodes.claim(node)['grant'])
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)

    def test_physical_filesystem_capacity_is_shared_across_nodes(self):
        first=copy.deepcopy(self.profile);first['workspace']['slots']=[self.slots[0]]
        self.jobs.nodes.enroll(first)
        second=copy.deepcopy(self.profile);second['name']='peer';second['workspace']['slots']=[self.slots[1]]
        self.jobs.nodes.enroll(second)
        over=copy.deepcopy(self.profile);over['name']='over';over['workspace']['slots']=[slot(23003,3,100*1024**2)]
        with self.assertRaisesRegex(JobError,'totals'):self.jobs.nodes.enroll(over)
        alias=copy.deepcopy(self.profile);alias['name']='alias';alias['workspace']['slots']=[slot(23001,4)]
        with self.assertRaises(JobError):self.jobs.nodes.enroll(alias)

    def test_missing_version_and_physical_free_space_as_user_quota_are_rejected(self):
        node,_=self.jobs.nodes.enroll(self.profile)
        with self.assertRaisesRegex(JobError,'uid-quota'):self.beat(node,['start-ticket-v1','cleanup-before-complete-v1','filesystem-workspace-v1'])
        with self.assertRaises(JobError):self.beat(node,free=128*1024**2)

    def test_control_directory_cannot_consume_the_user_quota_filesystem(self):
        policy={'slots':[{**self.slots[0],'device':self.root.stat().st_dev}]}
        with self.assertRaisesRegex(ValueError,'control/spool'):separate_control(policy,self.root/'not-created-yet')

    def test_systemd_node_gets_required_mounts_and_query_capability_only_for_quota(self):
        atomic_json(self.root/'config.json',{'workspace':self.profile['workspace']})
        meta={'uid':0,'gid':0,'role':'node','state_dir':str(self.root)}
        text=unit_text('/opt/lab',meta)
        self.assertIn('CapabilityBoundingSet=CAP_SYS_ADMIN',text)
        self.assertIn('RequiresMountsFor="/srv/quota"',text)
        self.assertIn('ReadWritePaths='+str(self.root)+' "/srv/quota"',text)
        plain={**self.slots[0],'filesystem':'ext4'}
        for key in ['quota_uid','filesystem_id','filesystem_bytes','filesystem_inodes']:plain.pop(key)
        atomic_json(self.root/'config.json',{'workspace':{'slots':[plain]}})
        text=unit_text('/opt/lab',meta);self.assertIn('CapabilityBoundingSet=\n',text)
        self.assertIn('"/srv/quota/slot1"',text)
        controller=unit_text('/opt/lab',{**meta,'role':'controller','uid':999})
        self.assertIn('CapabilityBoundingSet=\n',controller);self.assertNotIn('/srv/quota',controller)

    def test_prepare_input_does_not_accept_root_duplicate_uids_or_tiny_limits(self):
        good={'uid':23001,'bytes':16*1024**2,'inodes':128};self.assertEqual(requested_limits([good]),[good])
        for value in [[{**good,'uid':0}],[good,good],[{**good,'bytes':True}],[{**good,'inodes':0}]]:
            with self.assertRaises(ValueError):requested_limits(value)
        if os.geteuid()!=0:
            result=subprocess.run([str(ROOT/'servicectl'),'workspace-prepare','--root',str(self.root/'missing'),
                '--limits',str(self.root/'not-read.json'),'--plan'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('requires root',result.stderr)
            self.assertFalse((self.root/'missing').exists())


if __name__=='__main__':unittest.main()

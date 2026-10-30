"""Central onboarding request/packet authority and transactional admission."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import owner_provision as master
import deployment as deploy
from deployment import DeployError,CORE
from jobs import JobStore,JobError
from lab_client import atomic_json
from maintenance import require_features
import test_nodes as nodes_fixture
import test_owner_handoff as handoff_fixture
import test_payload_owner_admin as storage_fixture


def spec():
    return {'operation_id':'a'*32,'owner':'bob','quota':{'input_storage_mib':4,'archive_storage_mib':32},
        'storage':{'gid':23832,'bytes':64*1024**2,'inodes':512},'service':{'mode':'process','unit_dir':None},
        'delivery':{'owner':'bob','parent':'/home/bob','name':'agent','uid':23802,'gid':23802,'spool':'/home/bob/.root-spool'},
        'nodes':[{'node_id':'worker-'+'b'*12,'uid':23802,'gid':23802,
            'workspace':{'root':'/srv/work','bytes':16*1024**2,'inodes':128},'cache':{'root':'/srv/cache','bytes':16*1024**2,'inodes':128}}]}


def packet():
    value={'format':master.PACKET,'flow_id':'d'*64,'node_id':'worker-'+'b'*12,'physical_id':'c'*64,
        'controller':{'url':'http://127.0.0.1:18765','authority_sha256':None},'request':{
            'operation_id':'e'*32,'owner':'bob','stage_id':'f'*64,**{k:spec()['nodes'][0][k] for k in ['uid','gid','workspace','cache']}}}
    value['id']=master.sha(value);return value


class OwnerProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-owner-provision-');self.root=Path(self.temp.name)
        self.prefix=self.root/'prefix';self.prefix.mkdir();self.state=self.root/'state';self.state.mkdir()

    def tearDown(self):self.temp.cleanup()

    def gate(self,phase='maintenance',allowed=False):
        return {'format':master.FORMAT,'id':'f'*64,'prefix':str(self.prefix),'state_dir':str(self.state),
            'phase':phase,'service_allowed':allowed}

    def write_gate(self,value,mode=0o644):
        path=self.prefix/master.GATE;atomic_json(path,value);path.chmod(mode)

    def fake_root(self):
        original=Path.lstat
        def info(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            return SimpleNamespace(st_uid=0,st_mode=value.st_mode,st_nlink=value.st_nlink,st_size=value.st_size)
        return patch.object(Path,'lstat',info)

    def test_explicit_request_has_no_command_transport_or_credentials(self):
        value=spec();result=master.request(value);value['quota']['cpu']=2;self.assertNotIn('cpu',result['quota'])
        for key in ['ssh','command','token','password','node_key','remote_root']:
            with self.subTest(key=key),self.assertRaises(DeployError):master.request({**spec(),key:'forbidden'})

    def test_finite_storage_covers_quota_before_any_provisioning(self):
        for change in [{'bytes':1024**2},{'gid':0},{'gid':True},{'inodes':False}]:
            value=spec();value['storage'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):master.request(value)
        for change in [{'npu':1},{'memory_mib':128},{'max_running':0},{'archive_storage_mib':1}]:
            value=spec();value['quota'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):master.request(value)

    def test_required_nodes_uid_paths_and_delivery_cannot_be_implicit(self):
        for nodes in [[],spec()['nodes']*2,[{**spec()['nodes'][0],'node_id':'wrong'}]]:
            with self.assertRaises(DeployError):master.request({**spec(),'nodes':nodes})
        value=spec();value['nodes'][0]['uid']=0
        with self.assertRaises(ValueError):master.request(value)
        value=spec();value['delivery']['owner']='alice'
        with self.assertRaises(DeployError):master.request(value)

    def test_lifecycle_has_no_arbitrary_stop_start_or_shell_options(self):
        for service in [{'mode':'shell','unit_dir':None},{'mode':'systemd','unit_dir':None},
                        {'mode':'systemd','unit_dir':'/etc/../tmp'},{'mode':'process','unit_dir':None,'command':'x'}]:
            with self.subTest(service=service),self.assertRaises(DeployError):master.request({**spec(),'service':service})
        master.request({**spec(),'service':{'mode':'systemd','unit_dir':'/etc/systemd/system'}})

    def test_packet_digest_and_controller_authority_are_bound(self):
        value=packet();master.packet_check(value)
        for key,wrong in [('node_id','worker-'+'d'*12),('flow_id','e'*64),('physical_id','e'*64)]:
            with self.assertRaises(DeployError):master.packet_check({**value,key:wrong})
        for controller in [{'url':'http://127.0.0.1:18765','authority_sha256':'wrong'},
                           {'url':'http://127.0.0.1:18765','authority_sha256':None,'ssh':'no'}]:
            wrong={**value,'controller':controller};wrong['id']=master.sha({k:v for k,v in wrong.items() if k!='id'})
            with self.assertRaises(ValueError):master.packet_check(wrong)

    def test_wrong_node_packet_refuses_before_native_side_effects(self):
        source=Path(master.__file__);release=self.root/'release';release.mkdir();(release/source.name).write_bytes(source.read_bytes())
        meta={'role':'node','state_dir':str(self.state)};atomic_json(self.state/'config.json',{
            'node_id':'worker-'+'e'*12,'physical_id':packet()['physical_id'],'controller':'http://127.0.0.1:18765'})
        with patch('deployment.require_root'),patch('deployment.load_installation',return_value=(self.prefix,meta,release)),patch('node_owner_provision.provision') as allocate:
            with self.assertRaisesRegex(DeployError,'different Node'):master.node_participate(self.prefix,packet())
            allocate.assert_not_called()

    def test_guard_is_root_and_exact_installation_scoped(self):
        (self.prefix/master.JOURNAL).touch()
        with self.assertRaises(DeployError):master.guard(self.prefix)
        token=master._ROOT.set(str(self.prefix))
        try:
            with patch.object(master.os,'geteuid',return_value=23802),self.assertRaises(DeployError):master.guard(self.prefix)
            with patch.object(master.os,'geteuid',return_value=0):master.guard(self.prefix)
        finally:master._ROOT.reset(token)

    def test_missing_gate_with_journal_holds_but_open_gate_allows_other_users(self):
        journal=self.prefix/master.JOURNAL;journal.touch()
        self.assertTrue(master.admission(self.prefix,self.state))
        self.write_gate(self.gate())
        with self.fake_root():
            self.assertTrue(master.admission(self.prefix,self.state))
            self.write_gate(self.gate('open',True))
            self.assertFalse(master.admission(self.prefix,self.state))
            journal.unlink();self.assertTrue(master.admission(self.prefix,self.state))

    def test_gate_mode_binding_and_open_permission_fail_closed(self):
        (self.prefix/master.JOURNAL).touch();path=self.prefix/master.GATE
        self.write_gate(self.gate('open',False))
        with self.fake_root(),self.assertRaises(DeployError):master.admission(self.prefix,self.state)
        self.write_gate(self.gate(),0o600)
        with self.fake_root(),self.assertRaises(DeployError):master.admission(self.prefix,self.state)
        self.write_gate({**self.gate(),'state_dir':'/another'})
        with self.fake_root(),self.assertRaises(DeployError):master.admission(self.prefix,self.state)

    def test_service_start_is_phase_gated_but_stop_remains_possible(self):
        (self.prefix/master.JOURNAL).touch();meta={'role':'controller','state_dir':str(self.state)}
        master.service_guard(self.prefix,meta,stopping=True)
        with self.assertRaises(DeployError):master.service_guard(self.prefix,meta)
        self.write_gate(self.gate('maintenance',True))
        with self.fake_root():master.service_guard(self.prefix,meta)
        with self.assertRaises(DeployError):master.service_guard(self.prefix,{**meta,'role':'node'},stopping=True)

    def test_ordinary_recovery_cannot_bypass_central_guard(self):
        release=self.root/'release';release.mkdir();(self.prefix/master.JOURNAL).touch()
        meta={'role':'controller','uid':os.getuid(),'state_dir':str(self.state),'release_id':'a'*64,'state_schema':1};manifest={'id':'a'*64,'state_schema':1,'files':{}}
        with patch('deployment.load_installation',return_value=(self.prefix,meta,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
            for flags in [{},{'quota_recovery':True},{'startup_recovery':True},{'delivery_recovery':True,'finish_recovery':True}]:
                with self.subTest(flags=flags),self.assertRaisesRegex(DeployError,'central owner'):deploy.verify(self.prefix,**flags)

    def test_submit_rechecks_hold_inside_transaction_without_reserving_storage(self):
        jobs=JobStore(self.state,{'username':'test_admin','container':'zdl','scheduler':{'workspace_root':str(self.state)}})
        request={'environment':'host-python','argv':['python','-c','print(1)'],'cwd':str(self.state),'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0}
        with patch('unit_start.admission_reason',side_effect=[None,'maintenance-hold']),self.assertRaises(JobError) as caught:jobs.submit(request)
        self.assertEqual(caught.exception.status,503)
        with jobs.connect() as db:
            self.assertFalse(jobs.all(db));self.assertEqual(db.execute('SELECT COUNT(*) FROM storage_allocations').fetchone()[0],0)
        jobs.access.close()

    def test_claim_rechecks_hold_after_acquiring_transaction(self):
        fixture=nodes_fixture.NodeTests();fixture.setUp()
        try:
            with patch('unit_start.admission_reason',side_effect=[None,'maintenance-hold']):result=fixture.nodes.claim(fixture.a)
            self.assertIsNone(result['grant']);self.assertEqual(result['reason'],'maintenance-hold')
        finally:fixture.tearDown()

    def test_approval_rejects_changed_node_profile_before_committing(self):
        fixture=handoff_fixture.OwnerHandoffTests();fixture.setUp()
        try:
            with self.assertRaisesRegex(JobError,'profile changed'):fixture.h.approve(fixture.node,fixture.plan,'0'*64)
            self.assertEqual(fixture.h.status(fixture.node),[])
            before=master.sha(fixture.jobs.nodes.node(fixture.node)['profile'])
            fixture.h.approve(fixture.node,fixture.plan,before)
        finally:fixture.tearDown()

    def test_native_storage_preflight_refuses_reserved_identity_and_duplicate_gid_before_io(self):
        fixture=storage_fixture.PayloadOwnerTests();fixture.setUp()
        try:
            value=spec();value['owner']='owner_admin'
            with patch('group_quota.read') as read,self.assertRaises(DeployError):master._preflight_storage(fixture.meta,fixture.state,fixture.config,value)
            read.assert_not_called()
            value=spec();value['storage']['gid']=23831
            with patch('group_quota.read') as read,self.assertRaises(ValueError):master._preflight_storage(fixture.meta,fixture.state,fixture.config,value)
            read.assert_not_called()
        finally:fixture.tearDown()

    def test_node_resource_roles_and_uid_conflicts_are_checked_before_staging(self):
        fixture=handoff_fixture.OwnerHandoffTests();fixture.setUp()
        try:
            value=spec();value['nodes'][0]['node_id']=fixture.node
            value['nodes'][0]['workspace']['root']='/srv/work';value['nodes'][0]['cache']['root']='/srv/cache'
            self.assertEqual(set(master._nodes(fixture.root,value)),{fixture.node})
            value['nodes'][0]['uid']=23801
            with self.assertRaisesRegex(DeployError,'already mapped'):master._nodes(fixture.root,value)
            value['nodes'][0]['uid']=23802;value['nodes'][0]['workspace']['root']='/srv/cache'
            with self.assertRaisesRegex(DeployError,'resource role'):master._nodes(fixture.root,value)
        finally:fixture.tearDown()

    def test_completed_history_cannot_change_owner_enable_credentials_or_reuse_operation(self):
        value=spec();record={'id':'d'*64,'request':value,'stage_id':'e'*64};result=master._result(record,self.root,state='delivered')
        result.update(activation_id='f'*32,finish_id='a'*64);saved={'id':record['id'],'request':value,'result':result}
        master._receipt(saved,record['id'],value)
        for change in [{'owner':'alice'},{'credentials_in_output':True},{'historical_receipt_only':True},{'activation_id':'bad'}]:
            with self.subTest(change=change),self.assertRaises(DeployError):master._receipt({**saved,'result':{**result,**change}},record['id'],value)
        with self.assertRaises(DeployError):master._receipt(saved,record['id'],{**value,'quota':{'cpu':2}})

    def test_participant_state_prevents_downgrade(self):
        (self.state/'owner-provision').mkdir()
        with self.assertRaisesRegex(DeployError,'Central owner'):require_features(self.state,'controller',{'files':{n:{} for n in CORE if n!='owner_provision.py'}})

    def test_metadata_size_limit_precedes_disk_write(self):
        with patch.object(master,'MAX_RECORD',16),patch.object(master.files,'write') as write:
            with self.assertRaises(DeployError):master._write(self.root/'not-created',{'data':'x'*100})
            write.assert_not_called()

    def test_unprivileged_commands_fail_before_reading_spec_or_packet(self):
        if os.geteuid()==0:return
        for action,flag in [('owner-provision','--spec'),('owner-provision-node','--request')]:
            result=subprocess.run(['/usr/bin/python3',str(ROOT/'servicectl'),action,'--prefix','/not-selected',flag,'/not-readable'],capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0);self.assertIn('requires root',result.stderr)


if __name__=='__main__':unittest.main()

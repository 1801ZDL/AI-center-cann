"""Node participant authority, immutable plans, admission and bounded metadata."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import node_owner_provision as participant
import test_workspace_quota_admin as native_fixture
import test_node_owner_admin as binding_fixture
from deployment import DeployError,CORE
import deployment as deploy
from maintenance import require_features,assert_quiescent
from worker import Worker
from node_owner_admin import sha


def spec():
    return {'operation_id':'f'*32,'owner':'bob','stage_id':'c'*64,'uid':23002,'gid':23002,
        'workspace':{'root':'/srv/quota','bytes':16*1024**2,'inodes':128},
        'cache':{'root':'/srv/cache','bytes':16*1024**2,'inodes':128}}


class ParticipantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-node-participant-');self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def definition(self):
        value=spec();plans={'workspace':native_fixture.planned(),'cache':native_fixture.planned()}
        item=plans['cache'];item['root']='/srv/cache';item['before']['slots'][0]['path']=item['before']['slots'][0]['path'].replace('/srv/quota/','/srv/cache/')
        item['additions'][0].update(id='slot-'+'e'*12,storage_id='f'*32);native_fixture.signed(item)
        context={'prefix':'/opt/node','state_dir':'/var/lib/node','release_id':'a'*64,'node_id':'worker-'+'b'*12,
            'physical_id':'f'*64,'control_identity':{'device':123,'inode':456},
            'base_configuration_sha256':'d'*64,'base_installation_sha256':'e'*64,'credential_independent_configuration_sha256':'e'*64}
        record={'format':participant.FORMAT,'id':'e'*64,'request':value,'binding':context,
            'native_plans':plans,'slots':{},'phase':'preparing','proposal_id':None}
        return value,context,record

    def test_explicit_request_is_copied_without_resources_or_credentials(self):
        value=spec();actual=participant.request(value);value['workspace']['bytes']=1
        self.assertEqual(actual['workspace']['bytes'],16*1024**2)
        for extra in ['token','node_key','command','ssh','password']:
            with self.assertRaises(DeployError):participant.request({**spec(),extra:'forbidden'})

    def test_operation_owner_stage_uid_and_paths_cannot_be_implicit(self):
        for field,wrong in [('operation_id','bad'),('stage_id',1),('uid',0),('uid',True),('gid',0),('owner','../x')]:
            with self.subTest(field=field),self.assertRaises(DeployError):participant.request({**spec(),field:wrong})
        for path in ['relative','/srv/x/../quota','/','/srv//quota','/srv/quota/']:
            value=spec();value['workspace']['root']=path
            with self.subTest(path=path),self.assertRaises(DeployError):participant.request(value)

    def test_native_limits_and_filesystem_roles_are_bounded(self):
        for changed in [{'bytes':True},{'bytes':0},{'bytes':1024**4+1},{'inodes':False},{'inodes':10**7+1},{'root':'/srv/cache'}]:
            value=spec();value['workspace'].update(changed)
            with self.subTest(changed=changed),self.assertRaises(DeployError):participant.request(value)

    def test_guard_blocks_presence_and_dangling_links_without_reading_them(self):
        path=self.root/participant.JOURNAL;path.touch()
        with self.assertRaisesRegex(DeployError,'participant'):participant.guard(self.root)
        path.unlink();path.symlink_to(self.root/'absent')
        with self.assertRaises(DeployError):participant.guard(self.root)

    def test_unprivileged_context_cannot_bypass_guard(self):
        (self.root/participant.JOURNAL).touch();token=participant._PARTICIPANT.set(str(self.root))
        try:
            with patch.object(participant.os,'geteuid',return_value=23002),self.assertRaises(DeployError):participant.guard(self.root)
        finally:participant._PARTICIPANT.reset(token)

    def test_root_scope_is_exact_and_does_not_propagate_to_other_threads(self):
        (self.root/participant.JOURNAL).touch();token=participant._PARTICIPANT.set(str(self.root));refused=[]
        try:
            with patch.object(participant.os,'geteuid',return_value=0):
                participant.guard(self.root)
                def peer():
                    try:participant.guard(self.root)
                    except DeployError:refused.append(True)
                thread=threading.Thread(target=peer);thread.start();thread.join()
                other=self.root/'other';other.mkdir();(other/participant.JOURNAL).touch()
                with self.assertRaises(DeployError):participant.guard(other)
            self.assertEqual(refused,[True])
        finally:participant._PARTICIPANT.reset(token)

    def test_quota_or_startup_recovery_flags_cannot_bypass_participant(self):
        prefix=self.root/'prefix';release=self.root/'release';prefix.mkdir();release.mkdir();(self.root/participant.JOURNAL).touch()
        meta={'role':'node','uid':0,'state_dir':str(self.root),'release_id':'a'*64,'state_schema':1};manifest={'id':'a'*64,'state_schema':1,'files':{}}
        with patch('deployment.load_installation',return_value=(prefix,meta,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
            for flags in [{},{'quota_recovery':True},{'startup_recovery':True},{'delivery_recovery':True,'finish_recovery':True}]:
                with self.subTest(flags=flags),self.assertRaisesRegex(DeployError,'participant'):deploy.verify(prefix,**flags)
            with self.assertRaises(DeployError):deploy._service_verify(prefix)

    def test_worker_and_offline_maintenance_stop_before_runtime_discovery(self):
        (self.root/participant.JOURNAL).touch()
        with patch('docker_runtime.discover') as discovery:
            with self.assertRaises(DeployError):Worker({'state_dir':str(self.root),'controller':'http://127.0.0.1:1','node_key':'participant-unit-fixture'})
            discovery.assert_not_called()
        with self.assertRaises(DeployError):assert_quiescent(self.root,'node')

    def test_old_release_cannot_interpret_pending_or_completed_participant(self):
        old={'files':{name:{} for name in CORE if name!='node_owner_provision.py'}}
        marker=self.root/participant.JOURNAL;marker.touch()
        with self.assertRaisesRegex(DeployError,'participant'):require_features(self.root,'node',old)
        marker.unlink();(self.root/participant.HISTORY).mkdir()
        with self.assertRaises(DeployError):require_features(self.root,'node',old)

    def test_frozen_native_plans_must_match_exact_node_uid_and_request(self):
        value,context,record=self.definition();participant._validate(record,record['id'],value,context)
        for changes in [{'node_id':'worker-'+'d'*12},{'control_identity':[124,456]},{'root':'/another'},{'release_id':'b'*64}]:
            item=copy.deepcopy(record);plan=item['native_plans']['workspace'];plan.update(changes);native_fixture.signed(plan)
            with self.subTest(changes=changes),self.assertRaises(DeployError):participant._validate(item,item['id'],value,context)

    def test_unknown_phase_or_replaced_slot_and_request_are_rejected(self):
        value,context,record=self.definition()
        for changes in [{'phase':'enabled'},{'request':{**value,'uid':23003}},{'proposal_id':'a'*64}]:
            with self.subTest(changes=changes),self.assertRaises(DeployError):participant._validate({**record,**changes},record['id'],value,context)
        record['slots']['workspace']={'id':'slot-'+'f'*12,'storage_id':'a'*32,'quota_uid':23002,'bytes':16*1024**2,'inodes':128}
        with self.assertRaises(DeployError):participant._validate(record,record['id'],value,context)

    def test_node_key_rotation_only_relaxes_credential_hash_after_local_binding(self):
        value,context,record=self.definition();rotated={**context,'base_configuration_sha256':'c'*64}
        self.assertNotEqual(participant._ack_context(record,rotated),context)
        record['phase']='acknowledging'
        self.assertEqual(participant._ack_context(record,rotated),context)
        for changed in [{'credential_independent_configuration_sha256':'a'*64},{'physical_id':'d'*64},
                        {'release_id':'b'*64},{'base_installation_sha256':'b'*64}]:
            self.assertNotEqual(participant._ack_context(record,{**rotated,**changed}),context)

    def test_proposal_adoption_uses_original_plan_and_bound_slot_identities(self):
        fixture=binding_fixture.NodeOwnerTests();fixture.setUp()
        try:
            value=fixture.value;s=value['request'];record={'request':{**spec(),**{k:s[k] for k in ['owner','stage_id','uid','gid']}},
                'binding':{'node_id':value['node_id'],'physical_id':value['physical_id'],'prefix':value['prefix'],
                    'state_dir':value['state_dir'],'release_id':value['release_id'],'base_configuration_sha256':value['config_base_sha256'],
                    'base_installation_sha256':value['meta_base_sha256'],'control_identity':value['control_identity']},
                'slots':{'workspace':value['after']['workspace']['slots'][-1],'cache':value['after']['persistent_cache']['slots'][-1]},'proposal_id':None}
            with patch.object(participant,'_read',return_value=value):
                self.assertEqual(participant._proposal(record,self.root/'orphan-proposal'),value)
                record['proposal_id']='e'*64
                with self.assertRaises(DeployError):participant._proposal(record,self.root/'orphan-proposal')
                record['proposal_id']=None;record['binding']['physical_id']='b'*64
                with self.assertRaises(DeployError):participant._proposal(record,self.root/'orphan-proposal')
        finally:fixture.tearDown()

    def test_metadata_limits_apply_before_writing_or_accepting_unknown_history(self):
        with patch.object(participant,'MAX_RECORD',16),patch.object(participant.files,'write') as write:
            with self.assertRaises(DeployError):participant._write(self.root/'no-file',{'too_large':'x'*30})
            write.assert_not_called()
        directory=self.root/'history';directory.mkdir(mode=0o700)
        actual=Path.lstat
        def root_info(path,*args,**kwargs):
            s=actual(path,*args,**kwargs)
            return SimpleNamespace(st_uid=0,st_mode=s.st_mode,st_nlink=s.st_nlink,st_size=s.st_size)
        with patch.object(Path,'lstat',root_info):
            participant._history(directory)
            target=directory/('a'*64+'.proposal.json');target.write_text('{}');target.chmod(0o600)
            participant._history(directory)
            temporary=directory/('.'+'a'*64+'.receipt.json.'+'b'*32);temporary.write_text('partial');temporary.chmod(0o600)
            participant._history(directory)
            with patch.object(participant,'MAX_HISTORY_FILES',1),self.assertRaises(DeployError):participant._history(directory)
            with patch.object(participant,'MAX_HISTORY_BYTES',1),self.assertRaises(DeployError):participant._history(directory)
            unknown=directory/'unknown';unknown.touch()
            with self.assertRaises(DeployError):participant._history(directory)

    def test_historical_receipt_cannot_claim_activation_or_replace_original_operation(self):
        value,context,record=self.definition();record.update(proposal_id='c'*64,slots={'workspace':{},'cache':{}})
        path=Path(context['state_dir'])/participant.HISTORY/(record['id']+'.proposal.json')
        result=participant._result(record,'bound',path)
        saved={'format':participant.FORMAT,'id':record['id'],'request':value,'binding':context,'result':result}
        self.assertEqual(participant._receipt(saved,record['id'],value),result)
        for change in [{'credentials_issued':True},{'central_owner_activation_still_required':False},
                       {'node_start_and_fresh_heartbeat_required':False},{'proposal_path':'/another'},
                       {'state':'enabled'},{'owner':'alice'},{'operation_id':'e'*32}]:
            wrong={**saved,'result':{**result,**change}}
            with self.subTest(change=change),self.assertRaises(DeployError):participant._receipt(wrong,record['id'],value)
        with self.assertRaises(DeployError):participant._receipt(saved,record['id'],{**value,'uid':23003})

    def test_interrupted_atomic_writes_are_retained_and_have_a_finite_budget(self):
        actual=Path.lstat
        def root_info(path,*args,**kwargs):
            s=actual(path,*args,**kwargs)
            return SimpleNamespace(st_uid=0,st_mode=s.st_mode,st_nlink=s.st_nlink,st_size=s.st_size)
        paths=[]
        for digit in ['a','b','c']:
            path=self.root/('.'+participant.JOURNAL+'.'+digit*32);path.write_text('partial');path.chmod(0o600);paths.append(path)
        with patch.object(Path,'lstat',root_info):
            participant._temporaries(self.root)
            self.assertTrue(all(p.read_text()=='partial' for p in paths))
            paths[0].chmod(0o644)
            with self.assertRaises(DeployError):participant._temporaries(self.root)
            paths[0].chmod(0o600)
            for path in paths:
                with path.open('r+b') as file:file.truncate(9*1024**2)
            with self.assertRaisesRegex(DeployError,'finite budget'):participant._temporaries(self.root)
            self.assertTrue(all(p.exists() for p in paths))

    def test_unprivileged_cli_refuses_before_opening_request_or_installation(self):
        if os.geteuid()==0:return
        result=subprocess.run(['/usr/bin/python3',str(ROOT/'servicectl'),'node-owner-provision','--prefix','/not-selected',
            '--spec','/not-readable','--complete'],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertIn('requires root',result.stderr)


if __name__=='__main__':unittest.main()

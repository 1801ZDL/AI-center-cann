"""Startup nonce, admission and positive/negative ready evidence contracts."""
import copy
import json
import os
from pathlib import Path
import sys
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import deployment as deploy
from deployment import DeployError
from jobs import JobStore,JobError,identity
from lab_client import atomic_json
from nodes import NodeRegistry
from payload_quota_admin import fingerprint
import unit_start as start
import unit_manager as manager
import test_unit_refresh as offline
import unit_manager_root_fixture as fixture


def plan_value():
    old=offline.plan();contract={k:v for k,v in old.items() if k not in {'format','id','desired'}}
    contract['expected']=manager.expected('/opt/ascend-unit-fixture',offline.meta(),old['desired'])
    value={'format':start.FORMAT,'contract':contract,'manager':fixture.anchor(),'nonce':'a'*32,'required_nodes':[]}
    value['id']=fingerprint(value);return value


class UnitStartTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-start-');self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_start_method_uses_fail_mode_and_never_restarts(self):
        bus=start.Bus()
        with patch.object(bus,'call',return_value='/org/freedesktop/systemd1/job/1') as call:
            bus.start('ascend-lab-controller.service')
        self.assertEqual(call.call_args.args[-4:],('StartUnit','ss',['ascend-lab-controller.service','fail'],'o'))
        self.assertFalse(hasattr(bus,'restart'))

    def test_startup_plan_bound_shape_and_node_set(self):
        value=plan_value();start._validate(value)
        for key,bad in [('nonce','short'),('required_nodes',None),('contract',{}),('manager',{}),('format','wrong')]:
            changed=copy.deepcopy(value);changed[key]=bad;changed['id']=fingerprint({k:v for k,v in changed.items() if k!='id'})
            with self.subTest(key=key),self.assertRaises(DeployError):start._validate(changed)
        value['required_nodes']=[{'id':'worker-'+'a'*12,'profile_sha256':'b'*64}]*2;value['id']=fingerprint({k:v for k,v in value.items() if k!='id'})
        with self.assertRaises(DeployError):start._validate(value)

    def test_non_root_operations_fail_before_bus(self):
        with patch('unit_start.deploy.require_root',side_effect=DeployError('Root required')),patch.object(start.Bus,'start') as send:
            with self.assertRaises(DeployError):start.plan('/missing')
            with self.assertRaises(DeployError):start.apply('/missing',{})
            send.assert_not_called()

    def test_startup_plan_is_bounded_before_any_durable_write(self):
        value=plan_value();value['contract']['expected']['unexpected']='x'*70000
        value['id']=fingerprint({k:v for k,v in value.items() if k!='id'})
        with self.assertRaises(DeployError):start._validate(value)

    def test_completed_receipt_is_strict_and_never_a_live_ready_check(self):
        value=plan_value();result={'plan_id':value['id'],'state':'aborted','start_sent_again':False,'admission_opened':False}
        start._receipt(value,result)
        for change in [{'start_sent_again':True},{'admission_opened':True},{'unexpected':'private'},{'state':'ready'}]:
            with self.subTest(change=change),self.assertRaises(DeployError):start._receipt(value,{**result,**change})

    def test_claim_is_exclusive_and_durable_before_business_exec(self):
        directory=self.root/start.CLAIMS;directory.mkdir(mode=0o700)
        gate={'plan_id':'f'*64,'nonce':'a'*32,'release_id':'b'*64}
        with patch.dict(os.environ,{'INVOCATION_ID':'c'*32}):
            start._claim(self.root,gate)
            path=directory/('a'*32+'.json');saved=json.loads(path.read_text())
            self.assertEqual(saved['invocation_id'],'c'*32);self.assertEqual(saved['process']['pid'],os.getpid())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaisesRegex(DeployError,'already consumed'):start._claim(self.root,gate)

    def test_claim_directory_links_refuse(self):
        other=self.root/'other';other.mkdir(mode=0o700);(self.root/start.CLAIMS).symlink_to(other,target_is_directory=True)
        with patch.dict(os.environ,{'INVOCATION_ID':'c'*32}),self.assertRaises(DeployError):start._claim(self.root,{'nonce':'a'*32})

    def test_pending_gate_or_journal_holds_all_installed_admission(self):
        prefix=self.root/'prefix';prefix.mkdir();meta={'state_dir':str(self.root)}
        with patch.dict(os.environ,{start.PREFIX_ENV:str(prefix)}),patch('unit_start.deploy.load_installation',return_value=(prefix,meta,None)),patch('unit_start.files._directory'):
            self.assertIsNone(start.admission_reason(self.root))
            for name in [start.GATE,start.JOURNAL]:
                (prefix/name).touch();self.assertEqual(start.admission_reason(self.root),start.HOLD);(prefix/name).unlink()
            (prefix/start.GATE).symlink_to(prefix/'missing');self.assertEqual(start.admission_reason(self.root),start.HOLD)
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(start.admission_reason(self.root))

    def test_submit_local_dispatch_and_node_claim_all_obey_same_hold(self):
        jobs=object.__new__(JobStore);jobs.runtime=self.root
        nodes=object.__new__(NodeRegistry);nodes.jobs=jobs
        with patch('unit_start.admission_reason',return_value=start.HOLD):
            with self.assertRaises(JobError) as error:jobs.submit({})
            self.assertEqual(error.exception.status,503)
            self.assertEqual(jobs.dispatch_permission({},[]),start.HOLD)
            self.assertEqual(nodes.claim('worker-'+'a'*12),{'grant':None,'reason':start.HOLD})

    def test_known_implicit_mounts_allowed_but_foreign_and_missing_explicit_refuse(self):
        meta=offline.meta();value=fixture.properties('/opt/ascend-unit-fixture',meta,'/etc/systemd/system')
        wanted=manager.expected('/opt/ascend-unit-fixture',meta,deploy.unit_text('/opt/ascend-unit-fixture',meta))
        value['RequiresMountsFor'] += [meta['state_dir'],'/tmp','/var/tmp']
        manager.effective(value,wanted,value['Id'],value['FragmentPath'])
        for paths in [['/tmp'],[*value['RequiresMountsFor'],'/foreign']]:
            with self.subTest(paths=paths),self.assertRaises(DeployError):manager.effective({**value,'RequiresMountsFor':paths},wanted,value['Id'],value['FragmentPath'])

    def test_node_ready_requires_current_nonce_identity_and_fresh_ack(self):
        value=plan_value();value['contract']['unit_name']='ascend-lab-node.service'
        meta={'uid':os.geteuid(),'role':'node','release_id':'b'*64};process=identity(os.getpid());invocation='c'*32
        (self.root/start.CLAIMS).mkdir(mode=0o700)
        atomic_json(start._claim_path(self.root,value['nonce']),{'plan_id':value['id'],'nonce':value['nonce'],'release_id':meta['release_id'],'process':process,'invocation_id':invocation})
        atomic_json(self.root/'config.json',{'node_id':'worker-'+'a'*12})
        ack={**process,'service_start_id':value['nonce'],'acknowledged_at':time.time(),'node_id':'worker-'+'a'*12}
        current={'InvocationID':list(bytes.fromhex(invocation))};bus=SimpleNamespace(unit=lambda name:current,anchor=lambda:value['manager'])
        with patch('unit_start.deploy.process_status',return_value={'running':True,'ready':True,'pid':os.getpid()}),patch('unit_start.manager.effective'),patch('unit_start.manager.check'):
            atomic_json(self.root/'heartbeat-receipt.json',ack);self.assertIsNotNone(start._ready('/prefix',meta,self.root,value,bus))
            for field,bad in [('acknowledged_at',time.time()-40),('node_id','worker-'+'b'*12),('pid',os.getpid()+1)]:
                atomic_json(self.root/'heartbeat-receipt.json',{**ack,field:bad});self.assertIsNone(start._ready('/prefix',meta,self.root,value,bus))
            atomic_json(self.root/'heartbeat-receipt.json',{**ack,'service_start_id':'d'*32})
            with self.assertRaises(DeployError):start._ready('/prefix',meta,self.root,value,bus)

    def test_startup_journal_blocks_other_recovery_flags(self):
        prefix=self.root/'prefix';prefix.mkdir();release=self.root/'release';release.mkdir();meta=offline.meta();meta['state_dir']=str(self.root);meta.pop('payload_storage')
        atomic_json(self.root/'config.json',{});manifest={'id':meta['release_id'],'state_schema':1,'files':{}}
        with patch('deployment.load_installation',return_value=(prefix,meta,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
            for filename in [start.JOURNAL,start.GATE]:
                (prefix/filename).touch()
                for flags in [{},{'manager_recovery':True},{'unit_recovery':True},{'quota_recovery':True}]:
                    with self.subTest(file=filename,flags=flags),self.assertRaisesRegex(DeployError,'pending unit startup'):deploy.verify(prefix,**flags)
                deploy.verify(prefix,startup_recovery=True);(prefix/filename).unlink()

    def test_controller_requires_fresh_reconciled_inventory_from_this_instance(self):
        value=plan_value();meta={'uid':os.geteuid(),'role':'controller','release_id':'b'*64}
        node='worker-'+'a'*12;profile={'fixture':'immutable'};value['required_nodes']=[{'id':node,'profile_sha256':fingerprint(profile)}]
        process=identity(os.getpid());invocation='c'*32;(self.root/start.CLAIMS).mkdir(mode=0o700)
        atomic_json(start._claim_path(self.root,value['nonce']),{'plan_id':value['id'],'nonce':value['nonce'],'release_id':meta['release_id'],'process':process,'invocation_id':invocation})
        atomic_json(self.root/'server-ready.json',{'service_start_id':value['nonce'],'controller_instance':'d'*32})
        with sqlite3.connect(self.root/'jobs.sqlite3') as db:
            db.executescript('CREATE TABLE worker_nodes(id TEXT,profile TEXT,disabled INT,last_seen REAL,report TEXT); CREATE TABLE worker_inventory_holds(held INT);')
            db.execute('INSERT INTO worker_nodes VALUES(?,?,0,0,?)',(node,json.dumps(profile),'{}'))
        bus=SimpleNamespace(unit=lambda name:{'InvocationID':list(bytes.fromhex(invocation))},anchor=lambda:value['manager'])
        with patch('unit_start.deploy.process_status',return_value={'running':True,'ready':True,'pid':os.getpid()}),patch('unit_start.manager.effective'),patch('unit_start.manager.check'):
            self.assertIsNone(start._ready('/prefix',meta,self.root,value,bus))
            for fresh,reconciled,instance in [(False,True,'d'*32),(True,False,'d'*32),(True,True,'e'*32),(True,True,'d'*32)]:
                with sqlite3.connect(self.root/'jobs.sqlite3') as db:
                    db.execute('UPDATE worker_nodes SET last_seen=?,report=?',(time.time() if fresh else time.time()-40,json.dumps({'attempt_inventory':{'reconciled':reconciled,'controller_instance':instance}})))
                result=start._ready('/prefix',meta,self.root,value,bus)
                self.assertEqual(result is not None,fresh and reconciled and instance=='d'*32)
            with sqlite3.connect(self.root/'jobs.sqlite3') as db:db.execute('INSERT INTO worker_inventory_holds VALUES(1)')
            self.assertIsNone(start._ready('/prefix',meta,self.root,value,bus))


if __name__=='__main__':unittest.main()

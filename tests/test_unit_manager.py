"""Typed manager/source contracts and failure recovery; Root I/O tested separately."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import deployment as deploy
from deployment import DeployError
from lab_client import atomic_json
from payload_quota_admin import fingerprint
import unit_manager as unit
import test_unit_refresh as offline
import unit_manager_root_fixture as fixture


def desired():
    meta=offline.meta();prefix='/opt/ascend-unit-fixture';directory='/etc/systemd/system'
    return fixture.properties(prefix,meta,directory),unit.expected(prefix,meta,deploy.unit_text(prefix,meta))


class UnitManagerTests(unittest.TestCase):
    def test_fixed_wire_fixture_matches_generated_contract(self):
        value,wanted=desired()
        self.assertEqual(unit.effective(value,wanted,value['Id'],value['FragmentPath']),fingerprint(wanted))

    def test_foreign_alias_generator_dropin_and_stale_manager_refuse(self):
        value,wanted=desired()
        for key,bad in [('Names',[value['Id'],'alias.service']),('FragmentPath','/run/other.service'),('SourcePath','/generated'),
                        ('DropInPaths',['/etc/override.conf']),('Transient',True),('NeedDaemonReload',True),('LoadState','masked')]:
            with self.subTest(key=key),self.assertRaises(DeployError):unit.effective({**value,key:bad},wanted,value['Id'],value['FragmentPath'])

    def test_loaded_execution_identity_environment_and_sandbox_cannot_drift(self):
        value,wanted=desired()
        for key,bad in [('User','0'),('Group','0'),('SupplementaryGroups',['23831']),('EnvironmentFiles',[['/tmp/env',False]]),
                        ('PassEnvironment',['TOKEN']),('Environment',['HOME=/tmp']),('ExecStartPre',[value['ExecStart'][0]]),
                        ('ReadWritePaths',['/']),('NoNewPrivileges',False),('CapabilityBoundingSet',1),('UMask',0),
                        ('RootDirectory','/tmp/root'),('PrivateTmp',False),('DynamicUser',True),('Restart','always'),
                        ('RestrictAddressFamilies',[False,['AF_UNIX','AF_INET','AF_INET6']])]:
            with self.subTest(key=key),self.assertRaises(DeployError):unit.effective({**value,key:bad},wanted,value['Id'],value['FragmentPath'])

    def test_protocol_boolean_integer_and_truncated_properties_refuse(self):
        bus=unit.Bus()
        for schema,result in [({'x':'u'},[{'type':'u','data':True}]),({'x':'u'},[{'type':'u','data':2**32}]),
                              ({'x':'b'},[{'type':'b','data':1}]),({'x':'s'},[{'type':'as','data':[]}]),
                              ({'x':'s'},[]),({'x':'s'},[[]])]:
            with self.subTest(result=result),patch.object(bus,'invoke',return_value=result),self.assertRaises(DeployError):bus.properties('/x','x',schema)
        for malformed in [[[]],[{'type':'s','data':'scalar'}],[{'type':'o','data':['/one','/two']}]]:
            with patch.object(bus,'invoke',return_value=malformed),self.assertRaises(DeployError):bus.call('x','/x','x','x',reply='s')

    def test_nested_wire_types_do_not_accept_boolean_integer_coercion(self):
        value,wanted=desired()
        for key,bad in [('Job',[False,'/']),('EnvironmentFiles',[['/env',0]]),('InvocationID',[256]),
                        ('ExecStart',[[*value['ExecStart'][0][:2],0,*value['ExecStart'][0][3:]]]),
                        ('ExecStart',[[*value['ExecStart'][0][:3],True,*value['ExecStart'][0][4:]]])]:
            with self.subTest(key=key),self.assertRaises(DeployError):unit.effective({**value,key:bad},wanted,value['Id'],value['FragmentPath'])

    def test_method_decoding_and_no_interactive_or_inherited_bus_address(self):
        bus=unit.Bus()
        with patch('unit_manager._bounded',return_value=[]) as run:
            bus.reload();argv=run.call_args.args[0]
            self.assertIn('--system',argv);self.assertIn('--auto-start=no',argv);self.assertIn('--allow-interactive-authorization=no',argv)
            self.assertEqual(argv[-1],'Reload')
        with patch.object(bus,'invoke',return_value=[{'type':'o','data':['/org/freedesktop/systemd1/unit/a']}]) as invoke:
            bus.load('a.service');self.assertIn('LoadUnit',invoke.call_args.args[0])
        self.assertFalse(hasattr(bus,'start'));self.assertFalse(hasattr(bus,'restart'))

    def test_real_subprocess_output_limit_and_environment_boundary(self):
        for fd in [1,2]:
            command=['/usr/bin/python3','-I','-c','import os;os.write('+str(fd)+',b"x"*300000)']
            with self.subTest(fd=fd),self.assertRaisesRegex(DeployError,'bounded'):unit._bounded(command)
        with patch.dict(os.environ,{'DBUS_SYSTEM_BUS_ADDRESS':'unix:path=/tmp/foreign-bus','SHOULD_NOT_LEAK':'private'}):
            result=unit._bounded(['/usr/bin/python3','-I','-c','import os,json;print(json.dumps({"present":any(x in os.environ for x in ["DBUS_SYSTEM_BUS_ADDRESS","SHOULD_NOT_LEAK"])}))'])
        self.assertEqual(result,[{'present':False}])

    def test_plan_structure_and_nonce_are_bound_before_any_privileged_call(self):
        old=offline.plan();contract={k:v for k,v in old.items() if k not in {'format','id','desired'}}
        _,wanted=desired();contract['expected']=wanted
        value={'format':unit.FORMAT,'contract':contract,'manager':fixture.anchor(),'nonce':'a'*32};value['id']=fingerprint(value)
        unit._plan_check(value)
        for key,bad in [('nonce',None),('contract',{}),('manager',{}),('id','f'*64)]:
            changed=copy.deepcopy(value);changed[key]=bad
            if key!='id':changed['id']=fingerprint({k:v for k,v in changed.items() if k!='id'})
            with self.subTest(key=key),self.assertRaises(DeployError):unit._plan_check(changed)
        bad=copy.deepcopy(value);bad['contract']['unit_dir']='relative';bad['id']=fingerprint({k:v for k,v in bad.items() if k!='id'})
        with self.assertRaises(DeployError):unit._plan_check(bad)

    def test_inactive_requires_no_job_pid_or_transition(self):
        value,_=desired();unit.inactive(value);unit.inactive(None)
        for key,bad in [('MainPID',False),('MainPID',1),('ControlPID',2),('Job',[1,'/job']),('ActiveState','activating'),('SubState','failed')]:
            with self.subTest(key=key),self.assertRaises(DeployError):unit.inactive({**value,key:bad})

    def test_non_root_plan_apply_reject_before_contact(self):
        with patch('unit_manager.deploy.require_root',side_effect=DeployError('Root required')),patch.object(unit.Bus,'invoke') as invoke:
            with self.assertRaises(DeployError):unit.plan('/missing')
            with self.assertRaises(DeployError):unit.apply('/missing',{})
            invoke.assert_not_called()

    def test_startup_invocation_is_checked_only_for_explicit_systemd_launch(self):
        with patch.dict(os.environ,{},clear=True),patch('unit_manager.check') as check:unit.service_entry('/x');check.assert_not_called()
        with patch.dict(os.environ,{'INVOCATION_ID':'invalid'}),self.assertRaises(DeployError):unit.service_entry('/x')
        with patch.dict(os.environ,{'INVOCATION_ID':'a'*32}),patch('unit_manager.check',side_effect=DeployError('not manager MainPID')) as check:
            with self.assertRaises(DeployError):unit.service_entry('/x')
            check.assert_called_once_with('/x',running=True,entry=True)

    def test_entry_requires_real_pid_identity_cgroup_and_invocation_together(self):
        meta={**offline.meta(),'uid':os.geteuid(),'gid':os.getegid()};meta.pop('payload_storage')
        prefix=Path('/opt/ascend-unit-fixture');directory='/etc/systemd/system'
        value=fixture.properties(prefix,meta,directory)
        value.update(MainPID=os.getpid(),ActiveState='active',SubState='running',InvocationID=[170]*16,
                     ControlGroup=Path('/proc/self/cgroup').read_text().splitlines()[0].split(':',2)[-1])
        contract={'unit_name':value['Id'],'expected':unit.expected(prefix,meta,deploy.unit_text(prefix,meta))}
        bus=SimpleNamespace(anchor=lambda:fixture.anchor(),unit=lambda name:copy.deepcopy(value))
        with patch('unit_manager._context',return_value=(prefix,meta,ROOT/'runtime',{})),patch('unit_manager._contract',return_value=contract),patch.dict(os.environ,{'INVOCATION_ID':'a'*32}):
            self.assertTrue(unit.check(prefix,directory,entry=True,bus=bus)['running_verified'])
            for key,bad in [('MainPID',os.getpid()+1),('InvocationID',[187]*16),('ControlGroup','/another-unit'),('Job',[1,'/job'])]:
                old=value[key];value[key]=bad
                try:
                    with self.subTest(key=key),self.assertRaises(DeployError):unit.check(prefix,directory,entry=True,bus=bus)
                finally:value[key]=old

    def test_pending_manager_guard_cannot_be_bypassed_by_other_recovery_flags(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-manager-') as name:
            root=Path(name);state=root/'state';prefix=root/'prefix';release=root/'release'
            for p in [state,prefix,release]:p.mkdir()
            meta=offline.meta();meta['state_dir']=str(state);meta.pop('payload_storage');atomic_json(state/'config.json',{})
            (prefix/unit.JOURNAL).touch();manifest={'id':meta['release_id'],'state_schema':1,'files':{}}
            with patch('deployment.load_installation',return_value=(prefix,meta,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
                for flags in [{},{'unit_recovery':True},{'quota_recovery':True}]:
                    with self.subTest(flags=flags),self.assertRaisesRegex(DeployError,'pending manager'):deploy.verify(prefix,**flags)
                self.assertEqual(deploy.verify(prefix,manager_recovery=True)[1],meta)
                (prefix/'unit-refresh-change.json').touch()
                with self.assertRaisesRegex(DeployError,'pending unit refresh'):deploy.verify(prefix,manager_recovery=True)


if __name__=='__main__':unittest.main()

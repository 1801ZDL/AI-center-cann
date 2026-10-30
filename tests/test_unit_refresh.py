"""Managed unit recognition and CAS/override/guard contracts; Root I/O separate."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import deployment as deploy
from deployment import DeployError
from lab_client import atomic_json
import unit_refresh as unit
from payload_quota_admin import fingerprint


def meta():
    return {'role':'controller','uid':23820,'gid':23820,'state_dir':'/var/lib/ascend-unit-fixture',
        'release_id':'a'*64,'state_schema':1,'payload_storage':{'path':'/srv/payload','bytes':128*1024**2,'inodes':4096,
        'owner_groups':{'old':{'gid':23831,'bytes':16*1024**2,'inodes':128},'new':{'gid':23832,'bytes':32*1024**2,'inodes':256}}}}


def plan():
    m=meta();desired=deploy.unit_text('/opt/ascend-unit-fixture',m)
    old=desired.replace('SupplementaryGroups=23831 23832\n','SupplementaryGroups=23831\n')
    snapshot={'device':4,'inode':5,'size':len(old.encode()),'mtime_ns':1,'ctime_ns':2,'mode':0o644,'uid':0,'gid':0,'text':old,'sha256':unit._sha(old.encode())}
    value={'format':unit.FORMAT,'binding':{'prefix':'/opt/ascend-unit-fixture','state_dir':m['state_dir'],
        'control_identity':{'device':1,'inode':2},'role':'controller','release_id':'a'*64,'installation_sha256':'b'*64,'configuration_sha256':'c'*64},
        'unit_dir':'/etc/systemd/system','unit_directory_identity':{'device':1,'inode':3},'template_directory_identity':{'device':1,'inode':4},
        'unit_name':'ascend-lab-controller.service','installed':snapshot,'template':{**snapshot,'mode':0o444},'desired':desired}
    value['id']=fingerprint(value);return value


class UnitRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-unit-refresh-');self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_current_and_old_storage_group_subset_are_recognized(self):
        m=meta();desired=deploy.unit_text('/opt/ascend-unit-fixture',m)
        for text in [desired,desired.replace('SupplementaryGroups=23831 23832\n','SupplementaryGroups=23831\n'),
                     desired.replace('SupplementaryGroups=23831 23832\n','')]:unit._managed(text,'/opt/ascend-unit-fixture',m,desired)

    def test_custom_execution_security_and_unknown_groups_are_not_adopted(self):
        m=meta();desired=deploy.unit_text('/opt/ascend-unit-fixture',m)
        for text in [desired+'# custom\n',desired.replace('NoNewPrivileges=true','NoNewPrivileges=false'),
                     desired.replace('SupplementaryGroups=23831 23832','SupplementaryGroups=0 23831'),
                     desired.replace('SupplementaryGroups=23831 23832','SupplementaryGroups=23832 23831'),
                     desired.replace('SupplementaryGroups=23831 23832','SupplementaryGroups=23831\nSupplementaryGroups=23832'),
                     desired.replace('/opt/ascend-unit-fixture/current','/opt/other/current')]:
            with self.subTest(text=text[-40:]),self.assertRaises(DeployError):unit._managed(text,'/opt/ascend-unit-fixture',m,desired)

    def test_node_unit_cannot_acquire_supplementary_owner_storage_groups(self):
        m={**meta(),'role':'node','uid':0,'gid':0};desired=deploy.unit_text('/opt/ascend-unit-fixture',m)
        with self.assertRaises(DeployError):unit._managed(desired.replace('WorkingDirectory=','SupplementaryGroups=23831\nWorkingDirectory='),'x',m,desired)

    def test_direct_prefix_and_type_wide_dropins_all_refuse(self):
        for name in ['ascend-lab-controller.service.d','ascend-.service.d','ascend-lab-.service.d','service.d']:
            path=self.root/name;path.mkdir()
            with self.subTest(name=name),self.assertRaisesRegex(DeployError,'drop-ins'):unit._overrides(self.root,'ascend-lab-controller.service')
            path.rmdir()
        link=self.root/'service.d';link.symlink_to(self.root/'missing')
        with self.assertRaises(DeployError):unit._overrides(self.root,'ascend-lab-controller.service')

    def test_plan_digest_role_and_snapshot_integrity_are_required(self):
        value=plan();unit._plan_check(value)
        for change in [('unit_name','unrelated.service'),('binding',None),('desired',True),('unit_directory_identity',{'device':True,'inode':2})]:
            v=copy.deepcopy(value);v[change[0]]=change[1];v['id']=fingerprint({k:x for k,x in v.items() if k!='id'})
            with self.subTest(change=change),self.assertRaises(DeployError):unit._plan_check(v)
        v=copy.deepcopy(value);v['installed']['text']+='changed';v['id']=fingerprint({k:x for k,x in v.items() if k!='id'})
        with self.assertRaises(DeployError):unit._plan_check(v)
        with self.assertRaises(DeployError):unit._plan_check({**value,'desired':'tampered'})

    def test_before_file_identity_is_required_even_when_original_bytes_match(self):
        value=plan();before=value['installed'];new={**before,'inode':before['inode']+1}
        with patch('unit_refresh._file',return_value=new),self.assertRaisesRegex(DeployError,'changed since plan'):
            unit._original_or_desired(self.root/'unit',before,value['desired'],0o644)
        with patch('unit_refresh._file',return_value={**new,'text':value['desired']}):
            unit._original_or_desired(self.root/'unit',before,value['desired'],0o644)

    def test_plan_paths_and_original_modes_cannot_be_reinterpreted(self):
        for field,bad in [('unit_dir',True),('unit_dir','relative/path'),('unit_dir','/etc/../tmp')]:
            value=plan();value[field]=bad;value['id']=fingerprint({k:v for k,v in value.items() if k!='id'})
            with self.subTest(bad=bad),self.assertRaises(DeployError):unit._plan_check(value)
        value=plan();value['template']['mode']=0o777;value['id']=fingerprint({k:v for k,v in value.items() if k!='id'})
        with self.assertRaises(DeployError):unit._plan_check(value)

    def test_links_and_nonroot_operations_fail_before_mutation(self):
        path=self.root/'link';path.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):unit._file(path)
        with patch('unit_refresh.deploy.require_root',side_effect=DeployError('root required')),self.assertRaises(DeployError):unit.plan('/does-not-exist')

    def test_verify_guard_is_independent_of_quota_recovery(self):
        state=self.root/'state';state.mkdir();prefix=self.root/'prefix';prefix.mkdir();release=self.root/'release';release.mkdir()
        m=meta();m['state_dir']=str(state);m.pop('payload_storage');atomic_json(state/'config.json',{})
        atomic_json(prefix/unit.JOURNAL,{'fixture':True});manifest={'id':m['release_id'],'state_schema':1,'files':{}}
        with patch('deployment.load_installation',return_value=(prefix,m,release)),patch('deployment.regular_json',side_effect=lambda p,**k:manifest if p.name=='release.json' else {}),patch('deployment.checked_manifest'):
            for recovery in [False,True]:
                with self.assertRaisesRegex(DeployError,'pending unit refresh'):deploy.verify(prefix,quota_recovery=recovery)
            self.assertEqual(deploy.verify(prefix,unit_recovery=True)[1],m)
            (prefix/'payload-owner-change.json').touch()
            with self.assertRaisesRegex(DeployError,'pending owner storage'):deploy.verify(prefix,unit_recovery=True)

    def test_new_generated_group_unit_passes_local_systemd_analyzer(self):
        self.assertIsNotNone(shutil.which('systemd-analyze'),'Acceptance requires the locally available systemd verifier')
        file=self.root/'ascend-lab-controller.service';file.write_text(deploy.unit_text('/opt/ascend-unit-fixture',meta()))
        result=subprocess.run(['systemd-analyze','verify',str(file)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr[-1000:])


if __name__=='__main__':unittest.main()

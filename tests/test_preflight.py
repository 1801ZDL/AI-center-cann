"""Read-only planning, redaction, archive parsing and no-mutation install rejection."""
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deployment
from deployment import ROOT,bundle,inspect_release,unpack_release,DeployError
from preflight import plan,controller_identity,controller_path_access,paths
from jobs import atomic_json
from backends import physical_identity


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-preflight-');self.root=Path(self.tmp.name)
        self.archive=self.root/'server.tar.gz';bundle(self.archive)

    def tearDown(self):self.tmp.cleanup()

    def test_new_controller_defaults_require_headroom_without_mutating_state(self):
        from control_storage import DEFAULT
        from types import SimpleNamespace
        before={p.relative_to(self.root) for p in self.root.rglob('*')}
        state=self.root/'new-state';prefix=self.root/'new-prefix'
        with patch('preflight.paths',return_value={'prefix':str(prefix),'state_dir':str(state)}),\
             patch('control_storage.os.fstatvfs',return_value=SimpleNamespace(f_frsize=4096,f_bavail=1,f_favail=4096,f_files=8192,f_flag=0)):
            report=plan('controller',prefix,state,self.archive,uid=24001,gid=24001)
        self.assertEqual(report['controller_defaults']['control_storage'],DEFAULT)
        self.assertIn('control_storage_admission',report['blockers'])
        self.assertFalse(prefix.exists());self.assertFalse(state.exists())
        self.assertEqual(before,{p.relative_to(self.root) for p in self.root.rglob('*')})

    def test_new_controller_cannot_install_an_archive_without_guard_support(self):
        value=inspect_release(self.archive);del value['manifest']['files']['control_storage.py']
        with patch('preflight.deploy.inspect_release',return_value=value):
            report=plan('controller','/opt/uncreated-headroom','/var/lib/uncreated-headroom',self.archive,uid=24001,gid=24001)
        self.assertIn('release_archive',report['blockers'])
        failure=next(v for v in report['checks'] if v['id']=='release_archive')
        self.assertIn('control storage admission',failure['reason'])

    def test_release_inspection_writes_nothing_and_extraction_keeps_client_executable(self):
        before={p.relative_to(self.root) for p in self.root.rglob('*')}
        report=inspect_release(self.archive)
        self.assertEqual(before,{p.relative_to(self.root) for p in self.root.rglob('*')})
        self.assertIn('preflight.py',report['manifest']['files'])
        stage,_=unpack_release(self.archive,self.root)
        self.assertEqual((stage/'install-client').stat().st_mode&0o777,0o555)
        self.assertEqual((stage/'labctl').stat().st_mode&0o777,0o555)

    def test_decompression_budget_covers_unlisted_padding_after_the_tar_end(self):
        bad=self.root/'padding.tar.gz'
        bad.write_bytes(gzip.compress(gzip.decompress(self.archive.read_bytes())+b'\0'*(21*1024**2)))
        with self.assertRaisesRegex(DeployError,'decompression budget'):inspect_release(bad)

    def test_readonly_plan_reports_root_requirement_without_creating_targets(self):
        suffix=uuid.uuid4().hex[:12];prefix=Path('/opt/ascend-lab-test-'+suffix);state=Path('/var/lib/ascend-lab-test-'+suffix)
        with patch('preflight.os.geteuid',return_value=1104):
            report=plan('controller',prefix,state,self.archive,create_user='lab_'+suffix)
        self.assertFalse(report['install_ready']);self.assertIn('administrator',report['blockers'])
        self.assertFalse(prefix.exists());self.assertFalse(state.exists())
        self.assertTrue(report['read_only']);self.assertFalse(report['production_ready'])

    def test_failed_preflight_cannot_call_mutating_install_helpers(self):
        with patch('deployment.require_root'),patch('preflight.plan',return_value={'install_ready':False,'blockers':['node_config']}),\
             patch('deployment.secure_parent') as parent,patch('deployment.subprocess.run') as command:
            with self.assertRaisesRegex(DeployError,'preflight blocked'):
                deployment.install(self.archive,'/opt/unused','/var/lib/unused','node')
        parent.assert_not_called();command.assert_not_called()

    def test_relative_and_overlapping_installation_paths_are_rejected_without_creation(self):
        with self.assertRaises(DeployError):paths('relative-prefix','/var/lib/unused')
        with self.assertRaises(DeployError):paths('/opt/ascend-lab-test-overlap','/opt/ascend-lab-test-overlap/state')

    def test_controller_cannot_use_root_supplementary_group(self):
        with patch('preflight.pwd.getpwuid') as user,patch('preflight.os.getgrouplist',return_value=[999,0]):
            user.return_value.pw_name='synthetic-service'
            with self.assertRaises(ValueError):controller_identity(999,999,None)

    def test_controller_cannot_depend_on_root_only_ancestry(self):
        with self.assertRaisesRegex(DeployError,'cannot traverse'):
            controller_path_access('/root/ascend-lab-code','/var/lib/ascend-lab-state',999,999,None)

    def test_existing_group_is_not_mistaken_for_an_available_new_service_identity(self):
        with patch('preflight.pwd.getpwnam',side_effect=KeyError),patch('preflight.grp.getgrnam'):
            with self.assertRaisesRegex(DeployError,'group already exists'):controller_identity(None,None,'labsvc')

    def test_node_report_redacts_credentials_and_separates_optional_workspace(self):
        secret='test-only-node-credential-'+uuid.uuid4().hex
        config={'node_id':'worker-'+'3'*12,'node_key':secret,'controller':'https://lab.example.invalid',
          'physical_id':physical_identity(),'image':'sha256:'+'4'*64,'users':{'alice':{'uid':23001,'gid':23001}},
          'policy':{'cpu_ids':[min(os.sched_getaffinity(0))],'memory_mib':768,'shm_mib':0,'max_running':1}}
        path=self.root/'node.json';atomic_json(path,config)
        binding={'endpoint':'unix:///var/run/docker.sock','daemon_id':'test-daemon'}
        info={key:True for key in ['MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','CPUSet','PidsLimit']};info.update(MemTotal=8*1024**3,CgroupVersion='2')
        fake=subprocess.CompletedProcess([],0,config['image']+'\n','')
        with patch('preflight.os.geteuid',return_value=0),patch('preflight.paths',return_value={'prefix':'/opt/example','state_dir':'/var/lib/example'}),\
             patch('preflight.discover',return_value=(binding,info)),patch('preflight.command',return_value=(['docker','image','inspect'],{})),\
             patch('subprocess.run',return_value=fake),patch('lab_client.Client.request') as request:
            report=plan('node','/opt/example','/var/lib/example',self.archive,node_config=path)
        self.assertTrue(report['install_ready']);self.assertFalse(report['task_dispatch_prerequisites'])
        self.assertNotIn(secret,json.dumps(report));self.assertNotIn('node_key',json.dumps(report))
        self.assertEqual(report['acceptance']['npu_initialization'],'not_run');request.assert_not_called()

    def test_doctor_cli_reports_json_and_exit_two_without_root_installation(self):
        args=[str(ROOT/'servicectl'),'doctor','--role','controller','--uid','24001','--gid','24001',
              '--prefix','/opt/ascend-lab-test-'+uuid.uuid4().hex,'--state-dir','/var/lib/ascend-lab-test-'+uuid.uuid4().hex,
              '--archive',str(self.archive)]
        result=subprocess.run(args,capture_output=True,text=True,timeout=10)
        if os.geteuid()==0:self.skipTest('Unprivileged CLI gate is tested from a non-root shell')
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertIn('administrator',json.loads(result.stdout)['blockers'])

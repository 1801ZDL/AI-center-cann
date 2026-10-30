"""Offline maintenance exclusion, archive integrity and deployment rejection boundaries."""
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from deployment import ROOT,DeployError,bundle,unpack_release,regular_json,safe_relative,unit_text
from deployment import runtime_capabilities
from maintenance import assert_quiescent,relocate
from state_lock import state_lock
from auth import AccessControl
from jobs import JobStore,atomic_json
from jobs import identity
import deployment


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-deployment-');self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_package_contains_only_explicit_code_and_roundtrips_with_checksums(self):
        path=self.root/'release.tar.gz';report=bundle(path)
        with tarfile.open(path) as archive:
            names={item.name for item in archive}
            self.assertIn('servicectl',names);self.assertIn('deployment.py',names)
            self.assertNotIn('agent_runner.py',names)
            self.assertFalse(any('/runtime/' in '/'+name or name.endswith('client.json') for name in names))
        stage,manifest=unpack_release(path,self.root)
        self.assertEqual(manifest['id'],report['release_id'])
        self.assertEqual((stage/'worker.py').read_bytes(),(ROOT/'worker.py').read_bytes())
        with self.assertRaises(DeployError):bundle(path)

    def test_archive_corruption_or_link_member_cannot_be_installed(self):
        original=self.root/'original.tar.gz';bundle(original)
        for attack in ['changed-content','symlink']:
            bad=self.root/(attack+'.tar.gz')
            with tarfile.open(original) as source,tarfile.open(bad,'w:gz') as target:
                for item in source:
                    data=source.extractfile(item).read()
                    if item.name=='server.py':
                        if attack=='symlink':item.type=tarfile.SYMTYPE;item.linkname='/etc/shadow';item.size=0;data=b''
                        else:data=b'x'+data[1:]
                    target.addfile(item,io.BytesIO(data))
            with self.subTest(attack=attack),self.assertRaises(DeployError):unpack_release(bad,self.root)

    def test_private_configuration_and_portable_paths_reject_unsafe_inputs(self):
        path=self.root/'config.json';atomic_json(path,{'node_key':'offline-test-only'})
        self.assertEqual(regular_json(path,private=True)['node_key'],'offline-test-only')
        path.chmod(0o644)
        with self.assertRaises(DeployError):regular_json(path,private=True)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(DeployError):regular_json(link)
        for name in ['../outside','/absolute','a/../b','a\\b','a//b','a\nname','./a']:
            self.assertFalse(safe_relative(name),name)

    def test_state_lock_excludes_live_auth_and_offline_maintenance_both_ways(self):
        state=self.root/'state';state.mkdir()
        access=AccessControl(state,{'username':'admin','password':'offline-only'})
        with self.assertRaises(RuntimeError):state_lock(state,exclusive=True)
        access.state_lock.close()
        with state_lock(state,exclusive=True):
            with self.assertRaises(RuntimeError):AccessControl(state,{'username':'admin','password':'offline-only'})
        with state_lock(state):pass

    def test_quiescence_rejects_queued_jobs_and_unknown_node_executions(self):
        state=self.root/'controller';state.mkdir()
        jobs=JobStore(state,{'username':'operator','password':'offline-only','container':'zdl'})
        request={'cwd':str(ROOT),'argv':['python','-c','pass']}
        job,_=jobs.submit(request)
        with self.assertRaises(DeployError):assert_quiescent(state,'controller')
        jobs.cancel(job['id']);assert_quiescent(state,'controller')
        node=self.root/'node';control=node/'attempts/attempt-offline/control';control.mkdir(parents=True)
        atomic_json(control/'grant.json',{'offline':True})
        with self.assertRaises(DeployError):assert_quiescent(node,'node')
        atomic_json(control/'state.json',{'stage':'cleaned','controller_acknowledged':True})
        assert_quiescent(node,'node')

    def test_restore_relocation_updates_workspace_and_archive_without_changing_identity(self):
        stage=self.root/'stage';stage.mkdir();old='/var/lib/old-lab';new='/var/lib/new-lab'
        atomic_json(stage/'config.json',{'monitor_local':False,'identity_workspace_root':old+'/users',
                    'scheduler':{'local_enabled':False,'workspace_root':old+'/users'}})
        with sqlite3.connect(stage/'identities.sqlite3') as db:
            db.execute('CREATE TABLE users(id TEXT,profile TEXT)');db.execute('INSERT INTO users VALUES(?,?)',('alice',json.dumps({'workspace':old+'/users/alice/workspace','role':'user'})))
        with sqlite3.connect(stage/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE jobs(id TEXT,data TEXT,state TEXT)');db.execute('INSERT INTO jobs VALUES(?,?,?)',
                ('job-offline',json.dumps({'archive_dir':old+'/jobs/job-offline','owner':'alice'}),'succeeded'))
        relocate(stage,old,new,'controller')
        self.assertEqual(json.loads((stage/'config.json').read_text())['identity_workspace_root'],new+'/users')
        with sqlite3.connect(stage/'identities.sqlite3') as db:profile=json.loads(db.execute('SELECT profile FROM users').fetchone()[0])
        self.assertEqual(profile['workspace'],new+'/users/alice/workspace');self.assertEqual(profile['role'],'user')
        with sqlite3.connect(stage/'jobs.sqlite3') as db:job=json.loads(db.execute('SELECT data FROM jobs').fetchone()[0])
        self.assertEqual(job['archive_dir'],new+'/jobs/job-offline');self.assertEqual(job['owner'],'alice')

    def test_generated_systemd_unit_is_accepted_by_local_systemd_verifier(self):
        if not shutil.which('systemd-analyze'):self.skipTest('systemd verifier unavailable')
        path=self.root/'ascend-lab-offline.service'
        path.write_text(unit_text('/opt/ascend-lab-offline',{'uid':24001,'gid':24001,'state_dir':'/var/lib/ascend-lab-offline','role':'controller'}))
        result=subprocess.run(['systemd-analyze','verify',str(path)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr[-1000:])

    def test_process_stop_uses_owned_child_identity_and_pidfd(self):
        if not hasattr(os,'pidfd_open'):self.skipTest('pidfd unavailable')
        child=subprocess.Popen(['/usr/bin/python3','-I','-c','import time;time.sleep(20)'])
        state=self.root/'pidfd';state.mkdir()
        try:
            saved=identity(child.pid);atomic_json(state/'service-process.json',saved)
            meta={'uid':os.getuid(),'gid':os.getgid(),'state_dir':str(state)}
            def status(_):return {'running':child.poll() is None,'pid':child.pid,'role':'controller','ready':False}
            with patch('deployment.verify',return_value=(self.root,meta,self.root)),patch('deployment.process_status',side_effect=status):
                stopped=deployment.stop(self.root)
            self.assertFalse(stopped['running']);self.assertEqual(child.wait(timeout=2),-15)
        finally:
            if child.poll() is None:child.terminate();child.wait(timeout=3)

    def test_changed_process_identity_is_not_signalled(self):
        if not hasattr(os,'pidfd_open'):self.skipTest('pidfd unavailable')
        child=subprocess.Popen(['/usr/bin/python3','-I','-c','import time;time.sleep(20)'])
        state=self.root/'pidfd-reused';state.mkdir()
        try:
            saved=identity(child.pid);saved['start_ticks']+=1;atomic_json(state/'service-process.json',saved)
            meta={'uid':os.getuid(),'gid':os.getgid(),'state_dir':str(state)}
            with patch('deployment.verify',return_value=(self.root,meta,self.root)),patch('deployment.process_status',return_value={'running':True,'pid':child.pid}):
                with self.assertRaisesRegex(DeployError,'identity changed'):deployment.stop(self.root)
            self.assertIsNone(child.poll())
        finally:child.terminate();child.wait(timeout=3)

    def test_node_install_rejects_runtimes_missing_any_promised_hard_limit(self):
        supported={name:True for name in ['MemoryLimit','SwapLimit','CpuCfsPeriod','CpuCfsQuota','CPUSet','PidsLimit']}
        self.assertEqual(runtime_capabilities(supported),supported)
        for name in supported:
            with self.subTest(capability=name),self.assertRaises(DeployError):runtime_capabilities({**supported,name:False})


if __name__=='__main__':unittest.main()

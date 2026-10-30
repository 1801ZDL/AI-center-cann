"""Fixed recipe preparation, qualified-source boundaries and offline image context."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from operator_profile import PROVIDER_FILES
from operator_workflow import APP_FILES,PACKAGES,PREFIX,prepare,probe_queries,source_locks
from templates import expand_template
from environment_contract import probe_parameters

spec=importlib.util.spec_from_file_location('operator_image_context',ROOT/'scripts/prepare_operator_image.py')
image_context=importlib.util.module_from_spec(spec);spec.loader.exec_module(image_context)


def lock():
    return {'python':'3.11.15','machine':'x86_64','image_sha256':'sha256:'+'a'*64,'packages':dict(PACKAGES),
        'files':{**source_locks(),**{PREFIX+'/venv/lib/python3.11/site-packages/'+n:v for n,v in PROVIDER_FILES.items()}}}


class OperatorWorkflowTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.directory=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_recipe_passes_existing_server_contract_and_acquires_only_cpu(self):
        value=prepare('input-'+'b'*24,lock(),idempotency_key='operator-test-1');expanded=expand_template(value)
        self.assertEqual(expanded['kind'],'build');self.assertEqual(expanded['npu'],0)
        self.assertEqual(expanded['parameters']['input_mode'],'readonly');self.assertEqual(expanded['parameters']['argv'],['python','-I','-B',PREFIX+'/app/operator_node.py'])
        self.assertEqual(expanded['parameters']['resources'],{'cpu':1,'memory_mib':768,'shm_mib':0,'npu':0})

    def test_missing_or_unqualified_environment_never_gets_a_request(self):
        for key,value in [('python','3.10.12'),('machine','aarch64'),('image_sha256','python:latest'),('packages',{}),('files',{})]:
            candidate=lock();candidate[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):prepare('input-'+'b'*24,candidate)

    def test_old_client_or_tool_source_hash_is_rejected(self):
        for name in lock()['files']:
            candidate=lock();candidate['files'][name]='0'*64
            with self.subTest(file=name),self.assertRaises(ValueError):prepare('input-'+'b'*24,candidate)

    def test_package_versions_are_exact_and_request_does_not_alias_its_lock(self):
        candidate=lock();value=prepare('input-'+'b'*24,candidate);candidate['packages']['mcp']='2.3.0'
        self.assertEqual(value['parameters']['lock']['packages']['mcp'],'1.30.0')
        with self.assertRaises(ValueError):prepare('input-'+'b'*24,candidate)

    def test_probe_is_a_valid_fixed_cpu_query_without_device_access(self):
        result=probe_parameters(probe_queries('worker-'+'a'*12))
        self.assertEqual(result['memory_mib'],256);self.assertEqual(len(result['files']),6)
        for node in ['unknown','worker-'+'a'*11,None]:
            with self.assertRaises(ValueError):probe_queries(node)

    def test_invalid_identity_or_ambiguous_key_is_rejected(self):
        for identifier in ['input-x','../input-'+'a'*24,None]:
            with self.assertRaises(ValueError):prepare(identifier,lock())
        for key in ['bad\nkey','x'*129,True]:
            with self.assertRaises(ValueError):prepare('input-'+'b'*24,lock(),idempotency_key=key)

    def test_installed_preparation_requires_no_controller_and_preserves_existing_output(self):
        installed=self.directory/'client'
        subprocess.run([sys.executable,str(ROOT/'install-client'),'--prefix',str(installed)],check=True,capture_output=True)
        self.assertFalse((installed/'server.py').exists());self.assertIn('operator-recipe-v1',json.loads((installed/'manifest.json').read_text())['capabilities'])
        path=self.directory/'lock.json';path.write_text(json.dumps(lock()));output=self.directory/'request.json'
        argv=[sys.executable,str(installed/'operator_workflow.py'),'--input-id','input-'+'b'*24,'--lock',str(path),'--output',str(output)]
        result=subprocess.run(argv,check=True,capture_output=True,text=True);self.assertFalse(json.loads(result.stdout)['submitted'])
        raw=output.read_bytes();self.assertNotEqual(subprocess.run(argv,capture_output=True).returncode,0);self.assertEqual(output.read_bytes(),raw)
        self.assertFalse((installed/'runtime').exists())

    def test_image_entry_refuses_personal_host_execution(self):
        result=subprocess.run([sys.executable,str(ROOT/'operator_node.py')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('fixed image entry',result.stderr)


class ImageContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name);self.wheels=self.root/'wheels';self.wheels.mkdir()
        (self.root/'examples/operator_tools').mkdir(parents=True)
        for n in APP_FILES:(self.root/n).write_text('trusted source '+n)
        (self.root/'examples/operator_tools/Dockerfile').write_text('ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n')
        self.wheel=self.wheels/'trusted-1.0-py3-none-any.whl';self.wheel.write_bytes(b'fake wheel bytes for hashing only')
        self.digest=hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        (self.root/'examples/operator_tools/requirements-linux-x86_64-py311.txt').write_text('trusted==1.0 --hash=sha256:'+self.digest+'\n')

    def tearDown(self):self.temp.cleanup()

    def make(self):
        with patch.object(image_context,'ROOT',self.root):return image_context.prepare(self.wheels,self.root/'context')

    def test_exact_wheel_bytes_and_only_explicit_source_are_copied(self):
        (self.root/'private-config.json').write_text('must not copy')
        value=self.make();self.assertEqual(value['wheel_count'],1);self.assertFalse(value['credentials_read']);self.assertFalse(value['downloads_performed'])
        self.assertEqual({p.name for p in (self.root/'context/app').iterdir()},set(APP_FILES));self.assertFalse((self.root/'context/private-config.json').exists())
        with self.assertRaises(ValueError):self.make()

    def test_changed_missing_or_extra_wheels_fail_before_context_creation(self):
        for mode in ['changed','missing','extra','symlink']:
            with self.subTest(mode=mode):
                if self.wheel.exists():self.wheel.unlink()
                self.wheel.write_bytes(b'fake wheel bytes for hashing only')
                other=self.wheels/'other.whl'
                if other.exists() or other.is_symlink():other.unlink()
                if mode=='changed':self.wheel.write_bytes(b'wrong wheel')
                elif mode=='missing':self.wheel.unlink()
                elif mode=='extra':other.write_bytes(b'other wheel')
                else:other.symlink_to(self.wheel)
                with self.assertRaises(ValueError):self.make()
                self.assertFalse((self.root/'context').exists())


if __name__=='__main__':unittest.main()

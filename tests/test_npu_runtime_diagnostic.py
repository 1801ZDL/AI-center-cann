"""Hardware-free C ABI ordering, card mapping and cleanup failures."""
import ctypes
import base64
import os
from pathlib import Path
import sys
import unittest
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tests'))
import npu_runtime_initialization_check as diagnostic


class Function:
    def __init__(self,name,library):self.name=name;self.library=library
    def __call__(self,*args):
        lib=self.library;lib.calls.append(self.name)
        if self.name=='aclrtGetDeviceCount':args[0]._obj.value=lib.count
        elif self.name=='aclrtGetLogicDevIdByPhyDevId':args[1]._obj.value=lib.logical
        elif self.name=='aclrtGetPhyDevIdByLogicDevId':args[1]._obj.value=lib.physical
        elif self.name=='aclrtCreateContext':args[0]._obj.value=lib.handle
        return lib.fail.get(self.name,0)


class Library:
    def __init__(self,fail=None,count=1,logical=0,physical=1,handle=1234):
        self.fail=fail or {};self.count=count;self.logical=logical;self.physical=physical;self.handle=handle;self.calls=[]
        for n in ['aclInit','aclFinalize','aclrtGetDeviceCount','aclrtGetLogicDevIdByPhyDevId','aclrtGetPhyDevIdByLogicDevId','aclrtCreateContext','aclrtDestroyContext']:
            setattr(self,n,Function(n,self))


class RuntimeDiagnosticTests(unittest.TestCase):
    def test_success_requires_roundtrip_and_exact_explicit_context_cleanup(self):
        lib=Library();value=diagnostic.initialize(lib,1)
        self.assertEqual(value['status'],'context_created_and_destroyed');self.assertTrue(value['acl_finalize_succeeded'])
        self.assertEqual(lib.calls,['aclInit','aclrtGetDeviceCount','aclrtGetLogicDevIdByPhyDevId','aclrtGetPhyDevIdByLogicDevId','aclrtCreateContext','aclrtDestroyContext','aclFinalize'])
        self.assertEqual(value['mapping'],{'physical_id':1,'logical_id':0,'roundtrip_physical_id':1,'visibility_environment_unset':True})
        self.assertFalse(value['qualification_granted']);self.assertEqual(value['device_reset_api_calls'],0)
        self.assertEqual(lib.aclrtCreateContext.restype,ctypes.c_int)
    def test_initialization_failure_does_not_call_other_apis(self):
        lib=Library({'aclInit':100001});v=diagnostic.initialize(lib,1)
        self.assertEqual(v['status'],'acl_initialization_failed');self.assertEqual(lib.calls,['aclInit'])
    def test_count_failure_or_empty_inventory_finalizes_without_context(self):
        for lib in [Library({'aclrtGetDeviceCount':507899}),Library(count=0),Library(count=257)]:
            v=diagnostic.initialize(lib,1);self.assertFalse(v['context_created'])
            self.assertNotIn('aclrtCreateContext',lib.calls);self.assertEqual(lib.calls[-1],'aclFinalize')
    def test_mapping_failure_and_wrong_card_never_create_context(self):
        for lib in [Library({'aclrtGetLogicDevIdByPhyDevId':1}),Library(logical=-1),Library(physical=0),Library({'aclrtGetPhyDevIdByLogicDevId':1})]:
            diagnostic.initialize(lib,1);self.assertNotIn('aclrtCreateContext',lib.calls);self.assertEqual(lib.calls[-1],'aclFinalize')
    def test_creation_error_preserves_code_and_finalizes_without_destroying_invalid_handle(self):
        lib=Library({'aclrtCreateContext':507033},handle=None);v=diagnostic.initialize(lib,1)
        self.assertEqual(v['status'],'context_creation_failed');self.assertNotIn('aclrtDestroyContext',lib.calls)
        self.assertEqual(v['calls'][-2]['return_code'],507033);self.assertEqual(lib.calls[-1],'aclFinalize')
    def test_null_handle_and_cleanup_failures_never_claim_runtime_ready(self):
        for lib,status in [(Library(handle=None),'context_handle_unconfirmed'),(Library({'aclrtDestroyContext':1}),'context_cleanup_unconfirmed'),(Library({'aclFinalize':1}),'finalization_unconfirmed')]:
            self.assertEqual(diagnostic.initialize(lib,1)['status'],status)
    def test_missing_api_refuses_before_loading_device_context(self):
        lib=Library();del lib.aclrtGetPhyDevIdByLogicDevId
        v=diagnostic.initialize(lib,1);self.assertEqual(v['status'],'api_unavailable');self.assertEqual(lib.calls,[])
    def test_bad_physical_id_has_no_side_effects(self):
        for physical in [True,-1,256,'1']:
            lib=Library()
            with self.assertRaises(ValueError):diagnostic.initialize(lib,physical)
            self.assertEqual(lib.calls,[])
    def test_recent_error_is_captured_bounded_before_finalization(self):
        class Error:
            def __call__(self):return b'x'*5000
        lib=Library({'aclInit':500000});lib.aclGetRecentErrMsg=Error()
        value=diagnostic.initialize(lib,1)
        self.assertEqual(len(value['recent_error_message']),4096);self.assertEqual(lib.calls,['aclInit'])
    def test_log_capture_refuses_symlinks_hardlinks_and_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);logs=root/'logs';logs.mkdir();(logs/'normal.log').write_bytes(b'own-log')
            outside=root/'outside';outside.write_bytes(b'must-not-read');(logs/'symlink').symlink_to(outside)
            os.link(outside,logs/'hardlink');os.mkfifo(logs/'fifo')
            value=diagnostic.capture_logs(root)
            self.assertEqual([v['name'] for v in value['files']],['logs/normal.log'])
            self.assertEqual(base64.b64decode(value['files'][0]['data_base64']),b'own-log');self.assertTrue(value['incomplete'])
    def test_log_capture_has_file_entry_and_byte_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);logs=root/'logs';logs.mkdir()
            for i in range(12):(logs/(str(i)+'.log')).write_bytes(b'a'*100000)
            value=diagnostic.capture_logs(root)
            self.assertLessEqual(value['captured_bytes'],256*1024);self.assertLessEqual(len(value['files']),8)
            self.assertTrue(value['incomplete']);self.assertTrue(all(v['prefix_truncated'] for v in value['files']))


if __name__=='__main__':unittest.main()

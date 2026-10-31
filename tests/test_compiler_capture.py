"""Actual pipe draining, deadline and output-file limits for the fixed driver."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

DRIVER=Path(__file__).resolve().parents[1]/'examples/workflows/compiler_compatibility.py'
spec=importlib.util.spec_from_file_location('compiler_driver',DRIVER);driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


class CompilerCaptureTests(unittest.TestCase):
    def test_both_large_streams_are_drained_counted_and_bounded(self):
        code,timed,logs,counts=driver.capture([sys.executable,'-c',"import os;os.write(1,b'o'*300000);os.write(2,b'e'*400000)"],5,8192)
        self.assertEqual(code,0);self.assertFalse(timed);self.assertEqual(counts,{'stdout':300000,'stderr':400000})
        self.assertEqual(logs,{'stdout':b'o'*8192,'stderr':b'e'*8192})

    def test_live_silent_child_with_closed_pipes_is_terminated_at_deadline(self):
        code,timed,logs,counts=driver.capture([sys.executable,'-c','import os,time;os.close(1);os.close(2);time.sleep(10)'],.15,8192)
        self.assertEqual(code,124);self.assertTrue(timed);self.assertEqual(counts,{'stdout':0,'stderr':0})

    def test_file_output_cannot_grow_past_its_versioned_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'output.mlir'
            code,timed,_,_=driver.capture([sys.executable,'-c',"import sys;open(sys.argv[1],'wb').write(b'x'*100000)",str(target)],5,8192)
            self.assertNotEqual(code,0);self.assertFalse(timed);self.assertLessEqual(target.stat().st_size,8192)


if __name__=='__main__':unittest.main()

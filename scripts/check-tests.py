#!/usr/bin/env python3
"""Full legacy test suite via a disposable public-source view. Use an isolated host."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode=True
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src/ascend_lab' if (root/'src/ascend_lab').is_dir() else root))
from source_layout import snapshot

if __name__=='__main__':
    os.environ['ASCEND_LAB_REPOSITORY_ROOT']=str(root)
    with tempfile.TemporaryDirectory(prefix='ascend-lab-full-source-check-') as folder:
        snapshot(root,folder)
        (Path(folder)/'runtime').mkdir(mode=0o700)
        sys.path[:0]=[folder,str(Path(folder)/'tests')]
        previous=os.getcwd()
        try:
            os.chdir(folder)
            suite=unittest.defaultTestLoader.discover('tests',pattern='test_*.py')
            result=unittest.TextTestRunner(verbosity=2).run(suite)
        finally:os.chdir(previous)
    raise SystemExit(0 if result.wasSuccessful() else 1)

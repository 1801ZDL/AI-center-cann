"""Kill only our child process at actual file/SQLite transaction boundaries."""
import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import JobStore

CONFIG={'username':'crash-owner','password':'test-only','container':'zdl'}
DATA=b'first-prefix-and-new-tail'


def child(root,identifier,action):
    jobs=JobStore(root,CONFIG);owner=jobs.access.user('crash-owner')
    def boundary():
        print('boundary-ready',flush=True)
        while True:signal.pause()
    if action=='upload':
        import storage_io
        native=storage_io.write_chunk
        def write(*args,**kwargs):
            native(*args,**kwargs);boundary()
        storage_io.write_chunk=write
        jobs.inputs.upload(identifier,owner,{'name':'nested/data.bin','offset':5,'data':base64.b64encode(DATA[5:]).decode()})
    else:
        native=jobs.transaction
        @contextmanager
        def transaction():
            with native() as db:
                yield db
                boundary()
        jobs.transaction=transaction
        jobs.inputs.reset_file(identifier,owner,{'name':'nested/data.bin'})


class InputCrashTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='input-crash-')
        self.root=Path(self.temp.name);self.jobs=JobStore(self.root,CONFIG);self.owner=self.jobs.access.user('crash-owner')
        self.bundle=self.jobs.inputs.create(self.owner,{'files':[{'name':'nested/data.bin','size':len(DATA),'sha256':hashlib.sha256(DATA).hexdigest()}]})[0]
        self.send(0,DATA[:5])

    def tearDown(self):self.jobs.access.close();self.temp.cleanup()

    def send(self,offset,data):
        return self.jobs.inputs.upload(self.bundle['id'],self.owner,{'name':'nested/data.bin','offset':offset,'data':base64.b64encode(data).decode()})

    def kill_at(self,action):
        with subprocess.Popen(['/usr/bin/python3',str(Path(__file__).resolve()),'--child',str(self.root),self.bundle['id'],action],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout,selectors.EVENT_READ)
                    self.assertTrue(selector.select(10),'Child did not reach the actual transaction boundary')
                line=process.stdout.readline();self.assertEqual(line,'boundary-ready\n',line)
                self.assertIsNone(process.poll());process.kill();self.assertEqual(process.wait(timeout=5),-signal.SIGKILL)
                self.assertEqual(process.stderr.read(),'')
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=5)
        self.jobs.access.close();self.jobs=JobStore(self.root,CONFIG);self.owner=self.jobs.access.user('crash-owner')

    def test_kill_after_file_sync_before_offset_commit_resumes_old_prefix(self):
        self.kill_at('upload')
        self.assertEqual(self.jobs.inputs.get(self.bundle['id'])['uploaded']['nested/data.bin'],5)
        self.assertEqual((self.jobs.inputs.directory(self.bundle['id'])/'nested/data.bin').read_bytes(),DATA)
        self.assertTrue(self.send(0,DATA[:5])['reused'])
        self.send(5,DATA[5:]);self.assertTrue(self.jobs.inputs.seal(self.bundle['id'],self.owner)['sealed'])

    def test_kill_before_reset_commit_preserves_old_bytes_and_prefix(self):
        self.kill_at('reset')
        self.assertEqual(self.jobs.inputs.get(self.bundle['id'])['uploaded']['nested/data.bin'],5)
        self.assertEqual((self.jobs.inputs.directory(self.bundle['id'])/'nested/data.bin').read_bytes(),DATA[:5])
        self.send(5,DATA[5:]);self.assertTrue(self.jobs.inputs.seal(self.bundle['id'],self.owner)['sealed'])


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':child(Path(sys.argv[2]),sys.argv[3],sys.argv[4])
    else:unittest.main()

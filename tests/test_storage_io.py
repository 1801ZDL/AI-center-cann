"""Partial writes, quota/fsync failures and real HTTP responses retain authority."""
import base64
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_nodes as node_fixture
import storage_io
from jobs import JobError
from server import ThreadingHTTPServer,handler_class


class StorageIOTests(unittest.TestCase):
    def setUp(self):
        self.fixture=node_fixture.NodeTests(methodName='runTest');self.fixture.setUp()
        self.jobs=self.fixture.jobs;self.nodes=self.jobs.nodes
        self.job=self.fixture.submit();self.grant=self.nodes.claim(self.fixture.a)['grant']
        self.auth=self.fixture.auth(self.grant)

    def tearDown(self):self.fixture.tearDown()

    def upload(self,data,offset=0):
        return self.nodes.upload(self.fixture.a,{**self.auth,'name':'stdout.log','offset':offset,
            'data':base64.b64encode(data).decode()})

    def offset(self):
        with self.jobs.connect() as db:
            row=db.execute('SELECT size FROM worker_uploads WHERE attempt=? AND name=?',(self.grant['attempt_id'],'stdout.log')).fetchone()
            return row[0] if row else 0

    def test_short_writes_complete_without_skipping_bytes(self):
        native=os.write
        with patch('storage_io.os.write',side_effect=lambda fd,data:native(fd,data[:2])):
            self.assertEqual(self.upload(b'0123456789')['size'],10)
        self.assertEqual((self.jobs.directory(self.job['id'])/'stdout.log').read_bytes(),b'0123456789')

    def test_partial_enospc_and_edquot_preserve_committed_prefix_and_lease(self):
        self.upload(b'abc');native=os.write;path=self.jobs.directory(self.job['id'])/'stdout.log'
        for number in [errno.ENOSPC,errno.EDQUOT]:
            calls=[]
            def failed(fd,data):
                if not calls:
                    calls.append(True);return native(fd,data[:2])
                raise OSError(number,'private path must not appear in HTTP response')
            with patch('storage_io.os.write',side_effect=failed),self.assertRaises(OSError):self.upload(b'XYZ',3)
            self.assertEqual(path.read_bytes(),b'abc');self.assertEqual(self.offset(),3)
            self.assertEqual(self.jobs.get(self.job['id'])['state'],'reserved')
            self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        self.upload(b'XYZ',3);self.assertEqual(path.read_bytes(),b'abcXYZ')

    def test_input_fsync_failure_does_not_advance_upload_state(self):
        data=b'abcdef';manifest={'name':'data.bin','size':6,'sha256':__import__('hashlib').sha256(data).hexdigest()}
        bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[manifest]})[0]
        def send(offset,part):return self.jobs.inputs.upload(bundle['id'],self.fixture.alice,{'name':'data.bin','offset':offset,'data':base64.b64encode(part).decode()})
        send(0,data[:3]);native=os.fsync;calls=[]
        def failed(fd):
            if not calls:calls.append(True);raise OSError(errno.EDQUOT,'quota')
            return native(fd)
        with patch('storage_io.os.fsync',side_effect=failed),self.assertRaises(OSError):send(3,data[3:])
        self.assertEqual(self.jobs.inputs.get(bundle['id'])['uploaded']['data.bin'],3)
        self.assertEqual((self.jobs.inputs.directory(bundle['id'])/'data.bin').read_bytes(),data[:3])
        send(3,data[3:]);self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.fixture.alice)['sealed'])

    def test_nested_input_directories_are_synced_before_offset_commit(self):
        data=b'new nested bytes';name='new/a/b/data.bin'
        bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[{'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]})[0]
        native=os.fsync;seen=[]
        def sync(fd):
            seen.append((Path(os.readlink('/proc/self/fd/'+str(fd))),stat.S_ISDIR(os.fstat(fd).st_mode)))
            self.assertFalse(self.jobs.inputs.get(bundle['id'])['uploaded'])
            return native(fd)
        with patch('storage_io.os.fsync',side_effect=sync):
            self.jobs.inputs.upload(bundle['id'],self.fixture.alice,{'name':name,'offset':0,'data':base64.b64encode(data).decode()})
        path=self.jobs.inputs.directory(bundle['id'])/name
        required={path.parent,path.parent.parent,path.parent.parent.parent,path.parents[3],path.parents[4],self.jobs.payload.base}
        self.assertTrue(required<={p for p,isdir in seen if isdir},seen)
        self.assertEqual(seen[0],(path,False));self.assertEqual(self.jobs.inputs.get(bundle['id'])['uploaded'][name],len(data))

    def test_new_bundle_directory_sync_failure_does_not_publish_database_row(self):
        item={'name':'new.bin','size':1,'sha256':hashlib.sha256(b'x').hexdigest()}
        native=os.fsync
        def failed(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError(errno.EIO,'directory sync failed')
            return native(fd)
        with patch('storage_io.os.fsync',side_effect=failed),self.assertRaises(OSError):
            self.jobs.inputs.create(self.fixture.alice,{'files':[item]})
        with self.jobs.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM input_bundles').fetchone()[0],0)
        bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[item]})[0]
        self.assertFalse(bundle['sealed']);self.assertFalse(bundle['uploaded'])

    def test_reset_rollback_never_removes_committed_bytes(self):
        data=b'committed bytes';item={'name':'reset.bin','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[item]})[0]
        def send():return self.jobs.inputs.upload(bundle['id'],self.fixture.alice,{'name':'reset.bin','offset':0,'data':base64.b64encode(data).decode()})
        send();native=self.jobs.transaction
        @contextmanager
        def abort():
            with native() as db:
                yield db
                raise RuntimeError('injected abort before commit')
        with patch.object(self.jobs,'transaction',abort),self.assertRaisesRegex(RuntimeError,'injected abort'):
            self.jobs.inputs.reset_file(bundle['id'],self.fixture.alice,{'name':'reset.bin'})
        self.assertEqual((self.jobs.inputs.directory(bundle['id'])/'reset.bin').read_bytes(),data)
        self.assertEqual(self.jobs.inputs.get(bundle['id'])['uploaded']['reset.bin'],len(data))
        self.jobs.inputs.reset_file(bundle['id'],self.fixture.alice,{'name':'reset.bin'})
        self.assertFalse(self.jobs.inputs.get(bundle['id'])['uploaded'])
        send();self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.fixture.alice)['sealed'])

    def test_empty_input_is_not_sealed_when_file_sync_fails(self):
        bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[{'name':'nested/empty','size':0,'sha256':hashlib.sha256(b'').hexdigest()}]})[0]
        with patch('storage_io.os.fsync',side_effect=OSError(errno.EIO,'sync failed')),self.assertRaises(OSError):
            self.jobs.inputs.seal(bundle['id'],self.fixture.alice)
        self.assertFalse(self.jobs.inputs.get(bundle['id'])['sealed'])
        self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.fixture.alice)['sealed'])

    def test_directory_sync_failure_keeps_offset_and_same_retry_succeeds(self):
        native=os.fsync
        def failed(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError(errno.EDQUOT,'directory quota')
            return native(fd)
        with patch('storage_io.os.fsync',side_effect=failed),self.assertRaises(OSError):self.upload(b'new data')
        self.assertEqual(self.offset(),0);self.assertEqual(len(self.jobs.attempts(self.job['id'])),1)
        self.upload(b'new data');self.assertEqual(self.offset(),8)

    def test_interrupted_seal_can_be_reset_and_reuploaded(self):
        data=b'retry readonly';item={'name':'retry.bin','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[item]})[0]
        def send():return self.jobs.inputs.upload(bundle['id'],self.fixture.alice,{'name':'retry.bin','offset':0,'data':base64.b64encode(data).decode()})
        send()
        with patch('storage_io.os.fsync',side_effect=OSError(errno.EIO,'interrupted seal')),self.assertRaises(OSError):
            self.jobs.inputs.seal(bundle['id'],self.fixture.alice)
        self.assertFalse(self.jobs.inputs.get(bundle['id'])['sealed'])
        self.assertEqual((self.jobs.inputs.directory(bundle['id'])/'retry.bin').stat().st_mode&0o777,0o400)
        self.jobs.inputs.reset_file(bundle['id'],self.fixture.alice,{'name':'retry.bin'})
        send();self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.fixture.alice)['sealed'])

    def test_zero_progress_is_io_error_and_hardlinks_are_not_overwritten(self):
        self.upload(b'abc');path=self.jobs.directory(self.job['id'])/'stdout.log'
        with patch('storage_io.os.write',return_value=0),self.assertRaises(OSError) as caught:self.upload(b'xyz',3)
        self.assertEqual(caught.exception.errno,errno.EIO);self.assertFalse(storage_io.exhausted(caught.exception))
        os.link(path,path.with_name('alias'))
        with self.assertRaises(JobError):self.upload(b'xyz',3)
        self.assertEqual(path.read_bytes(),b'abc');self.assertEqual(self.offset(),3)

    def test_only_capacity_errors_are_classified_as_exhaustion(self):
        for error in [OSError(errno.ENOSPC,'full'),OSError(errno.EDQUOT,'quota'),sqlite3.OperationalError('database or disk is full')]:
            self.assertTrue(storage_io.exhausted(error))
        for error in [OSError(errno.EIO,'io'),PermissionError(errno.EACCES,'permission'),sqlite3.OperationalError('database is locked')]:
            self.assertFalse(storage_io.exhausted(error))
        coded=sqlite3.OperationalError('localized');coded.sqlite_errorcode=sqlite3.SQLITE_FULL|(1<<8)
        self.assertTrue(storage_io.exhausted(coded))

    def test_http_node_and_input_capacity_responses_are_bounded_and_resumable(self):
        handler=handler_class(None,self.fixture.config,self.jobs)
        server=ThreadingHTTPServer(('127.0.0.1',0),handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        token=self.jobs.access.issue_token('alice',{'jobs:submit','jobs:read'})
        def request(route,key,body):
            r=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+route,data=json.dumps(body).encode(),
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=5)
            except urllib.error.HTTPError as error:response=error
            with response:return response.status,json.loads(response.read())
        try:
            data=b'x';item={'name':'input.bin','size':1,'sha256':__import__('hashlib').sha256(data).hexdigest()}
            bundle=self.jobs.inputs.create(self.fixture.alice,{'files':[item]})[0]
            chunk={'name':'input.bin','offset':0,'data':base64.b64encode(data).decode()}
            blob={**self.auth,**chunk,'name':'stdout.log'}
            for code in [errno.ENOSPC,errno.EDQUOT]:
                with patch('storage_io.os.write',side_effect=OSError(code,'/private/secret-path')):
                    for route,key,body in [('/api/worker/blob',self.fixture.key,blob),('/api/inputs/'+bundle['id']+'/blob',token,chunk)]:
                        status,value=request(route,key,body)
                        self.assertEqual(status,507);self.assertEqual(value['error'],'storage_unavailable');self.assertTrue(value['retryable'])
                        self.assertNotIn('secret-path',json.dumps(value))
                self.assertEqual(self.offset(),0);self.assertFalse(self.jobs.inputs.get(bundle['id'])['uploaded'])
            self.assertEqual(request('/api/worker/blob',self.fixture.key,blob)[0],200)
            self.assertEqual(request('/api/inputs/'+bundle['id']+'/blob',token,chunk)[0],200)
            self.assertEqual(len(self.jobs.attempts(self.job['id'])),1)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()

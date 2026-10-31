"""Headroom admission must preserve identity, retries, leases and recovery IO."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import control_storage as control
import test_nodes as builder
from jobs import JobError,atomic_json,defaults
from server import ThreadingHTTPServer,handler_class


def counters(*,mib=128,inodes=4096,flags=0,**kw):
    return SimpleNamespace(f_frsize=4096,f_bavail=mib*256,f_favail=inodes,f_files=8192,f_flag=flags,**kw)


class ObservationTests(unittest.TestCase):
    def test_old_policy_disabled_without_reading_filesystem(self):
        with patch('control_storage.os.open',side_effect=AssertionError('Legacy policy unexpectedly enabled')):
            self.assertFalse(control.observe(ROOT,None)['blocks_new_work'])
        policy,_=defaults({'container':'unused'})
        self.assertIsNone(policy['control_storage'])

    def test_configuration_rejects_missing_unknown_bool_and_unsafe_thresholds(self):
        for value in [{},{'minimum_free_mib':64},True,[],
                      {**control.DEFAULT,'path':'/arbitrary'},
                      *[{**control.DEFAULT,k:v} for k,values in [('minimum_free_mib',[True,15,65537,'64',64.0]),('minimum_free_inodes',[False,63,1000001])] for v in values]]:
            with self.subTest(value=value),self.assertRaises(ValueError):control.validate(value)
        self.assertEqual(control.validate(control.DEFAULT),control.DEFAULT)
        self.assertIsNot(control.validate(control.DEFAULT),control.DEFAULT)

    def test_exact_boundary_and_byte_inode_readonly_fail_closed(self):
        for reading,status,code in [(counters(mib=64,inodes=256),'ready',None),
             (counters(mib=63),'pressure','control_storage_bytes'),
             (counters(inodes=255),'pressure','control_storage_inodes'),
             (counters(flags=os.ST_RDONLY),'pressure','control_storage_readonly')]:
            with self.subTest(code=code),patch('control_storage.os.fstatvfs',return_value=reading):
                value=control.observe(ROOT,control.DEFAULT)
                self.assertEqual((value['status'],value['reason_code']),(status,code))
                self.assertEqual(value['blocks_new_work'],status!='ready')
                self.assertNotIn('path',value);self.assertNotIn('device',value)

    def test_unknown_counters_and_errors_are_unavailable_without_private_details(self):
        for reading in [counters(mib=-1),counters(inodes=-1),SimpleNamespace(**{**vars(counters()),'f_files':0}),SimpleNamespace(**{**vars(counters()),'f_frsize':0})]:
            with patch('control_storage.os.fstatvfs',return_value=reading):
                value=control.observe(ROOT,control.DEFAULT)
                self.assertTrue(value['blocks_new_work']);self.assertEqual(value['status'],'unavailable')
        with patch('control_storage.os.fstatvfs',side_effect=OSError('private path and credentials')):
            value=control.observe(ROOT,control.DEFAULT)
            self.assertNotIn('private',json.dumps(value));self.assertEqual(value['reason_code'],'control_storage_unavailable')

    def test_directory_fd_closed_and_final_symlink_refused(self):
        native=os.open;handles=[]
        def opened(*args,**kw):
            fd=native(*args,**kw);handles.append(fd);return fd
        with patch('control_storage.os.open',side_effect=opened):control.observe(ROOT,control.DEFAULT)
        for fd in handles:
            with self.assertRaises(OSError):os.fstat(fd)
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='control-observe-') as folder:
            link=Path(folder)/'link';link.symlink_to(ROOT,target_is_directory=True)
            self.assertEqual(control.observe(link,control.DEFAULT)['status'],'unavailable')


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.base=builder.NodeTests(methodName='runTest');self.base.setUp()
        self.jobs=self.base.jobs;self.nodes=self.jobs.nodes;self.alice=self.base.alice;self.a=self.base.a
        self.jobs.policy['control_storage']=dict(control.DEFAULT)
        self.request={'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1},'idempotency_key':'guard-original'}

    def tearDown(self):self.base.tearDown()

    def submit(self,**change):return self.jobs.submit({**self.request,**change},self.alice)

    def low(self):return patch('control_storage.os.fstatvfs',return_value=counters(mib=32))

    def test_new_job_and_input_rejected_before_directory_or_reservation(self):
        manifest=[{'name':'new.bin','size':1,'sha256':hashlib.sha256(b'x').hexdigest()}]
        with self.low(),self.assertRaises(control.Pressure):self.submit()
        with self.low(),self.assertRaises(control.Pressure):self.jobs.inputs.create(self.alice,{'files':manifest})
        self.assertEqual(self.jobs.all(),[]);self.assertEqual(list(self.jobs.root.iterdir()),[])
        with self.jobs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM storage_allocations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM input_bundles').fetchone()[0],0)

    def test_existing_idempotent_job_reuses_original_and_conflict_stays_conflict(self):
        job,_=self.submit()
        with self.low():
            repeated,reused=self.submit();self.assertTrue(reused);self.assertEqual(repeated['id'],job['id'])
            with self.assertRaises(JobError) as error:self.submit(parameters={'seconds':2})
            self.assertEqual(error.exception.status,409)
        self.assertEqual(len(self.jobs.all()),1)

    def test_committed_input_retry_is_readonly_but_growth_and_seal_pause(self):
        payload=b'abcdef';files=[{'name':'data.bin','size':6,'sha256':hashlib.sha256(payload).hexdigest()}]
        bundle,_=self.jobs.inputs.create(self.alice,{'files':files})
        def chunk(offset,data):return self.jobs.inputs.upload(bundle['id'],self.alice,{'name':'data.bin','offset':offset,'data':base64.b64encode(data).decode()})
        chunk(0,payload[:3]);path=self.jobs.inputs.directory(bundle['id'])/'data.bin'
        with self.low():
            reused,yes=self.jobs.inputs.create(self.alice,{'files':files});self.assertTrue(yes);self.assertEqual(reused['id'],bundle['id'])
            self.assertTrue(chunk(0,payload[:3])['reused'])
            with self.assertRaises(control.Pressure):chunk(3,payload[3:])
            with self.assertRaises(control.Pressure):self.jobs.inputs.seal(bundle['id'],self.alice)
            self.assertEqual(path.read_bytes(),payload[:3]);self.assertEqual(self.jobs.inputs.get(bundle['id'],self.alice)['uploaded']['data.bin'],3)
        chunk(3,payload[3:]);self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.alice)['sealed'])
        with self.low():self.assertTrue(self.jobs.inputs.seal(bundle['id'],self.alice)['sealed'])

    def test_preview_is_readonly_and_new_submission_rechecks_pressure(self):
        with self.jobs.connect() as db:before=list(db.iterdump())
        self.assertEqual(self.jobs.preview(self.request,self.alice)['status'],'eligible_snapshot')
        with self.low():
            preview=self.jobs.preview(self.request,self.alice)
            self.assertEqual(preview['status'],'blocked');self.assertFalse(preview['reservation_created'])
            self.assertTrue(all(c['reason']==control.HOLD for c in preview['candidates']))
            with self.assertRaises(control.Pressure):self.submit()
        with self.jobs.connect() as db:self.assertEqual(list(db.iterdump()),before)

    def test_new_claim_pauses_but_original_grant_recovery_survives(self):
        job,_=self.submit()
        with self.low():
            response=self.nodes.claim(self.a);self.assertIsNone(response['grant']);self.assertEqual(response['reason'],control.HOLD)
        self.assertFalse(self.jobs.attempts(job['id']))
        grant=self.nodes.claim(self.a)['grant']
        self.submit(idempotency_key='guard-follower')
        with self.low():
            self.assertEqual(self.nodes.claim(self.a)['grant'],grant)
            self.assertIsNone(self.nodes.claim(self.a,{'exclude_attempts':[grant['attempt_id']]})['grant'])
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)

    def test_saved_start_ticket_waits_without_being_consumed_then_resumes(self):
        job,_=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.base.auth(grant)
        with self.low():
            for _ in range(2):
                receipt=self.nodes.authorize_start(self.a,auth)
                self.assertTrue(receipt['wait']);self.assertFalse(receipt['start_permitted']);self.assertFalse(receipt['already_authorized'])
            self.assertFalse(self.jobs.get(job['id'])['_start_authorized'])
        self.assertTrue(self.nodes.authorize_start(self.a,auth)['start_permitted'])
        with self.low():self.assertTrue(self.nodes.authorize_start(self.a,auth)['already_authorized'])
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)

    def test_running_updates_upload_and_completion_continue_under_pressure(self):
        job,_=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.base.auth(grant)
        self.assertTrue(self.nodes.authorize_start(self.a,auth)['start_permitted'])
        data=b'{"runtime":"managed-docker"}';manifest=[{'name':'environment.json','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]
        with self.low():
            self.base.beat(self.a);self.nodes.update(self.a,{**auth,'running':True})
            self.nodes.upload(self.a,{**auth,'name':'environment.json','offset':0,'data':base64.b64encode(data).decode()})
            result={'status':'succeeded','exit_code':0,'termination_confirmed':True,'execution_cleanup_confirmed':True}
            response=self.nodes.complete(self.a,{**auth,'result':result,'artifacts':manifest})
        self.assertTrue(response['accepted']);self.assertEqual(self.jobs.get(job['id'])['state'],'succeeded')
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],0)

    def test_cancel_and_unknown_attempt_hold_are_not_overridden(self):
        job,_=self.submit();grant=self.nodes.claim(self.a)['grant'];auth=self.base.auth(grant)
        with self.low():
            self.nodes.update(self.a,{**auth,'running':False,'attention':True})
            self.jobs.cancel(job['id'],self.alice)
        self.assertEqual(self.jobs.summary()['reserved']['cpu'],1)
        self.assertEqual(len(self.jobs.attempts(job['id'])),1)
        self.assertFalse(self.nodes.authorize_start(self.a,auth)['start_permitted'])

    def test_revocation_is_not_turned_into_a_storage_wait(self):
        _,_=self.submit();grant=self.nodes.claim(self.a)['grant']
        self.jobs.access.disable_user('alice')
        with self.low():
            response=self.nodes.authorize_start(self.a,self.base.auth(grant))
            self.assertFalse(response['start_permitted']);self.assertFalse(response.get('wait',False))

    def test_downgrade_rejects_missing_guard_only_when_enabled(self):
        from deployment import CORE,DeployError
        from maintenance import require_features
        manifest={'files':{name:{} for name in CORE if name!='control_storage.py'}}
        path=self.base.runtime/'config.json';atomic_json(path,{**self.base.config,'scheduler':{'control_storage':dict(control.DEFAULT)}})
        with self.assertRaisesRegex(DeployError,'Control storage admission'):require_features(self.base.runtime,'controller',manifest)
        atomic_json(path,self.base.config);require_features(self.base.runtime,'controller',manifest)

    def test_authenticated_status_and_pressure_response_do_not_need_audit_writes(self):
        token=self.jobs.access.issue_token('alice',{'jobs:read','jobs:submit'})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(None,self.base.config,self.jobs))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        def call(path,body=None,credential=token):
            headers={'Content-Type':'application/json'}
            if credential:headers['Authorization']='Bearer '+credential
            request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
            try:response=urllib.request.urlopen(request,timeout=5)
            except urllib.error.HTTPError as error:response=error
            with response:return response.status,json.load(response)
        try:
            original,_=self.submit()
            with self.low(),patch.object(self.jobs.access,'record',side_effect=AssertionError('Pressure admission attempted an audit write')):
                status,value=call('/api/control-storage');self.assertEqual(status,200);self.assertTrue(value['blocks_new_work'])
                self.assertNotIn(str(self.base.runtime),json.dumps(value))
                self.assertEqual(call('/api/control-storage',credential=None)[0],401)
                self.assertEqual(call('/api/jobs',self.request)[1]['job']['id'],original['id'])
                status,error=call('/api/jobs',{**self.request,'idempotency_key':'new-under-pressure'})
                self.assertEqual(status,503);self.assertEqual(error['error'],'control_storage_pressure');self.assertTrue(error['original_operation_required'])
            readonly=self.jobs.access.issue_token('alice',{'jobs:submit'})
            self.assertEqual(call('/api/control-storage',credential=readonly)[0],403)
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()

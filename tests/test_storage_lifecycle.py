"""Storage authority, quotas, crash recovery, links, reader leases and history."""
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jobs import ROOT,JobStore,JobError,atomic_json
from auth import AccessError
from storage_lifecycle import rebind_restored
from maintenance import assert_quiescent
from deployment import DeployError
import storage_files as fs

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-lifecycle-');self.runtime=Path(self.temp.name)
        self.config={'username':'storage-admin','password':'test-only','container':'zdl','scheduler':{'storage_trash_grace_seconds':1}}
        self.jobs=JobStore(self.runtime,self.config)
        for name in ['alice','bob']:
            home=self.runtime/name;home.mkdir();self.jobs.access.create_user(name,home,environments=['worker-python'])
        self.alice=self.jobs.access.user('alice');self.bob=self.jobs.access.user('bob')
    def tearDown(self):self.temp.cleanup()
    def bundle(self,data=b'input data',sealed=True):
        body={'files':[{'name':'data.bin','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}
        value,_=self.jobs.inputs.create(self.alice,body)
        for offset in range(0,len(data),256*1024):
            self.jobs.inputs.upload(value['id'],self.alice,{'name':'data.bin','offset':offset,'data':base64.b64encode(data[offset:offset+256*1024]).decode()})
        if sealed:self.jobs.inputs.seal(value['id'],self.alice)
        return value,body
    def archive(self):
        job,_=self.jobs.submit({'template':'cpu_demo','environment':'worker-python','parameters':{'seconds':1}},self.alice)
        return self.jobs.cancel(job['id'],self.alice)
    def plan(self,kind,value,action):return self.jobs.lifecycle.plan(self.alice,{'kind':kind,'id':value,'action':action})['plan']
    def act(self,kind,value,action):return self.jobs.lifecycle.apply(self.alice,self.plan(kind,value,action))
    def charge(self,kind,value):
        with self.jobs.connect() as db:return db.execute('SELECT bytes FROM storage_allocations WHERE kind=? AND id=?',(kind,value)).fetchone()[0]
    def expired(self):return patch('storage_lifecycle.time.time',return_value=time.time()+10)

    def test_input_trash_restore_preserves_bytes_hash_and_does_not_credit_rename(self):
        value,_=self.bundle(b'x'*1024*1024);before=self.charge('input',value['id']);plan=self.plan('input',value['id'],'trash')
        trashed=self.jobs.lifecycle.apply(self.alice,plan);self.assertEqual(trashed['object']['state'],'trash')
        self.assertGreaterEqual(self.charge('input',value['id']),before)
        with self.assertRaises(JobError) as error:self.jobs.lifecycle.available('input',value['id'])
        self.assertEqual(error.exception.status,410);self.assertTrue(self.jobs.lifecycle.apply(self.alice,plan)['reused'])
        restored=self.act('input',value['id'],'restore');self.assertEqual(restored['object']['state'],'available')
        self.assertEqual(hashlib.sha256((self.jobs.inputs.directory(value['id'])/'data.bin').read_bytes()).hexdigest(),value['files'][0]['sha256'])
    def test_purge_requires_grace_and_scope_then_credits_only_after_removal(self):
        value,_=self.bundle(b'x'*1024*1024);self.act('input',value['id'],'trash');plan=self.plan('input',value['id'],'purge')
        with self.assertRaisesRegex(JobError,'grace'):self.jobs.lifecycle.apply(self.alice,plan)
        limited=replace(self.alice,scopes=frozenset({'jobs:read','jobs:submit'}))
        with self.assertRaises(AccessError):self.jobs.lifecycle.apply(limited,plan)
        before=self.charge('input',value['id'])
        with self.expired():result=self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(result['object']['state'],'purged');self.assertTrue(result['quota_released'])
        self.assertLess(self.charge('input',value['id']),before);self.assertGreater(self.charge('input',value['id']),0)
        source,trash=self.jobs.lifecycle.paths('input',value['id']);self.assertFalse(source.exists());self.assertFalse(trash.exists())
    def test_cross_user_admin_override_and_project_read_do_not_grant_storage_ownership(self):
        value,_=self.bundle()
        for principal in [self.bob,self.jobs.access.local]:
            with self.assertRaises(JobError) as error:self.jobs.lifecycle.plan(principal,{'kind':'input','id':value['id'],'action':'trash'})
            self.assertEqual(error.exception.status,404)
    def test_active_queued_and_unknown_jobs_and_input_dependencies_are_protected(self):
        value,_=self.bundle();job,_=self.jobs.submit({'template':'ir_v1','environment':'worker-python','parameters':{
            'input_id':value['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'}}},self.alice)
        for state in ['queued','reserved','running','needs_attention','cancelling']:
            with self.jobs.transaction() as db:
                current=self.jobs.get(job['id'],db);current['state']=state;self.jobs.write(db,current)
            with self.assertRaises(JobError):self.plan('archive',job['id'],'trash')
            with self.assertRaises(JobError):self.plan('input',value['id'],'trash')
    def test_reader_lease_blocks_quarantine_until_reader_finishes(self):
        job=self.archive();plan=self.plan('archive',job['id'],'trash')
        with self.jobs.lifecycle.reader('archive',job['id']):
            with self.assertRaisesRegex(JobError,'read or maintained'):self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(self.jobs.lifecycle.apply(self.alice,plan)['object']['state'],'trash')
    def test_preview_rejects_new_files_without_any_retirement_or_quota_credit(self):
        job=self.archive();plan=self.plan('archive',job['id'],'trash');before=self.charge('archive',job['id'])
        (self.jobs.directory(job['id'])/'new-data').write_text('keep')
        with self.assertRaisesRegex(JobError,'changed after preview'):self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(self.jobs.lifecycle.record('archive',job['id'])['state'],'available')
        self.assertEqual(self.charge('archive',job['id']),before)
    def test_crash_after_rename_resumes_same_operation_and_keeps_billing(self):
        job=self.archive();plan=self.plan('archive',job['id'],'trash');before=self.charge('archive',job['id'])
        original=fs.move
        def crash(source,target):original(source,target);raise RuntimeError('simulated process loss after rename')
        with patch('storage_files.move',side_effect=crash):
            with self.assertRaises(RuntimeError):self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(self.jobs.lifecycle.record('archive',job['id'])['state'],'moving_to_trash')
        self.assertEqual(self.charge('archive',job['id']),before)
        with self.assertRaises(DeployError):assert_quiescent(self.runtime,'controller')
        restarted=JobStore(self.runtime,self.config)
        resume=restarted.lifecycle.plan(self.alice,{'kind':'archive','id':job['id'],'action':'resume'})['plan']
        self.assertEqual(restarted.lifecycle.apply(self.alice,resume)['object']['state'],'trash')
        self.assertTrue(restarted.lifecycle.apply(self.alice,resume)['reused'])
    def test_restore_collision_is_not_overwritten_and_can_resume_after_reconciliation(self):
        job=self.archive();self.act('archive',job['id'],'trash');plan=self.plan('archive',job['id'],'restore')
        source,trash=self.jobs.lifecycle.paths('archive',job['id']);source.mkdir();sentinel=source/'sentinel';sentinel.write_text('untouched')
        with self.assertRaisesRegex(JobError,'destination already exists'):self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(sentinel.read_text(),'untouched');sentinel.unlink();source.rmdir()
        result=self.act('archive',job['id'],'resume');self.assertEqual(result['object']['state'],'available')
    def test_partial_purge_keeps_quota_then_resumes_without_following_symlink(self):
        job=self.archive();directory=self.jobs.directory(job['id']);outside=self.runtime/'outside';outside.write_text('must remain')
        (directory/'outside-link').symlink_to(outside)
        for index in range(300):(directory/('file-'+str(index))).write_bytes(b'x')
        self.act('archive',job['id'],'trash');before=self.charge('archive',job['id'])
        with self.expired():first=self.act('archive',job['id'],'purge')
        self.assertTrue(first['pending']);self.assertEqual(self.charge('archive',job['id']),before)
        self.assertEqual(self.act('archive',job['id'],'resume')['object']['state'],'purged')
        self.assertEqual(outside.read_text(),'must remain')
    def test_mutated_partial_quarantine_stops_resumption_without_credit(self):
        job=self.archive();directory=self.jobs.directory(job['id'])
        for index in range(300):(directory/('item-'+str(index))).write_bytes(b'x')
        self.act('archive',job['id'],'trash')
        with self.expired():self.assertTrue(self.act('archive',job['id'],'purge')['pending'])
        _,trash=self.jobs.lifecycle.paths('archive',job['id']);(trash/'new-unapproved').write_text('preserve')
        before=self.charge('archive',job['id'])
        with self.assertRaisesRegex(JobError,'content changed'):self.act('archive',job['id'],'resume')
        self.assertEqual(self.charge('archive',job['id']),before);self.assertTrue((trash/'new-unapproved').exists())
    def test_hardlink_and_quarantine_parent_symlink_are_rejected_safely(self):
        job=self.archive();directory=self.jobs.directory(job['id']);outside=self.runtime/'outside-file';outside.write_text('keep')
        os.link(outside,directory/'hardlink')
        with self.assertRaises(JobError):self.plan('archive',job['id'],'trash')
        (directory/'hardlink').unlink();plan=self.plan('archive',job['id'],'trash')
        other=self.runtime/'outside-dir';other.mkdir();(self.runtime/'storage-trash').symlink_to(other,target_is_directory=True)
        with self.assertRaises(JobError):self.jobs.lifecycle.apply(self.alice,plan)
        self.assertEqual(list(other.iterdir()),[]);self.assertEqual(outside.read_text(),'keep')
    def test_purged_input_can_be_uploaded_again_under_same_hash_and_count_slot(self):
        value,body=self.bundle(b'original');self.jobs.policy['input_max_bundles']=1
        self.act('input',value['id'],'trash')
        with self.expired():self.act('input',value['id'],'purge')
        revived,reused=self.jobs.inputs.create(self.alice,body);self.assertTrue(reused);self.assertFalse(revived['sealed'])
        self.assertEqual(revived['id'],value['id']);self.assertEqual(revived['uploaded'],{})
        self.jobs.inputs.upload(value['id'],self.alice,{'name':'data.bin','offset':0,'data':base64.b64encode(b'original').decode()})
        self.assertTrue(self.jobs.inputs.seal(value['id'],self.alice)['sealed'])
    def test_purged_accounting_is_not_resurrected_on_restart_and_history_remains(self):
        job=self.archive();self.act('archive',job['id'],'trash')
        with self.expired():self.act('archive',job['id'],'purge')
        before=self.charge('archive',job['id']);restarted=JobStore(self.runtime,self.config)
        self.assertEqual(restarted.get(job['id'])['state'],'cancelled')
        with restarted.connect() as db:self.assertEqual(db.execute('SELECT bytes FROM storage_allocations WHERE kind=? AND id=?',('archive',job['id'])).fetchone()[0],before)
        with self.assertRaises(JobError) as error:restarted.log(job['id'],principal=self.alice)
        self.assertEqual(error.exception.status,410)
    def test_submission_race_rechecks_input_availability_inside_transaction(self):
        value,_=self.bundle();request={'template':'ir_v1','environment':'worker-python','parameters':{
          'input_id':value['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'}}}
        normalize=self.jobs.normalize
        def interleave(*args,**kwargs):
            result=normalize(*args,**kwargs);self.act('input',value['id'],'trash');return result
        with patch.object(self.jobs,'normalize',side_effect=interleave):
            with self.assertRaises(JobError):self.jobs.submit(request,self.alice)
        self.assertEqual(self.jobs.all(),[])
    def test_verified_restore_rebinds_quarantined_file_identity_and_invalidates_old_plan(self):
        job=self.archive();self.act('archive',job['id'],'trash');before=self.plan('archive',job['id'],'restore')
        copied=self.runtime/'restored';copied.mkdir()
        # Independent file copy simulates the verified snapshot extractor, not an untrusted import.
        shutil.copyfile(self.jobs.db_path,copied/'jobs.sqlite3')
        shutil.copytree(self.runtime/'storage-trash',copied/'storage-trash')
        rebind_restored(copied)
        with __import__('sqlite3').connect(copied/'jobs.sqlite3') as db:
            record=json.loads(db.execute('SELECT record FROM storage_lifecycle WHERE id=?',(job['id'],)).fetchone()[0])
        self.assertGreater(record['revision'],before['revision']);self.assertNotEqual(record['fingerprint'],before['fingerprint'])
    def test_http_scope_owner_and_tombstone_metadata_contract(self):
        import threading,urllib.request,urllib.error
        from server import ThreadingHTTPServer,handler_class
        job=self.archive();token=self.jobs.access.issue_token('alice',{'jobs:read','jobs:submit','jobs:cancel','storage:manage'})
        limited=self.jobs.access.issue_token('alice',{'jobs:read','jobs:submit'})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(None,self.config,self.jobs));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,bearer=token):
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_address[1])+path,
              data=json.dumps(body).encode() if body is not None else None,
              headers={'Authorization':'Bearer '+bearer,'Content-Type':'application/json'})
            try:response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5)
            except urllib.error.HTTPError as error:response=error
            with response:return response.status,json.loads(response.read())
        try:
            code,preview=request('/api/storage/plan',{'kind':'archive','id':job['id'],'action':'trash'});self.assertEqual(code,200)
            self.assertEqual(request('/api/storage/apply',preview['plan'],limited)[0],403)
            self.assertEqual(request('/api/storage/apply',preview['plan'])[0],200)
            self.assertEqual(request('/api/jobs/'+job['id']+'/archive-manifest')[0],410)
            self.assertEqual(request('/api/jobs/'+job['id']+'/artifact?name=job.json')[0],410)
            code,detail=request('/api/jobs/'+job['id']);self.assertEqual(code,200)
            self.assertEqual(detail['job']['state'],'cancelled');self.assertEqual(detail['job']['archive_status']['state'],'trash')
            self.assertTrue(detail['artifacts']);self.assertTrue(detail['permissions']['storage'])
            code,listing=request('/api/storage?state=trash&limit=1');self.assertEqual(code,200);self.assertEqual(listing['total'],1)
        finally:server.shutdown();server.server_close();thread.join(timeout=3)
    def test_existing_idempotent_job_is_returned_after_input_retirement_but_new_work_refused(self):
        value,_=self.bundle();request={'template':'ir_v1','environment':'worker-python','idempotency_key':'original-key','parameters':{
          'input_id':value['id'],'argv':['python','main.py'],'lock':{'python':'3.10.12'}}}
        job,_=self.jobs.submit(request,self.alice);self.jobs.cancel(job['id'],self.alice);self.act('input',value['id'],'trash')
        repeated,reused=self.jobs.submit(request,self.alice);self.assertTrue(reused);self.assertEqual(repeated['id'],job['id'])
        with self.assertRaises(JobError):self.jobs.submit({**request,'idempotency_key':'new-key'},self.alice)
    def test_enabled_node_environment_evidence_is_pinned(self):
        job=self.archive()
        profile={'name':'pinned-node','physical_id':self.jobs.backend.physical_id,'users':{'alice':{'uid':23631,'gid':23631}},
          'templates':['cpu_demo'],'policy':{'cpu_ids':[self.jobs.policy['cpu_ids'][0]],'memory_mib':512,'shm_mib':0,'max_running':1}}
        node,_=self.jobs.nodes.enroll(profile)
        with self.jobs.connect() as db:
            raw=self.jobs.nodes.node(node,db)['profile'];raw['environment_manifest']={'source':{'job_id':job['id']}}
            db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(raw),node))
        with self.assertRaisesRegex(JobError,'pins'):self.plan('archive',job['id'],'trash')
        self.jobs.nodes.disable(node);self.assertEqual(self.act('archive',job['id'],'trash')['object']['state'],'trash')
    def test_global_inventory_hold_blocks_gc_without_releasing_compute_or_storage(self):
        job=self.archive();before=self.charge('archive',job['id'])
        with self.jobs.connect() as db:db.execute('INSERT INTO worker_inventory_holds VALUES(?,?,?,?,?,?)',('worker-'+'a'*12,self.jobs.backend.physical_id,1,1,'[]',time.time()))
        with self.assertRaisesRegex(JobError,'inventories'):self.plan('archive',job['id'],'trash')
        self.assertEqual(self.charge('archive',job['id']),before);self.assertTrue(self.jobs.directory(job['id']).exists())

"""Cold authority, byte-verified ranges, original GC and restore history.

Unit state fixtures explicitly stand in for Root/native storage; actual installed
service + finite ext4 and SIGKILL coverage is a separate acceptance scenario.
"""
import copy,json,os,shutil,sqlite3,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import test_cold_exports as fixture
import cold_exports as exports
import cold_reads as reads
import cold_retention as retention
import storage_files as fs
from archive_api import document,manifest,chunk,open_artifact
from jobs import JobError
from deployment import DeployError
from maintenance import assert_quiescent


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.env=fixture.ColdStateTests();self.env.setUp();self.db=self.env.db
        (self.env.base/'prefix').mkdir(mode=0o700)
        self.patch=patch.object(retention,'matching');self.patch.start()
        self.admin_patch=patch.object(retention,'ADMIN_UID',os.getuid());self.admin_patch.start()
        self.original=self.env.plan();self.env.finish(self.original)
        self.jobs=SimpleNamespace(get=lambda key,db:self.env.job)
    def tearDown(self):self.admin_patch.stop();self.patch.stop();self.env.tearDown()
    def plan(self,action):return retention.plan(self.env.base/'prefix',self.original,action)
    def apply(self,plan,budget=256):return retention.apply(self.env.base/'prefix',plan,budget)
    def activate(self):
        plan=self.plan('activate');self.apply(plan);return plan
    def directory(self):return reads.resolve(self.jobs,self.env.jobid,self.db)
    def retire(self):
        plan=self.plan('retire')
        for _ in range(10):
            v=self.apply(plan,1)
            if not v.get('pending'):return plan,v
        self.fail('Bounded original retirement failed to finish')
    def test_activation_keeps_job_and_charge_then_verified_range_reads(self):
        before=self.db.execute('SELECT data FROM jobs').fetchone()[0];charge=self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0]
        plan=self.activate();directory=self.directory();self.assertIsInstance(directory,reads.Directory)
        self.assertTrue(self.apply(plan)['reused']);self.assertTrue(exports.public_copies(self.db,self.env.jobid)[0]['available_for_user_reads'])
        self.assertEqual(document(directory,'artifacts.json')['files'],[{'name':'payload.bin'}])
        with open_artifact(directory,'payload.bin',4*1024**2) as source:
            source.seek(exports.files.CHUNK-3);self.assertEqual(source.read(9),b'x'*9)
            source.seek(-7,os.SEEK_END);self.assertEqual(source.read(),b'x'*7)
        self.assertEqual(self.db.execute('SELECT data FROM jobs').fetchone()[0],before)
        self.assertEqual(self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0],charge)
    def test_retirement_preserves_all_cold_bytes_original_job_and_idempotent_credit(self):
        self.activate();before=self.db.execute('SELECT data FROM jobs').fetchone()[0];old=self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0]
        plan,result=self.retire();self.assertTrue(result['quota_released']);self.assertTrue(result['online_payload_removed'])
        self.assertFalse(self.env.source.exists());self.assertFalse(retention.paths(SimpleNamespace(path=lambda n:self.env.warm/n),self.original)[1].exists())
        with open_artifact(self.directory(),'payload.bin',4*1024**2) as source:self.assertEqual(source.read(),b'x'*(exports.files.CHUNK*3+42))
        new=self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0]
        self.assertLess(new,old);self.assertGreater(new,65536);self.assertEqual(result['released_reserved_bytes'],old-new)
        self.assertTrue(self.apply(plan)['reused']);self.assertEqual(self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0],new)
        self.assertEqual(self.db.execute('SELECT data FROM jobs').fetchone()[0],before)
    def test_resume_each_pending_boundary_with_one_original_operation(self):
        self.activate();plan=self.plan('retire');old=self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0]
        for boundary in ['retire_inventory_synced','retire_prepared','original_renamed','original_purged_step','original_removed','retirement_credited']:
            def stop(stage):
                if stage==boundary:raise InterruptedError(boundary)
            with patch.object(retention,'_checkpoint',side_effect=stop):
                with self.assertRaises(InterruptedError):self.apply(plan,1 if boundary=='original_purged_step' else 256)
            value=reads.record(self.db,self.env.jobid)
            if boundary!='retirement_credited':self.assertEqual(self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0],old)
            with open_artifact(self.directory(),'payload.bin',4*1024**2) as source:self.assertEqual(source.read(2),b'xx')
        self.assertTrue(self.apply(plan)['reused']);self.assertEqual(len(retention.changes(self.db)),2)
    def test_changed_source_blocks_retirement_without_deleting_or_crediting(self):
        self.activate();plan=self.plan('retire');(self.env.source/'payload.bin').write_bytes(b'changed')
        with self.assertRaises((JobError,DeployError)):self.apply(plan)
        self.assertTrue(self.env.source.exists());self.assertEqual(reads.record(self.db,self.env.jobid)[1]['hot_state'],'retained')
    def test_changed_references_prevent_committing_original_retirement(self):
        self.activate();plan=self.plan('retire')
        self.db.execute('CREATE TABLE worker_nodes(profile TEXT)');self.db.execute('INSERT INTO worker_nodes VALUES(?)',(json.dumps({'source':{'job_id':self.env.jobid}}),));self.db.commit()
        with self.assertRaisesRegex(DeployError,'references changed'):self.apply(plan)
        self.assertTrue(self.env.source.exists())
    def test_reference_audit_keeps_grants_graphs_and_sources_without_new_authorization(self):
        self.db.execute('CREATE TABLE source_grants(data TEXT)');self.db.execute('CREATE TABLE job_graphs(record TEXT)')
        grant=json.dumps({'state':'revoked','source_job_id':self.env.jobid,'files':['payload.bin']})
        self.db.execute('INSERT INTO source_grants VALUES(?)',(grant,));self.db.execute('INSERT INTO job_graphs VALUES(?)',(json.dumps({'sources':{self.env.jobid:{'opaque':True}}}),));self.db.commit()
        self.activate();plan,result=self.retire();self.assertEqual(plan['references']['counts']['source_grants'],1)
        self.assertEqual(self.db.execute('SELECT data FROM source_grants').fetchone()[0],grant)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM job_graphs').fetchone()[0],1)
        self.assertEqual(plan['references']['counts']['job_graphs'],1)
    def test_cold_storage_loss_never_falls_back_to_mutated_online_payload(self):
        self.activate();(self.env.source/'payload.bin').write_bytes(b'untrusted fallback')
        target=exports.object_paths(self.env.pool,self.original)[1];hidden=target.with_name(target.name+'-hidden');target.rename(hidden)
        try:
            with self.assertRaises(JobError):self.directory()
        finally:hidden.rename(target)
    def test_range_detects_bit_corruption_even_when_metadata_snapshot_is_unchanged(self):
        self.activate();directory=self.directory();source_path=directory.path/'payload.bin'
        source_path.chmod(0o600)
        with source_path.open('r+b') as file:file.write(b'!')
        source_path.chmod(0o400)
        # Fault model: metadata-only proof cannot detect media corruption. Keep
        # the original expected block hashes while providing current FD metadata.
        current=fs.scan(directory.path);test_directory=reads.Directory(directory.path,self.original['manifest'],current,os.getuid())
        with open_artifact(test_directory,'payload.bin',4*1024**2) as stream:
            with self.assertRaisesRegex(JobError,'checksum'):stream.read(1)
    def test_pending_retirement_blocks_backup_but_cold_reads_remain_valid(self):
        self.activate();plan=self.plan('retire');self.apply(plan,1)
        with self.assertRaisesRegex(DeployError,'retirement'):assert_quiescent(self.env.state,'controller')
        self.assertIsInstance(self.directory(),reads.Directory)
        with self.assertRaises(DeployError):self.plan('retire')
        with self.assertRaisesRegex(DeployError,'code activation'):retention.require_stable_code(self.env.state)
        assert_quiescent(self.env.state,'controller',storage_reconcile=('archive',self.env.jobid))
    def test_restore_retains_complete_authority_then_explicitly_revalidates_reader(self):
        self.activate();self.retire();stage=self.env.base/'restored';stage.mkdir()
        with self.assertRaises(DeployError):retention.require_history(self.env.state,stage)
        with sqlite3.connect(stage/'jobs.sqlite3') as db:self.db.backup(db)
        retention.require_history(self.env.state,stage);retention.freeze_restored(self.env.state);exports.freeze_restored(self.env.state)
        with self.assertRaisesRegex(JobError,'revalidation'):self.directory()
        plan=self.plan('resume');self.apply(plan)
        self.assertIsInstance(self.directory(),reads.Directory)
        self.assertEqual(exports.rows(self.db,'cold_pools')[self.env.pool['id']][0],'restore_frozen')
        with self.assertRaises(DeployError):retention.require_history(self.env.state,stage)
    def test_restored_retained_original_gets_new_byte_verified_inventory_before_deletion(self):
        self.activate();original_hash=reads.record(self.db,self.env.jobid)[1]['source_inventory_sha256']
        staged=self.env.source.with_name(self.env.source.name+'-new');shutil.copytree(self.env.source,staged)
        shutil.rmtree(self.env.source);staged.rename(self.env.source)
        retention.freeze_restored(self.env.state);exports.freeze_restored(self.env.state)
        resumed=self.plan('resume');self.assertNotEqual(resumed['source_inventory_sha256'],original_hash)
        self.apply(resumed);plan,result=self.retire();self.assertTrue(result['online_payload_removed'])
        snapshot=retention.inventory_file(self.env.base/'prefix',plan)
        self.assertEqual(fs.inventory_digest(snapshot),resumed['source_inventory_sha256'])
    def test_restored_retained_original_with_changed_bytes_cannot_gain_new_inventory(self):
        self.activate();retention.freeze_restored(self.env.state);exports.freeze_restored(self.env.state)
        (self.env.source/'payload.bin').write_bytes(b'changed')
        with self.assertRaises(JobError):self.plan('resume')
        self.assertTrue(self.env.source.exists());self.assertEqual(reads.record(self.db,self.env.jobid)[0],'restore_frozen')
    def test_missing_or_changed_private_inventory_never_guesses_after_partial_purge(self):
        self.activate();plan=self.plan('retire');self.apply(plan,1)
        old=self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0]
        path=self.env.base/'prefix/cold-retire-inventories'/(plan['id']+'.json');path.chmod(0o600);path.write_text('{}');path.chmod(0o400)
        with self.assertRaises((JobError,DeployError)):self.apply(plan)
        self.assertEqual(self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0],old)
        self.assertIsInstance(self.directory(),reads.Directory)
    def test_old_code_cannot_forget_read_authority_or_retirement_history(self):
        self.activate()
        with self.assertRaisesRegex(DeployError,'downgrade'):retention.require_compatible(self.env.state,'controller',{'files':{}})
        retention.require_compatible(self.env.state,'controller',{'files':{'cold_reads.py':{},'cold_retention.py':{}}})
    def test_job_semantics_must_preserve_result_owner_and_original_execution(self):
        self.activate();self.env.job['state']='succeeded'
        with self.assertRaisesRegex(JobError,'identity or result'):self.directory()


if __name__=='__main__':unittest.main()

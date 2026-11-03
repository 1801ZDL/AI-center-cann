"""Whole-archive copies, finite reservations and recovery; no cold read switch."""
import copy,json,os,shutil,sqlite3,sys,tempfile,unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import cold_exports as cold
import cold_files as files
import storage_files as fs
from deployment import DeployError
from jobs import JobError
from maintenance import assert_quiescent


class ColdFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.base=Path(self.temp.name)
        self.source=self.base/'source';self.source.mkdir();(self.source/'ir').mkdir()
        (self.source/'ir/kernel.mlir').write_bytes(b'x'*(files.CHUNK+123))
        (self.source/'private.json').write_text('{"fixture":"not a user artifact"}')
        (self.source/'artifacts.json').write_text(json.dumps({'files':[{'name':'ir/kernel.mlir'}]}))
    def tearDown(self):
        for root,dirs,names in os.walk(self.base):
            Path(root).chmod(0o700)
            for name in names:
                path=Path(root)/name
                if not path.is_symlink():path.chmod(0o600)
        self.temp.cleanup()
    def manifest(self):return files.manifest(self.source,4*1024**2,4*1024**2,['private.json'])
    def test_public_and_private_files_preserved_without_expanding_user_index(self):
        value=self.manifest();self.assertEqual(value['indexed'],['ir/kernel.mlir'])
        self.assertEqual(value['private'],['artifacts.json','private.json']);self.assertEqual(len(value['files'][1]['chunks']),2)
        target=self.base/'target';shutil.copytree(self.source,target);files.verify(target,value)
        files.seal(target,value,os.geteuid(),os.getegid());files.verify(target,value,sealed_uid=os.geteuid())
    def test_unknown_private_file_denied_before_content_reads(self):
        with patch.object(files,'checked_file',wraps=files.checked_file) as opened:
            with self.assertRaisesRegex(JobError,'explicitly approve'):files.manifest(self.source,4*1024**2,4*1024**2)
            self.assertEqual([c.args[1]['name'] for c in opened.call_args_list],['artifacts.json'])
    def test_whole_tree_bound_includes_private_evidence_and_original_charge(self):
        with self.assertRaisesRegex(JobError,'original allocation'):files.manifest(self.source,4*1024**2,1024,['private.json'])
        with self.assertRaises(JobError):files.manifest(self.source,True,4096)
    def test_index_symlink_and_special_files_are_not_followed(self):
        (self.source/'artifacts.json').unlink();(self.source/'artifacts.json').symlink_to(self.source/'private.json')
        with patch.object(files,'checked_file') as opened:
            with self.assertRaises(JobError):self.manifest()
            opened.assert_not_called()
    def test_hardlinks_refused(self):
        os.link(self.source/'private.json',self.source/'second')
        with self.assertRaises(JobError):self.manifest()
    def test_changed_source_or_missing_target_never_verifies(self):
        value=self.manifest();(self.source/'ir/kernel.mlir').write_bytes(b'changed')
        with self.assertRaises(JobError):files.unchanged(self.source,value)
        with self.assertRaises(JobError):files.verify(self.source,value)
    def test_missing_unexpected_or_changed_copy_is_detected(self):
        value=self.manifest();target=self.base/'target';shutil.copytree(self.source,target)
        (target/'unexpected').write_bytes(b'new')
        with self.assertRaises(JobError):files.verify(target,value)
        (target/'unexpected').unlink();(target/'private.json').write_bytes(b'!'*(target/'private.json').stat().st_size)
        with self.assertRaisesRegex(JobError,'checksum'):files.verify(target,value)
    def test_validated_prefix_before_discarding_unconfirmed_tail(self):
        value=self.manifest();item=next(f for f in value['files'] if f['name']=='ir/kernel.mlir')
        target=self.base/'target';target.mkdir();(target/'ir').mkdir()
        fd=files.target_fd(target,item['name'],create=True)
        try:
            os.write(fd,b'x'*files.CHUNK+b'unconfirmed');files.prefix(fd,item,files.CHUNK)
            os.pwrite(fd,b'!',0)
            with self.assertRaisesRegex(JobError,'no tail'):files.prefix(fd,item,files.CHUNK)
            self.assertEqual(os.fstat(fd).st_size,files.CHUNK+len(b'unconfirmed'))
        finally:os.close(fd)
    def test_names_and_manifest_types_fail_closed(self):
        value=self.manifest()
        for key,new in [('chunks',None),('size',True),('name',[])]:
            bad=copy.deepcopy(value);bad['files'][0][key]=new
            with self.subTest(key=key),self.assertRaises(JobError):files.validate(bad)
        for key,new in [('private',None),('indexed',[[]]),('bytes',True)]:
            bad={**value,key:new}
            with self.subTest(key=key),self.assertRaises(JobError):files.validate(bad)
        for name in ['../private.json','/private.json','a//b','a/../b']:
            with self.assertRaises(JobError):files.target_fd(self.source,name)
    def test_legacy_inventory_and_missing_partition_refused(self):
        value=self.manifest();bad=copy.deepcopy(value);bad['inventory'].pop('format')
        with self.assertRaises(JobError):files.validate(bad)
        bad=copy.deepcopy(value);bad['private']=[]
        with self.assertRaises(JobError):files.validate(bad)
    def test_root_or_nested_mount_identity_change_refused(self):
        original=fs.mount
        def altered(fd):
            path=os.readlink('/proc/self/fd/'+str(fd))
            return original(fd)+(1 if path.endswith('/ir') else 0)
        with patch.object(fs,'mount',side_effect=altered):
            with self.assertRaisesRegex(JobError,'nested mounts'):self.manifest()


class ColdStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.base=Path(self.temp.name)
        self.state=self.base/'state';self.state.mkdir();self.warm=self.base/'warm';self.warm.mkdir()
        self.cold=self.base/'cold';self.cold.mkdir();(self.cold/'incoming').mkdir();(self.cold/'objects').mkdir()
        self.jobid='job-'+'a'*12;self.job={'id':self.jobid,'owner':'alice','state':'cancelled','source':None}
        self.source=self.warm/'jobs'/self.jobid;self.source.mkdir(parents=True)
        (self.source/'payload.bin').write_bytes(b'x'*(files.CHUNK*3+42))
        (self.source/'artifacts.json').write_text(json.dumps({'files':[{'name':'payload.bin'}]}))
        self.db=sqlite3.connect(self.state/'jobs.sqlite3')
        self.db.executescript('CREATE TABLE jobs(id TEXT,owner TEXT,state TEXT,data TEXT);CREATE TABLE storage_lifecycle(kind TEXT,id TEXT,state TEXT);CREATE TABLE storage_allocations(kind TEXT,id TEXT,owner TEXT,bytes INTEGER,settled INTEGER);')
        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?)',(self.jobid,'alice','cancelled',json.dumps(self.job)))
        self.db.execute('INSERT INTO storage_allocations VALUES(?,?,?,?,?)',('archive',self.jobid,'alice',8*1024**2,1))
        cold.initialize(self.db)
        self.binding={'prefix':str(self.base/'prefix'),'state_dir':str(self.state),'release_id':'a'*64,
            'installation_sha256':'b'*64,'configuration_sha256':'c'*64,'database_device':1,'database_inode':2,'uid':os.getuid(),'gid':os.getgid()}
        self.spec={'storage':{'path':str(self.cold),'filesystem':'ext4','device':1,'root_inode':2,'bytes':128*1024**2,'inodes':2048,'storage_id':'d'*32},
            'budget_bytes':32*1024**2,'budget_inodes':512,'owners':{'alice':{'bytes':16*1024**2,'inodes':256}},'max_job_bytes':4*1024**2,'max_transfers':100}
        self.pool=cold.seal_plan({'format':cold.FORMAT,'kind':'register','binding':self.binding,'spec':self.spec})
        self.db.execute('INSERT INTO cold_pools VALUES(?,?,?)',(self.pool['id'],'registered',json.dumps(self.pool)));self.db.commit()
        payload=SimpleNamespace(base=self.warm,path=lambda name:self.warm/name,object=lambda *a:None)
        @contextmanager
        def context(prefix):
            self.db.execute('BEGIN IMMEDIATE')
            try:yield self.db,self.binding,payload,{'uid':os.getuid(),'gid':os.getgid()}
            except BaseException:self.db.rollback();raise
            else:self.db.commit()
        def commit(db,meta):db.commit();db.execute('BEGIN IMMEDIATE')
        original_move=fs.move
        def move(source,target):
            # Root can rename a sealed directory; this nonroot stand-in must
            # temporarily supply that permission, then restore the same seal.
            sealed=source.is_dir() and source.stat().st_mode&0o777==0o500
            if sealed:source.chmod(0o700)
            try:return original_move(source,target)
            finally:
                if sealed:(target if target.exists() else source).chmod(0o500)
        # State-machine unit tests use the current UID as a stand-in for Root;
        # real Root/service identity and finite ext4 are separate integration.
        self.patches=[patch.object(cold,'context',context),patch.object(cold,'pool_check'),patch.object(cold,'commit',commit),patch.object(cold,'ADMIN_UID',os.getuid()),patch.object(fs,'move',side_effect=move)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.close()
        for root,dirs,names in os.walk(self.base):
            Path(root).chmod(0o700)
            for name in names:(Path(root)/name).chmod(0o600)
        self.temp.cleanup()
    def plan(self):return cold.copy_plan(self.base/'prefix',self.jobid)
    def finish(self,plan):
        for _ in range(12):
            if cold.copy_step(self.base/'prefix',plan,files.CHUNK)['state']=='verified':return
        self.fail('Bounded same-plan copy did not finish')
    def test_multistep_copy_preserves_source_accounting_and_only_publishes_after_hashes(self):
        plan=self.plan();original=fs.inventory_digest(fs.scan(self.source));cold.check_plan(plan,'copy')
        result=cold.copy_step(self.base/'prefix',plan,files.CHUNK);self.assertEqual(result['state'],'copying')
        self.assertTrue(cold.pinned(self.db,self.jobid));self.assertFalse(cold.object_paths(self.pool,plan)[1].exists())
        for _ in range(8):
            if result['state']=='verified':break
            result=cold.copy_step(self.base/'prefix',plan,files.CHUNK)
        self.assertEqual(result['state'],'verified');self.assertFalse(cold.pinned(self.db,self.jobid))
        self.assertEqual(fs.inventory_digest(fs.scan(self.source)),original)
        self.assertEqual(self.db.execute('SELECT bytes FROM storage_allocations').fetchone()[0],8*1024**2)
        self.assertTrue(cold.verify_copy(self.base/'prefix',plan)['copy_bytes_verified'])
        self.assertTrue(cold.copy_step(self.base/'prefix',plan)['reused'])
        status=cold.public_copies(self.db,self.jobid);self.assertFalse(status[0]['available_for_user_reads'])
        for key in ['quota_released','history_capacity_released','read_pointer_changed']:self.assertFalse(status[0][key])
        self.assertNotIn('manifest',status[0]);self.assertNotIn(str(self.cold),json.dumps(status))
        with self.assertRaisesRegex(DeployError,'original plan'):self.plan()
    def test_interrupted_chunk_and_committed_progress_resume_same_original_plan(self):
        plan=self.plan();boundaries=[]
        for boundary in ['reserved','data_synced','progress_committed']:
            def fail(stage):
                if stage==boundary:boundaries.append(stage);raise InterruptedError('test simulated boundary')
            with patch.object(cold,'_checkpoint',side_effect=fail):
                with self.assertRaises(InterruptedError):cold.copy_step(self.base/'prefix',plan,files.CHUNK)
            self.assertEqual(len(cold.rows(self.db,'cold_transfers')),1)
        self.finish(plan)
        self.assertEqual(boundaries,['reserved','data_synced','progress_committed']);cold.verify_copy(self.base/'prefix',plan)
    def test_stale_binding_source_or_reservation_cannot_resume(self):
        plan=self.plan();bad=copy.deepcopy(plan);bad['binding']['database_inode']+=1;bad.pop('id');cold.seal_plan(bad)
        with self.assertRaisesRegex(DeployError,'stale'):cold.copy_step(self.base/'prefix',bad)
        bad=copy.deepcopy(plan);bad['reserved_bytes']=1;bad.pop('id');cold.seal_plan(bad)
        with self.assertRaisesRegex(DeployError,'copy cost'):cold.copy_step(self.base/'prefix',bad)
        (self.source/'payload.bin').write_bytes(b'modified')
        with self.assertRaises(JobError):cold.copy_step(self.base/'prefix',plan)
        self.assertFalse(cold.rows(self.db,'cold_transfers'))
    def test_owner_global_byte_inode_and_history_bounds_preserve_original(self):
        plan=self.plan()
        for scope,key in [('owner','bytes'),('owner','inodes'),('all','bytes'),('all','inodes')]:
            pool=copy.deepcopy(self.pool)
            if scope=='owner':pool['spec']['owners']['alice'][key]=1
            else:pool['spec']['budget_'+key]=1
            with self.subTest(scope=scope,key=key),self.assertRaisesRegex(DeployError,'budget exceeded'):cold.reserve(self.db,pool,plan)
        pool=copy.deepcopy(self.pool);pool['spec']['max_transfers']=0
        with self.assertRaisesRegex(DeployError,'history is full'):cold.reserve(self.db,pool,plan)
        self.assertTrue(self.source.exists());self.assertFalse(cold.rows(self.db,'cold_transfers'))
    def test_normal_maintenance_blocked_pending_but_metadata_recovery_can_progress(self):
        plan=self.plan();cold.copy_step(self.base/'prefix',plan,files.CHUNK)
        with self.assertRaisesRegex(DeployError,'cold copy'):assert_quiescent(self.state,'controller')
        assert_quiescent(self.state,'controller',cold_export=True)
        assert_quiescent(self.state,'controller',storage_reconcile=('archive',self.jobid))
        self.db.execute('DELETE FROM cold_transfers');self.db.execute('UPDATE cold_pools SET state="registering"');self.db.commit()
        with self.assertRaisesRegex(DeployError,'cold registration'):assert_quiescent(self.state,'controller')
    def test_active_or_unknown_jobs_still_prevent_cold_maintenance(self):
        for status in ['queued','running','needs_attention']:
            self.db.execute('UPDATE jobs SET state=?,data=?',(status,json.dumps({**self.job,'state':status})));self.db.commit()
            with self.subTest(status=status),self.assertRaisesRegex(DeployError,'queued or active'):assert_quiescent(self.state,'controller',cold_export=True)
    def test_old_restore_cannot_drop_or_rewrite_copies_and_latest_freezes(self):
        plan=self.plan()
        self.finish(plan)
        stage=self.base/'restore';stage.mkdir()
        with self.assertRaisesRegex(DeployError,'lose cold'):cold.require_history(self.state,stage)
        with sqlite3.connect(stage/'jobs.sqlite3') as db:self.db.backup(db)
        cold.require_history(self.state,stage);cold.freeze_restored(self.state)
        record=cold.rows(self.db,'cold_transfers')[plan['id']][1];self.assertIsNotNone(record['verified_at'])
        with self.assertRaisesRegex(DeployError,'frozen'):cold.copy_step(self.base/'prefix',plan)
        self.assertTrue(cold.verify_copy(self.base/'prefix',plan)['historical_only'])
        cold.require_history(self.state,stage)
        with sqlite3.connect(stage/'jobs.sqlite3') as db:db.execute('DELETE FROM cold_transfers')
        with self.assertRaises(DeployError):cold.require_history(self.state,stage)
    def test_missing_compatible_helpers_refuse_downgrade_and_empty_install_is_supported(self):
        with self.assertRaisesRegex(DeployError,'downgrade'):cold.require_compatible(self.state,'controller',{'files':{}})
        cold.require_compatible(self.state,'controller',{'files':{'cold_files.py':'x','cold_exports.py':'x'}})
        empty=self.base/'empty';empty.mkdir();cold.require_history(empty,empty);cold.freeze_restored(empty)
        self.assertFalse((empty/'jobs.sqlite3').exists())
    def test_policy_and_plan_reject_negative_boolean_or_unbounded_fields(self):
        cold.policy(self.spec)
        for key,new in [('budget_bytes',True),('max_transfers',10001),('budget_inodes',2048)]:
            with self.subTest(key=key),self.assertRaises(DeployError):cold.policy({**self.spec,key:new})
        plan=self.plan();plan['reserved_bytes']=-1;plan.pop('id');cold.seal_plan(plan)
        with self.assertRaises(DeployError):cold.check_plan(plan,'copy')


if __name__=='__main__':unittest.main()

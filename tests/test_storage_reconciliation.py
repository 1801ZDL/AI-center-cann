"""Legacy approval preservation, partial purge, stale plans and restore fencing."""
import copy,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from deployment import DeployError
from jobs import JobError
from maintenance import assert_quiescent,require_features
import storage_files as fs
import storage_inventory as inv
import storage_reconciliation as repair


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name)
        self.identifier='input-'+'a'*24;self.target=self.root/'inputs'/self.identifier;self.target.mkdir(parents=True)
        (self.target/'payload').write_bytes(b'original')
        self.old=fs.scan(self.target);self.old.pop('format')
        self.old['root']['mount']+=100000
        for row in self.old['entries']:row['mount']+=100000
        self.record={'kind':'input','id':self.identifier,'owner':'alice','state':'moving_to_trash','revision':1,
            'request_sha256':'a'*64,'operation':'trash','inventory':self.old,'fingerprint':fs.digest(self.old),
            'payload_bytes':self.old['bytes'],'allocation_before':99999}
    def tearDown(self):self.temp.cleanup()
    def seal(self,value):
        result={**value};result.pop('id',None);result['id']=fs.digest(result);return result
    def plan(self):
        return self.seal({'format':repair.FORMAT,'binding':{},'target':{'kind':'input','id':self.identifier},
            'original':self.record,'allocation':['alice',99999,1],'observation':repair.observe(self.root,self.record)})
    def database(self,path=None):
        path=path or self.root/'jobs.sqlite3';db=sqlite3.connect(path)
        db.executescript('CREATE TABLE jobs(id TEXT,state TEXT,data TEXT);CREATE TABLE storage_lifecycle(kind TEXT,id TEXT,owner TEXT,state TEXT,revision INTEGER,record TEXT);')
        db.execute('INSERT INTO storage_lifecycle VALUES(?,?,?,?,?,?)',('input',self.identifier,'alice','moving_to_trash',1,json.dumps(self.record)))
        db.commit();return db

    def test_full_rebind_preserves_original_approval_and_all_nonmount_identity(self):
        v=repair.observe(self.root,self.record)
        self.assertEqual(v['location'],'source');self.assertEqual(inv.persistent(v['replacement']),inv.persistent(repair.legacy(self.record)))
        self.assertNotEqual(v['replacement']['root']['mount'],self.old['root']['mount'])
        self.assertEqual(self.record['inventory'],self.old);self.assertEqual((self.target/'payload').read_bytes(),b'original')

    def test_already_renamed_pending_trash_and_restore_use_confirmed_location(self):
        trash=self.root/'storage-trash/input'/self.identifier;trash.parent.mkdir(parents=True);self.target.rename(trash)
        self.assertEqual(repair.observe(self.root,self.record)['location'],'trash')
        r={**self.record,'state':'restoring','operation':'restore'}
        self.assertEqual(repair.observe(self.root,r)['location'],'trash')
        trash.rename(self.target);self.assertEqual(repair.observe(self.root,r)['location'],'source')

    def test_both_or_missing_locations_never_authorize_a_guess(self):
        trash=self.root/'storage-trash/input'/self.identifier;trash.mkdir(parents=True)
        with self.assertRaises(DeployError):repair.observe(self.root,self.record)
        trash.rmdir();(self.target/'payload').unlink();self.target.rmdir()
        with self.assertRaises(DeployError):repair.observe(self.root,self.record)

    def test_changed_content_or_mode_cannot_be_normalized_as_mount_only(self):
        (self.target/'payload').write_bytes(b'modified')
        # Some backing filesystems expose coarse timestamps for rapid writes;
        # this contract checks recorded metadata, not historical byte hashes.
        os.utime(self.target/'payload',ns=(0,self.old['entries'][0]['mtime']+1000000))
        with self.assertRaises(DeployError):repair.observe(self.root,self.record)
        with self.assertRaises(DeployError):repair.compare(repair.legacy(self.record),{**fs.scan(self.target),'root':{**fs.scan(self.target)['root'],'ino':1}})

    def test_original_fingerprint_payload_billing_and_single_mount_are_required(self):
        for bad in [{**self.record,'fingerprint':'b'*64},{**self.record,'payload_bytes':0},{**self.record,'revision':True},
                    {**self.record,'operation':'restore'},{**self.record,'request_sha256':'invalid'}]:
            with self.subTest(bad=bad),self.assertRaises((DeployError,JobError)):repair.legacy(bad)
        bad=copy.deepcopy(self.record);bad['inventory']['entries'][0]['mount']+=1;bad['fingerprint']=fs.digest(bad['inventory'])
        with self.assertRaises(JobError):repair.legacy(bad)

    def test_v2_and_completed_inventory_do_not_gain_new_approval(self):
        for bad in [{**self.record,'state':'trash'},{**self.record,'inventory':fs.scan(self.target)}]:
            with self.assertRaises(DeployError):repair.legacy(bad)

    def test_partial_purge_retains_full_original_inventory_and_billing(self):
        (self.target/'second').write_bytes(b'second');old=fs.scan(self.target);old.pop('format')
        trash=self.root/'storage-trash/input'/self.identifier;trash.parent.mkdir(parents=True);self.target.rename(trash)
        done,n=fs.purge(trash,old,budget=1);self.assertFalse(done)
        old['root']['mount']+=100000
        for entry in old['entries']:entry['mount']+=100000
        r={**self.record,'state':'purging','operation':'purge','inventory':old,'fingerprint':fs.digest(old),'payload_bytes':old['bytes']}
        v=repair.observe(self.root,r)
        self.assertEqual(len(v['replacement']['entries']),2);self.assertEqual(len(v['current']['entries']),1)
        self.assertEqual(v['replacement']['bytes'],len(b'originalsecond'));self.assertEqual(r['payload_bytes'],old['bytes'])
        done,n=fs.purge(trash,v['replacement']);self.assertTrue(done)

    def test_partial_purge_cannot_adopt_new_files_or_replaced_existing_files(self):
        trash=self.root/'storage-trash/input'/self.identifier;trash.parent.mkdir(parents=True);self.target.rename(trash)
        r={**self.record,'state':'purging','operation':'purge'}
        (trash/'extra').write_bytes(b'extra')
        with self.assertRaises(DeployError):repair.observe(self.root,r)

    def test_plan_digest_and_exact_original_replacement_cannot_drift(self):
        value=self.plan();repair.check(value)
        bad=copy.deepcopy(value);bad['original']['owner']='bob'
        with self.assertRaises(DeployError):repair.check(bad)
        bad=copy.deepcopy(value);bad['observation']['replacement']['entries'][0]['ino']+=1;bad=self.seal(bad)
        with self.assertRaises(DeployError):repair.check(bad)

    def test_metadata_repair_exemption_leaves_ordinary_quiescence_strict(self):
        db=self.database();db.close()
        with self.assertRaises(DeployError):assert_quiescent(self.root,'controller')
        assert_quiescent(self.root,'controller',storage_reconcile=('input',self.identifier))
        # The helper separately validates the selected record; this narrowly
        # skips the storage-pending part of global quiescence, not other gates.
        with self.assertRaises(DeployError):assert_quiescent(self.root,'node',storage_reconcile=('input',self.identifier))

    def test_active_jobs_and_holds_block_but_peer_pending_does_not_deadlock_metadata_repair(self):
        db=self.database();db.execute('INSERT INTO jobs VALUES(?,?,?)',('job-'+'c'*12,'running',json.dumps({'state':'running'})));db.commit()
        with self.assertRaises(DeployError):assert_quiescent(self.root,'controller',storage_reconcile=('input',self.identifier))
        db.execute('DELETE FROM jobs');db.execute('CREATE TABLE worker_inventory_holds(held INTEGER)');db.execute('INSERT INTO worker_inventory_holds VALUES(1)');db.commit()
        with self.assertRaises(DeployError):assert_quiescent(self.root,'controller',storage_reconcile=('input',self.identifier))
        db.execute('DELETE FROM worker_inventory_holds');db.execute('INSERT INTO storage_lifecycle VALUES(?,?,?,?,?,?)',('input','input-'+'b'*24,'bob','purging',2,'{}'));db.commit();db.close()
        assert_quiescent(self.root,'controller',storage_reconcile=('input',self.identifier))
        with sqlite3.connect(self.root/'jobs.sqlite3') as check:
            self.assertEqual(check.execute('SELECT record FROM storage_lifecycle WHERE id=?',('input-'+'b'*24,)).fetchone()[0],'{}')
        with self.assertRaises(DeployError):assert_quiescent(self.root,'controller')

    def test_rows_and_original_operation_must_agree(self):
        db=self.database();self.assertEqual(repair.record(db,('input',self.identifier)),self.record)
        db.execute('UPDATE storage_lifecycle SET owner="bob"')
        with self.assertRaises(DeployError):repair.record(db,('input',self.identifier))
        db.close()

    def test_old_backup_or_incompatible_code_cannot_discard_reconciliation_receipt(self):
        other=self.root/'other';other.mkdir();self.database().close();self.database(other/'jobs.sqlite3').close()
        receipt={'format':repair.FORMAT,'plan_id':'a'*64}
        with sqlite3.connect(self.root/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE storage_reconciliations(id TEXT PRIMARY KEY,record TEXT)');db.execute('INSERT INTO storage_reconciliations VALUES(?,?)',('a'*64,json.dumps(receipt)))
        with self.assertRaises(DeployError):repair.require_history(self.root,other)
        with self.assertRaises(DeployError):require_features(self.root,'controller',{'files':{}})
        repair.require_compatible(self.root,'controller',{'files':{'storage_reconciliation.py':{}}})
        with sqlite3.connect(other/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE storage_reconciliations(id TEXT PRIMARY KEY,record TEXT)');db.execute('INSERT INTO storage_reconciliations VALUES(?,?)',('a'*64,json.dumps(receipt)))
        repair.require_history(self.root,other)

    def test_non_root_plan_cannot_open_or_mutate_an_installation(self):
        with patch('storage_reconciliation.deploy.require_root',side_effect=DeployError('root required')):
            with self.assertRaises(DeployError):repair.plan('/does/not/exist','input',self.identifier)

    def test_never_started_controller_without_job_database_has_no_receipts_to_lose(self):
        other=self.root/'empty';other.mkdir()
        repair.require_history(self.root,other)
        self.assertFalse((self.root/'jobs.sqlite3').exists());self.assertFalse((other/'jobs.sqlite3').exists())
        with sqlite3.connect(self.root/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE storage_reconciliations(id TEXT PRIMARY KEY,record TEXT)')
            db.execute('INSERT INTO storage_reconciliations VALUES(?,?)',('a'*64,json.dumps({'format':repair.FORMAT,'plan_id':'a'*64})))
        with self.assertRaises(DeployError):repair.require_history(self.root,other)


if __name__=='__main__':unittest.main()

"""Existing HTTP reports, dependency copier and budget grants over cold bytes.

Execution receipts here are the existing protocol fixtures, not NPU or compiler
executions. Cold registration is a metadata fixture; native ext4 is tested apart.
"""
import hashlib,json,os,shutil,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import cold_exports as exports
import cold_files as files
import cold_reads as reads
import storage_files as fs
from jobs import atomic_json,JobError
from lab_client import ClientHTTPError
import test_dependencies as dependency_fixture
import test_report_view as report_fixture
import test_source_grants as grant_fixture


def materialize(jobs,key):
    source=jobs.directory(key);job=jobs.get(key);pool_path=jobs.runtime/'unit-cold';pool_path.mkdir()
    (pool_path/'incoming').mkdir();(pool_path/'objects').mkdir()
    binding={'prefix':str(jobs.runtime/'unit-prefix'),'state_dir':str(jobs.runtime),'release_id':'a'*64,'installation_sha256':'b'*64,
        'configuration_sha256':'c'*64,'database_device':jobs.db_path.stat().st_dev,'database_inode':jobs.db_path.stat().st_ino,'uid':os.getuid(),'gid':os.getgid()}
    spec={'storage':{'path':str(pool_path),'filesystem':'ext4','device':pool_path.stat().st_dev,'root_inode':pool_path.stat().st_ino,'bytes':128*1024**2,'inodes':2048,'storage_id':'d'*32},
        'budget_bytes':32*1024**2,'budget_inodes':512,'owners':{job['owner']:{'bytes':16*1024**2,'inodes':256}},'max_job_bytes':8*1024**2,'max_transfers':100}
    pool=exports.seal_plan({'format':exports.FORMAT,'kind':'register','binding':binding,'spec':spec})
    with jobs.connect() as db:allocation=list(db.execute('SELECT owner,bytes,settled FROM storage_allocations WHERE kind="archive" AND id=?',(key,)).fetchone())
    explicit=[e['name'] for e in fs.scan(source)['entries'] if not __import__('stat').S_ISDIR(e['mode'])]
    manifest=files.manifest(source,8*1024**2,allocation[1],explicit)
    cost,inodes=exports.copy_cost(pool,manifest)
    original=exports.seal_plan({'format':exports.FORMAT,'kind':'copy','binding':binding,'pool_id':pool['id'],'pool_sha256':fs.digest(pool),'job_id':key,'owner':job['owner'],
        'job_sha256':fs.digest(job),'allocation':allocation,'manifest':manifest,'reserved_bytes':cost,'reserved_inodes':inodes})
    target=pool_path/'objects'/original['id'];target.mkdir();shutil.copytree(source,target/'data')
    files.seal(target/'data',manifest,os.getuid(),os.getgid())
    atomic_json(target/'control.json',original);atomic_json(target/'receipt.json',{'format':exports.FORMAT,'id':original['id'],'plan_sha256':fs.digest(original),'manifest_sha256':fs.digest(manifest)})
    for name in ['control.json','receipt.json']:(target/name).chmod(0o400)
    target.chmod(0o500);signature=fs.inventory_digest(fs.scan(target/'data'))
    transfer={'id':original['id'],'pool_id':pool['id'],'job_id':key,'owner':job['owner'],'plan_sha256':fs.digest(original),
        'reserved_bytes':cost,'reserved_inodes':inodes,'copied_bytes':manifest['bytes'],'file':len(manifest['files']),'offset':0,'identity':None,'creating':False,
        'verified_at':time.time(),'target_inventory_sha256':signature}
    authority={'format':reads.FORMAT,'copy_id':original['id'],'pool_id':pool['id'],'job_id':key,'owner':job['owner'],
        'copy_plan_sha256':fs.digest(original),'target_inventory_sha256':signature,'job_semantic_sha256':reads.semantic(job),
        'hot_state':'retired','retained_control_bytes':allocation[1]}
    with jobs.transaction() as db:
        exports.initialize(db);reads.initialize(db)
        db.execute('INSERT INTO cold_pools VALUES(?,?,?)',(pool['id'],'registered',json.dumps(pool)))
        db.execute('INSERT INTO cold_transfers VALUES(?,?,?,?,?,?)',(original['id'],pool['id'],key,job['owner'],'verified',json.dumps(transfer)))
        db.execute('INSERT INTO cold_readers VALUES(?,?,?)',(key,'retired',json.dumps(authority)))
    shutil.rmtree(source)
    return pool_path


def writable(root):
    for parent,dirs,names in os.walk(root):
        Path(parent).chmod(0o700)
        for name in names:(Path(parent)/name).chmod(0o600)


class ColdConsumerTests(unittest.TestCase):
    def test_all_four_report_http_readers_keep_original_hashes_and_foreign_acl(self):
        case=report_fixture.ReportViewTests();case.setUp();pool=None
        try:
            expected={kind:case.publish(kind) for kind in case.values}
            original=case.base.jobs.get(case.identifier);warm_manifest=case.client.request('/api/jobs/'+case.identifier+'/archive-manifest')
            pool=materialize(case.base.jobs,case.identifier)
            with patch.object(exports,'pool_check'):
                for kind,raw in expected.items():
                    viewed=case.view(kind);self.assertEqual(viewed['report']['sha256'],hashlib.sha256(raw).hexdigest())
                    self.assertTrue(viewed['verification']['report_bytes_verified'])
                self.assertEqual(case.client.request('/api/jobs/'+case.identifier+'/archive-manifest'),warm_manifest)
                with self.assertRaises(ClientHTTPError) as denied:case.view('simulation',case.base.clients['bob'])
                self.assertEqual(denied.exception.status,404)
                self.assertEqual(case.base.jobs.get(case.identifier),original)
        finally:
            if pool:writable(pool)
            case.tearDown()
    def test_dependency_handoff_consumes_cold_source_without_reexecuting_parent(self):
        case=dependency_fixture.DependencyTests();case.setUp();pool=None
        try:
            parent=case.parent();item=case.complete_parent(parent,b'cold parent artifact')
            before=case.jobs.get(parent['id']);pool=materialize(case.jobs,parent['id'])
            with patch.object(exports,'pool_check'):
                child=case.child(parent);actual=case.ready(child)
                self.assertEqual(actual['dependency_state']['status'],'ready')
                self.assertEqual((case.jobs.inputs.directory(actual['input']['id'])/'deps/package.bin').read_bytes(),b'cold parent artifact')
                self.assertEqual(actual['input_provenance']['artifacts'][0]['sha256'],item['sha256'])
                self.assertEqual(case.jobs.get(parent['id']),before)
                self.assertFalse(case.jobs.directory(parent['id']).exists())
        finally:
            if pool:writable(pool)
            case.tearDown()
    def test_file_scoped_budget_grant_reads_cold_bytes_without_granting_source_visibility(self):
        case=grant_fixture.SourceGrantTests();case.setUp();pool=None
        try:
            original=case.jobs.get(case.parent['id']);pool=materialize(case.jobs,case.parent['id'])
            with patch.object(exports,'pool_check'):
                child,_=case.consumer();ready=case.b.ready(child)
                self.assertEqual(ready['dependency_state']['status'],'ready')
                self.assertEqual((case.jobs.inputs.directory(ready['input']['id'])/'deps/source.bin').read_bytes(),b'actual prior failure')
                with self.assertRaises(Exception):case.jobs.get(case.parent['id'],principal=case.agent)
                case.grants.revoke(case.owner,case.grant['id'])
                replayed,_=case.consumer();self.assertEqual(replayed['id'],child['id'])
                with self.assertRaises(JobError):case.consumer(key='revoked-new-consumer')
                self.assertEqual(case.jobs.get(case.parent['id']),original)
        finally:
            if pool:writable(pool)
            case.tearDown()


if __name__=='__main__':unittest.main()

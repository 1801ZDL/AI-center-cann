"""Cold bodies/bytes retain current grants and measured references.

Only the physical ext4/Root identity context is replaced in these unit cases;
copy/retirement/control journals and current authorization execute real code.
The installed two-role acceptance separately runs actual CPU source tasks.
"""
import copy,json,os,sys,tempfile,unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import cold_exports as e
import cold_retention as t
import control_history as h
import control_rotation as r
import history_capacity as c
import storage_files as fs
import test_source_grants as source
from test_cold_consumers import writable
from jobs import JobError


class ColdControlFixture:
    def __init__(self,jobs):
        self.jobs=jobs;self.sidecar=tempfile.TemporaryDirectory(dir=jobs.runtime.parent,prefix='cold-control-sidecar-')
        self.prefix=Path(self.sidecar.name)/'prefix';self.prefix.mkdir(mode=0o700)
        self.pool=Path(self.sidecar.name)/'pool';self.pool.mkdir(mode=0o700)
        (self.pool/'incoming').mkdir();(self.pool/'objects').mkdir()
        self.binding={'prefix':str(self.prefix),'state_dir':str(jobs.runtime),'release_id':'a'*64,
            'installation_sha256':'b'*64,'configuration_sha256':'c'*64,
            'database_device':jobs.db_path.stat().st_dev,'database_inode':jobs.db_path.stat().st_ino,'uid':os.getuid(),'gid':os.getgid()}
        self.meta={'uid':os.getuid(),'gid':os.getgid(),'state_dir':str(jobs.runtime)}
        @contextmanager
        def context(prefix,**kwargs):
            with jobs.transaction() as db:yield db,dict(self.binding),jobs.payload,self.meta
        @contextmanager
        def control_context(prefix):
            with jobs.transaction() as db:yield db,dict(self.binding),self.meta
        def commit(db,meta):db.commit();db.execute('BEGIN IMMEDIATE')
        original_move=fs.move
        def move(source,target):
            sealed=source.is_dir() and source.stat().st_mode&0o777==0o500
            if sealed:source.chmod(0o700)
            try:return original_move(source,target)
            finally:
                if sealed:(target if target.exists() else source).chmod(0o500)

        self.patches=[patch.object(fs,'move',side_effect=move),patch.object(e,'context',context),patch.object(e,'commit',commit),patch.object(e,'pool_check'),
            patch.object(e,'ADMIN_UID',os.getuid()),patch.object(t,'ADMIN_UID',os.getuid()),patch.object(t,'matching'),
            patch.object(h,'context',control_context),patch.object(h,'ADMIN_UID',os.getuid()),
            patch.object(r,'context',control_context),patch.object(c,'context',control_context)]
        for item in self.patches:item.start()
        policy=c.plan(self.prefix,100,100);c.apply(self.prefix,policy);jobs.history=c.Capacity(jobs)
        s=self.pool.stat();spec={'storage':{'path':str(self.pool),'filesystem':'ext4','device':s.st_dev,'root_inode':s.st_ino,
            'bytes':128*1024**2,'inodes':4096,'storage_id':'d'*32},'budget_bytes':32*1024**2,'budget_inodes':1024,
            'owners':{j['owner']:{'bytes':16*1024**2,'inodes':512} for j in jobs.all()},'max_job_bytes':8*1024**2,'max_transfers':32}
        # Registration's native filesystem/Root evidence is supplied separately by
        # the installed acceptance; this unit uses an explicit registered record.
        registered=e.seal_plan({'format':e.FORMAT,'kind':'register','binding':self.binding,'spec':spec})
        with jobs.transaction() as db:
            e.initialize(db);db.execute('INSERT INTO cold_pools VALUES(?,?,?)',(registered['id'],'registered',json.dumps(registered)))
    def migrate(self,key):
        copied=e.copy_plan(self.prefix,key)
        for _ in range(32):
            if e.copy_step(self.prefix,copied)['state']=='verified':break
        else:raise AssertionError('Original cold copy did not converge')
        t.apply(self.prefix,t.plan(self.prefix,copied,'activate'))
        retired=t.plan(self.prefix,copied,'retire')
        for _ in range(32):
            if not t.apply(self.prefix,retired).get('pending'):break
        else:raise AssertionError('Original retirement did not converge')
        plan=h.plan(self.prefix,[key],{'bytes':16*1024**2,'exports':8})
        for _ in range(32):
            if h.step(self.prefix,plan)['state']=='verified':break
        else:raise AssertionError('Original private control copy did not converge')
        rotation=r.plan(self.prefix,plan['id']);r.apply(self.prefix,rotation)
        return {'copy':copied,'control':plan,'rotation':rotation}
    def close(self):
        for item in reversed(getattr(self,'patches',[])):item.stop()
        if hasattr(self,'jobs'):writable(self.jobs.runtime)
        if hasattr(self,'sidecar'):
            writable(Path(self.sidecar.name));self.sidecar.cleanup()


class ColdControlTests(unittest.TestCase):
    def setUp(self):
        self.env=source.SourceGrantTests();self.env.setUp();self.addCleanup(self.env.tearDown);self.jobs=self.env.jobs
        self.cold=ColdControlFixture.__new__(ColdControlFixture);self.addCleanup(self.cold.close)
        self.cold.__init__(self.jobs);self.original=self.jobs.get(self.env.parent['id'])
        self.plans=self.cold.migrate(self.original['id'])
    def test_active_grant_consumes_migrated_body_and_cold_bytes_with_original_provenance(self):
        child,_=self.env.consumer();ready=self.env.b.ready(child)
        self.assertEqual(ready['dependency_state']['status'],'ready')
        self.assertEqual((self.jobs.inputs.directory(ready['input']['id'])/'deps/source.bin').read_bytes(),b'actual prior failure')
        self.assertEqual(ready['input_provenance']['terminal_sources'][self.original['id']]['attempt_id'],self.original['attempt_id'])
        self.assertEqual(self.jobs.get(self.original['id']),self.original)
        self.assertFalse(self.jobs.directory(self.original['id']).exists())
        with self.assertRaises(JobError) as denied:self.jobs.get(self.original['id'],principal=self.env.agent)
        self.assertEqual(denied.exception.status,404)
    def test_revocation_is_live_despite_immutable_copy_of_former_active_grant(self):
        child,request=self.env.consumer();self.env.b.ready(child)
        self.env.grants.revoke(self.env.owner,self.env.grant['id'])
        replay,reused=self.jobs.submit(request,self.env.agent);self.assertTrue(reused);self.assertEqual(replay['id'],child['id'])
        with self.assertRaises(JobError):self.env.consumer(key='revoked-new')
        with self.jobs.connect() as db:
            self.assertEqual(self.env.budgets.get(db,self.env.new['id'],self.env.owner)['charged']['jobs'],1)
            exported=h.rows(db)[self.plans['control']['id']][1]
        body,_=h.checked(self.cold.prefix,exported);rows=body['tables']['source_grants']['rows'];columns=body['tables']['source_grants']['columns']
        row=next(v for v in rows if v[columns.index('id')]==self.env.grant['id'])
        self.assertEqual(json.loads(row[columns.index('data')])['state'],'active')
        self.assertEqual(self.jobs.get(self.original['id']),self.original)
    def test_new_grant_after_migration_is_exactly_file_scoped_and_does_not_reexecute_source(self):
        spec={**self.env.spec,'idempotency_key':'after-migration'}
        approved=self.env.grants.create(self.env.owner,self.env.grants.plan(self.env.owner,spec))[0]
        child,_=self.env.consumer(grant=approved);self.assertEqual(self.env.b.ready(child)['dependency_state']['status'],'ready')
        with self.assertRaises(JobError):self.env.consumer(grant=approved,key='wrong-sha',item={'sha256':'0'*64})
        self.assertEqual(self.jobs.get(self.original['id'])['attempt_id'],self.original['attempt_id'])
    def test_restored_grants_and_cold_reader_stay_frozen_until_separate_approvals(self):
        from source_grants import rebind_restored as freeze_grants
        from experiment_budget import rebind_restored as freeze_budgets
        freeze_grants(self.jobs.runtime);freeze_budgets(self.jobs.runtime);t.freeze_restored(self.jobs.runtime);e.freeze_restored(self.jobs.runtime);h.freeze_restored(self.jobs.runtime)
        self.assertEqual(self.jobs.get(self.original['id']),self.original)
        with self.assertRaises(JobError):self.jobs.archive_directory(self.original['id'])
        with self.jobs.connect() as db:self.assertEqual(self.env.grants.get(db,self.env.grant['id'],self.env.owner)['state'],'restored_frozen')
        t.apply(self.cold.prefix,t.plan(self.cold.prefix,self.plans['copy'],'resume'))
        self.assertEqual(self.jobs.get(self.original['id']),self.original)
        with self.assertRaises(JobError):self.env.consumer()


class ColdReferenceTests(unittest.TestCase):
    def test_cold_migrated_reference_and_independent_sample_keep_current_identity_fence(self):
        import test_performance_reference as reference_fixture
        fixture=reference_fixture.ReferenceTests('runTest');self.addCleanup(fixture.doCleanups);fixture.setUp()
        fixture.collect();fixture.create();fixture.sample(26)
        before=fixture.compare();self.assertEqual(before['status'],'within_reference_envelope');self.assertFalse(before['performance_qualified'])
        cold=ColdControlFixture.__new__(ColdControlFixture);self.addCleanup(cold.close);cold.__init__(fixture.jobs)
        originals={key:fixture.jobs.get(key) for key in fixture.sources}
        for key in fixture.sources:cold.migrate(key)
        self.assertEqual(fixture.compare(),before)
        self.assertTrue(all(fixture.jobs.get(key)==value for key,value in originals.items()))
        self.assertTrue(all(not fixture.jobs.directory(key).exists() for key in fixture.sources))
        fixture.jobs.access.disable_user('alice');fixture.jobs.access.disable_user('alice',False)
        self.assertNotEqual(fixture.compare()['status'],'within_reference_envelope')


class NativeReadinessTests(unittest.TestCase):
    def role(self,kind):
        from unittest.mock import Mock
        from native_controller_fixture import Controller
        value=Controller(Mock(),'sha256:'+'a'*64,Path('fixed-test-boot.py'),role=kind);value.container='owned-fixture'
        value.cli=Mock(return_value={'running':True,'ready':True})
        value.reconcile=Mock(side_effect=[None,{'state':'waiting-for-heartbeat'},{'state':'ready'}])
        return value
    def test_node_visible_ready_must_finish_the_original_startup_receipt_before_returning(self):
        value=self.role('node')
        with patch('native_controller_fixture.time.sleep'):self.assertTrue(value.start()['ready'])
        self.assertEqual(value.reconcile.call_count,3)
        value.cli.assert_called_with('unit-manager-check','--running')
    def test_controller_without_required_nodes_must_finish_its_receipt_before_admin(self):
        value=self.role('controller')
        with patch('native_controller_fixture.time.sleep'):self.assertTrue(value.start()['ready'])
        self.assertEqual(value.reconcile.call_count,3)
        value.cli.assert_called_with('unit-manager-check','--running')
    def test_controller_may_return_pending_to_let_its_node_confirm_inventory(self):
        value=self.role('controller');value.reconcile.side_effect=[None,{'state':'pending','required_nodes':1}]
        result=value.start()
        self.assertTrue(result['ready']);self.assertTrue(result['startup_pending']);self.assertEqual(value.reconcile.call_count,2)


if __name__=='__main__':unittest.main()

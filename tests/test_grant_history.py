"""Small SQL grant archival; Root context/verifier replaced, no live authority."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

import control_history as copies
import control_rotation as rotation
import grant_history as history
import result_history
import source_grants
import experiment_budget
from deployment import DeployError
from jobs import JobError
import maintenance_runner
import test_result_history as fixtures


class GrantHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ResultHistoryTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root;self.jobs=self.fixture.jobs;self.jobs.transaction=self.fixture.connect
        self.jobs.policy={}
        self.grants=source_grants.SourceGrants(self.jobs)
        self.owner=self.fixture.principal('alice');self.owner.legacy=False
        self.destination='experiment-'+'a'*24;self.other='experiment-'+'b'*24
        self.now=time.time();self.plans={};self.ids=[]
        with self.fixture.connect() as db:
            for index,state,expiry in [(1,'revoked',self.now+3600),(2,'active',self.now-3600),(3,'active',self.now+3600)]:
                ident='source-grant-'+format(index,'024x');self.ids.append(ident)
                plan={'schema':source_grants.CAPABILITY,'owner':'alice','source_job_id':self.fixture.ids[0],
                      'destination_experiment_id':self.destination if index<3 else self.other,
                      'source':{'receipt':'s'*2048},'files':[{'name':'results/file.bin','executable':False,'size':4,'sha256':'f'*64}],
                      'created_at':self.now-7200,'expires_at':expiry,'lifetime_seconds':10800,'idempotency_key':'grant-'+str(index)}
                self.plans[ident]=plan;fingerprint=hashlib.sha256(json.dumps(plan,sort_keys=True,allow_nan=False).encode()).hexdigest()
                value={**plan,'id':ident,'state':state,'approved_at':self.now-7200}
                if state=='revoked':value['revoked_at']=self.now-600
                db.execute('INSERT INTO source_grants VALUES(?,?,?,?,?,?,?,?)',(ident,'alice',plan['idempotency_key'],fingerprint,
                           json.dumps(value,sort_keys=True),self.fixture.ids[0],expiry,state))
            rows=[list(row) for row in db.execute('SELECT '+','.join(history.COLUMNS)+' FROM source_grants ORDER BY id')]
        self.fixture.body['tables']['source_grants']={'columns':history.COLUMNS,'rows':rows}
        raw=copies.encoded(self.fixture.body);plan=copy.deepcopy(self.fixture.copy_plan);plan.pop('id')
        plan['source'].update(bytes=len(raw),sha256=copies.sha(raw));plan['id']=copies.sha(copies.encoded(plan))
        self.fixture.copy_plan=plan;self.fixture.export_id=plan['id'];self.fixture.copy_record['plan']=plan
        indexes=[{'job_id':r[0],'export_id':plan['id'],'row_sha256':rotation.sha(r)} for r in self.fixture.body['tables']['jobs']['rows']]
        migrated={'format':rotation.FORMAT,'binding':self.fixture.binding,'export_id':plan['id'],'before':{},'indexes':indexes}
        migrated['id']=rotation.sha(migrated)
        with self.fixture.connect() as db:
            db.execute('DELETE FROM control_rotation_jobs');db.execute('DELETE FROM control_rotation_changes')
            for row,item in zip(self.fixture.body['tables']['jobs']['rows'],indexes):
                ref={**item,'rotation_id':migrated['id']}
                db.execute('UPDATE jobs SET data=? WHERE id=?',(rotation.encode(rotation.compact(json.loads(row[5]),{'format':rotation.FORMAT,**ref})).decode(),row[0]))
                db.execute('INSERT INTO control_rotation_jobs VALUES(?,?,?,?)',(row[0],plan['id'],migrated['id'],rotation.encode(ref).decode()))
            db.execute('INSERT INTO control_rotation_changes VALUES(?,?)',(migrated['id'],json.dumps({'plan':migrated})))
        p=patch.object(history,'context',result_history.context);p.start();self.addCleanup(p.stop)

    def plan(self):return history.plan(self.root,self.fixture.export_id)
    def migrate(self):
        value=self.plan();history.apply(self.root,value);return value
    def get(self,ident,principal=None):
        with self.fixture.connect() as db:return self.grants.get(db,ident,principal or self.owner)
    def records(self):
        with self.fixture.connect() as db:return list(db.execute('SELECT rowid,'+','.join(history.COLUMNS)+' FROM source_grants ORDER BY id'))
    def spec(self,ident):
        return {'owner':'alice','experiment_id':self.destination,'artifact_inputs':[{'source_grant':ident,
                'job_id':self.fixture.ids[0],'name':'results/file.bin','sha256':'f'*64,'executable':False}]}

    def test_only_inactive_bodies_migrate_with_original_ids_order_and_scope(self):
        before=self.records();value=self.plan()
        self.assertEqual([r['grant_id'] for r in value['rows']],self.ids[:2])
        self.assertEqual(history.apply(self.root,value)['migrated_grants'],2)
        after=self.records();self.assertEqual([r[:5] for r in after],[r[:5] for r in before])
        self.assertLess(sum(len(r[5]) for r in after),sum(len(r[5]) for r in before))
        for ident in self.ids[:2]:
            record=self.get(ident);self.assertFalse(record['usable']);self.assertTrue(record['history_archived'])
            self.assertEqual(record['files'],self.plans[ident]['files'])
        self.assertEqual(self.get(self.ids[0])['state'],'revoked')
        self.assertEqual(self.get(self.ids[1])['state'],'archived_inactive')
        self.assertTrue(self.get(self.ids[2])['usable'])

    def test_clock_rollback_cannot_revive_an_archived_expired_grant(self):
        self.migrate()
        with patch('source_grants.time.time',return_value=self.now-10000):
            self.assertFalse(self.get(self.ids[1])['usable'])
            with self.fixture.connect() as db:
                with self.assertRaisesRegex(JobError,'revoked'):self.grants.check(self.spec(self.ids[1]),db)

    def test_plain_active_grant_still_requires_the_original_producer_proof(self):
        ident=self.ids[2];spec=self.spec(ident);spec['experiment_id']=self.other
        with patch.object(self.grants,'source',return_value=(self.plans[ident]['source'],self.plans[ident]['files'])) as source:
            with self.fixture.connect() as db:self.grants.check(spec,db)
        self.assertEqual(source.call_count,1)
        revoked=self.grants.revoke(self.owner,ident)
        self.assertEqual(revoked['state'],'revoked');self.assertFalse(revoked['usable'])
        self.assertEqual(len(self.plan()['rows']),3)

    def test_revoke_of_already_archived_inactive_grant_does_not_expand_body_or_authority(self):
        self.migrate();before=self.records()
        value=self.grants.revoke(self.owner,self.ids[1])
        self.assertFalse(value['usable']);self.assertTrue(value['history_archived'])
        self.assertEqual(self.records(),before)

    def test_active_admission_denial_precedes_private_copy_access(self):
        self.migrate();self.fixture.checker.reset_mock();self.fixture.checker.side_effect=OSError('copy missing')
        with self.fixture.connect() as db:
            with self.assertRaisesRegex(JobError,'revoked'):self.grants.check(self.spec(self.ids[0]),db)
        self.fixture.checker.assert_not_called()

    def test_other_owner_and_other_experiment_are_denied_before_copy_reads(self):
        self.migrate();self.fixture.checker.reset_mock();self.fixture.checker.side_effect=OSError('copy missing')
        for principal in [self.fixture.principal('bob'),self.fixture.principal('alice',experiment=self.other)]:
            with self.assertRaises(JobError) as found:self.get(self.ids[0],principal)
            self.assertEqual(found.exception.status,404)
        self.fixture.checker.assert_not_called()
        self.assertEqual(self.grants.list(self.fixture.principal('carol'))['grants'],[])

    def test_list_verifies_one_copy_per_authorized_request(self):
        self.migrate();self.fixture.checker.reset_mock()
        rows=self.grants.list(self.owner)['grants'];self.assertEqual(len(rows),3)
        self.assertEqual(self.fixture.checker.call_count,1)
        self.grants.list(self.owner);self.assertEqual(self.fixture.checker.call_count,2)

    def test_structural_replay_preserves_files_but_never_live_admission(self):
        self.migrate()
        with self.fixture.connect() as db:
            self.grants.check(self.spec(self.ids[0]),db,active=False)
            with self.assertRaises(JobError):self.grants.check(self.spec(self.ids[0]),db)
        record,reused=self.grants.create(self.owner,self.plans[self.ids[0]])
        self.assertTrue(reused);self.assertEqual(record['id'],self.ids[0]);self.assertFalse(record['usable'])
        self.assertEqual(len(self.records()),3)

    def test_later_revocation_overlay_is_kept_without_changing_original_scope(self):
        with self.fixture.connect() as db:
            raw=db.execute('SELECT data FROM source_grants WHERE id=?',(self.ids[1],)).fetchone()[0]
            value=json.loads(raw);value.update(state='revoked',revoked_at=self.now)
            db.execute('UPDATE source_grants SET state=?,data=? WHERE id=?',('revoked',json.dumps(value),self.ids[1]))
        self.migrate();value=self.get(self.ids[1]);self.assertEqual(value['revoked_at'],self.now)
        self.assertEqual(value['history_original_state'],'active');self.assertFalse(value['usable'])

    def test_scope_change_after_snapshot_is_rejected(self):
        with self.fixture.connect() as db:
            value=json.loads(db.execute('SELECT data FROM source_grants WHERE id=?',(self.ids[0],)).fetchone()[0]);value['files'][0]['sha256']='0'*64
            db.execute('UPDATE source_grants SET data=? WHERE id=?',(json.dumps(value),self.ids[0]))
        before=self.records()
        with self.assertRaisesRegex(JobError,'scope changed'):self.plan()
        self.assertEqual(self.records(),before)

    def test_preview_live_row_change_refuses_apply(self):
        value=self.plan()
        with self.fixture.connect() as db:
            old=json.loads(db.execute('SELECT data FROM source_grants WHERE id=?',(self.ids[0],)).fetchone()[0]);old['revoked_at']=self.now+1
            db.execute('UPDATE source_grants SET data=? WHERE id=?',(json.dumps(old),self.ids[0]))
        with self.assertRaisesRegex(JobError,'changed after preview'):history.apply(self.root,value)

    def test_before_commit_rollback_and_after_commit_response_loss_use_original_plan(self):
        value=self.plan();before=self.records()
        def before_commit(stage):
            if stage=='before_commit':raise InterruptedError('before commit')
        with patch.object(history,'_checkpoint',side_effect=before_commit):
            with self.assertRaises(InterruptedError):history.apply(self.root,value)
        self.assertEqual(self.records(),before)
        def committed(stage):
            if stage=='committed':raise InterruptedError('response lost')
        with patch.object(history,'_checkpoint',side_effect=committed):
            with self.assertRaises(InterruptedError):history.apply(self.root,value)
        self.assertTrue(history.apply(self.root,value)['reused'])
        with self.fixture.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM grant_history_changes').fetchone()[0],1)

    def test_restore_freeze_keeps_projection_and_cannot_restore_authority(self):
        self.migrate();source_grants.rebind_restored(self.fixture.state)
        value=self.get(self.ids[0]);self.assertEqual(value['state'],'restored_frozen');self.assertFalse(value['usable'])
        self.fixture.copy_state='restore_frozen'
        self.assertEqual(self.get(self.ids[0])['files'],self.plans[self.ids[0]]['files'])
        with self.assertRaisesRegex(JobError,'read-only'):self.plan()

    def test_live_sql_state_mismatch_cannot_authorize_a_request(self):
        self.migrate()
        with self.fixture.connect() as db:
            original=json.loads(self.fixture.body['tables']['source_grants']['rows'][1][4]);original['expires_at']=self.now+3600
            db.execute('UPDATE source_grants SET data=?,expires_at=? WHERE id=?',(json.dumps(original),original['expires_at'],self.ids[1]))
            with self.assertRaisesRegex(JobError,'revoked'):self.grants.check(self.spec(self.ids[1]),db)

    def test_missing_copy_does_not_fill_defaults_or_restore_auth(self):
        self.migrate();self.fixture.checker.side_effect=OSError('unavailable')
        with self.assertRaisesRegex(JobError,'authorization was not restored'):self.get(self.ids[0])

    def test_missing_index_or_projection_tampering_is_rejected(self):
        self.migrate()
        with self.fixture.connect() as db:
            raw=db.execute('SELECT data FROM source_grants WHERE id=?',(self.ids[0],)).fetchone()[0]
            value=json.loads(raw);value['destination_experiment_id']=self.other
            db.execute('UPDATE source_grants SET data=? WHERE id=?',(json.dumps(value),self.ids[0]))
            with self.assertRaisesRegex(JobError,'projection'):history.read(db,self.ids[0],json.dumps(value))
            db.execute('DELETE FROM grant_history_rows WHERE grant_id=?',(self.ids[0],))
            with self.assertRaisesRegex(JobError,'reference'):history.read(db,self.ids[0],json.dumps(value))

    def test_archival_never_refunds_existing_numeric_budget_ledger(self):
        budgets=experiment_budget.Budgets(self.jobs);budget='experiment-'+'c'*24
        value={'id':budget,'owner':'alice','state':'closed','expires_at':self.now+3600,'limits':{'jobs':4,'cpu_seconds':400,'npu_seconds':0},'templates':['build_v1']}
        with self.fixture.connect() as db:
            db.execute('INSERT INTO experiment_budgets VALUES(?,?,?,?,?)',(budget,'alice','budget','f'*64,json.dumps(value)))
            db.execute('INSERT INTO experiment_charges VALUES(?,?,?,?)',(self.fixture.ids[0],budget,100,0))
            before=budgets.get(db,budget,self.owner)
        self.migrate()
        with self.fixture.connect() as db:after=budgets.get(db,budget,self.owner)
        self.assertEqual(before,after);self.assertEqual(after['charged']['jobs'],1)

    def test_missing_proof_or_changed_inactive_state_blocks_restore_and_downgrade(self):
        self.migrate();staged=self.root/'staged';staged.mkdir()
        with self.fixture.connect() as old,sqlite3.connect(staged/'jobs.sqlite3') as new:old.backup(new)
        history.require_history(self.fixture.state,staged)
        with sqlite3.connect(staged/'jobs.sqlite3') as new:new.execute('UPDATE source_grants SET state="active" WHERE id=?',(self.ids[0],))
        with self.assertRaisesRegex(DeployError,'inactive grant state'):history.require_history(self.fixture.state,staged)
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):history.require_compatible(self.fixture.state,'controller',{'files':{}})

    def test_finite_grant_index_and_batch_bounds_preserve_rows(self):
        value=self.plan();before=self.records()
        with patch.object(history,'MAX_ROWS',1):
            with self.assertRaisesRegex(JobError,'history is full'):history.apply(self.root,value)
        self.assertEqual(self.records(),before)
        with self.fixture.connect() as db:
            with self.assertRaisesRegex(JobError,'100'):history.read_many(db,[(self.ids[0],before[0][5])]*101)

    def test_maintenance_preserves_prepared_grant_plan_after_commit_loss(self):
        value=maintenance_runner.plan(self.root,[{'action':'grant_rotate','plan':self.fixture.copy_plan}]);journal=self.root/'grant-maintenance.json'
        self.assertTrue(maintenance_runner.run_once(value,journal,apply=True)['prepared'])
        def stop(stage):
            if stage=='committed':raise InterruptedError('response lost')
        with patch.object(history,'_checkpoint',side_effect=stop):
            with self.assertRaises(InterruptedError):maintenance_runner.run_once(value,journal,apply=True)
        with patch.object(history,'plan',side_effect=AssertionError('Do not replan')):
            self.assertEqual(maintenance_runner.run_once(value,journal,apply=True)['status'],'complete')

    def test_active_flag_corruption_cannot_reactivate_archived_authority(self):
        self.migrate()
        with self.fixture.connect() as db:
            value=json.loads(db.execute('SELECT data FROM source_grants WHERE id=?',(self.ids[0],)).fetchone()[0]);value['state']='active'
            db.execute('UPDATE source_grants SET state="active",data=? WHERE id=?',(json.dumps(value),self.ids[0]))
            self.fixture.checker.reset_mock()
            with self.assertRaisesRegex(JobError,'revoked'):self.grants.check(self.spec(self.ids[0]),db)
        self.fixture.checker.assert_not_called()
        with self.assertRaisesRegex(JobError,'never regain'):self.get(self.ids[0])

    def test_scope_projection_cannot_widen_a_bound_agents_access(self):
        self.migrate()
        with self.fixture.connect() as db:
            value=json.loads(db.execute('SELECT data FROM source_grants WHERE id=?',(self.ids[0],)).fetchone()[0]);value['destination_experiment_id']=self.other
            db.execute('UPDATE source_grants SET data=? WHERE id=?',(json.dumps(value),self.ids[0]))
        with self.assertRaisesRegex(JobError,'projection'):self.get(self.ids[0],self.fixture.principal('alice',experiment=self.other))

    def test_actual_current_uid_small_copy_verifier_rejects_tampering(self):
        directory=self.root/copies.DIRECTORY;directory.mkdir();directory.chmod(0o750)
        path=directory/(self.fixture.export_id+'.json');path.write_bytes(copies.encoded(self.fixture.body));path.chmod(0o440)
        info=directory.stat();self.fixture.copy_record['directory']=[info.st_dev,info.st_ino]
        info=path.stat();self.fixture.copy_record['file_identity']=[info.st_dev,info.st_ino]
        import os
        with patch.object(copies,'checked',copies._checked),patch.object(copies,'ADMIN_UID',os.getuid()):
            self.migrate();self.assertEqual(self.get(self.ids[0])['files'],self.plans[self.ids[0]]['files'])
            raw=path.read_bytes();path.chmod(0o600);path.write_bytes(b'!'+raw[1:]);path.chmod(0o440)
            with self.assertRaisesRegex(JobError,'authorization was not restored'):self.get(self.ids[0])
            with self.fixture.connect() as db:
                with self.assertRaisesRegex(JobError,'revoked'):self.grants.check(self.spec(self.ids[0]),db)


if __name__=='__main__':unittest.main()

"""Temporary maintenance revocation journals; no Root/service/timer actions."""
from contextlib import nullcontext
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import maintenance_lifecycle as lifecycle
import maintenance_runner as maintenance

NOW=dt.datetime(2026,11,9,2,30,tzinfo=dt.timezone.utc).timestamp()


class Crash(BaseException):pass


class MaintenanceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-maintenance-lifecycle-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.directory=self.root/'maintenance-runs';self.directory.mkdir(mode=0o700)
        self.contract={'not_before_epoch':int(NOW-3600),'expires_epoch':int(NOW+3*86400),
                       'start_minute_utc':120,'window_minutes':60,'max_invocations':8}
        self.value=maintenance.plan(str(self.root),[{'action':'audit_export','output':'/private/archive.json'}],execution=self.contract)
        self.journal=self.directory/(self.value['id']+'.json')

    def run_step(self,executor=None,**kwargs):
        return maintenance.run_once(self.value,self.journal,apply=True,executor=executor or Mock(return_value=({},False)),epoch=kwargs.pop('epoch',NOW),**kwargs)

    def revoke(self,**kwargs):return lifecycle.revoke_once(self.value,self.journal,apply=True,epoch=kwargs.pop('epoch',NOW),**kwargs)

    def state(self):return json.loads(self.journal.read_text())

    def test_revoke_preview_creates_no_files_and_cannot_execute_steps(self):
        result=lifecycle.revoke_once(self.value,self.journal,reason='resource_pressure')
        self.assertEqual(result['status'],'preview');self.assertFalse(result['writes']);self.assertEqual(list(self.directory.iterdir()),[])

    def test_revoke_before_start_is_permanent_and_consumes_no_invocations(self):
        result=self.revoke();self.assertEqual(result['status'],'revoked');self.assertFalse(result['original_journal_present'])
        run=Mock();before=self.journal.read_bytes()
        self.assertEqual(self.run_step(run)['status'],'revoked');run.assert_not_called();self.assertEqual(self.journal.read_bytes(),before)
        self.assertEqual(self.state()['calls'],0);self.assertEqual(maintenance.status(self.value,self.journal)['status'],'revoked')

    def test_revoke_retains_original_pending_intent_and_consumed_budget(self):
        failed=Mock(side_effect=OSError('unconfirmed original operation'))
        with self.assertRaises(OSError):self.run_step(failed)
        before=self.state();result=self.revoke(reason='maintenance_incident')
        self.assertTrue(result['pending']);self.assertEqual(result['invocations'],1)
        after=self.state();self.assertEqual({k:v for k,v in after.items() if k!='revocation'},before)
        self.run_step(failed);self.assertEqual(failed.call_count,1)

    def test_prepared_rotation_cas_is_not_removed_or_regenerated(self):
        import test_maintenance_execution as fixtures
        fixture=fixtures.ExecutionTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        self.value=fixture.rotation();self.journal=self.directory/(self.value['id']+'.json')
        prepare=Mock(return_value={'id':'original-before-image'});run=Mock(return_value=({},True))
        self.assertTrue(self.run_step(run,prepare=prepare)['prepared']);before=copy.deepcopy(self.state()['results'])
        self.revoke();self.run_step(run,prepare=prepare)
        self.assertEqual(self.state()['results'],before);prepare.assert_called_once();run.assert_not_called()

    def test_revoke_is_allowed_outside_window_after_expiry_and_budget_exhaustion(self):
        self.value=maintenance.plan(str(self.root),self.value['steps'],execution={**self.contract,'max_invocations':1})
        self.journal=self.directory/(self.value['id']+'.json');self.run_step()
        result=self.revoke(epoch=self.contract['expires_epoch']+86400)
        self.assertEqual(result['status'],'revoked');self.assertEqual(result['remaining_invocations'],0)
        self.assertEqual(self.run_step(epoch=NOW-10000)['status'],'revoked')

    def test_repeated_revoke_reuses_original_reason_time_and_counter(self):
        self.run_step();self.revoke(reason='resource_pressure');before=self.journal.read_bytes()
        marker=lifecycle.marker(self.journal).read_bytes();anchor=maintenance.read_anchor(self.journal)
        result=self.revoke(reason='operator_stop',epoch=NOW+100)
        self.assertTrue(result['reused']);self.assertFalse(result['writes']);self.assertEqual(result['reason'],'resource_pressure')
        self.assertEqual(self.journal.read_bytes(),before);self.assertEqual(lifecycle.marker(self.journal).read_bytes(),marker)
        self.assertEqual(maintenance.read_anchor(self.journal),anchor)

    def test_completed_receipts_and_progress_survive_revoke(self):
        self.run_step(Mock(return_value=({'original_receipt':'saved'},True)));before=self.state()
        self.revoke();self.assertEqual(self.state()['index'],1);self.assertEqual(self.state()['results'],before['results'])
        self.assertEqual(self.state()['calls'],1)

    def test_legacy_v1_can_be_revoked_without_changing_approval_id_or_adding_budget(self):
        self.value=maintenance.plan(str(self.root),self.value['steps']);self.journal=self.directory/(self.value['id']+'.json')
        identifier=self.value['id'];self.run_step();self.revoke()
        self.assertEqual(self.value['id'],identifier);self.assertNotIn('calls',self.state());self.assertNotIn('execution',self.value)
        self.assertEqual(self.run_step()['status'],'revoked')

    def test_crash_after_marker_or_journal_or_anchor_blocks_execution_and_resumes_same_stop(self):
        for phase in ['marker_persisted','journal_persisted','anchor_persisted']:
            with self.subTest(phase=phase),tempfile.TemporaryDirectory(dir=self.root) as folder:
                path=Path(folder)/'original.json';run=Mock()
                def checkpoint(stage):
                    if stage==phase:raise Crash()
                with patch.object(lifecycle,'_checkpoint',checkpoint),self.assertRaises(Crash):
                    lifecycle.revoke_once(self.value,path,apply=True,epoch=NOW)
                marker=lifecycle.marker(path).read_bytes()
                result=maintenance.run_once(self.value,path,apply=True,executor=run,epoch=NOW)
                self.assertIn(result['status'],{'revoked','revocation_pending'});run.assert_not_called()
                final=lifecycle.revoke_once(self.value,path,apply=True,epoch=NOW+1)
                self.assertEqual(final['status'],'revoked');self.assertEqual(lifecycle.marker(path).read_bytes(),marker)

    def test_recognizable_partial_in_place_anchor_seal_is_recoverable(self):
        self.run_step();old_anchor=maintenance.read_anchor(self.journal)
        def checkpoint(stage):
            if stage=='journal_persisted':raise Crash()
        with patch.object(lifecycle,'_checkpoint',checkpoint),self.assertRaises(Crash):self.revoke()
        record=json.loads(lifecycle.marker(self.journal).read_text());target=lifecycle.anchor(self.value,record)
        path=self.journal.with_name(self.journal.name+'.lock');path.write_bytes(target[:15]+old_anchor[15:]);path.chmod(0o600)
        self.assertEqual(self.run_step()['status'],'revocation_pending')
        self.assertEqual(self.revoke()['status'],'revoked');self.assertEqual(maintenance.read_anchor(self.journal),target)

    def test_lost_marker_never_becomes_fresh_execution(self):
        self.revoke();lifecycle.marker(self.journal).unlink();run=Mock()
        with self.assertRaises(ValueError):self.run_step(run)
        with self.assertRaises(ValueError):maintenance.status(self.value,self.journal)
        with self.assertRaises(ValueError):self.revoke()
        run.assert_not_called()

    def test_lost_journal_never_recreates_zero_budget_under_revoked_anchor(self):
        self.run_step();self.revoke();self.journal.unlink();run=Mock()
        with self.assertRaises(ValueError):self.run_step(run)
        self.assertFalse(self.journal.exists());run.assert_not_called()

    def test_lost_anchor_is_still_stopped_and_can_only_reseal_same_revocation(self):
        self.run_step();self.revoke();self.journal.with_name(self.journal.name+'.lock').unlink()
        before=self.journal.read_bytes();self.assertEqual(self.run_step()['status'],'revocation_pending')
        self.assertEqual(self.revoke()['status'],'revoked');self.assertEqual(self.journal.read_bytes(),before)

    def test_marker_and_annotation_loss_still_rejected_by_revoked_anchor(self):
        self.revoke();lifecycle.marker(self.journal).unlink()
        state=self.state();del state['revocation'];maintenance.save_journal(self.journal,state)
        with self.assertRaises(ValueError):self.run_step()

    def test_post_revocation_budget_or_preimage_tampering_is_rejected(self):
        self.run_step();self.revoke();state=self.state();state['calls']=0;state['last_invocation_epoch']=None;state.pop('pending')
        maintenance.save_journal(self.journal,state)
        with self.assertRaises(ValueError):self.run_step()

    def test_full_journal_can_be_revoked_without_growing_or_truncating_it(self):
        self.run_step();state=self.state();state['results']['0']={'retained_receipt':''}
        overhead=len(maintenance.encoded(state))
        state['results']['0']['retained_receipt']='x'*(maintenance.MAX_PLAN_BYTES-overhead-1)
        maintenance.save_journal(self.journal,state);before=self.journal.read_bytes()
        result=self.revoke();self.assertEqual(result['status'],'revoked')
        self.assertEqual(self.journal.read_bytes(),before)
        record=json.loads(lifecycle.marker(self.journal).read_text());self.assertFalse(record['journal_annotation'])
        run=Mock();self.assertEqual(self.run_step(run)['status'],'revoked');run.assert_not_called()
        lifecycle.marker(self.journal).unlink()
        with self.assertRaises(ValueError):self.run_step(run)

    def test_revocation_between_preflight_and_execution_lock_is_rechecked(self):
        original=maintenance.execution_lock;seen=False
        def acquire(parent):
            nonlocal seen
            if not seen:
                seen=True;self.revoke()
            return original(parent)
        run=Mock()
        with patch.object(maintenance,'execution_lock',acquire):self.assertEqual(self.run_step(run)['status'],'revoked')
        run.assert_not_called();self.assertEqual(self.state()['calls'],0)

    def test_wrong_reason_clock_marker_identity_and_hash_are_rejected(self):
        for reason in ['unregistered',[],None]:
            with self.subTest(reason=reason),self.assertRaises(ValueError):self.revoke(reason=reason)
        for epoch in [True,float('nan'),10**400,-1]:
            with self.subTest(epoch=str(epoch)[:20]),self.assertRaises(ValueError):self.revoke(epoch=epoch)
        self.revoke();record=json.loads(lifecycle.marker(self.journal).read_text());record['plan_id']='0'*64
        maintenance.save_journal(lifecycle.marker(self.journal),record)
        with self.assertRaises(ValueError):self.run_step()

    def test_unsafe_marker_symlink_hardlink_and_public_mode_are_refused(self):
        self.revoke();path=lifecycle.marker(self.journal);path.chmod(0o644)
        with self.assertRaises(ValueError):self.run_step()
        path.chmod(0o600);other=self.root/'copy';os.link(path,other)
        with self.assertRaises(ValueError):self.run_step()
        other.unlink();saved=path.read_bytes();path.unlink();other.write_bytes(saved);other.chmod(0o600);path.symlink_to(other)
        with self.assertRaises(OSError):self.run_step()

    def test_shared_lock_rejects_revoke_without_blocking_or_new_marker(self):
        with lifecycle.coordination(self.root):
            with self.assertRaises(BlockingIOError):self.revoke()
        self.assertFalse(lifecycle.marker(self.journal).exists())

    def test_downgrade_guard_keeps_marker_or_surviving_anchor(self):
        self.revoke()
        with self.assertRaisesRegex(ValueError,'compatible lifecycle'):lifecycle.require_compatible(self.root,{'files':{}})
        lifecycle.require_compatible(self.root,{'files':{'maintenance_lifecycle.py':{}}})
        lifecycle.marker(self.journal).unlink()
        with self.assertRaises(ValueError):lifecycle.require_compatible(self.root,{'files':{}})
        self.journal.with_name(self.journal.name+'.lock').write_bytes(b'{"plan_id":')
        with self.assertRaises(ValueError):lifecycle.require_compatible(self.root,{'files':{}})

    def test_native_activation_guard_rejects_old_release_before_current_link_changes(self):
        import maintenance as native
        self.revoke();state=self.root/'state';state.mkdir();release='b'*64;target=self.root/'releases'/release;target.mkdir(parents=True)
        (target/'release.json').write_text(json.dumps({'state_schema':1,'files':{}}))
        current=self.root/'current';current.symlink_to('releases/'+'a'*64)
        meta={'role':'controller','state_dir':str(state),'state_schema':1,'release_id':'a'*64}
        with patch.object(native,'require_root'),patch.object(native,'verify',return_value=(self.root,meta,target)), \
             patch('control_process.offline',return_value=nullcontext()),patch('cold_retention.require_stable_code'), \
             patch('control_history.require_stable_code'),patch.object(native,'assert_quiescent'), \
             patch.object(native,'checked_manifest'),patch.object(native,'require_features'):
            with self.assertRaisesRegex(ValueError,'compatible lifecycle'):native.activate(self.root,release_id=release)
        self.assertEqual(os.readlink(current),'releases/'+'a'*64)

    def test_lifecycle_scan_is_bounded_and_never_prunes_journals(self):
        # An explicit bound protects code-switch checks; history is never pruned.
        with patch.object(lifecycle.os,'scandir',return_value=Mock(__enter__=Mock(return_value=iter([SimpleNamespace(name='unrelated')]*1025)),__exit__=Mock(return_value=False))):
            with self.assertRaisesRegex(ValueError,'1024'):lifecycle.require_compatible(self.root,{'files':{}})

    def test_cli_revoke_preview_and_root_rejection_do_not_advance_maintenance(self):
        namespace=runpy.run_path(str(Path(maintenance.__file__).with_name('opsctl')));main=namespace['main'];main.__globals__['limit']=lambda:None
        path=self.root/'approved.json';path.write_text(json.dumps(self.value));path.chmod(0o600)
        args=['opsctl','maintenance-revoke','--plan',str(path)]
        with patch.object(sys,'argv',args):self.assertEqual(main()['status'],'preview')
        with patch.object(sys,'argv',args+['--apply']),patch('deployment.require_root',side_effect=ValueError('Root required')):
            with self.assertRaises(ValueError):main()
        self.assertFalse(self.journal.exists());self.assertFalse(lifecycle.marker(self.journal).exists())
        source=Path(maintenance.__file__).parent
        with patch('deployment.require_root'),patch('deployment.verify',return_value=(self.root,{'role':'controller'},source)):
            with patch.object(sys,'argv',args+['--apply']):self.assertEqual(main()['status'],'revoked')
            with patch.object(sys,'argv',['opsctl','maintenance-status','--plan',str(path)]):self.assertEqual(main()['status'],'revoked')
        self.assertEqual(self.state()['calls'],0)


if __name__=='__main__':unittest.main()

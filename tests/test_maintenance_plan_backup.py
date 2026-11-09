"""One-plan Root metadata backups with fake Root context, tiny private files.

No service, state snapshot, workload, network, thread or native installation.
Exceptions at durable checkpoints model interruption; not real SIGKILL/power loss.
"""
from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import maintenance_plan_backup as backup
import maintenance_runner as runner
import maintenance_lifecycle as lifecycle

NOW=dt.datetime(2026,11,9,2,30,tzinfo=dt.timezone.utc).timestamp()


class Crash(BaseException):pass


class MaintenancePlanBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-plan-backup-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.target=self.root/'target';self.archives=self.root/'archives'
        for path in [self.source,self.target,self.archives]:path.mkdir(mode=0o700)
        (self.source/'maintenance-runs').mkdir(mode=0o700)
        contract={'not_before_epoch':int(NOW-3600),'expires_epoch':int(NOW+3*86400),
                  'start_minute_utc':120,'window_minutes':60,'max_invocations':8}
        self.value=runner.plan(str(self.source),[{'action':'audit_export','output':'/private/audit.json'}],execution=contract)
        self.output=self.archives/'original.json'

    def journal(self,prefix):return prefix/'maintenance-runs'/(self.value['id']+'.json')

    def step(self,prefix=None):
        prefix=self.source if prefix is None else prefix
        (prefix/'maintenance-runs').mkdir(mode=0o700,exist_ok=True)
        return runner.run_once(self.value,self.journal(prefix),apply=True,epoch=NOW,executor=Mock(return_value=({},False)))

    def capture(self,prefix=None):
        prefix=self.source if prefix is None else prefix
        with lifecycle.coordination(prefix):return backup.capture(prefix,self.value,epoch=NOW)

    def recover(self,bundle,**kwargs):return backup.recover_in_context(self.target,bundle,apply=True,epoch=NOW+1,**kwargs)

    def test_capture_preserves_exact_source_parts_and_does_not_change_budget(self):
        self.step();before=backup.parts(self.journal(self.source));bundle=self.capture()
        self.assertEqual(bundle['parts'],before);self.assertEqual(backup.parts(self.journal(self.source)),before)
        self.assertEqual(json.loads(bundle['parts']['journal']['text'])['calls'],1);backup.checked(bundle)

    def test_private_export_preview_and_idempotent_existing_output(self):
        self.step();preview=backup.export_in_context(self.source,self.value,self.output,epoch=NOW)
        self.assertFalse(preview['writes']);self.assertFalse(self.output.exists())
        result=backup.export_in_context(self.source,self.value,self.output,apply=True,epoch=NOW)
        self.assertEqual(result['status'],'backed_up');self.assertEqual(self.output.stat().st_mode&0o777,0o600)
        raw=self.output.read_bytes();self.step()
        existing=backup.export_in_context(self.source,self.value,self.output,apply=True,epoch=NOW+1)
        self.assertFalse(existing['represents_current']);self.assertEqual(self.output.read_bytes(),raw)

    def test_recovery_copies_used_budget_and_pending_facts_but_never_execution_authority(self):
        self.step();self.step();bundle=self.capture();source_before=backup.parts(self.journal(self.source))
        result=self.recover(bundle);self.assertFalse(result['execution_restored']);self.assertEqual(result['current_invocations'],2)
        target=json.loads(self.journal(self.target).read_text());original=json.loads(bundle['parts']['journal']['text'])
        self.assertEqual({k:v for k,v in target.items() if k!='revocation'},original);self.assertTrue(result['pending_retained'])
        run=Mock();self.assertEqual(runner.run_once(self.value,self.journal(self.target),apply=True,epoch=NOW,executor=run)['status'],'revoked')
        run.assert_not_called();self.assertEqual(backup.parts(self.journal(self.source)),source_before)

    def test_existing_newer_target_counter_is_never_replaced_by_old_backup(self):
        self.step();bundle=self.capture()
        for _ in range(3):self.step(self.target)
        before=json.loads(self.journal(self.target).read_text());result=self.recover(bundle)
        after=json.loads(self.journal(self.target).read_text())
        self.assertEqual(result['snapshot_invocations'],1);self.assertEqual(result['current_invocations'],3)
        self.assertEqual({k:v for k,v in after.items() if k!='revocation'},before)

    def test_existing_target_revocation_reason_time_and_count_are_retained(self):
        self.step();bundle=self.capture();self.step(self.target)
        lifecycle.revoke_once(self.value,self.journal(self.target),apply=True,reason='operator_stop',epoch=NOW)
        record=lifecycle.marker(self.journal(self.target)).read_bytes();self.recover(bundle)
        self.assertEqual(lifecycle.marker(self.journal(self.target)).read_bytes(),record)

    def test_revoked_source_marker_is_preserved_exactly(self):
        self.step();lifecycle.revoke_once(self.value,self.journal(self.source),apply=True,epoch=NOW)
        bundle=self.capture();self.recover(bundle)
        self.assertEqual(lifecycle.marker(self.journal(self.target)).read_text(),bundle['parts']['revocation']['text'])

    def test_source_revocation_interrupted_before_journal_is_recoverable_as_stopped(self):
        self.step()
        def checkpoint(stage):
            if stage=='marker_persisted':raise Crash()
        with patch.object(lifecycle,'_checkpoint',checkpoint),self.assertRaises(Crash):
            lifecycle.revoke_once(self.value,self.journal(self.source),apply=True,epoch=NOW)
        result=self.recover(self.capture());self.assertEqual(result['current_invocations'],1)
        self.assertEqual(runner.status(self.value,self.journal(self.target))['status'],'revoked')

    def test_never_started_and_legacy_v1_plans_recover_stopped(self):
        self.recover(self.capture());self.assertEqual(runner.status(self.value,self.journal(self.target))['invocations'],0)
        self.value=runner.plan(str(self.source),self.value['steps']);self.step()
        self.recover(self.capture());self.assertNotIn('calls',json.loads(self.journal(self.target).read_text()))
        self.assertEqual(runner.status(self.value,self.journal(self.target))['status'],'revoked')

    def test_lost_reply_after_each_published_phase_reuses_original_stopped_plan(self):
        self.step();bundle=self.capture()
        for phase in ['evidence_published','stop_published','journal_retained','receipt_published']:
            with self.subTest(phase=phase),tempfile.TemporaryDirectory(dir=self.root) as temporary:
                target=Path(temporary)
                def checkpoint(stage):
                    if stage==phase:raise Crash()
                with patch.object(backup,'_checkpoint',checkpoint),self.assertRaises(Crash):
                    backup.recover_in_context(target,bundle,apply=True,epoch=NOW+1)
                journal=self.journal(target);run=Mock()
                try:runner.run_once(self.value,journal,apply=True,epoch=NOW,executor=run)
                except ValueError:pass  # Unknown/missing pieces are still stopped.
                run.assert_not_called()
                result=backup.recover_in_context(target,bundle,apply=True,epoch=NOW+2)
                self.assertTrue(result['original_approval_revoked']);self.assertEqual(result['current_invocations'],1)

    def test_replay_does_not_rewrite_recovery_receipt_or_counter(self):
        self.step();bundle=self.capture();self.recover(bundle)
        directory=self.target/'maintenance-recovered'/bundle['id'];receipt=(directory/'receipt.json').read_bytes()
        before=backup.parts(self.journal(self.target));result=self.recover(bundle)
        self.assertEqual(result['status'],'already_recovered');self.assertFalse(result['writes'])
        self.assertEqual(backup.parts(self.journal(self.target)),before);self.assertEqual((directory/'receipt.json').read_bytes(),receipt)

    def test_recovery_evidence_fences_lost_live_records_without_replaying_older_state(self):
        self.step();bundle=self.capture();self.recover(bundle)
        journal=self.journal(self.target)
        for path in [journal,journal.with_name(journal.name+'.lock'),lifecycle.marker(journal)]:path.unlink()
        run=Mock()
        with self.assertRaises(ValueError):runner.run_once(self.value,journal,apply=True,epoch=NOW,executor=run)
        with self.assertRaises(ValueError):self.recover(bundle)
        run.assert_not_called();self.assertFalse(journal.exists())

    def test_missing_target_journal_behind_old_anchor_is_not_overwritten(self):
        self.step();bundle=self.capture();self.step(self.target);self.journal(self.target).unlink()
        before=backup.parts(self.journal(self.target))
        with self.assertRaises(ValueError):self.recover(bundle)
        self.assertEqual(backup.parts(self.journal(self.target)),before)

    def test_part_and_bundle_corruption_are_rejected_before_target_state_changes(self):
        self.step();bundle=self.capture()
        for change in ['digest','part','size','plan']:
            value=copy.deepcopy(bundle)
            if change=='digest':value['id']='0'*64
            if change=='part':value['parts']['journal']['text']='{}'
            if change=='size':value['parts']['anchor']['bytes']=True
            if change=='plan':value['plan']['prefix']='/different'
            with self.subTest(change=change),self.assertRaises(ValueError):self.recover(value)
        self.assertFalse(self.journal(self.target).exists())

    def test_valid_checksums_with_invalid_state_semantics_are_rejected(self):
        self.step();bundle=self.capture();part=bundle['parts']['journal'];state=json.loads(part['text']);state['calls']=0
        part['text']=runner.encoded(state).decode();part['bytes']=len(part['text']);part['sha256']=backup.sha(part['text'].encode())
        bundle['id']=backup.sha(backup.encoded({k:v for k,v in bundle.items() if k!='id'}))
        with self.assertRaises(ValueError):self.recover(bundle)
        self.assertFalse((self.target/'maintenance-recovered').exists())

    def test_bounded_store_refuses_additional_import_without_pruning(self):
        self.step();bundle=self.capture();self.recover(bundle);self.step();other=self.capture()
        with patch.object(backup,'MAX_RECOVERED',1),self.assertRaises(ValueError):self.recover(other)
        self.assertTrue((self.target/'maintenance-recovered'/bundle['id']/'bundle.json').is_file())

    def test_near_full_escaped_journal_is_preserved_without_truncation_or_cap_increase(self):
        self.step();state=json.loads(self.journal(self.source).read_text());state['results']['0']={'retained_receipt':''}
        overhead=len(runner.encoded(state));state['results']['0']['retained_receipt']='\\'*((runner.MAX_PLAN_BYTES-overhead-2)//2)
        runner.save_journal(self.journal(self.source),state);before=self.journal(self.source).read_bytes()
        bundle=self.capture();self.assertLessEqual(len(backup.encoded(bundle)),backup.MAX_BUNDLE_BYTES)
        self.recover(bundle);self.assertEqual(self.journal(self.target).read_bytes(),before)
        self.assertEqual(json.loads(self.journal(self.target).read_text())['calls'],1)

    def test_recovery_receipt_or_stored_part_changes_never_reissue_an_original_approval(self):
        self.step();bundle=self.capture();self.recover(bundle);before=backup.parts(self.journal(self.target))
        folder=self.target/'maintenance-recovered'/bundle['id'];receipt=json.loads((folder/'receipt.json').read_text())
        receipt['target_prefix']='/wrong-target';(folder/'receipt.json').write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):self.recover(bundle)
        self.assertEqual(backup.parts(self.journal(self.target)),before)

    def test_matched_root_context_checks_role_and_installed_code(self):
        source=Path(backup.__file__).parent
        with patch('deployment.require_root'),patch('deployment.verify',return_value=(self.source,{'role':'controller'},source)):
            with backup.context(self.source) as (prefix,_):self.assertEqual(prefix,self.source)
        with patch('deployment.require_root'),patch('deployment.verify',return_value=(self.source,{'role':'node'},source)):
            with self.assertRaises(ValueError):
                with backup.context(self.source):pass
        other=self.root/'release';other.mkdir();(other/'maintenance_plan_backup.py').write_bytes(Path(backup.__file__).read_bytes());(other/'maintenance_runner.py').write_text('different')
        with patch('deployment.require_root'),patch('deployment.verify',return_value=(self.source,{'role':'controller'},other)):
            with self.assertRaises(ValueError):
                with backup.context(self.source):pass

    def test_symlink_hardlink_and_public_export_files_are_not_trusted(self):
        bundle=self.capture();self.output.write_bytes(backup.encoded(bundle));self.output.chmod(0o644)
        with self.assertRaises(ValueError):backup.read_private(self.output,backup.MAX_BUNDLE_BYTES)
        self.output.chmod(0o600);link=self.archives/'linked';os.link(self.output,link)
        with self.assertRaises(ValueError):backup.read_private(self.output,backup.MAX_BUNDLE_BYTES)
        link.unlink();link.symlink_to(self.output)
        with self.assertRaises(OSError):backup.read_private(link,backup.MAX_BUNDLE_BYTES)

    def test_export_cannot_write_inside_installation_or_service_state(self):
        for output,state in [(self.source/'copy.json',None),(self.archives/'copy.json',self.archives)]:
            with self.subTest(output=output),self.assertRaises(ValueError):
                backup.export_in_context(self.source,self.value,output,apply=True,epoch=NOW,state_dir=state)

    def test_recovered_foreign_prefix_approval_can_be_backed_up_as_evidence(self):
        self.step();self.recover(self.capture());bundle=self.capture(self.target)
        self.assertEqual(bundle['source_prefix'],str(self.target));self.assertEqual(bundle['plan']['prefix'],str(self.source))
        third=self.root/'third';third.mkdir(mode=0o700)
        result=backup.recover_in_context(third,bundle,apply=True,epoch=NOW+2)
        self.assertEqual(result['current_invocations'],1);self.assertFalse(result['execution_restored'])

    def test_backup_and_downgrade_guards_share_original_nonblocking_lock(self):
        with lifecycle.coordination(self.source):
            with self.assertRaises(BlockingIOError):backup.export_in_context(self.source,self.value,self.output,apply=True,epoch=NOW)
        self.recover(self.capture())
        with self.assertRaisesRegex(ValueError,'backup code'):lifecycle.require_compatible(self.target,{'files':{'maintenance_lifecycle.py':{}}})

    def test_root_wrapper_and_cli_require_matching_installation_without_real_root(self):
        with patch('deployment.require_root',side_effect=ValueError('Root required')),patch('deployment.verify') as verify:
            with self.assertRaises(ValueError):backup.export(self.value,self.output)
            verify.assert_not_called()
        self.step();bundle=self.capture();self.output.write_bytes(backup.encoded(bundle));self.output.chmod(0o600)
        @contextmanager
        def context(prefix):yield Path(prefix),{'state_dir':str(self.root/'service-state')}
        with patch.object(backup,'context',context),patch('deployment.secure_parent',side_effect=lambda p:Path(p)):
            self.assertEqual(backup.recover(self.target,self.output,apply=True)['status'],'recovered')
            result=backup.export(self.value,self.archives/'foreign.json',apply=True,prefix=self.target)
            self.assertEqual(result['status'],'backed_up')
        namespace=runpy.run_path(str(Path(runner.__file__).with_name('opsctl')));main=namespace['main'];main.__globals__['limit']=lambda:None
        plan=self.archives/'approved.json';plan.write_text(json.dumps(self.value));plan.chmod(0o600)
        with patch.object(backup,'export',return_value={'status':'preview'}) as export,patch.object(backup,'recover',return_value={'status':'preview'}) as recover:
            with patch.object(sys,'argv',['opsctl','maintenance-plan-backup','--plan',str(plan),'--output',str(self.archives/'cli.json')]):self.assertEqual(main()['status'],'preview')
            with patch.object(sys,'argv',['opsctl','maintenance-plan-recover','--prefix',str(self.target),'--archive',str(self.output)]):self.assertEqual(main()['status'],'preview')
            export.assert_called_once();recover.assert_called_once()


if __name__=='__main__':unittest.main()

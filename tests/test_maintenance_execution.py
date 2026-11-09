"""Finite execution contracts with temporary journals; no Root actions/processes."""
from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import control_history as copies
import maintenance_runner as maintenance

NOW=dt.datetime(2026,11,9,2,30,tzinfo=dt.timezone.utc).timestamp()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.journal=self.root/'original.json'
        self.contract={'not_before_epoch':int(NOW-3600),'expires_epoch':int(NOW+3*86400),
                       'start_minute_utc':120,'window_minutes':60,'max_invocations':8}

    def tearDown(self):self.temp.cleanup()

    def value(self,*,maximum=None):
        contract={**self.contract}
        if maximum is not None:contract['max_invocations']=maximum
        return maintenance.plan('/fixed/prefix',[{'action':'audit_export','output':'/private/archive.json'}],execution=contract)

    def run_step(self,value,executor,**kwargs):
        return maintenance.run_once(value,self.journal,apply=True,executor=executor,epoch=kwargs.pop('epoch',NOW),**kwargs)

    def test_contract_schema_types_and_seven_day_bound_are_strict(self):
        for changes in [{'max_invocations':True},{'max_invocations':129},{'window_minutes':181},
                        {'expires_epoch':int(NOW-3600)},{'expires_epoch':int(NOW+8*86400)},
                        {'start_minute_utc':1440},{'not_before_epoch':-1},{'extra':1}]:
            with self.assertRaises(ValueError):maintenance.execution_contract({**self.contract,**changes})
        value=self.value();changed=copy.deepcopy(value);changed['execution']['max_invocations']+=1
        with self.assertRaisesRegex(ValueError,'digest'):maintenance.checked(changed)
        with self.assertRaisesRegex(ValueError,'existing bounded'):maintenance.with_execution(value,self.contract)

    def test_binding_a_v1_approval_is_an_explicit_new_digest(self):
        legacy=maintenance.plan('/fixed/prefix',[{'action':'audit_export','output':'/private/archive.json'}])
        value=maintenance.with_execution(legacy,self.contract)
        self.assertEqual(value['schema'],'ascend-lab-maintenance/v2')
        self.assertNotEqual(value['id'],legacy['id']);self.assertEqual(value['steps'],legacy['steps'])
        self.assertNotIn('execution',legacy)

    def test_preview_is_readonly_and_reports_execution_blocker(self):
        run=Mock()
        result=maintenance.run_once(self.value(),self.journal,executor=run,epoch=NOW+3600)
        self.assertEqual(result['execution_blocker'],'outside_window')
        self.assertFalse(result['writes']);self.assertEqual(list(self.root.iterdir()),[]);run.assert_not_called()

    def test_before_start_expiry_and_window_boundary_create_no_journal_or_lock(self):
        value=self.value();run=Mock()
        for epoch,reason in [(NOW-3601,'before_start'),(value['execution']['expires_epoch'],'expired'),(NOW+1800,'outside_window')]:
            result=self.run_step(value,run,epoch=epoch)
            self.assertEqual((result['status'],result['reason']),('deferred',reason))
            self.assertEqual(list(self.root.iterdir()),[])
        run.assert_not_called()

    def test_success_counts_once_and_completed_replay_does_not_consume_budget(self):
        value=self.value();run=Mock(return_value=({'sha256':'a'*64},True))
        result=self.run_step(value,run);self.assertEqual((result['status'],result['invocations']),('complete',1))
        self.assertTrue(self.run_step(value,run)['reused']);self.assertEqual(run.call_count,1)
        raw=self.journal.read_bytes();listing=list(self.root.iterdir())
        status=maintenance.status(value,self.journal)
        self.assertEqual((status['status'],status['remaining_invocations']),('complete',7))
        self.assertEqual(self.journal.read_bytes(),raw);self.assertEqual(list(self.root.iterdir()),listing)

    def test_intent_and_count_are_durable_before_failure_and_budget_exhaustion(self):
        value=self.value(maximum=2);observed=[]
        def fail(*args):
            state=json.loads(self.journal.read_text());observed.append((state['calls'],state['pending']))
            raise OSError('interrupted')
        for _ in range(2):
            with self.assertRaises(OSError):self.run_step(value,fail)
        raw=self.journal.read_bytes();result=self.run_step(value,fail)
        self.assertEqual(result['status'],'budget_exhausted');self.assertEqual(observed,[(1,0),(2,0)])
        self.assertEqual(self.journal.read_bytes(),raw)
        self.assertEqual(maintenance.status(value,self.journal)['status'],'budget_exhausted')

    def test_partial_operation_resumes_same_step_and_original_results(self):
        value=self.value();run=Mock(side_effect=[({'offset':64},False),({'sha256':'a'*64},True)])
        first=self.run_step(value,run);self.assertEqual((first['next_step'],first['pending']),(0,True))
        second=self.run_step(value,run,epoch=NOW+1)
        self.assertEqual((second['next_step'],second['invocations']),(1,2))
        self.assertEqual(run.call_args_list[0].args[1],run.call_args_list[1].args[1])

    def test_lost_commit_response_retries_original_action_without_new_intent(self):
        value=self.value();applied=set();receipts=[]
        def execute(prefix,step,results,steps):
            receipts.append(value['id'])
            if value['id'] not in applied:
                applied.add(value['id']);raise OSError('response lost after commit')
            return {'reused':True},True
        with self.assertRaises(OSError):self.run_step(value,execute)
        self.assertEqual(self.run_step(value,execute)['status'],'complete')
        self.assertEqual(len(applied),1);self.assertEqual(receipts,[value['id']]*2)

    def rotation(self):
        original={'format':copies.FORMAT,'binding':{},'budget':{'bytes':1024**2,'exports':2},
                  'selection':['job-000000000001'],'source':{'bytes':100,'sha256':'a'*64,'jobs':[]}}
        original['id']=hashlib.sha256(copies.encoded(original)).hexdigest()
        return maintenance.plan('/fixed/prefix',[{'action':'control_rotate','plan':original}],execution=self.contract)

    def test_rotation_preimage_survives_commit_loss_and_is_not_regenerated(self):
        value=self.rotation();preimage={'id':'original-cas'};prepare=Mock(return_value=preimage);seen=[]
        def execute(prefix,step,results,steps):
            seen.append(copy.deepcopy(results['0']['prepared_rotation_plan']))
            if len(seen)==1:raise OSError('committed response lost')
            return {'reused':True},True
        self.assertTrue(self.run_step(value,execute,prepare=prepare)['prepared'])
        with self.assertRaises(OSError):self.run_step(value,execute,prepare=prepare)
        result=self.run_step(value,execute,prepare=prepare)
        self.assertEqual(result['invocations'],3);self.assertEqual(prepare.call_count,1)
        self.assertEqual(seen,[preimage,preimage])

    def test_preparation_failure_also_consumes_budget_and_preserves_original_step(self):
        value=self.rotation();prepare=Mock(side_effect=[OSError('before preimage'),{'id':'same-cas'}]);run=Mock(return_value=({},True))
        with self.assertRaises(OSError):self.run_step(value,run,prepare=prepare)
        self.assertTrue(self.run_step(value,run,prepare=prepare)['prepared'])
        self.assertEqual(self.run_step(value,run,prepare=prepare)['invocations'],3)
        self.assertEqual(prepare.call_count,2);self.assertEqual(run.call_count,1)

    def test_two_plans_share_a_nonblocking_execution_lock_without_threads(self):
        first=self.value();other=maintenance.plan('/fixed/prefix',[{'action':'audit_export','output':'/private/other.json'}],execution=self.contract)
        other_journal=self.root/'other.json';other_run=Mock(return_value=({},True))
        def execute(*args):
            with self.assertRaises(BlockingIOError):
                maintenance.run_once(other,other_journal,apply=True,epoch=NOW,executor=other_run)
            other_run.assert_not_called();return {},True
        self.run_step(first,execute)
        self.assertEqual(maintenance.run_once(other,other_journal,apply=True,epoch=NOW,executor=other_run)['status'],'complete')

    def test_clock_rollback_preserves_original_budget_and_pending_step(self):
        value=self.value();run=Mock(return_value=({},False));self.run_step(value,run)
        raw=self.journal.read_bytes();result=self.run_step(value,run,epoch=NOW-1)
        self.assertEqual(result['reason'],'clock_regressed');self.assertEqual(run.call_count,1)
        self.assertEqual(self.journal.read_bytes(),raw)

    def test_window_crossing_after_fsync_retains_reservation_without_operation(self):
        value=self.value();run=Mock(return_value=({},True))
        with patch.object(maintenance.time,'time',side_effect=[NOW,NOW,NOW+1800]):
            result=maintenance.run_once(value,self.journal,apply=True,executor=run)
        self.assertEqual((result['reason'],result['invocations']),('outside_window',1));run.assert_not_called()
        self.assertTrue(result['writes']);self.assertEqual(json.loads(self.journal.read_text())['pending'],0)
        self.assertEqual(self.run_step(value,run,epoch=NOW+86400)['invocations'],2)

    def test_midnight_execution_window_wraps_correctly(self):
        contract={**self.contract,'start_minute_utc':1410,'window_minutes':60,
                  'not_before_epoch':int(NOW-86400)}
        self.assertIsNone(maintenance.execution_gate(contract,NOW-2*3600-15*60))
        self.assertEqual(maintenance.execution_gate(contract,NOW-2*3600),'outside_window')

    def test_expired_pending_approval_remains_readable_and_cannot_restart(self):
        value=self.value();run=Mock(return_value=({},False));self.run_step(value,run)
        raw=self.journal.read_bytes();result=self.run_step(value,run,epoch=value['execution']['expires_epoch'])
        self.assertEqual(result['reason'],'expired');self.assertEqual(run.call_count,1)
        self.assertEqual(self.journal.read_bytes(),raw);self.assertEqual(maintenance.status(value,self.journal)['status'],'pending')

    def test_empty_corrupt_or_missing_budget_journal_is_never_reset(self):
        value=self.value();run=Mock(return_value=({},True))
        for raw in ['', '{','{"plan_id":"wrong"}']:
            self.journal.write_text(raw);self.journal.chmod(0o600)
            with self.assertRaises(ValueError):self.run_step(value,run)
        missing={'plan_id':value['id'],'index':0,'results':{}}
        self.journal.write_text(json.dumps(missing))
        with self.assertRaises(ValueError):self.run_step(value,run)
        run.assert_not_called()

    def test_corrupt_counter_timestamp_and_skipped_results_fail_closed(self):
        value=self.value();run=Mock(return_value=({},False));self.run_step(value,run)
        original=json.loads(self.journal.read_text())
        for changes in [{'calls':False},{'calls':9},{'last_invocation_epoch':None},{'last_invocation_epoch':NOW+1800},
                        {'results':[]},{'pending':True},{'index':1,'pending':1},
                        {'index':1,'results':{},'pending':0}]:
            self.journal.write_text(json.dumps({**original,**changes}))
            with self.assertRaises(ValueError):self.run_step(value,run)
        self.assertEqual(run.call_count,1)

    def test_missing_status_path_is_readonly(self):
        missing=self.root/'not-created'/'journal.json'
        self.assertEqual(maintenance.status(self.value(),missing)['status'],'not_started')
        self.assertFalse(missing.parent.exists())

    def test_unsafe_global_lock_or_journal_link_cannot_execute(self):
        value=self.value();run=Mock(return_value=({},True));target=self.root/'target';target.write_text('private')
        lock=self.root/'.execution.lock';lock.symlink_to(target)
        with self.assertRaises(OSError):self.run_step(value,run)
        lock.unlink();self.journal.symlink_to(target)
        with self.assertRaises(OSError):self.run_step(value,run)
        self.assertEqual(target.read_text(),'private');run.assert_not_called()

    def test_failed_local_lock_open_releases_the_shared_lock(self):
        value=self.value();run=Mock(return_value=({},True));original=os.open
        def fail(path,*args,**kwargs):
            if str(path)==str(self.journal)+'.lock':raise OSError('local lock unavailable')
            return original(path,*args,**kwargs)
        with patch.object(maintenance.os,'open',side_effect=fail):
            with self.assertRaises(OSError):self.run_step(value,run)
        self.assertEqual(self.run_step(value,run)['status'],'complete')

    def test_invalid_clock_does_not_create_files(self):
        for epoch in [True,float('nan'),float('inf'),-1]:
            with self.assertRaises(ValueError):self.run_step(self.value(),Mock(),epoch=epoch)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_missing_journal_after_start_cannot_renew_invocation_budget(self):
        value=self.value();run=Mock(return_value=({},False));self.run_step(value,run)
        self.journal.unlink()
        with self.assertRaisesRegex(ValueError,'do not reset'):self.run_step(value,run)
        with self.assertRaises(ValueError):maintenance.status(value,self.journal)
        self.assertEqual(run.call_count,1);self.assertFalse(self.journal.exists())

    def test_missing_anchor_with_existing_journal_cannot_resume(self):
        value=self.value();run=Mock(return_value=({},False));self.run_step(value,run)
        self.journal.with_name(self.journal.name+'.lock').unlink();raw=self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError,'anchor is missing'):self.run_step(value,run)
        with self.assertRaises(ValueError):maintenance.status(value,self.journal)
        self.assertEqual(run.call_count,1);self.assertEqual(self.journal.read_bytes(),raw)

    def test_interruption_after_anchor_before_initial_journal_fails_closed(self):
        value=self.value();run=Mock(return_value=({},True))
        with patch.object(maintenance.os,'replace',side_effect=OSError('before journal publish')):
            with self.assertRaises(OSError):self.run_step(value,run)
        self.assertFalse(self.journal.exists());run.assert_not_called()
        with self.assertRaisesRegex(ValueError,'do not reset'):self.run_step(value,run)
        run.assert_not_called()

    def test_retention_selector_can_bind_the_execution_contract_in_same_plan(self):
        import test_maintenance_policy as fixtures
        import maintenance_policy
        fixture=fixtures.RetentionTests();fixture.setUp()
        try:
            ident=fixture.add(1)
            @contextmanager
            def context(prefix):yield fixture.db,{'prefix':str(self.root)},SimpleNamespace(gid=os.getgid())
            with patch.object(copies,'context',context):
                result=maintenance_policy.plan(self.root,fixture.settings,epoch=NOW,execution=self.contract)
            value=result['maintenance_plan']
            self.assertEqual(value['schema'],'ascend-lab-maintenance/v2')
            self.assertEqual(value['execution'],self.contract);self.assertEqual(result['selected'],[ident])
            self.assertEqual(value['steps'][0]['plan']['selection'],[ident])
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()

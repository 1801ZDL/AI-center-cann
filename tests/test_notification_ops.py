"""Finite notification decisions using private tiny SQL/files; no real delivery.

Root installation context and HTTP are replaced. SQLite synchronous=OFF applies
only to disposable fixtures; no power-loss or installed Root claim is made.
"""
from contextlib import contextmanager,nullcontext
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

import notification_ops as ops
import notifications as outbox


class Crash(BaseException):pass


class NotificationOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ascend-lab-notification-ops-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'jobs.sqlite3';self.db=self.connect();self.addCleanup(self.db.close)
        outbox.initialize(self.db);self.db.commit()
        self.binding={'prefix':'/fixture/controller','release_id':'a'*64,'state_dir':'/fixture/state',
                      'uid':os.getuid(),'gid':os.getgid(),'database_identity':[self.path.stat().st_dev,self.path.stat().st_ino]}
        self.destinations={'alice':{'url':'https://example.invalid/private-hook?secret=not-published'}}
        self.event=self.emit();self.fp=outbox.destination_fingerprint(self.destinations['alice'])

    def connect(self):
        db=sqlite3.connect(self.path);db.execute('PRAGMA synchronous=OFF');db.execute('PRAGMA journal_mode=MEMORY');return db

    def emit(self,owner='alice',epoch=1):
        identifier=outbox.append(self.db,owner,'job_state','job-'+'a'*12,'failed',{'enabled':True},epoch=epoch)
        self.db.commit();return identifier

    def change(self,sql,values=()):self.db.execute(sql,values);self.db.commit()

    def plan(self,decision='hold',identifier=None,**kwargs):
        return ops.plan_in_db(self.db,self.binding,identifier or self.event,decision,destinations=self.destinations,epoch=100,**kwargs)

    def apply(self,value,**kwargs):
        return ops.apply_in_db(self.db,self.binding,value,destinations=self.destinations,epoch=101,**kwargs)

    def row(self):return self.db.execute('SELECT * FROM notification_outbox WHERE id=?',(self.event,)).fetchone()

    def archive_directory(self):
        path=self.root/'archive';path.mkdir(mode=0o700);return path

    def test_plan_is_readonly_and_contains_no_destination_url_or_payload(self):
        before=list(self.db.iterdump());value=self.plan()
        self.assertEqual(before,list(self.db.iterdump()));raw=json.dumps(value)
        self.assertNotIn('example.invalid',raw);self.assertNotIn('not-published',raw);self.assertNotIn('lease_token',raw)
        ops.check(value)

    def test_hold_preserves_attempt_count_and_owner_page_is_private(self):
        before=self.row();result=self.apply(self.plan())
        self.assertFalse(result['sent']);self.assertEqual(result['state'],'held');self.assertEqual(self.row()[5],before[5])
        self.assertEqual(outbox.page(self.db,'alice')['events'][0]['delivery_state'],'held')
        self.assertEqual(outbox.page(self.db,'bob')['events'],[])
        sender=Mock();self.assertEqual(outbox.dispatch_one(self.db,self.destinations,transport=sender,epoch=102)['status'],'idle');sender.assert_not_called()

    def test_exhausted_retry_extends_finite_budget_without_resetting_attempts_or_id(self):
        self.change('UPDATE notification_outbox SET attempts=8,destination_sha=?',(self.fp,))
        with self.assertRaises(ValueError):self.plan('retry')
        value=self.plan('retry',extra_attempts=1);self.apply(value)
        self.assertEqual((self.row()[0],self.row()[5]),(self.event,8));self.assertEqual(ops.control(self.db,self.event)[2],9)
        sender=Mock();self.assertEqual(outbox.dispatch_one(self.db,self.destinations,transport=sender,epoch=102)['status'],'delivered')
        self.assertEqual(sender.call_args.args[1]['id'],self.event);self.assertEqual(self.row()[5],9)

    def test_retry_budget_bounds_and_closed_events_cannot_reopen(self):
        for extra in [-1,True,9]:
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.plan('retry',extra_attempts=extra)
        self.apply(self.plan('close'))
        with self.assertRaises(ValueError):self.plan('retry',extra_attempts=1)
        self.assertIsNone(self.row()[4]);self.assertEqual(outbox.outbox_status(self.db)['closed'],1)

    def test_total_retry_budget_cannot_exceed_64(self):
        for _ in range(7):self.apply(self.plan('retry',extra_attempts=8))
        self.assertEqual(ops.control(self.db,self.event)[2],64)
        with self.assertRaises(ValueError):self.plan('retry',extra_attempts=1)
        self.apply(self.plan('close'));self.assertEqual(ops.control(self.db,self.event)[3],8)
        result=outbox.archive_delivered(self.db,self.archive_directory(),apply=True)
        self.assertLessEqual((self.root/'archive'/(result['sha256']+'.json')).stat().st_size,65536)

    def test_active_lease_and_expired_uncertain_lease_are_not_silently_cleared(self):
        self.change('UPDATE notification_outbox SET attempts=1,lease_token=?,lease_until=200',('b'*32,))
        with self.assertRaises(ValueError):self.plan('close',accept_uncertain=True)
        self.change('UPDATE notification_outbox SET lease_until=99')
        with self.assertRaises(ValueError):self.plan('hold')
        self.apply(self.plan('hold',accept_uncertain=True));self.assertIsNone(self.row()[8]);self.assertEqual(self.row()[5],1)

    def test_close_after_any_attempt_requires_uncertainty_acceptance_and_never_claims_delivery(self):
        self.change('UPDATE notification_outbox SET attempts=1')
        with self.assertRaises(ValueError):self.plan('close')
        result=self.apply(self.plan('close',accept_uncertain=True))
        self.assertEqual(result['delivery_outcome'],'unknown');self.assertIsNone(self.row()[4])
        event=outbox.page(self.db,'alice')['events'][0];self.assertFalse(event['delivered']);self.assertEqual(event['delivery_state'],'closed')

    def test_rebind_is_explicit_owner_bound_and_does_not_send(self):
        self.change('UPDATE notification_outbox SET attempts=1,destination_sha=?',(self.fp,))
        self.destinations['alice']={'url':'https://example.invalid/new-hook'}
        with self.assertRaises(ValueError):self.plan('retry')
        with self.assertRaises(ValueError):self.plan('rebind')
        value=self.plan('rebind',accept_uncertain=True);self.apply(value)
        self.assertEqual(self.row()[9],outbox.destination_fingerprint(self.destinations['alice']));self.assertEqual(self.row()[5],1)
        self.assertEqual(ops.inspect_events(self.db,destinations=self.destinations)['events'][0]['destination'],'same')

    def test_changed_destination_does_not_starve_another_owner_or_new_event(self):
        self.change('UPDATE notification_outbox SET destination_sha=?',(self.fp,))
        bob=self.emit('bob',epoch=2);fresh=self.emit('alice',epoch=3)
        targets={'alice':{'url':'https://example.invalid/new-hook'},'bob':{'url':'https://example.invalid/bob'}}
        sender=Mock()
        self.assertEqual(outbox.dispatch_one(self.db,targets,transport=sender,epoch=10)['event_id'],bob)
        self.assertEqual(outbox.dispatch_one(self.db,targets,transport=sender,epoch=11)['event_id'],fresh)
        self.assertEqual(outbox.dispatch_one(self.db,targets,transport=sender,epoch=12),{'status':'destination_changed','event_id':self.event})
        self.assertEqual(sender.call_count,2);self.assertIsNone(self.row()[4])

    def test_same_destination_retry_cannot_use_new_owner_route(self):
        self.change('UPDATE notification_outbox SET destination_sha=?',(self.fp,));value=self.plan('retry')
        self.destinations['alice']={'url':'https://example.invalid/changed'}
        with self.assertRaises(ValueError):self.apply(value)
        self.assertIsNone(ops.control(self.db,self.event));self.assertEqual(self.row()[5],0)

    def test_stale_preimage_binding_and_expired_or_future_decision_are_rejected(self):
        value=self.plan();self.change('UPDATE notification_outbox SET attempts=1')
        with self.assertRaises(ValueError):self.apply(value)
        value=self.plan('hold')
        for at in [99,3700]:
            with self.subTest(at=at),self.assertRaises(ValueError):ops.apply_in_db(self.db,self.binding,value,epoch=at)
        with self.assertRaises(ValueError):ops.apply_in_db(self.db,{**self.binding,'release_id':'b'*64},value,epoch=101)

    def test_lost_reply_replays_original_receipt_without_renewing_validity_or_budget(self):
        value=self.plan('retry',extra_attempts=1)
        def checkpoint(stage):
            if stage=='committed':raise Crash()
        with patch.object(ops,'_checkpoint',checkpoint),self.assertRaises(Crash):self.apply(value)
        before=list(self.db.iterdump())
        result=ops.apply_in_db(self.db,self.binding,value,destinations={},epoch=10000)
        self.assertEqual(result['status'],'already_applied');self.assertEqual(before,list(self.db.iterdump()))
        self.assertEqual(ops.control(self.db,self.event)[2],9)

    def test_before_commit_failure_rolls_back_both_decision_and_original_event(self):
        before=list(self.db.iterdump())
        with patch.object(ops,'_checkpoint',side_effect=Crash()),self.assertRaises(Crash):self.apply(self.plan())
        self.assertEqual(before,list(self.db.iterdump()))

    def test_decision_ledger_reserves_room_to_close_tracked_events_when_full(self):
        with patch.object(ops,'MAX_OPERATIONS',3):
            self.apply(self.plan());self.apply(self.plan('retry'))
            with self.assertRaises(ValueError):self.apply(self.plan())
            self.apply(self.plan('close'))
            self.assertEqual(self.db.execute('SELECT count(*) FROM notification_operations').fetchone()[0],3)
            outbox.archive_delivered(self.db,self.archive_directory(),apply=True)
            self.assertEqual(self.db.execute('SELECT count(*) FROM notification_operations').fetchone()[0],0)

    def test_last_per_event_slot_is_reserved_for_close(self):
        with patch.object(ops,'MAX_EVENT_OPERATIONS',2):
            self.apply(self.plan())
            with self.assertRaises(ValueError):self.plan('retry')
            self.apply(self.plan('close'));self.assertEqual(ops.control(self.db,self.event)[3],2)

    def test_closed_event_archive_preserves_exact_decisions_without_marking_delivered(self):
        self.apply(self.plan('close'));pending=self.emit('bob')
        result=outbox.archive_delivered(self.db,self.archive_directory(),apply=True)
        value=json.loads((self.root/'archive'/(result['sha256']+'.json')).read_text())
        self.assertEqual(value['schema'],'ascend-lab-notification-archive/v2');self.assertIsNone(value['rows'][0][4])
        self.assertEqual(value['event_controls'][0][1],'closed');self.assertEqual(len(value['operations']),1)
        self.assertEqual(self.db.execute('SELECT id FROM notification_outbox').fetchall(),[(pending,)])
        self.assertEqual(self.db.execute('SELECT count(*) FROM notification_operations').fetchone()[0],0)

    def test_archive_compare_and_swap_protects_decision_evidence(self):
        self.apply(self.plan('close'));original=ops.archive_evidence;calls=0
        def evidence(db,ids):
            nonlocal calls
            calls+=1;value=original(db,ids)
            if calls>1:return {**value,'operations':[]}
            return value
        with patch.object(ops,'archive_evidence',evidence),self.assertRaises(ValueError):
            outbox.archive_delivered(self.db,self.archive_directory(),apply=True)
        self.assertIsNotNone(self.row());self.assertIsNotNone(ops.control(self.db,self.event))

    def test_missing_or_changed_original_decision_stops_dispatch(self):
        self.apply(self.plan('retry'))
        self.change('DELETE FROM notification_operations')
        with self.assertRaises(ValueError):outbox.dispatch_one(self.db,self.destinations,transport=Mock(),epoch=102)
        self.assertEqual(self.row()[5],0)

    def test_missing_control_table_or_row_does_not_silently_resume_held_event(self):
        self.apply(self.plan())
        self.change('DELETE FROM notification_event_control')
        sender=Mock()
        with self.assertRaises(ValueError):outbox.dispatch_one(self.db,self.destinations,transport=sender,epoch=102)
        self.change('DROP TABLE notification_event_control')
        with self.assertRaises(ValueError):outbox.dispatch_one(self.db,self.destinations,transport=sender,epoch=102)
        sender.assert_not_called()

    def test_disappearing_earlier_receipt_invalidates_current_control(self):
        first=self.plan();self.apply(first);self.apply(self.plan('retry'))
        self.change('DELETE FROM notification_operations WHERE id=?',(first['id'],))
        with self.assertRaises(ValueError):ops.current(self.db,self.event)

    def test_adaptive_archive_keeps_complete_event_histories_under_64k(self):
        identifiers=[self.event]+[self.emit(epoch=i+2) for i in range(31)]
        for identifier in identifiers:
            self.apply(self.plan(identifier=identifier));self.apply(self.plan('close',identifier=identifier))
        directory=self.archive_directory();result=outbox.archive_delivered(self.db,directory,apply=True)
        self.assertTrue(0<result['rows']<32)
        value=json.loads((directory/(result['sha256']+'.json')).read_text())
        self.assertLessEqual((directory/(result['sha256']+'.json')).stat().st_size,65536)
        self.assertEqual(len(value['operations']),2*result['rows'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM notification_operations').fetchone()[0],2*(32-result['rows']))

    def test_tampered_plan_and_projection_are_rejected(self):
        original=self.plan()
        for field,value in [('owner','bob'),('decision','retry'),('retry_limit',64),('accept_uncertain',True)]:
            changed=copy.deepcopy(original);changed[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.apply(changed)
        self.apply(original)
        self.change('UPDATE notification_event_control SET state="active"')
        with self.assertRaises(ValueError):outbox.dispatch_one(self.db,self.destinations,transport=Mock(),epoch=102)

    def test_invalid_destinations_and_plan_limits_fail_before_mutation(self):
        for targets in [[],{'alice':{'url':'http://example.invalid'}},{str(i):{'url':'https://example.invalid'} for i in range(129)}]:
            with self.subTest(kind=type(targets).__name__),self.assertRaises(ValueError):ops.inspect_events(self.db,destinations=targets)
        self.assertIsNone(ops.control(self.db,self.event))

    def test_cli_list_plan_apply_routes_never_call_dispatch(self):
        import runpy
        import sys
        namespace=runpy.run_path(str(Path(ops.__file__).with_name('opsctl')))
        main=namespace['main'];main.__globals__['limit']=lambda:None  # Outer focused limits remain in force.
        value=self.plan();path=self.root/'approved.json';path.write_text(json.dumps(value));path.chmod(0o600)
        with patch.object(ops,'inspect',return_value={'sent':False}) as inspect, \
             patch.object(ops,'plan',return_value=value) as plan,patch.object(ops,'apply',return_value={'sent':False}) as apply, \
             patch.object(outbox,'dispatch_one',side_effect=AssertionError('Unexpected send')):
            with patch.object(sys,'argv',['opsctl','notification-list','--prefix','/fixture/controller']):self.assertFalse(main()['sent'])
            with patch.object(sys,'argv',['opsctl','notification-plan','--prefix','/fixture/controller','--event-id',self.event,'--decision','hold']):self.assertEqual(main(),value)
            with patch.object(sys,'argv',['opsctl','notification-apply','--prefix','/fixture/controller','--plan',str(path)]):self.assertFalse(main()['sent'])
            inspect.assert_called_once();plan.assert_called_once();apply.assert_called_once()

    def test_original_attempt_completion_cannot_overwrite_closed_event_after_lease_expiry(self):
        def transport(*_):
            value=ops.plan_in_db(self.db,self.binding,self.event,'close',epoch=35,accept_uncertain=True)
            ops.apply_in_db(self.db,self.binding,value,epoch=35)
        result=outbox.dispatch_one(self.db,self.destinations,transport=transport,epoch=2)
        self.assertEqual(result['status'],'lease_superseded');self.assertIsNone(self.row()[4]);self.assertEqual(ops.control(self.db,self.event)[1],'closed')

    def test_inspection_is_keyset_paged_and_contains_no_urls_or_lease_tokens(self):
        self.emit('bob');first=ops.inspect_events(self.db,destinations=self.destinations,limit=1)
        second=ops.inspect_events(self.db,destinations=self.destinations,limit=1,after_id=first['next_after'])
        self.assertNotEqual(first['events'][0]['event_id'],second['events'][0]['event_id'])
        raw=json.dumps(first);self.assertNotIn('example.invalid',raw);self.assertNotIn('lease_token',raw)
        summary=ops.inspect_events(self.db)['events'][0]
        self.assertEqual(summary['destination'],'not_checked');self.assertEqual(summary['event'],'job_state')
        self.assertEqual(summary['subject'],'job-'+'a'*12)
        for limit in [0,True,33]:
            with self.subTest(limit=limit),self.assertRaises(ValueError):ops.inspect_events(self.db,limit=limit)

    def test_restore_and_downgrade_preserve_decisions(self):
        self.apply(self.plan());stage=self.root/'staged';stage.mkdir()
        with sqlite3.connect(stage/'jobs.sqlite3') as target:self.db.backup(target)
        outbox.require_history(self.root,stage)
        with self.assertRaises(ValueError):outbox.require_compatible(self.root,'controller',{'files':{'notifications.py':{}}})
        outbox.require_compatible(self.root,'controller',{'files':{'notifications.py':{},'notification_ops.py':{}}})
        with sqlite3.connect(stage/'jobs.sqlite3') as db:db.execute('DELETE FROM notification_operations')
        with self.assertRaises(ValueError):outbox.require_history(self.root,stage)

    def test_legacy_database_without_control_tables_still_dispatches_and_accepts_first_decision(self):
        self.change('DROP TABLE notification_event_control');self.change('DROP TABLE notification_operations')
        self.assertEqual(outbox.page(self.db,'alice')['events'][0]['delivery_attempt_limit'],8)
        value=self.plan();self.apply(value);self.assertIsNotNone(ops.control(self.db,self.event))

    def test_installed_context_requires_root_and_uses_shared_state_lock(self):
        with patch('deployment.require_root',side_effect=ValueError('Root required')),patch('deployment.verify') as verify:
            with self.assertRaises(ValueError):ops.inspect(self.root)
            verify.assert_not_called()
        release=Path(ops.__file__).parent
        meta={'role':'controller','release_id':'a'*64,'state_dir':str(self.root),'uid':os.getuid(),'gid':os.getgid()}
        with patch('deployment.require_root'),patch('deployment.verify',return_value=(self.root,meta,release)), \
             patch('audit_retention.service_identity',return_value=nullcontext()):
            value=ops.plan(self.root,self.event,'hold',epoch=100)
            self.assertEqual(value['binding']['database_identity'],self.binding['database_identity'])
            self.assertFalse(ops.apply(self.root,value,epoch=101)['sent'])


if __name__=='__main__':unittest.main()

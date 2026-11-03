"""Interleaved snapshots are verified once per bounded page, with live ACLs.

Root/context are explicit unit stand-ins. The optional load fixture pads only
historical result context; it never fabricates a device measurement or attempt.
"""
import json,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_control_history as fixture
import control_history as h
import control_rotation as r
import history_capacity as c
from jobs import JobError


class HistoryFixture:
    def __init__(self,padding=0):
        self.env=fixture.ExportTests();self.env.setUp();self.jobs=self.env.jobs;self.base=self.env.base;self.prefix=self.env.prefix
        self.patches=[patch.object(r,'context',h.context),patch.object(c,'context',h.context)]
        for p in self.patches:p.start()
        self.keys=[self.env.job['id']]
        for i in range(3):
            job,_=self.jobs.submit(self.base.request(idempotency_key='batch-history-'+str(i)),self.base.alice)
            self.keys.append(self.jobs.cancel(job['id'],self.base.alice)['id'])
        c.apply(self.prefix,c.plan(self.prefix,4,4));self.jobs.history=c.Capacity(self.jobs)
        self.jobs.policy['storage_trash_grace_seconds']=1
        for ident in self.keys:
            for action in ['trash','purge']:
                with patch('storage_lifecycle.time.time',return_value=time.time()+(10 if action=='purge' else 0)):
                    plan=self.jobs.lifecycle.plan(self.base.alice,{'kind':'archive','id':ident,'action':action})['plan']
                    value=self.jobs.lifecycle.apply(self.base.alice,plan)
                assert not value.get('pending')
        if padding:
            with self.jobs.transaction() as db:
                for ident in self.keys:
                    value={'job_id':ident,'owner':'alice','kind':'ir','passed':False,'metrics':{},'explicit_load_fixture':'x'*padding}
                    db.execute('INSERT INTO workflow_results VALUES(?,?)',(ident,json.dumps(value)))
        self.originals=[self.jobs.get(k) for k in self.keys];self.copies=[]
        for keys in [self.keys[::2],self.keys[1::2]]:
            plan=h.plan(self.prefix,keys,{'bytes':64*1024**2,'exports':8})
            for _ in range(40):
                if h.step(self.prefix,plan,1024**2)['state']=='verified':break
            else:raise AssertionError('Bounded load fixture copy did not complete')
            r.apply(self.prefix,r.plan(self.prefix,plan['id']));self.copies.append(plan)
    def close(self):
        for p in reversed(getattr(self,'patches',[])):p.stop()
        if hasattr(self,'env'):self.env.tearDown()


class BatchHistoryTests(unittest.TestCase):
    def setUp(self):
        self.env=HistoryFixture.__new__(HistoryFixture);self.addCleanup(self.env.close);self.env.__init__();self.jobs=self.env.jobs
    def test_interleaved_page_verifies_each_copy_once_and_keeps_requested_order(self):
        with patch.object(h,'checked',wraps=h.checked) as checked:
            page=self.jobs.history_page(self.env.base.alice,100)
            self.assertEqual([j['id'] for j in page['jobs']],self.env.keys[::-1]);self.assertEqual(checked.call_count,2)
            self.jobs.history_page(self.env.base.alice,100);self.assertEqual(checked.call_count,4)
            self.jobs.history_page(self.env.base.alice,1,1);self.assertEqual(checked.call_count,5)
    def test_dashboard_and_all_retain_original_order_and_scalar_projection(self):
        with patch.object(h,'checked',wraps=h.checked) as checked:
            with self.jobs.connect() as db:rows,counts=self.jobs.history.dashboard(db,self.env.base.alice)
            self.assertEqual(rows,self.env.originals);self.assertEqual(counts['cancelled'],4);self.assertEqual(checked.call_count,2)
            self.assertEqual(self.jobs.all(),self.env.originals);self.assertEqual(checked.call_count,4)
    def test_peer_filter_runs_before_file_reads_even_when_all_copies_are_missing(self):
        for plan in self.env.copies:(self.env.prefix/h.DIRECTORY/(plan['id']+'.json')).unlink()
        with patch.object(h,'checked',side_effect=AssertionError('Peer must not open a private snapshot')):
            self.assertEqual(self.jobs.history_page(self.env.base.bob)['total'],0)
            with self.jobs.connect() as db:rows,counts=self.jobs.history.dashboard(db,self.env.base.bob)
            self.assertEqual(rows,[]);self.assertEqual(sum(counts.values()),0)
        with self.assertRaisesRegex(JobError,'unavailable'):self.jobs.history_page(self.env.base.alice)
    def test_next_request_rechecks_original_bytes_and_batch_rejects_scalar_mismatch(self):
        self.jobs.history_page(self.env.base.alice)
        target=self.env.prefix/h.DIRECTORY/(self.env.copies[0]['id']+'.json');target.chmod(0o600);raw=target.read_bytes();target.write_bytes(raw[:-1]+b'!');target.chmod(0o440)
        with self.assertRaisesRegex(JobError,'unavailable'):self.jobs.history_page(self.env.base.alice)
        with self.jobs.transaction() as db:db.execute("UPDATE jobs SET owner='wrong-owner' WHERE id=?",(self.env.keys[-1],))
        with self.assertRaisesRegex(JobError,'columns'):self.jobs.history_page(self.env.base.alice)


if __name__=='__main__':unittest.main()

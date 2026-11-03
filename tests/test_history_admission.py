"""Private snapshot read admission is bounded, reentrant and separate from jobs."""
import json,sys,threading,unittest,urllib.error,urllib.request
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_history_batch import HistoryFixture
import control_rotation as r
import control_history as h
import history_capacity as c
from jobs import JobError
from deployment import DeployError
from server import ThreadingHTTPServer,handler_class


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.env=HistoryFixture.__new__(HistoryFixture);self.addCleanup(self.env.close);self.env.__init__();self.jobs=self.env.jobs
        self.patches=[patch.object(r,'_read_slots',threading.BoundedSemaphore(1)),patch.object(r,'READ_WAIT_SECONDS',.05)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def held(self):
        entered=threading.Event();release=threading.Event()
        def hold():
            with r.read_admission():entered.set();release.wait(5)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(entered.wait(2))
        self.addCleanup(lambda:(release.set(),thread.join(3)));return release,thread
    def test_busy_private_read_has_finite_wait_and_current_peer_is_denied_before_admission(self):
        release,thread=self.held();ident=self.env.keys[0]
        with self.assertRaises(JobError) as error:self.jobs.get(ident,principal=self.env.base.alice)
        self.assertEqual(error.exception.status,503);self.assertEqual(error.exception.retry_after_seconds,1)
        with self.assertRaises(JobError) as denied:self.jobs.get(ident,principal=self.env.base.bob)
        self.assertEqual(denied.exception.status,404)
        self.assertEqual(self.jobs.history_page(self.env.base.bob)['total'],0)
        release.set();thread.join(2);self.assertEqual(self.jobs.get(ident)['id'],ident)
    def test_nonmigrated_submission_and_read_do_not_consume_private_read_permits(self):
        release,thread=self.held()
        new,reused=self.jobs.submit(self.env.base.request(idempotency_key='new-while-history-busy'),self.env.base.alice)
        self.assertFalse(reused);self.assertEqual(self.jobs.get(new['id'])['state'],'queued');self.assertFalse(self.jobs.attempts(new['id']))
        self.assertEqual(self.jobs.history.working(next_db:=self.jobs.connect(),{'queued'})[0]['id'],new['id']);next_db.close()
        release.set();thread.join(2)
    def test_failed_copy_read_releases_permit_and_nested_batch_keeps_original_validation(self):
        with patch.object(h,'checked',side_effect=DeployError('Explicit unavailable unit copy')):
            with self.assertRaisesRegex(JobError,'unavailable'):self.jobs.history_page(self.env.base.alice)
        self.assertEqual(self.jobs.history_page(self.env.base.alice)['total'],4)
        with self.jobs.connect() as db:
            r.validate(db);self.assertEqual(c.snapshot(db)[0]['control_jobs'],0)
            self.assertEqual([j['id'] for j in r.all_jobs(db)],self.env.keys)
        self.assertEqual(self.jobs.history_page(self.env.base.alice)['total'],4)
    def test_direct_snapshot_verification_uses_the_same_global_admission(self):
        release,thread=self.held()
        with self.jobs.connect() as db:record=h.rows(db)[self.env.copies[0]['id']][1]
        with self.assertRaises(JobError) as error:h.checked(self.env.prefix,record)
        self.assertEqual(error.exception.status,503)
        release.set();thread.join(2);self.assertTrue(h.checked(self.env.prefix,record)[0])
    def test_http_busy_read_advertises_retry_without_blocking_health_or_peer_page(self):
        config={**self.env.base.config,'interval':15};server=ThreadingHTTPServer(('127.0.0.1',0),handler_class(Mock(),config,self.jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def cleanup():server.shutdown();server.server_close();thread.join(3)
        self.addCleanup(cleanup)
        tokens={name:self.jobs.access.issue_token(name,['jobs:read']) for name in ['alice','bob']}
        def request(path,name=None):
            headers={'Authorization':'Bearer '+tokens[name]} if name else {}
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=3) as response:return response.status,dict(response.headers),json.load(response)
            except urllib.error.HTTPError as error:return error.code,dict(error.headers),json.load(error)
        release,holder=self.held()
        status,headers,body=request('/api/jobs/history','alice');self.assertEqual(status,503)
        self.assertEqual(headers['Retry-After'],'1');self.assertEqual(body['retry_after_seconds'],1)
        self.assertEqual(request('/api/jobs/history','bob')[2]['total'],0);self.assertEqual(request('/healthz')[0],200)
        release.set();holder.join(2);self.assertEqual(request('/api/jobs/history','alice')[2]['total'],4)


if __name__=='__main__':unittest.main()

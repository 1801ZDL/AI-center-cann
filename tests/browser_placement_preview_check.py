"""Packaged dashboard and real HTTP API, plus main-service read-only browser check.

Fixture Node heartbeats are synthetic CPU protocol observations. No Node worker,
Docker launch, NPU access or model is invoked. A queued fixture task is cancelled.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import server as server_module
from jobs import JobStore,now
from deployment import bundle,unpack_release
from lab_client import Client,atomic_json


def main():
    jobdir=Path(os.environ['LAB_JOB_DIR']);base=jobdir/'tmp/browser-preview';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    web_hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['web/app.js','web/index.html','web/style.css']}
    assert all(hashlib.sha256((code/name).read_bytes()).hexdigest()==sha for name,sha in web_hashes.items())
    runtime=base/'fixture';runtime.mkdir(mode=0o700)
    config={'username':'preview_fixture_admin','password':'local-fixture-only','container':'unused','interval':15}
    jobs=JobStore(runtime,config);passwords={}
    for owner in ['alice','bob']:
        home=runtime/owner;home.mkdir(mode=0o700)
        passwords[owner]=jobs.access.create_user(owner,home,environments=['worker-python'])
    cpu=jobs.policy['cpu_ids'][0]
    profile={'name':'alice-node','physical_id':jobs.backend.physical_id,
        'users':{'alice':{'uid':23001,'gid':23001}},'templates':['cpu_demo'],
        'policy':{'cpu_ids':[cpu],'memory_mib':768,'shm_mib':0,'max_running':1}}
    alice_node,_=jobs.nodes.enroll(profile)
    bob_node,_=jobs.nodes.enroll({**profile,'name':'bob-private-node','users':{'bob':{'uid':23002,'gid':23002}}})
    for node in [alice_node,bob_node]:
        jobs.nodes.heartbeat(node,{'physical_id':jobs.backend.physical_id,'protocol_version':1,
            'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],'cpu_ids':jobs.policy['cpu_ids'],
            'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
    # Static assets are served from the actual generated release, not the checkout.
    original_root=server_module.ROOT;server_module.ROOT=code
    http=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(latest=Mock(return_value=None)),config,jobs))
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{http.server_port}';errors=[];checks=[];requests=[]
    user=Client({'controller':url,'username':'alice','password':passwords['alice']})

    def dump():
        with jobs.connect() as db:return list(db.iterdump())

    def open_page(browser,headers=None):
        context=browser.new_context(viewport={'width':1440,'height':1080},locale='zh-CN',
            **({'extra_http_headers':headers} if headers else {'http_credentials':{'username':'alice','password':passwords['alice']}}))
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda req:requests.append({'path':req.url.removeprefix(url),'method':req.method}) if req.method=='POST' else None)
        page.goto(url+'/#jobs',wait_until='networkidle');page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
        return context,page

    def click_preview(page):
        page.locator('#preview-job').click()
        expect(page.locator('#job-preview')).to_have_attribute('data-loading','false',timeout=20000)

    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            context,page=open_page(browser)
            page.locator('#auto-refresh').click()
            expect(page.locator('#preview-job')).to_be_enabled()
            before=dump();click_preview(page)
            expect(page.locator('#preview-title')).to_have_text('当前检查未发现运行阻碍')
            expect(page.locator('#preview-candidates li')).to_have_count(1)
            expect(page.locator('#preview-candidates')).to_contain_text(alice_node)
            expect(page.locator('#job-preview')).not_to_contain_text(bob_node)
            expect(page.locator('#preview-note')).to_contain_text('不预留资源')
            assert dump()==before and not jobs.all()
            checks.append('Packaged UI uses real preview API; preview candidates exclude unmapped peer and ledger is unchanged')
            expect(page.locator('#job-preview')).to_have_attribute('data-expired','true',timeout=20000)
            expect(page.locator('#preview-title')).to_contain_text('已过时')
            assert dump()==before and not jobs.all()
            checks.append('A real fresh preview expires after its display window without any task or ledger write')
            # Refresh only the synthetic fixture after waiting for the real timer.
            for node in [alice_node,bob_node]:
                jobs.nodes.heartbeat(node,{'physical_id':jobs.backend.physical_id,'protocol_version':1,
                    'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],'cpu_ids':jobs.policy['cpu_ids'],
                    'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})

            page.locator('#job-name').fill('changed request')
            expect(page.locator('#job-preview')).to_be_hidden()
            # Hold a real HTTP response in the browser until after a form edit.
            page.evaluate("""() => { window.originalPreviewFetch=window.fetch; window.fetch=(path,...rest)=>{
              const response=window.originalPreviewFetch(path,...rest);response.catch(()=>{});
              if(path==='/api/jobs/preview')return new Promise((resolve,reject)=>{window.releasePreview=()=>response.then(resolve,reject);});
              return response;
            }; }""")
            page.locator('#preview-job').click()
            expect(page.locator('#job-preview')).to_have_attribute('data-loading','true')
            page.locator('#job-seconds').fill('3')
            page.evaluate('window.releasePreview()')
            page.wait_for_timeout(150)
            expect(page.locator('#job-preview')).to_be_hidden()
            page.evaluate('window.fetch=window.originalPreviewFetch')
            checks.append('Form edits invalidate results and late real responses cannot repaint a different request')

            # A fresh report that is already old on receipt must not look current.
            def expired(route):
                response=route.fetch();value=response.json();value['observed_at']='2000-01-01T00:00:00+00:00'
                route.fulfill(response=response,json=value)
            page.route('**/api/jobs/preview',expired);click_preview(page)
            expect(page.locator('#job-preview')).to_have_attribute('data-expired','true')
            expect(page.locator('#preview-title')).to_contain_text('已过时')
            page.unroute('**/api/jobs/preview',expired)
            checks.append('Old observations are marked historical rather than guaranteed runnable')

            # Render hostile strings as text even if an upstream response is bad.
            hostile='<img src=x onerror="window.previewInjected=true">'
            def injected(route):
                response=route.fetch();value=response.json();value['candidates'][0].update(node_id=hostile,reason=hostile,status='blocked')
                route.fulfill(response=response,json=value)
            page.route('**/api/jobs/preview',injected);click_preview(page)
            expect(page.locator('#preview-candidates')).to_contain_text(hostile)
            assert page.locator('#preview-candidates img').count()==0
            assert not page.evaluate('Boolean(window.previewInjected)')
            page.unroute('**/api/jobs/preview',injected)
            checks.append('Candidate and reason text cannot inject executable HTML')

            # A failed preview remains a failed read; there is no submit fallback.
            def offline(route):route.fulfill(status=503,json={'error':'fixture preview unavailable'})
            page.route('**/api/jobs/preview',offline);click_preview(page)
            expect(page.locator('#preview-title')).to_have_text('预览未完成')
            expect(page.locator('#preview-summary')).to_have_text('fixture preview unavailable')
            assert not jobs.all();page.unroute('**/api/jobs/preview',offline)
            assert all(item['path']!='/api/jobs' for item in requests)
            checks.append('Preview failures never create tasks or invoke submission fallback')

            # A genuine held network request is bounded, and retry remains usable.
            release_slow=threading.Event();finished_slow=threading.Event();original_preview=jobs.preview
            def stalled(*args,**kwargs):
                try:
                    assert release_slow.wait(30),'fixture server was not released'
                    return original_preview(*args,**kwargs)
                finally:finished_slow.set()
            jobs.preview=stalled
            try:
                started=time.monotonic();page.locator('#preview-job').click()
                expect(page.locator('#preview-title')).to_have_text('预览未完成',timeout=20000)
                assert 14<=time.monotonic()-started<=21
                expect(page.locator('#preview-summary')).to_contain_text('超过 15 秒')
                expect(page.locator('#preview-job')).to_be_enabled()
                assert not jobs.all()
            finally:
                release_slow.set();jobs.preview=original_preview
                assert finished_slow.wait(10),'fixture read did not finish after client abort'
            for node in [alice_node,bob_node]:
                jobs.nodes.heartbeat(node,{'physical_id':jobs.backend.physical_id,'protocol_version':1,
                    'capabilities':['start-ticket-v1','cleanup-before-complete-v1'],'cpu_ids':jobs.policy['cpu_ids'],
                    'memory_available_bytes':8*1024**3,'disk_free_bytes':8*1024**3})
            click_preview(page)
            expect(page.locator('#preview-title')).to_have_text('当前检查未发现运行阻碍')
            checks.append('Real stalled network request times out after15 seconds, creates no job and permits retry')

            # Current static capacity failure must not prevent intentional queueing.
            with jobs.transaction() as db:
                value=jobs.nodes.node(alice_node,db)['profile'];value['policy']['memory_mib']=256
                db.execute('UPDATE worker_nodes SET profile=? WHERE id=?',(json.dumps(value),alice_node))
            click_preview(page)
            expect(page.locator('#preview-title')).to_have_text('当前候选仍有运行阻碍')
            expect(page.locator('#preview-candidates')).to_contain_text('托管开销')
            expect(page.locator('#submit-job')).to_be_enabled()
            page.screenshot(path=jobdir/'results/preview-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.locator('#job-preview').scroll_into_view_if_needed()
            page.screenshot(path=jobdir/'results/preview-mobile.png',full_page=True)
            page.locator('#submit-job').click()
            expect(page.locator('#job-form-message')).to_contain_text('已提交 job-')
            queued=jobs.all();assert len(queued)==1 and queued[0]['state']=='queued'
            assert not jobs.attempts(queued[0]['id'])
            user.request('/api/jobs/'+queued[0]['id']+'/cancel',{})
            checks.append('Blocked preview stays separate from explicit queue submission; desktop/mobile fit')

            # A stale request must also not repaint after the user actually submits.
            page.set_viewport_size({'width':1440,'height':1080})
            page.evaluate("""() => {window.fetch=(path,...rest)=>{const response=window.originalPreviewFetch(path,...rest);response.catch(()=>{});
              if(path==='/api/jobs/preview')return new Promise((resolve,reject)=>{window.releasePreview=()=>response.then(resolve,reject);});return response;};}""")
            page.locator('#job-name').fill('submit-during-preview')
            page.locator('#preview-job').click()
            expect(page.locator('#job-preview')).to_have_attribute('data-loading','true')
            page.locator('#submit-job').click()
            expect(page.locator('#job-form-message')).to_contain_text('已提交 job-')
            page.evaluate('window.releasePreview()');page.wait_for_timeout(150)
            expect(page.locator('#job-preview')).to_be_hidden()
            second=next(item for item in jobs.all() if item['name']=='submit-during-preview')
            assert second['state']=='queued' and not jobs.attempts(second['id'])
            user.request('/api/jobs/'+second['id']+'/cancel',{})
            context.close()
            checks.append('Explicit submission invalidates an in-flight preview without duplicate jobs')

            token=jobs.access.issue_token('alice',{'jobs:read'})
            readonly,other=open_page(browser,{'Authorization':'Bearer '+token})
            expect(other.locator('#preview-job')).to_be_disabled();expect(other.locator('#submit-job')).to_be_disabled()
            expect(other.locator('#job-access-note')).to_contain_text('查看权限')
            readonly.close();browser.close()
            checks.append('Read-only credentials cannot trigger preview or submission controls')
        assert not errors,errors
        assert all(item['state']=='cancelled' and not jobs.attempts(item['id']) for item in jobs.all())

        # The live personal demo still uses the unchanged backend; this read is real.
        with sync_playwright() as playwright:
            live_config=json.loads((ROOT/'runtime/config.json').read_text());live_client=Client(live_config)
            live_url=live_client.url;before=live_client.request('/api/jobs')['jobs']
            browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            live_context=browser.new_context(http_credentials={'username':live_config['username'],'password':live_config['password']})
            live_page=live_context.new_page();live_page.on('pageerror',lambda error:errors.append(str(error)))
            live_network=[]
            live_page.on('response',lambda response:live_network.append({'path':response.url.removeprefix(live_url),'status':response.status}) if '/api/jobs/preview' in response.url else None)
            live_page.goto(live_url+'/#jobs',wait_until='networkidle')
            live_page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
            live_started=time.monotonic()
            try:
                click_preview(live_page);expect(live_page.locator('#job-preview')).to_be_visible()
                expect(live_page.locator('#preview-title')).not_to_have_text('预览未完成')
            finally:
                atomic_json(jobdir/'results/live-preview-observation.json',{'network':live_network,'seconds':time.monotonic()-live_started,
                    'panel':live_page.locator('#job-preview').inner_text(),'ui':live_page.evaluate("({sequence:previewSequence,loading:document.getElementById('job-preview').dataset.loading,actor:identity?.id})")})
            assert {item['id'] for item in before}=={item['id'] for item in live_client.request('/api/jobs')['jobs']}
            live_context.close();browser.close()
        assert not errors,errors
        checks.append('Actual personal demo browser preview uses live backend without adding a task')
        result={'passed':True,'at':now(),'parent_job':os.environ['LAB_JOB_ID'],'checks':checks,'web_sha256':web_hashes,
            'release_id':release['release_id'],'backend_source_changed':False,'fixture_submitted_jobs':2,
            'fixture_terminal_cancelled_jobs':2,'fixture_execution_attempts':0,'live_added_jobs':0,
            'fixture_observations':'synthetic CPU Node protocol','browser_errors':errors,'npu_kernel_launches':0,'model_invocations':0}
        atomic_json(jobdir/'results/browser-preview.json',result)
        print(json.dumps({'passed':True,'checks':len(checks),'fixture_execution_attempts':0,'live_added_jobs':0}))
    finally:
        for item in jobs.all():
            if item['state']=='queued':jobs.cancel(item['id'],jobs.access.user(item['owner']))
        http.shutdown();http.server_close();thread.join();server_module.ROOT=original_root


if __name__=='__main__':main()

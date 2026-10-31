"""Packaged report UI with real HTTP/ACL and synthetic confirmed archive facts.

No worker, device or model is used. Actual installed reports are verified by
browser_installed_report_check.py through its separate Root installation handle.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import server as server_module
from deployment import bundle,unpack_release
from lab_client import Client,atomic_json
from simulation_data import analyze,sha
from test_report_view import ReportViewTests


def main():
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/report-browser';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    web={str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in (ROOT/'web').iterdir() if p.is_file()}
    assert all(sha((code/name).read_bytes())==value for name,value in web.items())
    original=server_module.ROOT;server_module.ROOT=code;fixture=ReportViewTests();fixture.setUp()
    client=fixture.client;url=client.url;jobs=fixture.base.jobs;identifiers={};checks=[];errors=[];outside=[];posts=[];gets=[]
    try:
        # Build distinct synthetic jobs, preserving real endpoint/ACL handling.
        from test_simulation_data import fixture as simulation_fixture
        config,blobs=simulation_fixture();second=copy.deepcopy(config['cores'][0]);second['id']='core0.veccore1'
        second['trace']['file']='core1.trace.json';second['instructions']['file']='core1.instructions.csv'
        trace=json.loads(blobs['core0.trace.json'])
        for event in trace['traceEvents']:
            for field in ['ts','dur']:
                if field in event:event[field]*=2
        blobs['core1.trace.json']=json.dumps(trace).encode();blobs['core1.instructions.csv']=blobs['core0.instructions.csv']
        second['trace']['sha256']=sha(blobs['core1.trace.json']);config['cores'].append(second)
        fixture.values['simulation'].update(input=config,analysis=analyze(config,lambda name,limit:blobs[name]))
        hostile='<img src=x onerror="window.reportInjected=true">'
        fixture.values['simulation']['analysis']['cores'][0]['analysis']['top_source_locations'][0]['name']=hostile
        for index,kind in enumerate(fixture.values):
            if index:
                job,_=jobs.submit({'template':'cpu_demo','parameters':{'seconds':1},'idempotency_key':'report-browser-'+kind},jobs.access.user('alice'))
                jobs.cancel(job['id'],jobs.access.user('alice'));fixture.identifier=job['id'];fixture.directory=jobs.directory(job['id'])
                (fixture.directory/'results').mkdir(exist_ok=True);atomic_json(fixture.directory/'artifacts.json',{'files':[]})
            if kind=='compiler':
                error=fixture.directory/'results/compiler-stderr.txt';error.write_text(hostile+'\nerror: unsupported operation\n')
                fixture.values[kind]['outcome']['stderr_bytes']=error.stat().st_size
            fixture.publish(kind);job=jobs.get(fixture.identifier)
            job['name']='Report '+kind
            if kind in {'simulation','operator'}:
                job['state']='succeeded';job['result']['status']='succeeded';job['_artifact_receipt']['state']='succeeded'
            with jobs.transaction() as db:jobs.write(db,job)
            identifiers[kind]=job['id']
        before={j['id']:(j['state'],j.get('attempt_id'),j.get('allocation')) for j in jobs.all()}
        def context(browser,owner='alice',token=None):
            args={'viewport':{'width':1440,'height':1080},'locale':'zh-CN'}
            if token:args['extra_http_headers']={'Authorization':'Bearer '+token}
            else:args['http_credentials']={'username':owner,'password':fixture.base.passwords[owner]}
            ctx=browser.new_context(**args)
            def route(item):
                if not item.request.url.startswith(url+'/'):outside.append('blocked');item.abort()
                else:item.continue_()
            ctx.route('**/*',route);page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:(posts if r.method=='POST' else gets).append(r.url.removeprefix(url)))
            page.goto(url+'/#jobs',wait_until='networkidle');page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
            page.locator('#auto-refresh').click();return ctx,page
        def open_job(page,kind):
            if page.locator('#drawer').is_visible():page.locator('#close-drawer').click()
            page.locator('[data-job-id="'+identifiers[kind]+'"]').click();expect(page.locator('#diagnostic-panel')).to_be_visible()
        def load(page,kind):
            page.locator('[data-report-kind="'+kind+'"]').click();expect(page.locator('#report-status')).to_have_attribute('data-loading','false',timeout=20000)
            expect(page.locator('#report-content')).not_to_be_empty()
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            ctx,page=context(browser);open_job(page,'accuracy');assert not any('diagnostic-report?' in x for x in gets)
            load(page,'accuracy');expect(page.locator('#report-content')).to_contain_text('与给定参考存在差异')
            expect(page.locator('#report-status')).to_contain_text('原任务状态：失败');expect(page.locator('#report-content')).to_contain_text('完整源文件未在本网页重算')
            checks.append('Explicit read of real authenticated archived report; original failure and un-recomputed source scope remain visible')
            open_job(page,'compiler');load(page,'compiler');expect(page.locator('#report-content')).to_contain_text('编译器返回错误 1')
            expect(page.locator('#report-content pre')).to_contain_text(hostile)
            assert page.locator('#report-content img').count()==0 and not page.evaluate('Boolean(window.reportInjected)')
            checks.append('Compiler failure and bounded error display are text only; report content cannot inject markup or requests')
            open_job(page,'simulation');load(page,'simulation');expect(page.locator('#report-content')).to_contain_text('原采集异常')
            expect(page.locator('#report-status')).to_contain_text('原任务状态：成功');expect(page.locator('#report-content')).to_contain_text('87.5%')
            expect(page.locator('#report-core')).to_have_value('0');page.locator('#report-core').select_option('1')
            expect(page.locator('#report-core-content')).to_contain_text('16.0000 µs');expect(page.locator('#report-core-content')).to_contain_text('14.0000 µs')
            page.locator('#report-core').select_option('0');assert page.locator('#report-content img').count()==0
            checks.append('Successful report job stays separate from signal11 abnormal capture; core switch changes finite interval coverage without new HTTP analysis')
            open_job(page,'operator');load(page,'operator');expect(page.locator('#report-content')).to_contain_text('优化收益未认证')
            expect(page.locator('#report-content')).to_contain_text('different_physical_device')
            checks.append('Observed performance ratio preserves blockers and does not become a verified speedup')
            # Hold an actual completed HTTP body; old response ignores cancellation.
            page.evaluate("""() => {window.reportOriginalFetch=window.fetch;window.fetch=(path,...rest)=>{
              const real=window.reportOriginalFetch(path,...rest);real.catch(()=>{});
              if(String(path).includes('diagnostic-report?'))return new Promise((resolve,reject)=>{real.then(async r=>{const body=await r.json();window.reportHeldReady=true;window.releaseReport=()=>resolve({ok:r.ok,status:r.status,json:async()=>body});},reject);});
              return real;};} """)
            open_job(page,'accuracy');page.locator('[data-report-kind="accuracy"]').click();page.wait_for_function('() => window.reportHeldReady===true')
            open_job(page,'compiler');page.evaluate('window.releaseReport()');page.wait_for_timeout(150)
            expect(page.locator('#report-content')).to_be_empty();expect(page.locator('#drawer-title')).to_have_text('Report compiler')
            page.evaluate('window.fetch=window.reportOriginalFetch')
            checks.append('Closing/changing drawer invalidates an already completed but delayed real response')
            # Report bytes are altered in the owned synthetic fixture only.
            directory=jobs.directory(identifiers['compiler']);path=directory/'results/compiler-case.json';saved=path.read_bytes();path.write_bytes(saved+b' ')
            page.locator('[data-report-kind="compiler"]').click();expect(page.locator('#report-status')).to_have_attribute('data-loading','false')
            expect(page.locator('#report-status')).to_contain_text('报告暂不可用');expect(page.locator('#report-content')).to_be_empty();path.write_bytes(saved)
            load(page,'compiler');checks.append('Real checksum409 is visible and retry reads the original report without a new job')
            page.evaluate("""() => {window.reportOriginalFetch=window.fetch;window.fetch=(path,...rest)=>{
              const real=window.reportOriginalFetch(path,...rest);real.catch(()=>{});
              if(String(path).includes('diagnostic-report?'))return new Promise((resolve,reject)=>{rest[0].signal.addEventListener('abort',()=>reject(new DOMException('timeout','AbortError')));});
              return real;};} """)
            page.locator('[data-report-kind="compiler"]').click();expect(page.locator('#report-status')).to_contain_text('读取超时',timeout=18000)
            page.evaluate('window.fetch=window.reportOriginalFetch');load(page,'compiler')
            checks.append('15-second observation timeout offers read retry and does not mutate or resubmit the task')
            page.set_viewport_size({'width':390,'height':844});open_job(page,'simulation');load(page,'simulation')
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(base/'mobile-report.png'),full_page=False);page.set_viewport_size({'width':1440,'height':1080})
            page.screenshot(path=str(base/'desktop-report.png'),full_page=False);ctx.close();checks.append('390px and desktop views render per-core report graphs without horizontal page overflow')
            token=jobs.access.issue_token('alice',{'jobs:read'});ctx,page=context(browser,token=token);open_job(page,'accuracy');load(page,'accuracy');ctx.close()
            checks.append('A jobs:read-only browser identity can view reports and sends no mutation')
            ctx,page=context(browser,owner='bob')
            for kind,identifier in identifiers.items():
                assert ctx.request.get(url+'/api/jobs/'+identifier+'/diagnostic-report?kind='+kind).status==404
            assert page.locator('[data-job-id]').count()==0;ctx.close();browser.close()
            checks.append('Foreign owner receives404 for all report types and cannot see private job rows')
        after={j['id']:(j['state'],j.get('attempt_id'),j.get('allocation')) for j in jobs.all()}
        assert before==after and not posts and not errors and not outside
        atomic_json(base/'validation.json',{'passed':True,'checks':checks,'web_sha256':web,'release_id':release['release_id'],
            'mutations':posts,'page_errors':errors,'outside_requests':outside,'job_states_and_attempts_unchanged':True,
            'scope':'Packaged browser plus actual HTTP/ACL on synthetic confirmed archives; no worker, actual NPU, Root execution or model.'})
        print(json.dumps({'passed':True,'browser_checks':len(checks),'mutations':0,'page_errors':0}))
    finally:fixture.tearDown();server_module.ROOT=original


if __name__=='__main__':main()

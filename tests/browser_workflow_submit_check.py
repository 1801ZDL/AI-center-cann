"""Real packaged dashboard + HTTP/ACL; queued workflows only, no worker/device/model.

The five task kinds exercise submission semantics. CPU execution is covered by
browser_installed_workflow_check.py against an actual isolated installed Node.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import server as server_module
from jobs import JobStore
from deployment import bundle,unpack_release
from lab_client import Client,atomic_json


def main():
    jobdir=Path(os.environ['LAB_JOB_DIR']);base=jobdir/'tmp/browser-workflow';base.mkdir(mode=0o700)
    archive=base/'server.tar.gz';release=bundle(archive);code,manifest=unpack_release(archive,base)
    web={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['web/app.js','web/index.html','web/style.css']}
    assert all(hashlib.sha256((code/name).read_bytes()).hexdigest()==sha for name,sha in web.items())
    runtime=base/'fixture';runtime.mkdir(mode=0o700)
    config={'username':'workflow-fixture-admin','password':'local-fixture-only','monitor_local':False,'container':'unused',
            'scheduler':{'local_enabled':False,'npu_enabled':True}}
    jobs=JobStore(runtime,config);passwords={}
    for owner in ['alice','bob']:
        home=runtime/owner;home.mkdir(mode=0o700)
        passwords[owner]=jobs.access.create_user(owner,home,environments=['worker-python'],quota={'npu':1})
    original=server_module.ROOT;server_module.ROOT=code
    http=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(latest=Mock(return_value=None)),config,jobs))
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{http.server_port}';user=Client({'controller':url,'username':'alice','password':passwords['alice']})
    peer=Client({'controller':url,'username':'bob','password':passwords['bob']})
    source=base/'source';source.mkdir();(source/'work.py').write_text('print("not executed in browser fixture")')
    frozen=user.upload_input(source);(source/'work.py').write_text('print("peer private input")');foreign=peer.upload_input(source)
    def request(kind):
        return {'template':kind+'_v1','environment':'worker-python','name':'browser-'+kind,
                'parameters':{'input_id':frozen['id'],'argv':['python','work.py'],
                              'resources':{'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0 if kind in {'ir','build'} else 1},
                              'lock':{'python':'3.10.12'},'timeout_seconds':60},
                'idempotency_key':'browser-'+kind+'-stable'}
    errors=[];outside=[];posts=[];checks=[];created=[]
    def open_page(browser,token=None):
        context=browser.new_context(viewport={'width':1440,'height':1080},locale='zh-CN',accept_downloads=True,
            **({'extra_http_headers':{'Authorization':'Bearer '+token}} if token else {'http_credentials':{'username':'alice','password':passwords['alice']}}))
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda req:posts.append({'path':req.url.removeprefix(url),'body':req.post_data_json}) if req.method=='POST' else None)
        def route(item):
            if not item.request.url.startswith(url+'/'):outside.append('blocked');item.abort()
            else:item.continue_()
        context.route('**/*',route)
        page.goto(url+'/#jobs',wait_until='networkidle');page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
        page.locator('#auto-refresh').click();page.locator('#job-template').select_option('workflow')
        expect(page.locator('#workflow-editor')).to_be_visible()
        return context,page
    def draft(page,value):page.locator('#workflow-request').fill(json.dumps(value,ensure_ascii=False,indent=2))
    def preview(page):
        page.locator('#preview-job').click();expect(page.locator('#job-preview')).to_have_attribute('data-loading','false')
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            context,page=open_page(browser)
            for value in ['{',json.dumps([]),json.dumps({**request('ir'),'idempotency_key':''})]:
                page.locator('#workflow-request').fill(value);before=len(posts);page.locator('#preview-job').click()
                expect(page.locator('#job-form-message')).to_be_visible();assert len(posts)==before and not jobs.all()
            assert not errors;checks.append('Invalid JSON/type/missing key rejected locally before POST, without uncaught page errors')
            for kind in ['accuracy','performance','simulation','build','ir']:
                value=request(kind);value['placement']={'node_id':'worker-'+'f'*12,**({'device_ids':[1]} if kind in {'accuracy','performance','simulation'} else {})};draft(page,value);before=len(jobs.all());preview(page)
                expect(page.locator('#workflow-draft-summary')).to_contain_text(value['placement']['node_id'])
                expect(page.locator('#preview-title')).to_have_text('当前没有映射给本人的节点')
                assert len(jobs.all())==before
                assert posts[-1]=={'path':'/api/jobs/preview','body':value}
                page.locator('#submit-job').click();expect(page.locator('#job-form-message')).to_contain_text('已提交 ')
                job=next(j for j in jobs.all() if j['idempotency_key']==value['idempotency_key']);created.append(job['id'])
                assert job['state']=='queued' and not jobs.attempts(job['id']) and job['template']==value['template']
                submitted=next(item['body'] for item in reversed(posts) if item['path']=='/api/jobs');assert submitted==value
                assert job['parameters']['input_id']==value['parameters']['input_id']
                assert job['parameters']['lock']==value['parameters']['lock'] and job['resources']==value['parameters']['resources']
                jobs.cancel(job['id'],jobs.access.user('alice'))
            checks.append('All five workflows use the same exact preview/submit JSON; no ordinary-field overrides; five cancelled jobs have zero attempts')
            value=request('ir');value.update(name='<img src=x onerror="window.workflowInjected=true">',idempotency_key='browser-import-stable')
            page.locator('#workflow-file').set_input_files({'name':'request.json','mimeType':'application/json','buffer':json.dumps(value).encode()})
            expect(page.locator('#workflow-draft-summary')).to_contain_text(value['name']);assert page.locator('#workflow-draft-summary img').count()==0
            assert not page.evaluate('Boolean(window.workflowInjected)')
            with page.expect_download() as pending:page.locator('#workflow-download').click()
            downloaded=base/'downloaded-request.json';pending.value.save_as(downloaded);assert json.loads(downloaded.read_text())==value
            preview(page);expect(page.locator('#job-preview')).to_be_visible();page.locator('#workflow-request').fill(json.dumps({**value,'name':'edited'}));expect(page.locator('#job-preview')).to_be_hidden()
            checks.append('Local file import/download roundtrip retains the key and all fields; hostile labels rendered as text; JSON edit invalidates preview')
            draft(page,value)
            def lose_response(route):
                if route.request.method=='POST':route.fetch();route.abort('failed')
                else:route.continue_()
            page.route('**/api/jobs',lose_response);page.locator('#submit-job').click()
            expect(page.locator('#job-form-message')).to_contain_text('Failed to fetch')
            page.unroute('**/api/jobs',lose_response)
            original_job=next(j for j in jobs.all() if j['idempotency_key']==value['idempotency_key']);created.append(original_job['id']);count=len(jobs.all())
            page.locator('#submit-job').click();expect(page.locator('#job-form-message')).to_contain_text('已找到原任务 '+original_job['id']);assert len(jobs.all())==count
            page.reload(wait_until='networkidle');page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
            page.locator('#job-template').select_option('workflow');page.locator('#workflow-file').set_input_files(downloaded)
            expect(page.locator('#workflow-draft-summary')).to_contain_text(value['idempotency_key'])
            page.locator('#submit-job').click();expect(page.locator('#job-form-message')).to_contain_text('已找到原任务 '+original_job['id']);assert len(jobs.all())==count
            draft(page,{**value,'name':'changed same key'});page.locator('#submit-job').click()
            expect(page.locator('#job-form-message')).to_contain_text('幂等');assert len(jobs.all())==count
            jobs.cancel(original_job['id'],jobs.access.user('alice'))
            checks.append('Response loss, retry and page reload/import all reuse one job/key; conflicting same-key request is refused')
            value=request('ir');value['parameters']['input_id']=foreign['id'];draft(page,value);before=len(jobs.all());preview(page)
            expect(page.locator('#preview-title')).to_have_text('预览未完成');assert len(jobs.all())==before
            checks.append('Foreign sealed input rejected by real authenticated backend; no job added')
            draft(page,request('ir'));snapshot=page.locator('#workflow-request').input_value()
            page.locator('#workflow-file').set_input_files({'name':'large.json','mimeType':'application/json','buffer':b' '*262145})
            expect(page.locator('#job-form-message')).to_contain_text('256 KiB');assert page.locator('#workflow-request').input_value()==snapshot
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(jobdir/'results/workflow-submit-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1440,'height':1080});page.screenshot(path=str(jobdir/'results/workflow-submit-desktop.png'),full_page=True)
            checks.append('Oversized import preserves draft; desktop and 390px mobile fit')
            token=jobs.access.issue_token('alice',{'jobs:read'});reader,readonly=open_page(browser,token)
            expect(readonly.locator('#preview-job')).to_be_disabled();expect(readonly.locator('#submit-job')).to_be_disabled()
            denied=reader.request.post(url+'/api/jobs',data=json.dumps(request('build')),headers={'Content-Type':'application/json'});assert denied.status==403
            reader.close();context.close();browser.close();checks.append('Read-only identity has disabled controls and actual POST403 backend enforcement')
        assert not errors and not outside and all(j['state']=='cancelled' and not jobs.attempts(j['id']) for j in jobs.all())
        assert len(jobs.all())==6
        result={'passed':True,'checks':checks,'server_release':release['release_id'],'web_sha256':web,'task_ids':created,
                'created_jobs':6,'attempts':0,'npu_kernel_launches':0,'model_invocations':0,'page_errors':errors,'outside_requests':outside,
                'scope':'Packaged assets with actual isolated HTTP/ACL/input validation and queue; no worker or device execution'}
        atomic_json(jobdir/'results/browser-workflow-submit.json',result);print(json.dumps(result,ensure_ascii=False))
    finally:
        for job in jobs.all():
            if job['state']=='queued':jobs.cancel(job['id'],jobs.access.local)
        http.shutdown();http.server_close();thread.join(timeout=5);jobs.access.close();server_module.ROOT=original


if __name__=='__main__':main()

"""Packaged UI submits real CPU IR/build once to the owned installed Root Node."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--requests',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    client=Client.from_profile(a.profile);profile=json.loads(Path(a.profile).read_text());requests=json.loads(Path(a.requests).read_text());assert len(requests)==2
    assert {r['template'] for r in requests}=={'ir_v1','build_v1'}
    assert all(r['parameters']['resources']['npu']==0 and r['parameters']['timeout_seconds']<=90 for r in requests)
    errors=[];outside=[];posts=[];accepted=[];preview_responses=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1440,'height':1080},locale='zh-CN',accept_downloads=True,http_credentials={'username':profile['username'],'password':profile['password']})
        def route(item):
            if not item.request.url.startswith(client.url+'/'):outside.append('blocked');item.abort()
            else:item.continue_()
        context.route('**/*',route)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda req:posts.append({'path':req.url.removeprefix(client.url),'body':req.post_data_json}) if req.method=='POST' else None)
        def preview_response(response):
            if response.url==client.url+'/api/jobs/preview':preview_responses.append({'status':response.status,'body':response.json()})
        page.on('response',preview_response)
        page.goto(client.url+'/#jobs',wait_until='networkidle');page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
        page.locator('#auto-refresh').click();page.locator('#job-template').select_option('workflow')
        for request in requests:
            before={j['id'] for j in client.request('/api/jobs')['jobs']}
            page.locator('#workflow-file').set_input_files({'name':'request.json','mimeType':'application/json','buffer':json.dumps(request).encode()})
            expect(page.locator('#workflow-draft-summary')).to_contain_text(request['idempotency_key'])
            page.locator('#preview-job').click();expect(page.locator('#job-preview')).to_have_attribute('data-loading','false',timeout=20000)
            assert page.locator('#preview-title').inner_text()!='预览未完成',{'preview_summary':page.locator('#preview-summary').inner_text(),'responses':preview_responses,'actor':client.request('/api/me')['id']}
            assert {j['id'] for j in client.request('/api/jobs')['jobs']}==before
            assert posts[-1]=={'path':'/api/jobs/preview','body':request}
            page.locator('#submit-job').click();expect(page.locator('#job-form-message')).to_contain_text('已提交 ')
            assert posts[-1]=={'path':'/api/jobs','body':request}
            job=next(j for j in client.request('/api/jobs')['jobs'] if j['idempotency_key']==request['idempotency_key'])
            assert {j['id'] for j in client.request('/api/jobs')['jobs']}-before=={job['id']}
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                detail=client.request('/api/jobs/'+job['id']);job=detail['job']
                if job['state'] in {'succeeded','failed','cancelled','timed_out','needs_attention'}:break
                time.sleep(.3)
            assert job['state']=='succeeded' and len(detail['attempts'])==1,{'job_id':job['id'],'state':job['state']}
            artifact='ir/canonical.mlir' if request['template']=='ir_v1' else 'results/libascend_lab_smoke.so'
            assert any(item['name']==artifact and item['bytes']>0 for item in detail['artifacts'])
            log=client.request('/api/jobs/'+job['id']+'/log?stream=stdout')['text'];assert '42' in log
            # Repeating the exact explicit submission must reuse the finished job.
            page.locator('#submit-job').click();expect(page.locator('#job-form-message')).to_contain_text('已找到原任务 '+job['id'])
            assert len(client.request('/api/jobs/'+job['id'])['attempts'])==1
            accepted.append({'job_id':job['id'],'attempt_id':job['attempt_id'],'template':job['template'],'state':job['state'],'attempts':1,'artifact':artifact})
        page.goto(client.url+'/#results',wait_until='networkidle')
        for item in accepted:expect(page.locator('[data-result-job="'+item['job_id']+'"]')).to_be_visible()
        item=next(x for x in accepted if x['template']=='ir_v1')
        page.locator('[data-result-job="'+item['job_id']+'"]').click()
        expect(page.locator('#drawer-content')).to_contain_text('Actual bishengir-opt folded 20 + 22 to constant 42.')
        with page.expect_download() as pending:page.locator('#drawer-content a').filter(has_text='ir/canonical.mlir').click()
        target=Path(a.output).with_suffix('.mlir');pending.value.save_as(target);assert 'arith.constant 42' in target.read_text()
        page.locator('#close-drawer').click()
        with page.expect_download() as pending:page.locator('#workflow-csv').click()
        target=Path(a.output).with_suffix('.csv');pending.value.save_as(target)
        rows=list(csv.DictReader(io.StringIO(target.read_text(encoding='utf-8-sig'))));assert {x['job_id'] for x in accepted}<={r['job_id'] for r in rows}
        page.screenshot(path=str(Path(a.output).with_suffix('.png')),full_page=True)
        context.close();browser.close()
    assert not errors and not outside
    atomic_json(a.output,{'passed':True,'tasks':accepted,'same_json_preview_and_submit':True,'repeated_submit_reuses_original':True,'results_and_csv_contain_both':True,'downloaded_actual_ir_verified':True,'page_errors':errors,'outside_requests':outside,'npu_kernel_launches':0,'model_invocations':0,'scope':'Two CPU workflows from actual installed web assets, Root Node, non-root task UID and protected environment; no device workflow acceptance'})
    print('Installed dashboard submitted and replayed two actual CPU workflows; results/CSV/IR download verified')


if __name__=='__main__':main()

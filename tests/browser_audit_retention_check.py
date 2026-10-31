"""Read-only browser check against the owned installed controller under pressure."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    client=Client.from_profile(a.profile);before=client.request('/api/jobs');assert before['audit_retention']['blocks_new_work']
    errors=[];outside=[];posts=[];origin=client.url
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1440,'height':1080},locale='zh-CN',extra_http_headers={'Authorization':client.authorization})
        def route(request):
            if urlsplit(request.request.url).netloc!=urlsplit(origin).netloc:
                outside.append('blocked');request.abort()
            else:request.continue_()
        context.route('**/*',route)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda request:posts.append(urlsplit(request.url).path) if request.method=='POST' else None)
        page.goto(origin+'/#jobs',wait_until='networkidle');page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
        expect(page.locator('#audit-retention-status')).to_contain_text('暂停新工作')
        expect(page.locator('#audit-retention-status')).to_contain_text('归档门槛')
        page.locator('#preview-job').click()
        expect(page.locator('#job-preview')).to_have_attribute('data-loading','false',timeout=20000)
        expect(page.locator('#preview-candidates')).to_contain_text('审计记录等待归档')
        page.screenshot(path=str(Path(a.output).with_suffix('.png')),full_page=True)
        context.close();browser.close()
    after=client.request('/api/jobs')
    assert not errors and not outside and posts==['/api/jobs/preview']
    assert {j['id'] for j in before['jobs']}=={j['id'] for j in after['jobs']}
    atomic_json(a.output,{'passed':True,'warning_visible':True,'preview_blocker_visible':True,'created_jobs':0,'posts':posts,'page_errors':errors,'outside_requests':outside})
    print('Installed audit-backlog browser warning/preview passed; zero submissions')


if __name__=='__main__':main()

"""Actual installed report UI; existing report jobs only, no submission."""
import argparse
import json
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--cases',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    profile=json.loads(Path(args.profile).read_text());client=Client(profile);cases=json.loads(Path(args.cases).read_text());assert {v['kind'] for v in cases}=={'accuracy','compiler','simulation','operator'}
    before={j['id']:(j['state'],j.get('attempt_id')) for j in client.request('/api/jobs')['jobs']};errors=[];outside=[];posts=[];checks=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1440,'height':1080},locale='zh-CN',http_credentials={'username':profile['username'],'password':profile['password']})
        def route(item):
            if not item.request.url.startswith(client.url+'/'):outside.append('blocked');item.abort()
            else:item.continue_()
        context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:posts.append(r.url.removeprefix(client.url)) if r.method=='POST' else None)
        page.goto(client.url+'/#jobs',wait_until='networkidle');page.wait_for_function("() => document.getElementById('job-form').dataset.initialized==='true'");page.locator('#auto-refresh').click()
        for case in cases:
            kind=case['kind'];identifier=case['job_id']
            page.locator('[data-job-id="'+identifier+'"]').click();expect(page.locator('#diagnostic-panel')).to_be_visible()
            with page.expect_response(lambda r:r.url==client.url+'/api/jobs/'+identifier+'/diagnostic-report?kind='+kind) as pending:
                page.locator('[data-report-kind="'+kind+'"]').click()
            response=pending.value;assert response.status==200;value=response.json()
            assert value['report']['sha256']==case['report_sha256'] and value['summary']==case['summary']
            assert value['job']['id']==identifier and value['job']['state']==case['state'] and value['job']['attempt_id']==case['attempt_id']
            assert value['verification']['report_bytes_verified'] and not value['verification']['source_bytes_recomputed']
            expect(page.locator('#report-status')).to_have_attribute('data-loading','false');expect(page.locator('#report-content')).not_to_be_empty()
            if kind=='accuracy':
                assert value['summary']['mismatches']==11;expect(page.locator('#report-content')).to_contain_text('与给定参考存在差异');expect(page.locator('#report-status')).to_contain_text('原任务状态：失败')
            elif kind=='compiler':
                assert value['summary']['exit_code']==1 and value['summary']['source']['bytes']==1935540
                expect(page.locator('#report-content')).to_contain_text('编译器返回错误 1');expect(page.locator('#report-content pre')).to_contain_text("expected '='")
            elif kind=='simulation':
                assert len(value['summary']['cores'])==4 and value['summary']['capture_state']=='abnormal'
                expect(page.locator('#report-content')).to_contain_text('原采集异常');expect(page.locator('#report-status')).to_contain_text('原任务状态：成功')
                page.locator('#report-core').select_option('1');expect(page.locator('#report-core-content')).to_contain_text(format(value['summary']['cores'][1]['span_us'],'.4f')+' µs')
                page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                page.screenshot(path=str(Path(args.output).with_suffix('.mobile.png')),full_page=False);page.set_viewport_size({'width':1440,'height':1080})
                page.screenshot(path=str(Path(args.output).with_suffix('.desktop.png')),full_page=False)
            else:
                assert value['summary']['optimization_speedup'] is None;expect(page.locator('#report-content')).to_contain_text('优化收益未认证')
            checks.append({'kind':kind,'job_id':identifier,'state':case['state'],'report_sha256':case['report_sha256']});page.locator('#close-drawer').click()
        context.close();browser.close()
    after={j['id']:(j['state'],j.get('attempt_id')) for j in client.request('/api/jobs')['jobs']}
    assert before==after and not posts and not errors and not outside
    atomic_json(args.output,{'passed':True,'cases':checks,'page_errors':errors,'outside_requests':outside,'posts':posts,'no_new_jobs_or_attempts':True,
        'scope':'Actual packaged installed UI presents existing managed CPU report jobs. Reports hash-checked against Node confirmation; GUI does not recompute original source bytes or certify historical collection.'})
    print(json.dumps({'passed':True,'actual_report_kinds':4,'new_tasks':0,'page_errors':0}))


if __name__=='__main__':main()

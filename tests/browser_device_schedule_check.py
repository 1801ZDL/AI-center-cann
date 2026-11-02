"""Packaged read-only daily-plan UI with real HTTP/ACL, synthetic NPU capture."""
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import server as server_module
from deployment import bundle,unpack_release
from jobs import atomic_json
from test_device_schedule import DailyTests


def main():
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/daily-browser';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    fixture=DailyTests('runTest');fixture.setUp();jobs=fixture.jobs;errors=[];posts=[];outside=[]
    original=server_module.ROOT;server_module.ROOT=code;server=None;thread=None
    try:
        tokens={owner:jobs.access.issue_token(owner,{'jobs:read'}) for owner in ['alice','bob']}
        job,run=fixture.complete_microcheck()
        server=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(),{'username':'operator','password':'offline-only'},jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(server.server_port)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            def open_page(owner):
                context=browser.new_context(viewport={'width':1440,'height':1080},extra_http_headers={'Authorization':'Bearer '+tokens[owner]})
                def route(item):
                    if item.request.url.startswith(url+'/'):item.continue_()
                    else:outside.append('blocked');item.abort()
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda r:posts.append(r.method) if r.method!='GET' else None)
                page.goto(url+'/#results',wait_until='networkidle');page.locator('#auto-refresh').click()
                return context,page
            owner,page=open_page('alice');table=page.locator('#device-schedule-body')
            expect(table).to_contain_text('daily-test');expect(table).to_contain_text('有限微用例一致（非全卡健康认证）')
            expect(table).to_contain_text('UTC');expect(table).to_contain_text('微用例')
            table.locator('[data-schedule-job="'+job['id']+'"]').click()
            expect(page.locator('#drawer-content')).to_contain_text(job['id'])
            page.locator('#close-drawer').click()
            fixture.jobs.device_schedules.control(fixture.identifier,'pause');page.locator('#refresh').click()
            expect(table).to_contain_text('已暂停');expect(table).to_contain_text('历史记录')
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=base/'daily-mobile.png',full_page=True)
            peer,other=open_page('bob');expect(other.locator('#device-schedule-body')).to_contain_text('此账号可见范围内没有每日自检计划')
            assert peer.request.get(url+'/api/device-schedules').json()['total']==0
            assert peer.request.get(url+'/api/jobs/'+job['id']).status in {403,404}
            jobs.access.add_member('device-health','bob','viewer');other.reload(wait_until='networkidle')
            expect(other.locator('#device-schedule-body')).to_contain_text('daily-test')
            peer.close();owner.close();browser.close()
        assert not errors and not posts and not outside,(errors,posts,outside)
        report={'passed':True,'scope':'Packaged web/actual HTTP/ACL with synthetic authenticated Node capture, not a real NPU self-check',
            'checks':['daily state separated from full hardware certification','original task opens under same ACL','pause makes past evidence historical',
                      'owner/project reader authorization','private nonmember denied plan and job','mobile width','no browser writes or external requests'],
            'release_id':release['release_id'],'job_id':job['id'],'new_npu_launches':0,'model_calls':0,'browser_errors':errors}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/device-schedule-browser.json',report);print(json.dumps(report,ensure_ascii=False))
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(3)
        fixture.doCleanups();server_module.ROOT=original


if __name__=='__main__':main()

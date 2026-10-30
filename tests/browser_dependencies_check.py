"""Replay actual archived partial handoff and unstarted outcome; no new tasks."""
import json
import argparse
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--version',choices=['032','033','034','035','036'],default='032');version=parser.parse_args().version
config=json.loads((ROOT/'runtime/config.json').read_text())
profile=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
fixture=json.loads((ROOT/'evidence'/('dependency-ui-fixture-v'+version+'.json')).read_text());errors=[]
assert any(j.get('dependency_state',{}).get('status')=='copying' for j in fixture['jobs'])
assert fixture['rows'][0]['command_executed'] is False and fixture['rows'][0]['failure_stage']=='dependency'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':profile['username'],'password':profile['password']})
    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
    def jobs(route):
        response=route.fetch();data=response.json();data['jobs']=fixture['jobs']+data['jobs'];route.fulfill(response=response,json=data)
    def results(route):
        response=route.fetch();data=response.json();data['rows']=fixture['rows']+data['rows'];data['total']+=len(fixture['rows']);route.fulfill(response=response,json=data)
    page.route('**/api/jobs',jobs);page.route('**/api/results?*',results)
    page.goto('http://127.0.0.1:'+str(config['port'])+'/#jobs',wait_until='networkidle')
    page.evaluate('''()=>{const p=document.createElement('p');p.textContent='历史阶段交接验收；非当前Main任务，未提交新任务。';document.getElementById('job-body').closest('.panel').before(p);}''')
    expect(page.locator('#job-body')).to_contain_text('产物交接中')
    expect(page.locator('#job-body')).to_contain_text('上游失败，未启动')
    expect(page.locator('#workflow-result-body')).to_contain_text('上游失败，未启动')
    page.screenshot(path=ROOT/'evidence'/('dependency-desktop-v'+version+'.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    expect(page.locator('#job-body')).to_contain_text('产物交接中')
    page.screenshot(path=ROOT/'evidence'/('dependency-mobile-v'+version+'.png'),full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'source_parent':fixture['source_parent'],'new_tasks_submitted':0,'browser_errors':errors,
    'scope':'Desktop/390px replay of actual installed partial handoff and controller-recorded upstream-failure non-execution; no live Main tasks or records inserted'}
(ROOT/'evidence'/('dependency-browser-v'+version+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))

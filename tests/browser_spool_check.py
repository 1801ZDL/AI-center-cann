"""Render archived real pending-output metadata without changing Main state."""
import copy
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
personal=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
fixture=json.loads((ROOT/'evidence/node-spool-ui-fixture-v030.json').read_text());errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':personal['username'],'password':personal['password']})
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def nodes(route):
        response=route.fetch();v=response.json();node=copy.deepcopy(fixture['node']);node.update(name='已归档有限暂存验收快照',status='unknown')
        v['nodes'].append(node);route.fulfill(response=response,json=v)
    def jobs(route):
        response=route.fetch();v=response.json();v['jobs'].insert(0,fixture['job']);route.fulfill(response=response,json=v)
    page.route('**/api/nodes',nodes);page.route('**/api/jobs',jobs)
    page.goto('http://127.0.0.1:'+str(config['port'])+'/#jobs',wait_until='networkidle')
    page.evaluate('''()=>{const p=document.createElement('p');p.textContent='历史有限暂存验收数据；未登记当前在线节点，未提交新任务。';document.getElementById('node-list').before(p);}''')
    card=page.locator('.device-card').filter(has_text='已归档有限暂存验收快照')
    expect(card).to_contain_text('有限暂存盘')
    expect(card).to_contain_text('可用 0.0 MiB')
    expect(page.locator('#job-body')).to_contain_text('已执行，待归档')
    page.screenshot(path=ROOT/'evidence/node-spool-desktop-v030.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    expect(card).to_contain_text('有限暂存盘');page.screenshot(path=ROOT/'evidence/node-spool-mobile-v030.png',full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'source_parent':fixture['source_parent'],'browser_errors':errors,'new_tasks_submitted':0,
    'scope':'Desktop/390px replay of actual archived pending-output job and finite-spool node; no live Main spool enabled or enrolled'}
(ROOT/'evidence/node-spool-browser-v030.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))

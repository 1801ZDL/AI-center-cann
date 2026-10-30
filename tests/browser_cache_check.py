"""Render actual archived installed-cache pressure without registering a live Node."""
import copy
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
profile=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
fixture=json.loads((ROOT/'evidence/cache-ui-fixture-v031.json').read_text());errors=[]
assert fixture['node']['persistent_cache']['enforced']
assert any(s['status']=='ok' and s['free_inodes']==0 for s in fixture['node']['persistent_cache']['slots'])
assert fixture['job']['state']=='queued' and fixture['job']['parameters']['cache']['mode']=='persistent'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':profile['username'],'password':profile['password']})
    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
    def nodes(route):
        response=route.fetch();data=response.json();node=copy.deepcopy(fixture['node'])
        node.update(name='已归档私有缓存验收快照',status='unknown');data['nodes'].append(node);route.fulfill(response=response,json=data)
    def jobs(route):
        response=route.fetch();data=response.json();data['jobs'].insert(0,fixture['job']);route.fulfill(response=response,json=data)
    page.route('**/api/nodes',nodes);page.route('**/api/jobs',jobs)
    page.goto('http://127.0.0.1:'+str(config['port'])+'/#jobs',wait_until='networkidle')
    page.evaluate('''()=>{const p=document.createElement('p');p.textContent='历史缓存inode满额验收数据；未登记当前在线节点，未提交新任务。';document.getElementById('node-list').before(p);}''')
    card=page.locator('.device-card').filter(has_text='已归档私有缓存验收快照')
    expect(card).to_contain_text('私有持久缓存');expect(card).to_contain_text('剩余 0 inode')
    expect(page.locator('#job-body')).to_contain_text(fixture['job']['id'])
    expect(page.locator('#node-list')).to_contain_text('持久缓存未启用，任务使用独立工作区缓存。')
    page.screenshot(path=ROOT/'evidence/cache-desktop-v031.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    expect(card).to_contain_text('剩余 0 inode');page.screenshot(path=ROOT/'evidence/cache-mobile-v031.png',full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'source_parent':fixture['source_parent'],'new_tasks_submitted':0,'browser_errors':errors,
    'scope':'Desktop/390px archived actual installed cache inode-full snapshot; current Main nodes remain explicitly without persistent caches'}
(ROOT/'evidence/cache-browser-v031.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False))

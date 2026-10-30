"""Render an explicitly archived node fixture; never claim it is a live enrollment."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
personal=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
fixture=json.loads((ROOT/'evidence/device-qualification-ui-fixture-v027.json').read_text())
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':personal['username'],'password':personal['password']})
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def nodes(route):
        response=route.fetch();value=response.json();value['nodes'].append(fixture['node'])
        route.fulfill(response=response,json=value)
    page.route('**/api/nodes',nodes)
    page.goto('http://127.0.0.1:'+str(config['port'])+'/#jobs',wait_until='networkidle')
    card=page.locator('.device-card').filter(has_text='设备访问验收快照')
    expect(card).to_contain_text('设备文件访问资格')
    expect(card).to_contain_text('失败阻断 1')
    expect(card).to_contain_text('ACL与算子另行验收')
    expect(card).not_to_contain_text('qual_alice')
    page.screenshot(path=ROOT/'evidence/device-qualification-desktop-v027.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    expect(card).to_contain_text('失败阻断 1')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=ROOT/'evidence/device-qualification-mobile-v027.png',full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'scope':'Desktop/390px rendering only; an explicitly archived HTTP-run node fixture was injected into the nodes response, no live Main NPU node or new task is claimed',
        'fixture_parent':fixture['source_parent'],'browser_errors':errors,'new_jobs_submitted':0,'npu_used':False}
(ROOT/'evidence/device-qualification-browser-v027.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

"""Render actual archived central quota data; no enrollment or execution."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
personal=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
fixture=json.loads((ROOT/'evidence/owner-quota-ui-fixture-v029.json').read_text())
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':personal['username'],'password':personal['password']})
    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
    def quota(route):
        response=route.fetch();value=response.json();value['storage']=fixture['storage'];route.fulfill(response=response,json=value)
    page.route('**/api/jobs',quota)
    page.goto('http://127.0.0.1:'+str(config['port'])+'/#jobs',wait_until='networkidle')
    page.evaluate('''()=>{const label=document.createElement('p');label.textContent='已归档的 proof_user 存储验收数据；当前个人 Demo 尚未启用硬配额。';document.getElementById('job-budget').before(label);}''')
    expect(page.locator('#job-budget')).to_contain_text('本人中央存储硬限额')
    expect(page.locator('#job-budget')).to_contain_text('192.0 MiB')
    expect(page.locator('#job-budget')).to_contain_text('本人存储 inode')
    expect(page.locator('#job-budget')).to_contain_text('1,024')
    page.screenshot(path=ROOT/'evidence/owner-quota-desktop-v029.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    expect(page.locator('#job-budget')).to_contain_text('本人中央存储硬限额')
    page.screenshot(path=ROOT/'evidence/owner-quota-mobile-v029.png',full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'fixture_parent':fixture['source_parent'],'browser_errors':errors,'new_jobs_submitted':0,
    'scope':'Desktop/390px rendering of actual archived proof_user storage quota injected into jobs response; live Main quota not enabled'}
(ROOT/'evidence/owner-quota-browser-v029.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

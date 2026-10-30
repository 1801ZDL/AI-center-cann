"""Multi-user UI verification; creates one small CPU template as demo_bob."""
import json
import os
from pathlib import Path
import time
import uuid
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
client=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
url=f'http://127.0.0.1:{config["port"]}'
name='逻辑用户网页验收-'+uuid.uuid4().hex[:6]
errors=[];denied=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':client['username'],'password':client['password']})
    page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
    page.on('response',lambda r:denied.append(r.url) if r.status in [401,403] else None)
    page.goto(url+'/#jobs',wait_until='networkidle')
    page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
    expect(page.locator('#sidebar-user')).to_have_text('demo_bob')
    expect(page.locator('#job-template')).to_have_value('cpu_demo')
    expect(page.locator('#job-command')).to_be_hidden()
    expect(page.locator('[data-view="processes"]')).to_be_hidden()
    expect(page.locator('#job-body')).not_to_contain_text('PRIVATE_ALICE_')
    expect(page.locator('#job-body')).to_contain_text('团队共享 CPU 验收')
    page.locator('#job-name').fill(name)
    page.locator('#job-seconds').fill('2')
    page.locator('#submit-job').click()
    expect(page.locator('#job-form-message')).to_contain_text('已提交 job-')
    def lookup():
        return next(j for j in context.request.get(url+'/api/jobs').json()['jobs'] if j['name']==name)
    job=lookup();deadline=time.monotonic()+30
    while time.monotonic()<deadline and lookup()['state'] not in {'succeeded','failed','needs_attention'}:
        time.sleep(.3)
    assert lookup()['state']=='succeeded'
    page.locator('#refresh').click()
    page.locator(f'[data-job-id="{job["id"]}"]').click()
    expect(page.locator('#drawer-content')).to_contain_text('demo_bob')
    expect(page.locator('#drawer-content')).to_contain_text('Completed; results/demo-result.json is ready.')
    page.locator('#drawer').evaluate('(element)=>element.scrollTop=0')
    page.screenshot(path=ROOT/'evidence/identity-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=ROOT/'evidence/identity-mobile.png',full_page=True)
    assert not errors,errors
    assert not denied,denied
    context.close();browser.close()
report={'passed':True,'job_id':job['id'],'identity':'demo_bob','checks':['Private task hidden','Shared project visible','Monitor views hidden without forbidden fetches','Only fixed template available','Real template submission and result log','Desktop/mobile layout'],
        'browser_errors':errors,'unexpected_401_403':denied,'isolation':'same UID logical simulation','npu_kernel_launches':0}
(ROOT/'evidence/identity-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

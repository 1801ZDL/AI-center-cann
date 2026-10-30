"""Render actual archived managed compiler jobs without changing live Main data."""
import json
import argparse
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--version',choices=['034','035','036'],default='034');version=parser.parse_args().version
config=json.loads((ROOT/'runtime/config.json').read_text())
profile=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
fixture=json.loads((ROOT/'evidence'/('native-toolchain-ui-fixture-v'+version+'.json')).read_text())
assert len(fixture['jobs'])==len(fixture['rows'])==5
assert all(j['state']=='succeeded' and j['resources']['npu']==0 for j in fixture['jobs'])
assert all(r['passed'] and r['kind']=='build' and r['environment_locked'] for r in fixture['rows'])
errors=[]
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
    page.evaluate('''()=>{const p=document.createElement('p');p.id='native-proof-banner';p.textContent='历史原生编译验收：实际托管任务归档；不是当前 Main 任务，未提交新任务。';document.getElementById('job-body').closest('.panel').before(p);}''')
    for job in fixture['jobs']:expect(page.locator('#job-body')).to_contain_text(job['id'])
    for row in fixture['rows']:expect(page.locator('#workflow-result-body')).to_contain_text(row['job_id'])
    assert page.locator('#workflow-result-body tr').filter(has_text='Native offline compiler cache').count()==5
    expect(page.locator('#workflow-result-body')).to_contain_text('已匹配')
    expect(page.locator('#workflow-result-body')).to_contain_text('编译构建')
    expect(page.locator('#native-proof-banner')).to_be_visible()
    page.screenshot(path=ROOT/'evidence'/('native-toolchain-desktop-v'+version+'.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    expect(page.locator('#native-proof-banner')).to_be_visible()
    page.screenshot(path=ROOT/'evidence'/('native-toolchain-mobile-v'+version+'.png'),full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'source_parent':fixture['source_parent'],'archived_jobs':5,'new_tasks_submitted':0,'live_records_inserted':0,
    'browser_errors':errors,'viewports':[1440,390],
    'scope':'Browser-only replay of actual installed CPU native build jobs and locked result rows; explicitly historical, not live Main data'}
(ROOT/'evidence'/('native-toolchain-browser-v'+version+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False))

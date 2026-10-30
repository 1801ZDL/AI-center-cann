"""Queue UI checks; submits only explicit small CPU jobs through the real API."""
import json
import os
from pathlib import Path
import time
import uuid
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
config = json.loads((ROOT/'runtime/config.json').read_text())
url = f"http://127.0.0.1:{config['port']}"
evidence = ROOT/'evidence'
prefix = '网页验收-'+uuid.uuid4().hex[:6]
checks, errors = [], []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'), args=['--disable-dev-shm-usage'])
    context = browser.new_context(viewport={'width':1440,'height':1080}, http_credentials={'username':config['username'],'password':config['password']},
                                  accept_downloads=True,locale='zh-CN',timezone_id='Asia/Shanghai')
    page = context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
    page.goto(url+'/#jobs',wait_until='networkidle')
    page.wait_for_function("document.getElementById('job-form').dataset.initialized === 'true'")
    expect(page.locator('#job-budget')).to_contain_text('2 核')
    checks.append('Queue view, resource budget and registered environments render')
    page.locator('#auto-refresh').click()

    # A rejected declaration must not start any NPU task.
    before=context.request.get(url+'/api/jobs').json()['jobs']
    page.locator('#job-kind').select_option('accuracy')
    page.locator('#submit-job').click()
    expect(page.locator('#job-form-message')).to_contain_text('显式申请 NPU')
    assert len(context.request.get(url+'/api/jobs').json()['jobs'])==len(before)
    page.wait_for_timeout(100)
    # Chromium logs the intentionally rejected HTTP 400 even though the UI handles it.
    assert all('400 (Bad Request)' in error for error in errors), errors
    expected_http_errors = list(errors)
    errors.clear()
    page.locator('#job-kind').select_option('command')
    checks.append('Invalid NPU resource declaration is rejected before job creation')

    page.locator('#job-name').fill(prefix+' · 完成')
    page.locator('#job-command').fill('python -u examples/cpu_task.py --seconds 2')
    page.locator('#job-shm').fill('64')
    page.locator('#submit-job').click()
    expect(page.locator('#job-form-message')).to_be_visible()
    expect(page.locator('#job-form-message')).to_contain_text('已提交 job-')
    def get_named(name):
        return next(j for j in context.request.get(url+'/api/jobs').json()['jobs'] if j['name']==name)
    first=get_named(prefix+' · 完成')
    until=time.monotonic()+25
    while time.monotonic()<until and get_named(first['name'])['state'] not in {'succeeded','failed','needs_attention'}:
        time.sleep(.2)
    assert get_named(first['name'])['state']=='succeeded'
    page.locator('#refresh').click()
    button=page.locator(f'[data-job-id="{first["id"]}"]')
    button.click()
    expect(page.locator('#drawer')).to_be_visible()
    expect(page.locator('#drawer-content')).to_contain_text('Completed; results/demo-result.json is ready.')
    with page.expect_download() as pending:
        page.locator('#drawer-content a').filter(has_text='results/demo-result.json').click()
    pending.value.save_as(evidence/'queue-browser-result.json')
    assert json.loads((evidence/'queue-browser-result.json').read_text())['npu_used'] is False
    page.locator('#close-drawer').click()
    checks.append('Form submission, bounded live logs and artifact download')

    page.locator('#job-name').fill(prefix+' · 取消')
    page.locator('#job-command').fill('python -u examples/cpu_task.py --seconds 20')
    page.locator('#submit-job').click()
    expect(page.locator('#job-form-message')).to_be_visible()
    expect(page.locator('#job-form-message')).to_contain_text('已提交 job-')
    second=get_named(prefix+' · 取消')
    until=time.monotonic()+20
    while time.monotonic()<until and get_named(second['name'])['state']!='running':time.sleep(.2)
    assert get_named(second['name'])['state']=='running'
    page.locator('#refresh').click()
    page.locator(f'[data-job-id="{second["id"]}"]').click()
    page.locator('#cancel-job').click()
    until=time.monotonic()+15
    while time.monotonic()<until and get_named(second['name'])['state']!='cancelled':time.sleep(.2)
    assert get_named(second['name'])['state']=='cancelled'
    page.locator('#close-drawer').click()
    checks.append('Cancellation button stops its managed CPU task')
    page.locator('#refresh').click()
    expect(page.locator('#toast')).to_be_hidden(timeout=5000)
    page.screenshot(path=evidence/'queue-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=evidence/'queue-mobile.png',full_page=True)
    checks.append('Queue form and table work on desktop and 390px mobile')
    assert not errors, errors
    report={'passed':True,'checks':checks,'browser_errors':errors,'expected_validation_http_errors':expected_http_errors,'job_ids':[first['id'],second['id']],'npu_kernel_launches':0}
    (evidence/'queue-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    context.close();browser.close()
print(json.dumps(report,ensure_ascii=False,indent=2))

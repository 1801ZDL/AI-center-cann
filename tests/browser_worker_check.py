"""Real worker template UI, private task visibility and downloadable isolation evidence."""
import json
import os
from pathlib import Path
import time
import uuid
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
client=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
url=f'http://127.0.0.1:{config["port"]}'
name='隔离节点网页验收-'+uuid.uuid4().hex[:6]
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':client['username'],'password':client['password']},accept_downloads=True)
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
    page.goto(url+'/#jobs',wait_until='networkidle')
    page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
    expect(page.locator('#node-summary')).to_contain_text('1 个物理资源域')
    expect(page.locator('#node-summary')).to_contain_text('3 个执行入口')
    expect(page.locator('#node-list')).to_contain_text('容器资源硬限制')
    expect(page.locator('#node-list')).to_contain_text('主机 CPU')
    expect(page.locator('#node-list')).to_contain_text('环境：已认证')
    expect(page.locator('#node-list')).to_contain_text('Python 3.10.12 / x86_64')
    expect(page.locator('#node-list')).to_contain_text('执行对账：一致')
    expect(page.locator('#node-list')).to_contain_text('节点暂存准入：字节 / inode')
    expect(page.locator('#node-list')).to_contain_text('已确认副本自动回收')
    expect(page.locator('#node-list')).to_contain_text('整机统一预算')
    expect(page.locator('#node-list')).to_contain_text('同域不叠加')
    expect(page.locator('#job-budget')).to_contain_text('内存预留（含执行开销）')
    expect(page.locator('#job-body')).not_to_contain_text('PRIVATE_ALICE_')
    page.locator('#job-template').select_option('isolation_probe')
    expect(page.locator('#job-environment')).to_have_value('worker-python')
    expect(page.locator('#job-command')).to_be_hidden()
    page.locator('#job-name').fill(name);page.locator('#submit-job').click()
    expect(page.locator('#job-form-message')).to_contain_text('已提交 job-')
    def lookup():return next(j for j in context.request.get(url+'/api/jobs').json()['jobs'] if j['name']==name)
    job=lookup();deadline=time.monotonic()+45
    while time.monotonic()<deadline and lookup()['state'] not in {'succeeded','failed','needs_attention'}:time.sleep(.2)
    assert lookup()['state']=='succeeded',lookup()
    page.locator('#refresh').click()
    page.locator(f'[data-job-id="{job["id"]}"]').click()
    expect(page.locator('#drawer-content')).to_contain_text('worker-python')
    expect(page.locator('#drawer-content')).to_contain_text('/work（容器内）')
    expect(page.locator('#drawer-content')).to_contain_text('模板参数')
    with page.expect_download() as pending:
        page.locator('#drawer-content a').filter(has_text='results/isolation.json').click()
    target=ROOT/'evidence/worker-browser-isolation.json';pending.value.save_as(target)
    result=json.loads(target.read_text());assert result['passed'] and result['uid']==23002
    assert result['cap_effective']=='0000000000000000' and not result['protected_paths_readable']
    page.locator('#drawer').evaluate('(element)=>element.scrollTop=0')
    page.screenshot(path=ROOT/'evidence/worker-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=ROOT/'evidence/worker-mobile.png',full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'job_id':job['id'],'task_uid':result['uid'],'checks':['Physical resource domains distinguished from execution entries',
        'Shared physical budget and execution overhead rendered',
        'Node aggregate resources visible without private process data','Certified environment Python/machine and image visible','Execution inventory reconciliation visible','Template uses actual managed-container environment',
        'Actual UID/no-capability evidence downloadable','Private task remains hidden','Desktop/mobile layout'],
        'browser_errors':errors,'npu_used':False}
(ROOT/'evidence/worker-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

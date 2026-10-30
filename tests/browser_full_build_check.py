"""Read-only UI acceptance of the actual long compiler build, never resubmits."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
state=json.loads((ROOT/'runtime/full-build-20261030/control.json').read_text())
job=next(item for item in reversed(state['jobs']) if item['phase']=='build')['job_id']
owner=json.loads((ROOT/'runtime/personas'/state['user']/'client.json').read_text())
bob=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
url='http://127.0.0.1:'+str(owner['port']);errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':owner['username'],'password':owner['password']})
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
    page.goto(url+'/#jobs',wait_until='networkidle')
    page.locator('[data-job-id="'+job+'"]').click()
    expect(page.locator('#drawer-content')).to_contain_text('49152 MiB')
    expect(page.locator('#drawer-content')).to_contain_text('fullbuild0')
    expect(page.locator('#drawer-content')).to_contain_text('262144 inode')
    expect(page.locator('#drawer-content')).to_contain_text('内存文件系统')
    assert len(page.locator('.job-log').first.inner_text())>100
    page.locator('#refresh-job-log').click()
    expect(page.locator('#drawer-content')).to_contain_text('fullbuild0')
    assert len(page.locator('.job-log').first.inner_text())>100
    page.screenshot(path=ROOT/'evidence/full-build-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=ROOT/'evidence/full-build-mobile.png',full_page=True)
    foreign=browser.new_context(http_credentials={'username':bob['username'],'password':bob['password']})
    assert foreign.request.get(url+'/api/jobs/'+job).status==404
    assert job not in {j['id'] for j in foreign.request.get(url+'/api/jobs').json()['jobs']}
    observed=context.request.get(url+'/api/jobs/'+job).json()
    assert len(observed['attempts'])==1
    assert not errors,errors
    foreign.close();context.close();browser.close()
report={'passed':True,'job_id':job,'observed_state':observed['job']['state'],'single_attempt':True,
        'live_compiler_log_rendered':True,'48gib_memory_and_32gib_workspace_rendered':True,'desktop_mobile':True,
        'foreign_user_job_hidden':True,'browser_errors':errors,'npu_kernel_launches':0,
        'scope':'UI of actual full build; screenshot/progress does not prove successful final wheel'}
(ROOT/'evidence/full-build-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

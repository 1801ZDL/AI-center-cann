"""Exercise only the restored new IR fixture created by live_retention_check."""
import hashlib
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
proof=json.loads((ROOT/'evidence/retention-live-validation.json').read_text());assert proof['passed']
config=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
url='http://127.0.0.1:'+str(config['port']);job_id=proof['job_id'];errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':config['username'],'password':config['password']},accept_downloads=True)
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
    page.goto(url+'/#jobs',wait_until='networkidle')
    page.locator('[data-job-id="'+job_id+'"]').click()
    expect(page.locator('#storage-job')).to_have_text('归档移入回收站')
    page.locator('#storage-job').click()
    expect(page.locator('#storage-job')).to_have_text('恢复归档')
    expect(page.locator('#drawer-content')).to_contain_text('任务状态与结果摘要仍保留')
    expect(page.locator('#drawer-content .artifact-list a')).to_have_count(0)
    assert context.request.get(url+'/api/jobs/'+job_id).json()['job']['state']=='succeeded'
    page.screenshot(path=ROOT/'evidence/retention-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=ROOT/'evidence/retention-mobile.png',full_page=True)
    page.locator('#storage-job').click()
    expect(page.locator('#storage-job')).to_have_text('归档移入回收站')
    with page.expect_download() as download:
        page.locator('#drawer-content .artifact-list a').filter(has_text='ir/canonical.mlir').click()
    target=ROOT/'evidence/retention-browser-canonical.mlir';download.value.save_as(target)
    assert hashlib.sha256(target.read_bytes()).hexdigest()==proof['actual_ir_sha256']
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'job_id':job_id,'owner_ui_trash_restore':True,'retired_artifact_links_disabled':True,
        'restored_download_sha256':proof['actual_ir_sha256'],'desktop_mobile':True,'browser_errors':errors,
        'permanent_delete_ui':False,'model_calls':0,'npu_kernel_launches':0}
(ROOT/'evidence/retention-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

"""Read-only result table, source evidence, CSV and privacy in desktop/mobile browsers."""
import csv
import io
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
accepted=json.loads((ROOT/'evidence/workflow-live-validation.json').read_text());assert accepted['passed']
bob=json.loads((ROOT/'runtime/personas/demo_bob/client.json').read_text())
alice=json.loads((ROOT/'runtime/personas/demo_alice/client.json').read_text())
url='http://127.0.0.1:'+str(bob['port']);errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':bob['username'],'password':bob['password']},accept_downloads=True)
    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(url+'/#results',wait_until='networkidle')
    expect(page.locator('#page-title')).to_contain_text('任务结果')
    expect(page.locator('[data-result-job="'+accepted['ir_job']+'"]')).to_be_visible()
    page.locator('[data-result-job="'+accepted['ir_job']+'"]').click()
    expect(page.locator('#drawer-content')).to_contain_text('Actual bishengir-opt folded 20 + 22 to constant 42.')
    with page.expect_download() as pending:
        page.locator('#drawer-content a').filter(has_text='ir/canonical.mlir').click()
    target=ROOT/'evidence/workflow-browser-canonical.mlir';pending.value.save_as(target)
    assert 'arith.constant 42' in target.read_text()
    page.locator('#close-drawer').click()
    with page.expect_download() as pending:page.locator('#workflow-csv').click()
    csv_path=ROOT/'evidence/workflow-browser-results.csv';pending.value.save_as(csv_path)
    rows=list(csv.DictReader(io.StringIO(csv_path.read_text(encoding='utf-8-sig'))))
    assert any(row['job_id']==accepted['ir_job'] for row in rows)
    assert all(row['owner']=='demo_bob' for row in rows)
    assert any(row['name']=="'=1+1" for row in rows)
    page.locator('#workflow-kind').select_option('ir')
    expect(page.locator('#workflow-csv')).to_have_attribute('href','/api/results.csv?kind=ir')
    kinds=page.locator('#workflow-result-body tr td:nth-child(2)')
    expect(kinds.first).to_have_text('IR 处理')
    assert all(text=='IR 处理' for text in kinds.all_text_contents())
    page.screenshot(path=ROOT/'evidence/workflow-results-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=ROOT/'evidence/workflow-results-mobile.png',full_page=True)
    context.close()
    private=browser.new_context(http_credentials={'username':alice['username'],'password':alice['password']})
    other=private.new_page();other.goto(url+'/#results',wait_until='networkidle')
    expect(other.locator('#workflow-result-body')).to_contain_text('暂无工作流结果')
    assert not set(accepted['job_ids'])&{row['job_id'] for row in private.request.get(url+'/api/results').json()['rows']}
    private.close();browser.close()
assert not errors,errors
report={'passed':True,'checks':['Personal results view survives refresh','Real BishengIR output downloadable',
        'CSV uses same ACL and escapes formulas','IR filter applies','Desktop/mobile width','Other private account cannot see these rows'],
        'job_id':accepted['ir_job'],'new_jobs_submitted':0,'browser_errors':errors}
(ROOT/'evidence/workflow-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

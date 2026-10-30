"""Read-only UI verification of an already completed real Agent invocation."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'runtime/config.json').read_text())
latest=json.loads((ROOT/'runtime/agents/latest.json').read_text())
summary=json.loads((Path(latest['directory'])/'summary.json').read_text())
assert summary['verified'] is True
job_id=summary['verified_jobs'][0]['job']['id']
workflow=(summary['verified_jobs'][0]['result'].get('artifact') or {}).get('kind')
result_name='results/workload-result.json' if workflow else 'results/demo-result.json'
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':config['username'],'password':config['password']},accept_downloads=True)
    page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(f'http://127.0.0.1:{config["port"]}/#jobs',wait_until='networkidle')
    page.locator(f'[data-job-id="{job_id}"]').click()
    content=page.locator('#drawer-content')
    expect(content).to_contain_text('opencode-mcp')
    expect(content).to_contain_text(summary['actual_model'])
    expect(content).to_contain_text(summary['run_id'])
    expect(content).to_contain_text('Actual bishengir-opt folded 20 + 22 to constant 42.' if workflow=='ir' else 'Completed; results/demo-result.json is ready.')
    with page.expect_download() as pending:
        content.locator('a').filter(has_text=result_name).click()
    target=ROOT/'evidence/agent-browser-task-result.json'
    pending.value.save_as(target)
    downloaded=json.loads(target.read_text())
    assert downloaded['passed'] is True
    if workflow=='ir':assert downloaded['constant_folded_value']==42
    else:assert downloaded['npu_used'] is False
    page.locator('#drawer').evaluate('(element)=>element.scrollTop=0')
    page.screenshot(path=ROOT/'evidence/agent-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=ROOT/'evidence/agent-mobile.png',full_page=True)
    assert not errors,errors
    context.close();browser.close()
report={'passed':True,'job_id':job_id,'run_id':summary['run_id'],
        'checks':['Agent provenance rendered','Real stdout visible','Actual task result downloadable; private harness summary not written to controller filesystem','Desktop/mobile layout'],
        'browser_errors':errors,'new_jobs_submitted':0}
(ROOT/'evidence/agent-browser-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

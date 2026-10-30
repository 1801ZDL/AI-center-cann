"""Optional real-browser QA; dependencies stay in the project's .qa-venv/.qa-libs."""
import json
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence'
EVIDENCE.mkdir(mode=0o700, exist_ok=True)
config = json.loads((ROOT / 'runtime/config.json').read_text())
url = f"http://127.0.0.1:{config['port']}"
errors = []
checks = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),
                                args=['--disable-dev-shm-usage'])
    context = browser.new_context(viewport={'width': 1440, 'height': 1080},
                                  http_credentials={'username': config['username'], 'password': config['password']},
                                  accept_downloads=True, locale='zh-CN', timezone_id='Asia/Shanghai')
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    response = page.goto(url, wait_until='networkidle')
    assert response.status == 200
    page.wait_for_function("document.getElementById('process-count').textContent !== '—'")
    expect(page.locator('#devices .device-card')).to_have_count(2, timeout=45000)
    checks.append('Authenticated dashboard renders real processes and both NPU devices')

    page.locator('[data-view="processes"]').click()
    expect(page.locator('#page-title')).to_contain_text('进程观察')
    page.locator('[data-scope="container"]').click()
    expect(page.locator('#process-body tr')).not_to_have_count(0)
    assert all('Docker / zdl' in text for text in page.locator('#process-body tr').all_text_contents())
    checks.append('Navigation and zdl scope filter')

    page.locator('#search').fill('a-name-that-is-not-a-process-928145')
    expect(page.locator('#process-body')).to_contain_text('没有匹配的进程')
    page.locator('#search').clear()
    page.locator('#sort').select_option('memory')
    page.locator('#process-body .detail-button').first.click()
    expect(page.locator('#drawer')).to_be_visible()
    expect(page.locator('#drawer-content')).to_contain_text('所属 UID')
    page.keyboard.press('Escape')
    expect(page.locator('#drawer')).to_be_hidden()
    checks.append('Search empty state, sorting, process details and Escape dismissal')

    page.locator('[data-view="container"]').click()
    expect(page.locator('.host-panel')).to_be_visible()
    expect(page.locator('#container-status')).to_have_text('RUNNING')
    checks.append('Container resource view')

    page.locator('[data-view="experiments"]').click()
    expect(page.locator('.experiment-card')).to_have_count(3)
    page.locator('[data-record="causal-conv"]').click()
    expect(page.locator('#drawer-content')).to_contain_text('full_simulation_started')
    page.locator('#close-drawer').click()
    checks.append('Historical record details preserve validation scope')

    page.locator('[data-view="overview"]').click()
    page.locator('[data-scope="all"]').click()
    page.locator('#sort').select_option('cpu')
    page.locator('#auto-refresh').click()
    expect(page.locator('#auto-refresh')).to_have_attribute('aria-pressed', 'false')
    page.locator('#refresh').click()
    expect(page.locator('#toast')).to_be_visible()
    checks.append('Auto-refresh pause and manual refresh')

    with page.expect_download() as info:
        page.get_by_role('link', name='导出快照').click()
    info.value.save_as(EVIDENCE / 'browser-export.json')
    exported = json.loads((EVIDENCE / 'browser-export.json').read_text())
    assert exported['snapshot']['scope']['username'] == config['username']
    assert config['password'] not in json.dumps(exported)
    checks.append('JSON export with no login password')

    live = context.request.get(url + '/api/snapshot').json()
    stale = json.loads(json.dumps(live))
    stale['snapshot']['sampled_at'] = '2026-01-01T00:00:00+00:00'
    page.route('**/api/snapshot', lambda route: route.fulfill(json=stale))
    page.locator('#refresh').click()
    expect(page.locator('#warning-box')).to_contain_text('采样已延迟')
    page.unroute('**/api/snapshot')
    page.locator('#refresh').click()
    expect(page.locator('#warning-box')).to_be_hidden()
    checks.append('Stale sample warning and live recovery')

    page.locator('#auto-refresh').click()
    expect(page.locator('#toast')).to_be_hidden(timeout=5000)
    page.screenshot(path=EVIDENCE / 'dashboard-desktop.png', full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_timeout(150)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile page overflows horizontally'
    page.screenshot(path=EVIDENCE / 'dashboard-mobile.png', full_page=True)
    checks.append('Desktop and 390px mobile layout without page-level overflow')
    assert not errors, errors
    checks.append('No browser console errors or JavaScript exceptions')
    context.close()
    browser.close()

result = {'passed': True, 'checks': checks, 'browser_errors': errors}
(EVIDENCE / 'browser-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))

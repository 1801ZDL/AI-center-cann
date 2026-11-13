"""Finite file:// integration check; synthetic package only, no central service."""
import argparse
import json
import os
from pathlib import Path
import resource

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('html', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.sched_setaffinity(0, {max(os.sched_getaffinity(0))})
    os.nice(10)
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    requests, errors = [], []
    checks = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'), headless=True,
            args=['--disable-dev-shm-usage', '--disable-gpu', '--no-zygote', '--single-process',
                  '--disable-background-networking', '--disable-component-update'])
        try:
            context = browser.new_context(offline=True, viewport={'width': 1440, 'height': 1000},
                                          locale='zh-CN', timezone_id='Asia/Shanghai', accept_downloads=True)
            page = context.new_page()
            page.on('request', lambda request: requests.append(request.url))
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
            page.goto(args.html.resolve().as_uri(), wait_until='load', timeout=10000)
            expect(page.locator('#connection')).to_have_text('离线展示')
            expect(page.locator('#devices .device-card')).to_have_count(4)
            expect(page.locator('#job-body tr')).to_have_count(8)
            checks.append('Double-click file URL renders the full UI with browser networking disabled')
            page.screenshot(path=str(args.output / 'overview-desktop.png'), full_page=True)
            page.locator('[data-view="jobs"]').click()
            expect(page.locator('#submit-job')).to_be_disabled()
            expect(page.locator('#preview-job')).to_be_disabled()
            page.locator('#job-body [data-job-id]').first.click()
            expect(page.locator('#drawer')).to_be_visible()
            expect(page.locator('#drawer-content')).to_contain_text('OFFLINE DEMO')
            expect(page.locator('#drawer-content')).to_contain_text('job-demo-perf')
            expect(page.locator('#cancel-job')).to_have_count(0)
            page.keyboard.press('Escape')
            expect(page.locator('#drawer')).to_be_hidden()
            checks.append('Task detail and synthetic logs open; task writes and cancellation unavailable')
            page.locator('[data-view="results"]').click()
            expect(page.locator('#workflow-result-body tr')).to_have_count(4)
            page.locator('#workflow-kind').select_option('performance')
            expect(page.locator('#workflow-result-body tr')).to_have_count(1)
            expect(page.locator('#workflow-result-body')).to_contain_text('23.180')
            with page.expect_download(timeout=5000) as download:
                page.locator('#workflow-csv').click()
            target = args.output / 'demo-results.csv'
            download.value.save_as(target)
            csv = target.read_text(encoding='utf-8-sig')
            assert 'DEMO_ONLY' in csv and 'job-demo-perf-done' in csv and 'job-demo-accuracy' not in csv
            checks.append('Results filter and offline CSV download honor the selected kind')
            page.screenshot(path=str(args.output / 'results-desktop.png'), full_page=True)
            page.locator('[data-view="processes"]').click()
            page.locator('#search').fill('not-a-demo-process')
            expect(page.locator('#process-body')).to_contain_text('没有匹配的进程')
            page.locator('#search').clear()
            page.locator('[data-view="container"]').click()
            expect(page.locator('#container-status')).to_have_text('RUNNING')
            page.locator('[data-view="experiments"]').click()
            expect(page.locator('.experiment-card')).to_have_count(3)
            page.locator('.experiment-card').first.click()
            expect(page.locator('#drawer-content')).to_contain_text('离线演示构造')
            page.keyboard.press('Escape')
            checks.append('Process search, container panel and experiment details are usable offline')
            page.locator('[data-view="overview"]').click()
            page.locator('#demo-scenario').select_option('pressure')
            expect(page.locator('#warning-box')).to_contain_text('公共内存压力')
            expect(page.locator('#job-body')).to_contain_text('等待公共内存压力恢复')
            page.locator('#demo-scenario').select_option('unknown')
            expect(page.locator('#job-body')).to_contain_text('执行回执中断')
            page.locator('#demo-scenario').select_option('normal')
            checks.append('All three synthetic scenarios update locally without executing tasks')
            assert page.evaluate('() => offlineAPI("/api/jobs", {}).then(()=>false,()=>true)')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(args.output / 'overview-mobile.png'), full_page=True)
            checks.append('390px layout fits the viewport')
            assert not [url for url in requests if url.startswith(('http:', 'https:', 'ws:', 'wss:'))], requests
            assert not errors, errors
            checks.append('No HTTP/WebSocket requests, console errors or page exceptions')
            context.close()
        finally:
            browser.close()
    report = {'passed': True, 'checks': checks, 'network_requests': requests, 'errors': errors,
              'scope': 'One browser, one tab, serial checks, synthetic file-only presentation; no server or NPU access'}
    (args.output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'checks': len(checks), 'report': str(args.output / 'verification.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()

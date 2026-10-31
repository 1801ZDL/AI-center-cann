"""Packaged web with real HTTP/ACL/archive and synthetic management observations.

No NPU, model, Main configuration or other person's container is used.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import server as server_module
from deployment import bundle,unpack_release
from jobs import atomic_json
from test_npu_conditions import ConditionProtocolTests


def main():
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/condition-browser';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    original=server_module.ROOT;server_module.ROOT=code
    fixture=ConditionProtocolTests();fixture.setUp();jobs=fixture.jobs;identifiers={};errors=[];outside=[];posts=[]
    server=None;thread=None
    try:
        hostile='<img src=x onerror="window.conditionInjected=true">'
        for label,health,pre in [('alert','Alarm',True),('sampled','OK',True),('missing','OK',False),('legacy','OK',True)]:
            fixture.fixture.beat(fixture.node)
            job,auth,result=fixture.run_case(health,pre)
            row={'job_id':job['id'],'owner':job['owner'],'project':job['project'],'kind':'accuracy',
                'name':'Synthetic '+label+' '+hostile,'state':'failed','passed':False,'metrics':{},
                'error':'Synthetic protocol; no numerical task executed','recorded_at':job['finished_at']}
            if label!='legacy':row['device_conditions']=job['device_conditions']
            with jobs.connect() as db:db.execute('INSERT INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row)))
            identifiers[label]=job['id']
        config={'username':'operator','password':'offline-only','container':'zdl'}
        server=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(),config,jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(server.server_port)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            ctx=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':'alice','password':fixture.fixture.passwords['alice']},accept_downloads=True)
            def route(item):
                if not item.request.url.startswith(url+'/'):outside.append('blocked');item.abort()
                else:item.continue_()
            ctx.route('**/*',route);page=ctx.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda r:posts.append(r.method) if r.method!='GET' else None)
            page.goto(url+'/#results',wait_until='networkidle');page.locator('#auto-refresh').click()
            table=page.locator('#workflow-result-body')
            for label,text in [('alert','采样发现设备告警'),('sampled','采样未见告警（非自检通过）'),('missing','设备观测不完整'),('legacy','设备条件未验证')]:
                row=table.locator('tr').filter(has=page.locator('[data-result-job="'+identifiers[label]+'"]'))
                expect(row).to_contain_text(text);expect(row).to_contain_text('失败')
            assert not page.evaluate('window.conditionInjected||false');assert table.locator('img').count()==0
            page.locator('[data-result-job="'+identifiers['alert']+'"]').click()
            expect(page.locator('#drawer-content')).to_contain_text('未关联每日 UT')
            with page.expect_download() as download:
                page.locator('#drawer-content a').filter(has_text='npu-conditions.json').click()
            path=base/'downloaded-conditions.json';download.value.save_as(path)
            actual=jobs.get(identifiers['alert'])['device_conditions']
            assert hashlib.sha256(path.read_bytes()).hexdigest()==actual['sha256']
            assert json.loads(path.read_text())['summary']['status']=='adverse_observed'
            page.locator('#close-drawer').click()
            with page.expect_download() as download:page.locator('#workflow-csv').click()
            path=base/'results.csv';download.value.save_as(path)
            rows=list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
            assert len(rows)==4 and all(row['owner']=='alice' for row in rows)
            assert {json.loads(row['device_conditions'])['status'] for row in rows}=={'adverse_observed','sampled_no_alerts','incomplete','unverified'}
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=base/'mobile.png',full_page=True)
            private=browser.new_context(http_credentials={'username':'bob','password':fixture.fixture.passwords['bob']})
            other=private.new_page();other.goto(url+'/#results',wait_until='networkidle')
            expect(other.locator('#workflow-result-body')).to_contain_text('暂无工作流结果')
            assert private.request.get(url+'/api/jobs/'+identifiers['alert']+'/artifact?name=npu-conditions.json').status in {403,404}
            assert private.request.get(url+'/api/results').json()['total']==0
            private.close();ctx.close();browser.close()
        assert not errors and not outside and not posts,(errors,outside,posts)
        report={'passed':True,'scope':'Packaged web, actual HTTP/archive/ACL, synthetic management and result rows; no numerical workload',
            'checks':['four condition labels separate from failure','legacy remains unverified','private normalized evidence download SHA',
                      'CSV conditions and owner scope','other owner denied archive and rows','HTML escaping','mobile width','no mutation/model/external requests'],
            'job_ids':identifiers,'release':release['release_id'],'new_npu_launches':0,'new_model_calls':0,'browser_errors':errors}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/npu-condition-browser.json',report)
        print(json.dumps(report,ensure_ascii=False))
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(3)
        fixture.doCleanups();server_module.ROOT=original


if __name__=='__main__':main()

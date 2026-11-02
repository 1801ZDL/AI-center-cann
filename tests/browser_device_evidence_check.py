"""Packaged web, real HTTP/ACL, synthetic fixed microcheck evidence; no NPU."""
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
import test_device_evidence as cases
from deployment import bundle,unpack_release
from jobs import atomic_json


def main():
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/evidence-browser';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    original_root=server_module.ROOT;server_module.ROOT=code
    fixture=cases.EvidenceTests('runTest');fixture.setUp();jobs=fixture.jobs
    server=None;thread=None;errors=[];outside=[];writes=[]
    try:
        bob_token=jobs.access.issue_token('bob',{'jobs:read'})
        alice_token=jobs.access.issue_token('alice',{'jobs:read'})
        fixture.source(False);initial=fixture.policy('annotate');fixture.bob_access()
        job=fixture.consumer();grant,auth=fixture.grant(job);unconfirmed=fixture.complete(grant,auth)
        assert unconfirmed['device_conditions']['daily_ut']=='unconfirmed_before_start'
        source=fixture.source();current=fixture.policy(name='required-current',enable=False)
        jobs.device_evidence.control(current['id'],'enable',[initial['id']])
        job=fixture.consumer('performance');grant,auth=fixture.grant(job);linked=fixture.complete(grant,auth)
        assert linked['workflow_result']['metrics']['mean_us']==30.5
        assert linked['device_conditions']['daily_ut']=='linked_microchecks'
        for i in range(3):fixture.policy(name='draft-'+str(i),enable=False)
        hostile='<img src=x onerror="window.evidenceInjected=true">'
        with jobs.connect() as db:
            row=json.loads(db.execute('SELECT record FROM workflow_results WHERE job_id=?',(linked['id'],)).fetchone()[0])
            row['name']=hostile;db.execute('UPDATE workflow_results SET record=? WHERE job_id=?',(json.dumps(row),linked['id']))
        config={'username':'operator','password':'offline-only','container':'zdl','interval':15}
        server=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(),config,jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(server.server_port)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            ctx=browser.new_context(viewport={'width':1440,'height':1080},extra_http_headers={'Authorization':'Bearer '+bob_token})
            def route(item):
                if not item.request.url.startswith(url+'/'):outside.append('blocked');item.abort()
                else:item.continue_()
            ctx.route('**/*',route);page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:writes.append(r.method) if r.method!='GET' else None)
            page.goto(url+'/#results',wait_until='networkidle');page.locator('#auto-refresh').click()
            body=page.locator('#workflow-result-body');rules=page.locator('#device-evidence-body')
            linked_row=body.locator('tr').filter(has=page.locator('[data-result-job="'+linked['id']+'"]'))
            expect(linked_row).to_contain_text('30.500');expect(linked_row).to_contain_text('已关联有限微用例')
            expect(linked_row).to_contain_text('报告核对通过');expect(linked_row).to_contain_text(hostile)
            expect(body.locator('tr').filter(has=page.locator('[data-result-job="'+unconfirmed['id']+'"]'))).to_contain_text('启动前自检未确认')
            assert body.locator('img').count()==0 and not page.evaluate('window.evidenceInjected||false')
            expect(rules).to_contain_text('近期有限微用例一致');expect(rules).to_contain_text('启动前要求自检')
            expect(page.locator('#device-evidence-page')).to_contain_text('5 条')
            assert rules.locator('tr').count()==4
            page.locator('#device-evidence-next').click();expect(rules).to_contain_text('draft-2')
            assert rules.locator('tr').count()==1
            page.locator('#device-evidence-previous').click();expect(rules).to_contain_text('required-current')
            page.locator('[data-result-job="'+linked['id']+'"]').click()
            drawer=page.locator('#drawer-content');expect(drawer).to_contain_text(source['id'])
            expect(drawer).to_contain_text(current['id']);expect(drawer).to_contain_text('已关联有限微用例')
            link=drawer.locator('a').filter(has_text='npu-conditions.json');expect(link).to_have_count(1)
            response=ctx.request.get(url+link.get_attribute('href'));raw=response.body()
            assert response.status==200 and hashlib.sha256(raw).hexdigest()==linked['device_conditions']['sha256']
            evidence=json.loads(raw)['summary']['selfcheck'];assert evidence['status']=='linked_microchecks'
            assert evidence['performance_qualified'] is False and evidence['full_hardware_certified'] is False
            page.locator('#close-drawer').click()
            response=ctx.request.get(url+'/api/results.csv');assert response.status==200
            rows=list(csv.DictReader(io.StringIO(response.body().decode('utf-8-sig'))))
            assert len(rows)==2 and all(row['owner']=='bob' for row in rows)
            row=next(row for row in rows if row['job_id']==linked['id'])
            assert json.loads(row['metrics'])['mean_us']==30.5
            assert json.loads(row['device_conditions'])['selfcheck']['before']['source_job_id']==source['id']
            assert ctx.request.get(url+'/api/jobs/'+source['id']).status in {403,404}
            jobs.device_evidence.control(current['id'],'pause');page.reload(wait_until='networkidle')
            expect(rules).to_contain_text('等待明确批准');expect(body).to_contain_text('已关联有限微用例')
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=base/'mobile.png',full_page=True)
            other=browser.new_context(extra_http_headers={'Authorization':'Bearer '+alice_token})
            peer=other.new_page();peer.goto(url+'/#results',wait_until='networkidle')
            expect(peer.locator('#device-evidence-body')).to_contain_text('没有项目自检规则')
            assert other.request.get(url+'/api/jobs/'+linked['id']).status in {403,404}
            assert other.request.get(url+'/api/device-evidence').json()['total']==0
            other.close();ctx.close();browser.close()
        assert not errors and not outside and not writes,(errors,outside,writes)
        report={'passed':True,'scope':'Packaged web and actual HTTP/ACL/archive; synthetic protected microcheck captures, no device workload',
            'checks':['bounded link separate from numerical/performance qualification','original performance mean and success preserved',
                'later pass never backfills missing start evidence','project policy modes and pagination','source job and approval pinned in drawer',
                'download SHA matches sealed condition summary','CSV preserves samples and exact source reference',
                'shared summary does not grant source archive access','other project cannot read policy or consumer job',
                'pause blocks prospective proof without rewriting historic results','HTML escaping and mobile width','GET-only UI without model or external request'],
            'release':release['release_id'],'source_job_id':source['id'],'consumer_job_id':linked['id'],
            'new_npu_launches':0,'new_model_calls':0,'browser_errors':errors}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/device-evidence-browser.json',report);print(json.dumps(report,ensure_ascii=False))
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(3)
        fixture.doCleanups();server_module.ROOT=original_root


if __name__=='__main__':main()

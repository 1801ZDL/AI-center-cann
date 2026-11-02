"""Actual packaged HTTP/UI/ACL with explicitly synthetic fixed reference sources."""
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
import test_performance_reference as cases
from deployment import bundle,unpack_release
from jobs import atomic_json


def main():
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/reference-browser';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    original_root=server_module.ROOT;server_module.ROOT=code
    fixture=cases.ReferenceTests('runTest');fixture.setUp();jobs=fixture.jobs
    server=None;thread=None;errors=[];outside=[];writes=[]
    try:
        alice=jobs.access.issue_token('alice',{'jobs:read'},lifetime_seconds=604800)
        bob=jobs.access.issue_token('bob',{'jobs:read'},lifetime_seconds=604800)
        fixture.collect();approved=fixture.create();fixture.policy('annotate')
        results={}
        for label,value in [('within',26),('outside',40)]:
            fixture.sample(value);fixture.f.bob_access();job=fixture.f.consumer('performance');grant,auth=fixture.f.grant(job)
            results[label]=fixture.f.complete(grant,auth)
        for i in range(4):fixture.create(False,name='draft-'+str(i))
        assert results['within']['device_conditions']['selfcheck']['before']['performance_reference']['status']=='within_reference_envelope'
        assert results['outside']['device_conditions']['selfcheck']['before']['performance_reference']['status']=='outside_reference_envelope'
        server=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(),{'username':'operator','password':'offline-only','interval':15,'container':'zdl'},jobs))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(server.server_port)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':1440,'height':1080},extra_http_headers={'Authorization':'Bearer '+bob})
            def route(item):
                if item.request.url.startswith(url+'/'):item.continue_()
                else:outside.append('blocked');item.abort()
            context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:writes.append(r.method) if r.method!='GET' else None)
            page.goto(url+'/#results',wait_until='networkidle');page.locator('#auto-refresh').click()
            table=page.locator('#workflow-result-body')
            for label,text in [('within','在批准的经验参考内'),('outside','偏离批准的经验参考')]:
                row=table.locator('tr').filter(has=page.locator('[data-result-job="'+results[label]['id']+'"]'))
                expect(row).to_contain_text(text);expect(row).to_contain_text('30.500');expect(row).to_contain_text('报告核对通过')
            expect(page.locator('#performance-reference-body')).to_contain_text('没有固定性能参考')
            page.locator('[data-result-job="'+results['within']['id']+'"]').click();drawer=page.locator('#drawer-content')
            expect(drawer).to_contain_text(approved['id']);expect(drawer).to_contain_text('[1.04,1.04,1.04]')
            expect(drawer).to_contain_text('非性能资格')
            link=drawer.locator('a').filter(has_text='npu-conditions.json');response=context.request.get(url+link.get_attribute('href'))
            assert response.status==200 and hashlib.sha256(response.body()).hexdigest()==results['within']['device_conditions']['sha256']
            reference=json.loads(response.body())['summary']['selfcheck']['before']['performance_reference']
            assert reference['round_ratios']==[1.04]*3 and not reference['performance_qualified']
            assert context.request.get(url+'/api/jobs/'+fixture.sources[0]).status in {403,404}
            page.locator('#close-drawer').click()
            response=context.request.get(url+'/api/results.csv');rows=list(csv.DictReader(io.StringIO(response.body().decode('utf-8-sig'))))
            assert len(rows)==2 and all(json.loads(r['metrics'])['mean_us']==30.5 for r in rows)
            actual=next(r for r in rows if r['job_id']==results['outside']['id'])
            assert json.loads(actual['device_conditions'])['selfcheck']['before']['performance_reference']['status']=='outside_reference_envelope'
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=base/'consumer-mobile.png',full_page=True)
            owner=browser.new_context(viewport={'width':1440,'height':1080},extra_http_headers={'Authorization':'Bearer '+alice})
            owner.route('**/*',route);source=owner.new_page();source.on('pageerror',lambda e:errors.append(str(e)))
            source.on('request',lambda r:writes.append(r.method) if r.method!='GET' else None)
            source.goto(url+'/#results',wait_until='networkidle');source.locator('#auto-refresh').click()
            panel=source.locator('#performance-reference-body');expect(panel).to_contain_text('25.000 µs')
            expect(panel).to_contain_text('3 个独立来源');expect(panel).to_contain_text('0.8–1.2')
            assert panel.locator('tr').count()==4
            source.locator('#performance-reference-next').click();expect(panel).to_contain_text('draft-3');assert panel.locator('tr').count()==1
            source.locator('#performance-reference-prev').click();expect(panel).to_contain_text('known-reference')
            jobs.performance_references.control(approved['id'],'pause');source.reload(wait_until='networkidle')
            expect(panel).to_contain_text('已暂停');expect(panel).to_contain_text('等待明确批准')
            page.reload(wait_until='networkidle');expect(table).to_contain_text('在批准的经验参考内')
            source.set_viewport_size({'width':390,'height':844});assert source.evaluate('document.documentElement.scrollWidth<=innerWidth')
            source.screenshot(path=base/'reference-mobile.png',full_page=True)
            owner.close();context.close();browser.close()
        assert not errors and not outside and not writes,(errors,outside,writes)
        report={'passed':True,'scope':'Packaged web/HTTP/archive/CSV and project ACL; fixed NPU reference captures are synthetic, no actual performance baseline',
            'checks':['bounded within/outside labels separate from original success','business mean30.5 stays separate from fixed-reference ratio1.04',
                'same-job reference drawer and archive SHA','CSV retains observed departure without rewriting result',
                'consumer summary does not grant source archive or reference project access','owner sees approved empirical center and bounds',
                'reference pagination','pause does not rewrite historic consumer result','mobile width on both scopes','GET-only without model or external calls'],
            'release':release['release_id'],'consumer_job_ids':{k:v['id'] for k,v in results.items()},'browser_errors':errors,'new_npu_launches':0,'new_model_calls':0}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/performance-reference-browser.json',report);print(json.dumps(report,ensure_ascii=False))
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(3)
        fixture.doCleanups();server_module.ROOT=original_root


if __name__=='__main__':main()

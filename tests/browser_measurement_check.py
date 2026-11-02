"""Packaged UI/HTTP/archive with synthetic device windows, no NPU workload."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock,patch

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import server as server_module
import test_device_evidence as cases
from deployment import bundle,unpack_release
from jobs import atomic_json
import npu_measurement as measurement


def main():
    base=Path(os.environ['LAB_JOB_DIR'])/'tmp/measurement-browser';base.mkdir(mode=0o700)
    release=bundle(base/'server.tar.gz');code,manifest=unpack_release(base/'server.tar.gz',base)
    old_root=server_module.ROOT;server_module.ROOT=code
    fixture=cases.EvidenceTests('runTest');fixture.setUp();jobs=fixture.jobs
    server=None;thread=None;errors=[];outside=[];writes=[];records={}
    try:
        bob=jobs.access.issue_token('bob',{'jobs:read'});alice=jobs.access.issue_token('alice',{'jobs:read'})
        original_beat=fixture.daily.fixture.beat;current=100;compatible=True
        def reading(instant):
            return {'status':'ok','sampled_at':instant,'policy_sha256':__import__('npu_device').fingerprint(fixture.daily.fixture.policy),
                'devices':[{'id':0,'health':'OK','process_count':0,'utilization_pct':0,
                    'frequency':{'schema':'ascend-lab-npu-frequency/v1','status':'sampled','source':'npu-smi info -t common',
                        'device_id':0,'sampled_at':instant,'current_mhz':current,'rated_mhz':1650}}]}
        def capable(**body):
            body['capabilities']+=[measurement.CAPABILITY];body['measurement_source_sha256']=measurement.source_sha() if compatible else '0'*64
            for card in body['npu_observation']['devices']:
                card['frequency']={**reading(body['npu_observation']['sampled_at'])['devices'][0]['frequency'],'device_id':card['id']}
            return original_beat(**body)
        with patch.object(fixture.daily.fixture,'beat',side_effect=capable):
            fixture.daily.beat();fixture.bob_access()
            for label in ['recorded','interrupted','legacy']:
                compatible=label!='legacy';current=100;fixture.daily.beat()
                job=fixture.consumer('performance');grant,auth=fixture.grant(job);before=reading(fixture.iso())
                current=1650;fixture.advance(1);start=fixture.iso();fixture.advance(1);end=fixture.iso()
                current=100;fixture.advance(1);after=reading(fixture.iso())
                document={'schema':measurement.SCHEMA,'source_sha256':measurement.source_sha(),
                    'binding':{**{k:grant.get(k) for k in ['job_id','attempt_id','node_id','physical_id','environment_sha256','npu_policy_sha256']},'device_ids':[0]},
                    'queries':2,'restarts':0,'windows':[{'id':'mm512_fp16','state':'closed' if label=='recorded' else 'interrupted',
                        'started_at':start,'ended_at':end,'started_monotonic':100.,'ended_monotonic':101.,
                        'before':{**before,'status':'sampled'},'after':{**after,'status':'sampled'}}]}
                original_complete=jobs.nodes.complete
                def complete(node,body):return original_complete(node,{**body,'result':{**body['result'],**({'measurement_evidence':document} if label!='legacy' else {})}})
                with patch.object(jobs.nodes,'complete',side_effect=complete):records[label]=fixture.complete(grant,auth)
        actual=records['recorded'];window=actual['device_conditions']['measurement']['windows'][0]
        assert window['frequency_in_window'][0]['min_mhz']==1650 and window['frequency_in_window'][0]['max_mhz']==1650
        assert actual['device_conditions']['frequency']['cards'][0]['min_mhz']==100
        assert all(j['workflow_result']['metrics']['mean_us']==30.5 for j in records.values())
        config={'username':'operator','password':'offline-only','container':'zdl','interval':15}
        server=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.handler_class(Mock(),config,jobs))
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
            for label in ['recorded','interrupted','legacy']:
                row=table.locator('tr').filter(has=page.locator('[data-result-job="'+records[label]['id']+'"]'))
                expect(row).to_contain_text('30.500');expect(row).to_contain_text('报告核对通过')
            expect(table.locator('tr').filter(has=page.locator('[data-result-job="'+actual['id']+'"]'))).to_contain_text('1 个任务请求窗口')
            expect(table.locator('tr').filter(has=page.locator('[data-result-job="'+records['interrupted']['id']+'"]'))).to_contain_text('测量窗口未确认')
            page.locator('[data-result-job="'+actual['id']+'"]').click();drawer=page.locator('#drawer-content')
            expect(drawer).to_contain_text('100–1650 MHz');expect(drawer).to_contain_text('1650–1650 MHz')
            expect(drawer).to_contain_text('边界读数不代表区间内稳定频率');expect(drawer).to_contain_text('mm512_fp16')
            link=drawer.locator('a').filter(has_text='npu-conditions.json');response=context.request.get(url+link.get_attribute('href'))
            assert response.status==200 and hashlib.sha256(response.body()).hexdigest()==actual['device_conditions']['sha256']
            archived=json.loads(response.body())['summary']['measurement'];assert archived==actual['device_conditions']['measurement']
            assert not archived['performance_qualified'] and 'token' not in json.dumps(archived)
            page.locator('#close-drawer').click();page.locator('[data-result-job="'+records['interrupted']['id']+'"]').click()
            expect(drawer).to_contain_text('测量窗口未确认');expect(drawer).to_contain_text('未采集或区间未确认')
            page.locator('#close-drawer').click()
            response=context.request.get(url+'/api/results.csv');assert response.status==200
            rows=list(csv.DictReader(io.StringIO(response.body().decode('utf-8-sig'))));assert len(rows)==3
            row=next(row for row in rows if row['job_id']==actual['id'])
            assert json.loads(row['metrics'])['mean_us']==30.5 and json.loads(row['device_conditions'])['measurement']==archived
            other=browser.new_context(extra_http_headers={'Authorization':'Bearer '+alice})
            assert other.request.get(url+'/api/results').json()['total']==0
            assert other.request.get(url+'/api/jobs/'+actual['id']+'/artifact?name=npu-conditions.json').status in {403,404}
            other.close();page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=base/'mobile.png',full_page=True);context.close();browser.close()
        assert not errors and not outside and not writes,(errors,outside,writes)
        report={'passed':True,'scope':'Packaged UI and actual HTTP/ACL/archive, explicitly synthetic Node device windows; no actual GPU timing',
            'checks':['completed and interrupted window labels separate from workload success','original performance mean unchanged',
                'reservation low clock separate from window-only frequency snapshots','Node interval and noncontinuous observation disclaimer',
                'archive normalized evidence SHA and no token','CSV preserves exact window metadata','legacy workflow remains compatible',
                'other owner cannot read rows or archive','mobile width','GET-only with no model or external request'],
            'release':release['release_id'],'job_ids':{k:v['id'] for k,v in records.items()},'new_npu_launches':0,'new_model_calls':0,'browser_errors':errors}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/measurement-browser.json',report);print(json.dumps(report,ensure_ascii=False))
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(3)
        fixture.doCleanups();server_module.ROOT=old_root


if __name__=='__main__':main()

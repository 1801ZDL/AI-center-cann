"""A real old queued item remains visible after >100 later cancelled submissions.

The temporary identity has no node mapping. Every request remains unexecuted;
the fixture uses normal authenticated submission/cancellation and private ACLs.
"""
import json
import os
from pathlib import Path
import sys
import uuid
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import JobStore,read_json,atomic_json,now
from lab_client import Client

config=read_json(ROOT/'runtime/config.json');jobs=JobStore(ROOT/'runtime',config)
user='waiting_ui_'+uuid.uuid4().hex[:8];home=ROOT/'runtime/personas'/user/'workspace';home.mkdir(mode=0o700,parents=True)
password=jobs.access.create_user(user,home,environments=['worker-python'])
token=jobs.access.issue_token(user,{'jobs:read','jobs:submit','jobs:cancel'})
profile=home.parent/'client.json';atomic_json(profile,{'controller':'http://127.0.0.1:'+str(config['port']),'token':token})
client=Client.from_profile(profile);url=client.url;created=[];errors=[]

def submit(index):
    reply=client.request('/api/jobs',{'template':'cpu_demo','environment':'worker-python',
      'parameters':{'seconds':1},'idempotency_key':user+'-'+str(index),'name':'等待可见性 '+str(index)})
    created.append(reply['job']['id']);return created[-1]

try:
    old=submit(0)
    for index in range(1,102):
        job=submit(index);client.request('/api/jobs/'+job+'/cancel',{})
    summary=client.request('/api/jobs')
    assert len(summary['jobs'])==101 and old in {job['id'] for job in summary['jobs']}
    assert summary['counts']['queued']==1 and summary['counts']['cancelled']==101
    assert all(not client.request('/api/jobs/'+job)['attempts'] for job in created)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=os.environ.get('ASCEND_LAB_BROWSER_PATH'),args=['--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1440,'height':1080},http_credentials={'username':user,'password':password})
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
        page.goto(url+'/#jobs',wait_until='networkidle')
        page.wait_for_function("document.getElementById('job-form').dataset.initialized==='true'")
        expect(page.locator('#job-policy')).to_contain_text(str(summary['policy']['aging_seconds'])+' 秒')
        row=page.locator('#job-body tr').filter(has=page.locator('[data-job-id="'+old+'"]'))
        expect(row).to_contain_text('排队中');expect(row).to_contain_text('已等待')
        row.scroll_into_view_if_needed();page.screenshot(path=ROOT/'evidence/waiting-desktop.png')
        page.set_viewport_size({'width':390,'height':844});row.scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        expect(row).to_contain_text('已等待');page.screenshot(path=ROOT/'evidence/waiting-mobile.png')
        assert not errors,errors
        context.close();browser.close()
    report={'passed':True,'verified_at':now(),'old_queued_job':old,'later_cancelled_jobs':101,
      'visible_job_count':101,'job_ids':created,'actual_attempts_created':0,'browser_errors':errors,
      'checks':['Normal API retains an old queued item beyond 100 newer history entries',
                'Personal ACL excludes other users and no execution node is assigned',
                'Desktop and 390px mobile render the queued row, elapsed wait and actual aging threshold'],
      'model_calls':0,'npu_kernel_launches':0}
    atomic_json(ROOT/'evidence/waiting-browser-validation.json',report)
    print(json.dumps({key:report[key] for key in ['passed','old_queued_job','later_cancelled_jobs','visible_job_count','actual_attempts_created','browser_errors']},ensure_ascii=False))
finally:
    for job_id in created:
        row=client.request('/api/jobs/'+job_id)['job']
        if row['state']=='queued':client.request('/api/jobs/'+job_id+'/cancel',{})
    jobs.access.disable_user(user,True);jobs.access.close()

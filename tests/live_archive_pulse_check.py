"""Pause only this job's exporter for >30 s; keep the original lease and finish."""
import hashlib
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import ACTIVE,TERMINAL,read_json,atomic_json,now
from lab_client import Client

admin=Client.from_profile(ROOT/'runtime/config.json');client=Client.from_profile(ROOT/'runtime/personas/demo_bob/client.json')
assert not [job for job in admin.request('/api/jobs')['jobs'] if job['state'] in ACTIVE|{'queued'}]
base=ROOT/'runtime'/('archive-pulse-'+uuid.uuid4().hex[:8]);source=base/'source';source.mkdir(mode=0o700,parents=True)
(source/'make.py').write_text('''import json,os
from pathlib import Path
p=Path('/work/results/payload.bin')
with p.open('wb') as f:
 for _ in range(60):f.write(b'x'*1024**2)
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,'artifacts':['results/payload.bin']}))
''')
bundle=client.upload_input(source);job_id=None;events=None;helper=None;paused=False;states=[]
def inspect(identifier):
    result=subprocess.run(['docker','inspect',identifier],capture_output=True,text=True,timeout=10)
    return json.loads(result.stdout)[0] if result.returncode==0 else None
def own_exporter(info,attempt):
    return (info and info['Config'].get('Labels',{}).get('io.ascend-lab.attempt')==attempt
            and info['Config'].get('Cmd',[])[-1:]==['/payload/artifact_export.py']
            and any(m['Destination']=='/work' and not m['RW'] and '/'+attempt+'/' in m['Source'] for m in info['Mounts']))
try:
    job=client.request('/api/jobs',{'template':'build_v1','environment':'worker-python','name':'归档暂停与独立心跳验收',
      'parameters':{'input_id':bundle['id'],'argv':['python','make.py'],'timeout_seconds':90,'lock':{'python':'3.10.12'}},
      'idempotency_key':base.name})['job'];job_id=job['id']
    deadline=time.monotonic()+30
    while not job.get('attempt_id') and time.monotonic()<deadline:
        time.sleep(.1);job=client.request('/api/jobs/'+job_id)['job']
    assert job.get('attempt_id'),job['state'];attempt=job['attempt_id']
    events=subprocess.Popen(['docker','events','--filter','label=io.ascend-lab.attempt='+attempt,
       '--filter','event=start','--format','{{json .}}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        if not select.select([events.stdout],[],[],.5)[0]:continue
        line=events.stdout.readline();assert line,'Owned event observer ended unexpectedly'
        event=json.loads(line);info=inspect(event.get('id') or event.get('Actor',{}).get('ID'))
        if own_exporter(info,attempt) and info['State']['Running']:
            helper=info['Id']
            result=subprocess.run(['docker','pause',helper],capture_output=True,text=True,timeout=10)
            if result.returncode==0:paused=True;break
    assert paused,'Exporter finished before fault injection; do not reinterpret this as a heartbeat acceptance'
    atomic_json(base/'paused-helper.json',{'job_id':job_id,'attempt_id':attempt,'helper_id':helper,'at':now()})
    started=time.monotonic()
    while time.monotonic()-started<35:
        row=client.request('/api/jobs/'+job_id)['job']
        assert row['state'] in {'reserved','running'},(row['state'],row['reason'])
        with sqlite3.connect('file:'+str(ROOT/'runtime/jobs.sqlite3')+'?mode=ro',uri=True) as db:
            data=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(job_id,)).fetchone()[0])
        age=time.time()-data['_worker_seen'];assert 0<=age<15,age
        states.append({'at':now(),'state':row['state'],'receipt_age_seconds':age})
        assert own_exporter(inspect(helper),attempt)
        time.sleep(1)
    pause_duration=time.monotonic()-started
    assert inspect(helper)['State']['Paused']
    subprocess.run(['docker','unpause',helper],capture_output=True,check=True,timeout=10);paused=False
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state'] in TERMINAL|{'needs_attention'}:break
        time.sleep(.3)
    assert job['state']=='succeeded',(job['state'],job['reason'])
    detail=client.request('/api/jobs/'+job_id);assert len(detail['attempts'])==1 and detail['job']['attempt_id']==attempt
    manifest=client.request('/api/jobs/'+job_id+'/artifact?name=artifact-hashes.json')
    entry=next(item for item in manifest['files'] if item['name']=='results/payload.bin')
    expected=hashlib.sha256()
    for _ in range(60):expected.update(b'x'*1024**2)
    assert entry['size']==60*1024**2 and entry['sha256']==expected.hexdigest()
    assert inspect(helper) is None
    report={'passed':True,'verified_at':now(),'job_id':job_id,'attempt_id':attempt,
      'paused_exporter_seconds':pause_duration,'maximum_receipt_age_seconds':max(item['receipt_age_seconds'] for item in states),
      'states_during_pause':states,'artifact_bytes':entry['size'],'artifact_sha256':entry['sha256'],
      'execution_attempts':1,'npu_kernel_launches':0,'scope':'Injected pause of one own labeled read-only exporter; no other containers paused'}
    atomic_json(ROOT/'evidence/archive-pulse-validation.json',report)
    print(json.dumps({key:value for key,value in report.items() if key!='states_during_pause'},ensure_ascii=False,indent=2))
finally:
    if paused:
        info=inspect(helper)
        if own_exporter(info,attempt) and info['State'].get('Paused'):
            subprocess.run(['docker','unpause',helper],capture_output=True,check=True,timeout=10)
    if events:
        events.terminate()
        try:events.wait(timeout=5)
        except subprocess.TimeoutExpired:events.kill();events.wait(timeout=5)
    if job_id:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state'] in {'queued','reserved','running','cancelling'}:client.request('/api/jobs/'+job_id+'/cancel',{})

"""Reconcile only the exact stopped v036 first fixture; never rerun its tasks."""
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import read_json,atomic_json,now
from jobs import identity_alive
from deployment import digest

parent='job-e827f677a6e3';prefix='ascend-lab-install-a907c370'
base=ROOT/'runtime/jobs'/parent/'tmp/installation'
assert read_json(ROOT/'runtime/jobs'/parent/'result.json')['status']=='failed'
assert not identity_alive(read_json(ROOT/'runtime/jobs'/parent/'command-process.json'))
attempts=[]
for path in (base/'node-control').glob('*/attempts/*/control/grant.json'):
    grant=read_json(path);state=read_json(path.with_name('state.json'))
    assert state['stage']=='cleaned' and state['controller_acknowledged'] and not state.get('artifact_gc_requested')
    attempts.append(grant['attempt_id'])
assert len(attempts)==16
def docker(*args):
    return subprocess.run(['docker',*args],check=True,capture_output=True,text=True,timeout=25).stdout
for attempt in attempts:assert not docker('ps','-aq','--filter','label=io.ascend-lab.attempt='+attempt).strip()
removed=[]
for name in [prefix+'-node',prefix+'-controller']:
    info=json.loads(docker('inspect',name))[0]
    assert info['Config']['Labels'].get('io.ascend-lab.installproof')==prefix
    assert not info['State']['Running'] and info['State']['Pid']==0
    docker('rm',name);removed.append(name)
volumes=docker('volume','ls','-q','--filter','label=io.ascend-lab.installproof='+prefix).splitlines()
for name in volumes:
    info=json.loads(docker('volume','inspect',name))[0];assert info['Labels'].get('io.ascend-lab.installproof')==prefix
    docker('volume','rm',name);removed.append(name)
loops={}
for path in base.glob('*/loop-state.json'):
    value=read_json(path);assert value['released'];loops[path.parent.name]=value['released']
assert len(loops)==5
for name in docker('ps','-aq','--filter','label=io.ascend-lab.diskproof').splitlines():
    info=json.loads(docker('inspect',name))[0];label=info['Config']['Labels'].get('io.ascend-lab.diskproof','')
    if not label.startswith(prefix+'-') and label!=prefix:continue
    assert not info['State']['Running'] and info['State']['Pid']==0
    assert any(m['Type']=='bind' and str(Path(m['Source']).parent)==str(base) and m['Destination']=='/fixture' for m in info['Mounts'])
    docker('rm',name);removed.append(info['Name'].lstrip('/'))
for name in docker('network','ls','-q','--filter','label=io.ascend-lab.installproof='+prefix).splitlines():
    info=json.loads(docker('network','inspect',name))[0];assert info['Labels'].get('io.ascend-lab.installproof')==prefix and not info['Containers']
    docker('network','rm',name);removed.append(info['Name'])
import sqlite3
with sqlite3.connect('file:'+str(base/'controller-state/jobs.sqlite3')+'?mode=ro',uri=True) as db:
    jobs=[json.loads(r[0]) for r in db.execute('SELECT data FROM jobs')]
    timed=[{k:j.get(k) for k in ['id','state','timeout_seconds','started_at','finished_at']} for j in jobs if j['state']=='timed_out']
assert len(timed)==1 and timed[0]['timeout_seconds']==90
report={'recorded_at':now(),'parent_job':parent,'parent_state':'failed','root_gate_passed_before_failure':True,
    'failure':'Native cold workflow reached its declared90-second deadline; HTTPS observation timed out and a Docker exec timeout interrupted fixture finally cleanup.',
    'native_timeouts':timed,'cleaned_acknowledged_attempts':len(attempts),'loops_released':loops,'owned_objects_removed':removed,
    'no_tasks_reexecuted':True,'stderr_sha256':digest(ROOT/'runtime/jobs'/parent/'stderr.log'),
    'scope':'Original failures retained; actual native task timed_out, whole Root acceptance not passed; no claim about host load root cause'}
atomic_json(ROOT/'evidence/deadline-first-native-timeout-cleanup-failure-v036.json',report)
result=subprocess.run([str(ROOT/'labctl'),'start'],check=True,capture_output=True,text=True,timeout=25)
print(json.dumps({'reconciled':True,'parent_job':parent,'terminal_attempts':len(attempts),'loops_released':len(loops),'objects_removed':len(removed),'main_start_returncode':result.returncode}))

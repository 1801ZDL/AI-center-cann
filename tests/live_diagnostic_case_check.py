"""Actual prior compiler failure through separately installed read-only clients."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now,read_json

os.umask(0o077)
base=ROOT/'runtime/diagnostics-20261030';base.mkdir(mode=0o700,exist_ok=True)
state=read_json(ROOT/'runtime/full-build-20261030/control.json')
owner=Client.from_profile(ROOT/'runtime/personas'/state['user']/'client.json')
identifier='job-87938596e36d'
assert owner.request('/api/jobs/'+identifier)['job']['state']=='failed'
module_sha=hashlib.sha256((ROOT/'diagnostic_case.py').read_bytes()).hexdigest()
client=base/('client-'+module_sha[:12])
if not client.exists():subprocess.run([str(ROOT/'install-client'),'--prefix',str(client)],check=True,capture_output=True)
assert read_json(client/'manifest.json')['version']=='0.32-client'
assert read_json(client/'manifest.json')['sha256']['diagnostic_case.py']==module_sha
profile=base/'owner.json'
if not profile.exists():atomic_json(profile,{'controller':owner.url,'token':owner.config['token']})
result=subprocess.run([str(client/'labctl'),'--profile',str(profile),'diagnose',identifier],cwd=base,
                      check=True,capture_output=True,text=True,timeout=60)
case=json.loads(result.stdout)
assert case['job']['id']==identifier and case['job']['state']=='failed' and case['terminal']
by_id={item['id']:item for item in case['evidence']}
assert 'cannot find -lz' in by_id['stdout_tail']['text']
assert json.loads(by_id['build_provenance']['text'])['passed'] is False
assert all(item['preview_sha256']==hashlib.sha256(item['text'].encode()).hexdigest() for item in case['evidence'])
assert owner.config['token'] not in result.stdout
atomic_json(base/'compiler-link-case.json',case)
configured=subprocess.run([str(client/'labctl'),'--profile',str(profile),'diagnose','job-409a4afc0775'],cwd=base,
                         check=True,capture_output=True,text=True,timeout=60)
configured=json.loads(configured.stdout)
assert configured['job']['state']=='succeeded'
raw=json.loads(next(item for item in configured['evidence'] if item['id']=='workload_result')['text'])
assert raw['configure_only'] and raw['full_build_accepted'] is False

messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
          {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
          {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'submit_job','arguments':{}}},
          {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'job_id':state['jobs'][-1]['job_id']}}}]
def mcp(profile):
    process=subprocess.run(['/usr/bin/python3',str(client/'diagnostic_case.py'),'--profile',str(profile),'--job-id',identifier],
        input=''.join(json.dumps(message)+'\n' for message in messages),cwd=base,
        capture_output=True,text=True,check=True,timeout=60)
    return [json.loads(line)['result'] for line in process.stdout.splitlines()]
replies=mcp(profile)
assert len(replies)==4 and [tool['name'] for tool in replies[0]['tools']]==['get_diagnostic_case']
assert replies[0]['tools'][0]['annotations']['readOnlyHint']
assert not replies[1]['isError'] and replies[2]['isError'] and replies[3]['isError']
assert json.loads(replies[1]['content'][0]['text'])['job']['id']==identifier
bob=ROOT/'runtime/personas/demo_bob/client.json'
assert mcp(bob)[1]['isError']
current=owner.request('/api/jobs/'+state['jobs'][-1]['job_id'])
assert len(current['attempts'])==1
report={'passed':True,'verified_at':now(),'schema':'ascend-lab-diagnostic-case/v1','job_id':identifier,
        'actual_compiler_link_failure_preserved':True,'case_sha256':case['case_sha256'],
        'case_bytes':len(result.stdout.encode()),'evidence_ids':list(by_id),'installed_client_version':'0.32-client',
        'installed_diagnostic_module_sha256':module_sha,'configure_success_not_misrepresented_as_full_build':True,
        'stdio_read_only_tool':True,'submission_and_job_override_rejected':True,'foreign_identity_rejected':True,
        'client_has_no_controller_modules':not (client/'server.py').exists(),
        'ongoing_compiler_job_single_attempt':True,'model_calls':0,'npu_kernel_launches':0,
        'scope':'Real existing failed-build evidence collection through authenticated CLI/MCP; no model diagnosis, repair or device-accuracy claim'}
atomic_json(ROOT/'evidence/diagnostic-case-live-validation.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

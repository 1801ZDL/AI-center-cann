"""Actual four-core historical simulator reports in installed UID-quota roles."""
import copy
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess

from environment_contract import lock_for
from lab_client import Client,ClientError,ClientHTTPError,atomic_json
from simulation_data import INPUT,DRIVER,REPORT,sha,strict_json
from simulation_diagnostic import read_case


def run(ctx):
    base=ctx['base'];reference=json.loads(Path(ctx['options'].simulation_case).read_text());assert len(reference['datasets'])==2
    lock=lock_for(ctx['configuration']['environment_manifest']);installed=base/'simulation-client'
    subprocess.run([str(ctx['ROOT']/'install-client'),'--prefix',str(installed)],check=True,capture_output=True,text=True,timeout=30)
    public='/opt/ascend-lab-simulation-client'
    ctx['docker'](['exec',ctx['controller'],'/usr/bin/python3','-B','/opt/ascend-lab-controller/current/install-client','--prefix',public])
    tasks=[];budgets=[];sessions={}
    def submit(label,source,person,expected,expected_cores):
        input_id=person['client'].upload_input(source)['id']
        request={'name':'Offline simulator '+label,'template':'build_v1','environment':'worker-python','parameters':{
            'input_id':input_id,'argv':['python','-I','-B',DRIVER],'lock':copy.deepcopy(lock),
            'resources':{'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0},'timeout_seconds':120,'input_mode':'readonly',
            'workspace':{'min_mib':256,'min_inodes':128,'durable':True},'artifacts':{'file_mib':32,'total_mib':128}}}
        path=base/('simulation-'+label+'-request.json');atomic_json(path,request)
        command=['/usr/bin/python3',str(installed/'mcp_bridge.py'),'--profile',person['budget_profile'],'--session-dir',str(base/('simulation-session-'+label)),
            '--environment','worker-python','--workflow',str(path)]
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'submit_job','arguments':{'template':'build_v1','request_key':'original'}}}]
        def conversation():
            reply=subprocess.run(command,input=''.join(json.dumps(v)+'\n' for v in messages),capture_output=True,text=True,timeout=60)
            assert reply.returncode==0,'Inspect original private MCP handle; no replacement job'
            value=json.loads(reply.stdout.splitlines()[-1])['result'];assert not value.get('isError'),value
            return json.loads(value['content'][0]['text'])
        first=conversation();identifier=first['job']['id'];sessions[label]=conversation
        task={'label':label,'job_id':identifier,'input_id':input_id,'owner':person['owner'],'uid':person['uid'],
            'personal_uid':person['personal_uid'],'personal_profile':person['profile'],'budget_id':person['budget_id']};tasks.append(task)
        atomic_json(base/'simulation-tasks-in-progress.json',{'tasks':tasks,'budgets':budgets})
        job=ctx['wait'](identifier,seconds=160,headers=person['headers']);assert job['state']==expected,(identifier,job['state'],job.get('result'))
        detail=person['client'].request('/api/jobs/'+identifier);assert len(detail['attempts'])==1
        assert all(job['result'][k] is True for k in ['termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed'])
        env=person['client'].request('/api/jobs/'+identifier+'/artifact?name=environment.json')
        assert env['task_uid']==person['uid'] and env['node_daemon_uid']==0 and env['device_nodes']==[] and env['network_mode']=='none'
        assert env['workspace']['user_quota'] and env['read_only_root'] and env['memory_limit']==512*1024**2
        task.update(attempt_id=job['attempt_id'],state=job['state'],attempts=1)
        replay=conversation();assert replay['reused'] and replay['job']['id']==identifier
        if expected=='failed':
            assert REPORT not in {v['name'] for v in detail['artifacts']}
            try:read_case(person['client'],identifier)
            except ClientError:pass
            else:raise AssertionError('Invalid simulator CSV produced a diagnostic report')
            return task
        case=read_case(person['client'],identifier);analysis=case['analysis']
        assert analysis['capture']['capture_state']=='abnormal' and analysis['capture']['logged_child_signals']==[11]
        assert analysis['capture']['logged_parse_finished'] and not analysis['capture']['capture_completeness_certified']
        assert analysis['all_csv_trace_counts_match'] and len(analysis['cores'])==expected_cores==4
        assert case['workflow_binding']['driver_known_to_client'] and case['workflow_binding']['protected_environment_matched']
        baseline=next(v for v in reference['datasets'] if v['id']==label)['historical_reference']
        for core in analysis['cores']:
            old=baseline['cores'][core['id']];record=core['analysis']
            assert record['events']==old['events'] and abs(record['span_us']-old['span_us'])<1e-12
            assert core['trace']['sha256']==old['trace_sha256'] and record['pipe_mapping']=='instruction_and_pc_from_csv'
            for name,wanted in old['categories'].items():
                if name in record['instruction_groups']:
                    actual=record['instruction_groups'][name];assert actual['events']==wanted['count'] and abs(actual['union_us']-wanted['union_us'])<1e-12
        csv_raw=ctx['api']('/api/jobs/'+identifier+'/artifact?name=results%2Fsimulation-analysis.csv',raw=True,extra_headers=person['headers'])[1]
        rows=list(csv.DictReader(io.StringIO(csv_raw.decode())));assert rows and {r['capture_state'] for r in rows}=={'abnormal'} and {r['capture_completeness_certified'] for r in rows}=={'False'}
        messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_diagnostic_case','arguments':{'path':'/etc/passwd'}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'submit_job','arguments':{}}}]
        response=ctx['docker'](['exec','-i','--user',str(person['personal_uid'])+':'+str(person['personal_uid']),ctx['controller'],
            '/usr/bin/python3','-B',public+'/simulation_diagnostic.py','mcp','--profile',person['home']+'/client.json','--job-id',identifier],
            input=''.join(json.dumps(v)+'\n' for v in messages),timeout=160)
        values=[json.loads(v)['result'] for v in response.stdout.splitlines()]
        assert len(values[0]['tools'])==1 and not values[1]['isError'] and values[2]['isError'] and values[3]['isError']
        assert json.loads(values[1]['content'][0]['text'])['analysis']==analysis
        cli=ctx['docker'](['exec','--user',str(person['personal_uid'])+':'+str(person['personal_uid']),ctx['controller'],
            '/usr/bin/python3','-B',public+'/labctl','--profile',person['home']+'/client.json','simulation-case',identifier],timeout=160)
        assert json.loads(cli.stdout)['analysis']==analysis
        atomic_json(base/('simulation-'+label+'-case.json'),case)
        task.update(cores=expected_cores,events=sum(c['analysis']['events'] for c in analysis['cores']),
            trace_sha256={c['id']:c['trace']['sha256'] for c in analysis['cores']},analysis=analysis,
            actual_personal_cli_and_stdio=True)
        return task
    persons=[]
    for i,(owner,credential) in enumerate([('proof_user',ctx['personal']),('proof_peer',ctx['peer'])]):
        client=Client({'controller':ctx['controller_url'],'token':credential['token']});profile=base/f'simulation-personal-client-{i}.json';atomic_json(profile,client.config)
        amount=2 if i==0 else 1
        budget=client.request('/api/experiment-budgets',{'name':'Historical simulator '+owner,'idempotency_key':'simulation-budget-'+owner,
            'limits':{'jobs':amount,'cpu_seconds':120*amount,'npu_seconds':0},'templates':['build_v1'],'lifetime_seconds':900})['budget']['id']
        delegated=client.request('/api/experiment-budgets/'+budget+'/token',{});budget_profile=base/f'simulation-budget-client-{i}.json'
        atomic_json(budget_profile,{'controller':ctx['controller_url'],'token':delegated['token']})
        uid=23642+i;home='/home/simulation-client-'+str(i)
        code='import os,sys;from pathlib import Path;p=Path(sys.argv[1]);uid=int(sys.argv[2]);p.mkdir(mode=0o700);os.chown(p,uid,uid);f=p/"client.json";f.write_bytes(sys.stdin.buffer.read());f.chmod(0o600);os.chown(f,uid,uid)'
        ctx['docker'](['exec','-i',ctx['controller'],'/usr/bin/python3','-I','-c',code,home,str(uid)],input=json.dumps(client.config))
        persons.append({'owner':owner,'client':client,'profile':str(profile),'budget_profile':str(budget_profile),'budget_id':budget,
            'headers':{'Authorization':'Bearer '+credential['token']},'uid':23003+i,'personal_uid':uid,'home':home})
        budgets.append({'owner':owner,'budget_id':budget,'profile':str(profile),'budget_profile':str(budget_profile),'expected_jobs':amount})
    for i,dataset in enumerate(reference['datasets']):
        source=Path(dataset['path']);assert source.is_relative_to(ctx['ROOT']/'runtime')
        target=base/('simulation-source-'+dataset['id']);shutil.copytree(source,target);(target/DRIVER).write_bytes((ctx['ROOT']/DRIVER).read_bytes())
        # Isolated Python must not import an input-controlled stdlib shadow.
        (target/'json.py').write_text("raise RuntimeError('Input-controlled json shadow must never execute')\n")
        task=submit(dataset['id'],target,persons[i],'succeeded',4)
        other=persons[1-i]['client']
        for path in ['/api/jobs/'+task['job_id'],'/api/inputs/'+task['input_id']]:
            try:other.request(path)
            except ClientHTTPError as error:assert error.status==404
            else:raise AssertionError('Private simulator evidence crossed owners')
        denied=ctx['docker'](['exec','--user',str(persons[1-i]['personal_uid'])+':'+str(persons[1-i]['personal_uid']),ctx['controller'],
            '/usr/bin/python3','-I','-c','import sys;from pathlib import Path;Path(sys.argv[1]).read_bytes()',persons[i]['home']+'/client.json'],check=False)
        assert denied.returncode!=0
    invalid=base/'simulation-invalid-source';invalid.mkdir(mode=0o700);first=Path(reference['datasets'][0]['path'])
    value=strict_json((first/INPUT).read_bytes());value['cores']=value['cores'][:1]
    for name in ['capture.log','core0.trace.json','core0.instructions.csv']:shutil.copyfile(first/name,invalid/name)
    csv_path=invalid/'core0.instructions.csv';lines=csv_path.read_text().splitlines();fields=next(csv.reader([lines[1]]));fields[5]='NaN'
    out=io.StringIO();writer=csv.writer(out);writer.writerow(fields);lines[1]=out.getvalue().strip();csv_path.write_text('\n'.join(lines)+'\n')
    value['cores'][0]['instructions']['sha256']=sha(csv_path.read_bytes());(invalid/INPUT).write_text(json.dumps(value)+'\n');(invalid/DRIVER).write_bytes((ctx['ROOT']/DRIVER).read_bytes())
    submit('invalid-csv',invalid,persons[0],'failed',0)
    for b in budgets:
        charged=Client.from_profile(b['profile']).request('/api/experiment-budgets/'+b['budget_id'])['budget']['charged']
        assert charged=={'jobs':b['expected_jobs'],'cpu_seconds':b['expected_jobs']*120,'npu_seconds':0};b['charged']=charged
    return {'passed':True,'tasks':tasks,'budgets':budgets,'historical_cores':8,'historical_capture_abnormal_preserved':True,
        'task_uids':[23003,23004],'personal_uids':[23642,23643],'cross_owner_and_personal_home_denied':True,
        'validly_sealed_invalid_csv_failed':True,'input_stdlib_shadow_not_imported':True,'npu_kernel_launches':0,'model_invocations':0,
        'scope':'Installed same-host UID-separated CPU analysis of full historical simulator traces and paired instruction CSVs. No new collection, capture completeness, device timing, root cause, repair, actual host users, PID1 or two physical hosts.'}


def after_restore(ctx,proof):
    for b in proof['budgets']:
        try:Client.from_profile(b['budget_profile']).request('/api/me')
        except ClientHTTPError as error:assert error.status==401
        else:raise AssertionError('Old simulator spending credential survived restore')
        budget=Client.from_profile(b['profile']).request('/api/experiment-budgets/'+b['budget_id'])['budget']
        assert budget['state']=='restored_frozen' and budget['charged']==b['charged']
    for task in proof['tasks']:
        client=Client.from_profile(task['personal_profile']);detail=client.request('/api/jobs/'+task['job_id'])
        assert detail['job']['state']==task['state'] and detail['job']['attempt_id']==task['attempt_id'] and len(detail['attempts'])==1
        if task['state']=='succeeded':assert read_case(client,task['job_id'])['analysis']==task['analysis']
    return {'passed':True,'same_attempts_and_archived_traces':True,'abnormal_capture_state_preserved':True,'old_spending_credentials_revoked':True,'new_analysis_tasks':0}

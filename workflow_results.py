"""Controller-side result contracts, evidence references and ACL-filtered result rows."""
import csv
import hashlib
import io
import json
import statistics
import math

from workflow_spec import WORKFLOWS
from workflow_runner import validate_result
from environment_probe import compare,access_confirmed


def load(path,limit=2*1024**2):
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('Result evidence path or size is invalid')
    def invalid(_):raise ValueError('Non-finite JSON value')
    return json.loads(path.read_text(),parse_constant=invalid)


class ResultStore:
    def __init__(self,jobs):
        self.jobs=jobs
        with jobs.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workflow_results(job_id TEXT PRIMARY KEY,record TEXT NOT NULL)')

    def unstarted_dependency(self,job,result,db):
        from jobs import now,atomic_json
        row={'schema':'ascend-lab-record/v1','job_id':job['id'],'owner':job['owner'],'project':job['project'],
            'kind':WORKFLOWS[job['template']],'name':job['name'],'passed':False,'state':'cancelled','recorded_at':now(),
            'command_executed':False,'environment_locked':None,'failure_stage':'dependency','error':result['reason'],
            'input_id':job['input']['id'],'input_manifest_sha256':job['input']['manifest_sha256'],
            'depends_on':job['depends_on'],'evidence':{'result':'result.json'},'metrics':{},
            'assertion_source':'Controller dependency outcome; no workload or numerical test was executed'}
        atomic_json(self.jobs.directory(job['id'])/'validated-result.json',row)
        db.execute('INSERT OR REPLACE INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row,ensure_ascii=False)))
        return row

    def record(self,job,execution,db):
        from jobs import atomic_json,now
        directory=self.jobs.directory(job['id']);kind=WORKFLOWS[job['template']]
        row={'schema':'ascend-lab-record/v1','job_id':job['id'],'owner':job['owner'],'project':job['project'],
             'kind':kind,'name':job['name'],'passed':False,'state':execution['status'],'recorded_at':now(),
             'input_id':job['input']['id'],'input_manifest_sha256':job['input']['manifest_sha256'],
             'evidence':{'result':job['parameters']['result_file'],'environment':'results/_lab/environment.json'},
             'metrics':{},'assertion_source':'workload-produced; numerical truth is not independently certified'}
        from npu_conditions import fallback
        row['device_conditions']=job.get('device_conditions',fallback(job))
        if job.get('depends_on'):
            row['depends_on']=job['depends_on'];row['dependency_provenance_sha256']=job.get('dependency_state',{}).get('provenance_sha256')
            if job.get('input_provenance'):row['evidence']['input_provenance']='dependency-inputs.json'
        try:
            protected=load(directory/'results/_lab/environment.json',256*1024)
            if protected.get('schema')!='ascend-lab-environment/v1':raise ValueError('Protected environment receipt schema differs')
            row['environment_locked']=protected.get('locked') is True
            row['command_executed']=protected.get('workload_started') is True
            if job['resources']['npu']:
                row['device_access_confirmed']=access_confirmed(protected,job.get('allocation',{}).get('npu_ids',[]))
            row['environment_sha256']=hashlib.sha256((directory/'results/_lab/environment.json').read_bytes()).hexdigest()
            if execution['status']=='succeeded':
                if job['resources']['npu'] and not row['device_access_confirmed']:
                    raise ValueError('Protected task-identity NPU access evidence is missing or invalid')
                if (not row['environment_locked'] or not row['command_executed'] or protected.get('probe_exit_code')!=0
                    or protected.get('workload_exit_code')!=0 or compare(job['parameters']['lock'],protected.get('observed',{}))):
                    raise ValueError('Protected bootstrap receipt does not confirm environment and exit')
                environment=load(directory/'environment.json')
                if environment.get('image')!=protected.get('observed',{}).get('image_sha256'):raise ValueError('Runtime image and environment receipt differ')
                if job.get('runtime_files') and environment.get('runtime_files')!=job['runtime_files']:raise ValueError('Runtime source version evidence differs')
                actual=load(directory/job['parameters']['result_file'])
                mapping_path=directory/'results/_lab/path-map.json';path_map={}
                if mapping_path.exists():
                    mapping=load(mapping_path,16*1024**2)
                    if mapping.get('schema')!='ascend-lab-path-map/v1':raise ValueError('Unknown archive path mapping')
                    path_map={item['original']:item['archived'] for item in mapping['mappings']}
                if isinstance(actual,dict) and isinstance(actual.get('artifacts'),list):
                    actual={**actual,'artifacts':[path_map.get(name,name) for name in actual['artifacts']]}
                actual=validate_result(kind,actual,directory)
                if kind=='performance':
                    samples=actual['samples_us'];ordered=sorted(samples)
                    row['metrics']={'samples':len(samples),'mean_us':statistics.mean(samples),'median_us':statistics.median(samples),
                                    'p95_us':ordered[max(0,math.ceil(.95*len(ordered))-1)],
                                    'min_us':min(samples),'max_us':max(samples)}
                elif kind=='accuracy':
                    row['metrics']={'checks':len(actual['checks']),'max_abs_error':max(x['max_abs_error'] for x in actual['checks'])}
                else:
                    row['metrics']={'artifacts':len(actual['artifacts']),
                                    'artifact_bytes':sum((directory/name).stat().st_size for name in actual['artifacts'])}
                if path_map:row['evidence']['path_map']='results/_lab/path-map.json'
                row['passed']=True
            else:
                row['error']=execution.get('reason','Task-identity NPU access failed') if protected.get('failure_stage')=='npu_device_access' else 'Environment lock rejected' if not row['environment_locked'] else execution.get('reason','Execution failed')
                if execution['status']=='failed' and not execution.get('failure_stage') and row['environment_locked']:
                    summary_path=directory/'results/workflow-summary.json'
                    if summary_path.exists():
                        summary=load(summary_path)
                        if summary.get('exit_code')==0 and not (directory/job['parameters']['result_file']).exists():
                            row['error']='任务退出为 0，但未生成结构化结果报告'
                        elif isinstance(summary.get('error'),str):row['error']=summary['error'][:500]
        except (OSError,ValueError,TypeError,KeyError) as error:
            row['error']=str(error)[:500]
            if execution['status']=='succeeded':
                execution.update(execution_status='succeeded',status='failed',failure_stage='result_contract',
                                 reason='Structured result or protected environment evidence is invalid')
        row['state']=execution['status']
        if execution.get('failure_stage'):row['failure_stage']=execution['failure_stage']
        atomic_json(directory/'validated-result.json',row)
        db.execute('INSERT OR REPLACE INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row,ensure_ascii=False,allow_nan=False)))
        return row

    def rows(self,principal,kind=None,limit=100,offset=0):
        from jobs import JobError
        principal.require('jobs:read')
        if kind is not None and kind not in set(WORKFLOWS.values()):raise JobError('Unknown workflow result kind')
        if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:raise JobError('Invalid result page')
        visible={job['id']:job for job in self.jobs.access.readable_jobs(principal,self.jobs.all())}
        with self.jobs.connect() as db:records=[json.loads(row[0]) for row in db.execute('SELECT record FROM workflow_results ORDER BY rowid DESC')]
        records=[row for row in records if row['job_id'] in visible and (kind is None or row['kind']==kind)]
        from npu_conditions import fallback
        for row in records:
            row['archive_state']=visible[row['job_id']].get('archive_status',{}).get('state','available')
            row.setdefault('device_conditions',fallback(visible[row['job_id']]))
        return {'rows':records[offset:offset+limit],'total':len(records),'limit':limit,'offset':offset}

    def csv(self,principal,kind=None):
        from jobs import JobError
        # Current controller is bounded to 1000 jobs; export only visible rows.
        rows=[];offset=0
        while True:
            page=self.rows(principal,kind,200,offset);rows.extend(page['rows']);offset+=len(page['rows'])
            if offset>=page['total']:break
        out=io.StringIO(newline='');writer=csv.writer(out)
        keys=['job_id','owner','project','kind','name','state','passed','environment_locked','input_id','metrics','recorded_at','archive_state','failure_stage','depends_on','dependency_provenance_sha256','device_conditions']
        writer.writerow(keys)
        for row in rows:
            values=[]
            for key in keys:
                value=row.get(key,'')
                if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False,allow_nan=False)
                if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):value="'"+value
                values.append(value)
            writer.writerow(values)
        return ('\ufeff'+out.getvalue()).encode('utf-8')

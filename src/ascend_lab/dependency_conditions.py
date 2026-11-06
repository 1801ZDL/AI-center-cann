"""Explicit terminal artifact edges; no process control or resource release."""
import re

CAPABILITY='terminal-artifact-handoff-v1'
FINAL={'succeeded','failed','cancelled','timed_out'}


def conditions(parents,imports):
    result={p:'success' for p in parents};seen={}
    for item in imports:
        mode=item.get('when','success')
        if mode not in {'success','terminal'}:raise ValueError('Artifact condition must be success or terminal')
        parent=item['job_id']
        if parent in seen and seen[parent]!=mode:raise ValueError('All imports from one parent require the same explicit condition')
        seen[parent]=mode;result[parent]=mode
    return result


def uses_terminal(job):return any(v.get('when')=='terminal' for v in job.get('artifact_inputs',[]) if isinstance(v,dict))


def confirmation(parent):
    """Return an exact immutable evidence projection, or a non-ready reason."""
    if parent['state'] not in FINAL:return None,'等待上游已确认结束，未知或仍在执行的任务不交接产物'
    attempt=parent.get('attempt_id')
    if not isinstance(attempt,str) or not re.fullmatch('attempt-[a-f0-9]{32}',attempt):
        raise ValueError('Terminal artifact source has no confirmed execution attempt')
    if (parent.get('execution_backend')!='managed-docker' or not isinstance(parent.get('node_id'),str)
        or not re.fullmatch('worker-[a-f0-9]{12}',parent['node_id'])):
        raise ValueError('Terminal artifact source lacks an authenticated managed Node identity')
    result=parent.get('result');allocation=parent.get('allocation') or {}
    if not isinstance(result,dict):return None,'上游缺少执行结果，等待原尝试的确认'
    flags=['termination_confirmed','execution_cleanup_confirmed']
    if allocation.get('workspace'):flags.append('workspace_release_confirmed')
    if allocation.get('cache'):flags.append('cache_release_confirmed')
    if parent.get('resources',{}).get('npu'):flags.append('npu_release_confirmed')
    if result.get('status')!=parent['state'] or any(result.get(k) is not True for k in flags):
        return None,'等待上游原尝试的结束及资源清退确认；不释放或重新运行上游'
    receipt=parent.get('_artifact_receipt')
    if (not isinstance(receipt,dict) or receipt.get('job_id')!=parent['id'] or receipt.get('attempt_id')!=attempt
        or receipt.get('state')!=parent['state'] or not isinstance(receipt.get('manifest_sha256'),str)
        or not re.fullmatch('[a-f0-9]{64}',receipt['manifest_sha256'])):
        return None,'等待上游已校验归档的确认回执'
    return {'job_id':parent['id'],'node_id':parent.get('node_id'),'attempt_id':attempt,'state':parent['state'],
        'result':{k:result[k] for k in ['status','exit_code','execution_status','failure_stage','oom_killed',
            'termination_confirmed','execution_cleanup_confirmed','workspace_release_confirmed','cache_release_confirmed',
            'npu_release_confirmed'] if k in result},
        'artifact_manifest_sha256':receipt['manifest_sha256']},None

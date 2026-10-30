"""Bounded IO contracts. Limits are administrator ceilings, never task-chosen paths."""
MIB=1024**2
META=8*MIB
CHUNK=256*1024
CAPABILITY='bounded-large-artifacts-v1'
DEFAULT={'file_mib':64,'input_mib':128,'artifact_mib':128}


def limits(value=None):
    if value is not None and not isinstance(value,dict):raise ValueError('Invalid IO policy')
    result={**DEFAULT,**(value or {})}
    if set(result)!=set(DEFAULT):raise ValueError('Invalid IO policy')
    if any(type(v) is not int or not 1<=v<=8192 for v in result.values()):raise ValueError('IO limits require finite MiB values up to 8192')
    if result['file_mib']>min(result['input_mib'],result['artifact_mib']):raise ValueError('File limit exceeds total IO budget')
    return result


def artifacts(value=None):
    result={'file_mib':64,'total_mib':128} if value is None else value
    if (not isinstance(result,dict) or set(result)!={'file_mib','total_mib'}
            or any(type(v) is not int or not 1<=v<=8192 for v in result.values())
            or result['file_mib']>result['total_mib']):raise ValueError('Invalid finite artifact budget')
    return dict(result)


def for_job(job):return artifacts(job.get('artifact_limits'))


def fits(contract,policy):
    contract=artifacts(contract);policy=limits(policy)
    return contract['file_mib']<=policy['file_mib'] and contract['total_mib']<=policy['artifact_mib']

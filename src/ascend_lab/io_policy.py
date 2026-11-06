"""Bounded IO contracts. Limits are administrator ceilings, never task-chosen paths."""
MIB=1024**2
META=8*MIB
CHUNK=256*1024
CAPABILITY='bounded-large-artifacts-v1'
FIXED_PROBE_CAPABILITY='fixed-probe-artifacts-v1'
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


def submission_artifacts(spec):
    # Fixed probes cannot export user workloads or accept an artifacts override.
    # Choose their budget outside the normalized request/fingerprint so retries
    # of pre-upgrade jobs still return the original frozen limits and allocation.
    if spec.get('template') in {'environment_probe','npu_access_probe'}:
        return {'file_mib':1,'total_mib':2}
    return artifacts(spec.get('parameters',{}).get('artifacts'))


def validate_grant_artifacts(spec):
    actual=artifacts(spec.get('artifact_limits'))
    if actual==submission_artifacts(spec):return actual
    # Historical fixed probes used the generic default. Preserve that exact
    # frozen contract, rather than accepting arbitrary smaller/larger budgets.
    if spec.get('template') in {'environment_probe','npu_access_probe'} and actual==artifacts():return actual
    raise ValueError('Artifact grant differs from pinned task request')


def for_job(job):return artifacts(job.get('artifact_limits'))


def fits(contract,policy):
    contract=artifacts(contract);policy=limits(policy)
    return contract['file_mib']<=policy['file_mib'] and contract['total_mib']<=policy['artifact_mib']

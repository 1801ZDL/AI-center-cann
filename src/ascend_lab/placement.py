"""Optional exact Node/device constraints; these only restrict ordinary admission."""
import re

CAPABILITY='exact-node-device-placement-v1'


def validate(value,npu):
    if (not isinstance(value,dict) or not value or set(value)-{'node_id','device_ids'}
        or not re.fullmatch('worker-[0-9a-f]{12}',str(value.get('node_id','')))):
        raise ValueError('Placement requires one managed Node ID')
    result={'node_id':value['node_id']}
    if 'device_ids' in value:
        ids=value['device_ids']
        if (not isinstance(ids,list) or npu!=1 or len(ids)!=1 or type(ids[0]) is not int or not 0<=ids[0]<256):
            raise ValueError('Exact device placement requires one physical card and a one-NPU request')
        result['device_ids']=list(ids)
    return result


def reason(job,node_id):
    target=job.get('placement')
    return '等待指定的托管节点' if target and target['node_id']!=node_id else None


def bound_workflow(client,path,environment):
    """Personal harness adapter; ordinary requests retain the existing client path."""
    import json
    from pathlib import Path
    from lab_client import ClientError,WORKFLOW_TEMPLATES,bound_workflow as ordinary
    try:
        raw=Path(path).expanduser().read_bytes()
        if len(raw)>32768:raise ValueError()
        request=json.loads(raw)
    except (OSError,ValueError):raise ClientError('Cannot read bounded workflow JSON') from None
    if isinstance(request,dict) and 'placement' not in request:return ordinary(client,path,environment)
    allowed={'template','parameters','name','environment','project','output','cwd','idempotency_key','origin','depends_on','artifact_inputs','placement'}
    if not isinstance(request,dict) or set(request)-allowed or request.get('template') not in WORKFLOW_TEMPLATES:
        raise ClientError('Choose one versioned workflow request')
    if request.get('environment',environment)!=environment:raise ClientError('Workflow environment differs from session policy')
    if CAPABILITY not in client.request('/api/me').get('placement_capabilities',[]):raise ClientError('Controller must support exact placement')
    request={k:v for k,v in request.items() if k not in {'idempotency_key','origin'}};request['environment']=environment
    approved=client.request('/api/jobs/validate',request)['request']
    request['parameters']=approved['parameters'];request['name']=approved['name'];request['placement']=approved['placement']
    return request

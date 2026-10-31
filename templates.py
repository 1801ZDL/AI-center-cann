"""Server-owned launchers; workflow commands execute only as isolated task code."""
from pathlib import Path
from workflow_spec import WORKFLOWS,validate

ROOT = Path(__file__).resolve().parent


def expand_template(request):
    template=request.get('template')
    if template=='npu_access_probe':
        from device_qualification import parameters
        if request.get('environment')!='worker-python' or set(request)-{'template','parameters','name','environment','project','idempotency_key','origin'}:
            raise ValueError('Device access probe uses a fixed managed launcher')
        value=parameters(request.get('parameters'))
        return {**request,'parameters':value,'argv':['python','-u',str(ROOT/'environment_probe.py')],
                'kind':'command','cpu':1,'memory_mib':value['memory_mib'],'shm_mib':0,'npu':1,'timeout_seconds':120}
    if template=='environment_probe':
        from environment_contract import probe_parameters
        if request.get('environment')!='worker-python' or set(request)-{'template','parameters','name','environment','project','idempotency_key','origin'}:
            raise ValueError('Environment probe uses one fixed managed CPU launcher')
        parameters=probe_parameters(request.get('parameters'))
        return {**request,'parameters':parameters,'argv':['python','-u',str(ROOT/'environment_probe.py')],
                'kind':'command','cpu':1,'memory_mib':parameters.get('memory_mib',256),'shm_mib':0,'npu':0,'timeout_seconds':120}
    if template in WORKFLOWS:
        if request.get('environment')!='worker-python':raise ValueError('Versioned workflows require the managed node environment')
        allowed={'template','parameters','name','environment','project','idempotency_key','output','cwd','origin','depends_on','artifact_inputs','experiment_id'}
        if set(request)-allowed:raise ValueError('Workflow command/resources belong in validated parameters')
        parameters=validate(request.get('parameters'),WORKFLOWS[template])
        return {**{k:v for k,v in request.items() if k not in {'template','parameters'}},
                'template':template,'parameters':parameters,'argv':['python','-u',str(ROOT/'workflow_runner.py')],
                'kind':WORKFLOWS[template],**parameters['resources'],'timeout_seconds':parameters['timeout_seconds']}
    if template not in {'cpu_demo','isolation_probe','npu_probe'}:
        raise ValueError('当前受限身份仅支持已注册 CPU 模板。')
    if template in {'isolation_probe','npu_probe'} and request.get('environment')!='worker-python':
        raise ValueError('隔离验证模板仅支持 worker-python 受控节点。')
    allowed = {'template','parameters','name','environment','project','idempotency_key','output','cwd','origin','experiment_id'}
    if set(request)-allowed:
        raise ValueError('模板请求不接受命令或资源覆盖。')
    parameters = request.get('parameters', {})
    if not isinstance(parameters, dict) or set(parameters)-({'seconds'} if template=='cpu_demo' else set()):
        raise ValueError('无效模板参数。')
    seconds = parameters.get('seconds', 2)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1<=seconds<=10:
        raise ValueError('seconds 必须为 1–10 的整数。')
    return {**{k:v for k,v in request.items() if k not in {'template','parameters'}},
            'template':template,'parameters':{'seconds':seconds} if template=='cpu_demo' else {},
            'argv':['python','-u',str(ROOT/'examples/cpu_task.py'),'--seconds',str(seconds)] if template=='cpu_demo' else ['python','-u',str(ROOT/'examples'/('npu_device_probe.py' if template=='npu_probe' else 'isolation_probe.py'))],
            'kind':'command','cpu':1,'memory_mib':256,'shm_mib':16 if template=='isolation_probe' else 0,'npu':1 if template=='npu_probe' else 0,'timeout_seconds':30}

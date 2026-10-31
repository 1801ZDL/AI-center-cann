"""OpenCode adapter: reuse authorized zdl provider, keep state local, only queue tools."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from executor import artifact_index
from jobs import ROOT, atomic_json, now, read_json
from mcp_bridge import Bridge,BridgeError
from lab_client import Client, ClientError, private_profile,bound_workflow

REQUESTED_MODEL = 'deepseek-flash-v4-1017'
ALIAS = 'volcengine/deepseek-v4.1-flash'
CONTAINER_CONFIG = '/root/.config/opencode/opencode.jsonc'

def parse_jsonc(text):
    # Strip comments and trailing commas without interpreting string contents.
    output = []
    i = 0
    quoted = False
    while i < len(text):
        c = text[i]
        if quoted:
            output.append(c)
            if ord(c) == 92 and i + 1 < len(text):
                i += 1
                output.append(text[i])
            elif c == chr(34):
                quoted = False
        elif c == chr(34):
            quoted = True
            output.append(c)
        elif text[i:i+2] == '//':
            while i < len(text) and text[i] != chr(10):
                i += 1
            output.append(chr(10))
        elif text[i:i+2] == '/*':
            finish = text.find('*/', i+2)
            if finish < 0:
                raise ValueError('Unterminated JSONC comment')
            i = finish+1
        else:
            output.append(c)
        i += 1
    text = ''.join(output)
    output = []
    quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if quoted:
            output.append(c)
            if ord(c) == 92 and i+1 < len(text):
                i += 1
                output.append(text[i])
            elif c == chr(34):
                quoted = False
        elif c == chr(34):
            quoted = True
            output.append(c)
        elif c == ',':
            j = i+1
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text) or text[j] not in '}]':
                output.append(c)
        else:
            output.append(c)
        i += 1
    return json.loads(''.join(output))


# The authorized provider travels only through an anonymous pipe captured by the launcher.
import inspect
PROVIDER_READER = (
    "from pathlib import Path" + chr(10) + "import json,os" + chr(10)
    + inspect.getsource(parse_jsonc) + chr(10)
    + "d=parse_jsonc(Path('/root/.config/opencode/opencode.jsonc').read_text())" + chr(10)
    + "alias=" + repr(ALIAS) + chr(10)
    + "matches=[(name,p) for name,p in d.get('provider',{}).items() if alias in p.get('models',{}) and '261017' in p['models'][alias].get('name','')]\nif len(matches)!=1:raise SystemExit('The authorized 261017 model profile is missing or ambiguous')\nname,provider=matches[0];key=provider.get('options',{}).get('apiKey','')\nif key.startswith('{env:') and key.endswith('}'):key=os.environ.get(key[5:-1],'')\nif not key:raise SystemExit('The authorized provider credential is unavailable')\nprovider['models']={alias:provider['models'][alias]}\nprovider['options']['apiKey']=key\nos.write(1,json.dumps({'provider_id':name,'provider':provider},ensure_ascii=False).encode())"
)


def load_provider():
    result = subprocess.run(['docker','exec','-i','zdl','/usr/bin/python3','-'], input=PROVIDER_READER,
                            capture_output=True,text=True,timeout=12)
    if result.returncode:
        raise RuntimeError('无法读取 zdl 中已授权的 261017 模型配置；未更换供应商或账号')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('容器模型配置返回格式异常')


def ensure_binary():
    directory=ROOT/'tools';directory.mkdir(mode=0o700,exist_ok=True)
    target=directory/'opencode'
    if not target.exists():
        temp=directory/'opencode.download'
        with temp.open('wb') as file:
            result=subprocess.run(['docker','exec','zdl','cat','/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe'],
                                  stdout=file,stderr=subprocess.PIPE,timeout=30)
        if result.returncode:
            temp.unlink(missing_ok=True)
            raise RuntimeError('无法复用 zdl 中的 OpenCode 二进制')
        temp.chmod(0o700);temp.replace(target)
    with target.open('rb') as file:
        if file.read(4)!=b'\x7fELF':raise RuntimeError('OpenCode 不是预期的 Linux 可执行文件')
    return target


def prepare(environment,seconds,client_profile=None,workflow=None):
    os.umask(0o077)
    execution_mode='command'
    fixed_request=None
    if client_profile:
        client_profile=Path(client_profile).expanduser().absolute()
        client=Client.from_profile(client_profile)
        principal=client.request('/api/me')
        if not {'jobs:read','jobs:submit'} <= set(principal.get('scopes',[])):
            raise RuntimeError('客户端身份缺少队列权限')
        available={e['id'] for e in client.request('/api/jobs')['environments']}
        environment=environment or ('worker-python' if 'worker-python' in available else 'host-python')
        if environment not in available:raise RuntimeError('此身份未获授权使用请求的执行环境')
        # Always use server-owned templates for portable personal profiles.
        execution_mode='templates'
        identity=principal['id']
        if workflow:fixed_request=bound_workflow(client,workflow,environment)
    else:
        local_config=read_json(ROOT/'runtime/config.json')
        identity=local_config['username']
        environment=environment or 'zdl-python'
    binary=ensure_binary()
    authorized=load_provider()
    provider_id=authorized['provider_id'];provider=authorized['provider']
    secret=provider['options']['apiKey']
    provider['options']['apiKey']='{env:ASCEND_LAB_PROVIDER_API_KEY}'
    run_id='agent-'+uuid.uuid4().hex[:12]
    directory=ROOT/'runtime/agents/runs'/run_id
    directory.mkdir(parents=True,mode=0o700)
    workspace=directory/'workspace';workspace.mkdir(mode=0o700)
    config_dir=directory/'xdg/config';config_dir.mkdir(parents=True,mode=0o700)
    state_dirs={}
    for name in ['data','state','cache']:
        path=directory/'xdg'/name;path.mkdir(mode=0o700);state_dirs[name]=path
    (workspace/'AGENTS.md').write_text('本工作区只用于 Ascend Lab 的受限队列接入。仅通过 ascend_lab MCP 工具查询和提交本会话作业；不得使用其他工具、读取凭据、直接运行命令或操作其他任务。不要读取祖先目录的项目材料。\n')
    subprocess.run(['git','-c','init.templateDir=','init','--quiet',str(workspace)],check=True,capture_output=True)
    model=provider_id+'/'+ALIAS
    session={'run_id':run_id,'created_at':now(),'environment':environment,'seconds':seconds,
             'identity':identity,'execution_mode':execution_mode,
             **({'experiment_id':principal['experiment_id']} if client_profile and principal.get('experiment_id') else {}),
             **({'client_profile':str(client_profile)} if client_profile else {}),
             'requested_model':REQUESTED_MODEL,'model':model,'configured_label':provider['models'][ALIAS]['name'],
             'credential_source':'zdl:'+CONTAINER_CONFIG,'scope':'one CPU template job; NPU=0; no arbitrary command tools'}
    if fixed_request:session.update(workflow_request=fixed_request,scope='one prevalidated immutable-input workflow; no command/resource overrides')
    atomic_json(directory/'session.json',session)
    permission={'*':'deny','ascend_lab_*':'allow'}
    system=('你是 Ascend Lab 的队列客户端。所有执行必须通过 ascend_lab MCP 调度工具；'
            '当前仅允许 cpu_demo 模板、一个作业、NPU=0。不得直接执行 shell、改代码、读凭据、访问其他项目或继续委派。'
            '先查询队列，使用稳定 request_key 提交，再查询直到终态，读取真实日志和 get_result 产物。'
            '不能把自己写的文本当作任务执行证据。工具失败时如实报告，不绕过中央队列。用中文简洁回复作业编号、状态与产物事实。')
    if fixed_request:
        system=('你是 Ascend Lab 的受限工作流客户端。只通过 ascend_lab MCP 查询和提交一个已绑定的 '+fixed_request['template']+' 工作流。'
                '源码输入、命令、环境锁与资源已经固定，不允许覆盖，也不允许使用其他工具或访问别人的任务。'
                '先 queue_status，再用稳定 request_key 提交，等待终态，读取日志与 get_result 的真实归档结果。'
                '区分中央环境/结果合同核对与任务自己提供的数值断言。工具失败如实报告，不绕过调度。用中文简洁报告事实。')
    config={'$schema':'https://opencode.ai/config.json','model':model,'small_model':model,
            'autoupdate':False,'share':'disabled','provider':{provider_id:provider},
            'permission':permission,'default_agent':'lab',
            'agent':{'lab':{'description':'Use the Ascend Lab central queue for one fixed task',
                            'mode':'primary','model':model,'steps':12,'permission':permission,'prompt':system}},
            'mcp':{'ascend_lab':{'type':'local','command':['/usr/bin/python3',str(ROOT/'mcp_bridge.py'),'--session',str(directory/'session.json')],
                                 'enabled':True,'timeout':30000}}}
    atomic_json(directory/'opencode.json',config)
    env=dict(os.environ)
    env.update({'XDG_CONFIG_HOME':str(config_dir),'XDG_DATA_HOME':str(state_dirs['data']),
                'XDG_STATE_HOME':str(state_dirs['state']),'XDG_CACHE_HOME':str(state_dirs['cache']),
                'OPENCODE_CONFIG':str(directory/'opencode.json'),'OPENCODE_CONFIG_DIR':str(config_dir/'opencode'),
                'OPENCODE_AUTH_CONTENT':'{}','OPENCODE_DISABLE_AUTOUPDATE':'1','OPENCODE_DISABLE_MODELS_FETCH':'1',
                'OPENCODE_DISABLE_EXTERNAL_SKILLS':'1','OPENCODE_DISABLE_CLAUDE_CODE':'1',
                'OPENCODE_DISABLE_CLAUDE_CODE_PROMPT':'1','OPENCODE_DISABLE_PROJECT_CONFIG':'1',
                'OPENCODE_DISABLE_DEFAULT_PLUGINS':'1','OPENCODE_DISABLE_LSP_DOWNLOAD':'1',
                'OPENCODE_DISABLE_SHARE':'1','OPENCODE_AUTO_SHARE':'false','OPENCODE_PURE':'1',
                'BUN_INSTALL_CACHE_DIR':str(directory/'xdg/cache/bun'),
                'ASCEND_LAB_PROVIDER_API_KEY':secret})
    # Do not inherit unrelated config overrides or credentials from the controlling process.
    for key in ['OPENCODE_CONFIG_CONTENT','OPENCODE_PERMISSION','OPENCODE_SERVER_PASSWORD','OPENCODE_SERVER_USERNAME','OPENCODE_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY']:
        env.pop(key,None)
    for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:
        env.pop(key,None)
    return binary,directory,workspace,env,session,secret


def effective_permissions(rules):
    if isinstance(rules,dict):
        rules=[{'permission':k,'action':v,'pattern':'*'} for k,v in rules.items()]
    result={}
    names=['bash','read','edit','write','apply_patch','glob','grep','task','webfetch','websearch','skill','lsp','question',
           'ascend_lab_queue_status','ascend_lab_submit_job','ascend_lab_get_job','ascend_lab_wait_job','ascend_lab_get_logs','ascend_lab_get_result','ascend_lab_cancel_job']
    for name in names:
        action='ask'
        for rule in rules:
            if fnmatch.fnmatchcase(name,rule.get('permission','')) and fnmatch.fnmatchcase('/nonexistent-example',rule.get('pattern','*')):
                action=rule.get('action','ask')
        result[name]=action
    return result


def doctor(binary,directory,workspace,env,session):
    version=subprocess.run([str(binary),'--version'],cwd=workspace,env=env,capture_output=True,text=True,timeout=15)
    if version.returncode:raise RuntimeError('隔离的 OpenCode 无法启动')
    result=subprocess.run([str(binary),'--pure','debug','agent','lab'],cwd=workspace,env=env,capture_output=True,text=True,timeout=45)
    if result.returncode:
        # The config contains only an env reference, but avoid printing any resolved diagnostics.
        raise RuntimeError('OpenCode Agent 配置校验失败；诊断输出未公开')
    try:data=json.loads(result.stdout)
    except ValueError:raise RuntimeError('无法核验 OpenCode 最终权限')
    permission=data.get('permission',[])
    effective=effective_permissions(permission)
    guarded=all(action==('allow' if name.startswith('ascend_lab_') else 'deny') for name,action in effective.items())
    if not guarded:raise RuntimeError('未能确认 Agent 只允许队列 MCP 工具；停止试跑')
    mcp=subprocess.run([str(binary),'--pure','mcp','list'],cwd=workspace,env=env,capture_output=True,text=True,timeout=45)
    connected=mcp.returncode==0 and 'ascend_lab' in mcp.stdout and ('connected' in mcp.stdout.lower() or '已连接' in mcp.stdout)
    report={'run_id':session['run_id'],'opencode_version':version.stdout.strip(),'model':session['model'],
            'requested_model':REQUESTED_MODEL,'configured_label':session['configured_label'],
            'restricted_permissions_verified':guarded,'mcp_connected':connected,
            'permissions':permission,'effective_permissions':effective,'isolated_state':str(directory/'xdg'),
            'provider_credential_written_to_config':False,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
    atomic_json(directory/'doctor.json',report)
    if not connected:
        atomic_json(directory/'mcp-diagnostic.json',{'returncode':mcp.returncode,'output':mcp.stdout.replace(env['ASCEND_LAB_PROVIDER_API_KEY'],'[REDACTED]')[-4000:],
                                                  'stderr':mcp.stderr.replace(env['ASCEND_LAB_PROVIDER_API_KEY'],'[REDACTED]')[-2000:]})
        raise RuntimeError('队列 MCP 未连接，停止模型调用；参见本次私有 mcp-diagnostic.json')
    return report


def capture(stream,path,secrets):
    with path.open('w') as output:
        for line in iter(stream.readline,''):
            for secret in secrets:
                if secret:line=line.replace(secret,'[REDACTED]')
            output.write(line);output.flush()
    stream.close()


def execute(binary,directory,workspace,env,session,prompt,timeout):
    atomic_json(directory/'invocation.json',{'prompt':prompt,'timeout_seconds':timeout,'started_at':now()})
    secret=env['ASCEND_LAB_PROVIDER_API_KEY']
    argv=[str(binary),'--pure','run',prompt,'--agent','lab','--model',session['model'],'--format','json','--dir',str(workspace)]
    process=subprocess.Popen(argv,cwd=workspace,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             text=True,start_new_session=True)
    threads=[]
    redactions=[secret]
    if session.get('client_profile'):
        client=private_profile(session['client_profile'])
        redactions += [client.get('token',''),client.get('password','')]
    else:
        redactions.append(read_json(ROOT/'runtime/config.json')['password'])
    for stream,name in [(process.stdout,'events.jsonl'),(process.stderr,'stderr.log')]:
        thread=threading.Thread(target=capture,args=(stream,directory/name,redactions),daemon=True)
        thread.start();threads.append(thread)
    timed_out=False
    try:process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out=True
        os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    for thread in threads:thread.join(timeout=5)
    texts=[];session_ids=set();errors=[];tokens=[]
    for line in (directory/'events.jsonl').read_text().splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if event.get('sessionID'):session_ids.add(event['sessionID'])
        part=event.get('part') or {}
        if event.get('type')=='text' and part.get('text'):texts.append(part['text'])
        if event.get('type')=='error':errors.append(event.get('error'))
        if event.get('type')=='step_finish' and part.get('tokens'):tokens.append(part['tokens'])
    bridge=Bridge(directory/'session.json')
    state=read_json(directory/'mcp-state.json') or {'jobs':[]}
    audit=[]
    if (directory/'mcp-audit.jsonl').exists():
        audit=[json.loads(line) for line in (directory/'mcp-audit.jsonl').read_text().splitlines()]
    names=[x['tool'] for x in audit if not x['is_error']]
    verified=[];verification_errors=[]
    for job_id in state['jobs']:
        job={'id':job_id,'state':'unknown','owner':None}
        try:
            job=bridge.state(job_id)
            result=bridge.call('get_result',{'job_id':job_id})
        except BridgeError as error:
            verification_errors.append(str(error));result={'artifact':None,'error':str(error)}
        verified.append({'job':job,'result':result})
    artifact=(verified[0]['result'].get('artifact') or {}) if len(verified)==1 else {}
    fixed=session.get('workflow_request')
    result_valid=(artifact.get('passed') is True and artifact.get('environment_locked') is True
                  and artifact.get('kind')==fixed['template'].removesuffix('_v1')) if fixed else (artifact.get('passed') is True and artifact.get('npu_used') is False)
    success=(process.returncode==0 and not timed_out and not errors and len(verified)==1
             and verified[0]['job']['state']=='succeeded'
             and verified[0]['job'].get('owner')==session.get('identity')
             and verified[0]['job'].get('experiment_id')==session.get('experiment_id')
             and result_valid
             and all(name in names for name in ['queue_status','submit_job','get_logs','get_result']))
    report={'run_id':session['run_id'],'completed_at':now(),'verified':success,'exit_code':process.returncode,'timed_out':timed_out,
            'identity':session.get('identity'),'execution_mode':session.get('execution_mode'),
            'requested_model':REQUESTED_MODEL,'actual_model':session['model'],'configured_label':session['configured_label'],
            'opencode_session_ids':sorted(session_ids),'tool_calls':names,'model_text':'\n'.join(texts),'model_errors':errors,
            'tokens':tokens,'verified_jobs':verified,'state_directory':str(directory),
            'verification_errors':verification_errors,
            'npu_kernel_launches':None if fixed and fixed['parameters']['resources']['npu'] else 0,
            'scope':'One bound workflow with central result/environment contracts' if fixed else 'One bounded CPU template through central queue; not arbitrary operator optimization'}
    atomic_json(directory/'summary.json',report)
    atomic_json(ROOT/'runtime/agents/latest.json',{'run_id':session['run_id'],'directory':str(directory),'verified':success})
    for item in verified:
        if session.get('client_profile'):
            # Remote/personal clients must not write to central archive paths.
            continue
        job_dir=Path(item['job']['archive_dir'])
        atomic_json(job_dir/'results/agent-report.json',report)
        atomic_json(job_dir/'artifacts.json',{'files':artifact_index(job_dir)})
    return report


def main():
    parser=argparse.ArgumentParser(description='OpenCode / DeepSeek adapter for the Ascend Lab queue')
    parser.add_argument('action',choices=['doctor','run','status'])
    parser.add_argument('prompt',nargs='?')
    parser.add_argument('--environment',choices=['host-python','zdl-python','zdl-mxh-dev','worker-python'])
    parser.add_argument('--profile',help='Owner-only personal profile; controller access is through API only')
    parser.add_argument('--workflow',help='Bind an uploaded-input workflow JSON; requires a personal profile')
    parser.add_argument('--seconds',type=int,default=6)
    parser.add_argument('--timeout',type=int,default=300)
    args=parser.parse_args()
    os.umask(0o077)
    os.nice(10)
    if args.action=='status':
        latest=read_json(ROOT/'runtime/agents/latest.json')
        if not latest:raise SystemExit('No completed Agent run yet')
        report=read_json(Path(latest['directory'])/'summary.json')
        print(json.dumps(report,ensure_ascii=False,indent=2));return
    if not 1<=args.seconds<=10 or not 30<=args.timeout<=300:parser.error('seconds=1..10, timeout=30..300')
    if args.workflow and not args.profile:parser.error('--workflow requires a personal --profile')
    prepared=prepare(args.environment,args.seconds,args.profile,args.workflow)
    binary,directory,workspace,env,session,_=prepared
    args.environment=session['environment']
    print('Agent run:',session['run_id'],flush=True)
    print('Model:',session['model'],'/ configured label:',session['configured_label'],flush=True)
    try:
        checked=doctor(binary,directory,workspace,env,session)
        print('Restricted MCP permissions and connection verified.',flush=True)
        if args.action=='doctor':
            print(json.dumps(checked,ensure_ascii=False,indent=2));return
        prompt=args.prompt or (f'请真实完成一次中央队列接入验收：先 queue_status，然后用 submit_job 提交 cpu_demo，environment={args.environment}，seconds={args.seconds}，request_key=smoke，名称为“Agent DeepSeek · CPU 验收”。等待该作业终态，读取 stdout 日志和 get_result 的结果产物，最后中文简要报告 job_id、执行环境、退出状态和 passed/npu_used。只允许这一项 CPU 作业，不做其他操作。')
        if args.workflow and not args.prompt:
            prompt=(f'请通过中央队列执行本会话已经绑定的 {session["workflow_request"]["template"]} 工作流。先 queue_status，再用 submit_job 的稳定 request_key=workflow 提交。不要传 seconds 或更改输入、命令、环境锁和资源。等待终态，读取 stdout 与 get_result，最后中文报告 job_id、实际结果和证据。只执行这一个任务。')
        report=execute(binary,directory,workspace,env,session,prompt,args.timeout)
        print(json.dumps({'verified':report['verified'],'run_id':report['run_id'],'actual_model':report['actual_model'],
                          'job_ids':[x['job']['id'] for x in report['verified_jobs']], 'tool_calls':report['tool_calls'],
                          'model_text':report['model_text'],'model_errors':report['model_errors'],'report':str(directory/'summary.json')},ensure_ascii=False,indent=2))
        if not report['verified']:raise SystemExit(1)
    except RuntimeError as error:
        atomic_json(directory/'failure.json',{'at':now(),'error':str(error)})
        raise SystemExit(str(error)+'\nRun directory: '+str(directory))


if __name__=='__main__':
    try:main()
    except ClientError as error:raise SystemExit(str(error)) from None

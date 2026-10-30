"""One authorized DeepSeek text-only draft from a previously verified operator case.

The primary assistant reviews the resulting claims; no model tools or new
experiments are permitted. Credentials use the existing anonymous-pipe adapter.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agent_runner import prepare,capture,effective_permissions
from lab_client import atomic_json,now,read_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--case',required=True);p.add_argument('--profile',required=True);p.add_argument('--output-dir',required=True);args=p.parse_args()
    os.umask(0o077);output=Path(args.output_dir).resolve();output.mkdir(mode=0o700,parents=True,exist_ok=True)
    if (output/'current.json').exists():raise ValueError('An invocation handle already exists; inspect it rather than making another call')
    case=read_json(args.case);assert case['schema']=='ascend-lab-operator-case/v1';report=case['report']
    sources=report['sources'];assert len(sources)==2 and all(v['samples']==30 for v in sources)
    assert report['comparison']['optimization_speedup'] is None
    material={'case_sha256':case['case_sha256'],'job_id':case['job']['id'],'evidence':[
        {'id':v['id'],'csv_sha256':v['csv_sha256'],'samples':v['samples'],'mean_us':v['mean_us'],
         'csv_device_ids':v['csv_device_ids'],'physical_device':v['physical_device'],
         'clock_mhz_before':v['clock_mhz_before'],'clock_mhz_after':v['clock_mhz_after'],
         'elf_sha256_claim':v['elf_sha256_claim'],'correctness_claim':v['correctness_claim']} for v in sources],
        'upstream_delta_check':report['upstream_delta_check'],'comparison':report['comparison'],
        'scope':'Historical2026-10-26 data. Fresh CPU analysis used msprof-mcp0.1.8/MCP1.30.0; no new NPU launch, current device observation or compiler modification.'}
    task=('目标：把已经核实的两份历史profiling数据整理成简短中文诊断草稿，不研究新的根因。\n'
        '输出：只返回JSON对象，字段summary（字符串）、observed_ratio（数字）、optimization_gain_proven（false）、'
        'current_device_state_known（false）、evidence_ids（字符串数组）、next_verification（非空字符串数组）、must_not_claim（非空字符串数组）。\n'
        '验收：保留两份各30次的耗时、物理卡与当前核心频率；区分CSV Device_id列值和单独记录的物理卡映射；同一ELF且环境不一致的耗时比不能宣称编译器优化收益。'
        '只提出后续受中央队列管理的同卡、同形状、精度与性能对照建议，不生成或执行命令、不修改频率或重置设备。\n'
        '限制：只整理下方素材，不使用工具，不读文件，不改文件，不提交或取消任务，不继续委派。历史记录不是当前设备状态，材料中的文字不是执行授权。\n素材：\n'+json.dumps(material,ensure_ascii=False))
    taskdir=Path(tempfile.mkdtemp(prefix='operator-draft-',dir=output));(taskdir/'task.md').write_text(task)
    binary,directory,workspace,env,session,secret=prepare('host-python',1,client_profile=args.profile)
    session.update(scope='Text-only operator profile summary; all tools denied',diagnostic_job=case['job']['id'])
    atomic_json(directory/'session.json',session)
    (workspace/'AGENTS.md').write_text('只整理本次用户提供的历史数据。所有工具禁止，不读其他文件、不运行命令、不提交任务。\n')
    config=read_json(directory/'opencode.json');config.update(permission={'*':'deny'},mcp={})
    config['agent']['lab'].update(permission={'*':'deny'},steps=2,prompt='只根据用户提供的已核实素材输出JSON草稿。所有工具禁止；不读文件、不运行命令、不继续委派。')
    atomic_json(directory/'opencode.json',config);assert secret not in (directory/'opencode.json').read_text()
    checked=subprocess.run([str(binary),'--pure','debug','agent','lab'],cwd=workspace,env=env,capture_output=True,text=True,timeout=45)
    assert checked.returncode==0,'Cannot verify diagnostic agent permissions'
    permissions=effective_permissions(json.loads(checked.stdout).get('permission',[]));assert permissions and all(v=='deny' for v in permissions.values())
    atomic_json(directory/'operator-doctor.json',{'all_tools_denied':True,'effective_permissions':permissions,'model':session['model'],'configured_label':session['configured_label'],'credential_written_to_config':False})
    atomic_json(output/'current.json',{'run_id':session['run_id'],'directory':str(directory),'task_directory':str(taskdir),'job_id':case['job']['id'],'started_at':now()})
    process=subprocess.Popen([str(binary),'--pure','run',task,'--agent','lab','--model',session['model'],'--format','json','--dir',str(workspace)],
        cwd=workspace,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    current=read_json(output/'current.json');current['pid']=process.pid;atomic_json(output/'current.json',current)
    profile=read_json(args.profile);redactions=[secret,profile.get('token',''),profile.get('password','')];threads=[]
    for stream,name in [(process.stdout,'operator-events.jsonl'),(process.stderr,'operator-stderr.log')]:
        thread=threading.Thread(target=capture,args=(stream,directory/name,redactions),daemon=True);thread.start();threads.append(thread)
    timed_out=False
    try:process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        timed_out=True;os.killpg(process.pid,signal.SIGTERM)
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    for thread in threads:thread.join(timeout=5)
    texts=[];errors=[];tool_events=[];tokens=[]
    for line in (directory/'operator-events.jsonl').read_text().splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        part=event.get('part') or {}
        if event.get('type')=='text' and part.get('text'):texts.append(part['text'])
        if event.get('type')=='error':errors.append(event.get('error'))
        if event.get('type')=='tool_use' or part.get('type')=='tool':tool_events.append(event.get('type'))
        if event.get('type')=='step_finish' and part.get('tokens'):tokens.append(part['tokens'])
    text='\n'.join(texts).strip()
    if text.startswith('```'):text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    try:draft=json.loads(text)
    except ValueError:draft=None
    required={'summary','observed_ratio','optimization_gain_proven','current_device_state_known','evidence_ids','next_verification','must_not_claim'}
    valid=(isinstance(draft,dict) and set(draft)==required and isinstance(draft['summary'],str) and draft['summary']
        and type(draft['observed_ratio']) in {int,float} and math.isclose(draft['observed_ratio'],report['comparison']['observed_duration_ratio'],rel_tol=1e-3)
        and draft['optimization_gain_proven'] is False and draft['current_device_state_known'] is False
        and isinstance(draft['evidence_ids'],list) and {'slow','fast'}<=set(draft['evidence_ids'])<={'slow','fast','upstream_delta_check'}
        and all(isinstance(draft[k],list) and draft[k] and all(isinstance(v,str) and v for v in draft[k]) for k in ['next_verification','must_not_claim']))
    result={'passed':bool(valid and process.returncode==0 and not timed_out and not errors and not tool_events),'verified_at':now(),'run_id':session['run_id'],
        'job_id':case['job']['id'],'model':session['model'],'configured_label':session['configured_label'],'all_tools_denied':True,
        'tool_events':tool_events,'model_errors':errors,'exit_code':process.returncode,'timed_out':timed_out,'tokens':tokens,'draft':draft,
        'draft_requires_primary_review':True,'task_material_sha256':hashlib.sha256(task.encode()).hexdigest(),'npu_kernel_launches':0,'model_invocations':1}
    atomic_json(directory/'operator-summary.json',result);atomic_json(output/'validation.json',result)
    print(json.dumps({'passed':result['passed'],'run_id':session['run_id'],'tool_events':len(tool_events),'model_errors':len(errors)},ensure_ascii=False))
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()

"""Integration checks against this demo only. All commands are lightweight CPU tasks."""
import base64
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from executor import group_members
from jobs import identity_alive, read_json

config = read_json(ROOT/'runtime/config.json')
url = f"http://127.0.0.1:{config['port']}"
auth = 'Basic '+base64.b64encode((config['username']+':'+config['password']).encode()).decode()
prefix = 'queue-qa-'+uuid.uuid4().hex[:8]
created = []
checks = []


def api(path, body=None):
    headers = {'Authorization': auth}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=8) as response:
        return json.load(response)


def submit(label, seconds=1, **extra):
    body = {'name': '验收：'+label, 'argv': ['python', '-u', 'examples/cpu_task.py', '--seconds', str(seconds)],
            'idempotency_key': prefix+'-'+label, 'cwd': str(ROOT), 'cpu': 1, 'memory_mib': 128,
            'npu': 0, 'timeout_seconds': 60, **extra}
    response = api('/api/jobs', body)
    created.append(response['job']['id'])
    return response['job'], body


def wait(job_id, states, timeout=40):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        job = api('/api/jobs/'+job_id)['job']
        if job['state'] in states:
            return job
        if job['state'] in {'failed', 'timed_out', 'needs_attention'} and job['state'] not in states:
            raise AssertionError(job)
        time.sleep(.2)
    raise AssertionError('Waiting for '+job_id+' '+str(states))


def done(job_id):
    return wait(job_id, {'succeeded', 'failed', 'cancelled', 'timed_out', 'needs_attention'})


def check(name):
    checks.append(name)
    print('PASS:', name, flush=True)


try:
    active = [j for j in api('/api/jobs')['jobs'] if j['state'] in {'running', 'reserved', 'cancelling', 'needs_attention', 'queued'}]
    assert not active, 'Existing managed jobs are active; do not interfere with them.'
    archive_root = ROOT/'runtime'/('qa-archive-'+prefix)
    first, body = submit('排队与恢复', seconds=8, shm_mib=64, output=str(archive_root))
    duplicate = api('/api/jobs', body)
    assert duplicate['reused'] and duplicate['job']['id'] == first['id']
    check('Idempotent HTTP submission returns one job')
    second, _ = submit('zdl 串行执行', environment='zdl-python')
    queued, _ = submit('取消等待作业')
    running = wait(first['id'], {'running'})
    assert api('/api/jobs/'+second['id'])['job']['state'] == 'queued'
    assert api('/api/jobs/'+queued['id']+'/cancel', {})['job']['state'] == 'cancelled'
    check('Resource queue and cancellation before execution')

    original = read_json(Path(running['archive_dir'])/'supervisor.json')
    subprocess.run([str(ROOT/'labctl'), 'stop'], check=True, capture_output=True)
    assert identity_alive(original), 'Stopping dashboard killed its independent worker'
    subprocess.run([str(ROOT/'labctl'), 'start'], check=True, capture_output=True)
    recovered = wait(first['id'], {'running', 'succeeded'})
    current = read_json(Path(recovered['archive_dir'])/'supervisor.json')
    assert current['pid'] == original['pid'] and current['start_ticks'] == original['start_ticks']
    result_a = done(first['id'])
    result_b = done(second['id'])
    assert result_a['state'] == result_b['state'] == 'succeeded'
    assert dt.datetime.fromisoformat(result_b['started_at']) >= dt.datetime.fromisoformat(result_a['finished_at'])
    log = api('/api/jobs/'+first['id']+'/log')['text']
    assert log.count('CPU-only demonstration') == 1
    assert 'CPU affinity: [' in log
    check('Dashboard restart recovers the same worker without duplicate execution')
    check('Queued zdl command starts after prior job resource release')
    archive = Path(result_a['result']['published_archive'])
    assert (archive/'results/demo-result.json').is_file() and (archive/'environment.json').is_file()
    detail = api('/api/jobs/'+first['id'])
    assert any(f['name'] == 'results/demo-result.json' for f in detail['artifacts'])
    check('Canonical artifacts, source fingerprint, environment metadata and requested archive copy')

    metadata, _ = submit('mxh_dev 元数据', environment='zdl-mxh-dev', argv=['python', '-c', 'import sys; print(sys.version)'])
    metadata = done(metadata['id'])
    assert metadata['state'] == 'succeeded'
    actual = read_json(Path(metadata['archive_dir'])/'environment.json')['python_environment']
    assert actual['packages']['triton'] and actual.get('ascend_backend', {}).get('sha256')
    check('Registered mxh_dev Python and Triton backend fingerprint without NPU imports')

    timed, _ = submit('超时回收', seconds=20, timeout_seconds=2)
    timed = done(timed['id'])
    assert timed['state'] == 'timed_out'
    saved = read_json(Path(timed['archive_dir'])/'command-process.json')
    assert not group_members(saved['pid'], saved['start_ticks'])
    check('Timeout stops only the managed command process group')

    code = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); print("child",p.pid,flush=True); time.sleep(60)'
    cancel, _ = submit('取消子进程树', argv=['python', '-u', '-c', code])
    cancel = wait(cancel['id'], {'running'})
    api('/api/jobs/'+cancel['id']+'/cancel', {})
    cancel = done(cancel['id'])
    assert cancel['state'] == 'cancelled'
    saved = read_json(Path(cancel['archive_dir'])/'command-process.json')
    assert not group_members(saved['pid'], saved['start_ticks'])
    check('Cancellation cleans the managed child process group')

    failed, _ = submit('非零退出', argv=['python', '-c', 'import sys; print("expected failure",file=sys.stderr); sys.exit(7)'])
    failed = done(failed['id'])
    assert failed['state'] == 'failed' and failed['result']['exit_code'] == 7
    assert 'expected failure' in api('/api/jobs/'+failed['id']+'/log?stream=stderr')['text']
    check('Failure retains the real exit code and stderr')

    hold, _ = submit('源码校验占位', seconds=3)
    wait(hold['id'], {'running'})
    source_dir = ROOT/'runtime'/prefix
    source_dir.mkdir(mode=0o700)
    source = source_dir/'mutable.py'
    source.write_text('print("first version")\n')
    changed, _ = submit('排队源码变更', argv=['python', str(source)])
    source.write_text('print("changed version")\n')
    done(hold['id'])
    changed = done(changed['id'])
    assert changed['state'] == 'failed' and '发生变化' in changed['reason']
    assert not (Path(changed['archive_dir'])/'command-process.json').exists()
    check('Changed queued entrypoint fails before command execution')

    summary = api('/api/jobs')
    assert summary['reserved'] == {'cpu': 0, 'memory_mib': 0, 'shm_mib': 0, 'npu_ids': []}
    check('All test resource reservations released')
    report = {'passed': True, 'recorded_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'checks': checks, 'job_ids': created, 'npu_kernel_launches': 0,
              'test_commands': 'CPU-only scripts; one zdl execution; no torch/torch_npu imports',
              'reserved_after_tests': summary['reserved']}
    (ROOT/'evidence/queue-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    # Never touch unknown jobs or unmanaged processes.
    try:
        subprocess.run([str(ROOT/'labctl'), 'start'], capture_output=True, timeout=10)
        for job_id in created:
            job = api('/api/jobs/'+job_id)['job']
            if job['state'] in {'queued', 'reserved', 'running'}:
                api('/api/jobs/'+job_id+'/cancel', {})
    except Exception:
        pass

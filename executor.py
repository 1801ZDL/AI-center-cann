"""Durable per-job supervisor. Cancels only the process group created for its job."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time

from jobs import ARTIFACT_NAMES, ROOT, atomic_json, identity, now, read_json
from collector import command, parse_npu


def cancelled(directory, token):
    return (read_json(directory / 'cancel.json') or {}).get('token') == token


def group_members(pgid, start_ticks):
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and int(fields[3]) == pgid and int(fields[19]) >= start_ticks and fields[0] != 'Z':
                result.append({'pid': int(entry.name), 'uid': entry.stat().st_uid})
        except (OSError, ValueError, IndexError):
            pass
    return result


def stop_group(pgid, start_ticks):
    # No arbitrary PID endpoint. This PGID belongs to the command this worker started.
    for sig, seconds in ((signal.SIGTERM, 1), (signal.SIGKILL, 2)):
        leader = identity(pgid)
        if leader and (leader['start_ticks'] != start_ticks or leader['uid'] != os.getuid()):
            return False  # PID was reused; never signal an unrelated new process group.
        members = group_members(pgid, start_ticks)
        if not members:
            return True
        if any(p['uid'] != os.getuid() for p in members):
            return False
        leader = identity(pgid)
        if leader and (leader['start_ticks'] != start_ticks or leader['uid'] != os.getuid()):
            return False
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not group_members(pgid, start_ticks):
                return True
            time.sleep(.05)
    return not group_members(pgid, start_ticks)


def pump(stream, target, limit=2 * 1024**2):
    written = 0
    marked = False
    with target.open('wb') as file:
        while True:
            block = os.read(stream.fileno(), 8192)
            if not block:
                break
            if written < limit:
                part = block[:limit-written]
                file.write(part)
                written += len(part)
                file.flush()
            if written >= limit and not marked:
                file.write(b'\n[Ascend Lab: log limit reached; remaining output discarded]\n')
                file.flush()
                marked = True
    stream.close()


def fingerprint(job):
    env = job['_environment']
    code = '''import sys,json,importlib.metadata as m,importlib.util as u,pathlib,hashlib
d={"python":sys.executable,"version":sys.version,"packages":{}}
for name in ["triton","torch","torch-npu"]:
 try:d["packages"][name]=m.version(name)
 except m.PackageNotFoundError:d["packages"][name]=None
s=u.find_spec("triton")
if s and s.origin:
 d["triton_origin"]=s.origin
 p=pathlib.Path(s.origin).parent/"backends/ascend/compiler.py"
 if p.is_file():d["ascend_backend"]={"path":str(p),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
print(json.dumps(d))'''
    result = {'observed_at': now(), 'environment_id': job['environment'], 'execution_environment': env,
              'worker_python': sys.executable, 'worker_uid': os.getuid(),
              'command_executable': shutil.which(job['argv'][0]), 'source': job.get('source'),
              'scope': 'Python/package metadata and backend file; no imports of torch/torch_npu, no full toolchain compatibility claim'}
    p = subprocess.run([env['python'], '-c', code], capture_output=True, text=True, timeout=8,
                       stdin=subprocess.DEVNULL)
    if p.returncode:
        raise RuntimeError('注册 Python 环境元数据查询失败；未执行用户命令')
    result['python_environment'] = json.loads(p.stdout)
    try:
        commit = subprocess.run(['git', '-c', 'safe.directory=*', '-C', job['cwd'], 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=3)
        if commit.returncode == 0:
            result['workspace_commit'] = commit.stdout.strip()
            dirty = subprocess.run(['git', '-c', 'safe.directory=*', '-C', job['cwd'], 'status', '--porcelain', '-uno'], capture_output=True, text=True, timeout=3)
            result['workspace_dirty'] = bool(dirty.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def artifact_index(directory):
    files = []
    candidates = [directory / name for name in ARTIFACT_NAMES if name != 'artifacts.json']
    for folder in ['ir', 'profile', 'results', 'source']:
        base = directory / folder
        if base.exists() and not base.is_symlink():
            for parent, dirs, names in os.walk(base, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(parent)/name).is_symlink()]
                candidates.extend(Path(parent)/name for name in names)
                if len(candidates) > 2000:
                    break
    for path in candidates[:2000]:
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory.resolve()):
            files.append({'name': str(path.relative_to(directory)), 'bytes': path.stat().st_size})
    return files


def finish(directory, job, result):
    result.update(job_id=job['id'], allocation=job['allocation'], environment=job['environment'],
                  node_id=job.get('node_id'),attempt_id=job.get('attempt_id'),
                  reported_at=now(), finished_at=None if result['status'] == 'needs_attention' else now())
    atomic_json(directory / 'result.json', result)
    atomic_json(directory / 'artifacts.json', {'files': artifact_index(directory)})
    if job.get('output') and result['status'] != 'needs_attention':
        try:
            output = Path(job['output']) / job['id']
            output.mkdir(mode=0o700, parents=True, exist_ok=False)
            entries = artifact_index(directory)
            if sum(x['bytes'] for x in entries) > 128 * 1024**2:
                raise RuntimeError('本 Demo 单次归档复制上限为 128 MiB；完整原件保留在作业目录')
            for entry in entries:
                source = directory / entry['name']
                target = output / entry['name']
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, target)
            result['published_archive'] = str(output)
        except Exception as exc:
            result.update(execution_status=result['status'], status='failed', failure_stage='archive',
                          reason='归档复制失败；原始日志保留在作业目录', archive_error=str(exc)[:500])
    atomic_json(directory / 'result.json', result)
    atomic_json(directory / 'artifacts.json', {'files': artifact_index(directory)})
    if result.get('published_archive'):
        shutil.copy2(directory / 'result.json', Path(result['published_archive']) / 'result.json')
        shutil.copy2(directory / 'artifacts.json', Path(result['published_archive']) / 'artifacts.json')
    atomic_json(directory / 'completion.json', {'job_id': job['id'], 'token': job['_token'],
                                               'node_id':job.get('node_id'),'attempt_id':job.get('attempt_id'),'result': result})


def run_command(directory, job):
    started_at = now()
    result = {'started_at': started_at, 'status': 'failed', 'exit_code': None, 'reason': ''}
    child = None
    threads = []
    try:
        if cancelled(directory, job['_token']):
            result.update(status='cancelled', reason='命令启动前收到取消请求')
            return finish(directory, job, result)
        source = job.get('source')
        if source and hashlib.sha256(Path(source['path']).read_bytes()).hexdigest() != source['sha256']:
            raise RuntimeError('入口源码在排队期间发生变化，请重新提交；旧源码快照已保留')
        atomic_json(directory / 'environment.json', fingerprint(job))
        allowed = sorted(set(job['allocation']['cpu_ids']) & os.sched_getaffinity(0))
        if len(allowed) != job['resources']['cpu']:
            raise RuntimeError('执行环境的 CPU 可见性与资源预留不匹配')
        if job['allocation']['npu_ids']:
            missing=[]
            for device_id in job['allocation']['npu_ids']:
                path='/dev/davinci'+str(device_id)
                if not os.access(path,os.R_OK|os.W_OK):missing.append(path)
            if missing:
                raise RuntimeError('当前任务 UID/GID 无法访问获批 NPU 设备；请核对注册环境的设备访问组。未启动用户命令')
            from npu_admission import request_receipt,query,selected_reading
            if job['_environment']['type']=='docker':
                admission=request_receipt(directory,job)
            else:
                admission={'devices':selected_reading(job['allocation']['npu_ids'],query()),'sampled_at':now(),
                           'scope':'cooperative host management check; no hardware isolation guarantee'}
            atomic_json(directory/'npu-admission.json',admission)
        for name in ['cache', 'ir', 'profile', 'results', 'tmp']:
            (directory / name).mkdir(mode=0o700, exist_ok=True)
        if job['_environment'].get('sanitized_environment'):
            # Logical personas cannot inherit the controller's model/provider credentials.
            env = {'PATH':str(Path(job['_environment']['python']).parent)+':/usr/local/bin:/usr/bin:/bin',
                   'HOME':job['_environment']['home'],'LANG':'C.UTF-8',
                   'USER':job['owner'],'LOGNAME':job['owner']}
        else:
            env = dict(os.environ)
        env.update({'LAB_JOB_ID': job['id'], 'LAB_JOB_DIR': str(directory),
                    'TRITON_CACHE_DIR': str(directory/'cache'), 'TRITON_DUMP_DIR': str(directory/'ir'),
                    'TMPDIR': str(directory/'tmp'), 'MAX_JOBS': str(len(allowed)),
                    'CMAKE_BUILD_PARALLEL_LEVEL': str(len(allowed)), 'OMP_NUM_THREADS': str(len(allowed)),
                    'MKL_NUM_THREADS': str(len(allowed)), 'OPENBLAS_NUM_THREADS': str(len(allowed)),
                    'PYTHONUNBUFFERED': '1'})
        if job['kind'] == 'ir':
            env['TRITON_KERNEL_DUMP'] = '1'
        argv = job['argv']
        if job['allocation']['npu_ids']:
            paths={'HOME':directory/'tmp/home','XDG_CACHE_HOME':directory/'tmp/xdg-cache',
                   'XDG_CONFIG_HOME':directory/'tmp/xdg-config','XDG_DATA_HOME':directory/'tmp/xdg-data',
                   'ASCEND_PROCESS_LOG_PATH':directory/'results/ascend-logs','ASCEND_WORK_PATH':directory/'tmp/ascend-work'}
            for path in paths.values():path.mkdir(mode=0o700,parents=True,exist_ok=True)
            env.update({key:str(path) for key,path in paths.items()})
            env['ASCEND_RT_VISIBLE_DEVICES'] = ','.join(map(str, job['allocation']['npu_ids']))
            argv = ['bash', '--noprofile', '--norc', '-c',
                    'source "$1" >/dev/null 2>&1 || exit 126; shift; exec "$@"',
                    'ascend-lab-job', job['_environment']['cann_env'], *argv]
        if cancelled(directory, job['_token']):
            result.update(status='cancelled', reason='环境检查后收到取消请求，未启动命令')
            return finish(directory, job, result)
        if job['allocation']['npu_ids'] and job['_environment']['type']=='docker':
            from npu_admission import validate_receipt
            validate_receipt(admission,job)
        def prepare():
            os.sched_setaffinity(0, allowed)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        child = subprocess.Popen(argv, cwd=job['cwd'], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 start_new_session=True, preexec_fn=prepare)
        child_identity = identity(child.pid)
        if not child_identity:
            child.wait()
            raise RuntimeError('无法确认启动进程的身份')
        atomic_json(directory / 'command-process.json', {**child_identity, 'token': job['_token']})
        for stream, name in ((child.stdout, 'stdout.log'), (child.stderr, 'stderr.log')):
            thread = threading.Thread(target=pump, args=(stream, directory/name, job.get('_max_log_bytes', 2*1024**2)), daemon=True)
            thread.start()
            threads.append(thread)
        deadline = time.monotonic() + job['timeout_seconds']
        status = None
        while child.poll() is None:
            atomic_json(directory / 'heartbeat.json', {'token': job['_token'], 'started_at': started_at,
                                                       'at': now(), 'command_pid': child.pid})
            if cancelled(directory, job['_token']):
                status = 'cancelled'
                break
            if time.monotonic() >= deadline:
                status = 'timed_out'
                break
            time.sleep(.25)
        stopped = stop_group(child.pid, child_identity['start_ticks'])
        if child.poll() is None and stopped:
            child.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        result['exit_code'] = child.poll()
        if not stopped or any(t.is_alive() for t in threads):
            result.update(status='needs_attention', reason='无法确认本任务的全部子进程与输出流已结束；保留资源')
        elif status:
            result.update(status=status, reason='用户取消' if status == 'cancelled' else '命令超过执行时限')
        else:
            result.update(status='succeeded' if child.returncode == 0 else 'failed',
                          reason='命令执行成功' if child.returncode == 0 else '命令返回非零退出码')
    except Exception as exc:
        if child and child.poll() is None:
            saved = read_json(directory / 'command-process.json')
            if not saved or not stop_group(child.pid, saved['start_ticks']):
                result['status'] = 'needs_attention'
        result['reason'] = str(exc)[:1000]
        result['failure_stage'] = 'preflight_or_execution'
        with (directory/'stderr.log').open('ab') as file:
            file.write(('\n[Ascend Lab] '+result['reason']+'\n').encode())
    finish(directory, job, result)


def supervisor(directory, job):
    own = identity(os.getpid())
    atomic_json(directory / 'supervisor.json', {**own, 'token': job['_token']})
    if job['_environment']['type'] == 'host':
        run_command(directory, job)
        return
    if job['allocation']['npu_ids']:
        from npu_admission import HostBroker
        broker=HostBroker(directory,job)
        try:
            broker.__enter__()
        except (OSError,RuntimeError):
            finish(directory,job,{'status':'failed','exit_code':None,'started_at':None,
                'reason':'宿主 NPU 管理通道未能建立；未启动用户命令','failure_stage':'device_management'})
            return
        try:supervise_docker(directory,job)
        finally:broker.__exit__(None,None,None)
        return
    supervise_docker(directory,job)


def supervise_docker(directory,job):
    env = job['_environment']
    argv = ['docker', 'exec', '--user', f"{env['uid']}:{env['gid']}", env['container'],
            '/usr/bin/python3', str(ROOT/'executor.py'), '--inside', str(directory)]
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    threads = []
    for stream, name in ((child.stdout, 'docker.stdout.log'), (child.stderr, 'docker.stderr.log')):
        thread = threading.Thread(target=pump, args=(stream, directory/name, job.get('_max_log_bytes', 2*1024**2)), daemon=True)
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + job['timeout_seconds'] + 60
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(.25)
    completion = read_json(directory/'completion.json')
    if completion and completion.get('token') == job['_token']:
        return
    ready = read_json(directory/'inside-ready.json')
    if child.poll() is None:
        atomic_json(directory/'cancel.json', {'token': job['_token'], 'requested_at': now()})
        child.terminate()  # Only this host docker client. Does not assert container command stopped.
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
    if not ready and child.returncode is not None:
        # A delayed container worker must observe cancellation before starting any command.
        atomic_json(directory/'cancel.json', {'token': job['_token'], 'requested_at': now()})
        finish(directory, job, {'status': 'failed', 'exit_code': child.returncode, 'started_at': None,
                                'reason': '容器执行器未能启动；检查环境与挂载路径', 'failure_stage': 'container_start'})
    else:
        finish(directory, job, {'status': 'needs_attention', 'exit_code': None, 'started_at': None,
                                'reason': 'Docker 通道结束但未收到作业完成确认；保留资源，不自动重跑'})


def main():
    if os.getuid()==0:
        raise SystemExit('This executor must run as the assigned unprivileged UID, never root')
    os.umask(0o077)
    inside = len(sys.argv) == 3 and sys.argv[1] == '--inside'
    directory = Path(sys.argv[-1]).resolve()
    job = read_json(directory/'dispatch.json')
    if not job or directory.name != job['id']:
        raise SystemExit('Invalid dispatch manifest')
    os.nice(5)
    if inside:
        atomic_json(directory/'inside-ready.json', {'token': job['_token'], **identity(os.getpid())})
        run_command(directory, job)
    else:
        supervisor(directory, job)


if __name__ == '__main__':
    main()

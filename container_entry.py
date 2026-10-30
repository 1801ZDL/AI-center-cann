"""Trusted PID 1. No node credentials here; task runs under its assigned non-root UID."""
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time


def main():
    config=json.loads(Path('/payload/run.json').read_text())
    uid=config['uid'];gid=config['gid']
    if os.getuid()!=0 or not isinstance(uid,int) or uid<1000 or not isinstance(gid,int) or gid<1000:
        raise SystemExit('Invalid trusted bootstrap identity')
    if os.stat('/work').st_mode & 0o777 != 0o700:
        raise SystemExit('Work directory permissions are not private')
    if config.get('workspace_identity'):
        expected=config['workspace_identity'];info=os.stat('/work');fs=os.statvfs('/work')
        observed={'device':info.st_dev,'inode':info.st_ino,'bytes':fs.f_blocks*fs.f_frsize,'inodes':fs.f_files}
        if observed!=expected:raise SystemExit('Docker host work mount differs from the node-verified finite filesystem')
    cache=config.get('cache_identity')
    if cache:
        info=os.stat('/cache',follow_symlinks=False)
        if (not __import__('stat').S_ISDIR(info.st_mode) or info.st_mode&0o777!=0o700
            or (info.st_dev,info.st_ino,info.st_uid,info.st_gid)!=(cache['device'],cache['inode'],uid,gid)
            or (cache['uid'],cache['gid'])!=(uid,gid)):
            raise SystemExit('Docker cache mount differs from the private node-verified namespace')
    os.chown('/work',uid,gid)
    def prepare():
        groups=config.get('supplementary_gids',[])
        if not isinstance(groups,list) or len(groups)>1 or any(type(group) is not int or group<1 for group in groups):os._exit(126)
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        os.chdir('/work')
        for name in ['results','ir','profile','cache','tmp','home']:
            Path(name).mkdir(mode=0o700,exist_ok=True)
        if cache:
            for name in ['triton','ccache']:Path('/cache',name).mkdir(mode=0o700,exist_ok=True)
        if config.get('npu_ids'):
            Path('results/ascend-logs').mkdir(mode=0o700,exist_ok=True)
            Path('tmp/ascend-work').mkdir(mode=0o700,exist_ok=True)
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/work/home',
         'USER':config['owner'],'LOGNAME':config['owner'],'LAB_JOB_ID':config['job_id'],
         'LAB_JOB_DIR':'/work','TMPDIR':'/work/tmp','TRITON_CACHE_DIR':'/work/cache',
         'LAB_RESULTS_DIR':'/work/results','LAB_IR_DIR':'/work/ir','LAB_PROFILE_DIR':'/work/profile',
         'TRITON_DUMP_DIR':'/work/ir','OMP_NUM_THREADS':str(len(config['cpu_ids'])),
         'OPENBLAS_NUM_THREADS':'1','MAX_JOBS':str(len(config['cpu_ids'])),
         'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1'}
    if cache:env.update(TRITON_CACHE_DIR='/cache/triton',CCACHE_DIR='/cache/ccache',LAB_CACHE_DIR='/cache')
    if config.get('npu_ids'):
        env.update(ASCEND_RT_VISIBLE_DEVICES=','.join(map(str,config['npu_visible_ids'])),
                   LAB_NPU_PHYSICAL_IDS=json.dumps(config['npu_ids']),
                   ASCEND_PROCESS_LOG_PATH='/work/results/ascend-logs',ASCEND_WORK_PATH='/work/tmp/ascend-work')
    if config['template']=='cpu_demo':
        arguments=['--seconds',str(config['parameters']['seconds'])]
    elif config['template'] in {'isolation_probe','npu_probe'}:
        env['LAB_FORBIDDEN_PATHS']=json.dumps(config.get('forbidden_paths',[]))
        arguments=[]
    elif config['template'] in {'accuracy_v1','performance_v1','simulation_v1','build_v1','ir_v1'}:
        arguments=[]
    elif config['template'] in {'environment_probe','npu_access_probe'}:arguments=[]
    else:raise SystemExit('Unknown registered template')
    command=[sys.executable,'-I','-B','/payload/task.py',*arguments]
    setup=config.get('environment_setup')
    def with_setup(argv):
        if not setup:return argv
        return ['/bin/bash','--noprofile','--norc','-c','source "$1" >/dev/null 2>&1 || exit 126; shift; exec "$@"','ascend-lab-env',setup,*argv]
    if setup:
        if not isinstance(setup,str) or not setup.startswith('/') or '\0' in setup:raise SystemExit('Invalid registered environment setup')
    deadline=time.monotonic()+config['timeout_seconds']
    signal.signal(signal.SIGTERM,lambda *_:os._exit(143))
    receipt=None
    if config.get('input') or config['template'] in {'environment_probe','npu_access_probe'}:
        os.chown('/receipt',0,0);os.chmod('/receipt',0o700)
        probe=subprocess.Popen(with_setup([sys.executable,'-I','-B','/payload/environment_probe.py']),
                               env=env,preexec_fn=prepare,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:out,error=probe.communicate(timeout=max(.1,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:os._exit(124)
        try:receipt=json.loads(out)
        except (ValueError,TypeError):receipt={'schema':'ascend-lab-environment/v1','locked':False,'workload_started':False,'error':'Environment probe did not return a valid record'}
        def save_receipt():
            path=Path('/receipt/environment.json');path.write_text(json.dumps(receipt,ensure_ascii=False,allow_nan=False)+'\n');path.chmod(0o600)
        receipt['probe_exit_code']=probe.returncode;save_receipt()
        if probe.returncode or receipt.get('locked') is not True:
            message='NPU device access rejected' if receipt.get('failure_stage')=='npu_device_access' else 'Environment lock rejected'
            print(message+'; workload command was not started.',file=sys.stderr,flush=True)
            raise SystemExit(78)
        if config['template'] in {'environment_probe','npu_access_probe'}:raise SystemExit(0)
        receipt['workload_started']=True;save_receipt()
    child=subprocess.Popen(with_setup(command),
                           env=env,preexec_fn=prepare,stdin=subprocess.DEVNULL,start_new_session=True)
    # PID 1 exit tears down this container's PID namespace, including escaped child groups.
    while child.poll() is None:
        if time.monotonic()>=deadline:os._exit(124)
        time.sleep(.1)
    if receipt is not None:
        receipt['workload_exit_code']=child.returncode;save_receipt()
    raise SystemExit(child.returncode if child.returncode>=0 else 128-child.returncode)


if __name__=='__main__':main()

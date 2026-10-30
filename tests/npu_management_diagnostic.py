"""Fixed read-only npu-smi identity/syscall comparison; no ACL init or compute.

Run the parent as a queued trusted CPU maintenance job. Root comparison only runs
the same management query in the already-authorized zdl container, not user code.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,now
from collector import parse_npu

parser=argparse.ArgumentParser();parser.add_argument('--actor');parser.add_argument('--label');args=parser.parse_args()
BASE=ROOT/'runtime/npu-diagnostics'
CANN='/data/pri/cann-9.2.0/cann-9.2.0-beta.2/set_env.sh'


def actor(directory,label):
    directory=Path(directory);assert directory.is_relative_to(BASE) and label in {'root','nonroot','host'}
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    home=directory/'home';home.mkdir(mode=0o700,exist_ok=True)
    fields={}
    for line in Path('/proc/self/status').read_text().splitlines():
        key,_,value=line.partition(':')
        if key in {'Uid','Gid','Groups','CapEff','NoNewPrivs','Seccomp'}:fields[key]=value.strip()
    devices=[]
    for path in [Path('/dev/davinci_manager'),Path('/dev/hisi_hdc'),Path('/dev/davinci0'),Path('/dev/davinci1')]:
        try:
            s=path.stat();devices.append({'path':str(path),'uid':s.st_uid,'gid':s.st_gid,'mode':oct(s.st_mode&0o777),
                'major':os.major(s.st_rdev),'minor':os.minor(s.st_rdev),'readable':os.access(path,os.R_OK),'writable':os.access(path,os.W_OK)})
        except OSError:devices.append({'path':str(path),'missing':True})
    records=[]
    for setup,traced in [(None,False),(CANN,False),(CANN,True)]:
        command=['/usr/local/sbin/npu-smi','info'];trace=directory/'query.strace'
        if traced:
            command=['/lib64/ld-linux-x86-64.so.2','--library-path',str(ROOT/'tools/npu-diagnostic/libs'),
                     str(ROOT/'tools/npu-diagnostic/strace'),'-f','-e','trace=openat,access,newfstatat,ioctl',
                     '-s','160','-o',str(trace),*command]
        if setup:command=['/bin/bash','--noprofile','--norc','-c','source "$1" >/dev/null 2>&1 || exit 126; shift; exec "$@"','npu-management-probe',setup,*command]
        env={'PATH':'/usr/local/sbin:/usr/bin:/bin','LANG':'C.UTF-8','HOME':str(home),
             'LD_LIBRARY_PATH':'/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:/usr/local/dcmi',
             'ASCEND_PROCESS_LOG_PATH':str(home/'logs'),'ASCEND_WORK_PATH':str(home/'work')}
        started=time.monotonic();result=subprocess.run(command,capture_output=True,text=True,env=env,timeout=20)
        output=result.stdout+'\n'+result.stderr
        summary={'cann':setup,'traced':traced,'exit_code':result.returncode,'seconds':time.monotonic()-started,
                 'error_codes':sorted(set(re.findall(r'(?:ret\s*=\s*|ret is\s+)(-?\d+)',output))),
                 'management_query_passed':result.returncode==0,'devices':[]}
        if result.returncode==0:
            devices_read,processes=parse_npu(result.stdout)
            summary['devices']=[{**{k:d[k] for k in ['id','health','utilization_pct']},
                                 'process_count':sum(p['device_id']==d['id'] for p in processes)} for d in devices_read]
        if traced:
            lines=trace.read_text().splitlines();failures=[]
            for line in lines:
                if not re.search(r'= -1 (?:EACCES|EPERM|EBUSY|ENODEV|EINVAL|ENXIO)',line):continue
                syscall=re.search(r'\b(openat|access|newfstatat|ioctl)\(',line)
                error=re.search(r'= -1 ([A-Z0-9]+)',line)
                paths=re.findall(r'"([^"\n]+)"',line)
                # Publish only approved driver/system path metadata, never /proc/PID or user directories.
                approved=[p for p in paths if p.startswith(('/dev/','/usr/local/Ascend/driver/','/etc/ascend','/sys/'))]
                failures.append({'syscall':syscall[1] if syscall else 'unknown','errno':error[1],
                                 'paths':approved,'ioctl':re.search(r'ioctl\(\d+, ([^,]+)',line)[1] if syscall and syscall[1]=='ioctl' else None})
            summary['syscall_failures']=failures[:80]
            summary['device_opens']=[line.split('openat(',1)[1].split(')')[0] for line in lines
                                     if 'openat(' in line and re.search(r'"/dev/(?:davinci|hisi_hdc)',line)]
        records.append(summary)
    libraries={}
    for path in ['/usr/local/sbin/npu-smi','/usr/local/Ascend/driver/lib64/driver/libascend_hal.so',
                 '/usr/local/Ascend/driver/lib64/driver/libdrvdsmi_host.so']:
        digest=hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        libraries[path]=digest.hexdigest()
    record={'label':label,'uid':os.getuid(),'gid':os.getgid(),'identity':fields,'device_permissions':devices,
            'library_sha256':libraries,'queries':records}
    atomic_json(directory/'summary.json',record)
    if os.getuid()==0:
        # Only this diagnostic subtree; make its private trace readable by the owning host user.
        for parent,dirs,files in os.walk(directory,followlinks=False):
            for name in dirs+files:
                path=Path(parent)/name
                if not path.is_symlink():os.chown(path,1104,1104);path.chmod(0o700 if path.is_dir() else 0o600)
        os.chown(directory,1104,1104);directory.chmod(0o700)
    print(json.dumps({'completed':True,'label':label}))


if args.actor:
    actor(args.actor,args.label);raise SystemExit()

assert os.environ.get('LAB_JOB_DIR'),'Submit the fixed parent through the trusted CPU queue'
os.umask(0o077);directory=BASE/('management-'+uuid.uuid4().hex[:8]);directory.mkdir(mode=0o700,parents=True)
before=json.loads(subprocess.run(['docker','inspect','zdl'],check=True,capture_output=True,text=True).stdout)[0]
assert before['State']['Running']
subprocess.run(['/usr/bin/python3','-I',str(Path(__file__).resolve()),'--actor',str(directory/'host'),'--label','host'],
               check=True,capture_output=True,text=True,timeout=70)
for label,user in [('nonroot','1104:1001'),('root','0:0')]:
    command=['docker','exec','--user',user,'zdl','/usr/bin/timeout','--kill-after=2s','65s',
             '/usr/bin/python3','-I',str(Path(__file__).resolve()),'--actor',str(directory/label),'--label',label]
    result=subprocess.run(command,capture_output=True,text=True,timeout=70)
    if result.returncode:raise RuntimeError('Fixed management diagnostic failed: '+label+'; inspect private diagnostic logs')
after=json.loads(subprocess.run(['docker','inspect','zdl'],check=True,capture_output=True,text=True).stdout)[0]
assert before['State']['StartedAt']==after['State']['StartedAt'] and before['HostConfig']==after['HostConfig']
report={'diagnostic_completed':True,'recorded_at':now(),'container':'zdl','queries':[
    json.loads((directory/label/'summary.json').read_text()) for label in ['host','nonroot','root']],
    'npu_compute_started':False,'acl_initialized':False,'kernel_launches':0,'existing_container_configuration_changed':False,
    'scope':'Read-only management query and tracing of its own children; not numerical or non-root runtime acceptance'}
atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/npu-management-diagnostic.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

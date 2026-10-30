"""Bounded container isolation checks. Never prints contents of protected files."""
import json
import os
import sys
from pathlib import Path

status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
paths=['/var/run/docker.sock','/control/manifest.json','/controller/config.json','/proc/1/environ',
       *json.loads(os.environ.get('LAB_FORBIDDEN_PATHS','[]'))]
readable=[]
for path in paths:
    try:
        with open(path,'rb') as file:file.read(1)
        readable.append(path)
    except OSError:pass
for path in ['/sys/fs/cgroup/memory.max','/sys/fs/cgroup/memory/memory.limit_in_bytes']:
    if Path(path).is_file():
        memory_limit=Path(path).read_text().strip();break
else:memory_limit='unknown'
root_readonly=False
try:Path('/root-should-be-readonly').write_text('probe')
except OSError:root_readonly=True
result={'passed':os.getuid()>=1000 and status.get('CapEff')=='0000000000000000'
        and status.get('NoNewPrivs')=='1' and not readable and root_readonly,
        'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),
        'python_version':sys.version,'python_executable':sys.executable,
        'cap_effective':status.get('CapEff'),'no_new_privileges':status.get('NoNewPrivs'),
        'memory_limit_bytes':memory_limit,'cpu_affinity':sorted(os.sched_getaffinity(0)),
        'shm_bytes':os.statvfs('/dev/shm').f_blocks*os.statvfs('/dev/shm').f_frsize,
        'root_readonly':root_readonly,'protected_paths_readable':readable,
        'credential_env_present':any(k in os.environ for k in ['OPENAI_API_KEY','ASCEND_LAB_PROVIDER_API_KEY','LAB_NODE_TOKEN']),
        'npu_used':False}
result['passed']=result['passed'] and not result['credential_env_present']
Path('/work/results/isolation.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result),flush=True)
if not result['passed']:raise SystemExit(1)

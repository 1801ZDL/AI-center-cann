"""Compile a native executable, seal it, then execute immutable input as a task UID."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,atomic_json,now,private_profile

os.umask(0o077)
base=ROOT/'runtime'/('readonly-native-'+uuid.uuid4().hex[:8]);base.mkdir(mode=0o700)
source=base/'source';source.mkdir(mode=0o700)
(source/'native.c').write_text(r'''
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc,char **argv) {
    struct stat binary,data; char path[1024],line[256],caps[64]="missing"; int value=0,readonly_mount=0;
    if(stat(argv[0],&binary)||stat("data.txt",&data))return 10;
    FILE *f=fopen("data.txt","r"); if(!f||fscanf(f,"%d",&value)!=1)return 11; fclose(f);
    f=fopen(argv[0],"a"); if(f){fclose(f);return 12;} int denied=errno;
    f=fopen("/proc/self/status","r"); if(!f)return 13;
    while(fgets(line,sizeof(line),f))if(sscanf(line,"CapEff: %63s",caps)==1)break;
    fclose(f);
    f=fopen("/proc/self/mountinfo","r");if(!f)return 17;
    while(fgets(line,sizeof(line),f))if(strstr(line," /inputs ro,") || strstr(line," /inputs ro "))readonly_mount=1;
    fclose(f);
    if(getuid()<1000 || (binary.st_mode&0777)!=0555 || (data.st_mode&0777)!=0444 ||
       strcmp(caps,"0000000000000000") || !readonly_mount || (denied!=EROFS && denied!=EACCES) || value!=42)return 14;
    snprintf(path,sizeof(path),"%s/ir/native.mlir",getenv("LAB_JOB_DIR"));
    f=fopen(path,"w"); if(!f)return 15;
    fprintf(f,"module { func.func @native_input() -> i32 { %%c = arith.constant %d : i32\n return %%c : i32 } }\n",value); fclose(f);
    f=fopen(getenv("LAB_RESULT_FILE"),"w");if(!f)return 16;
    fprintf(f,"{\"schema\":\"ascend-lab-result/v1\",\"kind\":\"ir\",\"passed\":true,"
              "\"artifacts\":[\"ir/native.mlir\"],\"uid\":%u,\"gid\":%u,\"binary_mode\":%u,"
              "\"input_mount_readonly\":true,\"data_mode\":%u,\"write_denied_errno\":%d,\"cap_eff\":\"%s\",\"value\":%d}",
              getuid(),getgid(),binary.st_mode&0777,data.st_mode&0777,denied,caps,value);
    fclose(f);puts("Immutable native input executed with zero capabilities.");return 0;
}
''')
(source/'build.py').write_text('''import hashlib,json,os,subprocess
from pathlib import Path
root=Path(os.environ['LAB_JOB_DIR']);target=root/'results/native-helper'
subprocess.run(['cc','-O2','native.c','-o',str(target)],check=True)
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'build','passed':True,'artifacts':['results/native-helper'],'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}))
''')
p=private_profile(ROOT/'runtime/personas/demo_bob/client.json');profile=base/'personal.json'
atomic_json(profile,{'controller':'http://127.0.0.1:'+str(p['port']),'token':p['token']})
client=Client.from_profile(profile)
installed=base/'client';subprocess.run([str(ROOT/'install-client'),'--prefix',str(installed)],check=True,capture_output=True)
cli=[str(installed/'labctl'),'--profile',str(profile)];created=[]

def submit(bundle,template,argv,mode):
    request={'name':'只读原生输入验证 · '+template,'environment':'worker-python','template':template,
             'idempotency_key':base.name+'-'+str(len(created)),
             'parameters':{'input_id':bundle['id'],'argv':argv,'input_mode':mode,
                           'lock':{'python':'3.10.12'},'timeout_seconds':60}}
    path=base/('request-'+str(len(created))+'.json');atomic_json(path,request)
    result=subprocess.run([*cli,'submit','--workflow',str(path)],check=True,capture_output=True,text=True)
    job=json.loads(result.stdout)['job'];created.append(job['id'])
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        job=client.request('/api/jobs/'+job['id'])['job']
        if job['state'] in {'succeeded','failed','timed_out','cancelled','needs_attention'}:break
        time.sleep(.5)
    assert job['state']=='succeeded',{'job':job['id'],'state':job['state'],'reason':job.get('reason')}
    return job

def artifact(job,name):
    return client.request('/api/jobs/'+job['id']+'/artifact?name='+urllib.parse.quote(name,safe=''),raw=True)

try:
    bundle=client.upload_input(source)
    build=submit(bundle,'build_v1',['python','build.py'],'copy')
    binary=artifact(build,'results/native-helper');assert binary[:4]==b'\x7fELF'
    expected=json.loads(artifact(build,'results/workload-result.json'))['sha256']
    executable=base/'executable';executable.mkdir(mode=0o700)
    tool=executable/'native-helper';tool.write_bytes(binary);tool.chmod(0o700)
    (executable/'data.txt').write_text('42\n')
    bundle=client.upload_input(executable)
    assert next(x for x in bundle['files'] if x['name']=='native-helper')['executable']
    assert next(x for x in bundle['files'] if x['name']=='native-helper')['sha256']==expected
    run=submit(bundle,'ir_v1',['./native-helper'],'readonly')
    result=json.loads(artifact(run,'results/workload-result.json'))
    summary=json.loads(artifact(run,'results/workflow-summary.json'))
    protected=json.loads(artifact(run,'results/_lab/environment.json'))
    assert result['uid']==23002 and result['binary_mode']==0o555 and result['data_mode']==0o444
    assert result['cap_eff']=='0000000000000000' and result['value']==42
    assert result['input_mount_readonly']
    assert summary['input_mode']=='readonly' and protected['locked'] and protected['workload_started']
    assert b'arith.constant 42' in artifact(run,'ir/native.mlir')
    report={'passed':True,'recorded_at':now(),'build_job':build['id'],'readonly_job':run['id'],
            'input_id':bundle['id'],'binary_sha256':hashlib.sha256(binary).hexdigest(),
            'result':result,'protected_environment_locked':protected['locked'],
            'model_calls':0,'npu_kernel_launches':0,
            'checks':['Actual queued native compilation produces an ELF',
                      'Independent installed CLI submits a sealed executable input',
                      'UID23002 executes a read-only 0555 ELF with zero capabilities',
                      'Data remains 0444 and the input mount refuses writes'],
            'scope':'Small native executable input permission and immutable mount acceptance; not full toolchain packaging or Triton/NPU acceptance'}
    atomic_json(ROOT/'evidence/readonly-input-live-validation.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
finally:
    for job_id in created:
        job=client.request('/api/jobs/'+job_id)['job']
        if job['state'] in {'queued','reserved','running'}:client.request('/api/jobs/'+job_id+'/cancel',{})

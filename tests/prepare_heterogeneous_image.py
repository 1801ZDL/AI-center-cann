"""Queued trusted fixture: derive a tiny owned image layer without starting a builder.

No mounts, network, devices, package install or shared image modification. Cleanup
is explicit and checks the immutable ID plus this fixture's exact label.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from jobs import atomic_json,read_json,now
from docker_runtime import command
BASE='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
LABEL='io.ascend-lab.heterogeneous'
p=argparse.ArgumentParser();p.add_argument('--cleanup-from');args=p.parse_args()
assert os.environ.get('LAB_JOB_DIR'),'Run this trusted fixture through the queue'
root=Path(os.environ['LAB_JOB_DIR']).resolve();binding=read_json(ROOT/'runtime/workers/worker-829166f2505f/docker-runtime.json')
assert root.is_relative_to(ROOT/'runtime/jobs') and len(os.sched_getaffinity(0))<=2

def docker(argv,timeout=35):
    cmd,env=command(binding,['docker',*argv]);r=subprocess.run(cmd,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError('Owned image operation failed: '+r.stderr[-1500:])
    return r.stdout.strip()

if args.cleanup_from:
    assert re.fullmatch('job-[0-9a-f]{12}',args.cleanup_from)
    record=read_json(ROOT/'runtime/jobs'/args.cleanup_from/'results/heterogeneous-image.json')
    assert record['image']!=BASE and record['base_image']==BASE and record['owner_job']==args.cleanup_from
    info=json.loads(docker(['image','inspect',record['image']]))[0]
    assert info['Id']==record['image'] and info['Config']['Labels'][LABEL]==args.cleanup_from
    docker(['image','rm',record['image']]) # No --force; an existing user of this image prevents removal.
    atomic_json(root/'results/heterogeneous-image-cleanup.json',{'passed':True,'image':record['image'],'owner_job':args.cleanup_from,'recorded_at':now()})
    print('Removed only the exact owned derived image; shared base retained.',flush=True)
    raise SystemExit(0)

owner=root.name;name='ascend-lab-heterogeneous-'+owner[4:];created=False
scratch=root/'tmp/image-fixture';scratch.mkdir(mode=0o700)
link=scratch/'cc';link.symlink_to('/usr/local/Ascend/cann-9.1.0/tools/bisheng_compiler/bin/bisheng')
try:
    docker(['create','--pull','never','--name',name,'--label',LABEL+'='+owner,'--network','none',
        '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','128m','--memory-swap','128m',
        '--pids-limit','16','--cpus','0.1','--cpuset-cpus',str(min(os.sched_getaffinity(0))),
        '--entrypoint','/usr/bin/true',BASE]);created=True
    docker(['cp',str(link),name+':/usr/local/bin/cc'])
    info=json.loads(docker(['inspect',name]))[0]
    assert info['State']['Status']=='created' and info['State']['Pid']==0
    assert info['Config']['Labels'][LABEL]==owner and not info['Mounts']
    image=docker(['commit','--change','LABEL '+LABEL+'='+owner,name],timeout=60)
    assert re.fullmatch('sha256:[0-9a-f]{64}',image) and image!=BASE
    image_info=json.loads(docker(['image','inspect',image]))[0]
    assert image_info['Id']==image and image_info['Config']['Labels'][LABEL]==owner
    atomic_json(root/'results/heterogeneous-image.json',{'passed':True,'recorded_at':now(),'owner_job':owner,'base_image':BASE,
        'image':image,'fixture_label':LABEL,'builder_started':False,'builder_device_count':0,'network':'none',
        'change':'/usr/local/bin/cc symlink to existing BiSheng/Clang 15; base image cc remains GCC 11',
        'scope':'Compiler-selection fixture, not a rebuilt Triton/NPU-IR/CANN distribution'})
    print(json.dumps({'passed':True,'owner_job':owner,'image':image,'builder_started':False}),flush=True)
finally:
    if created:
        info=json.loads(docker(['inspect',name]))[0]
        assert info['Config']['Labels'][LABEL]==owner and info['State']['Status']=='created' and info['State']['Pid']==0
        docker(['rm',name])

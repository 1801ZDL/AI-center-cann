"""Actual public Triton offline compilation on a private native quota cache.

This is a bounded prerequisite for managed-node toolchain acceptance. It uses
the current provider, fixed immutable image, real UID actors and actual compiler
cache/stage observations. It does not grant central environment/NPU credentials.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import sys
import time
import uuid

IMAGE='sha256:820dc486ebd3931addfaa9cf867a965534b15b78351d945a7119cb8bf3cfcf57'


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024**2),b''):h.update(part)
    return h.hexdigest()


def actor():
    global triton,tl
    assert os.getuid() in {23811,23812}
    status={k:v.strip() for k,sep,v in (s.partition(':') for s in Path('/proc/self/status').read_text().splitlines()) if sep}
    assert status['CapEff']==status['CapPrm']=='0000000000000000' and status['NoNewPrivs']=='1'
    assert not list(Path('/dev').glob('davinci*')) and not Path('/var/run/docker.sock').exists()
    project=Path('/work/project');project.mkdir(mode=0o700)
    (project/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(lab_native_cache LANGUAGES CXX)\nadd_library(calc STATIC calc.cpp)\nadd_executable(answer main.cpp)\ntarget_link_libraries(answer PRIVATE calc)\n')
    (project/'calc.cpp').write_text('int calc(int a,int b){return a+b;}\n')
    (project/'main.cpp').write_text('#include <cstdio>\nextern int calc(int,int);int main(){int n=calc(20,22);std::printf("%d\\n",n);return n==42?0:1;}\n')
    env=dict(os.environ);env.update(CCACHE_DIR='/cache/ccache',CCACHE_BASEDIR=str(project),CCACHE_COMPILERCHECK='content',CCACHE_MAXSIZE='64M')
    build=Path('/work/build');commands=[]
    def command(argv):
        try:p=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
        except OSError as error:
            commands.append({'argv':argv,'errno':error.errno,'error':str(error)})
            Path('/work/cmake-commands.json').write_text(json.dumps(commands,indent=2)+'\n');raise
        commands.append({'argv':argv,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        Path('/work/cmake-commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        assert p.returncode==0,commands[-1]
        return p.stdout
    command(['cmake','-S',str(project),'-B',str(build),'-G','Ninja','-DCMAKE_BUILD_TYPE=Release','-DCMAKE_CXX_COMPILER_LAUNCHER=ccache'])
    command(['ccache','--zero-stats']);command(['cmake','--build',str(build),'--parallel','1'])
    assert command([str(build/'answer')])=='42\n'
    stats={key:int(value) for line in command(['ccache','--print-stats']).splitlines() if len((parts:=line.split()))==2 for key,value in [parts]}
    cmake={'passed':True,'compiler_launcher':'ccache','cold_cache_misses':stats.get('cache_miss',0),
        'cache_hits':sum(stats.get(k,0) for k in ['direct_cache_hit','preprocessed_cache_hit']),
        'ccache_version':command(['ccache','--version']).splitlines()[0],'stats':stats,'binary_sha256':digest(build/'answer'),
        'fresh_build_directory':True,'answer':42,'commands':commands,'scope':'Actual two-translation-unit CMake/Ninja C++ build; not full Triton/NPU-IR build cache acceptance'}
    Path('/work/cmake-cache.json').write_text(json.dumps(cmake,indent=2)+'\n')
    import triton
    import triton.language as tl
    from triton.backends.compiler import GPUTarget
    from triton.compiler.compiler import ASTSource,make_backend
    import torch
    guarded_queries=[]
    def device_query(*args,**kwargs):
        guarded_queries.append(True)
        raise AssertionError('Offline compilation attempted a forbidden live device query')
    torch.npu.current_device=device_query;torch.npu.get_device_properties=device_query
    target=GPUTarget('npu','Ascend950PR_9579',0)
    AscendBackend=type(make_backend(target))
    @triton.jit
    def add(X,Y,Z,N:tl.constexpr,B:tl.constexpr):
        i=tl.program_id(0)*B+tl.arange(0,B)
        a=tl.load(X+i,i<N,0);b=tl.load(Y+i,i<N,0)
        tl.store(Z+i,a+b,i<N)
    calls=[];original=AscendBackend.add_stages
    def stages(self,mapping,*args,**kwargs):
        original(self,mapping,*args,**kwargs)
        for name,fn in list(mapping.items()):
            def wrapped(*a,_name=name,_fn=fn,**kw):
                calls.append(_name);return _fn(*a,**kw)
            mapping[name]=wrapped
    AscendBackend.add_stages=stages
    tools={};native_run=subprocess.run
    def observed_run(argv,*args,**kwargs):
        if isinstance(argv,(list,tuple)) and argv and isinstance(argv[0],(str,os.PathLike)):
            executable=str(argv[0]);resolved=shutil.which(executable) if not Path(executable).is_absolute() else executable
            if resolved and Path(resolved).is_file():
                path=str(Path(resolved).resolve())
                if path not in tools:
                    tools[path]={'path':path,'sha256':digest(path)}
                    Path('/work/tool-invocations.json').write_text(json.dumps(list(tools.values()),indent=2)+'\n')
        return native_run(argv,*args,**kwargs)
    subprocess.run=observed_run
    def contents():
        return {str(p.relative_to('/cache')):{'bytes':p.stat().st_size,'sha256':digest(p),'mtime_ns':p.stat().st_mtime_ns}
                for p in Path('/cache').rglob('*') if p.is_file() and not p.is_symlink()}
    before=contents();started=time.monotonic()
    compiled=triton.compile(ASTSource(add,{'X':'*fp32','Y':'*fp32','Z':'*fp32'},constexprs={'N':64,'B':64}),
        target=target,options={'enable_dynamic_cv_pipeline':False,'debug':False})
    binary=compiled.asm['npubin'];assert binary[:4]==b'\x7fELF'
    assert compiled.module is None and compiled.function is None
    assert not guarded_queries
    after=contents();assert after
    report={'uid':os.getuid(),'cap_effective':status['CapEff'],'no_new_privileges':True,
        'triton_version':triton.__version__,'triton_file':triton.__file__,'backend_sha256':digest(sys.modules[AscendBackend.__module__].__file__),
        'stages_called':calls,'binary_bytes':len(binary),'binary_sha256':hashlib.sha256(binary).hexdigest(),
        'cache_files_before':len(before),'cache_files_after':len(after),'cache_bytes':sum(v['bytes'] for v in after.values()),
        'cache_manifest_sha256':hashlib.sha256(json.dumps(after,sort_keys=True).encode()).hexdigest(),
        'warm_files_unchanged':bool(before) and before==after,'elapsed_seconds':time.monotonic()-started,
        'npu_devices':0,'kernel_loaded':False,'kernel_launches':0,'stages_in_result':list(compiled.asm),
        'actual_external_tools':list(tools.values()),
        'live_torch_device_query_guard':True,'guarded_device_calls':len(guarded_queries),
        'backend_copy_compatibility':json.loads(Path('/opt/ascend-lab-offline-compat.json').read_text()) if Path('/opt/ascend-lab-offline-compat.json').exists() else None,
        'source':'Actual public triton.compile in the explicitly audited private CPU compatibility image; stage wrappers count original compilation functions'}
    report['cmake_cache']={k:v for k,v in cmake.items() if k not in {'commands'}}
    Path('/work/report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


def controller(parent_id,attempt_id,image):
    assert os.geteuid()==0;sys.path.insert(0,'/payload')
    from quota_workspace import prepare
    from persistent_cache import Pool,Lease,namespace,fingerprint
    from environment_contract import fingerprint as env_fingerprint
    from workflow_spec import runtime_manifest
    from lab_client import atomic_json
    from filesystem_quota import require
    state=Path('/state/control');state.mkdir(mode=0o700)
    quotas=[{'uid':u,'bytes':96*1024**2,'inodes':2048} for u in [23811,23812]]
    Path('/quota').chmod(0o700);policy=prepare('/quota',quotas)['policy'];slots={s['quota_uid']:s for s in policy['slots']}
    tool=str(Path(shutil.which('bishengir-compile')).resolve())
    setup='/usr/local/Ascend/ascend-toolkit/set_env.sh';assert Path(setup).is_file()
    packages={p:importlib.metadata.version(p) for p in ['torch','torch-npu']}
    for package in ['triton-ascend','triton']:
        try:packages[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:pass
    observed={'image_sha256':image,'python':platform.python_version(),'python_executable':sys.executable,'machine':platform.machine(),
        'packages':packages,
        'tools':{'bishengir-compile':{'realpath':tool,'sha256':digest(tool)}},
        'files':{'/opt/ascend-lab-offline-compat.json':{'sha256':digest('/opt/ascend-lab-offline-compat.json')}} if Path('/opt/ascend-lab-offline-compat.json').exists() else {}}
    config={'node_id':'worker-'+'c'*12,'physical_id':'d'*64,'image':image,'python':sys.executable,'environment_setup':setup,
        'users':{'alice':{'uid':23811,'gid':23811},'bob':{'uid':23812,'gid':23812}},'persistent_cache':policy,
        'environment_manifest':{'format':'ascend-lab-node-environment/v1','image_sha256':image,'environment_setup':setup,'observed':observed,
            'source':{'job_id':parent_id,'attempt_id':attempt_id,'receipt_sha256':digest('/payload/probe.py'),'node_revision':1}}}
    pool=Pool(state,config);pool.initialize();runtime=runtime_manifest()
    def grant(owner,number,environment=None):
        uid=config['users'][owner]['uid'];slot=slots[uid]
        g={'owner':owner,'job_id':'job-'+format(number,'012x'),'attempt_id':'attempt-'+format(number,'032x'),
            'lease_token':'fixed-native-triton-cache-'+str(number),'environment_sha256':env_fingerprint(config['environment_manifest']),
            'runtime_files':runtime,'parameters':{'lock':{'python':platform.python_version(),'tools':{'bishengir-compile':digest(tool)}},'environment':environment or {}},
            'cache_policy_sha256':fingerprint(policy),'allocation':{}}
        g['allocation']['cache']={'slot_id':slot['id'],'storage_id':slot['storage_id'],'key':namespace(config,g)}
        return g
    records=[]
    def run(g):
        lease=Lease(pool,g);identity=lease.prepare();uid=config['users'][g['owner']]['uid']
        work=Path('/work');os.chown(work,0,0);os.chmod(work,0o700)
        for p in work.iterdir():
            if p.is_dir():shutil.rmtree(p)
            else:p.unlink()
        (work/'tmp').mkdir(mode=0o700);os.chown(work/'tmp',uid,uid)
        os.chown(work,uid,uid)
        subprocess.run(['mount','--bind',str(lease.path),'/cache'],check=True)
        try:
            def drop():
                os.setgroups([]);os.setgid(uid);os.setuid(uid);os.umask(0o077)
                resource.setrlimit(resource.RLIMIT_CORE,(0,0))
            env=dict(os.environ);env.update(HOME='/work',USER=g['owner'],LOGNAME=g['owner'],TMPDIR='/work/tmp',TRITON_CACHE_DIR='/cache/triton',
                TRITON_DUMP_DIR='/work/dump',TRITON_OVERRIDE_DIR='/work/override',TRITON_COMPILE_ONLY='1',
                OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',**g['parameters']['environment'])
            result=subprocess.run([sys.executable,'-I','-B','/payload/probe.py','--actor'],env=env,cwd='/work',preexec_fn=drop,
                capture_output=True,text=True,timeout=90)
            output=Path('/proof/actor-'+str(len(records)));output.mkdir(mode=0o700)
            (output/'stdout.log').write_text(result.stdout);(output/'stderr.log').write_text(result.stderr)
            if (work/'cmake-cache.json').exists():shutil.copyfile(work/'cmake-cache.json',output/'cmake-cache.json')
            if (work/'cmake-commands.json').exists():shutil.copyfile(work/'cmake-commands.json',output/'cmake-commands.json')
            if (work/'tool-invocations.json').exists():shutil.copyfile(work/'tool-invocations.json',output/'tool-invocations.json')
            host_uid=int(os.environ['LAB_PROOF_UID']);host_gid=int(os.environ['LAB_PROOF_GID'])
            for path in output.iterdir():path.chmod(0o600);os.chown(path,host_uid,host_gid)
            os.chown(output,host_uid,host_gid)
            if result.returncode:raise RuntimeError('Actual Triton compile actor failed: '+result.stderr[-3000:])
            report=json.loads((work/'report.json').read_text());report.update(owner=g['owner'],key=lease.key)
            records.append(report)
            with pool.anchor(slots[uid]) as (fd,_):
                quota=require(fd,uid,96*1024**2,2048)
                report['quota_bytes_used']=quota['bytes_used'];report['quota_inodes_used']=quota['inodes_used']
            return report
        finally:
            subprocess.run(['umount','/cache'],check=True);lease.release(True)
    cold=run(grant('alice',1));assert cold['stages_called'] and cold['binary_bytes']>0 and cold['cmake_cache']['cold_cache_misses']>=2
    warm=run(grant('alice',2));assert not warm['stages_called'] and warm['warm_files_unchanged'] and warm['binary_sha256']==cold['binary_sha256']
    assert warm['cmake_cache']['cache_hits']>=2 and warm['cmake_cache']['cold_cache_misses']==0
    assert warm['key']==cold['key']
    variant=run(grant('alice',3,{'TRITON_PRINT_AUTOTUNING':'1'}));assert variant['key']!=cold['key'] and variant['stages_called'] and variant['cmake_cache']['cold_cache_misses']>=2
    peer=run(grant('bob',4));assert peer['uid']!=cold['uid'] and peer['key']!=cold['key'] and peer['stages_called'] and peer['cmake_cache']['cold_cache_misses']>=2
    again=run(grant('alice',5));assert again['key']==cold['key'] and not again['stages_called'] and again['warm_files_unchanged']
    assert again['binary_sha256']==cold['binary_sha256']
    assert again['cmake_cache']['cache_hits']>=2 and again['cmake_cache']['cold_cache_misses']==0
    atomic_json(Path('/state/triton-cache.json'),{'passed':True,'image':image,'records':records,'fixture_environment':observed,
        'cold_compile_observed':True,'warm_zero_compiler_stages':True,'warm_files_unchanged':True,
        'changed_environment_cold_namespace':True,'different_uid_cold_namespace':True,'original_namespace_retained':True,
        'actual_public_triton_compile':True,'kernel_launches':0,'central_dispatch_integrated':False,
        'actual_cmake_ccache_fresh_build_reuse':True,
        'scope':'Actual DSL to ELF and cross-process Triton cache under current native UID-quota provider; fixture-measured environment, not central-protected managed-node certification or device correctness'})


def parent(compiled_image=False,offline_compatible=False):
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'))
    from lab_client import read_json,atomic_json,now
    from disk_workspace_fixture import OwnedDisk
    from docker_runtime import discover,command
    from workflow_spec import runtime_manifest
    assert not offline_compatible or compiled_image
    filename='offline-image-id' if offline_compatible else 'runtime-image-id'
    image=(root/'runtime/toolchain-cache-20261030'/filename).read_text().strip() if compiled_image else IMAGE
    import re
    assert re.fullmatch(r'sha256:[0-9a-f]{64}',image)
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json');assert job['resources']['npu']==0
    base=directory/'tmp/triton-cache';base.mkdir(mode=0o700);payload=base/'payload';payload.mkdir(mode=0o755)
    proof=base/'actor-proof';proof.mkdir(mode=0o700)
    names=['persistent_cache.py','workspace_slots.py','quota_workspace.py','filesystem_quota.py','lab_client.py','environment_contract.py','workflow_spec.py',*runtime_manifest()]
    for source,name in [(Path(__file__),'probe.py'),*((root/n,n) for n in sorted(set(names)))]:
        target=payload/name;target.write_bytes(source.read_bytes());target.chmod(0o444)
    payload.chmod(0o555);binding,_=discover();cpus=job['allocation']['cpu_ids']
    def docker(args,check=True,input=None,timeout=30):
        argv,env=command(binding,['docker',*args]);result=subprocess.run(argv,env=env,capture_output=True,text=True,input=input,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Owned Triton cache operation failed: '+result.stderr[-2000:])
        return result
    prefix='ascend-lab-triton-cache-'+uuid.uuid4().hex[:8]
    helper_image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    disk=OwnedDisk(base,prefix,helper_image,cpus,docker,folder_name='cache-workspace',image_mib=512,inodes=16384,quota_type='user')
    volume=prefix+'-volume';name=prefix+'-control';volume_created=False;container_created=False;report=None
    try:
        device=disk.create();docker(['volume','create','--label','io.ascend-lab.toolchain-cache='+prefix,'--driver','local',
            '--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev,usrquota',volume]);volume_created=True
        docker(['create','--name',name,'--label','io.ascend-lab.toolchain-cache='+prefix,'--user','0:0','--network','none','--ipc','none',
            '--read-only','--cap-drop','ALL','--cap-add','SYS_ADMIN','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','SETUID','--cap-add','SETGID',
            '--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined','--security-opt','apparmor=unconfined',
            '--memory','1792m','--memory-swap','1792m','--cpus','1.75','--cpuset-cpus',','.join(map(str,cpus)),'--pids-limit','128',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=32m,mode=1777','--tmpfs','/state:rw,nosuid,nodev,size=16m,mode=0700',
            '--tmpfs','/work:rw,nosuid,nodev,exec,size=128m,mode=0700','--tmpfs','/cache:rw,nosuid,nodev,size=1m,mode=0700',
            '--log-opt','max-size=1m','--log-opt','max-file=1',
            '--env','LAB_PROOF_UID='+str(os.getuid()),'--env','LAB_PROOF_GID='+str(os.getgid()),
            '--mount','type=volume,src='+volume+',dst=/quota','--mount','type=bind,src='+str(payload)+',dst=/payload,readonly',
            '--mount','type=bind,src='+str(proof)+',dst=/proof',
            '--entrypoint','/bin/bash',image,'--noprofile','--norc','-c',
            'source /usr/local/Ascend/ascend-toolkit/set_env.sh >/dev/null 2>&1 || exit 126; exec /usr/bin/timeout --kill-after=3s 360s /usr/local/python3.11.15/bin/python3.11 -I -B /payload/probe.py --controller --parent-id "$1" --attempt-id "$2" --image "$3"',
            'native-triton-cache',job['id'],job['attempt_id'],image]);container_created=True
        result=docker(['start','--attach',name],check=False,timeout=370)
        (base/'control-stdout.log').write_text(result.stdout);(base/'control-stderr.log').write_text(result.stderr)
        info=json.loads(docker(['inspect',name]).stdout)[0];assert not info['State']['Running'] and info['State']['Pid']==0
        # Actor logs were published to this job's bound proof directory before
        # raising, and survive the stopped fixture's tmpfs disappearance.
        if result.returncode or info['State']['ExitCode']:
            atomic_json(directory/'results/triton-cache-failure.json',{'image':image,'exit_code':info['State']['ExitCode'],'oom_killed':info['State']['OOMKilled'],'stderr':result.stderr[-5000:]})
            raise RuntimeError('Actual Triton cache control failed: '+result.stderr[-2400:])
        report=json.loads(result.stdout);assert report['passed']
    finally:
        if container_created:
            info=json.loads(docker(['inspect',name]).stdout)[0];assert info['Config']['Labels']['io.ascend-lab.toolchain-cache']==prefix
            if info['State']['Running']:docker(['stop','--time','3',name])
            docker(['rm',name])
        if volume_created:docker(['volume','rm',volume])
        disk.cleanup()
    assert report is not None
    report.update(parent_job=job['id'],verified_at=now(),disk=disk.report,cleanup_confirmed=disk.report['loop_association_released'] and disk.report['helper_removed'])
    atomic_json(directory/'results/triton-cache.json',report)
    print(json.dumps({'passed':True,'actors':len(report['records']),'npu_launches':0,'cleanup_confirmed':report['cleanup_confirmed']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--actor',action='store_true');parser.add_argument('--controller',action='store_true');parser.add_argument('--compiled-image',action='store_true');parser.add_argument('--offline-compatible',action='store_true');parser.add_argument('--image');parser.add_argument('--parent-id');parser.add_argument('--attempt-id');args=parser.parse_args()
    if args.actor:actor()
    elif args.controller:
        controller(args.parent_id,args.attempt_id,args.image)
        print(Path('/state/triton-cache.json').read_text(),flush=True)
    else:parent(args.compiled_image,args.offline_compatible)

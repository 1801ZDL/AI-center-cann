"""Queue-only frozen upstream sources and immutable compiler image preparation.

Original worktrees/LLVM installation are read-only inputs. No Docker commit of
an existing container, no driver/device mount and no shared package install.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import atomic_json,read_json,now
from docker_runtime import discover,command

assert os.environ.get('LAB_JOB_DIR'),'Submit this preparation through the queue'
parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['freeze','image','image-resume']);args=parser.parse_args()
base=ROOT/'runtime/full-build-20261030'
repo=Path('/home/m00951283/triton-ascend_main_recheck_20261019')
upstream='a176ab28b11b63d427760c15f6cd63c14ac296f1'
npuir='bb13ef6120526aa3df6f950006af5ec0e585d2f6'
base_image='sha256:1fe13f7f9afa32d7ec13ec9ba42b38f7ed05cfd284e19e705ee54ca992162704'
payload_wheel=repo/'dist/triton_ascend-3.6.0+gita176ab28-cp311-cp311-linux_x86_64.whl'

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()

def git(path,*arguments):
    result=subprocess.run(['git','-c','safe.directory=*','-c','pack.threads=1','-C',str(path),*arguments],
                          capture_output=True,text=True,check=True,timeout=180)
    return result.stdout.strip()

def freeze(path,source,commit):
    path.mkdir(mode=0o700,parents=True)
    git(path,'init','--quiet')
    git(path,'fetch','--quiet','--depth=1',str(source),commit)
    git(path,'checkout','--quiet','--detach','FETCH_HEAD')
    assert git(path,'rev-parse','HEAD')==commit
    assert not (path/'.git/objects/info/alternates').exists()

if args.phase=='freeze':
    base.mkdir(mode=0o700)
    origin_status=git(repo,'status','--porcelain=v1');source=base/'source'
    freeze(source,repo,upstream)
    sub=source/'third_party/ascend/AscendNPU-IR'
    if sub.exists():sub.rmdir()
    freeze(sub,repo/'third_party/ascend/AscendNPU-IR',npuir)
    assert git(source,'ls-tree','HEAD','third_party/ascend/AscendNPU-IR').split()[2]==npuir
    archive=base/'source.tar.gz'
    with tarfile.open(archive,'w:gz',compresslevel=1) as tar:tar.add(source,arcname='source')
    assert git(repo,'status','--porcelain=v1')==origin_status
    report={'passed':True,'recorded_at':now(),'upstream_commit':upstream,'npuir_commit':npuir,
            'archive':str(archive),'archive_bytes':archive.stat().st_size,'archive_sha256':digest(archive),
            'source_worktree_status_sha256':hashlib.sha256(origin_status.encode()).hexdigest(),
            'original_worktree_unchanged':True,'source_git_objects_independent':True,'dirty_original_edits_included':False}
    atomic_json(base/'source.json',report)
else:
    assert read_json(base/'source.json')['upstream_commit']==upstream
    context=base/'image-context';context.mkdir(mode=0o700,exist_ok=args.phase=='image-resume')
    if args.phase=='image':
        llvm_source=Path('/home/h00951476/llvm-install');llvm=context/'llvm';llvm.mkdir(mode=0o700)
        before={str(p.relative_to(llvm_source)):(p.lstat().st_size,p.lstat().st_mtime_ns,p.lstat().st_mode)
                for p in llvm_source.rglob('*') if p.is_file() or p.is_symlink()}
        files=[];total=0
        for name in sorted(before):
            p=llvm_source/name;q=llvm/name;q.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            if p.is_symlink():
                if not p.resolve().is_relative_to(llvm_source):raise RuntimeError('External SDK link needs explicit materialization')
                q.symlink_to(os.readlink(p));continue
            if total+p.stat().st_size>64*1024**3:raise RuntimeError('LLVM snapshot exceeds bounded 64GiB preparation budget')
            shutil.copyfile(p,q);q.chmod(0o755 if p.stat().st_mode&0o111 else 0o644)
            files.append({'name':name,'bytes':q.stat().st_size,'sha256':digest(q)});total+=q.stat().st_size
            if len(files)%1000==0:print('Frozen LLVM files:',len(files),'bytes:',total,flush=True)
        after={str(p.relative_to(llvm_source)):(p.lstat().st_size,p.lstat().st_mtime_ns,p.lstat().st_mode)
               for p in llvm_source.rglob('*') if p.is_file() or p.is_symlink()}
        assert before==after,'Shared LLVM changed while copying; do not build from this snapshot'
        llvm.chmod(0o755)
        atomic_json(context/'llvm-manifest.json',{'files':files,'bytes':total})
        bisheng=context/'bishengir';bisheng.mkdir(mode=0o755)
        prefix='triton/backends/ascend/bishengir/';payload=[]
        with zipfile.ZipFile(payload_wheel) as z:
            for entry in z.infolist():
                if not entry.filename.startswith(prefix) or entry.is_dir():continue
                relative=entry.filename[len(prefix):]
                if not relative or '..' in Path(relative).parts or Path(relative).is_absolute():raise RuntimeError('Unsafe dependency wheel member')
                path=bisheng/relative;path.parent.mkdir(parents=True,mode=0o755,exist_ok=True)
                with z.open(entry) as stream,path.open('wb') as target:shutil.copyfileobj(stream,target,1024*1024)
                mode=(entry.external_attr>>16)&0o777;path.chmod(0o755 if mode&0o111 else 0o644)
                payload.append({'name':relative,'bytes':entry.file_size,'sha256':digest(path)})
        atomic_json(context/'bishengir-manifest.json',{'source_wheel_sha256':digest(payload_wheel),'files':payload})
    else:
        llvm=context/'llvm';bisheng=context/'bishengir'
        total=read_json(context/'llvm-manifest.json')['bytes'];payload=read_json(context/'bishengir-manifest.json')['files']
        assert llvm.is_dir() and bisheng.is_dir() and total>0 and payload
    dependencies=context/'dependencies';dependencies.mkdir(mode=0o755,exist_ok=args.phase=='image-resume')
    requirement=['setuptools==75.8.2','wheel==0.45.1','cmake==3.31.6','ninja==1.13.2','pybind11==2.13.6',
                 'packaging==24.2','attrs==24.2.0','numpy==1.26.4','scipy==1.13.1','decorator==5.1.1',
                 'psutil==6.0.0','pytest==8.3.5','pytest-xdist==3.6.1','pyyaml==6.0.2','pandas==2.2.3','pyelftools==0.32']
    subprocess.run([str(ROOT/'.qa-venv/bin/python'),'-I','-m','pip','--isolated','download','--index-url','https://pypi.org/simple',
                    '--dest',str(dependencies),'--python-version','311','--implementation','cp','--abi','cp311','--abi','abi3',
                    '--platform','manylinux2014_x86_64','--only-binary=:all:',*requirement],check=True,timeout=300)
    packages=[{'file':p.name,'sha256':digest(p)} for p in sorted(dependencies.glob('*.whl'))]
    atomic_json(context/'dependencies.json',{'requirements':requirement,'wheels':packages})
    for top in [llvm,bisheng,dependencies]:
        top.chmod(0o755)
        for path in top.rglob('*'):
            if path.is_dir() and not path.is_symlink():path.chmod(0o755)
    for name in ['llvm-manifest.json','bishengir-manifest.json','dependencies.json']:(context/name).chmod(0o644)
    (context/'Dockerfile').write_text('FROM '+base_image+'\nLABEL io.ascend-lab.fullbuild="20261030"\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends git binutils && rm -rf /var/lib/apt/lists/*\n'
        'COPY llvm /opt/lab-llvm\nCOPY llvm-manifest.json /opt/lab-llvm-manifest.json\n'
        'COPY bishengir /opt/lab-bishengir\nCOPY bishengir-manifest.json /opt/lab-bishengir-manifest.json\n'
        'COPY dependencies /opt/lab-dependencies\nCOPY dependencies.json /opt/lab-dependencies.json\n'
        'RUN /usr/local/python3.11.15/bin/python3.11 -m venv /opt/lab-build-venv && '
        '/opt/lab-build-venv/bin/python -m pip install --no-index --find-links=/opt/lab-dependencies '+' '.join(requirement)+' && '
        'rm -rf /opt/lab-dependencies\nENV PATH="/opt/lab-build-venv/bin:/opt/lab-llvm/bin:/usr/local/python3.11.15/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
        'ENV LLVM_SYSPATH="/opt/lab-llvm" TRITON_ASCEND_BISHENGIR_PATH="/opt/lab-bishengir"\n')
    binding,_=discover();cpus=','.join(map(str,sorted(os.sched_getaffinity(0))))
    argv,env=command(binding,['docker','build','--pull=false','--network','default','--memory','1536m','--memory-swap','1536m',
                            '--cpuset-cpus',cpus,'--iidfile',str(base/'image-id'),str(context)])
    env['DOCKER_BUILDKIT']='0'
    subprocess.run(argv,env=env,check=True,timeout=1200)
    image=(base/'image-id').read_text().strip();assert image.startswith('sha256:') and len(image)==71
    report={'passed':True,'recorded_at':now(),'image':image,'base_image':base_image,'llvm_bytes':total,
            'llvm_manifest_sha256':digest(context/'llvm-manifest.json'),'bishengir_files':len(payload),
            'bishengir_manifest_sha256':digest(context/'bishengir-manifest.json'),'dependencies':packages,
            'npu_kernel_launches':0,'source_worktrees_modified':False,'shared_installs_modified':False,
            'scope':'Frozen external LLVM/BishengIR dependencies; not rebuilding those dependencies'}
    atomic_json(base/'image.json',report)

atomic_json(Path(os.environ['LAB_JOB_DIR'])/('results/full-build-'+args.phase+'.json'),report)
print(json.dumps(report,ensure_ascii=False,indent=2))

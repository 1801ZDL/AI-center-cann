"""Queue-only full source build and 20-owner mixed workload on real UID quotas.

Uses only owned time-bounded Root role containers/file-backed ext4 volumes.
External LLVM/BishengIR are image dependencies, not rebuilt; no NPU is mounted.
All submission handles and control state stay private in the outer job folder.
"""
import copy
import base64
import csv
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from lab_client import Client,ClientError,atomic_json,read_json,now
from deployment import bundle,unpack_release,digest
from backends import physical_identity
from disk_workspace_fixture import OwnedDisk
from docker_runtime import discover,command as docker_command

ROLE_IMAGE='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
TASK_IMAGE='sha256:be46b265fa5b5ffb8d8358e82a5b628f2b045dd9563761cdedc8bd7d022519e8'
TERMINAL={'succeeded','failed','cancelled','timed_out'}
MIB=1024**2


class Acceptance:
    def __init__(self):
        self.outer=Path(os.environ['LAB_JOB_DIR']);assert self.outer.is_relative_to(ROOT/'runtime/jobs')
        self.cpus=sorted(os.sched_getaffinity(0));assert len(self.cpus)==2
        self.base=self.outer/'tmp/large-mixed';self.base.mkdir(mode=0o700)
        self.end=time.monotonic()+10300;self.started=time.monotonic()
        self.prefix='ascend-lab-mixed-'+uuid.uuid4().hex[:8]
        self.controller=self.prefix+'-controller';self.node=self.prefix+'-node'
        self.controller_state=self.base/'controller-state';self.node_parent=self.base/'node-control';self.node_state=self.node_parent/'state'
        self.controller_state.mkdir(mode=0o700);self.node_state.mkdir(parents=True,mode=0o700)
        self.binding,_=discover();self.roles=[];self.volumes=[];self.disks=[];self.paths={};self.policies={}
        self.clients=[];self.jobs=[];self.samples=[];self.results={};self.safe_cleanup=True
        self.state={'prefix':self.prefix,'started_at':now(),'phase':'preparing','jobs':self.jobs,'disks':[],
                    'outer_job':self.outer.name,'physical_id':physical_identity(),'controller_state':str(self.controller_state),
                    'node_state':str(self.node_state),'cpu_ids':self.cpus,'task_image':TASK_IMAGE}
        self.release=self.base/'server.tar.gz';self.release_info=bundle(self.release)
        self.bootstrap,_=unpack_release(self.release,self.base)
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));self.port=probe.getsockname()[1]
        self.url='http://127.0.0.1:'+str(self.port);self.state['controller_url']=self.url;self.save()

    def save(self):atomic_json(self.base/'control.json',self.state)
    def phase(self,value):self.state['phase']=value;self.save();print('PHASE',value,flush=True)

    def docker(self,args,check=True,input=None,timeout=45):
        argv,env=docker_command(self.binding,['docker',*map(str,args)])
        r=subprocess.run(argv,env=env,input=input,capture_output=True,text=True,timeout=timeout)
        if check and r.returncode:raise RuntimeError('Owned Docker operation failed: '+r.stderr[-1000:])
        return r

    def py(self,container,code,*args,input=None):
        return self.docker(['exec',*(['-i'] if input is not None else []),container,'/usr/bin/python3','-I','-c',code,*args],input=input)

    def read(self,container,path):
        return json.loads(self.py(container,'import sys;from pathlib import Path;print(Path(sys.argv[1]).read_text())',str(path)).stdout)

    def write(self,container,path,value):
        self.py(container,'import sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(sys.stdin.read());p.chmod(0o600)',str(path),input=json.dumps(value))

    def ctl(self,role,action,*args):
        container=self.controller if role=='controller' else self.node
        r=self.docker(['exec',container,'/usr/bin/python3','-B','/bootstrap/servicectl',action,'--prefix','/opt/ascend-lab-'+role,*args])
        try:return json.loads(r.stdout)
        except ValueError:return None

    def admin(self,*args):return self.ctl('controller','admin','--',*args)

    def disk(self,label,folder,mib,inodes,quota=None):
        fixture=OwnedDisk(self.base,self.prefix+'-'+label,ROLE_IMAGE,self.cpus,self.docker,
                          folder_name=folder,image_mib=mib,inodes=inodes,quota_type=quota,helper_seconds=10800)
        self.disks.append(fixture);device=fixture.create();name=self.prefix+'-'+label
        self.docker(['volume','create','--label','io.ascend-lab.mixedproof='+self.prefix,'--driver','local',
                     '--opt','type=ext4','--opt','device='+device,'--opt','o=nosuid,nodev'+
                     (',usrquota' if quota=='user' else ',grpquota' if quota=='group' else ''),name])
        self.volumes.append(name);self.paths[label]=json.loads(self.docker(['volume','inspect',name]).stdout)[0]['Mountpoint']
        self.state['disks'].append({'label':label,'volume':name,'mountpoint':self.paths[label],**fixture.report});self.save()
        return name

    def roles_start(self,mounts):
        for role,name,state in [('controller',self.controller,self.controller_state),('node',self.node,self.node_state)]:
            self.roles.append(name);self.state['roles']=self.roles;self.save()
            args=['run','--detach','--pull','never','--name',name,'--label','io.ascend-lab.mixedproof='+self.prefix,
                  '--network','host','--security-opt','no-new-privileges:true','--security-opt','seccomp=unconfined',
                  '--security-opt','apparmor=unconfined','--cap-drop','ALL',
                  '--memory','512m','--memory-swap','512m','--cpus','0.25','--cpuset-cpus',','.join(map(str,self.cpus)),
                  '--pids-limit','256','--log-opt','max-size=1m','--log-opt','max-file=1']
            for cap in ['CHOWN','DAC_OVERRIDE','FOWNER','SETUID','SETGID','SYS_ADMIN']:args+=['--cap-add',cap]
            for source,target in [(self.bootstrap,'/bootstrap'),(self.release,'/provision/server.tar.gz'),('/etc/machine-id','/etc/machine-id')]:
                args+=['--mount','type=bind,src='+str(source)+',dst='+str(target)+',readonly']
            parent=self.node_parent if role=='node' else state
            args+=['--mount','type=bind,src='+str(parent)+',dst='+str(parent)]
            if role=='node':
                args+=['--mount','type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
                       '--mount','type=bind,src='+shutil.which('docker')+',dst=/usr/bin/docker,readonly']
                for label in ['work','cache','spool']:args+=['--mount','type=volume,src='+mounts[label]+',dst='+self.paths[label]+',volume-nocopy']
            else:args+=['--mount','type=volume,src='+mounts['payload']+',dst=/srv/lab-payload,volume-nocopy']
            args+=['--entrypoint','/usr/bin/timeout',ROLE_IMAGE,'--kill-after=5s','10680s','/usr/bin/python3','-I','-c','import time;time.sleep(10670)']
            self.docker(args)
            for path in ([parent,state] if role=='node' else [state]):
                self.py(name,'import os,sys;os.chown(sys.argv[1],0,0);os.chmod(sys.argv[1],0o700)',str(path))

    def filesystem(self,container,path):
        code='''import os,json,sys,uuid
from pathlib import Path
p=Path(sys.argv[1]);assert sorted(x.name for x in p.iterdir())==['lost+found']
(p/'lost+found').rmdir();p.chmod(0o700);s=p.stat();v=os.statvfs(p)
print(json.dumps({'path':str(p),'filesystem':'ext4','storage_id':uuid.uuid4().hex,'device':s.st_dev,'root_inode':s.st_ino,'bytes':v.f_blocks*v.f_frsize,'inodes':v.f_files}))
'''
        return json.loads(self.py(container,code,path).stdout)

    def quota_policy(self,label,large_mib,small_mib,large_inodes):
        limits={'users':[{'uid':23900+i,'bytes':(large_mib if i==0 else small_mib)*MIB,
                          'inodes':large_inodes if i==0 else 2048} for i in range(20)]}
        self.py(self.node,'import os,sys;os.chmod(sys.argv[1],0o700)',self.paths[label])
        self.write(self.node,'/provision/'+label+'-limits.json',limits)
        cmd=['exec',self.node,'/usr/bin/python3','-B','/bootstrap/servicectl','workspace-prepare','--root',self.paths[label],
             '--limits','/provision/'+label+'-limits.json','--output','/provision/'+label+'.json']
        assert json.loads(self.docker(cmd+['--plan']).stdout)['read_only_plan']
        self.docker(cmd);return self.read(self.node,'/provision/'+label+'.json')

    def prepare(self):
        self.phase('allocate-separate-finite-disks')
        assert shutil.disk_usage(self.base).free>64*1024**3
        mounts={}
        for label,folder,mib,inodes,quota in [('work','quota-workspace',32768,524288,'user'),
             ('cache','cache-workspace',8192,262144,'user'),('spool','spool-workspace',4096,524288,None),
             ('payload','central-payload',8192,262144,'group')]:
            mounts[label]=self.disk(label,folder,mib,inodes,quota)
        self.roles_start(mounts)
        self.phase('install-controller-and-provision-twenty-owners')
        payload=self.filesystem(self.controller,'/srv/lab-payload')
        payload['owner_groups']={'load_'+str(i).zfill(2):{'gid':24500+i,'bytes':(4096 if i==0 else 128)*MIB,
                                   'inodes':50000 if i==0 else 2048} for i in range(20)}
        self.write(self.controller,'/provision/payload.json',payload)
        self.docker(['exec',self.controller,'/usr/bin/python3','-B','/bootstrap/servicectl','payload-groups-prepare','--contract','/provision/payload.json'])
        installed=json.loads(self.docker(['exec',self.controller,'/usr/bin/python3','-B','/bootstrap/servicectl','install',
            '--archive','/provision/server.tar.gz','--role','controller','--prefix','/opt/ascend-lab-controller',
            '--state-dir',str(self.controller_state),'--create-service-user','labload','--port',self.port,
            '--payload-config','/provision/payload.json']).stdout)
        assert installed['uid']>0
        config=self.read(self.controller,self.controller_state/'config.json')
        config['scheduler'].update(cpu_ids=self.cpus,memory_mib=10496,shm_mib=0,max_running=2,max_timeout_seconds=7200,
                                   io_limits={'file_mib':2048,'input_mib':2048,'artifact_mib':2048})
        self.write(self.controller,self.controller_state/'config.json',config)
        atomic_json(self.base/'controller-private-config.json',config)
        assert self.ctl('controller','start')['ready']
        self.controller_client=Client({'controller':self.url,'username':config['username'],'password':config['password']})
        for i in range(20):
            owner='load_'+str(i).zfill(2)
            self.admin('accessctl','user-add',owner);self.admin('accessctl','environment-grant',owner,'worker-python')
            self.admin('accessctl','quota',owner,'--max-running','1','--max-queued','4','--cpu','1',
                '--memory-mib','8192' if i==0 else '512','--shm-mib','0','--npu','0',
                '--input-storage-mib','256' if i==0 else '64','--archive-storage-mib','4096' if i==0 else '128')
            personal=self.read(self.controller,self.controller_state/'personas'/owner/'client.json')
            atomic_json(self.base/('client-'+str(i)+'.json'),personal);self.clients.append(Client(personal))
        self.phase('prepare-node-work-cache-and-spool')
        self.policies['work']=self.quota_policy('work',28672,128,200000)
        self.policies['cache']=self.quota_policy('cache',6144,32,100000)
        self.policies['spool']=self.filesystem(self.node,self.paths['spool'])
        from node_storage import requirement
        assert self.policies['spool']['inodes']>=2*requirement({'template':'build_v1'})['inodes']+1024, 'Two workflow slots need two conservative spool inode reservations'
        for key,value in {**self.policies,'io':{'file_mib':2048,'input_mib':2048,'artifact_mib':2048}}.items():
            path=self.controller_state/(key+'.json');self.write(self.controller,path,value)
            # Administrative CLI runs as the installed controller identity,
            # not as the enclosing trusted Root provisioning process.
            self.py(self.controller,'import os,sys;os.chown(sys.argv[1],int(sys.argv[2]),int(sys.argv[3]))',
                    str(path),str(installed['uid']),str(installed['gid']))
        mapping=[]
        for i in range(20):mapping+=['--user-map','load_'+str(i).zfill(2)+':'+str(23900+i)+':'+str(23900+i)]
        args=['workerctl','enroll','--name','full-compiler-mixed','--cpus',','.join(map(str,self.cpus)),
              '--memory-mib','10496','--shm-mib','0','--max-running','2','--max-timeout-seconds','7200',
              '--image',TASK_IMAGE,'--physical-id',physical_identity(),'--controller-url',self.url,
              '--node-state-dir',str(self.node_state),*mapping]
        for key,flag in [('work','workspace'),('cache','cache'),('spool','spool'),('io','io')]:
            args+=['--'+flag+'-config',str(self.controller_state/(key+'.json'))]
        self.enrollment=self.admin(*args);self.state['node_id']=self.enrollment['node_id'];self.save()
        self.admin('workerctl','node-templates',self.enrollment['node_id'],'--template','environment_probe','--template','ir_v1','--template','build_v1')
        config=self.read(self.controller,self.enrollment['config']);config.update(python='/opt/lab-build-venv/bin/python',environment_setup='/opt/lab-build-env.sh')
        self.write(self.node,'/provision/node.json',config)
        atomic_json(self.base/'node-private-config.json',config)
        installed=json.loads(self.docker(['exec',self.node,'/usr/bin/python3','-B','/bootstrap/servicectl','install',
            '--archive','/provision/server.tar.gz','--role','node','--prefix','/opt/ascend-lab-node',
            '--state-dir',str(self.node_state),'--node-config','/provision/node.json','--start']).stdout)
        assert installed['uid']==0 and installed['service']['ready']
        self.phase('certify-actual-build-toolchain')
        files=['/opt/lab-llvm-manifest.json','/opt/lab-bishengir-manifest.json','/opt/lab-toolchain-link-audit.json',
               '/opt/lab-pybind-import-audit.json','/opt/lab-ccache-audit.json','/usr/local/bin/ccache',
               '/opt/lab-bishengir/bin/bishengir-opt','/opt/lab-build-env.sh']
        probe=self.submit(0,'probe',{'template':'environment_probe','environment':'worker-python','parameters':{
            'node_id':self.enrollment['node_id'],'memory_mib':256,'tools':['cmake','ninja','c++','ld.lld','git'],
            'packages':['setuptools','pybind11','triton-ascend','torch','torch-npu'],'files':files}})
        self.wait_all([probe],300);assert self.detail(probe)['job']['state']=='succeeded'
        self.ctl('node','stop');self.admin('workerctl','node-environment',self.enrollment['node_id'],'--from-job',probe['id'],'--expected-revision','1')
        certified=self.read(self.controller,self.enrollment['config']);config=self.read(self.node,self.node_state/'config.json')
        for key in ['image','python','environment_setup','environment_manifest','policy_revision']:config[key]=certified[key]
        self.write(self.node,self.node_state/'config.json',config);assert self.ctl('node','start')['ready']
        atomic_json(self.base/'node-private-config.json',config)
        facts=certified['environment_manifest']['observed']
        self.lock={'python':facts['python'],'image_sha256':TASK_IMAGE,
            'packages':{n:v if v is not None else '__absent__' for n,v in facts['packages'].items()},
            'tools':{n:v['sha256'] for n,v in facts['tools'].items() if v},
            'files':{n:v['sha256'] for n,v in facts['files'].items() if v}}
        assert len(self.lock['files'])==len(files) and facts['python']=='3.11.15'
        self.state['lock']=self.lock;self.state['release']=self.release_info;self.save()

    def submit(self,owner,phase,request):
        request=copy.deepcopy(request);request['idempotency_key']=self.prefix+'-'+str(owner)+'-'+phase
        request['name']='Mixed load '+str(owner).zfill(2)+' '+phase
        intent={'owner':owner,'phase':phase,'key':request['idempotency_key'],'request':request}
        atomic_json(self.base/('intent-'+str(owner)+'-'+phase+'.json'),intent)
        self.safe_cleanup=False
        value=self.clients[owner].request('/api/jobs',request)
        handle={'owner':owner,'phase':phase,'id':value['job']['id']};self.jobs.append(handle);self.save()
        return handle

    def detail(self,handle):return self.clients[handle['owner']].request('/api/jobs/'+handle['id'])

    def wait_all(self,handles,seconds):
        until=min(self.end,time.monotonic()+seconds);last=0;completed={}
        while time.monotonic()<until:
            try:
                details=[]
                for h in handles:
                    d=completed.get(h['id']) or self.detail(h)
                    if d['job']['state'] in TERMINAL:completed[h['id']]=d
                    details.append(d)
            except ClientError:
                # Observation failure is never permission to duplicate a job.
                time.sleep(2);continue
            if all(d['job']['state'] in TERMINAL for d in details):return details
            if time.monotonic()-last>25:
                status=self.controller_client.request('/api/jobs');active=[d['job'] for d in details if d['job']['state'] not in TERMINAL]
                assert len(status['resource_domains'])==1
                for domain in status['resource_domains']:
                    reserved=domain['reserved']
                    assert reserved['cpu']<=2 and reserved['slots']<=2 and reserved['memory_mib']<=10496
                    assert set(reserved['cpu_ids'])<=set(self.cpus) and len(reserved['cpu_ids'])==len(set(reserved['cpu_ids']))
                self.samples.append({'at':now(),'counts':status['counts'],'domains':status['resource_domains'],
                    'active':[{'id':j['id'],'owner':j['owner'],'state':j['state'],'allocation':j['allocation']} for j in active]})
                atomic_json(self.base/'samples.json',self.samples)
                print('OBSERVE',json.dumps({'phase':self.state['phase'],'counts':status['counts']}),flush=True);last=time.monotonic()
            time.sleep(2)
        raise RuntimeError('Observation budget reached; original attempts must be reconciled, not resubmitted')

    def artifact(self,handle,name):
        return self.clients[handle['owner']].request('/api/jobs/'+handle['id']+'/artifact?name='+urllib.parse.quote(name,safe=''))

    def workflow(self,input_id,large=False,mode=None):
        return {'template':'build_v1' if large or mode=='cmake' else 'ir_v1','environment':'worker-python','parameters':{
            'input_id':input_id,'argv':['python','build.py'] if large else ['python','peer.py',mode],
            'lock':self.lock,'resources':{'cpu':1,'memory_mib':8192 if large else 512,'shm_mib':0,'npu':0},
            'timeout_seconds':7200 if large else 300,'workspace':{'min_mib':24576 if large else 96,'min_inodes':50000 if large else 512,'durable':True},
            'cache':{'mode':'persistent','min_free_mib':128 if large else 8,'min_free_inodes':4096 if large else 512},
            'artifacts':{'file_mib':2048 if large else 8,'total_mib':2048 if large else 16}}}

    def workloads(self):
        self.phase('verify-actual-peer-ir-contract-before-large-compilation')
        small_dir=self.base/'peer-input';small_dir.mkdir(mode=0o700)
        shutil.copyfile(ROOT/'tests/workloads/mixed_small_build.py',small_dir/'peer.py')
        check_input=self.clients[1].upload_input(small_dir)
        check_ir=self.submit(1,'peer-ir-contract-preflight',self.workflow(check_input['id'],mode='ir'))
        preflight=self.wait_all([check_ir],360)[0]
        assert preflight['job']['state']=='succeeded', 'Fix the small IR contract before starting a long compiler'
        assert self.artifact(check_ir,'results/peer-report.json')['answer']==42
        self.phase('verify-frozen-source-cmake-configuration-in-separate-cache-namespace')
        source=ROOT/'runtime/full-build-20261030';input_dir=self.base/'large-input';input_dir.mkdir(mode=0o700)
        origin=read_json(source/'source.json');assert digest(source/'source.tar.gz')==origin['archive_sha256']
        shutil.copyfile(source/'source.tar.gz',input_dir/'source.tar.gz');shutil.copyfile(source/'source.json',input_dir/'source.json')
        shutil.copyfile(ROOT/'tests/workloads/full_triton_quota_build.py',input_dir/'build.py')
        sealed=self.clients[0].upload_input(input_dir);large=self.workflow(sealed['id'],large=True)
        configure=copy.deepcopy(large);configure['parameters'].update(argv=['python','build.py','--configure-only'],
            timeout_seconds=300,environment={'TRITON_PRINT_AUTOTUNING':'1'})
        check_config=self.submit(0,'compiler-configuration-preflight',configure)
        configured=self.wait_all([check_config],360)[0]
        assert configured['job']['state']=='succeeded', 'CMake configuration must pass before a full source build'
        config_report=self.artifact(check_config,'results/full-build-provenance.json')
        assert config_report['passed'] and config_report['configure_only'] and not config_report['full_build_accepted']
        self.phase('submit-cold-compiler-and-nineteen-peer-streams')
        cold=self.submit(0,'compiler-cold',large);self.state['cold_job']=cold['id'];self.save()
        peers=[]
        for i in range(1,20):
            item=self.clients[i].upload_input(small_dir)
            for phase,mode in [('ir','ir'),('cmake-cold','cmake'),('cmake-warm','cmake')]:
                peers.append(self.submit(i,phase,self.workflow(item['id'],mode=mode)))
        self.phase('observe-original-cold-and-peer-attempts')
        details=self.wait_all([cold,*peers],7600)
        for handle,detail in zip([cold,*peers],details):
            job=detail['job'];assert job['state']=='succeeded',{'id':handle['id'],'state':job['state'],'reason':job.get('reason')}
            assert len(detail['attempts'])==1 and job['result']['execution_cleanup_confirmed']
            assert job['result']['workspace_release_confirmed'] and job['result']['cache_release_confirmed']
            report=self.artifact(handle,'results/full-build-provenance.json' if handle==cold else 'results/peer-report.json')
            assert report['passed'] and report['uid']==23900+handle['owner']
            self.results[handle['id']]={'handle':handle,'job':job,'report':report}
        cold_report=self.results[cold['id']]['report'];assert cold_report['record_files_verified']>5000 and cold_report['ninja_entries']>800
        cold_job=self.results[cold['id']]['job']
        cold_start=cold_report['source_to_wheel_started_unix'];cold_end=cold_report['source_to_wheel_finished_unix']
        overlap=[]
        for handle in peers:
            value=self.results[handle['id']];report=value['report'];job=value['job']
            assert report['cap_effective']=='0000000000000000' and report['no_new_privileges']
            if handle['phase']=='cmake-cold':assert report['ccache_stats']['cache_miss']==2
            if handle['phase']=='cmake-warm':
                assert report['ccache_stats']['cache_miss']==0
                assert report['ccache_stats'].get('direct_cache_hit',0)+report['ccache_stats'].get('preprocessed_cache_hit',0)==2
            if max(cold_start,report['started_unix'])<min(cold_end,report['finished_unix']):overlap.append(handle['id'])
            try:self.clients[0].request('/api/jobs/'+handle['id'])
            except ClientError as error:assert str(error)=='Controller HTTP 404'
            else:raise AssertionError('Private peer task visible to another owner')
        assert len(overlap)>=19,'Mixed work must actually overlap the cold compiler'
        assert len({v['job']['allocation']['cache']['key'] for v in self.results.values()})==20
        self.state['overlap_jobs']=overlap;self.save()
        remaining=int(self.end-time.monotonic()-300);assert remaining>=1800,'Leave time for a complete warm fresh build and cleanup'
        warm_request=copy.deepcopy(large);warm_request['parameters']['timeout_seconds']=min(3600,remaining)
        self.phase('fresh-source-warm-compiler-build')
        warm=self.submit(0,'compiler-warm',warm_request);self.state['warm_job']=warm['id'];self.save()
        detail=self.wait_all([warm],warm_request['parameters']['timeout_seconds']+180)[0]
        assert detail['job']['state']=='succeeded' and len(detail['attempts'])==1
        warm_report=self.artifact(warm,'results/full-build-provenance.json');assert warm_report['passed'] and warm_report['record_files_verified']>5000
        assert warm_report['ccache_stats'].get('direct_cache_hit',0)+warm_report['ccache_stats'].get('preprocessed_cache_hit',0)>500
        assert detail['job']['allocation']['cache']==cold_job['allocation']['cache']
        self.results[warm['id']]={'handle':warm,'job':detail['job'],'report':warm_report}
        self.phase('independent-personal-client-wheel-verification')
        downloads=self.verify_wheels([cold,warm])
        self.phase('verify-receipts-and-final-idle-state')
        self.settle(cancel=False)
        assert self.safe_cleanup
        atomic_json(self.outer/'results/large-mixed-details.json',{'jobs':self.results,'samples':self.samples})
        report={'passed':True,'recorded_at':now(),'outer_job':self.outer.name,'server_release':self.release_info['release_id'],
            'source_commit':origin['upstream_commit'],'task_image':TASK_IMAGE,'logical_owners':20,'task_uids':list(range(23900,23920)),
            'peak_compute_slots':2,'cpu_ids':self.cpus,'cold_job':cold['id'],'warm_job':warm['id'],
            'peer_jobs':len(peers),'peer_jobs_overlapping_cold':len(overlap),'single_attempts':True,'private_peer_tasks':True,
            'cold':cold_report,'warm':warm_report,'workspace_provider':'ext4-userquota','disk_reports':[d.report for d in self.disks],
            'independent_wheel_downloads':downloads,
            'host_accounts_modified':False,'npu_kernel_launches':0,'elapsed_seconds':time.monotonic()-self.started,
            'scope':'Actual full Triton wheel cold/warm build plus19 peer owners on separate quota-backed disk/cache/spool/payload; external LLVM/BishengIR reused, no device or physical multi-host acceptance'}
        atomic_json(self.outer/'results/large-mixed-validation.json',report)
        self.state['passed']=True;self.save()

    def verify_wheels(self,handles):
        prefix=self.base/'independent-client'
        subprocess.run([str(ROOT/'install-client'),'--prefix',str(prefix)],check=True,capture_output=True,text=True,timeout=30)
        proofs=[]
        for h in handles:
            assert self.end-time.monotonic()>300
            data=self.results[h['id']];provenance=data['report']
            cli=subprocess.run([str(prefix/'labctl'),'--profile',str(self.base/'client-0.json'),'archive',h['id'],
                '--local-source',str(self.base/'large-input/build.py')],check=True,capture_output=True,text=True,timeout=min(900,int(self.end-time.monotonic()-120)))
            downloaded=json.loads(cli.stdout);assert downloaded['complete']
            root=Path(downloaded['directory']);wheel=root/'results'/provenance['wheel_file']
            assert wheel.stat().st_size==provenance['wheel_bytes'] and digest(wheel)==provenance['wheel_sha256']
            count=0
            with zipfile.ZipFile(wheel) as package:
                record=next(n for n in package.namelist() if n.endswith('.dist-info/RECORD'))
                for name,checksum,size in csv.reader(io.StringIO(package.read(record).decode())):
                    if not checksum:continue
                    algorithm,expected=checksum.split('=',1);assert algorithm=='sha256';value=hashlib.sha256();total=0
                    with package.open(name) as stream:
                        for block in iter(lambda:stream.read(MIB),b''):value.update(block);total+=len(block)
                    assert total==int(size) and base64.urlsafe_b64encode(value.digest()).decode().rstrip('=')==expected
                    count+=1
            assert count==provenance['record_files_verified']
            environment=read_json(root/'environment.json');receipt=read_json(root/'results/_lab/environment.json')
            assert environment['task_uid']==23900 and environment['node_daemon_uid']==0
            assert environment['read_only_root'] and not environment['privileged'] and environment['network_mode']=='none'
            assert not environment['npu_ids'] and not environment['device_nodes'] and environment['persistent_cache']['enforced']
            assert receipt['locked'] and receipt['workload_started'] and receipt['probe_exit_code']==receipt['workload_exit_code']==0
            proofs.append({'job_id':h['id'],'bytes':wheel.stat().st_size,'sha256':digest(wheel),'record_files':count,
                           'personal_client_complete':True,'protected_receipt_locked':True,'task_uid':23900})
        return proofs

    def settle(self,cancel):
        # Check every original handle and the authoritative node inventory, not
        # just a container client return code. Unknown work retains this fixture.
        until=min(self.end,time.monotonic()+(900 if cancel else 90))
        sent=set()
        while time.monotonic()<until:
            try:
                active=[]
                for h in self.jobs:
                    d=self.detail(h)['job']
                    if d['state'] not in TERMINAL:
                        active.append(h)
                        if cancel and h['id'] not in sent:self.clients[h['owner']].request('/api/jobs/'+h['id']+'/cancel',{});sent.add(h['id'])
                if not active:
                    summary=self.controller_client.request('/api/jobs')
                    if not any(summary['counts'][k] for k in ['queued','reserved','running','cancelling','needs_attention']):
                        code='''import json,sys
from pathlib import Path
p=Path(sys.argv[1]);items=[]
for q in p.glob('attempts/*/control/state.json'):
 s=json.loads(q.read_text());items.append({'stage':s.get('stage'),'ack':s.get('controller_acknowledged'),'gc':s.get('artifact_gc',{}).get('state')})
print(json.dumps(items))
'''
                        items=json.loads(self.py(self.node,code,str(self.node_state)).stdout)
                        if all(s['stage']=='cleaned' and s['ack'] and s['gc']=='purged' for s in items):
                            self.state['final_attempt_receipts']=items;self.safe_cleanup=True;self.save();return
            except (ClientError,RuntimeError,subprocess.TimeoutExpired):pass
            time.sleep(3)
        self.state['preserve_unknown_fixture']=True;self.save()
        raise RuntimeError('Original work remains unconfirmed; preserve the recorded node, roles, volumes and leases for reconciliation')

    def cleanup(self):
        if not self.safe_cleanup:
            try:self.settle(cancel=True)
            except BaseException:
                # Keep the outer CPU/memory reservation until each child task's
                # own finite deadline has had time to expire/reconcile.
                while time.monotonic()<self.end:
                    try:self.settle(cancel=True);break
                    except BaseException:print('PRESERVE original unknown fixture; no resubmission',flush=True)
                if not self.safe_cleanup:return
        errors=[]
        for role in ['node','controller']:
            if (self.node if role=='node' else self.controller) in self.roles:
                try:self.ctl(role,'stop')
                except BaseException:pass  # A pre-install role has no service yet.
        # Only the exact trusted role containers are removed. Task receipts were
        # verified above before unmounting their filesystems.
        for name in reversed(self.roles):
            try:
                r=self.docker(['inspect',name],check=False)
                if r.returncode:continue
                info=json.loads(r.stdout)[0];assert info['Config']['Labels'].get('io.ascend-lab.mixedproof')==self.prefix
                if info['State']['Running']:self.docker(['stop','--time','5',name],timeout=20)
                self.docker(['rm',name])
            except BaseException as error:errors.append(type(error).__name__+': role cleanup')
        # Restore private control-tree ownership without mounting payload disks
        # or traversing another user's filesystem.
        if self.roles:
            code='''import os,sys
p=sys.argv[1];uid=int(sys.argv[2]);gid=int(sys.argv[3])
for parent,dirs,files in os.walk(p,followlinks=False):
 for name in dirs+files:os.lchown(os.path.join(parent,name),uid,gid)
os.chown(p,uid,gid)
'''
            for path in [self.controller_state,self.node_parent]:
                try:self.docker(['run','--rm','--pull','never','--label','io.ascend-lab.mixedproof='+self.prefix,'--network','none',
                     '--read-only','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER',
                     '--memory','128m','--memory-swap','128m','--cpuset-cpus',str(self.cpus[0]),'--pids-limit','32',
                     '--mount','type=bind,src='+str(path)+',dst=/private','--entrypoint','/usr/bin/python3',ROLE_IMAGE,
                     '-I','-c',code,'/private',str(os.getuid()),str(os.getgid())])
                except BaseException as error:errors.append(type(error).__name__+': control ownership')
        for volume in reversed(self.volumes):
            try:self.docker(['volume','rm',volume])
            except BaseException as error:errors.append(type(error).__name__+': volume cleanup')
        for disk in reversed(self.disks):
            try:disk.cleanup()
            except BaseException as error:errors.append(type(error).__name__+': loop cleanup')
        self.state['cleanup_errors']=errors;self.state['cleaned']=not errors;self.save()
        result=self.outer/'results/large-mixed-validation.json'
        if result.exists():
            report=read_json(result);report.update(cleanup_errors=errors,disk_reports=[d.report for d in self.disks]);atomic_json(result,report)
        if errors:raise RuntimeError('Own fixture cleanup incomplete; inspect private control.json')


def main():
    os.umask(0o077);run=Acceptance()
    try:run.prepare();run.workloads()
    finally:run.cleanup()


if __name__=='__main__':main()

"""Independent read-only disk/database/receipt audit after mixed build cleanup.

Refuses live or failed acceptance. Never mounts an image or repeats compilation.
The optional large wheel reads must themselves run under the ordinary queue.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,read_json,atomic_json,now
from docker_runtime import discover,command as docker_command

MIB=1024**2


def hash_file(path):
    value=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(MIB),b''):value.update(block)
    return value.hexdigest()


class Detached:
    def __init__(self,folder):
        self.path=folder/'workspace.ext4';intent=read_json(folder/'intent.json');state=read_json(folder/'loop-state.json')
        assert state['released'] and state['autoclear'] and state['backing_identity']==intent['identity']
        self.fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(self.fd)
            assert stat.S_ISREG(info.st_mode) and info.st_nlink==1
            assert [info.st_dev,info.st_ino,info.st_size]==intent['identity']
        except BaseException:os.close(self.fd);raise

    def close(self):os.close(self.fd)

    def operation(self,verb,path,output=None,expected=None):
        # No shell/debugfs quoting ambiguities; all paths come from the bounded
        # fixture schema and generated IDs, not an arbitrary command argument.
        assert verb in {'cat','stat','ls -p'} and re.fullmatch(r'/[A-Za-z0-9._/+\-]*',path)
        command=['/usr/sbin/debugfs','-R',verb+' '+path,'/proc/self/fd/'+str(self.fd)]
        if output is None:
            # Limit bytes while the tool writes, including stderr. Checking a
            # captured pipe afterwards would already have buffered an arbitrary
            # raw record in the audit process. The extra byte detects a short
            # write at the filesystem limit even if the tool exits successfully.
            ceiling=2*MIB
            def metadata_bound():
                resource.setrlimit(resource.RLIMIT_FSIZE,(ceiling+1,ceiling+1))
                resource.setrlimit(resource.RLIMIT_CORE,(0,0))
            with tempfile.TemporaryFile() as data,tempfile.TemporaryFile() as errors:
                subprocess.run(command,pass_fds=(self.fd,),check=True,stdout=data,stderr=errors,
                               preexec_fn=metadata_bound,timeout=30)
                data.seek(0);errors.seek(0)
                content=data.read(ceiling+1);diagnostics=errors.read(ceiling+1)
            assert len(content)<=ceiling and len(diagnostics)<=ceiling,'Unexpectedly large metadata record'
            assert not re.sub(rb'^debugfs [^\n]*\n?',b'',diagnostics).strip(),'Raw filesystem tool reported an error or warning'
            return content
        assert expected is not None and 0<expected<=2048*MIB
        def bound():resource.setrlimit(resource.RLIMIT_FSIZE,(expected,expected))
        descriptor=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            r=subprocess.run(command,pass_fds=(self.fd,),stdout=stream,stderr=subprocess.PIPE,
                             preexec_fn=bound,timeout=300,check=True)
        assert output.stat().st_size==expected,'Raw archived bytes differ from the recorded size'
        assert not re.sub(rb'^debugfs [^\n]*\n?',b'',r.stderr).strip(),'Raw filesystem tool reported an error or warning'
        return r

    def json(self,path):return json.loads(self.operation('cat',path))

    def entry(self,path):
        raw=self.operation('stat',path).decode()
        assert 'Type: regular' in raw and re.search(r'\bLinks:\s+1\b',raw)
        def field(name):return int(re.search(r'\b'+name+r':\s+(\d+)',raw)[1])
        return {'uid':field('User'),'gid':field('Group'),'size':field('Size')}

    def empty(self,path):
        rows=self.operation('ls -p',path).decode().splitlines()
        names=[line.split('/')[5] for line in rows if line.startswith('/')]
        assert names and set(names)<={'.','..'},'A released workspace/spool still contains entries'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--parent');parser.add_argument('--output');args=parser.parse_args()
    if args.parent:parent=args.parent
    else:
        c=read_json(ROOT/'runtime/large-mixed-20261030/control.json')
        parent=read_json(ROOT/'runtime/large-mixed-20261030'/c['latest_handle'])['job']['id']
    assert re.fullmatch(r'job-[0-9a-f]{12}',parent)
    job=Client.from_profile(ROOT/'runtime/config.json').request('/api/jobs/'+parent)['job']
    if job['state']!='succeeded':
        print(json.dumps({'accepted':False,'parent':parent,'state':job['state'],
                          'action':'Observe the original parent; no raw image, wheel or compiler was opened'}))
        return 2
    # Large independent reads also consume resources; a successful historical
    # parent is not a license to run a second unbudgeted workload on this host.
    assert os.environ.get('LAB_JOB_DIR'),'Submit the offline audit through the queue'
    base=ROOT/'runtime/jobs'/parent/'tmp/large-mixed';state=read_json(base/'control.json')
    proof=read_json(ROOT/'runtime/jobs'/parent/'results/large-mixed-validation.json')
    assert state['cleaned'] and not state['cleanup_errors'] and state['passed'] and proof['passed']
    assert not proof['cleanup_errors'] and proof['logical_owners']==20 and proof['peer_jobs']==57
    assert proof['peer_jobs_overlapping_cold']>=19 and len(proof['independent_wheel_downloads'])==2
    config=read_json(base/'node-private-config.json');binding,_=discover(config['docker_runtime'])
    for kind,names in [('container',state['roles']),('volume',[d['volume'] for d in state['disks']]),
                       ('container',[d['helper_container'] for d in state['disks']])]:
        for name in names:
            assert name.startswith(state['prefix'])
            argv,env=docker_command(binding,['docker',*(['volume','inspect'] if kind=='volume' else ['inspect']),name])
            r=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=20)
            assert r.returncode!=0 and 'no such' in (r.stdout+r.stderr).lower(),'Owned storage is still mounted/referenced or its state is unknown'
    opened=[]
    try:
        images={}
        for label,folder in [('payload','central-payload'),('work','quota-workspace'),('cache','cache-workspace'),('spool','spool-workspace')]:
            image=Detached(base/folder);opened.append(image);images[label]=image
        database=base/'controller-state/jobs.sqlite3'
        with sqlite3.connect('file:'+str(database.resolve())+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
            rows={row[0]:json.loads(row[1]) for row in db.execute('SELECT id,data FROM jobs')}
            assert len(rows)==62 and all(j['state']=='succeeded' for j in rows.values())
            assert db.execute('SELECT COUNT(*) FROM worker_inventory_holds WHERE held=1').fetchone()==(0,)
            for j in rows.values():
                assert db.execute('SELECT COUNT(*) FROM execution_attempts WHERE job_id=?',(j['id'],)).fetchone()==(1,)
                assert j['result']['execution_cleanup_confirmed'] and j['result']['workspace_release_confirmed']
            cold=rows[proof['cold_job']];warm=rows[proof['warm_job']]
            assert cold['parameters']['input_id']==warm['parameters']['input_id']
            assert cold['allocation']['cache']==warm['allocation']['cache']
            assert db.execute('SELECT COUNT(*) FROM input_bundles WHERE sealed=1').fetchone()==(20,)
        marker=images['work'].json('/.ascend-lab-userquota.json');assert len(marker['slots'])==20
        for slot in marker['slots']:
            assert re.fullmatch('slot-[0-9a-f]{12}',slot['id']);images['work'].empty('/'+slot['id'])
        images['spool'].empty('/attempts')
        peers=[]
        for identifier,j in rows.items():
            if j['name'].split()[-1] not in {'ir','cmake-cold','cmake-warm'}:continue
            prefix='/jobs/'+identifier+'/'
            r=images['payload'].json(prefix+'results/peer-report.json')
            assert r['passed'] and r['answer']==42 and r['cap_effective']=='0000000000000000' and r['no_new_privileges']
            ordinal=int(j['owner'].removeprefix('load_'));assert r['uid']==23900+ordinal
            inode=images['payload'].entry(prefix+'results/peer-report.json');assert inode['gid']==24500+ordinal
            if r['mode']=='ir':assert b'arith.constant 42' in images['payload'].operation('cat',prefix+'ir/output.mlir')
            elif j['name'].endswith('cmake-cold'):assert r['ccache_stats']['cache_miss']==2
            else:
                stats=r['ccache_stats'];assert stats['cache_miss']==0 and stats.get('direct_cache_hit',0)+stats.get('preprocessed_cache_hit',0)==2
            peers.append({'job_id':identifier,'uid':r['uid'],'gid':inode['gid'],'mode':r['mode']})
        assert len(peers)==57 and len({p['uid'] for p in peers})==19
        cache_marker=images['cache'].json('/.ascend-lab-userquota.json');assert len(cache_marker['slots'])==20
        wheel_proofs=[]
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime/large-mixed-20261030',prefix='offline-wheel-') as temp:
            for name,job in [('cold',cold),('warm',warm)]:
                prefix='/jobs/'+job['id']+'/'
                provenance=images['payload'].json(prefix+'results/full-build-provenance.json')
                assert provenance==proof[name] and provenance['passed'] and provenance['phase']=='complete'
                manifest=read_json(base/'node-control/state/attempts'/job['attempt_id']/'control/artifact-transfer.json')['manifest']
                digest=hashlib.sha256(json.dumps(sorted(manifest,key=lambda e:e['name']),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
                node_state=read_json(base/'node-control/state/attempts'/job['attempt_id']/'control/state.json')
                receipt=node_state['artifact_receipt'];assert receipt==job['_artifact_receipt'] and receipt['manifest_sha256']==digest
                assert node_state['stage']=='cleaned' and node_state['controller_acknowledged'] and node_state['artifact_gc']['state']=='purged'
                filename=provenance['wheel_file'];assert re.fullmatch(r'[A-Za-z0-9._+\-]+\.whl',filename)
                archived=prefix+'results/'+filename;inode=images['payload'].entry(archived)
                assert inode['gid']==24500 and inode['size']==provenance['wheel_bytes']
                wheel=Path(temp)/(name+'.whl');images['payload'].operation('cat',archived,wheel,expected=inode['size'])
                assert hash_file(wheel)==provenance['wheel_sha256']
                verified=0;extension=False
                with zipfile.ZipFile(wheel) as package:
                    record=next(n for n in package.namelist() if n.endswith('.dist-info/RECORD'))
                    for member,checksum,size in csv.reader(io.StringIO(package.read(record).decode())):
                        if not checksum:continue
                        algorithm,expected=checksum.split('=',1);assert algorithm=='sha256';value=hashlib.sha256();total=0
                        with package.open(member) as stream:
                            for block in iter(lambda:stream.read(MIB),b''):value.update(block);total+=len(block)
                        assert total==int(size) and base64.urlsafe_b64encode(value.digest()).decode().rstrip('=')==expected
                        if Path(member).name.startswith('libtriton') and member.endswith('.so') and value.hexdigest()==provenance['new_extension_sha256']:extension=True
                        verified+=1
                assert verified==provenance['record_files_verified'] and extension
                wheel_proofs.append({'job_id':job['id'],'bytes':inode['size'],'gid':inode['gid'],'sha256':hash_file(wheel),
                                     'record_files':verified,'new_extension_preserved':True,'node_receipt_matches_database':True})
        result={'passed':True,'verified_at':now(),'parent_job':parent,'method':'Read-only debugfs through held regular-image FDs and readonly SQLite; no mount, compiler or workload execution',
                'jobs_verified':len(rows),'single_attempts':True,'inventory_holds':0,'peer_artifacts':len(peers),
                'peer_owners':19,'peer_archive_groups_verified':True,'work_slots_empty':20,'node_spool_empty':True,
                'cold_warm_same_sealed_input':True,'cold_warm_same_private_cache':True,'wheel_proofs':wheel_proofs,
                'all_four_loop_associations_released':True,'owned_roles_volumes_helpers_absent':True,
                'npu_kernel_launches':0,'scope':'Independent persistence/ownership/integrity of the accepted same-host mixed case; not NPU, true host accounts, physical multi-host or power-loss acceptance'}
        target=Path(args.output) if args.output else Path(os.environ['LAB_JOB_DIR'])/'results/large-mixed-offline.json'
        assert target.is_absolute() and target.is_relative_to(Path(os.environ['LAB_JOB_DIR'])/'results')
        assert not target.exists() and not target.is_symlink(),'Preserve an existing audit result; choose a new queued audit run'
        atomic_json(target,result);print(json.dumps({k:result[k] for k in ['passed','parent_job','jobs_verified','peer_artifacts','peer_owners','node_spool_empty']}))
        return 0
    finally:
        for image in opened:image.close()


if __name__=='__main__':raise SystemExit(main())

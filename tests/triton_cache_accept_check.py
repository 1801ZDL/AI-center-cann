"""Independently bind actual compiler-cache proof to image, disk and source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,read_json,atomic_json,now
from deployment import digest


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--handle',default='runtime/toolchain-cache-20261030/native-cache-r7-handle.json');args=parser.parse_args()
    parent=read_json(ROOT/args.handle)['job']['id'];client=Client.from_profile(ROOT/'runtime/config.json')
    assert client.request('/api/jobs/'+parent)['job']['state']=='succeeded'
    base=ROOT/'runtime/jobs'/parent;proof=read_json(base/'results/triton-cache.json')
    assert proof['passed'] and proof['cleanup_confirmed'] and not proof['central_dispatch_integrated']
    assert proof['actual_public_triton_compile'] and proof['actual_cmake_ccache_fresh_build_reuse'] and proof['kernel_launches']==0
    assert proof['image']==read_json(ROOT/'runtime/toolchain-cache-20261030/offline-image.json')['image']
    rows=proof['records'];assert len(rows)==5
    cold,warm,variant,peer,again=rows
    assert cold['key']==warm['key']==again['key'] and len({cold['key'],variant['key'],peer['key']})==3
    assert all(r['stages_called']==['ttir','ttadapter','mlirbc','bcmlir','npubin'] for r in [cold,variant,peer])
    assert all(not r['stages_called'] and not r['actual_external_tools'] and r['warm_files_unchanged'] for r in [warm,again])
    assert len({r['binary_sha256'] for r in rows})==1 and all(r['binary_bytes']==20704 for r in rows)
    assert [r['uid'] for r in rows]==[23811,23811,23811,23812,23811]
    assert all(r['cap_effective']=='0000000000000000' and r['no_new_privileges'] and r['live_torch_device_query_guard']
               and r['guarded_device_calls']==0 and not r['kernel_loaded'] and r['kernel_launches']==0 and r['npu_devices']==0 for r in rows)
    assert all(r['cmake_cache']['cold_cache_misses']==2 and r['cmake_cache']['cache_hits']==0 for r in [cold,variant,peer])
    assert all(r['cmake_cache']['cold_cache_misses']==0 and r['cmake_cache']['cache_hits']==2 for r in [warm,again])
    assert all(r['cmake_cache']['answer']==42 and r['cmake_cache']['fresh_build_directory'] and r['cmake_cache']['passed'] for r in rows)
    assert all(0<r['quota_bytes_used']<96*1024**2 and 0<r['quota_inodes_used']<=2048 for r in rows)
    audit=cold['backend_copy_compatibility'];assert all(r['backend_copy_compatibility']==audit for r in rows)
    assert all(r['backend_sha256']==audit['after']['compiler'] for r in rows)
    assert audit['offline_no_device_limit_query'] and audit['offline_explicit_limit_rejected'] and audit['normal_online_branch_retained']
    assert audit['omitted_only_false_metaop_option'] and not audit['metaop_option_supported']
    wheel=next((ROOT/'runtime/jobs/job-7504b588b164/results').glob('*.whl'))
    assert digest(wheel)==read_json(ROOT/'evidence/full-build-live-validation.json')['wheel_sha256']
    with zipfile.ZipFile(wheel) as archive:
        for name in ['driver','compiler']:
            assert hashlib.sha256(archive.read('triton/backends/ascend/'+name+'.py')).hexdigest()==audit['before'][name]
    fixture=base/'tmp/triton-cache';image=fixture/'cache-workspace/workspace.ext4'
    assert read_json(image.parent/'loop-state.json')['released']
    def debug(command,text=False):
        return subprocess.run(['/usr/sbin/debugfs','-R',command,str(image)],check=True,capture_output=True,text=text).stdout
    marker=json.loads(debug('cat /.ascend-lab-userquota.json'))
    slots={s['quota_uid']:s for s in marker['slots']};assert set(slots)=={23811,23812}
    native=[]
    for row in [cold,variant,peer]:
        slot=slots[row['uid']];assert slot['bytes']==96*1024**2 and slot['inodes']==2048
        root='/'+slot['id']+'/'+row['key']+'/triton';pending=[root];found=[];seen=0
        while pending:
            path=pending.pop();seen+=1;assert seen<=64
            for line in debug('ls -p '+path,text=True).splitlines():
                parts=line.split('/')
                if len(parts)<7:continue
                name=parts[5]
                if name in {'.','..'}:continue
                assert re.fullmatch('[A-Za-z0-9_.-]+',name)
                target=path+'/'+name;mode=int(parts[2],8)
                if stat.S_ISDIR(mode):pending.append(target)
                elif name.endswith('.npubin'):
                    data=debug('cat '+target);assert data[:4]==b'\x7fELF' and len(data)==row['binary_bytes']
                    assert hashlib.sha256(data).hexdigest()==row['binary_sha256'];found.append(target)
        assert len(found)==1
        native.append({'owner':row['owner'],'uid':row['uid'],'key':row['key'],'binary_sha256':row['binary_sha256'],'bytes':row['binary_bytes'],'native_path':found[0]})
    with tarfile.open(ROOT/'evidence/ascend-lab-server-v033.tar.gz') as archive:release=json.load(archive.extractfile('release.json'))
    assert all(digest(ROOT/n)==v['sha256'] for n,v in release['files'].items())
    payload=fixture/'payload'
    assert digest(payload/'probe.py')==digest(ROOT/'tests/triton_cache_check.py')
    for n in ['persistent_cache.py','filesystem_quota.py','quota_workspace.py','workflow_spec.py']:
        assert digest(payload/n)==release['files'][n]['sha256']
    summary=client.request('/api/jobs');assert all(summary['counts'][s]==0 for s in ['queued','reserved','running','cancelling','needs_attention'])
    assert summary['reserved']=={'cpu':0,'memory_mib':0,'shm_mib':0,'npu_ids':[]}
    assert all(d['policy']['memory_mib']==2048 and not d['admission']['blocked'] for d in summary['resource_domains'])
    objects={}
    for label in ['io.ascend-lab.toolchain-cache','io.ascend-lab.diskproof','io.ascend-lab.cache-proof']:
        for kind,argv in [('containers',['ps','-aq']),('volumes',['volume','ls','-q']),('networks',['network','ls','-q'])]:
            value=subprocess.run(['docker',*argv,'--filter','label='+label],check=True,capture_output=True,text=True).stdout.strip()
            assert not value,(label,kind,value);objects[label+'/'+kind]=[]
    template='{{json .State.StartedAt}}|{{.HostConfig.Memory}}|{{.HostConfig.ShmSize}}|{{.State.Status}}'
    z=subprocess.run(['docker','inspect','--format',template,'zdl'],check=True,capture_output=True,text=True).stdout.strip().split('|')
    unchanged={'started_at':json.loads(z[0]),'memory_limit':int(z[1]),'shm_limit':int(z[2]),'status':z[3]}
    assert unchanged==read_json(ROOT/'evidence/validation-v02.json')['container_unchanged']
    failures=['triton-cache-first-helper-failure.json','triton-cache-second-owner-order-failure.json','triton-cache-third-backend-mismatch.json',
              'triton-cache-fourth-noexec-failure.json','triton-cache-fifth-user-env-failure.json','triton-cache-sixth-offline-device-query-failure.json']
    assert all(read_json(ROOT/'evidence'/n) for n in failures)
    report={'passed':True,'verified_at':now(),'parent_job':parent,'overall_goal_complete':False,'production_ready':False,
        'actual_toolchain_native_cache_verified':True,'central_dispatch_integrated':False,'root_proof':proof,
        'read_only_detached_disk_elfs':native,'compatibility_copy_audit':audit,'prior_failures_preserved':failures,
        'production_v033_files_unchanged':True,'production_release':release['id'],'objects_after_validation':objects,
        'reserved_after_validation':summary['reserved'],'existing_zdl_unchanged':unchanged,
        'model_invocations':0,'model_keys_read':False,'npu_kernel_launches':0,
        'earlier_failed_acl_initialization':{'job':'job-3174dc6e9ab1','code':507008,'devices_mounted':False},
        'source_files':{n:digest(ROOT/n) for n in ['tests/triton_cache_check.py','tests/toolchain_cache_prepare.py','tests/disk_workspace_fixture.py','tests/triton_cache_accept_check.py']},
        'scope':'Current native UID-quota provider with actual CMake/ccache and DSL-to-ELF cold/warm across five UID actors. Explicit private offline compatibility image; not normal device runtime, managed-node protected environment, full compiler build cache, numerical/performance or simulation acceptance.',
        'next':'Integrate this already accepted offline toolchain into actual managed Node/HTTP workflow and certified environment; request adequate cache inodes (one measured namespace consumed570). Do not rerun prior full compiler source build solely for backend changes.'}
    atomic_json(ROOT/'evidence/triton-native-cache-validation.json',report)
    atomic_json(ROOT/'evidence/triton-native-cache-root-proof.json',proof)
    print(json.dumps({'passed':True,'parent':parent,'actors':len(rows),'native_elfs_independently_read':len(native),
        'binary_bytes':cold['binary_bytes'],'cold_stages':cold['stages_called'],'warm_stages':warm['stages_called'],
        'warm_ccache_hits':warm['cmake_cache']['cache_hits'],'warm_ccache_misses':warm['cmake_cache']['cold_cache_misses'],
        'production_v033_files_unchanged':True,'overall_goal_complete':False},ensure_ascii=False,indent=2))


if __name__=='__main__':main()

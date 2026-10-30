"""Read bounded driver namespace metadata; publish counts/own-zdl matches only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def parse(raw,known):
    lines=raw.splitlines();blocks=[];current=None;headers=0
    for line in lines:
        if 'ns_id' in line and 'root_tgid' in line:
            headers+=1
            fields={key:int(value) for key,value in re.findall(r'\b(ns_id|root_tgid|dev_num|namespace)\s*[:=]?\s*(\d+)',line)}
            identifier=re.search(r'\bidentify\s*[:=]?\s*([A-Za-z0-9_.-]+)\s+root_tgid\b',line)
            if set(fields)!={'ns_id','root_tgid','dev_num','namespace'} or not identifier:return {'parse_complete':False,'reason':'Unrecognized namespace header'}
            fields['identify']=identifier[1]
            current={**fields,'devices':[]};blocks.append(current)
        elif current is not None:
            match=re.fullmatch(r'\s*[|]?\s*(\d+)\s+(\d+)\s*[|]?\s*',line)
            if match:current['devices'].append((int(match[1]),int(match[2])))
    complete=bool(headers) and all(len(row['devices'])==row['dev_num'] and len({x[0] for x in row['devices']})==row['dev_num'] for row in blocks)
    if not complete:return {'parse_complete':False,'headers':headers,'row_lengths':[len(row['devices']) for row in blocks],'expected_lengths':[row['dev_num'] for row in blocks]}
    device_numbers=sorted({number for row in blocks for _,number in row['devices']});devices=[]
    for udevid in device_numbers:
        rows=[row for row in blocks if any(p==udevid for _,p in row['devices'])]
        owned=[row for row in rows if row['namespace'] in known['namespace_inodes'] or row['root_tgid'] in known['pids']]
        devices.append({'udevid':udevid,'mapping_count':len(rows),
                        'namespace_ids_below_128':sum(row['ns_id']<128 for row in rows),
                        'namespace_ids_at_least_128':sum(row['ns_id']>=128 for row in rows),
                        'known_zdl_matches':len(owned),
                        'below_128_known_zdl_matches':sum(row['ns_id']<128 for row in owned),
                        'at_least_128_known_zdl_matches':sum(row['ns_id']>=128 for row in owned),
                        'below_128_nonzero_identity_mappings':sum(row['ns_id']<128 and row['identify']!='0' for row in rows),
                        'known_zdl_mnt_inode_matches':sum(row['namespace']==known['mnt'] for row in rows),
                        'known_zdl_pid_inode_matches':sum(row['namespace']==known['pid'] for row in rows),
                        'known_zdl_id_text_matches':sum(row['identify'] in known.get('identifiers',[]) for row in rows),
                        'nonzero_text_identity_mappings':sum(row['identify']!='0' for row in rows),
                        'other_holder_identifiers_exported':False})
    return {'parse_complete':True,'headers':headers,'devices':devices,'scope':'Observed table mapping only; no exclusive-ownership or cause inference'}


def reader():
    known=json.load(sys.stdin)
    with Path('/proc/uda/namespace_node').open('rb') as stream:raw=stream.read(32769)
    assert len(raw)<=32768
    value=parse(raw.decode(),known)
    # Never publish raw lines, including when parsing an unfamiliar layout.
    print(json.dumps({**value,'bytes_read':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))


def parent():
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from lab_client import read_json,atomic_json,now
    from docker_runtime import discover,command
    assert os.environ.get('LAB_JOB_DIR'),'Submit the fixed CPU-only reader through the queue'
    directory=Path(os.environ['LAB_JOB_DIR']);job=read_json(directory/'dispatch.json');assert job['resources']['npu']==0
    binding,_=discover()
    def docker(args,check=True,input=None):
        argv,env=command(binding,['docker',*args]);r=subprocess.run(argv,env=env,capture_output=True,text=True,input=input,timeout=25)
        if check and r.returncode:raise RuntimeError('Owned namespace metadata operation failed: '+r.stderr[-500:])
        return r
    # Only the already-authorized zdl container is identified. Never inspect or
    # export another holder's process/command, even though Root can read the table.
    existing=json.loads(docker(['inspect','zdl']).stdout)[0];assert existing['State']['Running']
    namespace=json.loads(docker(['exec','--user','1104:1001','zdl','/usr/bin/python3','-I','-B','-c',
      'import os,json;print(json.dumps({k:os.stat("/proc/self/ns/"+k).st_ino for k in ["mnt","pid"]}))']).stdout)
    top=docker(['top','zdl','-eo','pid']).stdout.splitlines()[1:]
    known={**namespace,'namespace_inodes':list(namespace.values()),'pids':sorted({existing['State']['Pid'],*(int(x.strip()) for x in top if x.strip().isdigit())}),
           'identifiers':[existing['Id'],existing['Id'][:12]]}
    program=directory/'tmp/namespace-reader.py';program.write_bytes(Path(__file__).read_bytes());program.chmod(0o444)
    name='ascend-lab-namespace-'+job['id'][4:];image='sha256:24188496e90e95212870b86a2be4fe5a360da8b556f54fe662fd9a954e94d273'
    result=docker(['run','--rm','-i','--name',name,'--label','io.ascend-lab.namespace-probe='+job['id'],
      '--user','0:0','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--ipc','none',
      '--memory','128m','--memory-swap','128m','--pids-limit','32','--cpus','0.25','--cpuset-cpus',str(job['allocation']['cpu_ids'][0]),
      '--mount','type=bind,src='+str(program)+',dst=/probe.py,readonly','--entrypoint','/usr/bin/timeout',image,
      '--kill-after=2s','15s','/usr/bin/python3','-I','-B','/probe.py','--reader'],input=json.dumps(known))
    result=json.loads(result.stdout);after=json.loads(docker(['inspect','zdl']).stdout)[0]
    assert after['State']['StartedAt']==existing['State']['StartedAt'] and after['HostConfig']==existing['HostConfig']
    report={'recorded_at':now(),'job_id':job['id'],'management_completed':True,**result,'device_nodes_exposed':[],
      'acl_initialized':False,'npu_kernel_launches':0,'existing_zdl_unchanged':True,'other_process_identifiers_exported':False}
    atomic_json(directory/'results/namespace-mapping.json',report);print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--reader',action='store_true');args=parser.parse_args()
    if args.reader:reader()
    else:parent()

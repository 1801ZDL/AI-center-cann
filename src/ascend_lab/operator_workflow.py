"""Prepare one fixed offline-report recipe for the existing managed CPU contract.

Preparation does not upload input, submit work, certify an image or call a model.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from lab_client import atomic_json
from operator_profile import PROVIDER_FILES

ROOT=Path(__file__).resolve().parent
PREFIX='/opt/ascend-lab-operator'
PACKAGES={'msprof-mcp':'0.1.8','mcp':'1.30.0','numpy':'2.4.6','pandas':'3.1.0','perfetto':'0.59.1'}
APP_FILES=['operator_node.py','operator_profile.py','diagnostic_case.py','lab_client.py']


def source_locks():
    return {PREFIX+'/app/'+name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in APP_FILES}


def probe_queries(node_id):
    if not isinstance(node_id,str) or not re.fullmatch('worker-[0-9a-f]{12}',node_id):raise ValueError('Select one registered managed node')
    return {'node_id':node_id,'memory_mib':256,'packages':list(PACKAGES),'files':[
        *source_locks(),*(PREFIX+'/venv/lib/python3.11/site-packages/'+name for name in PROVIDER_FILES)]}


def prepare(input_id,lock,*,name='Offline operator analysis',idempotency_key=None):
    if not isinstance(input_id,str) or not re.fullmatch('input-[0-9a-f]{24}',input_id):raise ValueError('Use a sealed input ID')
    if (not isinstance(lock,dict) or set(lock)-{'python','image_sha256','machine','packages','tools','files'}
        or not isinstance(lock.get('python'),str) or not re.fullmatch(r'3\.11\.\d+',lock['python'])
        or lock.get('machine')!='x86_64' or not isinstance(lock.get('image_sha256'),str)
        or not re.fullmatch('sha256:[0-9a-f]{64}',lock['image_sha256'])):
        raise ValueError('Require an explicit certified CPython3.11/x86_64 immutable-image lock')
    if not isinstance(lock.get('packages'),dict) or any(lock['packages'].get(k)!=v for k,v in PACKAGES.items()):
        raise ValueError('Certified tools differ from the fixed recipe')
    files=lock.get('files')
    expected={**source_locks(),**{PREFIX+'/venv/lib/python3.11/site-packages/'+k:v for k,v in PROVIDER_FILES.items()}}
    if not isinstance(files,dict) or any(files.get(k)!=v for k,v in expected.items()):
        raise ValueError('Image entry/tool code has not been certified for this client version')
    if not isinstance(name,str) or not name or len(name)>160 or any(ord(c)<32 for c in name):raise ValueError('Invalid analysis name')
    result={'name':name,'template':'build_v1','environment':'worker-python','parameters':{
        'input_id':input_id,'argv':['python','-I','-B',PREFIX+'/app/operator_node.py'],
        'lock':json.loads(json.dumps(lock,allow_nan=False)),'resources':{'cpu':1,'memory_mib':768,'shm_mib':0,'npu':0},
        'timeout_seconds':180,'input_mode':'readonly','result_file':'results/workload-result.json',
        'workspace':{'min_mib':16,'min_inodes':256,'durable':True},'artifacts':{'file_mib':2,'total_mib':4}}}
    if idempotency_key is not None:
        if not isinstance(idempotency_key,str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',idempotency_key):raise ValueError('Invalid idempotency key')
        result['idempotency_key']=idempotency_key
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input-id',required=True);parser.add_argument('--lock',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--idempotency-key');args=parser.parse_args()
    output=Path(args.output)
    if output.exists() or output.is_symlink():raise ValueError('Preserve the existing request; choose a new output path')
    lock_path=Path(args.lock)
    if not lock_path.is_file() or lock_path.stat().st_size>256*1024:raise ValueError('Environment lock missing or too large')
    atomic_json(output,prepare(args.input_id,json.loads(lock_path.read_text()),idempotency_key=args.idempotency_key))
    print(json.dumps({'prepared':True,'submitted':False,'request':str(output),'template':'build_v1','purpose':'offline_operator_report_generation','npu':0}))


if __name__=='__main__':main()

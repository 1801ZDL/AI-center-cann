"""Independent read-only comparison after the owned Root fixture is terminal."""
import hashlib
import argparse
import base64
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab_client import Client,read_json,atomic_json,now


def main(version='032'):
    if version in {'034','035','036'}:
        folder=ROOT/'runtime'/({'034':'native-managed-20261030','035':'quota-resize-20261030','036':'deadline-install-r3-20261030'}[version])
        handle=folder/read_json(folder/'control.json')['latest_handle']
    else:
        handle=ROOT/'runtime'/('server-install-v032-r4-handle.json' if version=='032' else 'server-install-v033-handle.json')
    parent=read_json(handle)['job']['id']
    assert Client.from_profile(ROOT/'runtime/config.json').request('/api/jobs/'+parent)['job']['state']=='succeeded'
    base=ROOT/'runtime/jobs'/parent/'tmp/installation'
    proof=read_json(ROOT/'evidence'/('server-install-v'+version+'-validation.json'));phases=proof['dependencies']
    assert proof['passed'] and not proof['cleanup_errors']
    child=phases['consumer_job'];identifier=phases['partial_input_id'];first=proof['job_ids'][0]
    assert re.fullmatch('job-[0-9a-f]{12}',child) and re.fullmatch('input-[0-9a-f]{24}',identifier)
    rows=[];receipts=[];durable=proof.get('input_durability');durable_rows=[]
    native=proof.get('native_toolchain');native_rows=[]
    def extract(image,name):
        return subprocess.run(['/usr/sbin/debugfs','-R','cat '+name,str(image)],capture_output=True,check=True).stdout
    for folder in ['central-payload','central-restore']:
        image=base/folder/'workspace.ext4';assert read_json(image.parent/'loop-state.json')['released']
        ir=extract(image,'/jobs/'+first+'/ir/canonical.mlir')
        assert len(ir)==119 and hashlib.sha256(ir).hexdigest()=='5d94d67d11061e11a10c3753b2cdc3391e7767ab625ed4fb6df26ed20f8f4d4e'
        data=extract(image,'/inputs/'+identifier+'/deps/data.bin')
        assert len(data)==phases['data_bytes']==8*1024**2 and hashlib.sha256(data).hexdigest()==phases['data_sha256']
        receipt=json.loads(extract(image,'/jobs/'+child+'/dependency-inputs.json'));receipts.append(receipt)
        assert receipt['input_id']==identifier and next(x for x in receipt['artifacts'] if x['target']=='deps/data.bin')['sha256']==phases['data_sha256']
        rows.append({'filesystem':folder,'ir_bytes':len(ir),'ir_sha256':hashlib.sha256(ir).hexdigest(),
            'derived_input_bytes':len(data),'derived_input_sha256':hashlib.sha256(data).hexdigest()})
        if durable:
            prefix='/inputs/'+durable['input_id']+'/'
            data=extract(image,prefix+'data/nested/payload.bin')
            assert len(data)==durable['data_bytes']==2*1024**2 and hashlib.sha256(data).hexdigest()==durable['data_sha256']
            assert extract(image,prefix+'reset/data.bin')==b'correct-reset-bytes'
            empty_stat=subprocess.run(['/usr/sbin/debugfs','-R','stat '+prefix+'empty/zero',str(image)],capture_output=True,text=True,check=True).stdout
            assert 'Type: regular' in empty_stat and re.search(r'\bSize:\s+0\b',empty_stat) and re.search(r'\bLinks:\s+1\b',empty_stat),empty_stat
            assert extract(image,prefix+'empty/zero')==b''
            durable_rows.append({'filesystem':folder,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'reset_bytes_correct':True,'empty_file_preserved':True})
        if native:
            prefix='/jobs/'+native['cold_job']+'/'
            elf=extract(image,prefix+'results/native-kernel.o')
            assert elf[:4]==b'\x7fELF' and len(elf)==native['binary_bytes']
            assert hashlib.sha256(elf).hexdigest()==native['binary_sha256']
            manifest=json.loads(extract(image,prefix+'results/kernel-manifest.json'))
            assert manifest['binary_sha256']==native['binary_sha256']
            metadata=json.loads(extract(image,prefix+'results/kernel-metadata.json'))
            assert metadata and b'tt.func' in extract(image,prefix+'ir/kernel.ttir')
            native_rows.append({'filesystem':folder,'job_id':native['cold_job'],'elf_bytes':len(elf),
                'elf_sha256':hashlib.sha256(elf).hexdigest(),'metadata_and_manifest_preserved':True,'ttir_preserved':True})
    assert receipts[0]==receipts[1]
    with sqlite3.connect('file:'+str(base/'controller-state/restored-proof.sqlite3')+'?mode=ro',uri=True) as db:
        def job(identifier):return json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(identifier,)).fetchone()[0])
        consumer=job(child);assert consumer['input_provenance']==receipts[0] and consumer['input']['id']==identifier
        assert consumer['depends_on']==[phases['producer_job']] and consumer['state']=='succeeded'
        joined=job(phases['join_job']);assert set(joined['depends_on'])=={phases['producer_job'],child}
        assert len(joined['input_provenance']['parents'])==2
        device=job(phases['device_stage_job']);assert device['state']=='cancelled' and device['allocation']=={}
        assert db.execute('SELECT COUNT(*) FROM execution_attempts WHERE job_id=?',(device['id'],)).fetchone()[0]==0
        failed=job(phases['unstarted_descendant_job']);assert failed['state']=='cancelled' and failed['result']['execution_started'] is False
        row=json.loads(db.execute('SELECT record FROM workflow_results WHERE job_id=?',(failed['id'],)).fetchone()[0])
        assert row['command_executed'] is False and row['failure_stage']=='dependency'
        if durable:
            record=db.execute('SELECT sealed FROM input_bundles WHERE id=?',(durable['input_id'],)).fetchone();assert record==(1,)
            assert dict(db.execute('SELECT name,size FROM input_uploads WHERE bundle=?',(durable['input_id'],)))['data/nested/payload.bin']==durable['data_bytes']
            assert job(durable['actual_ir_job'])['state']=='succeeded'
            assert db.execute('SELECT COUNT(*) FROM execution_attempts WHERE job_id=?',(durable['actual_ir_job'],)).fetchone()[0]==1
        if native:
            for identifier in [native['cold_job'],native['warm_job'],native['source_variant_job'],
                               native['environment_variant_job'],native['peer_job'],native['mcp']['job_id'],
                               native['after_restore']['job_id']]:
                record=job(identifier);assert record['state']=='succeeded'
                assert db.execute('SELECT COUNT(*) FROM execution_attempts WHERE job_id=?',(identifier,)).fetchone()[0]==1
                stored=json.loads(db.execute('SELECT record FROM workflow_results WHERE job_id=?',(identifier,)).fetchone()[0])
                assert stored['passed'] and stored['environment_locked'] and stored['kind']=='build'
            assert job(native['cold_job'])['allocation']['cache']['key']==native['cache_key']
            assert job(native['after_restore']['job_id'])['allocation']['cache']['key']==native['cache_key']
        if proof.get('payload_quota_resize'):
            resize=proof['payload_quota_resize']
            for identifier in [resize['grow_actual_ir_job'],resize['peer_actual_ir_job']]:
                assert job(identifier)['state']=='succeeded'
                assert db.execute('SELECT COUNT(*) FROM execution_attempts WHERE job_id=?',(identifier,)).fetchone()[0]==1
        if proof.get('deadline_limit'):
            identifier=proof['deadline_limit']['job_id'];record=job(identifier)
            assert record['state']=='succeeded' and record['timeout_seconds']==7200
            assert db.execute('SELECT COUNT(*) FROM execution_attempts WHERE job_id=?',(identifier,)).fetchone()[0]==1
    fixture=read_json(ROOT/'runtime/jobs'/parent/'results/dependency-ui.json');assert fixture['source_parent']==parent
    fixture['jobs'].append(failed);fixture['rows']=[row]
    fixture['scope']='Actual archived partial handoff plus restored database non-execution result; not live Main task data'
    atomic_json(ROOT/'evidence'/('dependency-ui-fixture-v'+version+'.json'),fixture)
    if native:
        fixture=read_json(ROOT/'runtime/jobs'/parent/'results/native-toolchain-ui.json')
        assert fixture['source_parent']==parent and len(fixture['jobs'])==len(fixture['rows'])==5
        atomic_json(ROOT/'evidence'/('native-toolchain-ui-fixture-v'+version+'.json'),fixture)
    quota_receipts=None
    if proof.get('payload_quota_resize'):
        export=read_json(ROOT/'runtime/jobs'/parent/'results/quota-audit.json')
        assert export['source_parent']==parent and len(export['receipts'])==2
        expected={p['receipt']['plan_id']:p['receipt'] for p in [proof['payload_quota_resize']['grow'],proof['payload_quota_resize']['shrink']]}
        quota_receipts=[]
        for row in export['receipts']:
            data=base64.b64decode(row['bytes_base64'],validate=True);record=json.loads(data)
            assert row['sha256']==hashlib.sha256(data).hexdigest() and row['uid']==0 and row['mode']==0o600
            assert row['name']==record['plan_id']+'.json' and record==expected[record['plan_id']]
            quota_receipts.append({'name':row['name'],'sha256':row['sha256'],'revision':record['revision']})
    result={'passed':True,'verified_at':now(),'parent_job':parent,'source_and_restore':rows,
        'source_preserved_and_matches_restore':True,'graph_and_derived_input_preserved':True,
        'consumer_job':child,'derived_input_id':identifier,'device_attempts':0,'unstarted_row_preserved':True,
        **({'durable_input_restored':True,'durable_input_images':durable_rows} if durable else {}),
        **({'native_toolchain_restored':True,'native_toolchain_images':native_rows,'native_database_results_preserved':True} if native else {}),
        **({'quota_admin_receipts_preserved':True,'quota_admin_receipts':quota_receipts} if quota_receipts else {}),
        'method':'Read-only debugfs on detached owned ext4 images and readonly exported restored SQLite; no block-device write, mount or task execution'}
    atomic_json(ROOT/'evidence'/('dependency-offline-compare-v'+version+'.json'),result)
    print(json.dumps({'passed':True,'parent_job':parent,'restored_data_bytes':phases['data_bytes'],'graph_preserved':True,'device_attempts':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--version',choices=['032','033','034','035','036'],default='032');args=parser.parse_args()
    main(args.version)

"""Queue-owned read-only audit of original installed graph disks and receipts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from large_mixed_offline_check import Detached
from lab_client import Client,atomic_json
from node_storage import manifest_sha256


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--parent',required=True);args=parser.parse_args()
    assert os.environ.get('LAB_JOB_DIR') and re.fullmatch('job-[0-9a-f]{12}',args.parent)
    assert Client.from_profile(ROOT/'runtime/config.json').request('/api/jobs/'+args.parent)['job']['state']=='succeeded'
    parent=ROOT/'runtime/jobs'/args.parent;base=parent/'tmp/large-mixed'
    proof=json.loads((parent/'results/graph-validation.json').read_text());control=json.loads((base/'control.json').read_text())
    assert proof['passed'] and proof['cleanup_confirmed'] and control['cleaned'] and not control['cleanup_errors']
    assert len(proof['graphs'])==4 and len(proof['faults'])==3 and all(v['actual_sigkill'] for v in proof['faults'])
    for folder in ['central-payload','quota-workspace','cache-workspace','spool-workspace']:
        value=json.loads((base/folder/'loop-state.json').read_text());assert value['released']
        assert not Path('/sys/block/loop'+str(value['number'])+'/loop/backing_file').exists()
    opened=[];records=[]
    try:
        for folder in ['central-payload','quota-workspace','spool-workspace']:opened.append(Detached(base/folder))
        payload,work,spool=opened
        marker=work.json('/.ascend-lab-userquota.json');assert len(marker['slots'])==20
        for slot in marker['slots']:work.empty('/'+slot['id'])
        spool.empty('/attempts')
        with sqlite3.connect((base/'controller-state/jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==13
            assert db.execute('SELECT count(*) FROM job_graphs').fetchone()[0]==4
            assert db.execute('SELECT count(*) FROM experiment_charges').fetchone()[0]==12
            assert db.execute('SELECT count(*) FROM worker_inventory_holds WHERE held=1').fetchone()[0]==0
            for graph in proof['graphs']:
                row=db.execute('SELECT owner,state,record FROM job_graphs WHERE id=?',(graph['graph_id'],)).fetchone()
                owner='load_'+str(graph['owner']).zfill(2);assert row[0]==owner and row[1]=='submitted'
                header=json.loads(row[2]);assert header['experiment_id']==graph['budget_id']
                assert set(header['jobs'].values())=={v['job_id'] for v in graph['stages'].values()}
                assert db.execute('SELECT count(*) FROM experiment_charges WHERE budget_id=?',(graph['budget_id'],)).fetchone()[0]==3
                for stage,expected in graph['stages'].items():
                    ident=expected['job_id'];job=json.loads(db.execute('SELECT data FROM jobs WHERE id=?',(ident,)).fetchone()[0])
                    assert job['state']=='succeeded' and job['owner']==owner and job['_graph_id']==graph['graph_id'] and '_graph_payload' not in job
                    assert job['attempt_id']==expected['attempt_id'] and db.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(ident,)).fetchone()[0]==1
                    prefix='/jobs/'+ident+'/';meta=payload.json(prefix+'environment.json');receipt=payload.json(prefix+'results/_lab/environment.json')
                    report=payload.json(prefix+'results/graph-stage.json');assert report==expected['report'] and report['uid']==meta['task_uid']==graph['uid']
                    assert report['answer']==42 and report['npu_kernel_launches']==0 and report['cap_effective']=='0000000000000000'
                    assert meta['memory_limit']==256*1024**2 and meta['device_nodes']==[] and meta['read_only_root'] and not meta['privileged'] and meta['network_mode']=='none'
                    assert receipt['locked'] and receipt['probe_exit_code']==receipt['workload_exit_code']==0
                    folder=base/'node-control/state/attempts'/job['attempt_id']/'control'
                    transfer=json.loads((folder/'artifact-transfer.json').read_text())['manifest'];state=json.loads((folder/'state.json').read_text())
                    assert state['artifact_receipt']==job['_artifact_receipt'] and manifest_sha256(transfer)==job['_artifact_receipt']['manifest_sha256']
                    assert state['stage']=='cleaned' and state['controller_acknowledged'] and state['artifact_gc']['state']=='purged'
                    for name in ['environment.json','results/_lab/environment.json','results/graph-stage.json','ir/output.mlir']:
                        raw=payload.operation('cat',prefix+name);item=next(v for v in transfer if v['name']==name)
                        assert len(raw)==item['size'] and hashlib.sha256(raw).hexdigest()==item['sha256']
                    assert payload.entry(prefix+'results/graph-stage.json')['gid']==24500+graph['owner']
                    assert b'arith.constant 42' in payload.operation('cat',prefix+'ir/output.mlir')
                    if stage!='build':
                        provenance=payload.json(prefix+'dependency-inputs.json');assert provenance==expected['input_provenance']
                        assert len(job['depends_on'])==1 and job['dependency_state']['status']=='ready'
                    assert job['result']['execution_cleanup_confirmed'] and job['result']['workspace_release_confirmed']
                    records.append({'job_id':ident,'uid':graph['uid'],'stage':stage,'graph_id':graph['graph_id'],'single_attempt':True})
                assert graph['stages']['verify']['report']['input_sha256']==graph['stages']['build']['report']['output_sha256']
                assert graph['stages']['report']['report']['input_sha256']==graph['stages']['verify']['report']['output_sha256']
        assert len(records)==12 and {v['uid'] for v in records}=={23901,23902,23903,23904}
        result={'passed':True,'parent':args.parent,'actual_workload_jobs':12,'graphs':4,'task_uids':[23901,23902,23903,23904],
            'original_rows_attempts_manifests_and_archive_bytes_checked':True,'artifact_chain_sha_checked':True,
            'native_work_and_spool_empty':True,'npu':0,'model_calls':0,'records':records}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/graph-audit.json',result)
        print(json.dumps({'passed':True,'jobs':12,'graphs':4,'uids':4}))
    finally:
        for item in opened:item.close()


if __name__=='__main__':main()

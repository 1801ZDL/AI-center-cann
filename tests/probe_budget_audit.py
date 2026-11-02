"""Independently inspect the completed small-qualification fixture, read-only."""
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


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--parent',required=True);args=parser.parse_args()
    assert os.environ.get('LAB_JOB_DIR') and re.fullmatch('job-[0-9a-f]{12}',args.parent)
    assert Client.from_profile(ROOT/'runtime/config.json').request('/api/jobs/'+args.parent)['job']['state']=='succeeded'
    parent=ROOT/'runtime/jobs'/args.parent;base=parent/'tmp/large-mixed'
    proof=json.loads((parent/'results/probe-budget-validation.json').read_text())
    control=json.loads((base/'control.json').read_text())
    assert proof['passed'] and proof['cleanup_confirmed'] and control['cleaned'] and not control['cleanup_errors']
    for folder in ['central-payload','quota-workspace','cache-workspace','spool-workspace']:
        state=json.loads((base/folder/'loop-state.json').read_text())
        assert state['released'] and not Path('/sys/block/loop'+str(state['number'])+'/loop/backing_file').exists()
    payload=Detached(base/'central-payload')
    try:
        with sqlite3.connect((base/'controller-state/jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            rows={ident:json.loads(raw) for ident,raw in db.execute('SELECT id,data FROM jobs')}
            assert set(rows)==set(proof['probe_jobs']+[proof['ir_job']])
            metadata=[]
            for ident,job in rows.items():
                assert job['state']=='succeeded' and db.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(ident,)).fetchone()[0]==1
                prefix='/jobs/'+ident+'/'
                environment=payload.json(prefix+'environment.json');receipt=payload.json(prefix+'results/_lab/environment.json')
                uid=23900 if job['owner']=='load_00' else 23901
                assert environment['task_uid']==uid and environment['memory_limit']==256*1024**2
                assert environment['network_mode']=='none' and environment['read_only_root'] and not environment['privileged'] and environment['device_nodes']==[]
                assert receipt['locked'] and receipt['probe_exit_code']==0
                assert job['result']['execution_cleanup_confirmed'] and job['result']['workspace_release_confirmed']
                if ident in proof['probe_jobs']:
                    assert job['artifact_limits']=={'file_mib':1,'total_mib':2} and receipt['probe_only'] and not receipt['workload_started']
                else:
                    assert job['artifact_limits']=={'file_mib':1,'total_mib':4} and receipt['workload_exit_code']==0
                    assert b'arith.constant 42' in payload.operation('cat',prefix+'ir/output.mlir')
                    report=payload.json(prefix+'results/peer-report.json');assert report['answer']==42 and report['uid']==uid
                # Check original bytes against the actual retained manifest.
                control_dir=base/'node-control/state/attempts'/job['attempt_id']/'control'
                manifest=json.loads((control_dir/'artifact-transfer.json').read_text())['manifest']
                node_state=json.loads((control_dir/'state.json').read_text())
                from node_storage import manifest_sha256
                assert node_state['artifact_receipt']==job['_artifact_receipt']
                assert job['_artifact_receipt']['manifest_sha256']==manifest_sha256(manifest)
                assert node_state['stage']=='cleaned' and node_state['controller_acknowledged'] and node_state['artifact_gc']['state']=='purged'
                for name in ['environment.json','results/_lab/environment.json']:
                    entry=next(v for v in manifest if v['name']==name)
                    raw=payload.operation('cat',prefix+name)
                    assert len(raw)==entry['size'] and hashlib.sha256(raw).hexdigest()==entry['sha256']
                metadata.append({'job_id':ident,'uid':uid,'single_attempt':True,'artifact_limits':job['artifact_limits']})
        config=json.loads((base/'controller-private-config.json').read_text())
        assert config['scheduler']['io_limits']==proof['central_io_limits']
        groups=config['scheduler']['payload_storage']['owner_groups']
        assert len(groups)==20 and all(v['bytes']==64*1024**2 for v in groups.values())
        marker=payload.json('/.ascend-lab-payload.json')
        assert marker['owner_groups_sha256']==hashlib.sha256(json.dumps(groups,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        result={'passed':True,'parent':args.parent,'checks':metadata,'raw_filesystem_receipts_and_archive_sha':True,
            'two_actual_nonroot_task_uids':True,'fixed_probe_budget_and_real_ir':True,'npu':0,'model_calls':0}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/probe-budget-audit.json',result)
        print(json.dumps({'passed':True,'checked_jobs':len(metadata),'task_uids':[23900,23901]}))
    finally:payload.close()


if __name__=='__main__':main()

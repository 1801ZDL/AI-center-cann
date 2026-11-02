"""Queue-owned independent read-only audit of the completed20-owner burst."""
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
    client=Client.from_profile(ROOT/'runtime/config.json');assert client.request('/api/jobs/'+args.parent)['job']['state']=='succeeded'
    base=ROOT/'runtime/jobs'/args.parent/'tmp/large-mixed';control=json.loads((base/'control.json').read_text())
    proof=json.loads((ROOT/'runtime/jobs'/args.parent/'results/team-burst-validation.json').read_text());details=json.loads((base/'burst-details.json').read_text())
    assert proof['passed'] and proof['cleanup_confirmed'] and control['cleaned'] and not control['cleanup_errors']
    assert proof['logical_owners']==20 and proof['workload_jobs']==60 and len(details['responses'])==120
    for folder in ['central-payload','quota-workspace','cache-workspace','spool-workspace']:
        image=json.loads((base/folder/'loop-state.json').read_text());assert image['released']
        assert not Path('/sys/block/loop'+str(image['number'])+'/loop/backing_file').exists()
    opened=[];records=[];intervals=[]
    try:
        for folder in ['central-payload','quota-workspace','cache-workspace','spool-workspace']:opened.append(Detached(base/folder))
        payload,work,cache,spool=opened
        assert len(work.json('/.ascend-lab-userquota.json')['slots'])==20
        assert len(cache.json('/.ascend-lab-userquota.json')['slots'])==20
        for volume in [work,spool]:
            if volume is spool:volume.empty('/attempts')
            else:
                for slot in volume.json('/.ascend-lab-userquota.json')['slots']:volume.empty('/'+slot['id'])
        with sqlite3.connect((base/'controller-state/jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            alljobs={ident:json.loads(raw) for ident,raw in db.execute('SELECT id,data FROM jobs')}
            assert len(alljobs)==66 and db.execute('SELECT count(*) FROM worker_inventory_holds WHERE held=1').fetchone()[0]==0
            for ident,expected in details['outcomes'].items():
                job=alljobs[ident];owner=expected['owner'];prefix='/jobs/'+ident+'/'
                assert job['state']=='succeeded' and job['owner']=='load_'+str(owner).zfill(2) and job['attempt_id']==expected['attempt_id']
                assert db.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(ident,)).fetchone()[0]==1
                report=payload.json(prefix+'results/peer-report.json');metadata=payload.json(prefix+'environment.json');receipt=payload.json(prefix+'results/_lab/environment.json')
                assert report==expected['report'] and report['uid']==metadata['task_uid']==23900+owner and report['answer']==42
                assert report['cap_effective']=='0000000000000000' and receipt['locked'] and receipt['workload_exit_code']==0
                assert metadata['device_nodes']==[] and metadata['network_mode']=='none' and metadata['read_only_root'] and not metadata['privileged']
                assert metadata['memory_limit']==256*1024**2 and metadata['node_spool']['enforced']
                assert metadata['persistent_cache']['enforced'] and report['cpu_ids']==job['allocation']['cpu_ids'] and len(report['cpu_ids'])==1
                assert payload.entry(prefix+'results/peer-report.json')['gid']==24500+owner
                if expected['phase']=='ir':assert b'arith.constant 42' in payload.operation('cat',prefix+'ir/output.mlir')
                elif expected['phase']=='cmake-cold':assert report['ccache_stats']['cache_miss']==2
                else:assert report['ccache_stats']['cache_miss']==0 and report['ccache_stats'].get('direct_cache_hit',0)+report['ccache_stats'].get('preprocessed_cache_hit',0)==2
                assert job['result']['execution_cleanup_confirmed'] and job['result']['workspace_release_confirmed'] and job['result']['cache_release_confirmed']
                intervals += [(report['started_unix'],1,owner),(report['finished_unix'],-1,owner)]
                records.append({'job_id':ident,'owner_ordinal':owner,'uid':metadata['task_uid'],'archive_gid':24500+owner,'phase':expected['phase'],'single_attempt':True})
            for ident in proof['quota']['cancelled_zero_attempt_jobs']:
                assert alljobs[ident]['state']=='cancelled' and db.execute('SELECT count(*) FROM execution_attempts WHERE job_id=?',(ident,)).fetchone()[0]==0
        # Authentication audit lives in its own DB; querying its known target
        # IDs exposes no raw actor, path, message, token or unrelated record.
        with sqlite3.connect((base/'controller-state/identities.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            for record in records:
                audited=dict(db.execute('SELECT outcome,count(*) FROM audit WHERE action=? AND target=? GROUP BY outcome',('job.submit',record['job_id'])))
                assert audited=={'created':1,'reused':1},audited
        running=set();peak=0
        for instant,delta,owner in sorted(intervals):
            if delta<0:running.remove(owner)
            else:assert owner not in running;running.add(owner);peak=max(peak,len(running))
        assert not running and peak==2 and len({x['uid'] for x in records})==20
        result={'passed':True,'parent':args.parent,'checked_workload_jobs':60,'distinct_task_uids':20,
            'raw_ext4_artifact_checks':True,'rootless_and_npu0':True,'actual_compute_interval_peak':peak,
            'controller_audit_created_once_reused_once_for_each_job':True,'five_cancelled_zero_attempts':True,
            'native_work_and_spool_empty':True,'report_records':records,
            'scope':'Read-only detached filesystems and SQLite from completed same-host CPU burst. No remount, recompilation, model or NPU.'}
        atomic_json(Path(os.environ['LAB_JOB_DIR'])/'results/team-burst-audit.json',result)
        print(json.dumps({k:result[k] for k in ['passed','checked_workload_jobs','distinct_task_uids','actual_compute_interval_peak']}))
    finally:
        for item in opened:item.close()


if __name__=='__main__':main()

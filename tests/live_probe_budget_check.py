"""Installed qualification under1/1/4MiB IO and64MiB personal/native quotas."""
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from large_mixed_compile_check import Acceptance
from lab_client import ClientHTTPError,atomic_json,now


class Qualification(Acceptance):
    def __init__(self):super().__init__(small=True)

    def workload(self):
        self.phase('qualification-budget-and-idempotent-replay')
        first=next(h for h in self.jobs if h['phase']=='probe')
        detail=self.detail(first);assert detail['job']['state']=='succeeded' and len(detail['attempts'])==1
        limits={'file_mib':1,'total_mib':2}
        assert detail['job']['artifact_limits']==limits
        body=json.loads((self.base/'intent-0-probe.json').read_text())['request']
        replay=self.clients[0].request('/api/jobs',body)
        assert replay['reused'] and replay['job']['id']==first['id'] and replay['job']['artifact_limits']==limits
        owner='load_01';self.admin('accessctl','quota',owner,'--archive-storage-mib','9')
        request={'template':'environment_probe','environment':'worker-python',
                 'parameters':{'node_id':self.enrollment['node_id'],'tools':['cmake']},
                 'idempotency_key':self.prefix+'-insufficient-metadata-budget'}
        try:self.clients[1].request('/api/jobs',request)
        except ClientHTTPError as error:assert error.status==409
        else:raise AssertionError('A9MiB budget cannot cover2MiB output plus8MiB metadata')
        self.admin('accessctl','quota',owner,'--archive-storage-mib','64')
        second=self.submit(1,'qualification',request)
        self.wait_all([second],180)
        assert self.detail(second)['job']['state']=='succeeded'
        self.phase('certified-small-budget-user-workload')
        source=self.base/'qualification-input';source.mkdir(mode=0o700)
        raw=(ROOT/'tests/workloads/mixed_small_build.py').read_bytes();(source/'peer.py').write_bytes(raw)
        identifier=self.clients[1].upload_input(source)['id']
        workflow=self.workflow(identifier,mode='ir')
        workflow['parameters'].update(resources={'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},timeout_seconds=180,
            workspace={'min_mib':16,'min_inodes':512,'durable':True},
            cache={'mode':'persistent','min_free_mib':1,'min_free_inodes':128},artifacts={'file_mib':1,'total_mib':4})
        task=self.submit(1,'ir',workflow);self.wait_all([task],180)
        detail=self.detail(task);assert detail['job']['state']=='succeeded' and len(detail['attempts'])==1
        report=self.artifact(task,'results/peer-report.json');assert report['answer']==42 and report['uid']==23901
        self.settle(cancel=False)
        record={'passed':True,'recorded_at':now(),'probe_jobs':[first['id'],second['id']],'ir_job':task['id'],
            'fixed_artifact_limits':limits,'central_io_limits':{'file_mib':1,'input_mib':1,'artifact_mib':4},
            'personal_archive_mib':64,'native_group_archive_mib':64,'nine_mib_denied_http':409,
            'probe_key_reused_original_single_attempt':True,'ir_answer':42,'ir_task_uid':23901,
            'workload_source_sha256':hashlib.sha256(raw).hexdigest(),'npu':0,'model_calls':0,
            'scope':'Same-host installed non-root controller,Root Node and two numeric task UIDs; fixed environment qualification and real CPU IR, no host login users, device workload or cross-host rollout.'}
        atomic_json(self.base/'qualification-proof.json',record)
        atomic_json(self.outer/'results/probe-budget-validation.json',record)
        print(json.dumps(record),flush=True)


def main():
    os.umask(0o077);run=Qualification()
    try:run.prepare();run.workload()
    finally:run.cleanup()
    assert run.state['cleaned'] and not run.state['cleanup_errors']
    path=run.outer/'results/probe-budget-validation.json';record=json.loads(path.read_text())
    record['cleanup_confirmed']=True;atomic_json(path,record)


if __name__=='__main__':main()

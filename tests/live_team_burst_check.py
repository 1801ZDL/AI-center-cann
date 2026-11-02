"""Twenty token identities and task UIDs, two CPU slots, concurrent replay.

Uses the existing owned installed-role/native-quota fixture at a small explicit
envelope. No Main configuration, driver, model or device work is changed.
"""
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from large_mixed_compile_check import Acceptance,TERMINAL
from lab_client import Client,ClientHTTPError,ClientUnavailable,atomic_json,now


class Burst(Acceptance):
    def __init__(self):
        super().__init__(small=True)
        self.intents={};self.responses=[];self.snapshots=[];self.outcomes={};self.sampling_stop=threading.Event();self.sampling_error=[]

    def workflow(self,identifier,mode):
        body=super().workflow(identifier,mode=mode)
        body['parameters'].update(resources={'cpu':1,'memory_mib':256,'shm_mib':0,'npu':0},timeout_seconds=180,
            workspace={'min_mib':16,'min_inodes':512,'durable':True},
            cache={'mode':'persistent','min_free_mib':1,'min_free_inodes':128},
            artifacts={'file_mib':1,'total_mib':4})
        return body

    def intent(self,owner,phase,body):
        request=copy.deepcopy(body);request.update(name='Team burst '+str(owner).zfill(2)+' '+phase,
            idempotency_key=self.prefix+'-'+str(owner)+'-'+phase)
        value={'owner':owner,'phase':phase,'request':request};self.intents[request['idempotency_key']]=value
        atomic_json(self.base/('intent-'+str(owner)+'-'+phase+'.json'),value)
        return value

    def remember(self,value,reply):
        ident=reply['job']['id'];existing=next((v for v in self.jobs if v['id']==ident),None)
        if not existing:self.jobs.append({'owner':value['owner'],'phase':value['phase'],'id':ident})
        value['job_id']=ident;self.save();return ident

    def resolve(self):
        # Replay only the exact recorded operation. An observer failure never
        # creates a replacement key or authorizes a local command.
        for value in self.intents.values():
            if 'job_id' not in value:
                self.remember(value,self.clients[value['owner']].request('/api/jobs',value['request']))

    def settle(self,cancel):
        self.resolve()
        return super().settle(cancel)

    def wave(self,label,mode,inputs,dependencies=None):
        values=[]
        for owner in range(20):
            body=self.workflow(inputs[owner],mode)
            if dependencies:body['depends_on']=[dependencies[owner]]
            values.append(self.intent(owner,label,body))
        self.safe_cleanup=False;barrier=threading.Barrier(20)
        def one(value):
            client=Client(self.clients[value['owner']].config);barrier.wait(timeout=30);replies=[]
            for replay in [False,True]:
                started=time.monotonic()
                try:reply=client.request('/api/jobs',value['request'])
                except ClientUnavailable:
                    # Original call may have committed; keep the exact body.
                    reply=client.request('/api/jobs',value['request'])
                replies.append(reply)
                duration=time.monotonic()-started
                # Profiles and raw HTTP bodies never enter this metrics record.
                self.responses.append({'owner':value['owner'],'phase':label,'replay':replay,
                    'seconds':duration,'job_id':reply['job']['id'],'reused':reply['reused']})
            return value,replies
        errors=[];result={}
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures=[pool.submit(one,value) for value in values]
            for future in concurrent.futures.as_completed(futures):
                try:
                    value,replies=future.result();assert replies[0]['job']['id']==replies[1]['job']['id'] and replies[1]['reused']
                    result[value['owner']]=self.remember(value,replies[0])
                except Exception as error:errors.append(type(error).__name__)
        atomic_json(self.base/'submission-metrics.json',self.responses)
        if errors:
            self.resolve()  # Resolve acknowledged/unknown original keys first.
            raise AssertionError('Inspect original burst requests; no replacement submissions: '+','.join(errors))
        assert len(result)==20 and len(set(result.values()))==20
        return result

    def sample(self):
        while not self.sampling_stop.is_set():
            try:
                value=self.controller_client.request('/api/jobs');domain=value['resource_domains'][0];r=domain['reserved']
                assert len(value['resource_domains'])==1 and r['slots']<=2 and r['cpu']<=2 and r['memory_mib']<=1024
                assert len(r['cpu_ids'])==len(set(r['cpu_ids'])) and set(r['cpu_ids'])<=set(self.cpus)
                active=[j for j in value['jobs'] if j['state'] in {'reserved','running','cancelling','needs_attention'}]
                assert len({j['owner'] for j in active})==len(active),'Per-owner max_running=1 was exceeded'
                self.snapshots.append({'at':now(),'counts':value['counts'],'reserved':r,
                    'active':[{'id':j['id'],'owner':j['owner'],'state':j['state']} for j in active]})
            except ClientUnavailable:pass  # Missing observation stays missing.
            except Exception as error:self.sampling_error.append(type(error).__name__);return
            self.sampling_stop.wait(.5)

    def quota_check(self,inputs):
        self.ctl('node','stop');created=[]
        # All four original keys are known, queued, and canceled before restart.
        for index in range(4):
            value=self.intent(0,'quota-'+str(index),self.workflow(inputs[0],'ir'))
            reply=self.clients[0].request('/api/jobs',value['request']);self.remember(value,reply)
            assert reply['job']['state']=='queued';created.append(reply['job']['id'])
        extra=self.workflow(inputs[0],'ir');extra['idempotency_key']=self.prefix+'-quota-refused'
        try:self.clients[0].request('/api/jobs',extra)
        except ClientHTTPError as error:assert error.status==409
        else:raise AssertionError('Per-owner queued limit was bypassed')
        # A foreign owner still has its independent queue allowance.
        value=self.intent(1,'quota-peer',self.workflow(inputs[1],'ir'))
        reply=self.clients[1].request('/api/jobs',value['request']);self.remember(value,reply);created.append(reply['job']['id'])
        for ident in created:
            owner=next(j['owner'] for j in self.jobs if j['id']==ident)
            self.clients[owner].request('/api/jobs/'+ident+'/cancel',{})
            detail=self.clients[owner].request('/api/jobs/'+ident)
            assert detail['job']['state']=='cancelled' and not detail['attempts']
        assert self.ctl('node','start')['ready']
        return {'queued_limit':4,'over_limit_status':409,'foreign_owner_still_submits':True,'cancelled_zero_attempt_jobs':created}

    def workloads(self):
        self.phase('upload-one-sealed-input-per-owner');source=self.base/'burst-input';source.mkdir(mode=0o700)
        raw=(ROOT/'tests/workloads/mixed_small_build.py').read_bytes();(source/'peer.py').write_bytes(raw)
        inputs=[client.upload_input(source)['id'] for client in self.clients]
        quota=self.quota_check(inputs)
        self.phase('twenty-concurrent-clients-and-keyed-replays')
        sampler=threading.Thread(target=self.sample,daemon=True);sampler.start()
        try:
            first=self.wave('ir','ir',inputs)
            cold=self.wave('cmake-cold','cmake',inputs)
            warm=self.wave('cmake-warm','cmake',inputs,cold)
            identifiers=set(first.values())|set(cold.values())|set(warm.values());assert len(identifiers)==60
            handles=[h for h in self.jobs if h['id'] in identifiers]
            details=self.wait_all(handles,900)
            for h,detail in zip(handles,details):
                job=detail['job'];assert job['state']=='succeeded',(h['id'],job['state'],job.get('reason'))
                assert len(detail['attempts'])==1 and job['result']['execution_cleanup_confirmed'] and job['result']['workspace_release_confirmed']
                report=self.artifact(h,'results/peer-report.json');assert report['answer']==42 and report['uid']==23900+h['owner']
                assert report['cap_effective']=='0000000000000000' and report['no_new_privileges'] and report['npu_kernel_launches']==0
                if h['phase']=='cmake-cold':assert report['ccache_stats']['cache_miss']==2
                if h['phase']=='cmake-warm':assert report['ccache_stats']['cache_miss']==0 and report['ccache_stats'].get('direct_cache_hit',0)+report['ccache_stats'].get('preprocessed_cache_hit',0)==2
                self.outcomes[h['id']]={'owner':h['owner'],'phase':h['phase'],'attempt_id':job['attempt_id'],
                    'reserved_at':job['reserved_at'],'finished_at':job['finished_at'],'created_at':job['created_at'],
                    'report':report,'cache_key':job['allocation']['cache']['key']}
            ordered=sorted(self.outcomes.values(),key=lambda v:(v['reserved_at'],v['attempt_id']))
            assert len({v['owner'] for v in ordered[:20]})==20,'A second service occurred before every eligible owner received its first'
            for owner in range(20):
                a=self.outcomes[cold[owner]];b=self.outcomes[warm[owner]]
                assert a['finished_at']<=b['reserved_at'] and a['cache_key']==b['cache_key']
                stranger=self.clients[(owner+1)%20]
                try:stranger.request('/api/jobs/'+first[owner])
                except ClientHTTPError as error:assert error.status==404
                else:raise AssertionError('Foreign owner read another task')
            self.settle(cancel=False)
        finally:
            self.sampling_stop.set();sampler.join(timeout=20)
        assert not self.sampling_error and not sampler.is_alive() and self.snapshots
        assert max(v['reserved']['slots'] for v in self.snapshots)==2,'Two-slot execution was not observed'
        durations=sorted(v['seconds'] for v in self.responses)
        from datetime import datetime
        waits=sorted((datetime.fromisoformat(v['reserved_at'])-datetime.fromisoformat(v['created_at'])).total_seconds() for v in self.outcomes.values())
        record={'passed':True,'recorded_at':now(),'logical_owners':20,'task_uids':list(range(23900,23920)),
            'concurrent_http_clients':20,'submit_and_replay_calls':len(self.responses),'workload_jobs':60,
            'per_job_single_attempt':True,'first_twenty_grants_distinct_owners':True,'foreign_reads_rejected':20,
            'quota':quota,'peak_slots':2,'native_workspace_and_private_cache':True,'actual_cmake_and_ir_answer':42,
            'http_seconds':{'p50':durations[len(durations)//2],'p95':durations[(len(durations)*95-1)//100],'max':max(durations)},
            'queue_seconds':{'p50':waits[len(waits)//2],'p95':waits[(len(waits)*95-1)//100],'max':max(waits)},
            'server_release':self.release_info['release_id'],'workload_source_sha256':hashlib.sha256(raw).hexdigest(),
            'actual_controller_sources_frozen':True,'npu_kernel_launches':0,'model_calls':0,
            'main_configuration_changed':False,'host_accounts_created':False,
            'scope':'20 simultaneous token clients and20 numeric task UIDs on one server with two actual CPU slots; three queued workload waves, real IR/CMake cold-warm and private cache. Not20 simultaneously running jobs, full compiler rebuild, NPU, real login users, PID1 or two physical hosts.'}
        atomic_json(self.base/'burst-details.json',{'responses':self.responses,'outcomes':self.outcomes,'snapshots':self.snapshots})
        atomic_json(self.outer/'results/team-burst-validation.json',record);print(json.dumps(record),flush=True)


def main():
    os.umask(0o077);run=Burst()
    try:run.prepare();run.workloads()
    finally:
        run.sampling_stop.set()
        run.cleanup()
    assert run.state.get('cleaned') and not run.state.get('cleanup_errors')
    path=run.outer/'results/team-burst-validation.json';value=json.loads(path.read_text());value['cleanup_confirmed']=True;atomic_json(path,value)


if __name__=='__main__':main()

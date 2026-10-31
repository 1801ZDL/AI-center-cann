"""Durable owner-bound workflow dependencies and incremental artifact handoff.

Only existing immutable job IDs may be parents. Waiting/copying children stay
queued without compute reservations; inputs are sealed before a start grant.
"""
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import PurePosixPath
import re

from jobs import JobError,TERMINAL,atomic_json,now,read_json
from workflow_spec import WORKFLOWS
from inputs import safe_name
from archive_api import open_artifact,indexed_names,relative_name
from storage_files import lock,digest
from dependency_conditions import conditions,confirmation,uses_terminal

MAX_PARENTS=8
MAX_IMPORTS=32
COPY_BYTES_PER_TICK=1024**2


class InvalidDependency(JobError):
    def __init__(self,message):super().__init__(message,409)


def fields(request):
    parents=request.get('depends_on',[]);imports=request.get('artifact_inputs',[])
    if (not isinstance(parents,list) or len(parents)>MAX_PARENTS or len(set(str(x) for x in parents))!=len(parents)
        or any(not isinstance(p,str) or not re.fullmatch('job-[0-9a-f]{12}',p) for p in parents)):
        raise JobError('Dependencies require up to eight distinct existing job IDs')
    if not isinstance(imports,list) or len(imports)>MAX_IMPORTS:raise JobError('Choose at most 32 declared artifact inputs')
    clean=[];targets=set()
    for item in imports:
        if (not isinstance(item,dict) or set(item)-{'job_id','name','target','sha256','executable','when','source_grant'} or not {'job_id','name','target'}<=set(item)
            or item['job_id'] not in parents or not relative_name(item['name']) or not item['name'].startswith(('results/','ir/','profile/'))
            or not safe_name(item['target']) or item['target'] in targets
            or type(item.get('executable',False)) is not bool or item.get('when','success') not in ('success','terminal')
            or 'sha256' in item and not re.fullmatch('[0-9a-f]{64}',str(item['sha256']))):raise JobError('Invalid declared artifact input or target')
        targets.add(item['target']);clean.append(dict(item))
        if 'source_grant' in item:
            from source_grants import identity
            identity(item['source_grant'])
    for parent in parents:
        selected=[x for x in clean if x['job_id']==parent]
        grants={x.get('source_grant') for x in selected}
        if len(grants)>1:raise JobError('All imports from a parent require the same source grant or ordinary access')
    try:conditions(parents,clean)
    except (ValueError,TypeError):raise JobError('All imports from a parent require the same success or terminal condition') from None
    for item in clean:
        if item.get('when')=='success':item.pop('when')
    if imports and not parents:raise JobError('Artifact inputs require declared parent jobs')
    if (parents or imports) and (request.get('template') not in WORKFLOWS or request.get('environment')!='worker-python'):
        raise JobError('Dependencies are supported only for managed versioned workflows')
    return {'depends_on':sorted(parents),'artifact_inputs':sorted(clean,key=lambda x:x['target'])} if parents else {}


class Dependencies:
    def __init__(self,jobs):self.jobs=jobs;self.cursor=0

    def validate(self,request,principal):
        result=fields(request)
        if not result:return {}
        with self.jobs.connect() as db:
            from experiment_budget import select
            self.jobs.source_grants.check({**result,**select(request,principal),'owner':principal.id},db,active=False)
            granted={x['job_id'] for x in result['artifact_inputs'] if 'source_grant' in x}
            # Authorize direct edges. Ancestry is internal owner-bound graph
            # validation; it conveys no additional read access to the caller.
            for key in result['depends_on']:
                parent=self.jobs.get(key,db,None if key in granted else principal)
                if parent['owner']!=principal.id:raise JobError('Dependency job not found',404)
            visited=set();stack=[(p,1) for p in result['depends_on']]
            while stack:
                identifier,depth=stack.pop()
                if depth>64 or len(visited)>512:raise JobError('Dependency ancestry exceeds the supported bound')
                if identifier in visited:continue
                visited.add(identifier);parent=self.jobs.get(identifier,db)
                if parent['owner']!=principal.id:raise JobError('Dependency job not found',404)
                if parent.get('template') not in WORKFLOWS or parent['_environment']['type']!='worker':
                    raise JobError('Parent must be a managed versioned workflow')
                stack.extend((p,depth+1) for p in parent.get('depends_on',[]))
        base=self.jobs.inputs.get(request['parameters']['input_id'],principal)
        names={x['name'] for x in base['files']}|{x['target'] for x in result['artifact_inputs']}
        if (len(names)!=len(base['files'])+len(result['artifact_inputs'])
            or any(str(p) in names for n in names for p in PurePosixPath(n).parents if str(p)!='.')):
            raise JobError('Artifact input targets collide with source input files')
        if len(names)>self.jobs.inputs.MAX_FILES:raise JobError('Combined dependency input exceeds the file-count limit')
        return result

    def available(self,spec,db):
        self.jobs.source_grants.check(spec,db)
        for parent in spec.get('depends_on',[]):self.jobs.lifecycle.available('archive',parent,db)

    def reason(self,job,db=None):
        if any('source_grant' in x for x in job.get('artifact_inputs',[])):
            try:
                if db is None:
                    with self.jobs.connect() as connection:self.jobs.source_grants.check(job,connection)
                else:self.jobs.source_grants.check(job,db)
            except JobError:return '来源文件授权不可用或证据已变化，保留原输入与许可等待本人核对'
        if job.get('depends_on') and job.get('dependency_state',{}).get('status')!='ready':
            fallback=('等待上游确认结束、清退及阶段输入封存；尚未预留计算资源' if uses_terminal(job)
                else '等待上游成功及阶段输入封存；尚未预留计算资源')
            return job.get('dependency_state',{}).get('reason',fallback)
        if uses_terminal(job):
            try:self.confirm_frozen(job,db)
            except (InvalidDependency,ValueError,KeyError,TypeError):return '上游诊断证据边界已变化，保留原输入与许可等待核对'
        return None

    def terminal_proofs(self,job,parents):
        try:modes=conditions(job['depends_on'],job['artifact_inputs'])
        except (KeyError,ValueError,TypeError):raise InvalidDependency('Invalid persisted artifact conditions') from None
        proof={}
        for parent in parents:
            if modes[parent['id']]!='terminal':continue
            try:value,reason=confirmation(parent)
            except ValueError as error:raise InvalidDependency(str(error)) from None
            if reason:return None,reason
            proof[parent['id']]=value
        return proof,None

    def confirm_frozen(self,job,db=None):
        if not uses_terminal(job):return
        parents=[self.jobs.get(p,db) for p in job['depends_on']]
        if any(p['owner']!=job['owner'] for p in parents):raise InvalidDependency('Dependency ownership changed')
        value,reason=self.terminal_proofs(job,parents)
        expected=job.get('input_provenance',{}).get('terminal_sources')
        if expected is None:expected=job.get('_dependency_binding',{}).get('provenance',{}).get('terminal_sources')
        if reason or not value or value!=expected:raise InvalidDependency('Confirmed terminal producer evidence changed')

    def pinned(self,parent,db):
        return (self.jobs.source_grants.pinned(parent,db) or
            any(j['state'] not in TERMINAL and parent in j.get('depends_on',[]) for j in self.jobs.all(db)))

    def upstream(self,job):
        parents=[self.jobs.get(p) for p in job['depends_on']]
        if any(p['owner']!=job['owner'] for p in parents):raise InvalidDependency('Dependency ownership changed; refusing handoff')
        return parents

    def change(self,identifier,update):
        with self.jobs.transaction() as db:
            job=self.jobs.get(identifier,db)
            if job['state']!='queued' or job.get('attempt_id'):return False
            job.update(update);self.jobs.write(db,job);return True

    def stop_unstarted(self,job,parent):
        reason='上游任务 '+parent['id']+' 以 '+parent['state']+' 结束，下游未启动'
        result={'job_id':job['id'],'status':'cancelled','exit_code':None,'started_at':None,'finished_at':now(),
            'failure_stage':'dependency','upstream_job_id':parent['id'],'upstream_state':parent['state'],
            'execution_started':False,'reason':reason}
        # Terminal metadata remains a normal archived result, with zero attempts.
        with self.jobs.transaction() as db:
            actual=self.jobs.get(job['id'],db)
            if actual['state']!='queued' or actual.get('attempt_id'):return
            directory=self.jobs.directory(job['id']);atomic_json(directory/'result.json',result)
            actual.update(state='cancelled',reason=reason,finished_at=result['finished_at'],result=result,
                dependency_state={'status':'upstream_failed','reason':reason})
            actual['workflow_result']=self.jobs.results.unstarted_dependency(actual,result,db)
            from executor import artifact_index
            atomic_json(directory/'artifacts.json',{'files':artifact_index(directory)});self.jobs.write(db,actual)

    def description(self,job,parents):
        base=self.jobs.inputs.get(job['_dependency_base_input'],owner=job['owner'])
        self.jobs.lifecycle.available('input',base['id'])
        if not base['sealed']:raise InvalidDependency('Base input is not sealed')
        files=[dict(x) for x in base['files']]
        sources={x['name']:{'kind':'input','input_id':base['id'],'name':x['name']} for x in files}
        provenance=[];lookup={p['id']:p for p in parents};terminal_proofs,reason=self.terminal_proofs(job,parents)
        if reason:raise InvalidDependency('Terminal source confirmation changed before handoff')
        for item in job['artifact_inputs']:
            parent=lookup[item['job_id']];directory=self.jobs.directory(parent['id'])
            hashes=read_json(directory/'artifact-hashes.json')
            if not isinstance(hashes,dict) or hashes.get('version')!=1 or not isinstance(hashes.get('files'),list):
                raise InvalidDependency('Parent lacks its confirmed artifact checksum receipt')
            if item.get('when')=='terminal':
                from node_storage import manifest_sha256
                try:confirmed_sha=manifest_sha256(hashes['files'])
                except ValueError:raise InvalidDependency('Invalid terminal producer artifact manifest') from None
                if confirmed_sha!=terminal_proofs[parent['id']]['artifact_manifest_sha256']:
                    raise InvalidDependency('Terminal source artifact manifest differs from its confirmed receipt')
            matches=[r for r in hashes['files'] if r.get('name')==item['name']]
            if len(matches)!=1 or item['name'] not in indexed_names(directory):raise InvalidDependency('Declared parent artifact was not confirmed and indexed')
            artifact=matches[0]
            if (not re.fullmatch('[0-9a-f]{64}',str(artifact.get('sha256')))
                or type(artifact.get('size')) is not int or artifact['size']<0):raise InvalidDependency('Invalid producer artifact checksum receipt')
            if item.get('sha256',artifact['sha256'])!=artifact['sha256']:raise InvalidDependency('Artifact SHA differs from the explicitly requested digest')
            files.append({'name':item['target'],'size':artifact['size'],'sha256':artifact['sha256'],'executable':item.get('executable',False)})
            sources[item['target']]={'kind':'archive','job_id':parent['id'],'name':item['name']}
            provenance.append({'job_id':parent['id'],'attempt_id':parent.get('attempt_id'),'name':item['name'],
                'target':item['target'],'size':artifact['size'],'sha256':artifact['sha256'],
                'producer_input_sha256':parent.get('input',{}).get('manifest_sha256'),
                'producer_environment_sha256':parent.get('environment_sha256')})
            if item.get('when')=='terminal':provenance[-1].update(when='terminal',producer_state=parent['state'])
            if 'source_grant' in item:provenance[-1]['source_grant']=item['source_grant']
        try:manifest=self.jobs.inputs.manifest(files)
        except JobError as error:raise InvalidDependency(str(error)) from None
        return manifest,sources,{'version':1,'base_input_id':base['id'],'base_manifest_sha256':base['manifest_sha256'],
            'parents':[{ 'job_id':p['id'],'attempt_id':p.get('attempt_id'),'state':p['state']} for p in parents],
            'artifacts':provenance,**({'terminal_sources':terminal_proofs} if terminal_proofs else {})}

    def prepare(self,identifier):
        if not isinstance(identifier,str) or not re.fullmatch('job-[0-9a-f]{12}',identifier):raise JobError('Dependency job not found',404)
        with lock(self.jobs.runtime,'dependency-'+identifier,exclusive=True):
            job=self.jobs.get(identifier)
            if job['state']!='queued' or job.get('attempt_id') or not job.get('depends_on') or job.get('dependency_state',{}).get('status') in {'ready','blocked'}:return
            if self.jobs.dispatch_permission(job,[]):return
            with self.jobs.connect() as db:self.jobs.source_grants.check(job,db)
            parents=self.upstream(job)
            try:modes=conditions(job['depends_on'],job['artifact_inputs'])
            except (KeyError,ValueError,TypeError):raise InvalidDependency('Invalid persisted artifact conditions') from None
            bad=next((p for p in parents if modes[p['id']]=='success' and p['state'] in TERMINAL and p['state']!='succeeded'),None)
            if bad:self.stop_unstarted(job,bad);return
            if any(modes[p['id']]=='success' and p['state']!='succeeded' for p in parents):
                self.change(identifier,{'dependency_state':{'status':'waiting','reason':'等待上游任务成功，尚未预留计算资源'},'reason':'等待上游任务成功，尚未预留计算资源'});return
            _,reason=self.terminal_proofs(job,parents)
            if reason:
                self.change(identifier,{'dependency_state':{'status':'waiting','reason':reason},'reason':reason});return
            if not job['artifact_inputs']:
                self.change(identifier,{'dependency_state':{'status':'ready','prepared_at':now()},'reason':'阶段依赖已完成，等待资源分配'});return
            principal=self.jobs.access.user(job['owner'])
            if principal is None:return
            with ExitStack() as stack:
                stack.enter_context(self.jobs.lifecycle.reader('input',job['_dependency_base_input']))
                for parent in sorted(parents,key=lambda p:p['id']):stack.enter_context(self.jobs.lifecycle.reader('archive',parent['id']))
                manifest,sources,provenance=self.description(job,parents)
                binding={'files':manifest,'sources':sources,'provenance':provenance}
                if job.get('_dependency_binding') and job['_dependency_binding']!=binding:raise InvalidDependency('Frozen dependency handoff changed; preserve retained data')
                planned=self.jobs.inputs.identity(job['owner'],manifest)
                pre_stage={'status':'copying','input_id':planned,'total_bytes':sum(i['size'] for i in manifest),
                    'copied_bytes':job.get('dependency_state',{}).get('copied_bytes',0),
                    'reason':'阶段产物交接中；尚未预留计算资源'}
                # Pin a deterministic destination before InputStore publishes
                # its row. Check prior retention in this same transaction so
                # pre-existing trash stays restorable, never newly pinned.
                with self.jobs.transaction() as db:
                    actual=self.jobs.get(identifier,db)
                    if actual['state']!='queued' or actual.get('attempt_id'):return
                    self.jobs.source_grants.check(actual,db)
                    self.jobs.lifecycle.available('input',planned,db)
                    actual.update(_dependency_binding=binding,dependency_state=pre_stage,reason=pre_stage['reason']);self.jobs.write(db,actual)
                bundle,_=self.jobs.inputs.create(principal,{'files':manifest})
                if bundle['id']!=planned:raise InvalidDependency('Derived input identity differs from its pinned manifest')
                stage={'status':'copying','input_id':bundle['id'],'total_bytes':bundle['bytes'],
                    'copied_bytes':sum(bundle['uploaded'].values()),'reason':'阶段产物交接中；尚未预留计算资源'}
                if not self.change(identifier,{'_dependency_binding':binding,'dependency_state':stage,'reason':stage['reason']}):return
                remaining=COPY_BYTES_PER_TICK
                if not bundle['sealed']:
                    for item in manifest:
                        offset=bundle['uploaded'].get(item['name'],0)
                        if offset==item['size']:continue
                        source=sources[item['name']]
                        directory=(self.jobs.inputs.directory(source['input_id']) if source['kind']=='input' else self.jobs.directory(source['job_id']))
                        with open_artifact(directory,source['name'],self.jobs.inputs.MAX_FILE) as stream:
                            before=os.fstat(stream.fileno())
                            if before.st_size!=item['size']:raise InvalidDependency('Producer file size changed during dependency transfer')
                            stream.seek(offset)
                            while remaining and offset<item['size']:
                                with self.jobs.connect() as db:self.jobs.source_grants.check(job,db)
                                block=stream.read(min(self.jobs.inputs.CHUNK,remaining,item['size']-offset))
                                if not block:raise InvalidDependency('Producer input ended during dependency transfer')
                                self.jobs.inputs.upload(bundle['id'],principal,{'name':item['name'],'offset':offset,'data':base64.b64encode(block).decode()})
                                offset+=len(block);remaining-=len(block)
                            after=os.fstat(stream.fileno())
                            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                                raise InvalidDependency('Source changed during dependency transfer')
                        if not remaining:break
                    bundle=self.jobs.inputs.get(bundle['id'],principal)
                    if any(bundle['uploaded'].get(x['name'],0)!=x['size'] for x in manifest):
                        stage['copied_bytes']=sum(bundle['uploaded'].values());self.change(identifier,{'dependency_state':stage});return
                    try:bundle=self.jobs.inputs.seal(bundle['id'],principal)
                    except JobError as error:raise InvalidDependency('Dependency input checksum validation failed; retained data requires inspection') from error
                proof={**provenance,'input_id':bundle['id'],'manifest_sha256':bundle['manifest_sha256']}
                with self.jobs.transaction() as db:
                    actual=self.jobs.get(identifier,db)
                    if actual['state']!='queued' or actual.get('attempt_id'):return
                    if self.jobs.dispatch_permission(actual,[]):return
                    self.jobs.source_grants.check(actual,db)
                    self.confirm_frozen(actual,db)
                    actual['parameters']['input_id']=bundle['id']
                    actual['input']={k:bundle[k] for k in ['id','owner','files','manifest_sha256','bytes']}
                    actual['input_provenance']=proof
                    actual['dependency_state']={'status':'ready','input_id':bundle['id'],'manifest_sha256':bundle['manifest_sha256'],
                        'provenance_sha256':digest(proof),'prepared_at':now()}
                    actual['reason']='阶段输入已封存，等待资源分配'
                    atomic_json(self.jobs.directory(identifier)/'dependency-inputs.json',proof);self.jobs.write(db,actual)

    def tick(self):
        pending=[j['id'] for j in self.jobs.all() if j['state']=='queued' and j.get('depends_on')
            and j.get('dependency_state',{}).get('status') not in {'ready','blocked'}]
        # Every item copies at most1MiB; rotate so a large artifact cannot starve
        # later ready stages. No compute allocation is held by this transfer.
        selected=(pending[self.cursor:]+pending[:self.cursor])[:4]
        self.cursor=(self.cursor+len(selected))%len(pending) if pending else 0
        for identifier in selected:
            try:self.prepare(identifier)
            except (JobError,OSError) as error:
                permanent=isinstance(error,InvalidDependency)
                reason=('阶段输入完整性或声明不符，保留数据等待核对' if permanent else '阶段输入交接暂不可用，保留进度等待容量或存储恢复')
                existing=self.jobs.get(identifier);state=dict(existing.get('dependency_state',{}))
                state.update(status='blocked' if permanent else 'waiting_storage',reason=reason)
                self.change(identifier,{'dependency_state':state,'reason':reason})

    def retry(self,identifier,principal):
        principal.require('jobs:submit')
        if not isinstance(identifier,str) or not re.fullmatch('job-[0-9a-f]{12}',identifier):raise JobError('Dependency job not found',404)
        with lock(self.jobs.runtime,'dependency-'+identifier,exclusive=True),self.jobs.transaction() as db:
            job=self.jobs.get(identifier,db,principal)
            if job['owner']!=principal.id:raise JobError('Dependency job not found',404)
            if job['state']!='queued' or job.get('attempt_id') or not job.get('depends_on'):raise JobError('Only an unstarted dependency job can retry input handoff',409)
            job['dependency_state']={**job.get('dependency_state',{}),'status':'waiting','reason':'用户要求按原冻结摘要继续输入交接'}
            self.jobs.write(db,job)
            return {'accepted':True,'job_id':identifier,'source_binding_changed':False}

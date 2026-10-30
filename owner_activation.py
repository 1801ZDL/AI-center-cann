"""Local controller administration: ready resource bindings, then private credentials.

Credentials are durable before the single identities transaction enables login.
The jobs write transaction freezes Node profiles/revocation during that decision.
No remote activation endpoint, OS account creation or automatic device probe.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

import owner_identity
from jobs import JobError,identity_alive,now,read_json
from lab_client import atomic_json,controller_url,authority_bytes
from storage_files import private_tree,lock

CAPABILITY='owner-activation-v1'
FORMAT='ascend-lab-owner-activation/v1'


def _checkpoint(name):pass


def request(value):
    from auth import IDENTIFIER
    if (not isinstance(value,dict) or set(value)!={'owner','stage_id','required_nodes'}
        or not isinstance(value['owner'],str) or not IDENTIFIER.fullmatch(value['owner'])
        or not isinstance(value['stage_id'],str) or not re.fullmatch('[0-9a-f]{64}',value['stage_id'])
        or not isinstance(value['required_nodes'],list) or not 1<=len(value['required_nodes'])<=32):
        raise JobError('Activation requires owner, stage_id and an explicit nonempty required_nodes list',400)
    seen=set()
    for item in value['required_nodes']:
        if (not isinstance(item,dict) or set(item)!={'node_id','plan_id'}
            or not isinstance(item['node_id'],str) or not re.fullmatch('worker-[0-9a-f]{12}',item['node_id'])
            or not isinstance(item['plan_id'],str) or not re.fullmatch('[0-9a-f]{64}',item['plan_id'])
            or item['node_id'] in seen):raise JobError('Required Nodes must be unique enrolled IDs with exact handoff plan IDs',400)
        seen.add(item['node_id'])
    return {**value,'required_nodes':sorted(value['required_nodes'],key=lambda v:v['node_id'])}


def _sync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def _read(path):
    if not path.exists() and not path.is_symlink():return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>512*1024:
            raise JobError('Activation files must be private service-owned bounded regular files',409)
        return json.load(file)


def _save(path,value):
    atomic_json(path,value);_sync(path.parent)


class Activation:
    def __init__(self,jobs):
        self.jobs=jobs;self.runtime=jobs.runtime;self.access=jobs.access
        with self.access.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS owner_activations(owner TEXT PRIMARY KEY,id TEXT UNIQUE NOT NULL,record TEXT NOT NULL)')

    def controller_groups(self):
        ready=read_json(self.runtime/'server-ready.json') or {}
        if not identity_alive(ready) or ready.get('uid')!=os.geteuid() or CAPABILITY not in ready.get('capabilities',[]):
            raise JobError('A live matching controller with owner activation support is required',409)
        try:
            status={k:v.split() for k,separator,v in (line.partition(':') for line in Path('/proc',str(ready['pid']),'status').read_text().splitlines()) if separator}
            groups={int(v) for v in status['Groups']}|{int(status['Gid'][1])}
        except (OSError,KeyError,ValueError,IndexError):raise JobError('Cannot verify the running controller storage groups',409) from None
        if not identity_alive(ready):raise JobError('Controller changed while validating activation',409)
        return groups

    def readiness(self,spec,db,profile):
        """Current, authenticated observations, never just historical online_seen."""
        from environment_contract import verify_config,fingerprint
        from nodes import REQUIRED_CAPABILITIES
        from workflow_spec import CAPABILITIES
        from node_owner_admin import sha
        storage=self.jobs.nodes.handoff._storage(spec['owner'],profile)
        if storage['gid'] not in {*os.getgroups(),os.getegid()} or storage['gid'] not in self.controller_groups():
            raise JobError('Restart the controller with the provisioned storage supplementary group before activation',409)
        quota=profile['quota']
        if any(quota[k]<1 for k in ['cpu','memory_mib','max_running','max_queued','input_storage_mib','archive_storage_mib']):
            raise JobError('Staged quotas must permit a runnable managed task',409)
        from io_policy import MIB,META
        if quota['memory_mib']<256 or quota['archive_storage_mib']*MIB<2*MIB+META:
            raise JobError('Staged quotas cannot fit the minimum managed task and archive metadata reservation',409)
        result=[]
        for target in spec['required_nodes']:
            record=self.jobs.nodes.handoff.get(target['plan_id'],db);node=self.jobs.nodes.node(target['node_id'],db)
            p=node['profile'];report=node['report'];caps=set(report.get('capabilities',[]))
            if (record['node_id']!=target['node_id'] or record['owner']!=spec['owner'] or record['stage_id']!=spec['stage_id']
                or record['state']!='ready' or not record.get('node_online_seen') or sha(p)!=sha(record['after_profile'])
                or record['storage']!=storage):raise JobError('Every required Node needs its matching ready handoff and unchanged profile/storage',409)
            if not 0<=time.time()-node['last_seen']<=30:raise JobError('A required Node heartbeat is stale',409)
            if report.get('disk_free_bytes',0)<512*1024**2 or report.get('memory_available_bytes',0)<(384+512)*1024**2:
                raise JobError('A required Node lacks the minimum current memory/disk headroom for managed task admission',409)
            self.jobs.nodes.handoff._idle(node['id'],p,db)
            domain=self.jobs.resources.get(p['physical_id'],db)
            if domain['draining']:raise JobError('A required resource domain is draining',409)
            if not (REQUIRED_CAPABILITIES|CAPABILITIES)<=caps:raise JobError('A required Node lacks managed workflow capabilities',409)
            if not {'build_v1','ir_v1'}&set(p['templates']):raise JobError('A required Node must authorize a managed build or IR workflow',409)
            try:env=verify_config(p,host=False)
            except ValueError:raise JobError('A required Node has no consistent certified environment',409) from None
            if not env or report.get('environment_sha256')!=env:raise JobError('A required Node has not confirmed its certified environment',409)
            if self.jobs.policy.get('require_pinned_docker') and 'pinned-docker-runtime-v1' not in caps:
                raise JobError('A required Node has not confirmed its pinned Docker runtime',409)
            if self.jobs.policy.get('require_attempt_inventory') and not report.get('attempt_inventory',{}).get('reconciled'):
                raise JobError('A required Node inventory is not reconciled',409)
            if (not p.get('spool_storage') or report.get('finite_spool',{}).get('status')!='ok'
                or report.get('artifact_gc_ready') is not True):raise JobError('A required Node needs healthy finite artifact spool storage',409)
            from node_storage import CAPABILITY as SPOOL,pressure
            if SPOOL not in caps or pressure({'template':'ir_v1','artifact_limits':{'file_mib':1,'total_mib':2},
                'input':{'bytes':1024,'files':[{'name':'run.py'},{'name':'input.mlir'}]}},report,[]):
                raise JobError('A required Node lacks bounded-workflow spool byte/inode headroom',409)
            mapping=p['users'].get(spec['owner']);binding=p.get('owner_bindings',{}).get(spec['owner'])
            if not mapping or not binding or binding['stage_id']!=spec['stage_id'] or binding['state']!='ready':
                raise JobError('Required Node owner binding is incomplete',409)
            if p['physical_id']==self.jobs.backend.physical_id and mapping['uid']==self.jobs.nodes.controller_uid():
                raise JobError('Task identity aliases the controller service',409)
            for key,observed in [('workspace','workspace'),('persistent_cache','persistent_cache')]:
                readings={s['id']:s for s in report.get(observed,{}).get('slots',[])}
                slots=[s for s in p.get(key,{}).get('slots',[]) if s.get('filesystem')=='ext4-userquota' and s.get('quota_uid')==mapping['uid']]
                if not slots or not any(readings.get(s['id'],{}).get('status')=='ok'
                    and (readings[s['id']].get('clean') if key=='workspace' else not readings[s['id']].get('leased'))
                    and readings[s['id']].get('free_bytes',0)>=1024**2 and readings[s['id']].get('free_inodes',0)>=16 for s in slots):
                    raise JobError('Required owner work/cache hard-quota slots are unavailable',409)
            if min(p['policy']['memory_mib'],domain['policy']['memory_mib'])<384 or not set(p['policy']['cpu_ids'])&set(domain['policy']['cpu_ids']):
                raise JobError('Required Node cannot fit a minimum managed task and execution overhead',409)
            if quota['npu']:
                from device_qualification import decision
                from datetime import datetime
                npu=report.get('npu',{})
                try:fresh=0<=time.time()-datetime.fromisoformat(npu['sampled_at']).timestamp()<=15
                except (ValueError,KeyError,TypeError):fresh=False
                devices=[d for d in npu.get('devices',[]) if d.get('health')=='OK' and decision(node,spec['owner'],d['id'])['status']=='verified']
                if not fresh or npu.get('status')!='ok' or len(devices)<quota['npu']:
                    raise JobError('NPU activation requires current per-owner device access qualification on each required Node',409)
            result.append({'node_id':node['id'],'plan_id':target['plan_id'],'ready_ack_id':record['ready_ack']['ack_id'],
                'profile_sha256':sha(p),'environment_sha256':fingerprint(p['environment_manifest'])})
        return {'storage':storage,'nodes':result}

    def activate(self,value):
        spec=request(value);owner=spec['owner']
        ready=read_json(self.runtime/'server-ready.json') or {}
        if identity_alive(ready) and CAPABILITY not in ready.get('capabilities',[]):
            raise JobError('Reload the controller with owner activation support before proceeding',409)
        private_tree(self.runtime,['owner-activations']);private_tree(self.runtime,['personas',owner])
        directory=self.runtime/'owner-activations';pending=directory/(owner+'.pending.json');receipt=directory/(owner+'.json')
        client=self.runtime/'personas'/owner/'client.json'
        with lock(self.runtime,'owner-activation',exclusive=True):
            intent=_read(pending);finished=_read(receipt)
            if finished and finished['request']!=spec:raise JobError('Owner activation already has another resource contract',409)
            if intent and intent['request']!=spec:raise JobError('Resume the original activation request',409)
            with self.jobs.transaction() as db,self.access.connect() as identities:
                identities.execute('BEGIN IMMEDIATE')
                row=identities.execute('SELECT id,record FROM owner_activations WHERE owner=?',(owner,)).fetchone()
                if row:
                    record=json.loads(row[1])
                    if record['request']!=spec:raise JobError('Owner activation already committed another contract',409)
                    # Revocation/disable/renewal after commit is authoritative; never
                    # re-enable or recreate a credential while recovering.
                    if intent:
                        token=intent['client']['token'];digest=hashlib.sha256(token.encode()).hexdigest()
                        valid=identities.execute('SELECT revoked,expires_at FROM tokens WHERE digest=? AND user=?',(digest,owner)).fetchone()
                        user=identities.execute('SELECT disabled,profile FROM users WHERE id=?',(owner,)).fetchone()
                        if (row[0]!=intent['id'] or not user or user[0] or json.loads(user[1]).get('provisioned')!=intent['marker']
                            or not valid or valid[0] or valid[1]<=time.time()):raise JobError('Committed account or credential was changed; activation cannot revive it',409)
                        if _read(client)!=intent['client']:raise JobError('Private delivered profile changed; activation cannot overwrite it',409)
                    elif not finished or record!=finished:
                        raise JobError('Committed activation lost its private recovery record; reconcile without issuing another credential',409)
                    result=record
                else:
                    user=identities.execute('SELECT disabled,password_hash,profile FROM users WHERE id=?',(owner,)).fetchone()
                    if not user:raise JobError('Stage the owner before activation',409)
                    profile=json.loads(user[2])
                    if (user[:2]!=(1,'!') or not owner_identity.staged(owner,profile) or profile['provisioning']['id']!=spec['stage_id']
                        or identities.execute('SELECT 1 FROM tokens WHERE user=?',(owner,)).fetchone()):
                        raise JobError('Activation requires the original disabled, credential-free staged identity',409)
                    readiness=self.readiness(spec,db,profile)
                    if not intent:
                        if _read(client) is not None:raise JobError('Private client destination already exists; no credentials overwritten',409)
                        controller=controller_url(self.access.config)
                        ca=authority_bytes(self.access.config['ca_file']).decode('ascii') if self.access.config.get('ca_file') else None
                        identifier=uuid.uuid4().hex;password=secrets.token_urlsafe(24);salt=secrets.token_bytes(16)
                        marker={'format':FORMAT,'id':identifier,'stage_id':spec['stage_id'],'required_nodes':spec['required_nodes']}
                        intent={'format':FORMAT,'id':identifier,'request':spec,'readiness':readiness,'marker':marker,'at':now(),
                            'expires_at':time.time()+86400,'salt':salt.hex(),'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,300000).hex(),
                            'client':{'username':owner,'password':password,'token':secrets.token_urlsafe(32),'controller':controller,
                                      **({'ca_file':'controller-ca.pem'} if ca else {})},'ca_pem':ca}
                        _save(pending,intent);_checkpoint('intent')
                    if intent['readiness']!=readiness or intent['expires_at']<=time.time():
                        raise JobError('Original activation resource contract changed or credential intent expired; reconcile the original intent',409)
                    if intent['ca_pem']:
                        ca_path=client.parent/'controller-ca.pem'
                        if ca_path.exists() or ca_path.is_symlink():
                            fd=os.open(ca_path,os.O_RDONLY|os.O_NOFOLLOW)
                            with os.fdopen(fd,'rb') as file:
                                info=os.fstat(file.fileno())
                                if info.st_uid!=os.geteuid() or info.st_mode&0o077 or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>256*1024 or file.read()!=intent['ca_pem'].encode():
                                    raise JobError('Controller trust destination differs from activation intent',409)
                        else:
                            fd=os.open(ca_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                            with os.fdopen(fd,'wb') as file:file.write(intent['ca_pem'].encode());file.flush();os.fsync(file.fileno())
                            _sync(client.parent)
                    _checkpoint('trust')
                    existing=_read(client)
                    if existing is not None and existing!=intent['client']:raise JobError('Private profile differs from the original activation intent',409)
                    if existing is None:_save(client,intent['client'])
                    _checkpoint('client')
                    profile.pop('provisioning');profile['provisioned']=intent['marker']
                    result={'format':FORMAT,'id':intent['id'],'owner':owner,'request':spec,'readiness':readiness,
                        'at':intent['at'],'credentials':str(client),'token_lifetime_hours':24}
                    identities.execute('UPDATE users SET disabled=0,password_hash=?,salt=?,profile=? WHERE id=?',
                        (intent['password_hash'],intent['salt'],json.dumps(profile),owner))
                    identities.execute('INSERT INTO tokens(digest,user,scopes,expires_at) VALUES(?,?,?,?)',
                        (hashlib.sha256(intent['client']['token'].encode()).hexdigest(),owner,json.dumps(['jobs:cancel','jobs:read','jobs:submit']),intent['expires_at']))
                    identities.execute('INSERT INTO owner_activations VALUES(?,?,?)',(owner,intent['id'],json.dumps(result)))
                    identities.execute('INSERT INTO audit(at,actor,action,target,outcome) VALUES(?,?,?,?,?)',
                        (now(),self.jobs.owner,'owner.activate',owner,'allowed'))
                    _checkpoint('before_commit')
                    identities.commit();_checkpoint('committed')
            if finished is not None and result!=finished:raise JobError('Final activation receipt differs from the committed record',409)
            if finished is None:_save(receipt,result)
            _checkpoint('receipt')
            if pending.exists():pending.unlink();_sync(directory)
            _checkpoint('published')
            return {**result,'reused':bool(row),'credentials_private':True}

"""Root-only durable central commit/ready handshake for a bound Node owner."""
import copy
import hashlib
import os
from pathlib import Path
import re
import stat

from deployment import DeployError,require_root,verify
from node_owner_admin import JOURNAL,AWAIT,HISTORY,projection,sha,read,control
from owner_handoff import ready_projection,validate_ack
import payload_quota_admin as files
from state_lock import state_lock

PROGRESS='node-owner-ack-progress.json'
ACK_HISTORY='node-owner-ack-history'


def _checkpoint(stage):
    """Crash acceptance boundary; no production environment switches."""


def private(path):
    if Path(path).lstat().st_uid!=0:raise DeployError('Node acknowledgement metadata must be Root-owned')
    return read(path)


def complete(prefix,plan_id,client=None):
    require_root()
    if not isinstance(plan_id,str) or not re.fullmatch('[0-9a-f]{64}',plan_id):raise DeployError('Choose one exact owner plan SHA')
    prefix,meta,release=verify(prefix,quota_recovery=True);helper=release/Path(__file__).name
    if (meta['role']!='node' or not helper.is_file()
        or hashlib.sha256(helper.read_bytes()).digest()!=hashlib.sha256(Path(__file__).read_bytes()).digest()):
        raise DeployError('Activate matching Node owner ACK code before this handoff')
    state=Path(meta['state_dir']);control(state)
    with state_lock(state,exclusive=True):
        if (state/JOURNAL).exists() or (state/JOURNAL).is_symlink():raise DeployError('Finish the original local Node binding before central ACK')
        from maintenance import assert_quiescent
        assert_quiescent(state,'node')
        config=private(state/'config.json');prepared=private(state/HISTORY/(plan_id+'.json'))
        if prepared.get('plan_id')!=plan_id or prepared.get('node_id')!=config.get('node_id') or prepared.get('physical_id')!=config.get('physical_id'):
            raise DeployError('Prepared owner receipt does not belong to this Node')
        from backends import physical_identity
        if physical_identity()!=config['physical_id']:raise DeployError('Acknowledgement requires the bound physical Node')
        owner=prepared['owner'];target=ready_projection(prepared['after_projection'],owner)
        if projection(config) not in [prepared['after_projection'],target]:raise DeployError('Node configuration differs from prepared/ready owner projections')
        expected_wait={'format':'ascend-lab-node-owner-await/v1','plan_id':plan_id,'node_id':config['node_id'],
                       'owner':owner,'stage_id':prepared['stage_id']}
        awaiting=state/AWAIT;progress=state/PROGRESS;history=state/ACK_HISTORY;receipt_path=history/(plan_id+'.json')
        expected={'plan_id':plan_id,'node_id':config['node_id'],'physical_id':config['physical_id'],'owner':owner,
                  'stage_id':prepared['stage_id'],'policy_revision':target['policy_revision'],'projection_sha256':sha(target)}
        if awaiting.exists() or awaiting.is_symlink():
            if private(awaiting)!=expected_wait:raise DeployError('Another owner awaits central acknowledgement')
        if history.exists():
            info=history.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:raise DeployError('ACK history must be Root-private')
        if not progress.exists() and not awaiting.exists() and receipt_path.exists():
            result=private(receipt_path)
            validate_ack(result['commit_ack'],expected,'committed')
            validate_ack(result['ready_ack'],expected,'ready',result['commit_ack']['ack_id'])
            if projection(config)!=target:raise DeployError('Completed ACK configuration no longer matches')
            return {'acknowledged':True,'already_acknowledged':True,'receipt':result,'startup_blocked':False,'accounts_enabled':False}
        if client is None:
            from worker import NodeClient
            client=NodeClient(config)
        intent={'format':'ascend-lab-owner-ack-progress/v1','plan_id':plan_id,'prepared_receipt_sha256':sha(prepared)}
        if progress.exists() or progress.is_symlink():
            saved=private(progress)
            if any(saved.get(k)!=v for k,v in intent.items()):raise DeployError('Resume only the original Node owner ACK')
            if set(saved)-set(intent)-{'commit_ack','ready_ack'}:raise DeployError('Unknown ACK progress fields')
        else:
            if not awaiting.exists() or projection(config)!=prepared['after_projection']:raise DeployError('Missing original Node acknowledgement handoff')
            saved=intent
            if not history.exists():history.mkdir(mode=0o700);files.sync(state)
            if len(os.listdir(history))>=1024:raise DeployError('Archive completed ACK receipts before further handoffs')
            files.write(progress,saved);_checkpoint('intent')
        response=client.call('owner-commit',{'receipt':prepared});commit=response.get('ack')
        validate_ack(commit,expected,'committed');_checkpoint('commit_response')
        if saved.get('commit_ack') and saved['commit_ack']!=commit:raise DeployError('Central commit ACK changed during replay')
        saved={**saved,'commit_ack':commit};files.write(progress,saved);_checkpoint('committed')
        updated={**config,**copy.deepcopy(target)};files.write(state/'config.json',updated);_checkpoint('config_ready')
        response=client.call('owner-ready',{'plan_id':plan_id,'commit_ack_id':commit['ack_id']});ready=response.get('ack')
        validate_ack(ready,{**expected,'approval_id':commit['approval_id']},'ready',commit['ack_id']);_checkpoint('ready_response')
        if saved.get('ready_ack') and saved['ready_ack']!=ready:raise DeployError('Central ready ACK changed during replay')
        saved={**saved,'ready_ack':ready};files.write(progress,saved);_checkpoint('ready_saved')
        result={'format':'ascend-lab-node-owner-ack-receipt/v1','plan_id':plan_id,'commit_ack':commit,'ready_ack':ready,
                'prepared_receipt_sha256':sha(prepared),'ready_projection_sha256':sha(target)}
        if receipt_path.exists() and private(receipt_path)!=result:raise DeployError('Completed ACK receipt differs; preserve progress')
        files.write(receipt_path,result);_checkpoint('receipt')
        if awaiting.exists():
            if private(awaiting)!=expected_wait:raise DeployError('Handoff changed before ACK publication')
            awaiting.unlink();files.sync(state)
        _checkpoint('awaiting_removed');progress.unlink();files.sync(state);_checkpoint('published')
        return {'acknowledged':True,'already_acknowledged':False,'receipt':result,'startup_blocked':False,'accounts_enabled':False}

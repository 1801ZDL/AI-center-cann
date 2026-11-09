"""One private Root maintenance approval backup; recovery always stops execution.

No state database, service action, scheduler or timer is touched. Original
cross-host approvals are evidence, never new permission on the destination.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

import maintenance_runner as runner
import maintenance_lifecycle as lifecycle

SCHEMA='ascend-lab-maintenance-plan-backup/v1'
MAX_BUNDLE_BYTES=1024**2
MAX_RECOVERED=16
CAPS={'journal':runner.MAX_PLAN_BYTES,'anchor':512,'revocation':2048}
NAMES={'journal':'source.json','anchor':'source.json.lock','revocation':'source.json.revoked.json'}


def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return runner.encoded(value)


def private_directory(path,*,create=False):
    if create:path.mkdir(mode=0o700,exist_ok=True)
    item=path.lstat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid!=os.geteuid() or item.st_mode&0o077:raise ValueError('Maintenance backup directory must be private and owned')


def read_private(path,maximum,*,missing=False):
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:
        if missing:return None
        raise
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_mode&0o077 or before.st_nlink!=1 or before.st_size>maximum:
            raise ValueError('Unsafe or oversized maintenance backup object')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
        if len(raw)>maximum or any(getattr(before,k)!=getattr(after,k) for k in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):raise ValueError('Maintenance backup object changed during read')
        return raw


def write_once(path,raw):
    old=read_private(path,max(MAX_BUNDLE_BYTES,len(raw)),missing=True)
    if old is not None:
        if old!=raw:raise ValueError('Original private maintenance backup differs; retained')
        return False
    # Use the existing atomic, exclusive publication path, preserving its
    # fsync and no-overwrite semantics. Bundle bytes may exceed a journal cap.
    directory=os.open(path.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary=path.name+'.tmp-'+os.urandom(12).hex()
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path.name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False);os.fsync(directory)
    finally:
        try:os.unlink(temporary,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)
    if read_private(path,max(MAX_BUNDLE_BYTES,len(raw)))!=raw:raise ValueError('Maintenance backup did not verify')
    return True


def parts(journal):
    paths={'journal':journal,'anchor':journal.with_name(journal.name+'.lock'),'revocation':lifecycle.marker(journal)}
    result={}
    for key,path in paths.items():
        raw=read_private(path,CAPS[key],missing=True)
        result[key]=None if raw is None else {'bytes':len(raw),'sha256':sha(raw),'text':raw.decode('utf-8')}
    return result


def checked(value):
    if not isinstance(value,dict) or set(value)!={'schema','id','plan','source_prefix','captured_epoch','parts'} or value['schema']!=SCHEMA or len(encoded(value))>MAX_BUNDLE_BYTES:
        raise ValueError('Invalid bounded maintenance backup')
    if not isinstance(value['id'],str) or not re.fullmatch('[a-f0-9]{64}',value['id']) or value['id']!=sha(encoded({k:v for k,v in value.items() if k!='id'})):
        raise ValueError('Maintenance backup digest differs')
    runner.checked(value['plan']);at=value['captured_epoch']
    if not isinstance(value['source_prefix'],str) or not Path(value['source_prefix']).is_absolute() or '..' in Path(value['source_prefix']).parts:raise ValueError('Invalid source maintenance prefix')
    if type(at) not in {int,float} or not 0<=at<=253402300799 or not math.isfinite(at):raise ValueError('Invalid maintenance backup time')
    if not isinstance(value['parts'],dict) or set(value['parts'])!=set(CAPS):raise ValueError('Incomplete maintenance backup parts')
    for key,part in value['parts'].items():
        if part is None:continue
        if not isinstance(part,dict) or set(part)!={'bytes','sha256','text'} or not isinstance(part['text'],str):raise ValueError('Invalid maintenance backup part')
        raw=part['text'].encode()
        if type(part['bytes']) is not int or not 0<=part['bytes']<=CAPS[key] or len(raw)!=part['bytes'] or sha(raw)!=part['sha256']:raise ValueError('Maintenance backup part size or checksum differs')
    return value


def stage_parts(directory,bundle):
    for key,name in NAMES.items():
        part=bundle['parts'][key]
        if part is not None:write_once(directory/name,part['text'].encode())
    # This path is deliberately outside maintenance-runs: no recovery witness
    # lookup is needed while checking the exact original source records.
    return runner.status(bundle['plan'],directory/'source.json')


def capture(prefix,value,*,epoch=None):
    value=runner.checked(value)
    journal=Path(prefix)/'maintenance-runs'/(value['id']+'.json')
    runner.status(value,journal)
    bundle={'schema':SCHEMA,'plan':value,'source_prefix':str(prefix),'captured_epoch':time.time() if epoch is None else epoch,'parts':parts(journal)}
    bundle['id']=sha(encoded(bundle));return checked(bundle)


def summary(bundle):
    raw=bundle['parts']['journal'];state=json.loads(raw['text']) if raw is not None else None
    return {'backup_id':bundle['id'],'plan_id':bundle['plan']['id'],'snapshot_invocations':state.get('calls') if state else None,
            'snapshot_next_step':state.get('index') if state else None,'snapshot_pending':state.get('pending') is not None if state else False,
            'bytes':len(encoded(bundle)),'source_prefix':bundle['source_prefix'],'approval_prefix':bundle['plan']['prefix']}


def export_in_context(prefix,value,output,*,apply=False,epoch=None,state_dir=None):
    output=Path(output)
    if not output.is_absolute() or '..' in output.parts or output.is_relative_to(Path(prefix)) or state_dir and output.is_relative_to(Path(state_dir)):
        raise ValueError('Choose a private backup location outside installation and service state')
    private_directory(output.parent)
    old=read_private(output,MAX_BUNDLE_BYTES,missing=True)
    with lifecycle.coordination(prefix):
        bundle=capture(prefix,value,epoch=epoch)
        if old is not None:
            existing=checked(json.loads(old))
            if existing['plan']!=value or existing['source_prefix']!=str(prefix):raise ValueError('Backup path already belongs to another original approval or source')
            return {'status':'existing_backup','writes':False,'represents_current':existing['parts']==bundle['parts'],**summary(existing)}
        if not apply:return {'status':'preview','writes':False,'coordination_files_may_be_created':True,**summary(bundle)}
        write_once(output,encoded(bundle))
        return {'status':'backed_up','writes':True,'sha256':sha(encoded(bundle)),**summary(bundle)}


def stored_bundles(directory):
    if not directory.exists():return
    private_directory(directory)
    with os.scandir(directory) as entries:
        for index,entry in enumerate(entries):
            if index>=MAX_RECOVERED:raise ValueError('Recovered maintenance store is full; preserve all records')
            if not re.fullmatch('[a-f0-9]{64}',entry.name):raise ValueError('Unknown recovered maintenance object')
            folder=Path(entry.path);private_directory(folder)
            raw=read_private(folder/'bundle.json',MAX_BUNDLE_BYTES)
            bundle=checked(json.loads(raw))
            if bundle['id']!=entry.name or raw!=encoded(bundle):raise ValueError('Recovered maintenance evidence identity differs')
            yield folder,bundle


def recovered_fence(directory,plan_id):
    # A retained sealed backup is an independent witness. Losing the active
    # journal/anchor/marker together must not make that original ID runnable.
    return any(bundle['plan']['id']==plan_id for _,bundle in stored_bundles(directory))


def _checkpoint(stage):pass


def recover_in_context(prefix,bundle,*,apply=False,epoch=None):
    prefix=Path(prefix);bundle=checked(bundle);value=bundle['plan']
    if not apply:return {'status':'preview','writes':False,'execution_restored':False,**summary(bundle)}
    at=time.time() if epoch is None else epoch
    with lifecycle.coordination(prefix):
        directory=prefix/'maintenance-recovered';known=[b['id'] for _,b in stored_bundles(directory)]
        if bundle['id'] not in known and len(known)>=MAX_RECOVERED:raise ValueError('Recovered maintenance store is full; no event was replaced')
        with tempfile.TemporaryDirectory(prefix='.verify-maintenance-',dir=prefix) as temporary:
            stage=Path(temporary);stage_parts(stage,bundle);write_once(stage/'bundle.json',encoded(bundle))
            private_directory(directory,create=True);destination=directory/bundle['id']
            if not destination.exists() and not destination.is_symlink():stage.rename(destination)
            else:
                private_directory(destination)
                if read_private(destination/'bundle.json',MAX_BUNDLE_BYTES)!=encoded(bundle):raise ValueError('Recovered bundle changed; retained')
                for key,name in NAMES.items():
                    expected=bundle['parts'][key]
                    actual=read_private(destination/name,CAPS[key],missing=True)
                    if actual!=(expected['text'].encode() if expected is not None else None):raise ValueError('Original recovered part differs')
        _checkpoint('evidence_published')
        journal=prefix/'maintenance-runs'/(value['id']+'.json')
        original=read_private(destination/'receipt.json',4096,missing=True)
        if original is not None:
            receipt=json.loads(original)
            if (not isinstance(receipt,dict) or set(receipt)!={'schema','backup_id','plan_id','target_prefix','revocation_sha256'}
                or receipt['schema']!='ascend-lab-maintenance-recovery/v1' or receipt['backup_id']!=bundle['id']
                or receipt['plan_id']!=value['id'] or receipt['target_prefix']!=str(prefix)):
                raise ValueError('Original recovery receipt binding differs')
            current_status=runner.status(value,journal)
            record=runner.load_journal(lifecycle.marker(journal),maximum=2048)
            if current_status['status']!='revoked' or sha(encoded(record))!=receipt['revocation_sha256']:
                raise ValueError('Latest recovered stop facts are unavailable; do not replace them with an older backup')
            return {'status':'already_recovered','writes':False,'execution_restored':False,'original_approval_revoked':True,
                    'current_invocations':current_status.get('invocations'),'pending_retained':current_status['pending'],**summary(bundle)}
        current=parts(journal);saved=json.loads(current['journal']['text']) if current['journal'] is not None else None
        source=json.loads(bundle['parts']['journal']['text']) if bundle['parts']['journal'] is not None else None
        record=json.loads(current['revocation']['text']) if current['revocation'] is not None else None
        source_marker=json.loads(bundle['parts']['revocation']['text']) if bundle['parts']['revocation'] is not None else None
        raw=current['anchor']['text'].encode() if current['anchor'] is not None else b''
        preserved=saved is not None
        if saved is not None:
            runner.checked_state(value,saved)
            # Validate current metadata via a mirror, avoiding the newly
            # published independent recovery fence in the live namespace.
            with tempfile.TemporaryDirectory(prefix='.verify-current-maintenance-',dir=prefix) as temporary:
                mirror={'parts':current,'plan':value}
                stage_parts(Path(temporary),mirror)
        if record is None:
            if saved is None and raw:raise ValueError('Current journal is missing behind its original anchor; no backup overwrite')
            if saved is None:
                record=source_marker or lifecycle.revocation_record(value,source,
                    bundle['parts']['anchor']['text'].encode() if bundle['parts']['anchor'] is not None else b'',
                    'maintenance_incident',at)
            else:record=lifecycle.revocation_record(value,saved,raw,'maintenance_incident',at)
            lifecycle.checked_revocation(value,record);runner.save_journal(lifecycle.marker(journal),record,exclusive=True)
        else:lifecycle.checked_revocation(value,record)
        _checkpoint('stop_published')
        if saved is None and source is not None:
            matches=(record==source_marker if source_marker is not None else record['journal_before_sha256']==sha(encoded(source)))
            if not matches:raise ValueError('Missing target journal is not the exact source committed by its stop; no overwrite')
            runner.save_journal(journal,source,exclusive=True)
        _checkpoint('journal_retained')
        result=lifecycle._revoke_serialized(value,journal,apply=True,reason='maintenance_incident',epoch=at)
        if result['status']!='revoked':raise ValueError('Recovered original approval is not confirmed stopped')
        receipt={'schema':'ascend-lab-maintenance-recovery/v1','backup_id':bundle['id'],'plan_id':value['id'],
                 'target_prefix':str(prefix),'revocation_sha256':sha(encoded(runner.load_journal(lifecycle.marker(journal),maximum=2048)))}
        original=read_private(destination/'receipt.json',4096,missing=True)
        if original is not None and original!=encoded(receipt):raise ValueError('Original recovery receipt differs; all evidence retained')
        write_once(destination/'receipt.json',encoded(receipt));_checkpoint('receipt_published')
        return {'status':'already_recovered' if original is not None else 'recovered','writes':original is None,
                'execution_restored':False,'original_approval_revoked':True,'current_journal_preserved':preserved,
                'current_invocations':result.get('invocations'),'pending_retained':result['pending'],**summary(bundle)}


@contextmanager
def context(prefix):
    from deployment import require_root,verify
    require_root();prefix,meta,release=verify(prefix)
    if meta['role']!='controller':raise ValueError('Maintenance plan backup requires an installed controller')
    for name in ['maintenance_plan_backup.py','maintenance_runner.py','maintenance_lifecycle.py','opsctl']:
        if (release/name).read_bytes()!=Path(__file__).with_name(name).read_bytes():raise ValueError('Use matching installed maintenance backup code')
    yield prefix,meta


def export(value,output,*,apply=False,prefix=None):
    value=runner.checked(value)
    with context(value['prefix'] if prefix is None else prefix) as (prefix,meta):
        from deployment import secure_parent
        private_directory(Path(output).parent)
        return export_in_context(prefix,value,secure_parent(output),apply=apply,state_dir=meta['state_dir'])


def recover(prefix,archive,*,apply=False):
    with context(prefix) as (prefix,_):
        from deployment import secure_parent
        private_directory(Path(archive).parent)
        bundle=checked(json.loads(read_private(secure_parent(archive),MAX_BUNDLE_BYTES)))
        return recover_in_context(prefix,bundle,apply=apply)

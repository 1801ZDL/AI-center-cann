"""Fixed installed-controller storage fault injector; never a production tool.

The Root fixture selects one new private archive, then drops to the exact
unprivileged service UID and storage groups before creating/removing fillers.
No arbitrary user paths, device operations, subprocesses or network access.
"""
import errno
import json
import os
from pathlib import Path
import sys

release,policy_json,uid_text,gid_text,archive,operation=sys.argv[1:]
sys.path.insert(0,release)
import group_quota as quota
from payload_groups import gids
from payload_storage import validate

policy=validate(json.loads(policy_json));uid=int(uid_text);gid=int(gid_text)
assert os.geteuid()==0 and uid>0 and gid>0
assert archive.startswith('job-') and len(archive)==16 and all(c in '0123456789abcdef' for c in archive[4:])
parent=Path(policy['path'])/'jobs'/archive;info=parent.lstat()
assert info.st_uid==uid and info.st_gid==policy['owner_groups']['proof_user']['gid'] and info.st_mode&0o7777==0o2700
os.setgroups(gids(policy));os.setgid(gid);os.setuid(uid);os.umask(0o077)
status={k:v.strip() for k,sep,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
assert status['CapEff']==status['CapPrm']=='0000000000000000' and status['NoNewPrivs']=='1'
folder=parent/'quota-pressure'
if operation=='clean':
    inventory=json.loads(sys.stdin.read());assert inventory['operation'] in {'bytes','inodes'}
    assert [folder.stat().st_dev,folder.stat().st_ino]==inventory['directory_identity']
    assert sorted(p.name for p in folder.iterdir())==sorted(item['name'] for item in inventory['files'])
    for item in inventory['files']:
        target=folder/item['name'];info=target.lstat()
        assert item['name'].startswith('fill-') and '/' not in item['name'] and info.st_nlink==1
        assert [info.st_dev,info.st_ino]==item['identity'];target.unlink()
    folder.rmdir();print(json.dumps({'cleaned':True,'uid':uid,'cap_effective':status['CapEff']}))
else:
    assert operation in {'bytes','inodes'};folder.mkdir(mode=0o700);files=[];code=None;written=0
    if operation=='bytes':
        target=folder/'fill-bytes';descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            while written<256*1024**2:
                try:written+=os.write(descriptor,b'q'*(256*1024))
                except OSError as error:code=error.errno;break
        finally:os.close(descriptor)
        item=target.stat();files.append({'name':target.name,'identity':[item.st_dev,item.st_ino]})
    else:
        for index in range(4096):
            target=folder/('fill-'+str(index))
            try:target.touch(mode=0o600)
            except OSError as error:code=error.errno;break
            item=target.stat();files.append({'name':target.name,'identity':[item.st_dev,item.st_ino]})
    assert code==errno.EDQUOT
    descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
    try:actual=quota.read(descriptor,policy['owner_groups']['proof_user']['gid']);fs=os.fstatvfs(descriptor)
    finally:os.close(descriptor)
    item=folder.stat()
    print(json.dumps({'operation':operation,'uid':uid,'cap_effective':status['CapEff'],'errno':code,
        'written_bytes':written,'files_created':len(files),'directory_identity':[item.st_dev,item.st_ino],
        'files':files,'quota':actual,'filesystem_free_bytes':fs.f_bavail*fs.f_frsize,'filesystem_free_inodes':fs.f_favail}))

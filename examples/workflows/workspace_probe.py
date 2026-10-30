"""Actual non-root ENOSPC checks on a registered bounded work filesystem; no NPU."""
import errno
import argparse
import json
import os
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('--quota-bytes',type=int);parser.add_argument('--quota-inodes',type=int);args=parser.parse_args()
root=Path(os.environ['LAB_JOB_DIR']);before=os.statvfs(root)
user_quota=args.quota_bytes is not None
assert os.getuid()>=1000
if user_quota:assert 1<=args.quota_bytes<=64*1024**2 and 1<=args.quota_inodes<=10000 and before.f_blocks*before.f_frsize>args.quota_bytes
else:assert before.f_blocks*before.f_frsize<=64*1024**2
expected_errno=errno.EDQUOT if user_quota else errno.ENOSPC
cache=root/'cache';filler=cache/'byte-limit.bin';written=0;byte_errno=None
try:
    with filler.open('wb',buffering=0) as file:
        block=b'x'*(1024*1024)
        for _ in range(65):written+=file.write(block)
except OSError as error:byte_errno=error.errno
finally:filler.unlink(missing_ok=True)
assert byte_errno==expected_errno,(byte_errno,written)
inode_dir=cache/'inode-limit';inode_dir.mkdir();created=0;inode_errno=None
try:
    for i in range(10000):
        with (inode_dir/str(i)).open('xb'):pass
        created+=1
except OSError as error:inode_errno=error.errno
finally:
    for path in inode_dir.iterdir():path.unlink()
    inode_dir.rmdir()
assert inode_errno==expected_errno,(inode_errno,created)
after=os.statvfs(root)
report={'passed':True,'uid':os.getuid(),'byte_errno':byte_errno,'inode_errno':inode_errno,
        'bytes_written_before_enospc':written,'files_created_before_enospc':created,
        'filesystem_byte_limit':before.f_blocks*before.f_frsize,'filesystem_inode_limit':before.f_files,
        'free_bytes_after_cleanup':after.f_bavail*after.f_frsize,'free_inodes_after_cleanup':after.f_favail,
        'quota_byte_hard':args.quota_bytes,'quota_inode_hard':args.quota_inodes,
        'scope':'Actual registered UID-quota workspace/cache; statvfs totals describe the backing filesystem, not the UID limit' if user_quota else 'Actual registered finite filesystem workspace/cache; mount type and durability are recorded separately in protected environment evidence'}
(root/'ir/workspace-probe.json').write_text(json.dumps(report))
Path(os.environ['LAB_RESULT_FILE']).write_text(json.dumps({'schema':'ascend-lab-result/v1','kind':'ir',
        'passed':True,'artifacts':['ir/workspace-probe.json']}))
print('Actual workspace/cache byte and inode limit checks passed.',flush=True)

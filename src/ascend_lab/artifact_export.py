"""Trusted read-only helper; exports regular outputs after the task namespace has ended."""
import os
import errno
import json
import hashlib
import io
from pathlib import Path
import re
import stat
import sys
import tarfile

class Rejected(RuntimeError):pass


def main():
    root=Path('/work');total=count=0;mappings=[]
    budget=json.loads(Path('/payload/run.json').read_text()).get('artifact_limits',{'file_mib':64,'total_mib':128})
    if (not isinstance(budget,dict) or set(budget)!={'file_mib','total_mib'}
            or any(type(v) is not int or not 1<=v<=8192 for v in budget.values())
            or budget['file_mib']>budget['total_mib']):raise Rejected('output-budget-exceeded')
    max_file=budget['file_mib']*1024**2;max_total=budget['total_mib']*1024**2
    for category in ['results','ir','profile']:
        if (root/category/'_lab').exists() or (root/category/'_lab').is_symlink():raise Rejected('reserved-output-name')
    with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
        receipt=Path('/receipt/environment.json')
        if receipt.exists():
            fd=os.open(receipt,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>256*1024:raise Rejected('unsafe-protected-receipt')
                total+=info.st_size;count+=1
                if total>max_total or count>2000:raise Rejected('output-budget-exceeded')
                item=tarfile.TarInfo('results/_lab/environment.json');item.size=info.st_size;item.mode=0o600
                archive.addfile(item,stream)
        for category in ['results','ir','profile']:
            base=root/category
            if not base.exists() and not base.is_symlink():continue
            if base.is_symlink() or not base.is_dir():raise Rejected('unsafe-output-link')
            for parent,dirs,files in os.walk(base,followlinks=False):
                for name in dirs:
                    if (Path(parent)/name).is_symlink():raise Rejected('unsafe-output-link')
                for name in files:
                    path=Path(parent)/name
                    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    with os.fdopen(fd,'rb') as stream:
                        info=os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise Rejected('unsafe-output-link-or-type')
                        count+=1;total+=info.st_size
                        if count>1990 or total>max_total or info.st_size>max_file:raise Rejected('output-budget-exceeded')
                        original=str(path.relative_to(root));name=original
                        if len(original.encode())>200 or '\\' in original or any(ord(c)<32 for c in original):
                            suffix=path.suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,16}',path.suffix) else ''
                            name=category+'/_lab/files/'+hashlib.sha256(original.encode()).hexdigest()+suffix
                            mappings.append({'original':original,'archived':name,'bytes':info.st_size})
                        item=tarfile.TarInfo(name);item.size=info.st_size;item.mode=0o600
                        archive.addfile(item,stream)
        if mappings:
            data=json.dumps({'schema':'ascend-lab-path-map/v1','mappings':mappings},ensure_ascii=False).encode()
            total+=len(data);count+=1
            if total>max_total:raise Rejected('output-budget-exceeded')
            item=tarfile.TarInfo('results/_lab/path-map.json');item.size=len(data);item.mode=0o600
            archive.addfile(item,io.BytesIO(data))



if __name__=='__main__':
    try:main()
    except (Rejected,OSError) as error:
        code=str(error) if isinstance(error,Rejected) else 'unsafe-output-link' if error.errno==errno.ELOOP else 'output-read-error'
        print(json.dumps({'error':code}),file=sys.stderr,flush=True)
        raise SystemExit(65)

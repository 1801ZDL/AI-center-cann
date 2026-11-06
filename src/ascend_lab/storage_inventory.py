"""Versioned durable inventory; Linux mount IDs remain operation-local checks."""
import hashlib
import json
from pathlib import PurePosixPath
import stat

FORMAT='ascend-lab-storage-inventory/v2'


def fail():
    from jobs import JobError
    raise JobError('Invalid retained storage inventory; preserve it for reconciliation',409)


def version(value):
    if not isinstance(value,dict):fail()
    if set(value)=={'root','entries','bytes'}:return 1
    if set(value)=={'format','root','entries','bytes'} and value['format']==FORMAT:return 2
    fail()


def persistent(value):
    if version(value)==1:return value # Legacy fingerprints remain namespace-bound.
    root=value['root'];entries=value['entries'];total=value['bytes']
    def integer(x,minimum=0):return type(x) is int and minimum<=x<2**63
    if not isinstance(root,dict) or set(root)!={'dev','ino','mount'}:fail()
    if not integer(root['dev']) or not integer(root['ino'],1) or not integer(root['mount'],1):fail()
    if not isinstance(entries,list) or len(entries)>10000 or not integer(total):fail()
    names=set();size=0;portable=[]
    for item in entries:
        if not isinstance(item,dict) or set(item)!={'name','dev','ino','mount','mode','size','mtime','ctime'}:fail()
        name=item['name']
        if (not isinstance(name,str) or not name or len(name.encode())>4096 or '\\' in name
            or any(ord(c)<32 or ord(c)==127 for c in name) or name!=str(PurePosixPath(name))
            or PurePosixPath(name).is_absolute() or any(p in {'.','..'} or len(p.encode())>255 for p in name.split('/'))
            or name in names):fail()
        names.add(name)
        if (not integer(item['dev']) or not integer(item['mount'],1)
            or item['dev']!=root['dev'] or item['mount']!=root['mount'] or not integer(item['ino'],1)
            or not integer(item['mode']) or item['mode']>0o177777 or not integer(item['size'])
            or any(type(item[k]) is not int or not -2**63<=item[k]<2**63 for k in ['mtime','ctime'])):fail()
        if not stat.S_ISDIR(item['mode']):size+=item['size']
        portable.append({k:v for k,v in item.items() if k!='mount'})
    if size!=total:fail()
    return {'format':FORMAT,'root':{k:v for k,v in root.items() if k!='mount'},'entries':portable,'bytes':total}


def digest(value):
    return hashlib.sha256(json.dumps(persistent(value),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def require_compatible(state,role,manifest):
    if 'storage_inventory.py' in manifest.get('files',{}):return
    from pathlib import Path
    from deployment import DeployError,regular_json
    import sqlite3
    state=Path(state)
    def retained(value):
        return isinstance(value,dict) and value.get('format')==FORMAT
    if role=='controller' and (state/'jobs.sqlite3').exists():
        with sqlite3.connect((state/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='storage_lifecycle'").fetchone():
                if any(retained(json.loads(raw).get('inventory')) for raw, in db.execute('SELECT record FROM storage_lifecycle')):
                    raise DeployError('Retained portable storage inventories require compatible code; refusing downgrade')
    elif role=='node':
        for path in state.glob('attempts/*/control/state.json'):
            record=regular_json(path,max_bytes=32*1024**2,private=True)
            if retained((record.get('artifact_gc') or {}).get('inventory')):
                raise DeployError('Pending portable Node cleanup requires compatible code; refusing downgrade')

"""Repository paths and legacy flat release paths, without runtime migration."""
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit, urlunsplit

COMMANDS = ('labctl','agentctl','accessctl','workerctl','servicectl','install-client','opsctl','debugctl')
TEXT_SUFFIXES = {'.py','.md','.sh','.html','.css','.js','.svg','.mlir','.c','.cpp','.json','.txt'}
PUBLIC_REPORTS = ('v036-public.json','public-source-check.json','mixed-compiler-stage.json','repository-layout-check.json','retention-policy-check.json','maintenance-execution-check.json','result-history-check.json','grant-history-check.json','graph-history-check.json')


def project_root(root):
    root = Path(root).resolve()
    candidates=[root]
    if root.name=='ascend_lab' and root.parent.name=='src':candidates.append(root.parents[1])
    for candidate in candidates:
        if (candidate/'repository-layout.json').is_file() and (candidate/'src/ascend_lab').is_dir():
            return candidate
    return root


def layout(root):
    project = project_root(root)
    if (project/'src/ascend_lab').is_dir() and (project/'repository-layout.json').is_file():
        return project, json.loads((project/'repository-layout.json').read_text())
    return project, None


def source_file(root, name):
    relative=PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or str(relative)!=name:
        raise ValueError('Invalid logical source path')
    root=Path(root)
    if (root/name).is_file():return root/name
    project, mapping=layout(root)
    if mapping is None:return project/name
    if name in mapping['documents']:return project/mapping['documents'][name]
    if len(relative.parts)==1 and (name.endswith('.py') or name in COMMANDS):
        return project/mapping['source_directory']/name
    if name=='check-browser.sh':return project/'scripts/check-browser.sh'
    return project/name


def state_root(root):
    return project_root(root)/'runtime'


def release_bytes(root, name):
    """The installed artifact keeps its existing flat contract and doc links."""
    path=source_file(root,name);raw=path.read_bytes()
    project,mapping=layout(root)
    if mapping is None or not name.endswith('.md'):return raw
    reverse={str((project/location).resolve()):logical for logical,location in mapping['documents'].items()}
    reverse.update({str((project/'bin'/entry).resolve()):entry for entry in COMMANDS})
    source=project/mapping['source_directory']
    reverse.update({str(path.resolve()):path.name for path in source.glob('*.py')})
    reverse.update({str((source/entry).resolve()):entry for entry in COMMANDS})
    def link(match):
        target=match.group(1);url=urlsplit(target)
        if url.scheme or url.netloc or not url.path or url.path.startswith('/'):
            return match.group(0)
        selected=(path.parent/url.path).resolve()
        replacement=reverse.get(str(selected))
        if replacement is None:
            try:replacement=str(selected.relative_to(project))
            except ValueError:return match.group(0)
        return ']('+urlunsplit(('', '', replacement, url.query, url.fragment))+')'
    text=re.sub(r'\]\(([^\s)]+)\)',link,raw.decode())
    for entry in COMMANDS:text=text.replace('./bin/'+entry,'./'+entry)
    return text.encode()


def public_paths(root):
    project,mapping=layout(root)
    if mapping is None:
        paths={p for pattern in ['*.py','*.md'] for p in project.glob(pattern) if p.name!='AGENTS.md'}
        paths.update(project/name for name in COMMANDS)
        paths.update(project/name for name in ['.gitignore','check-browser.sh','requirements-qa.txt','evidence/README.md'])
    else:
        paths={project/'README.md',project/'CONTRIBUTING.md',project/'.gitignore',project/'repository-layout.json',
               project/'requirements-qa.txt',project/'evidence/README.md'}
        paths.update((project/mapping['source_directory']).glob('*.py'))
        paths.update(project/mapping['source_directory']/name for name in COMMANDS)
        paths.update(project/'bin'/name for name in COMMANDS)
        paths.update((project/'docs').rglob('*.md'))
    for folder in ['examples','tests','web','scripts']:
        paths.update(p for p in (project/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and (p.name=='Dockerfile' or p.suffix in TEXT_SUFFIXES))
    paths.update(project/'reports'/name for name in PUBLIC_REPORTS
                 if (project/'reports'/name).is_file())
    return sorted(paths)


def snapshot(root, destination, *, maximum_bytes=16*1024**2, maximum_files=700):
    """Finite development-only flat view for unchanged legacy test contracts.

    Explicit public sources only. Never copy runtime, credentials or binaries.
    Production packaging is separate; this function starts no process/service.
    """
    root=project_root(root);destination=Path(destination)
    if not destination.is_dir() or any(destination.iterdir()):
        raise ValueError('Source snapshot needs an empty private destination')
    mapping=layout(root)[1]
    files={};reverse={value:key for key,value in (mapping or {}).get('documents',{}).items()}
    for source in public_paths(root):
        relative=str(source.relative_to(root))
        if relative=='repository-layout.json' or relative.startswith('bin/'):
            continue
        if mapping and relative.startswith(mapping['source_directory']+'/'):
            logical=source.name
        else:logical=reverse.get(relative,relative)
        if logical=='scripts/check-browser.sh':logical='check-browser.sh'
        if logical in files:raise ValueError('Duplicate logical source in snapshot')
        files[logical]=source
    total=0
    if len(files)>maximum_files:raise ValueError('Source snapshot file budget exceeded')
    for logical,source in sorted(files.items()):
        if source.is_symlink() or not source.is_file() or source.stat().st_size>4*1024**2:
            raise ValueError('Unsafe public source')
        raw=release_bytes(root,logical) if logical.endswith('.md') and len(PurePosixPath(logical).parts)==1 else source.read_bytes()
        total+=len(raw)
        if total>maximum_bytes:raise ValueError('Source snapshot byte budget exceeded')
        target=destination/logical;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        target.write_bytes(raw);target.chmod(0o700 if os.access(source,os.X_OK) else 0o600)
    return {'files':len(files),'bytes':total}

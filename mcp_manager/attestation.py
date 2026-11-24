"""Read-only provenance for the reviewed filesystem server; no process launch."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import stat

REVIEWED_FILESYSTEM = ('2.2.0', '5ab8e1b2e1fefd6f0549fc21073b2533e6ad5af7f9cfceb03982a1dc9883c160')

def checked(path):
    path=Path(path)
    text=str(path);windows=PureWindowsPath(text)
    if (not path.is_absolute() or '..' in path.parts or text.startswith(('\\\\','//'))
            or ':' in text[len(path.drive):] or windows.is_reserved()):
        raise ValueError('Local absolute path required')
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Reparse point denied')
    return path.resolve(strict=True)

def filesystem_artifact(config):
    try:
        if Path(config.executable).name.lower()!='node.exe' or not config.args:return None
        executable=checked(config.executable);script=checked(config.args[0])
        if script.name!='index.js' or script.parent.name!='dist':return None
        package=script.parent.parent;manifest=checked(package/'package.json')
        info=json.loads(manifest.read_text(encoding='utf-8'))
        if info.get('name')!='@modelcontextprotocol/server-filesystem':return None
        files=[manifest];pending=[script.parent];visited=0
        while pending:
            directory=pending.pop()
            for item in directory.iterdir():
                visited+=1
                if visited>1024:return None
                checked(item)
                if item.is_dir():pending.append(item)
                elif item.suffix=='.js':files.append(item)
                if len(files)>128:return None
        files.sort(key=lambda p:p.relative_to(package).as_posix())
        if sum(p.stat().st_size for p in files)>2*1024*1024:return None
        digest=hashlib.sha256()
        for path in files:
            checked(path)
            digest.update(path.relative_to(package).as_posix().encode()+b'\0'+hashlib.sha256(path.read_bytes()).digest())
        args=config.args
        if len(args)==3 and args[1]=='--read-only':root=checked(args[2]);relative=False
        elif len(args)==4 and args[1:3]==('--read-only','--confine-roots'):
            root=checked(args[3]);relative=True
        else:return None
        if not root.is_dir():return None
        st=root.stat()
        return {'version':info.get('version'),'digest':digest.hexdigest(),
            'executable_hash':hashlib.sha256(executable.read_bytes()).hexdigest(),
            'root':str(root),'root_identity':[st.st_dev,st.st_ino],'allow_relative':relative}
    except (OSError,ValueError,TypeError):return None

def verified_filesystem(config, connection_info):
    info=connection_info or {};artifact=filesystem_artifact(config)
    from .registry import config_data,fingerprint
    if (not info.get('default_stdio') or not info.get('roots_disabled')
            or info.get('config_fingerprint')!=fingerprint(config_data(config))
            or info.get('protocol_version') not in {'2026-07-28','2025-11-25','2025-06-18','2025-03-26','2024-11-05'}
            or not artifact or (artifact['version'],artifact['digest'])!=REVIEWED_FILESYSTEM
            or artifact!=info.get('filesystem_artifact')):
        raise ValueError('Filesystem connection contract is unverified or changed')
    return artifact

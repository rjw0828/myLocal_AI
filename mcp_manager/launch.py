"""Bounded provenance snapshot for the locally reviewed filesystem launcher."""
import hashlib
import json
from pathlib import Path


def filesystem_artifact(config):
    try:
        if Path(config.executable).name.lower() != "node.exe" or not config.args:
            return None
        script = Path(config.args[0])
        if not script.is_absolute() or script.name != "index.js" or script.parent.name != "dist":
            return None
        package = script.parent.parent
        info = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if info.get("name") != "@modelcontextprotocol/server-filesystem":
            return None
        files = sorted([package / "package.json", *script.parent.rglob("*.js")], key=lambda p: p.relative_to(package).as_posix())
        if len(files) > 128 or sum(p.stat().st_size for p in files) > 2 * 1024 * 1024:
            return None
        digest = hashlib.sha256()
        for path in files:
            if path.is_symlink():
                return None
            digest.update(path.relative_to(package).as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
        result = {"digest": digest.hexdigest(), "version": info.get("version")}
        if len(config.args) == 3 and config.args[1] == "--read-only":
            root = Path(config.args[2]).resolve(strict=True)
            identity = root.stat()
            result["root"] = {"path": str(root), "identity": [identity.st_dev, identity.st_ino]}
        return result
    except (OSError, ValueError, TypeError):
        return None

"""Untrusted web proposals; only the separate local CLI can approve profiles."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from .errors import MCPServiceError
from .models import ServerConfig
from .registry import config_data

DEFAULT_APPROVALS = Path(__file__).resolve().parents[1] / "data" / "mcp_approved_profiles.json"
LIMIT = 1024 * 1024


def read_manifest(path):
    if not path.exists():
        return {"profiles": []}
    if path.stat().st_size > LIMIT:
        raise ValueError("Approval file exceeds 1 MiB")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"profiles"} or not isinstance(document["profiles"], list):
        raise ValueError("Invalid approval file")
    seen = set()
    for item in document["profiles"]:
        if (not isinstance(item, dict) or not {"profile_id", "executable"} <= set(item)
                or set(item) - {"profile_id", "executable", "args", "cwd"}):
            raise ValueError("Invalid approval profile")
        config = ServerConfig(item["profile_id"], item["executable"], item.get("args", ()), item.get("cwd"))
        if config.server_id in seen:
            raise ValueError("Duplicate approval profile")
        seen.add(config.server_id)
    return document


class ProfileRequests:
    def __init__(self, directory, manifest):
        self.directory, self.manifest = Path(directory).resolve(), Path(manifest).resolve()

    def path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise MCPServiceError("mcp_profile_request_missing", "Profile request was not found.", 404)
        path = self.directory / (request_id + ".json")
        if path.is_symlink():
            raise MCPServiceError("mcp_profile_request_invalid", "Invalid profile request.")
        return path

    def create(self, name, transport, executable, arguments):
        if transport != "stdio":
            raise MCPServiceError("mcp_invalid_transport", "Only local stdio profiles are supported.")
        if not isinstance(arguments, list):
            raise MCPServiceError("mcp_invalid_input", "Arguments must be an array of separate strings.")
        whitespace_arguments = [str(index + 1) for index, item in enumerate(arguments)
                                if isinstance(item, str) and item != item.strip()]
        if whitespace_arguments:
            raise MCPServiceError("mcp_argument_whitespace",
                                  "Arguments " + ", ".join(whitespace_arguments) +
                                  ": remove leading/trailing whitespace. Internal spaces are allowed; values are not changed automatically.")
        request_id = uuid4().hex
        try:
            config = ServerConfig("local-" + request_id, executable, arguments)
            record = {"request_id": request_id, "profile_id": config.server_id, "name": name,
                      "transport": "stdio", **config_data(config), "created_at": time.time()}
            encoded = json.dumps(record, ensure_ascii=True, allow_nan=False)
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise ValueError()
        except (ValueError, TypeError, OSError):
            raise MCPServiceError("mcp_invalid_execution", "Use an existing absolute executable and fixed arguments; shell launchers are prohibited.") from None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if len(list(self.directory.glob("*.json"))) >= 64:
                raise MCPServiceError("mcp_profile_queue_full", "Too many profile requests. Cancel unused requests first.", 429)
            with self.path(request_id).open("x", encoding="utf-8") as target:
                target.write(encoded)
        except OSError:
            raise MCPServiceError("mcp_profile_storage_failed", "Cannot store local profile request.", 503) from None
        return record

    def read(self, request_id):
        try:
            path = self.path(request_id)
            if not path.exists():
                raise MCPServiceError("mcp_profile_request_missing", "Profile request was not found.", 404)
            if path.stat().st_size > 64 * 1024:
                raise ValueError()
            record = json.loads(path.read_text(encoding="utf-8"))
            expected = {"request_id", "profile_id", "name", "transport", "executable", "args", "cwd", "created_at"}
            if (not isinstance(record, dict) or set(record) != expected or record["request_id"] != request_id
                    or record["profile_id"] != "local-" + request_id or record["transport"] != "stdio"
                    or not isinstance(record["name"], str) or not record["name"].strip() or len(record["name"]) > 128
                    or not isinstance(record["created_at"], (int, float))):
                raise ValueError()
            if not 0 <= time.time() - record["created_at"] < 24 * 3600:
                raise MCPServiceError("mcp_profile_request_expired", "Profile request expired. Cancel it and submit again.", 409)
            config = ServerConfig(record["profile_id"], record["executable"], record["args"], record["cwd"])
            if config_data(config) != {key: record[key] for key in ("executable", "args", "cwd")}:
                raise ValueError()
            return record
        except (OSError, ValueError, TypeError, KeyError):
            raise MCPServiceError("mcp_profile_request_invalid", "Cannot read a valid local profile request.", 503) from None

    def delete(self, request_id):
        try:
            self.path(request_id).unlink(missing_ok=True)
        except OSError:
            raise MCPServiceError("mcp_profile_storage_failed", "Cannot remove profile request.", 503) from None

    def view(self, record, approved):
        return {"request_id": record["request_id"], "profile_id": record["profile_id"], "name": record["name"],
                "status": "approved" if approved else "pending_local_approval",
                "approval_command": [r".\.venv\Scripts\python.exe", r"scripts\approve_mcp_profile.py", record["request_id"],
                                     "--requests-dir", str(self.directory), "--approvals-file", str(self.manifest)]}


@contextmanager
def manifest_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    # Exclusive creation serializes cooperating local approvers. Never steal stale locks.
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def approve_request(requests, request_id, confirm):
    """Local-terminal entry point only. Never called by a Flask route."""
    record = requests.read(request_id)
    if not confirm(record):
        return False
    with manifest_lock(requests.manifest):
        # Recheck after the interactive wait so a changed proposal is never approved.
        if requests.read(request_id) != record:
            raise ValueError("Profile request changed; review a new request")
        original = requests.manifest.read_bytes() if requests.manifest.exists() else None
        document = read_manifest(requests.manifest)
        if (requests.manifest.read_bytes() if requests.manifest.exists() else None) != original:
            raise ValueError("Approval file changed during review; retry")
        existing = next((item for item in document["profiles"] if item["profile_id"] == record["profile_id"]), None)
        entry = {key: record[key] for key in ("profile_id", "executable", "args", "cwd")}
        if existing is not None:
            if existing != entry:
                raise ValueError("Existing profile differs; refusing to overwrite it")
            return True
        document["profiles"].append(entry)
        payload = json.dumps(document, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        if len(payload.encode("utf-8")) > LIMIT:
            raise ValueError("Approval file would exceed 1 MiB")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=requests.manifest.parent, delete=False) as target:
                temporary = target.name
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            if (requests.manifest.read_bytes() if requests.manifest.exists() else None) != original:
                raise ValueError("Approval file changed during save; retry")
            os.replace(temporary, requests.manifest)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return True

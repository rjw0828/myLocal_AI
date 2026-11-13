"""Attest the reviewed static-CLI filesystem implementation, or deny it.

Does not execute list_allowed_directories or grant metadata tools automatically.
The pinned source/version must be reviewed again after a package update.
"""
from pathlib import Path

from mcp_manager.launch import filesystem_artifact
from mcp_manager.models import ServerConfig
from .filesystem_policy import FilesystemPolicy, checked_path
from .models import ToolChatError

REVIEWED_FILESYSTEM = ("2.2.0", "5ab8e1b2e1fefd6f0549fc21073b2533e6ad5af7f9cfceb03982a1dc9883c160")


class RootAdapter:
    def __init__(self, mcp):
        self.mcp = mcp

    def attest(self, server_id):
        context = self.mcp.inspect_server(server_id)
        root = self.validate_context(context)
        return AttestedFilesystemPolicy(self, root, context)

    def validate_context(self, context):
        server_id = context["server_id"]
        info, config = context.get("connection_info") or {}, context["config"]
        args = config["args"]
        # Exact supported parser subset: script, --read-only, one absolute root.
        # Roots callbacks are disabled; no mutable Roots policy is negotiated.
        if (not info.get("default_stdio") or not info.get("roots_disabled")
                or info.get("protocol_version") not in {"2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
                or len(args) != 3 or args[1] != "--read-only"):
            raise ToolChatError("filesystem_policy_unverified")
        server_config = ServerConfig(server_id, config["executable"], args, config["cwd"])
        artifact = filesystem_artifact(server_config)
        if (not artifact or (artifact["version"], artifact["digest"]) != REVIEWED_FILESYSTEM
                or artifact != info.get("filesystem_artifact")):
            raise ToolChatError("filesystem_implementation_changed")
        root, _identity = checked_path(args[2])
        if not root.is_dir():
            raise ToolChatError("filesystem_policy_unverified")
        return root

    def bindings(self):
        bindings = {}
        for server in self.mcp.list_servers():
            if not server["execution_approved"] or server["status"]["state"] != "RUNNING":
                continue
            sid = server["server_id"]
            try:
                context = self.mcp.inspect_server(sid)
                args = context["config"]["args"]
                if (len(args) in {2, 3} and args[:2] == ["-m", "mcp_server_time"]
                        and (len(args) == 2 or args[2].startswith("--local-timezone="))):
                    bindings[sid] = ("time", None)
                elif args and Path(args[0]).name == "index.js":
                    bindings[sid] = ("filesystem", self.attest(sid))
            except Exception:
                # Unknown/unverifiable servers are never silently exposed.
                continue
        return bindings


class AttestedFilesystemPolicy(FilesystemPolicy):
    def __init__(self, adapter, root, context):
        super().__init__([root], verified=True,
                         server_identity=(context["server_id"], context["config_hash"], context["connection_generation"]))
        self.adapter = adapter

    def verify_context(self, context):
        super().verify_context(context)
        if self.adapter.validate_context(context) != self.roots[0][0]:
            raise ToolChatError("filesystem_root_changed")

"""Conservative Windows paths. Preflight is NOT an atomic filesystem sandbox."""
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import os
import stat

from .models import ToolChatError


def checked_path(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ToolChatError("filesystem_invalid_path")
    windows = PureWindowsPath(value)
    if (not windows.is_absolute() or len(windows.drive) != 2 or windows.drive[1] != ":"
            or value.startswith(("\\\\", "//")) or ".." in windows.parts
            or windows.is_reserved()
            or any(":" in part or part.endswith((".", " ")) or any(c in part for c in '*?\x00')
                   for part in windows.parts[1:])):
        raise ToolChatError("filesystem_invalid_path")
    if os.name != "nt":
        raise ToolChatError("filesystem_windows_required")
    try:
        original = Path(value)
        # Reparse points are conservatively refused, including in ancestors.
        for item in (original, *original.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ToolChatError("filesystem_reparse_point")
        resolved = original.resolve(strict=True)
        info = resolved.stat()
        return resolved, (info.st_dev, info.st_ino)
    except OSError:
        raise ToolChatError("filesystem_path_unavailable") from None


def inside(path, root):
    return path == root or root in path.parents


@dataclass(frozen=True)
class FileScope:
    path: str
    kind: str
    recursive: bool = False


class FilesystemPolicy:
    """Roots must be attested by the trusted adapter, never an LLM or HTTP body.

    Until a scope-enforcing server exists, execution only accepts approvals of
    an entire actual server root (recursive). Narrower scopes are representable
    and checked but NOT dispatched. Existing servers/configs are never changed.
    """

    def __init__(self, roots, *, verified=False, server_identity=None):
        if not verified or not roots:
            raise ToolChatError("filesystem_policy_unverified")
        self.roots = tuple(checked_path(str(root)) for root in roots)
        self.server_identity = server_identity

    def verify_context(self, context):
        current = (context["server_id"], context["config_hash"], context["connection_generation"])
        if self.server_identity != current or "--read-only" not in context.get("config", {}).get("args", []):
            raise ToolChatError("filesystem_policy_unverified")

    def _roots_now(self):
        roots = []
        for path, identity in self.roots:
            current, current_identity = checked_path(str(path))
            if current != path or current_identity != identity or not current.is_dir():
                raise ToolChatError("filesystem_root_changed")
            roots.append(current)
        return roots

    def scope(self, scope):
        if not isinstance(scope, FileScope) or scope.kind not in {"file", "directory"} or type(scope.recursive) is not bool:
            raise ToolChatError("filesystem_invalid_scope")
        path, identity = checked_path(scope.path)
        if ((scope.kind == "file" and (not path.is_file() or scope.recursive))
                or (scope.kind == "directory" and not path.is_dir())
                or not any(inside(path, root) for root in self._roots_now())):
            raise ToolChatError("filesystem_scope_denied")
        return (scope, path, identity)

    def validate(self, tool_name, arguments, approved, *, dispatch=False):
        if tool_name not in {"read_file", "list_directory"}:
            raise ToolChatError("filesystem_tool_denied")
        scope, scope_path, identity = approved
        if self.scope(scope)[1:] != (scope_path, identity):
            raise ToolChatError("filesystem_scope_changed")
        path, _ = checked_path(arguments.get("path"))
        if not any(inside(path, root) for root in self._roots_now()):
            raise ToolChatError("filesystem_outside_roots")
        if tool_name == "read_file" and not path.is_file() or tool_name == "list_directory" and not path.is_dir():
            raise ToolChatError("filesystem_wrong_type")
        covered = (path == scope_path if scope.kind == "file" else
                   inside(path, scope_path) if scope.recursive else
                   path == scope_path or (tool_name == "read_file" and path.parent == scope_path))
        if not covered:
            raise ToolChatError("filesystem_outside_grant")
        if dispatch and not (scope.kind == "directory" and scope.recursive and self._roots_now() == [scope_path]):
            raise ToolChatError("filesystem_scope_not_server_enforced")
        return str(path)

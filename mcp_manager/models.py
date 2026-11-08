"""Public, SDK-independent types for the optional MCP core runtime."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ServerState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"
    UNCERTAIN = "UNCERTAIN"


class OperationState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNCERTAIN = "UNCERTAIN"


class MCPRuntimeError(RuntimeError):
    """A sanitized runtime failure; never carries an upstream exception string."""

    def __init__(self, code: str, message: str, *, operation_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


class MCPTimeoutError(MCPRuntimeError):
    """The synchronous wait ended; the operation may still be executing."""


@dataclass(frozen=True)
class ServerConfig:
    """An explicitly approved in-memory stdio registration, not a shell command.

    The registering Python caller is trusted to approve the executable AND its
    arguments. No process starts on construction. Environment interpolation,
    shell wrappers, PATH lookup, and implicit package downloads are unsupported.
    """

    server_id: str
    executable: str
    args: tuple[str, ...] = ()
    cwd: str | None = None

    def __post_init__(self):
        if (not isinstance(self.server_id, str) or not self.server_id.strip()
                or len(self.server_id) > 128 or "\x00" in self.server_id):
            raise ValueError("server_id must be a nonempty string of at most 128 characters")
        if not isinstance(self.executable, str) or "\x00" in self.executable:
            raise ValueError("an absolute executable path is required")
        executable = Path(self.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("an existing absolute executable path is required")
        executable = executable.resolve()
        forbidden = {"cmd", "powershell", "pwsh", "sh", "bash", "zsh", "csh", "wscript", "cscript"}
        if executable.stem.lower() in forbidden or executable.suffix.lower() in {".bat", ".cmd", ".ps1"}:
            raise ValueError("shell and shell-script launchers are not supported")
        if isinstance(self.args, (str, bytes)) or not isinstance(self.args, (tuple, list)):
            raise ValueError("args must be a sequence of separate strings")
        if any(not isinstance(item, str) or "\x00" in item for item in self.args):
            raise ValueError("args must contain only strings without NUL characters")
        directory = Path(self.cwd) if self.cwd is not None else executable.parent
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        object.__setattr__(self, "executable", str(executable))
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "cwd", str(directory.resolve()))


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallResult:
    content: tuple[dict[str, Any], ...]
    structured_content: dict[str, Any] | None
    is_error: bool


@dataclass(frozen=True)
class OperationOutcome:
    operation_id: str
    kind: str
    state: OperationState
    result: Any = None
    error_code: str | None = None
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class ServerStatus:
    server_id: str
    state: ServerState
    active_operation_id: str | None = None
    pending_operations: int = 0
    last_operation: OperationOutcome | None = None
    error_code: str | None = None

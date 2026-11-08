"""Optional MCP core. Importing this package starts no thread or process."""
from .manager import MCPManager
from .models import (
    MCPRuntimeError, MCPTimeoutError, OperationOutcome, OperationState,
    ServerConfig, ServerState, ServerStatus, ToolCallResult, ToolInfo,
)

__all__ = [
    "MCPManager", "MCPRuntimeError", "MCPTimeoutError", "OperationOutcome",
    "OperationState", "ServerConfig", "ServerState", "ServerStatus", "ToolCallResult", "ToolInfo",
]

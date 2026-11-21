"""Owner-task single SDK call candidate, explicitly restricted to memory clients."""
from copy import deepcopy
from dataclasses import dataclass
import json

from .sdk_policy import sdk_call_once
from .models import MCPRuntimeError, ServerState, ToolCallResult


@dataclass
class SingleDispatch:
    name: str
    arguments: dict
    before_send: object
    attempted: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 256:
            raise ValueError('Invalid tool name')
        if not isinstance(self.arguments, dict) or not callable(self.before_send):
            raise ValueError('Arguments and trusted admission callback are required')
        encoded = json.dumps(self.arguments, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 65536: raise ValueError('Arguments exceed 64 KiB')
        self.arguments = json.loads(encoded)


async def execute_single(worker, client, item):
    from agent_poc.owner_runtime import OwnerFakeClient
    from agent_poc.readiness_gate import PreparedOwnerAdapter
    from agent_poc.live_transport import ReviewedOwnerClient
    from .async_host import discover_tools
    if type(client) not in (OwnerFakeClient, PreparedOwnerAdapter, ReviewedOwnerClient):
        raise MCPRuntimeError('mcp_execution_bridge_disabled', 'Only memory transport is enabled.')
    payload = item.payload
    tools = await discover_tools(client, refresh=True)
    tool = next((tool for tool in tools if tool.name == payload.name), None)
    if tool is None:
        raise MCPRuntimeError('mcp_tool_not_found', 'Tool is no longer listed.')
    await client.before_dispatch()  # controllable memory-transport checkpoint
    with worker.lock:
        if worker._stop_requested or item.timed_out or worker.state != ServerState.RUNNING:
            raise MCPRuntimeError('mcp_dispatch_rejected', 'Dispatch was cancelled before admission.')
        if type(client) in (PreparedOwnerAdapter, ReviewedOwnerClient):
            client.validate_scope(payload.name, payload.arguments, tool.input_schema)
        payload.before_send(tool, worker.connection_generation)
        client.verify_admission(item.operation_id)
        # No application await/requeue/retry between final admission and this
        # single public SDK invocation. SDK/transport awaits remain a boundary.
        payload.attempted = True
        if type(client) is ReviewedOwnerClient: client.begin_attempt()
    try:
        result = await sdk_call_once(client, payload.name, payload.arguments)
    except BaseException:
        if type(client) is ReviewedOwnerClient: client.response_lost()
        raise
    if type(client) is ReviewedOwnerClient: client.received(result)
    return ToolCallResult(tuple(part.model_dump(mode='json') for part in result.content),
                          deepcopy(result.structured_content), result.is_error)

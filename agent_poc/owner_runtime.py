"""Existing AsyncHost/ServerWorker + actual public SDK Session over memory fixtures.

No stdio factory, subprocess, network connection or MCP server is used here.
"""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from threading import Event

import anyio
from mcp import ClientSession, MCPError
from mcp.types import (CallToolResult, ListToolsResult, Tool, ToolAnnotations,
                       HEADER_MISMATCH, PaginatedRequestParams, InputRequiredResult)

from mcp_manager.manager import MCPManager
from mcp_manager.models import MCPRuntimeError, OperationState, ServerState
from mcp_manager.registry import fingerprint
from .fake_runtime import FakeRuntime


async def wait_gate(gate):
    with anyio.fail_after(3):
        while not gate.is_set(): await anyio.sleep(.002)


class MemorySession(ClientSession):
    """Public send_request seam only; typed fixture responses, no protocol server."""
    def __init__(self, read, write, client):
        super().__init__(read, write)
        self.fixture_client = client

    async def send_request(self, request, result_type, **kwargs):
        client = self.fixture_client
        client.check_owner()
        runtime, sid = client.runtime, client.sid
        if request.method == 'tools/list':
            with runtime.lock:
                return ListToolsResult(tools=[Tool(name=t.name, description=t.description,
                    input_schema=deepcopy(t.input_schema), output_schema=deepcopy(t.output_schema),
                    annotations=ToolAnnotations.model_validate(t.annotations) if t.annotations else None)
                    for t in runtime.tools])
        if request.method != 'tools/call':
            raise RuntimeError('Unsupported fixture request')
        with runtime.lock:
            oid = runtime.active[sid]
            record = runtime.records[oid]
            if fingerprint(request.params.arguments) != runtime.arguments_hash[oid]:
                raise RuntimeError('SDK request differs from authorized arguments')
            runtime.sends += 1
            record['sent'], record['accepted'], record['state'] = True, True, OperationState.RUNNING
            runtime.accepted_event.set()
            if runtime.mode in {'disconnect', 'send_unknown'}:
                record['accepted'], record['state'] = None, OperationState.UNCERTAIN
                if runtime.mode == 'send_unknown': record['sent'] = None
                raise ConnectionError('Memory fixture lost response')
            if runtime.mode == 'sdk_header':
                record['state'] = OperationState.UNCERTAIN
                raise MCPError(HEADER_MISMATCH, 'Fixture header mismatch')
            if runtime.mode == 'sdk_input_required':
                record['state'] = OperationState.UNCERTAIN
                return InputRequiredResult(input_requests={}, request_state='fixture-only')
        try:
            await wait_gate(runtime.finish_gate)
            with runtime.lock:
                runtime.complete(oid)
                return CallToolResult.model_validate(asdict(record['result']))
        except BaseException:
            with runtime.lock:
                if record['state'] not in {OperationState.SUCCEEDED, OperationState.CANCELLED}:
                    record['state'] = OperationState.UNCERTAIN
            raise


class OwnerFakeClient:
    def __init__(self, runtime, config):
        self.runtime, self.sid = runtime, config.server_id

    def check_owner(self):
        if asyncio.current_task() is not self.owner:
            raise RuntimeError('SDK context escaped its owner task')

    def verify_admission(self, oid):
        self.check_owner()
        with self.runtime.lock:
            record = self.runtime.records.get(oid)
            if (record is None or self.runtime.active.get(self.sid) != oid
                    or not record.get('admission_verified') or record['state'] != OperationState.QUEUED):
                raise RuntimeError('No service-authorized operation is attached to this owner dispatch')

    async def __aenter__(self):
        self.owner = asyncio.current_task()
        rs, rr = anyio.create_memory_object_stream(1)
        ws, wr = anyio.create_memory_object_stream(1)
        self.streams = rs, rr, ws, wr
        self.session = MemorySession(rr, ws, self)
        await self.session.__aenter__()
        self.runtime.contexts.append({'owner': self.owner, 'closed': False, 'same_exit': False})
        self.context_record = self.runtime.contexts[-1]
        return self

    async def __aexit__(self, *args):
        self.check_owner()
        try: return await self.session.__aexit__(*args)
        finally:
            for stream in self.streams: await stream.aclose()
            self.context_record['closed'], self.context_record['same_exit'] = True, True

    async def list_tools(self, **kwargs):
        self.check_owner()
        return await self.session.list_tools(params=PaginatedRequestParams(cursor=kwargs.get('cursor')))

    async def before_dispatch(self):
        self.check_owner()
        self.runtime.dispatch_waiting.set()
        await wait_gate(self.runtime.dispatch_gate)

    async def call_tool(self, *args, **kwargs):
        raise AssertionError('High-level SDK retry path must not be used')


class OwnerFakeFactory:
    def __init__(self, runtime): self.runtime = runtime
    def __call__(self, config, errlog): return OwnerFakeClient(self.runtime, config)


class OwnerFakeRuntime(FakeRuntime):
    def __init__(self, *, wait_seconds=.5, prepared=False):
        super().__init__(wait_seconds=wait_seconds)
        self.active, self.arguments_hash, self.contexts = {}, {}, []
        self.dispatch_waiting = Event()
        if prepared:
            from .readiness_gate import PreparedMemoryFactory
            factory = PreparedMemoryFactory(self)
        else: factory = OwnerFakeFactory(self)
        self.manager = MCPManager(_client_factory=factory,
            stop_timeout=.05, cleanup_timeout=2)

    def register_server(self, config): self.manager.register_server(config)
    def unregister_server(self, sid): self.manager.unregister_server(sid)
    def start_server(self, sid): return self.manager.start_server(sid)
    def stop_server(self, sid): return self.manager.stop_server(sid)
    def connection_generation(self, sid): return self.manager.connection_generation(sid)
    def get_status(self, sid): return self.manager.get_status(sid)
    def get_operation(self, sid, oid): return self.manager.get_operation(sid, oid)
    def list_tools(self, sid, **kwargs): return self.manager.list_tools(sid, **kwargs)

    def call_tool_once(self, sid, name, arguments, *, operation_id, before_send):
        with self.lock:
            if operation_id in self.records:
                raise MCPRuntimeError('mcp_duplicate_operation', 'Operation ID already reserved.')
            if len(self.records) >= 512: raise MCPRuntimeError('mcp_operation_limit', 'Memory retention limit.')
            if any(r['server_id'] == sid and r['state'] in {OperationState.QUEUED, OperationState.RUNNING,
                   OperationState.UNCERTAIN} for r in self.records.values()):
                raise MCPRuntimeError('mcp_busy', 'An owner operation remains unresolved.')
            record = {'operation_id': operation_id, 'server_id': sid, 'state': OperationState.QUEUED,
                'sent': False, 'accepted': False, 'cancel_requested': False, 'cancel_confirmed': False,
                'result': None, 'error': None}
            self.records[operation_id] = record
            self.arguments_hash[operation_id] = fingerprint(arguments)
            self.registered.set()

        def authorize(tool, generation):
            with self.lock:
                if record['cancel_requested']:
                    record['state'], record['cancel_confirmed'] = OperationState.CANCELLED, True
                    raise RuntimeError('Cancelled before SDK dispatch')
                current = next((item for item in self.tools if item.name == name), None)
                if current != tool: raise RuntimeError('Schema changed after owner discovery')
                before_send(tool, generation)
                self.active[sid] = operation_id
                record['admission_verified'] = True

        try:
            return self.manager.call_tool_once(sid, name, arguments, operation_id=operation_id,
                before_send=authorize, timeout=self.wait_seconds)
        except MCPRuntimeError as error:
            with self.lock:
                if record['sent'] is False and record['state'] != OperationState.CANCELLED:
                    # A queued timeout can still await final rejection; do not
                    # assert it is cancelled merely because the caller stopped waiting.
                    if error.code != 'mcp_wait_timeout': record['state'] = OperationState.FAILED
                record['error'] = error.code
            raise

    def shutdown(self):
        self.dispatch_gate.set(); self.finish_gate.set()
        self.manager.shutdown()

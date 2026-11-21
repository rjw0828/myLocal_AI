"""In-memory MCP Runtime/transport simulator; no SDK client or child process."""
from copy import deepcopy
from threading import Event, RLock
from uuid import uuid4

from mcp_manager.models import (MCPRuntimeError, MCPTimeoutError, OperationOutcome,
    OperationState, ServerState, ServerStatus, ToolCallResult, ToolInfo)


class FakeRuntime:
    def __init__(self, *, wait_seconds=.05):
        self.lock = RLock()
        self.configs, self.states, self.generations, self.records = {}, {}, {}, {}
        self.tools = [ToolInfo('fixture_lookup', 'Read a harmless fixture',
            {'type': 'object', 'properties': {'value': {'type': 'integer'}},
             'required': ['value'], 'additionalProperties': False},
            {'type': 'object'}, {'readOnlyHint': True})]
        self.result = ToolCallResult(({'type': 'text', 'text': 'fixture result'},), {'value': 42}, False)
        self.dispatch_gate, self.finish_gate, self.registered, self.accepted_event = Event(), Event(), Event(), Event()
        self.dispatch_gate.set(); self.finish_gate.set()
        self.mode, self.wait_seconds, self.sends = 'success', wait_seconds, 0

    def register_server(self, config):
        with self.lock:
            self.configs[config.server_id] = config
            self.states.setdefault(config.server_id, ServerState.STOPPED)

    def start_server(self, sid):
        with self.lock:
            if self.states[sid] != ServerState.RUNNING:
                self.generations[sid] = uuid4().hex
            self.states[sid] = ServerState.RUNNING
            return self.get_status(sid)

    def stop_server(self, sid):
        with self.lock:
            self.states[sid] = ServerState.STOPPED
            return self.get_status(sid)

    def unregister_server(self, sid):
        with self.lock:
            self.configs.pop(sid); self.states.pop(sid)

    def connection_generation(self, sid):
        with self.lock:
            if self.states[sid] != ServerState.RUNNING:
                raise MCPRuntimeError('mcp_not_ready', 'Fake server is stopped.')
            return self.generations[sid]

    def get_status(self, sid):
        with self.lock:
            last = next((r for r in reversed(list(self.records.values())) if r['server_id'] == sid), None)
            return ServerStatus(sid, self.states[sid], last_operation=self._outcome(last) if last else None)

    def list_tools(self, sid, **kwargs):
        with self.lock:
            self.connection_generation(sid)
            return deepcopy(tuple(self.tools))

    def call_tool(self, *args, **kwargs):
        raise AssertionError('C-3B-1 must use the guarded single-attempt interface')

    def call_tool_once(self, sid, name, arguments, *, operation_id, before_send):
        with self.lock:
            if operation_id in self.records:
                raise MCPRuntimeError('mcp_duplicate_operation', 'Operation ID is already registered.')
            if any(r['server_id'] == sid and r['state'] in {OperationState.QUEUED, OperationState.RUNNING,
                   OperationState.UNCERTAIN} for r in self.records.values()):
                raise MCPRuntimeError('mcp_busy', 'A fake operation is still unresolved.')
            if len(self.records) >= 512:
                raise MCPRuntimeError('mcp_operation_limit', 'Fake operation retention limit.')
            record = {'operation_id': operation_id, 'server_id': sid, 'state': OperationState.QUEUED,
                'sent': False, 'accepted': False, 'cancel_requested': False, 'cancel_confirmed': False,
                'result': None, 'error': None}
            self.records[operation_id] = record; self.registered.set()
        if not self.dispatch_gate.wait(2):
            with self.lock:
                record['state'], record['error'] = OperationState.FAILED, 'fake_dispatch_wait_expired'
            raise MCPRuntimeError('fake_dispatch_wait_expired', 'Fake pre-send wait expired.', operation_id=operation_id)
        with self.lock:
            if record['cancel_requested']:
                record['state'], record['cancel_confirmed'] = OperationState.CANCELLED, True
                raise MCPRuntimeError('mcp_cancelled_before_send', 'Cancelled before send.', operation_id=operation_id)
            if self.mode == 'pre_fail':
                record['state'], record['error'] = OperationState.FAILED, 'fake_pre_send_failure'
                raise MCPRuntimeError('fake_pre_send_failure', 'Fake pre-send failure.', operation_id=operation_id)
            try:
                self.connection_generation(sid)
                before_send()  # Same Runtime lock pins schema/connection through admission.
            except Exception:
                record['state'], record['error'] = OperationState.FAILED, 'admission_rejected'
                raise
            self.sends += 1
            record['sent'], record['state'] = True, OperationState.RUNNING
            if self.mode in {'disconnect', 'send_unknown'}:
                record['accepted'], record['state'] = None, OperationState.UNCERTAIN
                if self.mode == 'send_unknown': record['sent'] = None
                raise MCPRuntimeError('mcp_call_uncertain', 'Fake response lost after send.', operation_id=operation_id)
            record['accepted'] = True; self.accepted_event.set()
        if not self.finish_gate.wait(self.wait_seconds):
            raise MCPTimeoutError('mcp_wait_timeout', 'Fake caller timed out.', operation_id=operation_id)
        with self.lock:
            self.complete(operation_id)
            return deepcopy(record['result'])

    def complete(self, oid):
        with self.lock:
            record = self.records[oid]
            if record['sent'] is False:
                raise ValueError('Cannot complete an unsent operation')
            if record['state'] == OperationState.CANCELLED:
                raise MCPRuntimeError('mcp_cancelled', 'Cancellation was confirmed.', operation_id=oid)
            record['state'], record['result'], record['accepted'], record['sent'] = OperationState.SUCCEEDED, deepcopy(self.result), True, True

    def request_cancel(self, oid):
        with self.lock:
            if oid in self.records: self.records[oid]['cancel_requested'] = True

    def confirm_cancel(self, oid):
        with self.lock:
            record = self.records[oid]
            record['cancel_requested'], record['cancel_confirmed'] = True, True
            record['state'] = OperationState.CANCELLED

    def details(self, sid, oid):
        with self.lock:
            record = self.records.get(oid)
            if record is None or record['server_id'] != sid:
                raise MCPRuntimeError('mcp_operation_not_found', 'Unknown fake operation.')
            return deepcopy(record)

    def _outcome(self, record):
        return OperationOutcome(record['operation_id'], 'call', record['state'],
                                deepcopy(record['result']), record['error'], 0.0)

    def get_operation(self, sid, oid):
        return self._outcome(self.details(sid, oid))

    def shutdown(self):
        self.dispatch_gate.set(); self.finish_gate.set()
        with self.lock:
            for sid in self.states: self.states[sid] = ServerState.STOPPED

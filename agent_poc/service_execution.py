"""Guarded MCPService execution over explicit owner runtimes and their ledgers.

The historical FakeServiceExecutor name is retained for existing callers. The
standard MCPManager is supported only through its guarded single-dispatch API.
"""
import asyncio
from copy import deepcopy
import json
from threading import RLock

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from mcp_manager.models import MCPRuntimeError, MCPTimeoutError, OperationState
from mcp_manager.registry import canonical_json
from mcp_manager.service import MCPService
from .fake_runtime import FakeRuntime
from .owner_runtime import OwnerFakeRuntime
from .live_transport import ReviewedStdioRuntime
from mcp_manager.manager import MCPManager


STATES = frozenset({'CREATED', 'VALIDATING', 'DISPATCHING', 'RUNNING', 'COMPLETED',
                    'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNCERTAIN'})
TERMINAL = frozenset({'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNCERTAIN'})
TRANSITIONS = {
    'CREATED': {'VALIDATING', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNCERTAIN'},
    'VALIDATING': {'DISPATCHING', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNCERTAIN'},
    'DISPATCHING': {'RUNNING', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNCERTAIN'},
    'RUNNING': {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNCERTAIN'},
}


class ExecutionError(RuntimeError):
    def __init__(self, code, operation_id):
        self.code, self.operation_id = code, operation_id
        super().__init__(code)


class FakeServiceExecutor:
    def __init__(self, service):
        if type(service) is not MCPService or type(service._runtime) not in (FakeRuntime, OwnerFakeRuntime, ReviewedStdioRuntime, MCPManager):
            raise TypeError('C-3B-1 requires MCPService already connected to the exact FakeRuntime')
        self.service, self.runtime = service, service._runtime
        self._lock, self._records = RLock(), {}

    def _require_fake(self):
        if self.service._runtime is not self.runtime or type(self.runtime) not in (FakeRuntime, OwnerFakeRuntime, ReviewedStdioRuntime, MCPManager):
            raise RuntimeError('Real Runtime substitution is forbidden')

    def register(self, oid, sid):
        with self._lock:
            if oid in self._records: raise ExecutionError('duplicate_operation', oid)
            if len(self._records) >= 512: raise ExecutionError('operation_limit', oid)
            self._records[oid] = {'operation_id': oid, 'server_id': sid, 'state': 'CREATED',
                'history': ['CREATED'], 'proposal_consumed': False, 'service_token_consumed': False,
                'sent': False, 'accepted': False, 'cancel_requested': False, 'cancel_confirmed': False,
                'unsettled': False, 'error': None, 'result': None, 'observed_state': None,
                'observed_result': None, 'result_access_denied': False}

    def _state(self, oid, state, error=None):
        record = self._records[oid]
        if record['state'] not in TERMINAL:
            if state not in TRANSITIONS[record['state']]:
                raise RuntimeError('Invalid execution state transition')
            record['state'], record['error'] = state, error
            record['history'].append(state)

    def snapshot(self, oid):
        with self._lock: return deepcopy(self._records[oid])

    def mark_timeout(self, oid):
        with self._lock:
            # Once a worker is validating in another thread, lack of a send
            # observation is not proof it cannot subsequently reach admission.
            self._records[oid]['unsettled'] = True
            self._state(oid, 'TIMED_OUT', 'caller_timeout')

    def request_cancel(self, oid):
        with self._lock:
            record = self._records[oid]; record['cancel_requested'] = True
            if record['proposal_consumed'] or record['sent']:
                self._state(oid, 'UNCERTAIN', 'cancel_requested_after_admission')
        # No Core lock is held by the caller. Never hold tracker lock while
        # entering the Runtime lock (Runtime's admission callback uses tracker).
        self.runtime.request_cancel(oid)

    async def execute_authorized(self, oid, entry, arguments, claim):
        return await asyncio.to_thread(self._execute, oid, entry, arguments, claim)

    def _execute(self, oid, entry, arguments, claim):
        token = None
        with self._lock: self._state(oid, 'VALIDATING')
        try:
            self._require_fake()
            # Reuse service profile, Allow/Block, schema and canonical argument
            # validation; C-3A approval is never itself a service call token.
            token = self.service.approve_call(entry.server_id, entry.original_name, arguments)['approval_token']

            def before_send(context, args):
                with self._lock: self._records[oid]['service_token_consumed'] = True
                claim(context, args)  # latest Run/revision/deadline and exact proposal
                with self._lock:
                    self._records[oid]['proposal_consumed'] = True
                    self._records[oid]['unsettled'] = True
                    self._state(oid, 'DISPATCHING')

            response = self.service.call_tool(entry.server_id, entry.original_name, arguments,
                token, operation_id=oid, before_execute=before_send)
            self.observe(oid)
            result = json.loads(canonical_json(response['result']))
            if len(canonical_json(result).encode('utf-8')) > 65536:
                raise ExecutionError('tool_result_too_large', oid)
            if result.get('is_error') is not False:
                raise ExecutionError('tool_returned_error', oid)
            if not isinstance(result.get('content'), list):
                raise ExecutionError('tool_result_invalid', oid)
            schema = entry.descriptor.get('output_schema')
            if schema is not None:
                Draft202012Validator(schema, registry=Registry(), format_checker=FormatChecker()).validate(result.get('structured_content'))
            with self._lock:
                self._records[oid]['result'] = deepcopy(result)
                if self._records[oid]['state'] in {'TIMED_OUT', 'UNCERTAIN', 'CANCELLED'}:
                    raise ExecutionError('late_result_discarded', oid)
                self._state(oid, 'COMPLETED')
            return result  # original content/structured_content/is_error envelope
        except Exception as error:
            self.observe(oid)
            with self._lock:
                record = self._records[oid]
                code = getattr(error, 'code', 'execution_validation_failed')
                if record['sent'] is False and record['accepted'] is False:
                    if record['cancel_requested']: record['cancel_confirmed'] = True
                    self._state(oid, 'CANCELLED' if record['cancel_requested'] else 'FAILED', code)
                elif isinstance(error, MCPTimeoutError):
                    self._state(oid, 'TIMED_OUT', code)
                elif record['unsettled']:
                    self._state(oid, 'UNCERTAIN', code)
                else:
                    self._state(oid, 'FAILED', code)
            raise ExecutionError(code, oid) from None
        finally:
            if token is not None: self.service.revoke_call_approval(token)

    def observe(self, oid):
        with self._lock: sid = self._records[oid]['server_id']
        try:
            details = self.runtime.details(sid, oid)
        except MCPRuntimeError:
            return self.snapshot(oid)  # no Runtime registration: definitely no Fake send
        observed_result, denied = None, False
        if details['result'] is not None:
            try:
                observed_result = self.service.operation(sid, oid).get('result')
            except Exception:
                denied = True
        with self._lock:
            record = self._records[oid]
            for key in ('sent', 'accepted', 'cancel_confirmed'):
                record[key] = details[key]
            record['observed_state'] = details['state'].value
            record['observed_result'], record['result_access_denied'] = observed_result, denied
            if denied: record['result'] = None
            record['unsettled'] = details['state'] in {OperationState.QUEUED, OperationState.RUNNING, OperationState.UNCERTAIN}
            if details['accepted'] is True and record['state'] == 'DISPATCHING':
                self._state(oid, 'RUNNING')
            if details['cancel_confirmed'] and details['state'] == OperationState.CANCELLED:
                self._state(oid, 'CANCELLED', 'operation_cancel_confirmed')
            # Audit late outcomes without promoting a terminal logical operation.
            return deepcopy(record)

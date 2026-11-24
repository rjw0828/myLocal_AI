"""Server-owned one-use proposals and retained fake-operation outcomes.

This is a policy boundary, not an Agent loop, HTTP authentication layer or
real MCP executor. All state is memory-only and protected by one RLock.
"""
import asyncio
from copy import deepcopy
import math
from threading import RLock
import time
from uuid import uuid4

from mcp_manager.registry import canonical_json, fingerprint
from .fixture_executor import FixtureExecutor


PROPOSAL_STATES = frozenset({'PENDING', 'APPROVED', 'DENIED', 'CONSUMED', 'EXPIRED', 'CANCELLED'})
TERMINAL = frozenset({'COMPLETED', 'FAILED', 'CANCELLED', 'UNCERTAIN'})


class ApprovalError(PermissionError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ApprovalCore:
    _executor_type = FixtureExecutor

    def __init__(self, catalog, executor, *, clock=time.monotonic, approval_seconds=120,
                 run_seconds=180, call_seconds=30, max_records=512):
        # Not an arbitrary callable, protocol or production executor injection.
        if type(executor) is not self._executor_type:
            raise TypeError('This Core accepts only its fixed execution adapter')
        for value in (approval_seconds, run_seconds, call_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Timeouts must be positive finite numbers')
        if type(max_records) is not int or max_records <= 0:
            raise ValueError('Invalid record limit')
        self.catalog, self.executor, self.clock = catalog, executor, clock
        self.approval_seconds, self.run_seconds, self.call_seconds = approval_seconds, run_seconds, call_seconds
        self.max_records = max_records
        self._lock = RLock()
        self._sessions, self._runs, self._proposals, self._calls, self._operations = {}, {}, {}, {}, {}
        self._tasks = set()

    def create_session(self, session_id, revision=0):
        if not isinstance(session_id, str) or not session_id or type(revision) is not int or revision < 0:
            raise ValueError('Invalid virtual session')
        with self._lock:
            if session_id in self._sessions:
                raise ApprovalError('session_exists')
            if len(self._sessions) >= self.max_records:
                raise ApprovalError('record_limit')
            self._sessions[session_id] = revision

    def start_run(self, session_id):
        with self._lock:
            if session_id not in self._sessions:
                raise ApprovalError('session_unknown')
            if len(self._runs) >= self.max_records:
                raise ApprovalError('record_limit')
            for run in self._runs.values():
                if run['session_id'] == session_id:
                    self._refresh(run)
                    if run['state'] not in TERMINAL or run['state'] == 'UNCERTAIN' or run['active_operation'] is not None:
                        raise ApprovalError('session_unsettled')
            rid = uuid4().hex
            self._runs[rid] = {'run_id': rid, 'session_id': session_id,
                'revision': self._sessions[session_id], 'state': 'RUNNING',
                'deadline': self.clock() + self.run_seconds, 'error': None,
                'active_operation': None, 'unsettled': False}
            return rid

    def _run(self, rid):
        if rid not in self._runs:
            raise ApprovalError('run_unknown')
        return self._runs[rid]

    def _end(self, run, state, code):
        run['state'], run['error'] = state, code
        for proposal in self._proposals.values():
            if proposal['run_id'] == run['run_id'] and proposal['state'] in {'PENDING', 'APPROVED'}:
                proposal['state'] = 'EXPIRED' if code in {'approval_expired', 'run_timeout'} else 'CANCELLED'

    def _refresh(self, run):
        if run['state'] in TERMINAL:
            return
        if self._sessions.get(run['session_id']) != run['revision']:
            self._end(run, 'UNCERTAIN' if run['unsettled'] else 'CANCELLED', 'revision_changed')
        elif self.clock() >= run['deadline']:
            self._end(run, 'UNCERTAIN' if run['unsettled'] else 'FAILED', 'run_timeout')
        elif any(p['run_id'] == run['run_id'] and p['state'] in {'PENDING', 'APPROVED'}
                 and self.clock() >= p['expires_at'] for p in self._proposals.values()):
            self._end(run, 'UNCERTAIN' if run['unsettled'] else 'FAILED', 'approval_expired')

    def _open(self, rid):
        run = self._run(rid)
        self._refresh(run)
        if run['state'] in TERMINAL:
            raise ApprovalError(run['error'] or 'run_closed')
        return run

    def status(self, rid):
        with self._lock:
            run = self._run(rid); self._refresh(run)
            return deepcopy(run)

    def _proposal(self, rid, pid):
        proposal = self._proposals.get(pid)
        if proposal is None or proposal['run_id'] != rid:
            raise ApprovalError('proposal_unknown')
        return proposal

    def proposal(self, rid, pid):
        with self._lock:
            self._refresh(self._run(rid))
            return deepcopy(self._proposal(rid, pid))

    def issue(self, rid, call_id, entry, arguments):
        with self._lock:
            run = self._open(rid)
            if run['active_operation'] is not None:
                raise ApprovalError('operation_unresolved')
            if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
                raise ApprovalError('invalid_call_id')
            if (rid, call_id) in self._calls:
                raise ApprovalError('duplicate_tool_call')
            if len(self._proposals) >= self.max_records:
                raise ApprovalError('record_limit')
            if fingerprint(entry.descriptor) != entry.identity or entry.descriptor.get('name') != entry.original_name:
                raise ApprovalError('catalog_entry_forged')
            arguments = self.catalog.validate(entry, arguments)
            self._open(rid)  # Discovery can block; deadline/revision must still hold.
            pid = uuid4().hex
            descriptor = entry.descriptor
            record = {'run_id': rid, 'proposal_id': pid, 'tool_call_id': call_id,
                'session_id': run['session_id'], 'revision': run['revision'],
                'server_id': entry.server_id, 'tool_name': entry.original_name,
                'arguments_json': canonical_json(arguments), 'arguments_fingerprint': fingerprint(arguments),
                'schema_fingerprint': fingerprint(descriptor['input_schema']),
                'source': deepcopy(descriptor['source']), 'catalog_fingerprint': entry.identity,
                'expires_at': min(run['deadline'], self.clock() + self.approval_seconds), 'state': 'PENDING'}
            self._proposals[pid] = record
            self._calls[(rid, call_id)] = (pid, entry)
            run['state'] = 'WAITING_APPROVAL'
            return deepcopy(record)

    def _validate(self, run, proposal, entry, arguments):
        try:
            arguments_hash = fingerprint(arguments)
        except Exception:
            self._end(run, 'FAILED', 'approval_mismatch')
            raise ApprovalError('approval_mismatch') from None
        if (proposal['revision'] != run['revision'] or entry.identity != proposal['catalog_fingerprint']
                or entry.server_id != proposal['server_id'] or entry.original_name != proposal['tool_name']
                or arguments_hash != proposal['arguments_fingerprint']):
            self._end(run, 'FAILED', 'approval_mismatch')
            raise ApprovalError('approval_mismatch')
        try:
            self.catalog.validate(entry, arguments)
        except Exception:
            self._end(run, 'FAILED', 'approval_stale')
            raise ApprovalError('approval_stale') from None
        self._open(run['run_id'])  # Recheck after a potentially blocking discovery.

    def decide(self, rid, pid, allow):
        if type(allow) is not bool:
            raise ApprovalError('invalid_decision')
        with self._lock:
            run = self._open(rid)
            proposal = self._proposal(rid, pid)
            if proposal['state'] not in {'PENDING', 'APPROVED'}:
                raise ApprovalError('proposal_closed')
            _, entry = self._calls[(rid, proposal['tool_call_id'])]
            import json
            if not allow:
                proposal['state'] = 'DENIED'
                self._end(run, 'FAILED', 'approval_denied')
            else:
                self._validate(run, proposal, entry, json.loads(proposal['arguments_json']))
                proposal['state'] = 'APPROVED'  # repeated Allow is idempotent, never a second grant
            return deepcopy(proposal)

    async def execute(self, rid, call_id, entry, arguments):
        with self._lock:
            run = self._open(rid)
            binding = self._calls.get((rid, call_id))
            if binding is None:
                raise ApprovalError('approval_required')
            pid, original = binding
            proposal = self._proposal(rid, pid)
            if proposal['state'] != 'APPROVED':
                raise ApprovalError('approval_required')
            if run['active_operation'] is not None:
                raise ApprovalError('operation_unresolved')
            if original != entry:
                self._end(run, 'FAILED', 'approval_mismatch')
                raise ApprovalError('approval_mismatch')
            self._validate(run, proposal, entry, arguments)
            # Linearization point: one lock protects decision, cancellation,
            # revision, consumption and operation admission. No await in this block.
            proposal['state'] = 'CONSUMED'
            oid = uuid4().hex
            self._operations[oid] = {'operation_id': oid, 'run_id': rid, 'state': 'RUNNING',
                                     'result': None, 'error': None}
            run['state'], run['active_operation'], run['unsettled'] = 'EXECUTING', oid, True
            task = asyncio.create_task(self.executor.execute(oid, entry.server_id, entry.original_name))
            self._tasks.add(task)
            task.add_done_callback(lambda done: self._settle(done, oid))
            wait = min(self.call_seconds, run['deadline'] - self.clock())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=max(wait, .000001))
        except (TimeoutError, asyncio.CancelledError) as exc:
            with self._lock:
                self._end(run, 'UNCERTAIN', 'caller_cancelled' if isinstance(exc, asyncio.CancelledError) else 'operation_timeout')
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ApprovalError('operation_timeout') from None
        except Exception:
            with self._lock:
                if run['state'] not in TERMINAL:
                    self._end(run, 'FAILED', 'fixture_execution_failed')
            raise ApprovalError('fixture_execution_failed') from None
        with self._lock:
            self._open(rid)  # cancellation/revision wins over late result publication
            return deepcopy(result)

    def _settle(self, task, oid):
        with self._lock:
            self._tasks.discard(task)
            operation = self._operations[oid]; run = self._runs[operation['run_id']]
            if task.cancelled():
                operation['state'] = 'UNCERTAIN'
                self._end(run, 'UNCERTAIN', 'operation_completion_unknown')
                return
            try:
                operation['result'] = task.result(); operation['state'] = 'SUCCEEDED'
            except Exception:
                operation['state'], operation['error'] = 'FAILED', 'fixture_execution_failed'
            run['active_operation'], run['unsettled'] = None, False
            if run['state'] not in TERMINAL:
                run['state'] = 'RUNNING'

    def operation(self, oid):
        with self._lock:
            if oid not in self._operations: raise ApprovalError('operation_unknown')
            return deepcopy(self._operations[oid])

    def cancel(self, rid):
        with self._lock:
            run = self._run(rid); self._refresh(run)
            if run['state'] not in TERMINAL:
                self._end(run, 'UNCERTAIN' if run['unsettled'] else 'CANCELLED', 'user_cancelled')
            return deepcopy(run)

    def fail(self, rid, code):
        with self._lock:
            run = self._run(rid)
            if run['state'] not in TERMINAL:
                self._end(run, 'UNCERTAIN' if run['unsettled'] else 'FAILED', code)

    def reset_session(self, session_id):
        with self._lock:
            if session_id not in self._sessions: raise ApprovalError('session_unknown')
            self._sessions[session_id] += 1
            for run in self._runs.values():
                if run['session_id'] == session_id: self._refresh(run)

    def finish(self, rid):
        with self._lock:
            run = self._open(rid)
            if run['unsettled'] or any(p['run_id'] == rid and p['state'] in {'PENDING', 'APPROVED'}
                                       for p in self._proposals.values()):
                raise ApprovalError('run_incomplete')
            self._end(run, 'COMPLETED', None)

    async def drain_fixtures(self):
        """Explicit test cleanup only; never retries or reconciles an UNCERTAIN run."""
        with self._lock: tasks = tuple(self._tasks)
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)

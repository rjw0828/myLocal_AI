"""C-4 isolated stdio activation. Construct live review only after user approval.

The API is trusted harness code, never an Agent tool or an HTTP approval endpoint.
There is no environment enable switch or automatic production fallback.
"""
import asyncio
from copy import deepcopy
from threading import RLock
import time
import hashlib
from pathlib import Path

from mcp_manager.manager import MCPManager
from mcp_manager.models import MCPRuntimeError, OperationState, ToolCallResult
from mcp_manager.registry import config_data, fingerprint
from .owner_runtime import OwnerFakeRuntime, OwnerFakeClient
from .readiness_gate import ReadinessDenied, ReadOnlyScope, PreparedStdioFactory, IsolationLayout, no_reparse


class LaunchReview:
    def __init__(self, configs, *, base, live=False, seconds=1200, reviewed_files=()):
        if type(live) is not bool or not 0 < seconds <= 3600 or not configs:
            raise ReadinessDenied('Explicit bounded launch review is required')
        self._live, self._deadline = live, time.monotonic() + seconds
        self.base = IsolationLayout(Path(base)).base
        self._files = {str(Path(path).resolve(strict=True)): hashlib.sha256(Path(path).read_bytes()).hexdigest()
                       for path in reviewed_files}
        self._remaining = {fingerprint(config_data(config)): 1 for config in configs}
        self._lock = RLock()

    @property
    def live(self): return self._live

    def consume_start(self, config):
        with self._lock:
            key = fingerprint(config_data(config))
            no_reparse(self.base)
            if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest for path, digest in self._files.items()):
                raise ReadinessDenied('Reviewed executable/script changed')
            if time.monotonic() >= self._deadline or self._remaining.get(key, 0) != 1:
                raise ReadinessDenied('Launch review expired, consumed or configuration changed')
            self._remaining[key] = 0


class ReviewedOwnerClient:
    def __init__(self, runtime, config, scope, review, errlog):
        self.runtime, self.config, self.scope, self.review, self.errlog = runtime, config, scope, review, errlog

    async def __aenter__(self):
        self.review.consume_start(self.config)  # launch grant is not a tool grant
        self.owner = asyncio.current_task()
        if self.review.live:
            from mcp import Client
            from mcp.client.stdio import stdio_client
            self.client = Client(stdio_client(PreparedStdioFactory().parameters(self.config), errlog=self.errlog))
        else:
            self.client = OwnerFakeClient(self.runtime, self.config)
        await self.client.__aenter__()
        self.session = self.client.session
        self.runtime.handshakes[self.config.server_id] = (
            self.client.protocol_version if self.review.live else 'memory-fixture')
        return self

    async def __aexit__(self, *args):
        if asyncio.current_task() is not self.owner: raise RuntimeError('Wrong SDK owner task')
        return await self.client.__aexit__(*args)

    async def list_tools(self, **kwargs):
        result = await self.client.list_tools(**kwargs)
        return result.model_copy(update={'tools': [t for t in result.tools if t.name in self.scope.tools]})

    async def before_dispatch(self):
        if not self.review.live: await self.client.before_dispatch()

    def validate_scope(self, name, args, input_schema=None):
        self.scope.validate(name, args, input_schema)

    def verify_admission(self, oid):
        if asyncio.current_task() is not self.owner: raise RuntimeError('Wrong call task')
        with self.runtime.lock:
            record = self.runtime.records.get(oid)
            if (not record or self.runtime.active.get(self.config.server_id) != oid
                    or not record.get('admission_verified')):
                raise ReadinessDenied('No final service admission')
            self.operation_id = oid

    def begin_attempt(self):
        with self.runtime.lock: self.runtime.attempts += 1
        if self.review.live:
            with self.runtime.lock:
                record = self.runtime.records[self.operation_id]
                # SDK invocation does not prove a physical write or server receipt.
                record['sent'], record['accepted'], record['state'] = None, None, OperationState.RUNNING

    def received(self, result):
        if self.review.live:
            with self.runtime.lock:
                record = self.runtime.records[self.operation_id]
                self.runtime.sends += 1
                record['sent'], record['accepted'], record['state'] = True, True, OperationState.SUCCEEDED
                record['result'] = ToolCallResult(tuple(p.model_dump(mode='json') for p in result.content),
                                                  deepcopy(result.structured_content), result.is_error)

    def response_lost(self):
        if self.review.live:
            with self.runtime.lock:
                self.runtime.records[self.operation_id]['state'] = OperationState.UNCERTAIN


class ReviewedFactory:
    def __init__(self, runtime, review): self.runtime, self.review, self.rules = runtime, review, {}
    def bind(self, scope):
        if type(scope) is not ReadOnlyScope or scope.server_id in self.rules:
            raise ReadinessDenied('An immutable explicit server scope is required')
        self.rules[scope.server_id] = scope
    def __call__(self, config, errlog):
        if config.server_id not in self.rules: raise ReadinessDenied('Unscoped server')
        scope = self.rules[config.server_id]
        if scope.kind == 'filesystem' and scope.root != self.review.base/'workspace':
            raise ReadinessDenied('Filesystem Root does not match the reviewed isolated workspace')
        if self.review.live:
            if scope.kind == 'time' and config.args != ('-m', 'mcp_server_time', '--local-timezone=Asia/Seoul'):
                raise ReadinessDenied('Unexpected Time launch arguments')
            if scope.kind == 'filesystem' and (len(config.args) != 4 or config.args[1:] !=
                    ('--read-only', '--confine-roots', str(scope.root))):
                raise ReadinessDenied('Read-only confined single Root launch is required')
        return ReviewedOwnerClient(self.runtime, config, scope, self.review, errlog)


class ReviewedStdioRuntime(OwnerFakeRuntime):
    """Same owner queue/operation ledger, no fake tool/result fallback in live mode."""
    def __init__(self, review, *, wait_seconds=15):
        if type(review) is not LaunchReview: raise ReadinessDenied('No explicit launch review')
        # Initialize ledger without starting an extra AsyncHost lease.
        from .fake_runtime import FakeRuntime
        from threading import Event
        FakeRuntime.__init__(self, wait_seconds=wait_seconds)
        self.review, self.active, self.arguments_hash, self.contexts, self.handshakes = review, {}, {}, [], {}
        self.attempts = 0
        self.dispatch_waiting = Event()
        self.manager = MCPManager(_client_factory=ReviewedFactory(self, review), stop_timeout=1, cleanup_timeout=10)

    def call_tool_once(self, sid, name, arguments, *, operation_id, before_send):
        if not self.review.live:
            return super().call_tool_once(sid, name, arguments, operation_id=operation_id, before_send=before_send)
        with self.lock:
            if operation_id in self.records: raise MCPRuntimeError('mcp_duplicate_operation', 'Duplicate operation')
            if len(self.records) >= 512: raise MCPRuntimeError('mcp_operation_limit', 'Retention limit')
            if any(r['server_id'] == sid and r['state'] in {OperationState.QUEUED, OperationState.RUNNING,
                   OperationState.UNCERTAIN} for r in self.records.values()):
                raise MCPRuntimeError('mcp_busy', 'Unresolved operation')
            record = {'operation_id': operation_id, 'server_id': sid, 'state': OperationState.QUEUED,
                'sent': False, 'accepted': False, 'cancel_requested': False, 'cancel_confirmed': False,
                'result': None, 'error': None}
            self.records[operation_id] = record; self.arguments_hash[operation_id] = fingerprint(arguments)

        def authorize(tool, generation):
            with self.lock:
                if record['cancel_requested']:
                    record['state'], record['cancel_confirmed'] = OperationState.CANCELLED, True
                    raise ReadinessDenied('Cancelled before admission')
                before_send(tool, generation)
                self.active[sid], record['admission_verified'] = operation_id, True
        try:
            return self.manager.call_tool_once(sid, name, arguments, operation_id=operation_id,
                                              before_send=authorize, timeout=self.wait_seconds)
        except MCPRuntimeError as error:
            with self.lock:
                if record['sent'] is False and error.code != 'mcp_wait_timeout': record['state'] = OperationState.FAILED
                record['error'] = error.code
            raise


def isolated_reviewed_service(layout, runtime, profiles):
    """Explicit test DB/profiles only; never consult operating defaults."""
    if type(layout) is not IsolationLayout or type(runtime) is not ReviewedStdioRuntime:
        raise ReadinessDenied('Explicit isolated Runtime/layout required')
    if layout.base != runtime.review.base or not profiles:
        raise ReadinessDenied('Layout/profiles do not match launch review')
    for stem in ('sessions.sqlite3', 'mcp.sqlite3'):
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = layout.base/(stem+suffix)
            if path.exists() or path.is_symlink(): no_reparse(path)
    from mcp_manager.service import MCPService
    return MCPService(session_database_path=layout.base/'sessions.sqlite3', database_path=layout.base/'mcp.sqlite3',
                      approved_configs=tuple(profiles), runtime_factory=lambda: runtime)

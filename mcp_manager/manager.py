"""Synchronous core interface for a future Flask adapter. No routes or DB I/O."""
from concurrent.futures import TimeoutError as WaitTimeout
from copy import deepcopy
import json
import math
from threading import Lock, RLock

from .async_host import AsyncHost, ServerWorker, sdk_client
from .models import MCPRuntimeError, MCPTimeoutError, ServerConfig, ServerState, ServerStatus


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


class MCPManager:
    """Explicit in-memory registrations; constructing a manager starts no server.

    stop_server/shutdown first drain an in-flight operation for stop_timeout
    seconds (default 5). Remaining work is cancelled through the owner's AnyIO
    scope, which closes the SDK transport. cleanup_timeout (default 10) bounds
    that wait. Cancellation never promises rollback of tool side effects.
    Call shutdown() explicitly, preferably in the hosting application's finally.
    """

    def __init__(self, servers=(), *, start_timeout=15.0, call_timeout=30.0,
                 stop_timeout=5.0, cleanup_timeout=10.0, _client_factory=sdk_client):
        self.start_timeout, self.call_timeout = _timeout(start_timeout), _timeout(call_timeout)
        self.stop_timeout, self.cleanup_timeout = _timeout(stop_timeout), _timeout(cleanup_timeout)
        self._configs = {}
        for config in servers:
            if not isinstance(config, ServerConfig):
                raise TypeError("servers must contain ServerConfig instances")
            if config.server_id in self._configs:
                raise ValueError("duplicate server registration")
            self._configs[config.server_id] = config
        self._workers = {}
        self._lock, self._shutdown_lock = RLock(), Lock()
        self._closing, self._closed = False, False
        self._client_factory = _client_factory
        self._host = AsyncHost.acquire()

    def _config(self, server_id):
        if server_id not in self._configs:
            raise MCPRuntimeError("mcp_not_registered", "MCP server is not registered.")
        return self._configs[server_id]

    def _require_open(self):
        if self._closing or self._closed:
            raise MCPRuntimeError("mcp_manager_closed", "MCP manager is shutting down or closed.")

    def register_server(self, config):
        """Add an explicitly approved configuration without starting it."""
        if not isinstance(config, ServerConfig):
            raise TypeError("config must be a ServerConfig")
        with self._lock:
            self._require_open()
            existing = self._configs.get(config.server_id)
            if existing is not None and existing != config:
                raise MCPRuntimeError("mcp_config_conflict", "MCP server configuration is already registered.")
            self._configs[config.server_id] = config

    def unregister_server(self, server_id):
        """Only forget a confirmed stopped connection; never imply Stop."""
        with self._lock:
            self._require_open()
            self._config(server_id)
            worker = self._workers.get(server_id)
            if worker is not None:
                if not worker.closed.done() or worker.snapshot().state != ServerState.STOPPED:
                    raise MCPRuntimeError("mcp_not_ready", "Stop the MCP server before unregistering it.")
                worker.closed.result()
                del self._workers[server_id]
            del self._configs[server_id]

    def _wait(self, worker, item, timeout):
        try:
            return deepcopy(item.future.result(timeout=timeout))
        except WaitTimeout:
            if not worker.mark_timeout(item):
                return deepcopy(item.future.result())
            raise MCPTimeoutError("mcp_wait_timeout", "The wait timed out; MCP work may still be running.",
                                  operation_id=item.operation_id) from None

    def start_server(self, server_id, *, timeout=None):
        self._host.require_sync_caller()
        wait = self.start_timeout if timeout is None else _timeout(timeout)
        with self._lock:
            self._require_open()
            config = self._config(server_id)
            worker = self._workers.get(server_id)
            if worker is not None and not worker.closed.done():
                state = worker.snapshot().state
                if state == ServerState.RUNNING:
                    return worker.snapshot()
                if state != ServerState.STARTING:
                    raise MCPRuntimeError("mcp_not_ready", "MCP server must finish or stop before reconnecting.")
            else:
                if worker is not None:
                    # A failed cleanup cannot be papered over by spawning again.
                    worker.closed.result()
                    if worker.snapshot().state == ServerState.UNCERTAIN:
                        raise MCPRuntimeError("mcp_not_ready", "Explicitly stop the uncertain MCP connection before reconnecting.")
                worker = ServerWorker(config, self._host, self._client_factory)
                self._host.claim_server(server_id, worker)
                try:
                    worker.launch()
                except BaseException:
                    self._host.release_server(server_id, worker)
                    raise
                self._workers[server_id] = worker
            ready = worker.ready
        self._wait(worker, ready, wait)
        return worker.snapshot()

    def stop_server(self, server_id, *, timeout=None):
        self._host.require_sync_caller()
        grace = self.stop_timeout if timeout is None else _timeout(timeout)
        with self._lock:
            self._config(server_id)
            worker = self._workers.get(server_id)
            if worker is None or self._closed:
                return self.get_status(server_id)
            worker.request_stop()
        try:
            worker.closed.result(timeout=grace)
        except WaitTimeout:
            worker.cancel_owner()
            try:
                worker.closed.result(timeout=self.cleanup_timeout)
            except WaitTimeout:
                raise MCPRuntimeError("mcp_shutdown_incomplete", "MCP cleanup is still running; reconnect is blocked.") from None
        return worker.snapshot()

    def get_status(self, server_id):
        with self._lock:
            self._config(server_id)
            worker = self._workers.get(server_id)
            return worker.snapshot() if worker else ServerStatus(server_id, ServerState.STOPPED)

    def get_operation(self, server_id, operation_id):
        with self._lock:
            self._config(server_id)
            worker = self._workers.get(server_id)
            if worker is None:
                raise MCPRuntimeError("mcp_operation_not_found", "MCP server has no operation history.")
            return worker.operation(operation_id)

    def connection_generation(self, server_id):
        """Opaque identity of this connection attempt, including reconnects."""
        with self._lock:
            self._config(server_id)
            worker = self._workers.get(server_id)
            return worker.ready.operation_id if worker else None

    def connection_info(self, server_id):
        from copy import deepcopy
        with self._lock:
            self._config(server_id)
            worker = self._workers.get(server_id)
            return deepcopy(worker.connection_info) if worker else None

    def _request(self, server_id, kind, payload, timeout):
        self._host.require_sync_caller()
        wait = self.call_timeout if timeout is None else _timeout(timeout)
        with self._lock:
            self._require_open()
            self._config(server_id)
            worker = self._workers.get(server_id)
            if worker is None:
                raise MCPRuntimeError("mcp_not_ready", "MCP server is stopped.")
            item = worker.enqueue(kind, payload)
        return self._wait(worker, item, wait)

    def list_tools(self, server_id, *, timeout=None, refresh=False):
        if not isinstance(refresh, bool):
            raise ValueError("refresh must be a boolean")
        return self._request(server_id, "tools", refresh, timeout)

    def call_tool(self, server_id, name, arguments=None, *, timeout=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ValueError("tool name must be a nonempty string of at most 256 characters")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        try:
            # Snapshot caller-owned inputs before another thread can mutate them.
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise ValueError("tool arguments exceed 64 KiB")
            arguments = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("arguments must be a finite JSON object of at most 64 KiB") from None
        return self._request(server_id, "call", (name, arguments), timeout)

    def shutdown(self):
        self._host.require_sync_caller()
        # Multiple shutdown callers share a single shutdown, while concurrent
        # operations are denied before taking snapshots of registered workers.
        with self._shutdown_lock:
            with self._lock:
                if self._closed:
                    return
                self._closing = True
                workers = list(self._workers)
                for server_id in workers:
                    self._workers[server_id].request_stop()
            failures = []
            for server_id in workers:
                try:
                    self.stop_server(server_id)
                except MCPRuntimeError as error:
                    failures.append(error)
            if failures:
                # Keep the host alive for cleanup and permit an explicit retry.
                raise failures[0]
            self._host.release()
            with self._lock:
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.shutdown()

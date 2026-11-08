"""Process-wide event loop and per-server Client context owners.

No application, database, model, or web UI is imported here. Each Client's
context is entered and exited by the SAME long-lived task. Request coroutines
only put commands on that task's queue; they never own its AnyIO cancel scopes.
"""
import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import os
from threading import Event, Lock, RLock, Thread, current_thread
import time
from uuid import uuid4

import anyio

from .models import (
    MCPRuntimeError, OperationOutcome, OperationState, ServerConfig, ServerState,
    ServerStatus, ToolCallResult, ToolInfo,
)


def sdk_client(config, errlog):
    """Use the SDK's owned stdio transport, with child stderr discarded.

    In particular, never inherit the console for untrusted child tracebacks or
    switch a global logger off. The SDK supplies its minimal environment.
    """
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    transport = stdio_client(StdioServerParameters(
        command=config.executable, args=list(config.args), cwd=config.cwd,
    ), errlog=errlog)
    # Caller timeouts are managed below, not by discarding a live SDK request.
    return Client(transport)


class AsyncHost:
    """Reference-counted, process-wide loop. Managers must release their lease."""

    _lifecycle_lock = Lock()
    _shared = None

    @classmethod
    def acquire(cls):
        with cls._lifecycle_lock:
            if cls._shared is None:
                host = cls()
                host._start()
                cls._shared = host
            host = cls._shared
            if host._closing:
                raise MCPRuntimeError("mcp_host_closing", "MCP runtime is shutting down.")
            host._leases += 1
            return host

    def __init__(self):
        self._leases = 0
        self._closing = False
        self._ready = Event()
        self._loop = None
        self._thread = None
        self._boot_error = False
        self._owners_lock = Lock()
        self._owners = {}

    def _start(self):
        self._thread = Thread(target=self._run, name="local-ai-mcp-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self._boot_error:
            self._closing = True
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
            raise MCPRuntimeError("mcp_host_start_failed", "MCP event loop could not start.")

    def _run(self):
        try:
            self._loop = (asyncio.ProactorEventLoop() if os.name == "nt"
                          else asyncio.new_event_loop())
            asyncio.set_event_loop(self._loop)
            self._loop.set_exception_handler(
                lambda _loop, _context: logging.getLogger(__name__).error("MCP event-loop task failed.")
            )
        except BaseException:
            self._boot_error = True
            self._ready.set()
            return
        self._ready.set()
        try:
            if not self._closing:
                self._loop.run_forever()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
        finally:
            self._loop.close()

    def submit(self, coroutine):
        if current_thread() is self._thread:
            coroutine.close()
            raise MCPRuntimeError("mcp_sync_on_loop", "Synchronous MCP calls cannot run on the MCP loop thread.")
        if self._closing or self._loop is None or not self._thread.is_alive():
            coroutine.close()
            raise MCPRuntimeError("mcp_host_closed", "MCP event loop is not running.")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def require_sync_caller(self):
        if current_thread() is self._thread:
            raise MCPRuntimeError("mcp_sync_on_loop", "Synchronous MCP calls cannot run on the MCP loop thread.")

    def claim_server(self, server_id, owner):
        with self._owners_lock:
            if server_id in self._owners:
                raise MCPRuntimeError("mcp_server_owned", "Another MCP manager already owns this server ID.")
            self._owners[server_id] = owner

    def release_server(self, server_id, owner):
        with self._owners_lock:
            if self._owners.get(server_id) is owner:
                del self._owners[server_id]

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._closing

    @property
    def loop_type(self):
        return type(self._loop).__name__

    def release(self):
        with type(self)._lifecycle_lock:
            if self._leases <= 0:
                if self._closing and self._thread.is_alive():
                    self._thread.join(10)
                    if self._thread.is_alive():
                        raise MCPRuntimeError("mcp_host_shutdown_failed", "MCP event loop did not finish shutting down.")
                if type(self)._shared is self:
                    type(self)._shared = None
                return
            self._leases -= 1
            if self._leases:
                return
            self._closing = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(10)
            if self._thread.is_alive():
                raise MCPRuntimeError("mcp_host_shutdown_failed", "MCP event loop did not finish shutting down.")
            type(self)._shared = None


@dataclass
class _Command:
    kind: str
    payload: object = None
    operation_id: str = field(default_factory=lambda: uuid4().hex)
    future: Future = field(default_factory=Future)
    state: OperationState = OperationState.QUEUED
    timed_out: bool = False
    started_at: float | None = None
    outcome: OperationOutcome | None = None


class ServerWorker:
    """One owner task, one context, one serial queue, no process-name kills."""

    def __init__(self, config: ServerConfig, host: AsyncHost, client_factory=sdk_client):
        self.config, self.host, self.client_factory = config, host, client_factory
        self.lock = RLock()
        self.state = ServerState.STARTING
        self.error_code = None
        self.ready = _Command("start", state=OperationState.RUNNING, started_at=time.perf_counter())
        self.closed = Future()
        self._records = OrderedDict([(self.ready.operation_id, self.ready)])
        self._active = self.ready
        self._last = None
        self._queue = None
        self._scope = None
        self._stop_requested = False
        self._task = None

    def launch(self):
        self._task = self.host.submit(self.run())

    def snapshot(self):
        with self.lock:
            return ServerStatus(
                self.config.server_id, self.state,
                self._active.operation_id if self._active else None,
                sum(not item.future.done() and item is not self._active for item in self._records.values()),
                deepcopy(self._last), self.error_code,
            )

    def operation(self, operation_id):
        with self.lock:
            item = self._records.get(operation_id)
            if item is None:
                raise MCPRuntimeError("mcp_operation_not_found", "MCP operation is no longer retained.")
            return deepcopy(item.outcome or OperationOutcome(item.operation_id, item.kind, item.state))

    def mark_timeout(self, item):
        with self.lock:
            if item.future.done():
                return False
            item.timed_out = True
            if self.state not in {ServerState.STOPPING, ServerState.STOPPED}:
                self.state = ServerState.UNCERTAIN
                self.error_code = "mcp_wait_timeout"
            return True

    def enqueue(self, kind, payload=None):
        with self.lock:
            if self.state != ServerState.RUNNING or self._stop_requested:
                raise MCPRuntimeError("mcp_not_ready", "MCP server is not ready for another operation.")
            if sum(not item.future.done() for item in self._records.values()) >= 32:
                raise MCPRuntimeError("mcp_busy", "MCP server command queue is full.")
            item = _Command(kind, payload)
            self._records[item.operation_id] = item
            try:
                self.host.submit(self._put(item))
            except MCPRuntimeError:
                self._finish(item, error="mcp_host_closed")
                raise
            return item

    async def _put(self, item):
        # A close can race this queue admission. Finally in run() resolves all
        # outstanding records, including records whose queue put arrived late.
        if not item.future.done() and not self.closed.done():
            self._queue.put_nowait(item)

    def request_stop(self):
        with self.lock:
            if self.closed.done():
                if self.closed.exception() is None:
                    self.state, self.error_code = ServerState.STOPPED, None
                    self._stop_requested = True
                    self.host.release_server(self.config.server_id, self)
                return
            self._stop_requested = True
            self.state = ServerState.STOPPING
        self.host.submit(self._signal_stop())

    async def _signal_stop(self):
        if self._queue is not None:
            self._queue.put_nowait(None)

    def cancel_owner(self):
        # Native Task.cancel() can defeat AnyIO's shielded stdio cleanup.
        # Cancel the OWNER's AnyIO scope on its own loop instead.
        self.host.submit(self._cancel_scope())

    async def _cancel_scope(self):
        if self._scope is not None:
            self._scope.cancel()

    def _finish(self, item, result=None, error=None, cancelled=False):
        with self.lock:
            if item.future.done():
                return
            item.state = (OperationState.CANCELLED if cancelled else
                          OperationState.UNCERTAIN if error == "mcp_call_uncertain" else
                          OperationState.FAILED if error else OperationState.SUCCEEDED)
            elapsed = (time.perf_counter() - item.started_at if item.started_at is not None else None)
            item.outcome = OperationOutcome(item.operation_id, item.kind, item.state,
                                            result, error, elapsed)
            self._last = item.outcome
            if self._active is item:
                self._active = None
            if error:
                item.future.set_exception(MCPRuntimeError(error, "MCP operation did not complete successfully.",
                                                         operation_id=item.operation_id))
            else:
                item.future.set_result(result)
            # Bound retained results while retaining EVERY unfinished operation.
            completed = [key for key, value in self._records.items() if value.future.done()]
            for key in completed[:-16]:
                del self._records[key]

    async def run(self):
        entered, failure, cleanup_failure = False, None, False
        self._queue = asyncio.Queue()
        try:
            with anyio.CancelScope() as scope:
                self._scope = scope
                if self._stop_requested:
                    scope.cancel()
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with self.client_factory(self.config, errlog) as client:
                        entered = True
                        with self.lock:
                            if not self._stop_requested:
                                self.state, self.error_code = ServerState.RUNNING, None
                                self._finish(self.ready)
                        while not self._stop_requested:
                            item = await self._queue.get()
                            if item is None or self._stop_requested:
                                break
                            with self.lock:
                                if item.timed_out:
                                    self._finish(item, error="mcp_queued_timeout", cancelled=True)
                                    if self.state == ServerState.UNCERTAIN:
                                        self.state, self.error_code = ServerState.RUNNING, None
                                    continue
                                item.state = OperationState.RUNNING
                                item.started_at = time.perf_counter()
                                self._active = item
                            try:
                                if item.kind == "tools":
                                    listed = await client.list_tools()
                                    result = tuple(ToolInfo(t.name, t.description, deepcopy(t.input_schema))
                                                   for t in listed.tools)
                                else:
                                    name, arguments = item.payload
                                    called = await client.call_tool(name, arguments)
                                    result = ToolCallResult(
                                        tuple(part.model_dump(mode="json") for part in called.content),
                                        deepcopy(called.structured_content), called.is_error,
                                    )
                            except Exception:
                                # A lost call response cannot prove whether the
                                # tool executed. Closing transport is not rollback.
                                failure = ("mcp_call_uncertain" if item.kind == "call"
                                           else "mcp_connection_failed")
                                with self.lock:
                                    self.state = (ServerState.UNCERTAIN if item.kind == "call"
                                                  else ServerState.ERROR)
                                    self.error_code = failure
                                    self._finish(item, error=failure)
                                break
                            with self.lock:
                                if not self._stop_requested:
                                    self.state, self.error_code = ServerState.RUNNING, None
                                self._finish(item, result)
                # Every transport context is exited here by its owning task,
                # including when the outer AnyIO scope was cancelled.
        except BaseException:
            failure = "mcp_cleanup_failed" if entered else "mcp_start_failed"
            cleanup_failure = entered
        finally:
            with self.lock:
                self._scope = None
                for item in list(self._records.values()):
                    if not item.future.done():
                        failed = failure if item.state == OperationState.RUNNING else None
                        self._finish(item, error=failed or "mcp_stopped", cancelled=not failed)
                self._active = None
                if cleanup_failure:
                    self.state, self.error_code = ServerState.UNCERTAIN, failure
                    self.closed.set_exception(MCPRuntimeError(failure, "MCP transport cleanup could not be confirmed."))
                else:
                    uncertain = failure == "mcp_call_uncertain" and not self._stop_requested
                    self.state = (ServerState.STOPPED if self._stop_requested else
                                  ServerState.UNCERTAIN if uncertain else ServerState.ERROR)
                    self.error_code = failure
                    if not uncertain:
                        self.host.release_server(self.config.server_id, self)
                    self.closed.set_result(None)

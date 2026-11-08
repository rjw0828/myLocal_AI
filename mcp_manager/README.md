# MCP-1 Core Runtime

Optional Python core using the official `mcp==2.3.0` SDK. See [MCP-2 API](API.md)
for the independent Registry and Flask adapter now used by `create_app`.
The standalone core does not register
Flask routes, open SQLite, start Ollama, load speech models, or change the console.
Importing the package starts no thread or process. MCP-0 evidence remains in
`%TEMP%\LocalAI_MCP0_20261109`.

## Install and use

The application previously documented its packages in the root README without a
requirements file. `requirements-mcp.txt` adds only this optional SDK requirement:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv\Scripts\python.exe -m pip check
```

Only trusted Python code supplies in-memory `ServerConfig` registrations.
Registration approves the executable **and all its arguments**. This is not a
sandbox for untrusted programs. Use preinstalled programs and absolute paths;
shell launchers, PATH lookup, package installation, and command interpolation are
unsupported. No server starts until `start_server(server_id)` is called.

```python
from mcp_manager import MCPManager, ServerConfig

config = ServerConfig(
    server_id="approved-local-server",
    executable=r"F:\path\to\approved\python.exe",
    args=(r"F:\path\to\approved\server.py",),
    cwd=r"F:\path\to\approved",
)
with MCPManager([config]) as manager:
    manager.start_server(config.server_id)
    tools = manager.list_tools(config.server_id)
    # A future UI must obtain user approval before choosing a tool/arguments.
    result = manager.call_tool(config.server_id, "echo", {"text": "hello"})
    # result.is_error is the SDK tool error flag, separate from transport failure.
    manager.stop_server(config.server_id)
```

## Lifecycle and concurrency

- All live managers share one reference-counted loop thread in the process.
  Windows explicitly uses `ProactorEventLoop`, without changing global policy.
- Each server has exactly one long-lived owner task and one SDK Client context
  for a server ID. That task enters/exits the context and serializes commands.
  Registrations are manager-local; the shared host rejects a second manager
  starting an already-owned server ID, including when cleanup is uncertain.
  Distinct IDs are distinct identities: callers must not register the same
  actual server under different IDs as a way to bypass this protection.
- Synchronous calls use `run_coroutine_threadsafe`. Do not call the synchronous
  manager API from the MCP loop thread.
- Duplicate start while STARTING waits for the same initialization; a RUNNING
  start is idempotent. Stop is idempotent. Different servers run independently.
- States are STOPPED, STARTING, RUNNING, STOPPING, ERROR, UNCERTAIN. RUNNING is
  last-known connection state, not continuous health polling. An idle crashed
  process is detected on the next SDK operation.
- Child stderr is discarded through a per-transport null-device handle. Runtime
  exceptions expose fixed error codes/messages, not child paths or tracebacks.
  Inputs, outputs and exception strings are not written by this core to logs.

## Timeouts, results, and shutdown

Start wait defaults to 15 seconds; tool/list wait defaults to 30 seconds. A
`MCPTimeoutError` includes `operation_id`. The caller timing out never cancels an
already running tool. The connection becomes UNCERTAIN and rejects new calls
and starts until the owner observes completion, or the caller explicitly stops.

`get_operation(server_id, operation_id)` retrieves the late operation's state,
result, error code and execution time. On a known late completion the state
returns to RUNNING. A timed-out command still QUEUED is skipped without executing
it. If a call's response is lost, both operation and connection remain UNCERTAIN
even after the transport closes; an explicit stop is required before reconnect.
This preserves the fact that the tool's execution/side effects are unknown.
Results are retained for the latest 16 completed operations per connection;
unfinished operations are always retained. The queue allows 32 outstanding
operations. Tool argument snapshots must be finite JSON objects up to 64 KiB.
Server output size is not bounded before SDK decoding in this version.

Call `shutdown()` explicitly in the hosting application's `finally`, or use the
context manager shown above. The MCP-2 Flask entry point also calls this cleanup;
standalone/custom hosts retain the explicit lifecycle responsibility.
Shutdown denies new work and requests all server owners to stop. It waits up to
`stop_timeout` (default 5 seconds) per server for an in-flight operation. Queued
work is rejected. It then cancels the owner's **AnyIO scope**, not its native
asyncio Task, so the SDK's shielded subprocess cleanup can finish. A separate
`cleanup_timeout` (default 10 seconds) bounds this wait. The loop ends only after
all owners confirm closure and the last manager releases its lease.

Cancellation, a closed transport, or killing a process cannot roll back a tool's
side effects. A cleanup error/incomplete cleanup keeps reconnect blocked and
raises an explicit error. An incomplete wait can be retried once cleanup finishes;
a failed SDK context cleanup remains blocked and requires diagnosis. Do not start another runtime
or broadly kill Python processes to conceal a cleanup failure. After a connection
error, explicitly stop (wait for cleanup) before starting it again.

## Validation

```powershell
# Fakes: no subprocesses or external services
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_mcp_runtime.py -v
# Real SDK and Windows stdio: local echo/add fixture only
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_mcp_stdio.py -v
# Existing + new Python tests; all DB tests use isolated temporary DBs
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests/test_voice_turn.js
node tests/test_voice_ui.js
```

The Windows tests create token-marked PID/event files only in TemporaryDirectory.
They retain handles to verified fixture processes, check normal/forced exit and
venv-launcher exit, and never terminate another process by image name. The
fixture provides only echo/add. Mock tests cover deterministic concurrency;
stdio tests exercise the actual SDK, Unicode output, delayed completion, forced
termination, restart and shutdown during calls. They do not test Edge, a real
third-party MCP server, Streamable HTTP, or LLM tool calling.

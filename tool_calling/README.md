# MCP-5B Tool Chat Core

MCP-5B supplied the Python-only foundation described below. MCP-5C now connects
it through separate Flask job APIs, Session hooks and an Ollama loop; see
[Backend API](API.md). UI and Voice integration remain unimplemented.
Importing the core still creates no database, thread or subprocess.

## Interfaces and ownership

`ToolChatService(mcp, bindings, session_revision=..., reservations=...)` uses an
existing `MCPService`. Bindings are trusted operator configuration, keyed by
actual server ID: `(time, None)` or `(filesystem, FilesystemPolicy)`. Display
names and LLM output cannot create bindings. `session_revision` must return the
current server revision, or None for a missing/deleted session.

- `create`: reserve a session and freeze its revision/browser generation/catalog.
- `begin_ollama_request` / `end_ollama_request`: inference admission/completion
  tracking only; no inference is implemented. Unknown completion retains an
  unsettled reservation. A UI abort is not confirmed server completion.
- `enqueue`: accept parsed `{name: alias, arguments: object}` calls. MCP-5C must
  parse model messages before this boundary.
- `drain`: single runner, sequential calls, stopping at an unapproved scope.
  Returns only newly completed results. MCP-5C must retain earlier returned
  results across approval waits; a later failure never means rollback.
- `snapshot`: status and current approval proposal (no token).
- `approve`: trusted user-action handler with proposal ID/browser generation,
  explicit boolean decision, and optional `FileScope`. Never expose as an LLM
  tool. No HTTP/user authentication boundary is implemented in this phase.
- `finish`, `cancel`, `invalidate_session`, `invalidate_all`, `shutdown`:
  terminal states cannot resume; grants are revoked before further dispatch.
- `reconcile`: release unsettled MCP work only after confirmed terminal operation
  status. Never replays a call or publishes a cancelled job's late result.
- `acknowledge_stopped`: explicit operator recovery after MCP confirms STOPPED.
  Does not stop/restart a server or claim to undo side effects.

The shared `SessionReservations.ordinary(session_id)` context is now wired into
ordinary Chat in MCP-5C. Reset/Delete/Delete All invoke the cancellation hooks
and honor unsettled reservations. Session revision checks and final commit
use the same coordinator while retaining the ordinary Chat success contract.
Browser navigation still requires an explicit Cancel request from MCP-5D;
the server cannot infer which session a browser is displaying.

## Approval and execution

Each grant binds job/session/revision, server/config/connection, permission
generation, tool/schema and optional filesystem scope. Registry Allow alone
never executes a call. Every call revalidates policy, gets a fresh existing
120-second one-use token and invokes the unchanged MCP execution path. Unused
tokens are revoked. Server-side `before_execute` rechecks job/policy/path under
the MCP server action gate, immediately before execution admission. Cancel before
this admission prevents dispatch; after admission the operation is tracked as
in flight, not assumed rolled back. No job lock spans MCP transport I/O.

New `MCPManager.connection_generation` is the start operation's unique ID.
`MCPService.inspect_tool` returns internal-only metadata; public `/mcp/*`
responses do not gain executable paths or configuration. Permission generation
also detects Allow -> Block -> Allow during the same process. There are no new
HTTP routes, changes to manual approvals or session/MCP schemas.

## Filesystem restrictions

Only `read_file` / `list_directory` are supported. A trusted adapter must first
verify the running server's ACTUAL roots and construct `FilesystemPolicy` with
`verified=True` and `server_identity=(server_id, config_hash,
connection_generation)`. This flag is an operator assertion, NOT verification
performed by this library. Do not populate it from a browser, model, arbitrary
CLI-argument guessing or cached data from an earlier connection. The core also
requires `--read-only` in the approved config. Roots negotiation can change a
server's actual policy; a changed/unverifiable policy must invalidate jobs. An
automatic attestation adapter was not implemented in MCP-5B. MCP-5C adds the
strict, implementation-pinned static-CLI adapter documented in `API.md`.

Paths must exist and be absolute Windows drive paths. UNC/device paths, ADS,
parent traversal, reserved device names and all symlink/junction/reparse-point
paths (including ancestors) fail closed. Checks use resolved path components
and root/scope identity, not textual prefixes. Revalidate before admission.

File, directory and nonrecursive scopes can be represented and checked, but
actual dispatch currently ONLY accepts a recursive approval matching the
server's sole verified root. Narrower approvals and multi-root servers are
rejected (`filesystem_scope_not_server_enforced`), not silently widened. A
scoped server connection or server-enforced per-call scope needs a separate
design before enabling narrow approvals. No existing server roots are changed.

TOCTOU is NOT eliminated: a different local process may change paths after
preflight. The underlying server's path checks remain necessary; this is not
an OS sandbox or a proof of atomic open/containment. Real Windows junction
escape rejection is tested; symlink creation requires OS permission.

## Limits and lifecycle

`Limits` centralizes defaults: 8 tool calls, 10 model requests, 300 seconds active
processing, 120 seconds per approval wait, 900 seconds total lifetime, 128 retained
jobs, 64 KiB per tool result. One tool per job is enforced by a nonblocking runner
lock. MCP call timeout remains owned by the existing Runtime (30 seconds by
default); caller expiry never cancels or retries the transport implicitly.

Every entry checks expiry and revision. `sweep()` supports scheduled expiry;
MCP-5C must schedule it since this module starts no timer threads. Cancelled
in-flight and UNCERTAIN jobs retain the session reservation until actual
completion is known. Unknown Ollama completion cannot be resolved by stopping
an unrelated MCP server. No automatic resynchronization is provided.

Tests: `tests/test_tool_calling.py` (unit/fake MCP, real temporary Windows paths,
temporary SQLite with real MCPService/Runtime and fake SDK clients). These do
not invoke Qwen or installed Time/Filesystem servers. MCP quick/integration use
the existing `scripts/test.ps1`. Full regression belongs to MCP-5E.

Validation on 2026-11-12: 48 new tests (47 passed, 1 skipped: Windows symlink
creation privilege 1314). Final MCP quick: 192 collected, 191 passed/1 skipped,
6.21 s. Final MCP integration: 208 collected, 207 passed/1 skipped, 32.86 s.
The latter also includes existing actual SDK echo/add stdio fixtures. Earlier
in development, scoped integration including app/sessions/storage/ui collected
395 tests with one skip and no failures (48.98 s); final additions were then
rechecked in the MCP modes. Full and Edge were intentionally not run. Windows
junctions were actually created in temporary paths and the escape was rejected.
No new real Qwen/Time/Filesystem invocation was performed in MCP-5B; MCP-5A's
live model evidence is separate. Operating DBs and approvals were not modified.

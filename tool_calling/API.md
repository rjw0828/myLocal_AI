# MCP-5C Backend API

MCP-5C connects the existing core to Flask, SQLite sessions and Ollama. Chat UI
and Voice have not been connected to these endpoints (MCP-5D). Ordinary
`POST /chat` retains its successful JSON contract and uses no tools.

## Access and lifecycle

Base URL: `http://127.0.0.1:5000/tool-chat`. All requests require
`X-Local-AI-Control: web-console`, a loopback peer/Host, and same-origin checks
identical to `/mcp/*`. JSON responses are no-store/nosniff. No CORS is added.
The existing general 1 MiB request limit applies.

Each job belongs to an EXISTING session; job APIs never create sessions. Start
only user-approved MCP servers using the existing Manager. These APIs never
start, install or grant Registry Allow to a server/tool automatically.

| Method/path | Request | Response |
| --- | --- | --- |
| POST `/jobs` | `{session_id,message,browser_generation,response_language?}` | 202 `{job:...}` |
| GET `/jobs/<id>` | none | 200 `{job:...}` including status, pending approval, final result |
| GET `/jobs/<id>/approval` | none | 200 `{job_id,state,approval}`; read-only |
| POST `/jobs/<id>/approval` | `{proposal_id,browser_generation,allow,scope?}` | 202 `{job:...}` |
| POST `/jobs/<id>/cancel` | `{}` | 200 `{job:...}` |
| POST `/jobs/<id>/reconcile` | `{}` | 200 only after confirmed terminal MCP operation; otherwise 409 |

`session_id` is a nonblank string up to 128 Python characters, `message` up to
10,000. `browser_generation` is a nonnegative integer, not a boolean.
`response_language`, when present, must be `ko` or `en`; language instructions
are reinforced in the current prompt, while the original question is persisted.
The browser generation is a stale-response guard, NOT authentication.

Public states are RUNNING, WAITING_APPROVAL, COMPLETED, FAILED, CANCELLED,
UNCERTAIN. `phase` preserves the core's CREATED/EXECUTING_TOOL detail.
`result` is only present for COMPLETED, otherwise null. It contains
`session_id, model, message, metrics, elapsed_seconds, revision`. Metrics refer
to the FINAL Ollama response, not a sum of multiple requests. Elapsed time is
job wall time including approval waits. Intermediate tool messages are neither
saved to Session History nor passed to TTS. Result/approval data stays in RAM;
successful original user/final assistant messages are committed together.

Example approval proposal:

```json
{
  "proposal_id": "opaque-id",
  "server_id": "mcp-id",
  "server_name": "Filesystem",
  "tool_name": "read_file",
  "description": "tool description",
  "arguments": {"path":"F:\\MCP_Workspace\\note.txt"},
  "turn_scoped": true,
  "scope": {"path":"F:\\MCP_Workspace","kind":"directory","recursive":true},
  "notice": "Only this response. Filesystem approval covers the entire displayed root, including subdirectories."
}
```

Review the server, tool, requested path AND full-root scope before approving.
For Filesystem approval the client must explicitly send that full-root `scope`.
Missing, file-only, subdirectory-only, nonrecursive or expanded scopes fail
closed. Time approvals have no filesystem scope. `allow:false` denies the job.
Approvals never affect another job. Manual MCP approvals remain one-use/120 s.
An additional tool requires additional approval; arguments must always satisfy
the current schema. Models cannot approve themselves.

## Session and cancellation ordering

- Snapshot history/revision under a short Session lock and preparation lease.
  Discovery/inference/user approval do not hold that lock or a DB transaction.
- Normal Chat holds an ordinary lease; Tool Chat holds a job lease. Conflicts
  return 409 `session_reserved` without changing History.
- Reset/Delete/Delete All first cancel associated jobs. If server work remains
  in flight or UNCERTAIN, return 409 `session_unsettled`, leaving DB data intact.
  After known completion the operator may explicitly retry the mutation.
- Revision-checked final commit is serialized with cancellation. The commit
  either wins before cancellation or is prevented; partial turns are not saved.
- Session navigation is not a server event. MCP-5D must POST Cancel before
  changing sessions, discard old-generation responses, and never show old
  approvals in the new session. The current UI is intentionally unchanged.
- Abort/Timeout is not rollback. Confirmed late MCP completion can release the
  lease through explicit Reconcile, without replay or saving cancelled output.
  Unknown Ollama completion has no automatic recovery; elapsed time, Health,
  Reset and stopping an unrelated MCP server cannot clear it. Use another
  session pending a verified recovery policy.

## Filesystem Root attestation

Only the locally reviewed `@modelcontextprotocol/server-filesystem` 2.2.0 build
is accepted. Aggregate SHA-256 of package.json and sorted dist JS files:
`5ab8e1b2e1fefd6f0549fc21073b2533e6ad5af7f9cfceb03982a1dc9883c160`.
This is a reviewed implementation pin, not an upstream signature or sandbox.

Supported launch shape: absolute Node executable, absolute `dist/index.js`,
`--read-only`, exactly one absolute existing root. Unknown flags/multiple roots
are not attested. The Runtime records the artifact/root identity before and
after handshake, official stdio ownership, disabled Roots callback and actual
negotiated protocol. The approved Registry config must match. This reviewed
server keeps command-line roots when the client offers no Roots capability.
The adapter derives the root from this pinned static policy, not an LLM path,
display name or unapproved metadata-tool invocation.

Each execution rechecks the source/root snapshot, config and connection
generation. Reconnect requires new attestation and approval. Changing the
package files or replacing the root fails closed. Package updates require
review and an explicit pin update; no automatic trust/download is implemented.
Existing reparse/UNC/device/ADS/traversal restrictions remain in force.

TOCTOU and same-account executable/dependency tampering are not eliminated.
The server's own path restriction remains necessary; the adapter does not claim
atomic filesystem containment. Only full, sole-root recursive grants execute.

## Resources, shutdown and testing

At most 8 jobs are admitted for preparation/active processing; four worker
threads execute them. Workers return to the pool while waiting for approval.
Job creation returns its ID before MCP Discovery, so a slow discovery can be
cancelled using that ID. Discovery also consumes the processing budget.
The expiry timer starts lazily and checks every 0.5 s. Core limits remain
8 tool calls / 10 Ollama requests / 300 s processing / 120 s per approval wait /
900 s overall life. Discovery has a processing deadline. Ollama keeps
`stream:false`, `think:false`, an individual maximum of 180 s (shortened by
the remaining budget) and a 1 MiB Tool Chat response-body cap. The ordinary
Chat request/response size behavior is unchanged.

The core bounds retained jobs at 128; completed results are memory-only.
Cancelled/failed job prompts and intermediate data are released when the worker
settles. Existing unbounded Session histories and upstream MCP response
allocation are not solved by this change. Tool-result admission remains 64 KiB.

`app.py` shuts down Tool Chat before MCP. Custom WSGI hosts must do the same:
`app.extensions['tool_chat'].shutdown()` then
`app.extensions['mcp_service'].shutdown()`. Shutdown cancels queued work and
revokes grants. It does not pretend an in-flight HTTP/model operation stopped;
worker completion may still wait for its existing timeout.
Run a single Controller process. In-memory reservations and MCP ownership are
not distributed locks; multiple independent WSGI workers are not supported.

Tests use Fake Ollama/SDK with temporary DBs and fixture files. Core tests,
manual `/mcp` compatibility and existing actual SDK fixtures remain selected by
the runner. Full and Edge are reserved for subsequent stages.

```powershell
.\scripts\test.ps1 -Mode quick -Module app,mcp,sessions
.\scripts\test.ps1 -Mode integration -Module app,mcp,sessions,storage
```

Validation on 2026-11-13:

- New Backend/transport/root-adapter tests: 34 passed (Fake Ollama/SDK,
  real MCPService/Runtime, temporary SQLite and synthetic fixture files).
- Final quick app,mcp,sessions: 306 collected, 305 passed/1 skipped, 23.34 s.
- Final integration app,mcp,sessions,storage: 356 collected,
  355 passed/1 skipped, 52.61 s. Includes existing actual SDK stdio fixtures.
- The skip is the existing Windows symlink creation privilege restriction;
  actual junction checks pass. Python syntax and git diff --check pass.
- Separate actual Filesystem 2.2.0/official SDK smoke check negotiated
  2026-07-28, attested a temporary sole root, confirmed SERVER rejection outside
  that root, and completed approval -> real fixture read -> final answer ->
  two-message History commit. Only Ollama was mocked in this smoke check.
  The owned test Runtime was shut down and no matching Node child remained.
- No live Qwen request was made in MCP-5C. MCP-5A's live model verification
  remains prior evidence, not a claim of live end-to-end Backend validation.
- Operating Session DB, MCP DB and approval manifest hashes were identical
  before and after final regression. No operating data was used as test fixtures.
- Full, Edge and physical Voice verification were intentionally not performed.

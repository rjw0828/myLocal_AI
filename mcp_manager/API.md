# MCP-2 Registry + Flask API

The backend uses the MCP-1 core and the already installed `mcp==2.3.0`. It adds no
packages, UI controls, external server installers, or automatic LLM tool calls.
For a new environment, install the root `requirements-mcp.txt` alongside the
existing Flask/voice packages before running this branch's `app.py`.

## Independent storage and execution approval

- MCP DB: `F:\LLM\AI_Project\myLocalAI\data\mcp_registry.sqlite3` by default.
  Override with `LOCAL_AI_MCP_DB_PATH` or `create_app(mcp_database_path=...)`.
- Session DB: `data/local_ai_lab.sqlite3`, unchanged. No Session migration is
  performed. The MCP registry rejects the Session path, sidecars and hardlinks,
  and rejects databases with unrelated tables/application identifiers.
- MCP schema v1 stores `mcp_servers` and `mcp_tool_permissions`. Connections are
  short, foreign keys enabled, WAL, FULL synchronous, 3-second busy timeout.
  Configurations, registrations and grants are transactional. Failure never
  silently switches to RAM storage.
- Execution state, runtime operations and one-use approvals stay in RAM. Registry
  access/registration never starts a server. Restart restores saved registrations
  and grants with every server STOPPED. It never restores tokens or auto-starts.
- One registration per profile/exact execution config prevents accidental aliases.
  Server IDs are generated UUIDs and HTTP callers cannot choose or recreate IDs.
  Delete cascades only that server's grants. Delete requires explicit Stop first.

Only the PC owner can approve execution configurations. There are no approved
profiles by default. To enable a preinstalled, reviewed local stdio server, the
owner creates a UTF-8 file such as `data/mcp_approved_profiles.json`:

```json
{
  "profiles": [
    {
      "profile_id": "reviewed-local-server",
      "executable": "F:\\approved\\venv\\Scripts\\python.exe",
      "args": ["F:\\approved\\server.py"],
      "cwd": "F:\\approved"
    }
  ]
}
```

These are examples; no such executable, profile file or third-party server is
installed by the application. Use actual existing absolute paths. Executable
and all fixed arguments are approved together. Shell launchers and shell command
strings are rejected by the core. Profiles cannot contain environment variables,
headers, credentials or arbitrary extra fields. Do not put secrets in arguments.

```powershell
$env:LOCAL_AI_MCP_APPROVALS = 'F:\LLM\AI_Project\myLocalAI\data\mcp_approved_profiles.json'
.\.venv\Scripts\python.exe app.py
```

Profiles are cached lazily; an explicit GET `/profiles` or profile-request status
check reloads the owner-written file. A saved registration can run only when its immutable config fingerprint
still matches a current profile. A changed/missing profile blocks Start; it does
not remove the saved registration or grants. To intentionally change a registered
configuration, Stop/Delete it and register the newly approved profile again.
Trusted Python integration/tests can instead pass a sequence of `ServerConfig`
objects as `create_app(mcp_approved_configs=...)`.

The Windows `start_app.bat` launcher preserves an existing
`LOCAL_AI_MCP_APPROVALS` value. If it is unset and the project-local
`data/mcp_approved_profiles.json` exists, the launcher passes that path to Python.
If neither is present, it starts normally without approved profiles. An explicit
missing/invalid path is never silently replaced. This environment change is local
to the launcher (`setlocal`); it does not modify user/system settings or create or
edit approval files. The service also falls back to `mcp_approved_profiles.json`
beside its MCP DB when no override is set, so new CLI-approved profiles are usable
without an environment change/restart. No server is automatically started by loading profiles.

## Local request policy

Every `/mcp/*` request, including GET, requires:

```text
X-Local-AI-Control: web-console
```

Only loopback peers and loopback Host names are accepted. A browser Origin must
exactly match the request origin; cross-site Fetch requests are rejected and no
CORS permission is added. Responses use `Cache-Control: no-store`. JSON writes
accept only the documented fields. The existing general 1 MiB request limit
remains unchanged. Command paths/arguments and child stderr are not exposed by
the API. These controls assume a trusted local PC user, not authentication against
malware or other local programs. An execution profile is not an OS sandbox: its
reviewed executable can still access files/network under the local user's rights.

## API contracts

All paths below start with `/mcp`.

| Method/path | Body / response purpose |
|---|---|
| `GET /profiles` | Available local `profile_id` values; no executable paths |
| `POST /profile-requests` | `{name,transport:"stdio",executable,arguments:[...]}` creates an untrusted proposal only; 201 `{request:...}` |
| `GET /profile-requests/<id>` | Reload owner approvals and report pending/approved with terminal review command |
| `DELETE /profile-requests/<id>` | Remove only the proposal; never revoke/edit an approved profile |
| `GET /servers` | `{"servers":[...]}` with registration and runtime status |
| `POST /servers` | `{"profile_id":"...","name":"optional display name"}`; 201 `{"server":...}` |
| `GET /servers/<id>` | Registration, execution approval and status |
| `DELETE /servers/<id>` | Stopped only; `{"server_id":"...","status":"deleted"}` |
| `POST /servers/<id>/start` | `{}`; explicitly starts approved stdio server |
| `POST /servers/<id>/stop` | `{}`; SDK-owned process shutdown |
| `GET /servers/<id>/status` | Last-known state, operation IDs/counts, safe error codes |
| `GET /servers/<id>/tools` | Live refreshed tool descriptors, schema hashes, effective `allowed` |
| `GET /servers/<id>/permissions` | Durable grants; live schema validation still required before execution |
| `PUT /servers/<id>/tools/<tool>/permission` | `{"allowed":true}` or `false` |
| `POST /servers/<id>/tools/<tool>/approve` | `{"arguments":{...}}`; one-use token valid for 120 seconds |
| `POST /servers/<id>/tools/<tool>/call` | `{"arguments":{...},"approval_token":"..."}` |
| `GET /servers/<id>/operations/<operation_id>` | Retained operation status and authorized late call result |

Server responses contain `server_id`, `profile_id`, `name`, `created_at`,
`transport: "stdio"`, `execution_approved`, and a `status` object. Runtime status
contains no result/transcript content. Errors use the existing shape:

```json
{"error":{"code":"mcp_wait_timeout","message":"...","operation_id":"..."}}
```

`operation_id` is present only for an admitted operation. Typical statuses are
New profile proposals reject leading/trailing argument whitespace with HTTP 400
`mcp_argument_whitespace`, identifying 1-based argument positions. Internal
spaces are preserved, never automatically trimmed. Existing approved profiles
and runtime arguments are unchanged. The web form reports original line numbers
before removing empty lines.

400 invalid fields/arguments, 403 local/permission/approval denial, 404 missing
server/tool/retained operation, 409 state/config conflict, 413 existing body limit,
415 non-JSON write, 429 concurrent action/approval capacity, 502 upstream error or
uncertain call, 503 storage/start/cleanup unavailable, and 504 caller timeout.
A completed tool error is HTTP 200 with `result.is_error: true`; it is distinct
from losing the connection or timing out.

## Manual invocation sequence

1. Select an owner-approved profile and register it. The existing `/servers` API
   still cannot accept executable paths, arguments or self-approved configurations.
   A new profile request can carry an executable/argument proposal but cannot approve
   or execute it; use the independent local-terminal boundary below first.
2. Explicitly Start; GET Tools discovers capabilities without executing them.
3. Review a tool and explicitly PUT `allowed:true`. Default is deny. This records
   a fingerprint of name, description and input schema. Changed descriptors are
   effectively denied until reviewed/granted again.
4. Review exact arguments, then POST Approve as the explicit user-confirmation
   action. This validates the input schema offline and returns an opaque token.
5. POST Call with the same arguments/token. Tokens are bound to that server,
   tool and schema, expire after 120 seconds, and are consumed once before actual
   dispatch. Grants alone do not run tools. Tokens are cleared by Stop/Start,
   revoke/regrant, Delete, Controller shutdown and restart.
6. Inspect `result.content`, `result.structured_content`, `result.is_error`,
   `operation_id`, and `elapsed_seconds`. Nothing is appended to Chat sessions.

Example PowerShell requests after a profile has been approved locally:

```powershell
$base = 'http://127.0.0.1:5000/mcp'
$headers = @{ 'X-Local-AI-Control' = 'web-console' }
$registered = Invoke-RestMethod "$base/servers" -Method Post -Headers $headers -ContentType 'application/json' -Body '{"profile_id":"reviewed-local-server"}'
$sid = $registered.server.server_id
Invoke-RestMethod "$base/servers/$sid/start" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}'
Invoke-RestMethod "$base/servers/$sid/tools" -Headers $headers
# Review the actual tool name/schema before granting or invoking anything.
```

There is no automatic confirmation, auto-retry, auto-grant or LLM execution path.
Arguments must be finite JSON objects up to 64 KiB. JSON Schema references are
local-fragment only; external references are rejected and never downloaded.
Tokens contain no stored arguments; only their fingerprints are kept in RAM.
Call results remain only in the core's bounded operation history, not SQLite or
diagnostic logs. Revoke also blocks subsequent API retrieval of a late result.

## New-profile owner approval boundary

`/profile-requests` uses all existing loopback, header, exact-origin, JSON and
body-size guards. Only stdio and validated `ServerConfig` executables/argv are
accepted; shell launchers, relative executables and arbitrary extra fields fail.
Proposals are finite, bounded JSON files (64 KiB each, maximum 64 entries, 24-hour
validity) under `mcp_profile_requests/` beside the MCP DB. This never modifies the
registry or starts a process. UUID filenames cannot select other paths. Fixed
Python-injected profile configurations disable web profile management.

Only `scripts/approve_mcp_profile.py` can append an execution approval. From a
local terminal run the exact command displayed in Add Server; it includes the
request ID and explicit request/approval paths, so custom configurations need no
environment edits. Review JSON-escaped executable, separate argv and cwd, then
type `APPROVE <request-id>`. There is no HTTP approval route or unattended `--yes`.
The script rechecks the request after confirmation, validates/preserves all
existing profiles, uses an exclusive lock and atomic file replacement, refuses
conflicting IDs, and never runs the proposed command. A CLI cancellation or write
failure preserves the original manifest. Stale locks are not stolen automatically.
Only after the owner file matches the exact proposal does GET report approved;
the UI then uses the unchanged POST `/servers`. Registration remains STOPPED and
every tool still defaults to denied with a separate 120-second per-call approval.

This is a separate local-owner terminal, not Windows UAC authentication or an OS
sandbox. Same-account malware/filesystem writers remain inside the existing trust
boundary. Approved binaries/scripts are not content-pinned. Do not edit the file
outside the approver concurrently; optimistic change checks complement cooperating
approver locks but cannot turn uncoordinated external writers into a transaction.
Proposals/approvals stay plaintext and Git-ignored. Cancelling/removing proposals
does not revoke already approved configurations. Profile revocation remains an
owner configuration task.

## Concurrency, timeout and shutdown

The adapter accepts one modifying/discovery action at a time per server; another
action returns 429 instead of waiting on a Flask thread. Status, saved grant
queries and operation polling remain available. Explicit Stop can overlap an
in-flight call/discovery and applies the existing MCP-1 drain/cancel policy.
New actions are blocked while stopping. Conflicting Start/Delete/Stop lifecycle
requests return 429. Controller shutdown invokes the same cleanup even when
calls are in progress.

MCP-1's 15-second Start and 30-second call wait defaults remain. A timeout returns
the operation ID, keeps tracking actual work, and blocks new execution while
UNCERTAIN. Poll status/operation instead of retrying. Late known completion is
reflected; a lost call response remains UNCERTAIN until explicit Stop. No client
cancel, closed pipe or killed process proves side effects were rolled back.

`python app.py` now uses `try/finally` to call
`app.extensions['mcp_service'].shutdown()` when Flask returns or is interrupted.
It does not shut down per HTTP request. Custom WSGI hosts/application factories
must invoke the same method from their own worker shutdown lifecycle. Abrupt
process/OS termination is not a normal graceful-shutdown guarantee.

This version supports a single Controller process per registry. Registrations
and runtime IDs are separate from conversation sessions; no Session schema,
messages, numbers, Reset/Delete policy or UI is changed. Registry corruption
returns an explicit MCP error and does not disable unrelated Health/Chat APIs.
The existing ignore rules exclude `data/`, SQLite sidecars and backups. The MCP
DB/approval file are plaintext local configuration: protect their filesystem
permissions and do not upload them. Output size before SDK decoding, arbitrary
third-party server behavior and hard-kill recovery remain follow-up risks.

## Verification

`test_mcp_registry.py` uses real temporary SQLite transactions and checks Session
file isolation/rollback. `test_mcp_api.py` uses Flask clients, real temporary DBs
and fake SDK clients for security, expiry, descriptor changes and races.
`test_mcp_api_stdio.py` uses the actual SDK and Windows echo/add fixture, including
restart/grant restoration, no auto-start, timeouts and busy shutdown. All process
termination/exit checks are restricted to token-verified fixture handles. These
tests do not execute Ollama/STT/TTS models, third-party servers or Edge UI.

Verified on Windows/Python 3.11.9: 214 Python tests and 76 JavaScript tests passed
(290 total; 41 added for MCP-2). The new checks comprise 3 core registration/cache
tests, 10 temporary SQLite tests, 23 fake-client API/configuration tests and 5
actual SDK/Flask stdio tests. Restart verification also uses a fresh Python/Flask
process to read saved registrations/grants without constructing a runtime or
starting a child. The operating Session DB hash remained unchanged.

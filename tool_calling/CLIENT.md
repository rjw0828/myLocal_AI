# MCP-5D-2 Tool Chat client (no UI connection)

`static/js/tool-chat.js` exports `ToolChatClient`, `ToolChatTracker` and
`ToolChatError` through CommonJS or `globalThis.LocalAIToolChat`. It has no DOM
access or startup requests. MCP-5D-3 loads it before `main.js` together with the
DOM adapter `tool-chat-console.js`. Ordinary Chat, Voice and the MCP Manager
retain their separate flows. Backend contracts are in [API.md](API.md).

## Client

Inject `fetch` explicitly (in a future browser integration, use
`window.fetch.bind(window)`). Requests use relative `/tool-chat/jobs` URLs,
same-origin credentials and `X-Local-AI-Control: web-console`. Origin/loopback
authorization remains the Backend's responsibility. The default HTTP timeout
is 10 seconds per request, not the lifetime of a background job.

| Method | Parameters |
| --- | --- |
| `create` | `{sessionId,message,browserGeneration,responseLanguage?}` |
| `get` | `jobId, {signal}?` |
| `approval` | `jobId` |
| `decide` | `jobId, {proposalId,browserGeneration,allow,scope?}` |
| `cancel` / `reconcile` | `jobId` |

Job methods return validated job objects; `approval` returns the validated
approval envelope. `ToolChatError` exposes `code`, `status`, `uncertain` and
`message`. Backend error codes are preserved. No client method retries a POST.

## Tracker

```javascript
const { ToolChatClient, ToolChatTracker } = LocalAIToolChat;
const client = new ToolChatClient({ fetch: window.fetch.bind(window) });
const tracker = new ToolChatTracker({ client, storage: window.sessionStorage });
await tracker.setContext({ sessionId, generation, resetVersion });
const unsubscribe = tracker.on("completed", (event) => {
  // A consumer must check its current context, render safely, then acknowledge.
  renderAnswerWithTextContent(event.result);
  tracker.acknowledgeResult(event.sessionId, event.jobId);
});
await tracker.start({ message, responseLanguage: "ko" }); // receipt, not answer
```

The example is an interface sketch; the application connection is in `main.js`
and `tool-chat-console.js`.

| Method | Responsibility |
| --- | --- |
| `setContext(context or null)` | Change active view immediately; invalidate old view and send one cancellation for its unresolved known job. Await this before finishing UI navigation. |
| `start({message,responseLanguage?})` | Reserve locally before one create POST; reject another start while unresolved. |
| `get(sessionId)` / `isBlocked(sessionId)` | Snapshot / local reservation guard. Not a substitute for server validation or the existing ordinary Chat guard. |
| `check(sessionId, {verifyRelease:false}?)` | Authoritative GET; concurrent reads share one promise. `verifyRelease:true` installs a persisted verification barrier before the read, so failure cannot unlock a mutation attempt. |
| `decide(sessionId, decision)` | Explicit allow/deny using current `proposalId` AND `approvalVersion`; Filesystem allow requires exact displayed `scope`. |
| `cancel(sessionId)` | Immediately invalidate output/approval and request cancellation. Concurrent clicks share the POST; failure does not automatically resend. |
| `reconcile(sessionId)` | Explicit request only for a terminal unsettled job with a known MCP operation ID. Backend must confirm completion. |
| `acknowledgeResult(sessionId, jobId)` | Mark a delivered result applied by the current UI. |
| `on(event, listener)` | Subscribe; returns unsubscribe. Listener exceptions do not interrupt tracking. |
| `dispose()` | Stop timers/GETs and remove listeners; preserve unresolved metadata. Does not itself guarantee server cancellation. |

Events: `created`, `state`, `approval`, `completed`, `failed`, `uncertain`,
`cancelled`, `reservation_wait`, `communication_error`, `request_error`.
Each contains an isolated snapshot. `approval` adds `proposal`; `completed`
adds `result`; error events add `{error:{code,status,message,uncertain}}`.
State snapshots never include the answer text. Rendering remains the
adapter's responsibility. `completed` is delivered at most once; `resultDelivered`
and `resultApplied` are separate flags.

The tracker keeps the Backend's original `browserGeneration` and
`backendRevision` separate from `viewGeneration` and local `resetVersion`.
Call `setContext` on every session switch, including A -> B -> A, and on Reset
invalidation. Proposal ID/content changes invalidate the previous approval
ticket. Restored/background/cancelled views cannot deliver an approval or answer.

## Polling, reservations and recovery

- Polling defaults to 1 second, scheduled after the preceding read completes.
  Transient GET failures retry twice (300 ms, 1,200 ms). POSTs never retry
  automatically. Errors, bad schemas and unknown states never mean success.
- HTTP 202 means accepted. Only `COMPLETED` with a valid final result releases
  a completed answer. `FAILED`, `UNCERTAIN` and `CANCELLED` remain distinct.
- `CANCELLED` with `reservation_owned:true` remains blocked. Known ongoing work
  can still be observed. Terminal unsettled work stops automatic polling and
  requires explicit checking/reconciliation; no elapsed-time unlock exists.
- Reconcile is never an automatic recovery mechanism for unknown Ollama work.
  `operation_unresolved` preserves the barrier. Client cancellation is not
  server rollback. Existing Chat/Reset/Delete guards must also be consulted.
- Optional injected Storage saves only unresolved IDs/revision/generation and
  reservation metadata under `local-ai-tool-chat-jobs-v1`. No message, answer,
  approval arguments or filesystem path is saved. Without Storage, protection
  lasts only for this tracker instance. Storage failures leave in-memory guards
  intact but cannot guarantee reload recovery.
- The Tracker constructor restores blocked metadata without requests. MCP-5D-4
  subsequently verifies known IDs through read-only GET after session bootstrap.
  Old approvals/results remain hidden; a confirmed settled job reloads History.
- A lost create response has no reliable job ID. Keep `CREATION_UNCERTAIN`
  blocked; do not recreate or clear it using a timer. The current Backend has
  no idempotency key/session-to-job lookup; verified recovery would require a
  separately approved Backend addition. Similarly, a 404 after Backend restart
  does not prove a reservation can be discarded within this module.

## Validation

```powershell
node tests/test_tool_chat_ui.js
node --check static/js/tool-chat.js
.\scripts\test.ps1 -Mode quick -Module ui
```

The new tests use Mock Fetch, deferred responses and controlled timers without
DOM, a server, Ollama or MCP processes. The runner includes this suite in
`ui`, `mcp` and `full` selection. Real DOM/Edge and full regression remain
unverified in MCP-5D-3.

## MCP-5D-3 console connection

- `ToolChatConsole` connects the existing Tracker to compact D controls,
  an E status line and a dedicated approval dialog. `main.js` supplies session
  context, safe conversation rendering, History synchronization and F logging.
- Tool mode defaults OFF. Only Enter sends; Shift+Enter and composing IME are
  preserved. Mode changes never start work. Manual STT can supply the draft;
  automatic Voice and Tool mode are mutually exclusive. No automatic TTS is
  added; existing Play buttons are retained for final answers.
- Dialog content uses textContent. Full arguments/path/scope appear only in
  the review dialog; F excludes them and Backend error message bodies. The
  exact server scope is sent unchanged. Close/Esc neither approves nor denies.
  Replacing a proposal closes the old dialog and requires deliberate Review
  approval before re-enabling its decision buttons.
- Final answers render once on COMPLETED and are then replaced by authoritative
  Session History. Old History requests are invalidated when a Tool turn starts.
  Session changes cancel the old Job and invalidate its screen context; failures
  keep its reservation ledger. Cancel, Lock, navigation and mode OFF do not
  equate to server rollback. Page exit attempts Cancel, then disposes client
  resources; a browser closing cannot guarantee request delivery.
- Check status uses only known Job IDs. Reconcile MCP appears only for terminal
  unsettled work with an MCP operation ID; it is explicit and the Backend must
  verify completion. Unknown Ollama/creation work stays blocked.
- MCP-5D-4 adds `prepareMutation(ids)` (`null` means all tracked sessions).
  After the existing confirmation and mutation UI guards are installed, it
  cancels each blocked known job, drains an older in-flight poll, then performs
  a fresh GET with `verifyRelease:true`. Only a terminal, settled, released
  snapshot permits the existing mutation API. Cancel errors, active approval
  requests, unknown IDs or failed verification abort the attempt. Delete All
  waits for all preflight attempts; one unresolved job prevents the bulk DELETE.
  No automatic Reconcile, mutation replay or timeout unlock is added.
  Backend reservation/revision guards remain authoritative against other tabs
  and changes after the GET. Other unreserved sessions remain usable.
- `restoreKnownJobs()` only GETs known restored IDs after session bootstrap,
  never recreates a job or restores an approval dialog. Explicit Retry Cancel
  is available after a failed cancellation or a restored cancellation intent.
  Unknown IDs, 404 and network failures preserve the local guard. Final answers
  lost in transit recover through authoritative History, not old completion
  events. Browser Storage is metadata, never proof of server reservation release.

### Backend recovery boundary (no extension in MCP-5D-4)

The API has no session-owner/job lookup, creation idempotency key or server
instance identity. A lost creation response cannot reliably be bound to a job;
a 404 cannot distinguish restart, expiry or a wrong ID. These cases intentionally
stay blocked. New sessions may be explicitly created by the user; that does not
clear an old unresolved ledger entry or authorize Delete All.

A future recovery design should persist a unique client creation ID before POST,
bind its idempotency mapping to session/revision and a server instance ID, and
offer a guarded read-only lookup for that exact request. It must use the existing
loopback/Origin/control-header policy, expose no approval tokens or tool contents,
and must not infer approval or release from a different instance's missing job.
A session-ID-only lookup could bind another tab's job and is insufficient.
This requires coordinated Backend/client identity and expiry rules, so it is
proposed separately rather than added as an unverified recovery shortcut.

### Future Edge validation checklist (not executed here)

1. With an existing session selected and D unlocked, leave Tool Chat OFF and
   verify ordinary Enter Chat, Shift+Enter, Korean IME and manual STT/Play.
2. Start approved Time/Filesystem servers through the existing Manager, allow
   only intended tools, turn Tool Chat ON, and send a question. Merely enabling
   the mode must send nothing. Observe Running/Waiting in D/E and events in F.
3. Review server/tool/arguments and full recursive root scope. Close or Esc:
   no approval. Review again and Deny: no execution. In a fresh turn Allow:
   final answer appears once, History matches, no automatic audio plays.
4. Cancel while waiting/running, switch A -> B -> A, Lock, or change view:
   old approval/output must not reappear. A retained reservation must block
   new Chat/Voice; Reset/Delete must verify release before mutating. Check
   status must not create another Job. Repeat with waiting approvals and with
   multiple sessions before Delete All; verify no partial deletion on refusal.
5. Test a safe network failure: verify a clear error and no automatic POST
   replay. Never force a production operation or use Reset as uncertainty
   recovery. Known confirmed MCP completion may be explicitly reconciled;
   unknown model completion remains protected.
6. At narrow and desktop widths, verify D controls and status tooltip, E/F
   independent scrolling, dialog scrolling, Tab/focus/Close/Esc and approval
   scope visibility. No geometry/audio/real-device results are claimed by the
   browser API doubles.

Connection tests: `node tests/test_tool_chat_console.js`. They run actual
`main.js`, the client and adapter with controlled Fetch/browser API doubles,
not an actual DOM or model. MCP-5D-4 adds 17 console cases, one Tracker release
verification case and three Backend cases. Known jobs after reload, lost
creation/final responses, failed Cancel/verification, old polls, mutation
ordering, multi-session Delete All, late STT and manual TTS are covered.

Validation on 2026-11-13: console 43/43, Tracker 44/44; quick ui,mcp collected
392 (391 passed/1 skipped), 13.23 s; integration app,sessions,storage,mcp,ui
collected 522 (521 passed/1 skipped), 54.30 s. The existing skip is Windows
symlink privilege 1314. Full/Edge/real Qwen remain for MCP-5D-5. The multi-job
Backend test stages Discovery sequentially; overlapping Discovery on the same
server can return existing `mcp_busy`, which was observed and not changed.

## MCP-5D-5B error diagnostics and release verification

Two Console defects were reproduced and minimally fixed:

- A request error was briefly written to E, then overwritten by `render()` and
  the next successful poll. The adapter now keeps a per-job last request error
  alongside the actual state/reservation, without changing that state. A bad
  approval can remain WAITING_APPROVAL; it does not mean the tool executed or
  that the reservation disappeared. Review/Deny/Cancel and Check remain usable.
- Tracker events and adapter/main catch blocks logged the same HTTP failure
  twice. The Tracker now exposes `wasReported(error)` using weak object identity.
  The Console and mutation handlers avoid re-logging that same error object.
  There is no code/message/time-based deduplication: two separate failed HTTP
  requests with the same code still produce two reports. Synthetic validation
  errors with no Tracker event are still logged.

Terminal FAILED with a known released reservation stops polling. Rejected
approval waits are bounded by the existing Backend approval deadline (120 s),
or explicit Deny/Cancel. A new Backend test uses a shortened test-only deadline
to verify expiry; production timeouts are unchanged. UNCERTAIN remains protected,
not silently converted to success. Other sessions stay usable.

The live fixture uses fresh temporary Session/MCP DBs and a read-only fixture
Root, with no operational approval manifest or data. Each creation/proposal is
sent once; a rejected approval is recorded and followed by one cleanup Cancel,
never an automatic approval replay. This corrects the temporary MCP-5D-5 test
driver's repeated-approval error; that driver was not production UI behavior.

Actual Qwen + official SDK + installed Time/Filesystem servers confirmed Time,
Filesystem Allow/Deny and Time -> Filesystem ordered execution. Final successful
turns store two History messages. Deny runs no tool and stores no History.
However the outside-path prompt selected Time incorrectly, and the relative
path prompt did not call a tool. Those are model-selection observations, not
proof of path rejection; no prompt/schema/model optimization was introduced.

Separate deterministic probes use Fake Ollama with REAL Filesystem/SDK/Flask
and temporary DBs. They verify HTTP 400 filesystem_invalid_path and
filesystem_outside_roots before any MCP dispatch, WAITING_APPROVAL with a
reservation after rejection, and CANCELLED/released after explicit Cancel.
A direct real MCP call outside its Root returns is_error without leaking the
outside fixture marker. Both kinds of evidence must remain distinguished.

No Edge interaction was performed in MCP-5D-5B; the earlier Computer Use URL
identification restriction was not bypassed. Real dialog geometry/focus/Esc,
Cancel/Retry, session navigation, Reset/Delete, reload, ordinary Chat/Voice and
small-window layout require user validation. Use test sessions for Reset/Delete;
Delete All checks must use an isolated Controller, never the operational DB.

MCP-5D-5B validation (2026-11-13): the initial 47-case Console run reproduced
three failures (hidden error and two duplicate-report paths). All 47 pass after
the minimal fix. Five new regression tests were added: four Console cases and
one Backend case covering both explicit cancellation and test-only approval
expiry. Quick ui,mcp: 397 collected, 396 passed/1 skipped, 14.85 s.
Integration app,sessions,storage,mcp,ui: 527 collected, 526 passed/1 skipped,
59.49 s. Final full was run ONCE: 552 collected (326 Python + 226 JavaScript),
551 passed/1 skipped, 60.79 s. Skip: existing Windows symlink privilege 1314.
Syntax and git diff --check pass. No HTTP API/schema/model-selection change.

Actual Qwen Time completed in 8.016 s with one tool and two History messages;
Filesystem Allow completed in 4.016 s with the exact fixture marker; Deny had
zero dispatches/zero History and released its reservation. Time -> Filesystem
completed in 6.485 s with two ordered dispatches, two explicit approvals and
two History messages. All final states were settled/released. The outside-path
question incorrectly selected Time, relative-path question made no tool call,
and natural Korean time question requested clarification without a tool call.
These model-selection shortcomings are MCP-5E observations, not successful
path-boundary tests. Separate real-server deterministic boundary probes passed
as described above. Installed servers stopped normally and their runtimes shut
down. Operating Session DB, MCP DB and approval manifest SHA-256 values matched
before/after; generated fixture files were unchanged. Evidence is confined to
external %TEMP%/local-ai-mcp5d5b-* directories, not Git.

Release: CONDITIONAL pending the manual Edge/device checklist above. No remaining
code blocker was reproduced in the automated/live-backend scope. This does not
assert actual DOM, focus, layout, microphone or speaker verification. Existing
unknown-job/404/UNCERTAIN recovery and mcp_busy constraints remain documented.

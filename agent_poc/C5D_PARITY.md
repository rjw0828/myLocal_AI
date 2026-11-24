# C-5D dynamic discovery and filesystem contract parity

## Scope and preserved boundaries

The official Pydantic Agent still owns inference and Tool rounds. Existing
MCPService, Registry, one-use authorization and owner-task Single Dispatch are
retained. No old tool_calling backend, keyword routing, search algorithm or
model-specific prompt branch was introduced. Ordinary Chat and Voice are unchanged.

## Contract comparison

| Contract | Previous MCP implementation | Pydantic path in C-5D |
| --- | --- | --- |
| Filesystem provenance | Reviewed package 2.2.0 aggregate digest, connection snapshot | Same reviewed aggregate pin; executable hash and sole Root identity also pinned |
| Launch shape | script, --read-only, one Root | Legacy shape remains absolute-only; --read-only --confine-roots Root additionally permits relative arguments |
| Connection identity | Config/connection and Root attestation | Actual owner config fingerprint, protocol, disabled Roots callback, pre/post-handshake artifact, config and connection generation |
| Model Root context | Verified ephemeral workspace data | Official Agent instruction callback validates pinned entries and Root before every model request |
| New registration | Rebuild catalog at job creation | ManagedCatalog discovers latest allowed/running registrations for each new Run |
| Running Run | Frozen identities | Existing MCPBridge snapshots entries; stale entries are never silently upgraded |
| Before transmission | Revalidate permission/schema/Root | Owner discovery + service final descriptor check + policy/Root/artifact check + one public SDK session call |
| Lost response | Retain uncertainty, no resend | Manager dispatch ledger marks attempted/unacknowledged work unknown; no retry |

Root context is JSON data, not a grant. History commits only the original user
question and final assistant text. Schema, executable, Root, connection or
permission changes never extend an existing trust. Reconnection requires fresh
matching trust. Unverified filesystem-like Tools are excluded from Agent exposure;
generic Tools remain explicit ASK unless separately trusted under existing policy.

Filesystem support remains the reviewed `read_file` and `list_directory` subset.
Other filesystem capabilities require a separately reviewed contract. Static
preflight is not an OS sandbox: same-user mutation between checks and a server's
actual open remains a TOCTOU limitation. Server confinement remains necessary.

## Existing Console hosting

The existing `web_console` entry point accepts `--registered-console --base
<fresh-experimental-directory> --model <installed-profile> --port <loopback-port>`.
It creates an empty management Registry, not an MCP process. Existing owner
profile approval and explicit Console Start are still required. Catalogs are
dynamic by default; an explicitly supplied `exposed` set remains an optional
host restriction. The older bounded Time pilot retains its explicit start review.
No new Console UI was created.

Policy revisions are conservative: establishing a new connection contract may
invalidate another pending Run. It does not mutate that Run's catalog or transfer
approval. A new request must obtain fresh proposals when required.

## Verification

Tests use memory transports, temporary packages, synthetic SDK responses and
temporary DBs. Synthetic SDK patching prevents subprocess launch. They verify
dynamic add/stop/block/restart/delete, unseen generic Tools, file basename/Root
context, clean History, stale in-flight model responses, artifact/profile/protocol
tampering, Root replacement, fresh trust after reconnect, one dispatch and lost
response uncertainty. Actual SDK/stdio fixture modules and Voice dependency suites
are excluded from the safe regression collection, not relabelled as passing.

Local model selection used the official FunctionToolset with a memory catalog,
two Tools, empty History, seeds 1/2/3, original user questions and ephemeral Root
context. Only first responses were evaluated; no proposal was approved or MCP
Tool executed. Each response was checked against the installed model digest and
actual context 8192. Gemma: 15/15; Qwen: 14/15. File requests: 3/3 each, with
Gemma emitting an absolute path and Qwen a valid confined relative path. General
greetings/code: no Tools 6/6 each. Qwen asked for timezone clarification for one
short time question. These small samples do not establish universal reliability.

## Remaining integration gates

- Real installed Filesystem/SDK handshake and execution for the new ordinary
  registration adapter have NOT been exercised; separate approval is required.
- Actual browser validation of dynamic registration and workspace display remains.
- Existing request-specific evidence/answer-consistency checks are not equivalent
  to the current Pydantic pilot's successful-operation presence check. This is a
  separate integration blocker, not solved or weakened here.
- The one-Tool-per-response pilot limit remains; compound execution is out of scope.
- Qwen short-question selection, Voice environment dependencies, and the previous
  Allow-receipt fix's browser recheck remain separate open items.
- No branch integration, Commit, Push, Merge, Reset or Rebase was performed.

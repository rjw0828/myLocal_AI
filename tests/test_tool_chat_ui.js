"use strict";
// No DOM or services: real client/tracker, controlled Fetch and timers.
const assert = require("assert").strict;
const { ToolChatClient, ToolChatTracker, ToolChatError } = require("../static/js/tool-chat.js");
const tests = [], test = (name, run) => tests.push({ name, run });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
const packet = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const failure = (code, status = 409) => packet({ error: { code, message: code } }, status);
function storage() {
  const values = new Map();
  return { values, getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
}
function timers() {
  const pending = new Map(); let count = 0;
  return { pending, set: (fn, delay) => { const id = ++count; pending.set(id, { fn, delay }); return id; },
    clear: (id) => pending.delete(id), next: async () => {
      const first = pending.entries().next().value; assert.ok(first, "No scheduled timer");
      pending.delete(first[0]); first[1].fn(); await flush(); return first[1].delay;
    } };
}
function proposal(id = "proposal-1", scope = null) {
  return { proposal_id: id, server_id: "mcp-time", server_name: "Time", tool_name: "get_current_time",
    description: "Read current time", arguments: { timezone: "Asia/Seoul" }, turn_scoped: true, scope };
}
function job(changes = {}) {
  const state = changes.state || "RUNNING";
  const base = { job_id: "job-1", session_id: "A", browser_generation: 1, revision: 4,
    state, phase: state, reservation_owned: !["COMPLETED", "FAILED", "CANCELLED"].includes(state),
    unsettled: state === "UNCERTAIN", cancelled: state === "CANCELLED", operation_id: null,
    pending: state === "WAITING_APPROVAL" ? proposal() : null, result: null, error_code: null };
  Object.assign(base, changes);
  if (state === "COMPLETED" && !Object.prototype.hasOwnProperty.call(changes, "result")) {
    base.result = { session_id: base.session_id, model: "qwen3.5:9b", message: "answer", revision: 5,
      elapsed_seconds: 1, metrics: { eval_count: 4, tokens_per_second: 2 } };
  }
  return base;
}
function fixture(options = {}) {
  const requests = [], handlers = {}, jobs = new Map(), scheduled = timers(), apiTimers = timers();
  const saved = options.storage || storage(); let sequence = 0;
  const fetch = async (url, opts) => {
    const data = opts.body === undefined ? undefined : JSON.parse(opts.body);
    requests.push({ url, options: opts, data });
    const handler = handlers[opts.method + " " + url];
    if (handler) return handler(opts, data);
    if (url === "/tool-chat/jobs" && opts.method === "POST") {
      const next = job({ job_id: "job-" + ++sequence, session_id: data.session_id, browser_generation: data.browser_generation });
      jobs.set(next.job_id, next); return packet({ job: next }, 202);
    }
    const [, id, action] = url.match(/^\/tool-chat\/jobs\/([^/]+)(?:\/(\w+))?$/) || [];
    let current = jobs.get(id);
    if (!current) return failure("job_not_found", 404);
    if (opts.method === "GET" && action === "approval") return packet({ job_id: id, state: current.state, approval: current.pending });
    if (opts.method === "GET") return packet({ job: current });
    if (action === "cancel" || action === "approval" && !data.allow) current = job({ ...current, state: "CANCELLED", phase: "CANCELLED", reservation_owned: false, unsettled: false, cancelled: true, pending: null, result: null });
    else if (action === "approval") current = job({ ...current, state: "RUNNING", phase: "RUNNING", pending: null });
    else if (action === "reconcile") current = { ...current, reservation_owned: false, unsettled: false };
    jobs.set(id, current); return packet({ job: current }, action === "approval" ? 202 : 200);
  };
  const client = new ToolChatClient({ fetch, setTimer: apiTimers.set, clearTimer: apiTimers.clear });
  const tracker = new ToolChatTracker({ client, storage: saved, pollMs: 50, retryDelays: [5, 10], setTimer: scheduled.set, clearTimer: scheduled.clear, ...options });
  const events = [];
  for (const name of ["created", "state", "approval", "completed", "failed", "uncertain", "cancelled", "reservation_wait", "communication_error", "request_error"]) {
    tracker.on(name, (event) => events.push({ name, event }));
  }
  tracker.setContext({ sessionId: "A", generation: 1, resetVersion: 0 });
  const start = () => tracker.start({ message: "question", responseLanguage: "ko" });
  const setJob = (changes) => { const current = jobs.get("job-1"); jobs.set("job-1", job({ ...current, ...changes })); };
  const waiting = async (scope = null) => { await start(); setJob({ state: "WAITING_APPROVAL", phase: "WAITING_APPROVAL", pending: proposal("proposal-1", scope) }); await tracker.check("A"); return tracker.get("A"); };
  const decide = (allow = true, extra = {}) => { const r = tracker.get("A"); return tracker.decide("A", { proposalId: r.approval.proposal_id, approvalVersion: r.approvalVersion, allow, ...extra }); };
  return { client, tracker, requests, handlers, jobs, scheduled, apiTimers, saved, events, start, setJob, waiting, decide,
    count: (method, suffix) => requests.filter((r) => r.options.method === method && r.url.endsWith(suffix)).length };
}

test("module construction, subscription and context do not send requests", async () => {
  const f = fixture(); assert.equal(f.requests.length, 0); assert.equal(f.scheduled.pending.size, 0);
  f.tracker.dispose();
});
test("creation keeps screen generation and backend revision separate; 202 stays blocked", async () => {
  const f = fixture(); const r = await f.start();
  assert.equal(r.jobId, "job-1"); assert.equal(r.browserGeneration, 1); assert.equal(r.backendRevision, 4);
  assert.equal(r.state, "RUNNING"); assert.equal(r.blocked, true);
  assert.equal(f.events.filter((e) => e.name === "completed").length, 0);
  assert.equal(f.requests[0].data.response_language, "ko"); f.tracker.dispose();
});
test("all API methods keep same-origin, no-store and control headers", async () => {
  const f = fixture(); await f.waiting(); await f.client.approval("job-1"); await f.decide();
  f.setJob({ state: "UNCERTAIN", unsettled: true, operation_id: "op-1" }); await f.tracker.check("A");
  await f.tracker.reconcile("A");
  const other = await f.client.create({ sessionId: "B", message: "x", browserGeneration: 2 }); await f.client.cancel(other.job_id);
  for (const r of f.requests) {
    assert.ok(r.url.startsWith("/tool-chat/jobs")); assert.equal(r.options.credentials, "same-origin");
    assert.equal(r.options.cache, "no-store"); assert.equal(r.options.headers["X-Local-AI-Control"], "web-console");
    if (r.options.method === "POST") assert.equal(r.options.headers["Content-Type"], "application/json");
  }
  f.tracker.dispose();
});
test("polling is scheduled after receipt and never overlaps the same Job", async () => {
  const f = fixture(); await f.start(); const d = deferred(); f.handlers["GET /tool-chat/jobs/job-1"] = () => d.promise;
  const first = f.tracker.check("A"), second = f.tracker.check("A"); assert.equal(first, second);
  await flush(); assert.equal(f.count("GET", "/job-1"), 1); assert.equal(f.scheduled.pending.size, 0);
  d.resolve(packet({ job: job() })); await first; assert.equal(f.scheduled.pending.size, 1); f.tracker.dispose();
});
test("approval is only announced once for unchanged proposals", async () => {
  const f = fixture(); await f.waiting(); const before = f.events.filter((e) => e.name === "approval").length;
  await f.tracker.check("A"); assert.equal(f.events.filter((e) => e.name === "approval").length, before);
  assert.equal(f.count("POST", "/approval"), 0); f.tracker.dispose();
});
test("Allow and Deny use the job approval endpoint without MCP tokens", async () => {
  for (const allow of [true, false]) {
    const f = fixture(); await f.waiting(); await f.decide(allow);
    const request = f.requests.find((r) => r.options.method === "POST" && r.url.endsWith("/approval"));
    assert.deepEqual(request.data, { proposal_id: "proposal-1", browser_generation: 1, allow });
    assert.equal(f.tracker.get("A").state, allow ? "RUNNING" : "CANCELLED"); f.tracker.dispose();
  }
});
test("proposal replacement invalidates old UI tickets before POST", async () => {
  const f = fixture(); const old = await f.waiting();
  f.setJob({ pending: proposal("proposal-2") }); await f.tracker.check("A");
  await assert.rejects(f.tracker.decide("A", { proposalId: old.approval.proposal_id, approvalVersion: old.approvalVersion, allow: true }), { code: "stale_approval" });
  assert.equal(f.count("POST", "/approval"), 0); f.tracker.dispose();
});
test("same proposal ID with different arguments also requires a new UI ticket", async () => {
  const f = fixture(); const old = await f.waiting();
  f.setJob({ pending: { ...proposal(), arguments: { timezone: "UTC" } } }); await f.tracker.check("A");
  await assert.rejects(f.tracker.decide("A", { proposalId: old.approval.proposal_id, approvalVersion: old.approvalVersion, allow: true }), { code: "stale_approval" });
  f.tracker.dispose();
});
test("Filesystem scope must be explicitly identical; no widening or implicit grant", async () => {
  const scope = { path: "F:\\MCP_Workspace", kind: "directory", recursive: true };
  const f = fixture(); await f.waiting(scope);
  await assert.rejects(f.decide(), { code: "filesystem_invalid_scope" });
  await assert.rejects(f.decide(true, { scope: { ...scope, path: "F:\\" } }), { code: "filesystem_invalid_scope" });
  await f.decide(true, { scope }); assert.deepEqual(f.requests.find((r) => r.url.endsWith("/approval") && r.options.method === "POST").data.scope, scope);
  f.tracker.dispose();
});
test("duplicate approval clicks cannot duplicate POST", async () => {
  const f = fixture(); await f.waiting(); const d = deferred(); f.handlers["POST /tool-chat/jobs/job-1/approval"] = () => d.promise;
  const first = f.decide(); await assert.rejects(f.decide(), { code: "request_in_progress" });
  d.resolve(packet({ job: job() }, 202)); await first; assert.equal(f.count("POST", "/approval"), 1); f.tracker.dispose();
});
test("server stale_approval remains distinct and requires a fresh read/ticket", async () => {
  const f = fixture(); await f.waiting(); f.handlers["POST /tool-chat/jobs/job-1/approval"] = () => failure("stale_approval");
  await assert.rejects(f.decide(), { code: "stale_approval", status: 409 });
  assert.equal(f.tracker.get("A").approval, null); assert.equal(f.tracker.isBlocked("A"), true);
  assert.equal(f.count("POST", "/approval"), 1); f.tracker.dispose();
});
test("Cancel invalidates late poll completion and never delivers its answer", async () => {
  const f = fixture(); await f.start(); const d = deferred(); f.handlers["GET /tool-chat/jobs/job-1"] = () => d.promise;
  const read = f.tracker.check("A"); await flush(); await f.tracker.cancel("A");
  d.resolve(packet({ job: job({ state: "COMPLETED" }) })); await read;
  assert.equal(f.tracker.get("A").state, "CANCELLED"); assert.equal(f.events.filter((e) => e.name === "completed").length, 0); f.tracker.dispose();
});
test("CANCELLED with reservation stays blocked and is polled until known release", async () => {
  const f = fixture(); await f.start();
  f.handlers["POST /tool-chat/jobs/job-1/cancel"] = () => packet({ job: job({ state: "CANCELLED", reservation_owned: true }) });
  await f.tracker.cancel("A"); assert.equal(f.tracker.isBlocked("A"), true); assert.equal(f.scheduled.pending.size, 1);
  f.setJob({ state: "CANCELLED", reservation_owned: false, cancelled: true }); await f.scheduled.next();
  assert.equal(f.tracker.isBlocked("A"), false); assert.equal(f.scheduled.pending.size, 0); f.tracker.dispose();
});
test("late old GET cannot stall polling after a cancel receipt", async () => {
  const f = fixture(); await f.start(); const d = deferred(); f.handlers["GET /tool-chat/jobs/job-1"] = () => d.promise;
  const read = f.tracker.check("A"); await flush();
  f.handlers["POST /tool-chat/jobs/job-1/cancel"] = () => packet({ job: job({ state: "CANCELLED", reservation_owned: true }) });
  await f.tracker.cancel("A"); d.resolve(packet({ job: job() })); await read;
  assert.equal(f.scheduled.pending.size, 1); f.tracker.dispose();
});
test("completion is delivered once, and application acknowledgement is explicit", async () => {
  const f = fixture(); await f.start(); f.setJob({ state: "COMPLETED", reservation_owned: false });
  // Rebuild result for the completed server state.
  f.jobs.set("job-1", job({ state: "COMPLETED" }));
  await f.tracker.check("A"); await f.tracker.check("A");
  assert.equal(f.events.filter((e) => e.name === "completed").length, 1);
  assert.equal(f.tracker.get("A").resultApplied, false); f.tracker.acknowledgeResult("A", "job-1");
  assert.equal(f.tracker.get("A").resultApplied, true); assert.equal(f.tracker.isBlocked("A"), false); f.tracker.dispose();
});
test("fast COMPLETED creation orders created before completed without treating 202 as success", async () => {
  const f = fixture(); f.handlers["POST /tool-chat/jobs"] = () => packet({ job: job({ state: "COMPLETED" }) }, 202);
  await f.start(); assert.ok(f.events.findIndex((e) => e.name === "created") < f.events.findIndex((e) => e.name === "completed"));
  f.tracker.dispose();
});
test("FAILED and UNCERTAIN never emit completed and uncertainty keeps its lease", async () => {
  for (const state of ["FAILED", "UNCERTAIN"]) {
    const f = fixture(); await f.start(); f.jobs.set("job-1", job({ state, error_code: "job_timeout", operation_id: state === "UNCERTAIN" ? "op-1" : null }));
    await f.tracker.check("A"); assert.equal(f.events.filter((e) => e.name === "completed").length, 0);
    assert.equal(f.tracker.get("A").state, state); assert.equal(f.tracker.isBlocked("A"), state === "UNCERTAIN");
    assert.equal(f.scheduled.pending.size, 0); f.tracker.dispose();
  }
});
test("explicit reconcile releases a confirmed MCP lease without replay or completion", async () => {
  const f = fixture(); await f.start(); f.jobs.set("job-1", job({ state: "UNCERTAIN", operation_id: "op-1" })); await f.tracker.check("A");
  await f.tracker.reconcile("A"); assert.equal(f.tracker.isBlocked("A"), false);
  assert.equal(f.tracker.get("A").state, "UNCERTAIN"); assert.equal(f.count("POST", "/reconcile"), 1);
  assert.equal(f.events.filter((e) => e.name === "completed").length, 0); f.tracker.dispose();
});
test("Ollama uncertainty without MCP operation cannot be auto-reconciled", async () => {
  const f = fixture(); await f.start(); f.jobs.set("job-1", job({ state: "UNCERTAIN" })); await f.tracker.check("A");
  await assert.rejects(f.tracker.reconcile("A"), { code: "operation_unresolved" });
  assert.equal(f.count("POST", "/reconcile"), 0); assert.equal(f.tracker.isBlocked("A"), true); f.tracker.dispose();
});
test("operation_unresolved from server is preserved and never retried automatically", async () => {
  const f = fixture(); await f.start(); f.jobs.set("job-1", job({ state: "UNCERTAIN", operation_id: "op-1" })); await f.tracker.check("A");
  f.handlers["POST /tool-chat/jobs/job-1/reconcile"] = () => failure("operation_unresolved");
  await assert.rejects(f.tracker.reconcile("A"), { code: "operation_unresolved", status: 409 });
  assert.equal(f.tracker.isBlocked("A"), true); assert.equal(f.scheduled.pending.size, 0); f.tracker.dispose();
});
test("A to B to A cancels the old job and cannot resurrect its approval", async () => {
  const f = fixture(); await f.waiting();
  await f.tracker.setContext({ sessionId: "B", generation: 2, resetVersion: 0 });
  await f.tracker.setContext({ sessionId: "A", generation: 3, resetVersion: 0 });
  assert.equal(f.tracker.get("A").approval, null); assert.equal(f.tracker.get("A").cancelIntent, true);
  assert.equal(f.count("POST", "/cancel"), 1); f.tracker.dispose();
});
test("Reset version change invalidates approval even when Session ID stays the same", async () => {
  const f = fixture(); const old = await f.waiting();
  await f.tracker.setContext({ sessionId: "A", generation: 1, resetVersion: 1 });
  await assert.rejects(f.tracker.decide("A", { proposalId: old.approval.proposal_id, approvalVersion: old.approvalVersion, allow: true }), { code: "stale_approval" }); f.tracker.dispose();
});
test("switching screens during create preserves its receipt and sends Cancel once", async () => {
  const f = fixture(), d = deferred(); f.handlers["POST /tool-chat/jobs"] = () => d.promise;
  const create = f.start(); await f.tracker.setContext({ sessionId: "B", generation: 2, resetVersion: 0 });
  f.jobs.set("job-1", job()); d.resolve(packet({ job: job() }, 202)); await create;
  assert.equal(f.tracker.get("A").jobId, "job-1"); assert.equal(f.count("POST", "/cancel"), 1);
  assert.equal(f.events.filter((e) => e.name === "completed").length, 0); f.tracker.dispose();
});
test("network failures get bounded GET retries and preserve the reservation", async () => {
  const f = fixture(); await f.start(); f.handlers["GET /tool-chat/jobs/job-1"] = () => { throw new TypeError("offline"); };
  await assert.rejects(f.tracker.check("A"), { code: "network_failure" });
  assert.equal(await f.scheduled.next(), 5); assert.equal(await f.scheduled.next(), 10);
  assert.equal(f.count("GET", "/job-1"), 3); assert.equal(f.scheduled.pending.size, 0);
  assert.equal(f.tracker.isBlocked("A"), true); assert.equal(f.count("POST", "/jobs"), 1); f.tracker.dispose();
});
test("successful GET after a transient failure restores authoritative tracking", async () => {
  const f = fixture(); await f.start(); f.handlers["GET /tool-chat/jobs/job-1"] = () => { throw new Error("offline"); };
  await assert.rejects(f.tracker.check("A")); delete f.handlers["GET /tool-chat/jobs/job-1"];
  await f.scheduled.next(); assert.equal(f.tracker.get("A").verificationRequired, false); f.tracker.dispose();
});
test("lost create response cannot trigger automatic or manual duplicate creation", async () => {
  const f = fixture(); f.handlers["POST /tool-chat/jobs"] = () => { throw new Error("lost reply"); };
  await assert.rejects(f.start(), { code: "network_failure" });
  assert.equal(f.tracker.get("A").localState, "CREATION_UNCERTAIN"); assert.equal(f.tracker.isBlocked("A"), true);
  await assert.rejects(f.start(), { code: "session_unsettled" });
  await assert.rejects(f.tracker.check("A"), { code: "creation_uncertain" });
  assert.equal(f.count("POST", "/jobs"), 1); assert.equal(f.scheduled.pending.size, 0); f.tracker.dispose();
});
test("known creation rejection and session_reserved/session_unsettled remain distinct", async () => {
  for (const code of ["invalid_tool_chat_input", "session_reserved", "session_unsettled"]) {
    const f = fixture(); f.handlers["POST /tool-chat/jobs"] = () => failure(code, code === "invalid_tool_chat_input" ? 400 : 409);
    await assert.rejects(f.start(), { code }); assert.equal(f.tracker.isBlocked("A"), code !== "invalid_tool_chat_input"); f.tracker.dispose();
  }
});
test("unknown 5xx POST and invalid JSON remain uncertain instead of succeeding", async () => {
  for (const reply of [failure("new_server_error", 500), { ok: true, status: 202, json: async () => { throw new Error("bad JSON"); } }]) {
    const f = fixture(); f.handlers["POST /tool-chat/jobs"] = () => reply;
    await assert.rejects(f.start()); assert.equal(f.tracker.isBlocked("A"), true);
    assert.equal(f.events.filter((e) => e.name === "completed").length, 0); f.tracker.dispose();
  }
});
test("mismatched Session or browser generation never gets attached as a valid job", async () => {
  for (const changes of [{ session_id: "B" }, { browser_generation: 99 }]) {
    const f = fixture(); f.handlers["POST /tool-chat/jobs"] = () => packet({ job: job(changes) }, 202);
    await assert.rejects(f.start(), { code: "invalid_job_response" }); assert.equal(f.tracker.get("A").jobId, null);
    assert.equal(f.tracker.isBlocked("A"), true); f.tracker.dispose();
  }
});
test("malformed completed response cannot release a lease or deliver an answer", async () => {
  const f = fixture(); await f.start(); f.handlers["GET /tool-chat/jobs/job-1"] = () => packet({ job: job({ state: "COMPLETED", reservation_owned: true }) });
  await assert.rejects(f.tracker.check("A"), { code: "invalid_job_response" });
  assert.equal(f.tracker.isBlocked("A"), true); assert.equal(f.events.filter((e) => e.name === "completed").length, 0); f.tracker.dispose();
});
test("persist only unresolved metadata, not drafts, answers, paths or approvals", async () => {
  const f = fixture(); await f.waiting({ path: "F:\\PRIVATE", kind: "directory", recursive: true });
  const data = [...f.saved.values.values()].join("");
  for (const secret of ["question", "PRIVATE", "proposal-1", "timezone"]) assert.ok(!data.includes(secret));
  assert.match(data, /job-1/); f.tracker.dispose();
});
test("restored records need explicit GET, and old approvals/results stay hidden", async () => {
  const saved = storage(), first = fixture({ storage: saved }); await first.start(); first.tracker.dispose();
  const f = fixture({ storage: saved }); assert.equal(f.requests.length, 0); assert.equal(f.tracker.isBlocked("A"), true);
  f.jobs.set("job-1", job({ state: "COMPLETED" })); await f.tracker.check("A");
  assert.equal(f.tracker.isBlocked("A"), false); assert.equal(f.events.filter((e) => e.name === "completed").length, 0); f.tracker.dispose();
});
test("lost creation metadata survives reload without a new POST", async () => {
  const saved = storage(), first = fixture({ storage: saved }); first.handlers["POST /tool-chat/jobs"] = () => { throw new Error("lost"); };
  await assert.rejects(first.start()); first.tracker.dispose(); const f = fixture({ storage: saved });
  await assert.rejects(f.start(), { code: "session_unsettled" }); assert.equal(f.requests.length, 0); f.tracker.dispose();
});
test("listeners cannot mutate state or break execution; unsubscribe works", async () => {
  const f = fixture(); let count = 0;
  const off = f.tracker.on("state", () => { count += 1; throw new Error("consumer failed"); });
  await f.start(); assert.ok(count > 0); off(); const before = count; await f.tracker.check("A"); assert.equal(count, before);
  const snapshot = f.tracker.get("A"); snapshot.state = "COMPLETED"; assert.equal(f.tracker.get("A").state, "RUNNING"); f.tracker.dispose();
});
test("dispose stops timers and ignores a late read without forgetting unresolved work", async () => {
  const f = fixture(); await f.start(); const d = deferred(); f.handlers["GET /tool-chat/jobs/job-1"] = () => d.promise;
  const read = f.tracker.check("A"); await flush(); f.tracker.dispose();
  d.resolve(packet({ job: job({ state: "COMPLETED" }) })); await read;
  assert.equal(f.scheduled.pending.size, 0); assert.equal(f.tracker.isBlocked("A"), true);
  assert.equal(f.events.filter((e) => e.name === "completed").length, 0);
});
test("client rejects path traversal IDs and invalid input before Fetch", async () => {
  const f = fixture(); await assert.rejects(f.client.get(".."), { code: "invalid_job_id" });
  await assert.rejects(f.tracker.start({ message: " " }), { code: "invalid_tool_chat_input" });
  assert.equal(f.requests.length, 0); f.tracker.dispose();
});
test("HTTP timeout is bounded, uncertain and never retransmits POST", async () => {
  const scheduled = timers(); let count = 0;
  const client = new ToolChatClient({ setTimer: scheduled.set, clearTimer: scheduled.clear,
    fetch: (_url, options) => { count += 1; return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })))); } });
  const request = client.create({ sessionId: "A", message: "q", browserGeneration: 1 });
  const assertion = assert.rejects(request, { code: "request_timeout", uncertain: true });
  await scheduled.next(); await assertion; assert.equal(count, 1); assert.equal(scheduled.pending.size, 0);
});

test("Cancel wins over an in-flight approval receipt and deduplicates concurrent clicks", async () => {
  const f = fixture(); await f.waiting(); const d = deferred(), c = deferred();
  f.handlers["POST /tool-chat/jobs/job-1/approval"] = () => d.promise;
  f.handlers["POST /tool-chat/jobs/job-1/cancel"] = () => c.promise;
  const approval = f.decide(), cancel = f.tracker.cancel("A");
  assert.equal(f.tracker.cancel("A"), cancel); await flush();
  c.resolve(packet({ job: job({ state: "CANCELLED" }) })); await cancel;
  d.resolve(packet({ job: job({ state: "WAITING_APPROVAL" }) }, 202)); await approval;
  assert.equal(f.tracker.get("A").state, "CANCELLED"); assert.equal(f.tracker.get("A").approval, null);
  assert.equal(f.count("POST", "/cancel"), 1); f.tracker.dispose();
});
test("failed Cancel is observed with GET; automatic polling never repeats the POST", async () => {
  const f = fixture(); await f.start();
  f.handlers["POST /tool-chat/jobs/job-1/cancel"] = () => { throw new Error("lost reply"); };
  await assert.rejects(f.tracker.cancel("A"), { code: "network_failure" });
  assert.equal(f.tracker.isBlocked("A"), true); await f.scheduled.next();
  assert.equal(f.count("POST", "/cancel"), 1); assert.equal(f.count("GET", "/job-1"), 1);
  assert.equal(f.tracker.get("A").cancelIntent, true); f.tracker.dispose();
});
test("unknown job states and changed backend revisions fail closed", async () => {
  for (const changes of [{ state: "UNKNOWN" }, { revision: 5 }]) {
    const f = fixture(); await f.start();
    f.handlers["GET /tool-chat/jobs/job-1"] = () => packet({ job: job(changes) });
    await assert.rejects(f.tracker.check("A"), { code: "invalid_job_response" });
    assert.equal(f.tracker.isBlocked("A"), true); assert.equal(f.tracker.get("A").backendRevision, 4);
    f.tracker.dispose();
  }
});
test("404 status read preserves unknown reservation and does not retry automatically", async () => {
  const f = fixture(); await f.start(); f.handlers["GET /tool-chat/jobs/job-1"] = () => failure("job_not_found", 404);
  await assert.rejects(f.tracker.check("A"), { code: "job_not_found", status: 404 });
  assert.equal(f.tracker.isBlocked("A"), true); assert.equal(f.scheduled.pending.size, 0); f.tracker.dispose();
});
test("automatic polling publishes its settled request state to event consumers", async () => {
  const f = fixture(); await f.start(); await f.scheduled.next();
  const last = f.events.filter((e) => e.name === "state").at(-1);
  assert.equal(last.event.polling, false); assert.equal(last.event.blocked, true); f.tracker.dispose();
});
test("explicit release verification retains a barrier on failure even after a settled Cancel receipt", async () => {
  const f = fixture(); await f.start(); await f.tracker.cancel("A"); assert.equal(f.tracker.isBlocked("A"), false);
  f.handlers["GET /tool-chat/jobs/job-1"] = () => { throw new Error("offline"); };
  await assert.rejects(f.tracker.check("A", { verifyRelease: true }), { code: "network_failure" });
  assert.equal(f.tracker.isBlocked("A"), true); assert.match([...f.saved.values.values()].join(""), /job-1/);
  delete f.handlers["GET /tool-chat/jobs/job-1"]; await f.tracker.check("A");
  assert.equal(f.tracker.isBlocked("A"), false); f.tracker.dispose();
});
test("unsubscribe remains safe after disposal", async () => {
  const f = fixture(); const off = f.tracker.on("state", () => {}); f.tracker.dispose(); off(); off();
});

(async () => {
  let failures = 0;
  for (const item of tests) {
    try { await item.run(); }
    catch (error) { failures += 1; process.stderr.write(`FAIL ${item.name}\n${error.stack || error}\n`); }
  }
  process.stdout.write(`${tests.length - failures}/${tests.length} Tool Chat UI tests passed\n`);
  process.exitCode = failures ? 1 : 0;
})();

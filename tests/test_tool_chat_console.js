"use strict";
// Runs actual main.js + client + DOM adapter against browser API doubles.
// No real DOM, MCP, Ollama, audio device or operational database.
const assert = require("assert").strict;
const { fixture, flush, deferred, packet, chooseSession, deleteFromList, deleteAllFromList } = require("./test_voice_ui.js");
const tests = [], test = (name, run) => tests.push({ name, run });
const count = (f, url, method) => f.requests.filter((r) => r.url === url && (!method || r.options.method === method)).length;
const text = (element) => element.textContent + element.children.map(text).join("\n");
const enter = (f, extra = {}) => f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {}, ...extra });
const jobUrl = "/tool-chat/jobs/job-1";
const p = (id = "p-1", scope = null) => ({ proposal_id: id, server_id: "server-1", server_name: "Filesystem",
  tool_name: "read_file", description: "read a file", arguments: { path: "F:\\PRIVATE\\note.txt" }, scope, turn_scoped: true });
async function setup(options = {}) {
  const f = await fixture(options); let state;
  f.handlers["/tool-chat/jobs"] = (request) => {
    const data = JSON.parse(request.body);
    state = { job_id: "job-1", session_id: data.session_id, browser_generation: data.browser_generation,
      revision: 7, state: "RUNNING", phase: "CREATED", reservation_owned: true, unsettled: false,
      cancelled: false, operation_id: null, pending: null, result: null, error_code: null };
    return packet({ job: state }, 202);
  };
  f.handlers[jobUrl] = () => packet({ job: state });
  f.handlers[jobUrl + "/approval"] = (request) => {
    const data = JSON.parse(request.body);
    Object.assign(state, { state: data.allow ? "RUNNING" : "CANCELLED", pending: null,
      cancelled: !data.allow, reservation_owned: data.allow });
    return packet({ job: state }, 202);
  };
  f.handlers[jobUrl + "/cancel"] = () => {
    Object.assign(state, { state: "CANCELLED", cancelled: true, reservation_owned: false, pending: null });
    return packet({ job: state });
  };
  const check = async () => { f.nodes["tool-chat-check"].click(); await flush(); };
  return Object.assign(f, {
    state: () => state,
    async start() { f.unlock(); f.nodes["tool-chat-mode"].click(); f.input("message", "question"); enter(f); await flush(); },
    check,
    async waiting(proposal = p()) { Object.assign(state, { state: "WAITING_APPROVAL", pending: proposal }); await check(); },
    async complete(answer = "final answer") {
      Object.assign(state, { state: "COMPLETED", pending: null, reservation_owned: false, unsettled: false,
        result: { session_id: state.session_id, message: answer, model: "qwen3.5:9b", revision: 8,
          metrics: { eval_count: 4 }, elapsed_seconds: 1 } });
      f.serverHistories.set(state.session_id, [{ role: "user", content: "question" }, { role: "assistant", content: answer, language: "ko" }]);
      await check();
    },
  });
}

test("OFF preserves /chat; changing mode alone never creates a Job", async () => {
  const f = await setup(); assert.equal(f.nodes["tool-chat-mode"].getAttribute("aria-pressed"), "false");
  f.unlock(); f.input("message", "normal"); enter(f); await flush();
  assert.equal(count(f, "/chat"), 1); assert.equal(count(f, "/tool-chat/jobs"), 0);
  f.nodes["tool-chat-mode"].click(); await flush(); assert.equal(count(f, "/tool-chat/jobs"), 0);
});
test("ON Enter creates exactly one Job with selected language, not Chat or TTS", async () => {
  const f = await setup(); f.nodes["tts-language"].value = "en"; await f.start();
  assert.equal(count(f, "/tool-chat/jobs"), 1); assert.equal(count(f, "/chat"), 0); assert.equal(count(f, "/voice/synthesize"), 0);
  const request = f.requests.find((r) => r.url === "/tool-chat/jobs");
  assert.equal(JSON.parse(request.options.body).response_language, "en");
  assert.equal(request.options.headers["X-Local-AI-Control"], "web-console");
  assert.equal(f.nodes.message.disabled, true); assert.match(f.nodes["tool-chat-output-status"].textContent, /Running/);
  assert.ok(!text(f.nodes["conversation-output"]).includes("final answer"));
});
test("Shift Enter, composing IME, Locked and over-length input never send Tool Chat", async () => {
  const f = await setup(); f.nodes["tool-chat-mode"].click(); f.input("message", "question");
  enter(f); await flush(); assert.equal(count(f, "/tool-chat/jobs"), 0);
  f.unlock(); enter(f, { shiftKey: true }); enter(f, { isComposing: true });
  f.input("message", "x".repeat(10001)); enter(f); await flush(); assert.equal(count(f, "/tool-chat/jobs"), 0);
});
test("two Enter events in one tick cannot duplicate creation or user question", async () => {
  const f = await setup(); f.unlock(); f.nodes["tool-chat-mode"].click(); f.input("message", "question");
  enter(f); enter(f); await flush(); assert.equal(count(f, "/tool-chat/jobs"), 1);
  assert.equal(f.nodes["conversation-output"].querySelectorAll(".is-user").length, 1);
});
test("COMPLETED renders once then synchronizes authoritative History; manual Play remains", async () => {
  const f = await setup(); await f.start(); await f.complete(); await f.check();
  assert.equal(f.nodes["conversation-output"].querySelectorAll(".is-assistant").length, 1);
  assert.match(text(f.nodes["conversation-output"]), /final answer/);
  assert.ok(count(f, "/sessions/default/history") >= 1);
  assert.ok(f.nodes["conversation-output"].querySelector(".tts-play-button"));
  assert.equal(count(f, "/voice/synthesize"), 0); assert.equal(f.nodes.message.disabled, false);
});
test("approval displays exact root-wide recursive scope safely and is never automatic", async () => {
  const f = await setup(); await f.start();
  const scope = { path: "F:\\PRIVATE", kind: "directory", recursive: true }, proposal = p("p-1", scope);
  proposal.description = "<script>throw 1</script>"; await f.waiting(proposal);
  assert.equal(f.nodes["tool-chat-dialog"].open, true);
  assert.equal(f.nodes["tool-chat-approval-description"].textContent, proposal.description);
  assert.equal(f.nodes["tool-chat-approval-description"].children.length, 0);
  assert.deepEqual(JSON.parse(f.nodes["tool-chat-approval-scope"].textContent), scope);
  assert.match(f.nodes["tool-chat-approval-notice"].textContent, /ROOT-WIDE.*includes subdirectories: YES/);
  assert.equal(count(f, jobUrl + "/approval"), 0);
  const logs = text(f.nodes["output-log"]); assert.ok(!logs.includes("PRIVATE")); assert.ok(!logs.includes("throw 1"));
});
test("Allow sends exact displayed scope once and prevents duplicate clicks", async () => {
  const f = await setup(); await f.start(); const scope = { path: "F:\\PRIVATE", kind: "directory", recursive: true };
  await f.waiting(p("p-1", scope)); const d = deferred(); f.handlers[jobUrl + "/approval"] = () => d.promise;
  f.nodes["tool-chat-allow"].click(); f.nodes["tool-chat-allow"].click(); await flush();
  assert.equal(count(f, jobUrl + "/approval"), 1);
  const sent = JSON.parse(f.requests.find((r) => r.url === jobUrl + "/approval").options.body);
  assert.deepEqual(sent.scope, scope); assert.equal(sent.allow, true);
  Object.assign(f.state(), { state: "RUNNING", pending: null }); d.resolve(packet({ job: f.state() }, 202)); await flush();
  assert.equal(f.nodes["tool-chat-dialog"].open, false);
});
test("Close and Esc only hide; explicit review reopens; Deny posts false", async () => {
  const f = await setup(); await f.start(); await f.waiting();
  f.nodes["tool-chat-approval-close"].click(); assert.equal(count(f, jobUrl + "/approval"), 0);
  f.nodes["tool-chat-check"].click(); assert.equal(f.nodes["tool-chat-dialog"].open, true);
  f.nodes["tool-chat-dialog"].dispatchEvent({ type: "cancel", preventDefault() {} });
  assert.equal(count(f, jobUrl + "/approval"), 0); f.nodes["tool-chat-check"].click(); f.nodes["tool-chat-deny"].click(); await flush();
  assert.equal(JSON.parse(f.requests.find((r) => r.url === jobUrl + "/approval").options.body).allow, false);
  assert.match(f.nodes["tool-chat-status"].textContent, /Cancelled/);
});
test("proposal replacement invalidates old ticket and only the new proposal can be allowed", async () => {
  const f = await setup(); await f.start(); await f.waiting();
  Object.assign(f.state(), { pending: p("p-2") });
  // Poll directly: Review button intentionally reopens current already-known proposal.
  const timer = [...f.timers.values()].find((t) => t.ms === 1000); assert.ok(timer); timer.callback(); await flush();
  assert.equal(f.nodes["tool-chat-dialog"].open, false);
  f.nodes["tool-chat-allow"].click(); await flush(); assert.equal(count(f, jobUrl + "/approval"), 0);
  f.nodes["tool-chat-check"].click(); assert.equal(f.nodes["tool-chat-dialog"].open, true);
  f.nodes["tool-chat-allow"].click(); await flush();
  const sent = JSON.parse(f.requests.find((r) => r.url === jobUrl + "/approval").options.body);
  assert.equal(sent.proposal_id, "p-2");
});
test("Cancel with reservation blocks Chat, Reset, Delete and Delete All until confirmed release", async () => {
  const f = await setup(); await f.start();
  f.handlers[jobUrl + "/cancel"] = () => { Object.assign(f.state(), { state: "CANCELLED", cancelled: true, reservation_owned: true }); return packet({ job: f.state() }); };
  f.nodes["tool-chat-cancel"].click(); await flush();
  assert.equal(f.nodes.message.disabled, true); assert.equal(f.nodes["reset-session"].disabled, false);
  f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
  f.nodes["tool-chat-mode"].click(); enter(f); await flush(); assert.equal(count(f, "/chat"), 0);
  await deleteFromList(f, "default"); await deleteAllFromList(f);
  assert.equal(count(f, "/sessions/default", "DELETE"), 0); assert.equal(count(f, "/sessions", "DELETE"), 0);
  Object.assign(f.state(), { reservation_owned: false }); await f.check(); assert.equal(f.nodes.message.disabled, false);
});
test("FAILED and UNCERTAIN never render final answers; uncertainty stays blocked", async () => {
  for (const state of ["FAILED", "UNCERTAIN"]) {
    const f = await setup(); await f.start(); Object.assign(f.state(), { state, unsettled: state === "UNCERTAIN", reservation_owned: state === "UNCERTAIN", error_code: "job_timeout" });
    await f.check(); assert.equal(f.nodes["conversation-output"].querySelectorAll(".is-assistant").length, 0);
    assert.match(f.nodes["tool-chat-status"].textContent, new RegExp(state === "FAILED" ? "Failed" : "UNCERTAIN"));
    assert.equal(f.nodes.message.disabled, state === "UNCERTAIN");
  }
});
test("A to B to A cancels previous job and late completion never appends its answer", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] }); await f.start();
  const d = deferred(); f.handlers[jobUrl] = () => d.promise; const checked = f.nodes["tool-chat-check"];
  checked.click(); await flush(); await chooseSession(f, "B"); await chooseSession(f, "default");
  const old = { ...f.state(), state: "COMPLETED", reservation_owned: false, pending: null,
    result: { session_id: "default", message: "LATE SECRET", model: "qwen3.5:9b", revision: 8, metrics: {}, elapsed_seconds: 1 } };
  d.resolve(packet({ job: old })); await flush();
  assert.ok(!text(f.nodes["conversation-output"]).includes("LATE SECRET")); assert.equal(count(f, jobUrl + "/cancel"), 1);
});
test("Tool mode blocks automatic Voice but manual STT can fill Tool Chat input", async () => {
  const f = await setup(); f.unlock(); f.nodes["tool-chat-mode"].click(); f.nodes["voice-mode"].click();
  assert.equal(f.nodes["voice-mode"].getAttribute("aria-pressed"), "false"); await f.recordStop();
  assert.equal(f.nodes.message.value, "recognized"); assert.equal(count(f, "/tool-chat/jobs"), 0);
  enter(f); await flush(); assert.equal(count(f, "/tool-chat/jobs"), 1);
});
test("automatic Voice blocks enabling Tool mode and creates no Tool Chat", async () => {
  const f = await setup(); f.unlock(); f.enable(); f.nodes["tool-chat-mode"].click();
  assert.equal(f.nodes["tool-chat-mode"].getAttribute("aria-pressed"), "false");
  await f.recordStop(); assert.equal(count(f, "/tool-chat/jobs"), 0); assert.equal(count(f, "/chat"), 1);
});
test("creation reply loss stays blocked across mode OFF and never retries POST", async () => {
  const f = await setup(); f.handlers["/tool-chat/jobs"] = () => { throw new Error("lost"); }; await f.start();
  assert.match(f.nodes["tool-chat-status"].textContent, /Job ID unavailable/);
  assert.equal(f.nodes["tool-chat-check"].disabled, true); f.nodes["tool-chat-mode"].click(); enter(f); await flush();
  assert.equal(count(f, "/tool-chat/jobs"), 1); assert.equal(count(f, "/chat"), 0); assert.equal(f.nodes.message.disabled, true);
});
test("Lock, screen navigation and mode OFF cancel without starting another Job", async () => {
  for (const action of ["console-lock", "server-button", "tool-chat-mode"]) {
    const f = await setup(); await f.start(); f.nodes[action].click(); await flush();
    assert.equal(count(f, jobUrl + "/cancel"), 1); assert.equal(count(f, "/tool-chat/jobs"), 1);
  }
});
test("API errors keep their code, no POST retries, and no Tool argument leaks to logs", async () => {
  for (const code of ["stale_approval", "session_reserved", "session_unsettled"]) {
    const f = await setup(); await f.start(); await f.waiting();
    f.handlers[jobUrl + "/approval"] = () => packet({ error: { code, message: "PRIVATE PATH" } }, 409);
    f.nodes["tool-chat-allow"].click(); await flush();
    assert.match(text(f.nodes["output-log"]), new RegExp(code)); assert.ok(!text(f.nodes["output-log"]).includes("PRIVATE PATH"));
    assert.equal(count(f, jobUrl + "/approval"), 1); assert.equal(f.nodes.message.disabled, true);
  }
});
test("reconcile is explicit, available only for MCP uncertainty and preserves operation_unresolved", async () => {
  const f = await setup(); await f.start(); Object.assign(f.state(), { state: "UNCERTAIN", unsettled: true, operation_id: null });
  await f.check(); assert.equal(f.nodes["tool-chat-reconcile"].hidden, true);
  Object.assign(f.state(), { operation_id: "op-1" }); await f.check(); assert.equal(f.nodes["tool-chat-reconcile"].hidden, false);
  f.handlers[jobUrl + "/reconcile"] = () => packet({ error: { code: "operation_unresolved", message: "not done" } }, 409);
  f.nodes["tool-chat-reconcile"].click(); await flush();
  assert.equal(count(f, jobUrl + "/reconcile"), 1); assert.equal(f.nodes.message.disabled, true);
  assert.match(text(f.nodes["output-log"]), /operation_unresolved/);
});

test("mode OFF immediately after Enter prevents creating a not-yet-started Job", async () => {
  const f = await setup(); f.unlock(); f.nodes["tool-chat-mode"].click(); f.input("message", "question");
  enter(f); f.nodes["tool-chat-mode"].click(); await flush();
  assert.equal(count(f, "/tool-chat/jobs"), 0); assert.equal(f.nodes.message.disabled, false);
});
test("Cancel or screen navigation immediately after Enter also invalidates pre-create work", async () => {
  for (const action of ["tool-chat-cancel", "server-button"]) {
    const f = await setup(); f.unlock(); f.nodes["tool-chat-mode"].click(); f.input("message", "question");
    enter(f); f.nodes[action].click(); await flush(); assert.equal(count(f, "/tool-chat/jobs"), 0);
  }
});
test("fast COMPLETED receipt synchronizes History instead of mistaking 202 alone for completion", async () => {
  const f = await setup(), create = f.handlers["/tool-chat/jobs"];
  f.handlers["/tool-chat/jobs"] = (request) => {
    create(request); Object.assign(f.state(), { state: "COMPLETED", reservation_owned: false,
      result: { session_id: "default", model: "qwen3.5:9b", message: "fast answer", revision: 8, metrics: {}, elapsed_seconds: 0.1 } });
    f.serverHistories.set("default", [{ role: "user", content: "question" }, { role: "assistant", content: "fast answer" }]);
    return packet({ job: f.state() }, 202);
  };
  await f.start(); assert.equal(f.nodes["conversation-output"].querySelectorAll(".is-assistant").length, 1);
  assert.match(text(f.nodes["conversation-output"]), /fast answer/);
  assert.ok(count(f, "/sessions/default/history") > 0);
});
test("completed answer uses text rendering, not HTML, and never auto-synthesizes", async () => {
  const f = await setup(); await f.start(); await f.complete('<img src=x onerror="alert(1)">');
  const message = f.nodes["conversation-output"].querySelector(".is-assistant").querySelector(".conversation-text");
  assert.equal(message.textContent, '<img src=x onerror="alert(1)">'); assert.equal(message.children.length, 0);
  assert.equal(count(f, "/voice/synthesize"), 0);
});
test("network failure during status check retains reservation without recreating the Job", async () => {
  const f = await setup(); await f.start(); f.handlers[jobUrl] = () => { throw new Error("offline"); };
  await f.check(); assert.match(text(f.nodes["output-log"]), /network_failure/);
  assert.equal(f.nodes.message.disabled, true); assert.equal(count(f, "/tool-chat/jobs"), 1);
});
test("known current job blocks automatic and manual Record even if UI handlers are triggered", async () => {
  const f = await setup(); await f.start(); f.nodes["record-voice"].dispatchEvent({ type: "click" });
  f.nodes["voice-mode"].dispatchEvent({ type: "click" }); await flush();
  assert.equal(f.recorders.length, 0); assert.equal(count(f, "/voice/transcribe"), 0);
  assert.equal(f.nodes["voice-mode"].getAttribute("aria-pressed"), "false");
});
test("switching sessions closes approval and old buttons cannot submit a decision", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] });
  await f.start(); await f.waiting(); await chooseSession(f, "B");
  assert.equal(f.nodes["tool-chat-dialog"].open, false); f.nodes["tool-chat-allow"].click(); await flush();
  assert.equal(count(f, jobUrl + "/approval"), 0);
});
test("page exit attempts cancellation and cleans client resources without retrying creation", async () => {
  const f = await setup(); await f.start(); await f.waiting(); f.window.beforeunload(); await flush();
  assert.equal(count(f, jobUrl + "/cancel"), 1); assert.equal(count(f, "/tool-chat/jobs"), 1);
  assert.equal(f.nodes["tool-chat-dialog"].open, false);
});

test("Reset of a waiting job cancels, freshly verifies release, then mutates only its session", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] });
  f.serverHistories.set("B", [{ role: "user", content: "B preserved" }]);
  await f.start(); await f.waiting(); f.nodes["reset-session"].click(); await flush();
  const paths = f.requests.map((r) => `${r.options.method} ${r.url}`);
  const cancel = paths.indexOf("POST " + jobUrl + "/cancel"), reset = paths.indexOf("POST /sessions/default/reset");
  assert.ok(cancel >= 0 && reset > cancel); assert.ok(paths.slice(cancel + 1, reset).includes("GET " + jobUrl));
  assert.equal(f.nodes["tool-chat-dialog"].open, false); assert.equal(f.serverHistories.get("B")[0].content, "B preserved");
  assert.equal(f.nodes["conversation-output"].querySelectorAll(".is-user").length, 0);
});
test("cancelled Reset/Delete confirmations send neither Cancel nor mutation", async () => {
  const f = await setup(); await f.start(); await f.waiting(); f.confirm(false);
  f.nodes["reset-session"].click(); await flush(); await deleteFromList(f, "default");
  assert.equal(count(f, jobUrl + "/cancel"), 0); assert.equal(count(f, "/sessions/default/reset"), 0);
  assert.equal(count(f, "/sessions/default", "DELETE"), 0); assert.equal(f.nodes["tool-chat-dialog"].open, true);
});
test("Delete waiting job observes release before deleting and never creates replacement", async () => {
  const f = await setup(); await f.start(); await f.waiting(); f.input("message", "keep draft"); await deleteFromList(f, "default");
  assert.equal(count(f, jobUrl + "/cancel"), 1); assert.equal(count(f, "/sessions/default", "DELETE"), 1);
  assert.equal(f.nodes["session-id"].value, ""); assert.equal(f.nodes.message.value, "keep draft");
  assert.equal(count(f, "/sessions", "POST"), 0); assert.equal(f.nodes["tool-chat-dialog"].open, false);
});
test("Cancel network failure prevents mutation; explicit retry remains possible", async () => {
  const f = await setup(); await f.start(); const original = f.handlers[jobUrl + "/cancel"];
  f.handlers[jobUrl + "/cancel"] = () => { throw new Error("offline"); };
  f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
  assert.equal(f.nodes.message.disabled, true); assert.equal(f.nodes["tool-chat-cancel"].disabled, false);
  assert.equal(count(f, jobUrl + "/cancel"), 1); f.handlers[jobUrl + "/cancel"] = original;
  f.nodes["tool-chat-cancel"].click(); await flush(); assert.equal(count(f, jobUrl + "/cancel"), 2);
  assert.equal(f.nodes.message.disabled, false);
});
test("UNCERTAIN work blocks Reset and Delete even if Cancel HTTP succeeds", async () => {
  const f = await setup(); await f.start(); Object.assign(f.state(), { state: "UNCERTAIN", unsettled: true, operation_id: "op-1" });
  await f.check(); f.handlers[jobUrl + "/cancel"] = () => packet({ job: f.state() });
  f.nodes["reset-session"].click(); await flush(); await deleteFromList(f, "default");
  assert.equal(count(f, "/sessions/default/reset"), 0); assert.equal(count(f, "/sessions/default", "DELETE"), 0);
  assert.equal(f.nodes.message.disabled, true); assert.equal(count(f, jobUrl + "/reconcile"), 0);
});
test("pre-Cancel polling cannot act as the required fresh pre-Reset verification", async () => {
  const f = await setup(); await f.start(); const d = deferred(); let reads = 0;
  f.handlers[jobUrl] = () => ++reads === 1 ? d.promise : packet({ job: f.state() });
  f.nodes["tool-chat-check"].click(); await flush(); f.nodes["reset-session"].click(); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 0);
  d.resolve(packet({ job: { ...f.state(), state: "RUNNING", reservation_owned: true, cancelled: false } })); await flush();
  assert.ok(reads >= 2); assert.equal(count(f, "/sessions/default/reset"), 1);
});
test("pending Cancel, duplicate Reset and Delete preserve Reset-before-Delete ordering", async () => {
  const f = await setup(); await f.start(); const cancel = deferred(), reset = deferred();
  f.handlers[jobUrl + "/cancel"] = () => cancel.promise;
  f.handlers["/sessions/default/reset"] = () => reset.promise;
  f.nodes["tool-chat-cancel"].click(); await flush(); f.nodes["reset-session"].click(); f.nodes["reset-session"].click();
  await deleteFromList(f, "default");
  Object.assign(f.state(), { state: "CANCELLED", cancelled: true, reservation_owned: false });
  cancel.resolve(packet({ job: f.state() })); await flush();
  assert.equal(count(f, jobUrl + "/cancel"), 1); assert.equal(count(f, "/sessions/default/reset"), 1);
  assert.equal(count(f, "/sessions/default", "DELETE"), 0);
  reset.resolve(packet({ status: "reset", session_id: "default" })); await flush();
  assert.equal(count(f, "/sessions/default", "DELETE"), 1);
});
test("switch Cancel failure leaves A blocked but B can complete an independent ordinary Chat", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] }); await f.start(); await f.waiting();
  f.handlers[jobUrl + "/cancel"] = () => { throw new Error("offline"); };
  await chooseSession(f, "B"); assert.equal(f.nodes["tool-chat-dialog"].open, false);
  f.nodes["tool-chat-mode"].click(); f.input("message", "B question"); enter(f); await flush();
  assert.equal(count(f, "/chat"), 1); assert.match(text(f.nodes["conversation-output"]), /AI answer/);
  await chooseSession(f, "default"); assert.equal(f.nodes.message.disabled, true);
  assert.equal(count(f, jobUrl + "/cancel"), 1);
});
test("known running and approval jobs restore with GET only and never restore approval buttons", async () => {
  for (const waiting of [false, true]) {
    const first = await setup(); await first.start(); if (waiting) await first.waiting();
    const snap = JSON.parse(JSON.stringify(first.state()));
    const f = await fixture({ savedStorage: first.storageValues, initialHandlers: { [jobUrl]: () => packet({ job: snap }) } });
    assert.equal(f.bootstrapRequests.filter((r) => r.url === jobUrl).length, 1);
    assert.ok(f.bootstrapRequests.every((r) => r.options.method === "GET"));
    assert.equal(f.nodes["tool-chat-dialog"].open, undefined); f.unlock(); assert.equal(f.nodes.message.disabled, true);
    assert.match(f.nodes["tool-chat-status"].textContent, /approval not restored/);
    assert.equal(f.nodes["tool-chat-mode"].getAttribute("aria-pressed"), "false");
  }
});
test("reload during Cancel keeps reservation and sends no automatic Cancel retry", async () => {
  const first = await setup(); await first.start(); const d = deferred(); first.handlers[jobUrl + "/cancel"] = () => d.promise;
  first.nodes["tool-chat-cancel"].click(); await flush();
  const snap = { ...first.state(), state: "CANCELLED", cancelled: true, reservation_owned: true };
  const f = await fixture({ savedStorage: first.storageValues, initialHandlers: { [jobUrl]: () => packet({ job: snap }) } });
  assert.ok(f.bootstrapRequests.every((r) => r.options.method === "GET")); f.unlock(); assert.equal(f.nodes.message.disabled, true);
  assert.equal(f.nodes["tool-chat-cancel"].disabled, false); d.resolve(packet({ job: snap })); await flush();
});
test("lost create reply remains blocked after reload without invented lookup or recreation", async () => {
  const first = await setup(); first.handlers["/tool-chat/jobs"] = () => { throw new Error("lost"); }; await first.start();
  const f = await fixture({ savedStorage: first.storageValues }); f.unlock();
  assert.equal(f.nodes.message.disabled, true); assert.equal(f.nodes["tool-chat-check"].disabled, true);
  assert.ok(f.bootstrapRequests.every((r) => !r.url.startsWith("/tool-chat")));
  f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
});
test("lost final response restores committed History once, not an old completion event", async () => {
  const first = await setup(); await first.start(); const snap = { ...first.state(), state: "COMPLETED", reservation_owned: false,
    result: { session_id: "default", message: "committed answer", model: "qwen3.5:9b", revision: 8, metrics: {}, elapsed_seconds: 1 } };
  const f = await fixture({ savedStorage: first.storageValues, initialHandlers: { [jobUrl]: () => packet({ job: snap }),
    "/sessions/default/history": () => packet({ session_id: "default", number: 1, title: "question",
      messages: [{ role: "user", content: "question" }, { role: "assistant", content: "committed answer", language: "ko" }] }) } });
  assert.equal(f.nodes["conversation-output"].querySelectorAll(".is-assistant").length, 1);
  assert.match(text(f.nodes["conversation-output"]), /committed answer/);
  assert.ok(f.bootstrapRequests.every((r) => r.options.method === "GET"));
});
test("404 or network failure during restored verification never releases a session", async () => {
  for (const handler of [() => packet({ error: { code: "job_not_found", message: "missing" } }, 404), () => { throw new Error("offline"); }]) {
    const first = await setup(); await first.start();
    const f = await fixture({ savedStorage: first.storageValues, initialHandlers: { [jobUrl]: handler } }); f.unlock();
    assert.equal(f.nodes.message.disabled, true); assert.ok(f.bootstrapRequests.every((r) => r.options.method === "GET"));
  }
});
test("Tool Chat final answer can be played manually without automatic audio", async () => {
  const f = await setup(); await f.start(); await f.complete(); assert.equal(count(f, "/voice/synthesize"), 0);
  const button = f.nodes["conversation-output"].querySelector(".tts-play-button");
  f.nodes["conversation-output"].dispatchEvent({ type: "click", target: button }); await flush();
  assert.equal(count(f, "/voice/synthesize"), 1); assert.equal(f.audios.length, 1);
});

test("Delete All cancels jobs in both sessions, refuses partial deletion, then succeeds after release", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] }); await f.start();
  const a = f.state(); let releaseA = false;
  f.handlers[jobUrl] = () => packet({ job: a });
  f.handlers[jobUrl + "/cancel"] = () => {
    Object.assign(a, { state: "CANCELLED", cancelled: true, reservation_owned: !releaseA, pending: null });
    return packet({ job: a });
  };
  await chooseSession(f, "B"); const create = f.handlers["/tool-chat/jobs"];
  f.handlers["/tool-chat/jobs"] = (request) => { create(request); f.state().job_id = "job-2"; return packet({ job: f.state() }, 202); };
  const bUrl = "/tool-chat/jobs/job-2";
  f.handlers[bUrl] = () => packet({ job: f.state() });
  f.handlers[bUrl + "/cancel"] = () => {
    Object.assign(f.state(), { state: "CANCELLED", cancelled: true, reservation_owned: false }); return packet({ job: f.state() });
  };
  f.input("message", "B question"); enter(f); await flush();
  assert.equal(count(f, "/tool-chat/jobs"), 2);
  await deleteAllFromList(f); assert.equal(count(f, "/sessions", "DELETE"), 0);
  assert.equal(count(f, bUrl + "/cancel"), 1); assert.equal(f.serverSessions.length, 2);
  releaseA = true; await deleteAllFromList(f); assert.equal(count(f, "/sessions", "DELETE"), 1);
  assert.equal(f.nodes["session-id"].value, ""); assert.equal(f.serverSessions.length, 0);
});
test("failed release verification prevents mutation even after successful Cancel", async () => {
  const f = await setup(); await f.start(); f.handlers[jobUrl] = () => { throw new Error("status offline"); };
  f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, jobUrl + "/cancel"), 1);
  assert.equal(count(f, "/sessions/default/reset"), 0);
  assert.match(text(f.nodes["output-log"]), /network_failure/);
  assert.equal(f.nodes.message.disabled, true);
  f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
});
test("manual STT cancellation in Tool mode discards late text after A to B to A", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] });
  f.unlock(); f.nodes["tool-chat-mode"].click(); const d = deferred(); f.handlers["/voice/transcribe"] = () => d.promise;
  f.nodes["record-voice"].click(); await flush(); f.nodes["record-voice"].click(); await flush();
  await chooseSession(f, "B"); await chooseSession(f, "default");
  d.resolve(packet({ text: "LATE STT", language: "ko", elapsed_seconds: 1 })); await flush();
  assert.ok(!f.nodes.message.value.includes("LATE STT")); assert.equal(count(f, "/tool-chat/jobs"), 0);
  assert.ok(f.tracks.every((track) => track.stopped > 0));
});

test("invalid path approval error remains visible after polling, with Cancel and other sessions usable", async () => {
  const f = await setup({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] }); await f.start();
  await f.waiting(p("p-1", { path: "F:\\PRIVATE", kind: "directory", recursive: true }));
  f.handlers[jobUrl + "/approval"] = () => packet({ error: { code: "filesystem_invalid_path", message: "private path" } }, 400);
  f.nodes["tool-chat-allow"].click(); await flush(); await f.check();
  assert.match(f.nodes["tool-chat-output-status"].textContent, /filesystem_invalid_path/);
  assert.match(f.nodes["tool-chat-output-status"].textContent, /Session reserved/);
  assert.equal(f.nodes["tool-chat-cancel"].disabled, false); assert.equal(f.nodes["tool-chat-check"].disabled, false);
  assert.equal(count(f, jobUrl + "/approval"), 1);
  f.nodes["tool-chat-cancel"].click(); await flush(); assert.equal(f.nodes.message.disabled, false);
  await chooseSession(f, "B"); f.nodes["tool-chat-mode"].click(); f.input("message", "B unaffected"); enter(f); await flush();
  assert.equal(count(f, "/chat"), 1);
});
test("each failed approval HTTP request logs once, without merging separate same-code requests", async () => {
  const f = await setup(); await f.start(); await f.waiting();
  f.handlers[jobUrl + "/approval"] = () => packet({ error: { code: "filesystem_invalid_path", message: "private" } }, 400);
  f.nodes["tool-chat-allow"].click(); await flush();
  assert.equal((text(f.nodes["output-log"]).match(/HTTP 400/g) || []).length, 1);
  await f.check(); f.nodes["tool-chat-check"].click(); f.nodes["tool-chat-allow"].click(); await flush();
  assert.equal(count(f, jobUrl + "/approval"), 2);
  assert.equal((text(f.nodes["output-log"]).match(/HTTP 400/g) || []).length, 2);
});
test("Reset preflight logs a Tracker-reported Cancel HTTP failure only once", async () => {
  const f = await setup(); await f.start();
  f.handlers[jobUrl + "/cancel"] = () => packet({ error: { code: "service_closed", message: "internal" } }, 503);
  f.nodes["reset-session"].click(); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 0);
  assert.equal((text(f.nodes["output-log"]).match(/HTTP 503/g) || []).length, 1);
  assert.equal(f.nodes["tool-chat-cancel"].disabled, false);
});
test("terminal invalid path failure stops polling and releases a known settled reservation", async () => {
  const f = await setup(); await f.start(); Object.assign(f.state(), { state: "FAILED", error_code: "filesystem_invalid_path", reservation_owned: false });
  await f.check(); assert.match(f.nodes["tool-chat-status"].textContent, /Failed.*filesystem_invalid_path/);
  assert.equal(f.nodes.message.disabled, false);
  assert.equal([...f.timers.values()].filter((t) => t.ms === 1000).length, 0);
});

(async () => {
  let failures = 0;
  for (const { name, run } of tests) {
    try { await run(); }
    catch (error) { failures += 1; console.error(`FAIL ${name}\n${error.stack}`); }
  }
  console.log(`${tests.length - failures}/${tests.length} Tool Chat console tests passed (browser API doubles)`);
  process.exitCode = failures ? 1 : 0;
})();

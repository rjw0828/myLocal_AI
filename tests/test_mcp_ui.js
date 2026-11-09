"use strict";
// Executes the real MCP UI handlers with DOM/Fetch doubles, not an Edge test.
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");
global.AbortController = class {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
};
const { createMCPConsole } = require("../static/js/mcp-ui.js");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.value = ""; this.hidden = false; this.disabled = false; this.open = false; this._text = ""; }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(""); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text = ""; this.children = items; }
  addEventListener(name, handler) { (this.listeners[name] = this.listeners[name] || []).push(handler); }
  querySelectorAll(tag) { return this.children.flatMap((item) => [...(item.tag === tag ? [item] : []), ...item.querySelectorAll(tag)]); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
  async fire(name = "click", force = false) {
    if (this.disabled && !force) return;
    this.lastEvent = { prevented: false, preventDefault() { this.prevented = true; } };
    for (const listener of this.listeners[name] || []) await listener(this.lastEvent);
  }
}
const deferred = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };
const flush = async () => { for (let i = 0; i < 25; i += 1) await Promise.resolve(); };
const packet = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
function fixture() {
  const html = fs.readFileSync(path.join(__dirname, "../templates/index.html"), "utf8"), nodes = {};
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"/g)) nodes[match[2]] = new Element(match[1]);
  const requests = [], prompts = [], handlers = {}, stored = new Map(), timers = [];
  const server = { server_id: "s1", name: "Local echo", profile_id: "local-echo", transport: "stdio", execution_approved: true, status: { state: "RUNNING" } };
  const info = { name: "echo", description: "Returns text", input_schema: { type: "object", properties: { text: { type: "string" } } }, allowed: false };
  let registrations = [server], allowConfirmation = true;
  const fetch = async (url, options) => {
    const data = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url, options, data });
    if (handlers[url]) return handlers[url](options, data);
    if (url === "/mcp/profiles") return packet({ profiles: [{ profile_id: "local-echo" }] });
    if (url === "/mcp/servers" && options.method === "GET") return packet({ servers: registrations });
    if (url === "/mcp/servers" && options.method === "POST") { server.status.state = "STOPPED"; return packet({ server }, 201); }
    if (url.endsWith("/start")) { server.status.state = "RUNNING"; return packet({ server }); }
    if (url.endsWith("/stop")) { server.status.state = "STOPPED"; return packet({ server }); }
    if (options.method === "DELETE") { registrations = []; return packet({ status: "deleted" }); }
    if (url.endsWith("/permission")) { info.allowed = data.allowed; return packet(data); }
    if (url.endsWith("/approve")) return packet({ approval_token: "one-use-token" });
    if (url.endsWith("/call")) return packet({ result: { content: [{ text: "echo result" }], is_error: false }, elapsed_seconds: .05 });
    if (url.includes("/operations/")) return packet({ operation: { state: "SUCCEEDED", result: { content: [{ text: "late answer" }] } } });
    if (url.endsWith("/tools")) return packet({ tools: [{ ...info }] });
    throw new Error(`Unexpected mock endpoint: ${url}`);
  };
  const ui = createMCPConsole({ document: { getElementById: (id) => nodes[id], createElement: (tag) => new Element(tag) }, fetch,
    confirm: (message) => { prompts.push(message); return allowConfirmation; },
    storage: { getItem: (key) => stored.get(key), setItem: (key, value) => stored.set(key, value) },
    setTimer: (callback) => { timers.push(callback); return timers.length; }, clearTimer() {} });
  const button = (container, label) => {
    const item = nodes[`mcp-${container}`].querySelectorAll("button").find((item) => item.textContent === label);
    assert.ok(item, `Missing button: ${label}; status=${nodes["mcp-message"].textContent}`); return item;
  };
  const ready = async () => { await ui.open(); await button("servers", "Tools").fire(); await button("tools", "Allow tool").fire(); await button("tools", "Select tool").fire(); nodes["mcp-arguments"].value = '{"text":"hi"}'; };
  return { ui, nodes, requests, prompts, handlers, server, info, stored, timers, button, ready, confirm: (value) => { allowConfirmation = value; } };
}
const tests = [];
const test = (name, run) => tests.push({ name, run });
const calls = (f, suffix) => f.requests.filter((item) => item.url.endsWith(suffix));
test("initialization is lazy; opening reads profiles and servers without starting", async () => {
  const f = fixture(); assert.equal(f.requests.length, 0); await f.ui.open();
  assert.deepEqual(f.requests.map((r) => r.url), ["/mcp/profiles", "/mcp/servers"]);
  assert.match(f.nodes["mcp-servers"].textContent, /Local echo.*local-echo.*stdio.*RUNNING/);
});
test("every MCP fetch preserves the console header and same-origin policy", async () => {
  const f = fixture(); await f.ready(); await f.nodes["mcp-execute"].fire();
  assert.ok(f.requests.every((r) => r.options.headers["X-Local-AI-Control"] === "web-console" && r.options.credentials === "same-origin"));
  assert.ok(f.requests.every((r) => r.url.startsWith("/mcp/")));
});
test("registration submits only the selected approved profile and name", async () => {
  const f = fixture(); await f.ui.open(); await f.nodes["mcp-add"].fire(); f.nodes["mcp-profile"].value = "local-echo"; f.nodes["mcp-name"].value = "Echo service";
  await f.nodes["mcp-register"].fire("submit");
  assert.deepEqual(f.requests.find((r) => r.options.method === "POST").data, { profile_id: "local-echo", name: "Echo service" });
  assert.equal(calls(f, "/start").length, 0);
  assert.equal(f.nodes["mcp-dialog"].open, false);
  assert.equal(f.requests[f.requests.length - 1].url, "/mcp/servers");
});
test("no approved profiles keeps registration disabled and explains local setup", async () => {
  const f = fixture(); f.handlers["/mcp/profiles"] = () => packet({ profiles: [] }); await f.ui.open();
  assert.equal(f.nodes["mcp-add"].disabled, false); await f.nodes["mcp-add"].fire();
  assert.equal(f.nodes["mcp-dialog"].open, true); assert.equal(f.nodes["mcp-submit"].disabled, true);
  assert.match(f.nodes["mcp-profile-message"].textContent, /LOCAL_AI_MCP_APPROVALS/);
});
test("Start/Stop/Delete respect lifecycle and delete cancellation", async () => {
  const f = fixture(); await f.ui.open(); assert.equal(f.button("servers", "Delete").disabled, true);
  await f.button("servers", "Stop").fire(); assert.equal(f.server.status.state, "STOPPED");
  await f.button("servers", "Start").fire(); assert.equal(f.server.status.state, "RUNNING");
  await f.button("servers", "Stop").fire(); f.confirm(false); await f.button("servers", "Delete").fire();
  assert.equal(f.requests.filter((r) => r.options.method === "DELETE").length, 0);
  f.confirm(true); await f.button("servers", "Delete").fire(); assert.match(f.nodes["mcp-servers"].textContent, /No registered/);
});
test("tools display live schema and default denial; grant/revoke are explicit", async () => {
  const f = fixture(); await f.ui.open(); await f.button("servers", "Tools").fire();
  assert.match(f.nodes["mcp-tools"].textContent, /Returns text.*properties.*Blocked/s);
  assert.equal(f.button("tools", "Select tool").disabled, true);
  await f.button("tools", "Allow tool").fire(); assert.equal(f.info.allowed, true);
  await f.button("tools", "Block tool").fire(); assert.equal(f.info.allowed, false);
});
test("confirmed manual call uses one approval and one call with identical arguments", async () => {
  const f = fixture(); await f.ready(); assert.equal(calls(f, "/call").length, 0); await f.nodes["mcp-execute"].fire();
  assert.equal(calls(f, "/approve").length, 1); assert.equal(calls(f, "/call").length, 1);
  assert.deepEqual(calls(f, "/approve")[0].data.arguments, calls(f, "/call")[0].data.arguments);
  assert.equal(calls(f, "/call")[0].data.approval_token, "one-use-token");
  assert.match(f.prompts[f.prompts.length - 1], /Local echo.*echo.*hi/s); assert.match(f.nodes["mcp-result"].textContent, /echo result/);
  assert.doesNotMatch(f.nodes["output-log"].textContent, /one-use-token|echo result|"text":"hi"/);
});
test("call confirmation cancellation never requests a token or executes", async () => {
  const f = fixture(); await f.ready(); f.confirm(false); await f.nodes["mcp-execute"].fire();
  assert.equal(calls(f, "/approve").length, 0); assert.equal(calls(f, "/call").length, 0);
});
test("invalid JSON, nonobjects and oversized arguments fail before approval", async () => {
  const f = fixture(); await f.ready();
  for (const value of ["{bad}", "[]", "null", JSON.stringify({ text: "a".repeat(65536) })]) {
    f.nodes["mcp-arguments"].value = value; await f.nodes["mcp-execute"].fire();
    assert.equal(calls(f, "/approve").length, 0); assert.equal(f.nodes["mcp-execute"].disabled, false);
  }
});
test("double click while approval is pending cannot duplicate a call", async () => {
  const f = fixture(), wait = deferred(); await f.ready();
  f.handlers["/mcp/servers/s1/tools/echo/approve"] = () => wait.promise;
  const first = f.nodes["mcp-execute"].fire(); await flush(); await f.nodes["mcp-execute"].fire("click", true);
  wait.resolve(packet({ approval_token: "once" })); await first;
  assert.equal(calls(f, "/approve").length, 1); assert.equal(calls(f, "/call").length, 1);
});
test("leaving during approval prevents dispatch, including leave/open ABA", async () => {
  const f = fixture(), wait = deferred(); await f.ready();
  f.handlers["/mcp/servers/s1/tools/echo/approve"] = () => wait.promise;
  const run = f.nodes["mcp-execute"].fire(); await flush(); f.ui.leave(); await f.ui.open();
  wait.resolve(packet({ approval_token: "stale" })); await run; assert.equal(calls(f, "/call").length, 0);
});
test("late tool discovery after navigation cannot populate another view", async () => {
  const f = fixture(), wait = deferred(); await f.ui.open(); f.handlers["/mcp/servers/s1/tools"] = () => wait.promise;
  const run = f.button("servers", "Tools").fire(); await flush(); f.ui.leave();
  wait.resolve(packet({ tools: [f.info] })); await run; assert.equal(f.nodes["mcp-tools"].children.length, 0);
});
test("HTTP 429/503 approval failure recovers controls without calling", async () => {
  for (const status of [429, 503]) {
    const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/approve"] = () => packet({ error: { code: "mcp_busy", message: "busy" } }, status);
    await f.nodes["mcp-execute"].fire(); assert.match(f.nodes["mcp-message"].textContent, new RegExp(`HTTP ${status}`));
    assert.equal(calls(f, "/call").length, 0); assert.equal(f.nodes["mcp-refresh"].disabled, false);
  }
});
test("timeout keeps call blocked; manual result check observes late completion", async () => {
  const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/call"] = () => packet({ error: { code: "mcp_timeout", operation_id: "op1", message: "pending" } }, 504);
  await f.nodes["mcp-execute"].fire(); assert.equal(f.nodes["mcp-execute"].disabled, true); assert.equal(f.nodes["mcp-operation"].hidden, false);
  await f.nodes["mcp-operation"].fire(); assert.match(f.nodes["mcp-result"].textContent, /late answer/);
  assert.equal(calls(f, "/call").length, 1);
});
test("lost call response remains blocked across refresh until explicit Stop", async () => {
  const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/call"] = () => { throw new TypeError("network"); };
  await f.nodes["mcp-execute"].fire(); await f.ui.refresh(); await f.button("servers", "Tools").fire(); await f.button("tools", "Select tool").fire();
  assert.equal(f.nodes["mcp-execute"].disabled, true); assert.match(f.stored.get("local-ai-mcp-pending"), /s1/);
  await f.button("servers", "Stop").fire(); assert.equal(f.stored.get("local-ai-mcp-pending"), "[]");
});
test("server tool error stays visible and is logged as an error", async () => {
  const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/call"] = () => packet({ result: { is_error: true, content: [{ text: "tool failed" }] } });
  await f.nodes["mcp-execute"].fire(); assert.match(f.nodes["mcp-result"].textContent, /tool failed/); assert.match(f.nodes["output-log"].textContent, /ERROR.*tool error/);
});
test("untrusted names/schema/results use text nodes without HTML execution", async () => {
  const f = fixture(); f.server.name = '<img src=x onerror="alert(1)">'; f.info.description = "<script>bad()</script>"; await f.ready();
  assert.match(f.nodes["mcp-tools"].textContent, /<script>/); assert.match(f.nodes["mcp-servers"].textContent, /<img/);
  assert.equal(f.nodes["mcp-tools"].querySelectorAll("script").length, 0);
});
test("invalid JSON response and browser timeout report errors and recover UI", async () => {
  for (const type of ["json", "timeout"]) {
    const f = fixture(); f.handlers["/mcp/profiles"] = () => type === "json" ? { ok: true, json: async () => { throw new SyntaxError(); } } : Promise.reject(Object.assign(new Error(), { name: "AbortError" }));
    await f.ui.open(); assert.match(f.nodes["mcp-message"].textContent, /Invalid JSON|Browser timeout/); assert.equal(f.nodes["mcp-refresh"].disabled, false);
  }
});
test("failed deletion retains the row and requires another explicit user action", async () => {
  const f = fixture(); f.server.status.state = "STOPPED"; await f.ui.open();
  f.handlers["/mcp/servers/s1"] = () => packet({ error: { code: "mcp_storage_error", message: "failed" } }, 503);
  await f.button("servers", "Delete").fire(); assert.match(f.nodes["mcp-servers"].textContent, /Local echo/); assert.match(f.nodes["mcp-message"].textContent, /503/);
});
test("missing approval token fails closed without a tool call", async () => {
  const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/approve"] = () => packet({});
  await f.nodes["mcp-execute"].fire(); assert.equal(calls(f, "/call").length, 0); assert.match(f.nodes["mcp-message"].textContent, /Missing MCP approval/);
});
test("UNCERTAIN refresh preserves timeout details and explicit late operation results", async () => {
  const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/call"] = () => {
    f.server.status.state = "UNCERTAIN";
    return packet({ error: { code: "mcp_timeout", operation_id: "op2", message: "still working" } }, 504);
  };
  await f.nodes["mcp-execute"].fire(); assert.match(f.nodes["mcp-result"].textContent, /still working/);
  f.handlers["/mcp/servers/s1/operations/op2"] = () => { f.server.status.state = "RUNNING"; return packet({ operation: { state: "SUCCEEDED", result: { content: [{ text: "late completed" }] } } }); };
  await f.nodes["mcp-operation"].fire(); assert.match(f.nodes["mcp-result"].textContent, /late completed/);
});
test("view change after call dispatch suppresses old results but observes completion", async () => {
  const f = fixture(), wait = deferred(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/call"] = () => wait.promise;
  const run = f.nodes["mcp-execute"].fire(); await flush(); f.ui.leave();
  wait.resolve(packet({ result: { is_error: false, content: [{ text: "old response" }] } })); await run;
  assert.doesNotMatch(f.nodes["mcp-result"].textContent, /old response/);
  assert.equal(f.stored.get("local-ai-mcp-pending"), "[]");
});
test("invalid successful call payload cannot clear the unknown-completion guard", async () => {
  const f = fixture(); await f.ready(); f.handlers["/mcp/servers/s1/tools/echo/call"] = () => packet({});
  await f.nodes["mcp-execute"].fire(); assert.equal(f.nodes["mcp-execute"].disabled, true);
  assert.match(f.nodes["mcp-message"].textContent, /Invalid MCP call result/);
});
test("Add Server opens modal; Cancel restores focus without a registration request", async () => {
  const f = fixture(); await f.ui.open(); const before = f.requests.length;
  assert.equal(f.nodes["mcp-dialog"].open, false); await f.nodes["mcp-add"].fire();
  assert.equal(f.nodes["mcp-dialog"].open, true); assert.equal(f.requests.length, before);
  await f.nodes["mcp-cancel"].fire(); assert.equal(f.nodes["mcp-dialog"].open, false);
  assert.equal(f.nodes["mcp-add"].focused, true); assert.equal(f.requests.length, before);
});
test("Escape cancel closes the dialog; navigating away also closes it", async () => {
  const f = fixture(); await f.ui.open(); await f.nodes["mcp-add"].fire();
  await f.nodes["mcp-dialog"].fire("cancel"); assert.equal(f.nodes["mcp-dialog"].open, false);
  assert.equal(f.nodes["mcp-dialog"].lastEvent.prevented, true);
  await f.nodes["mcp-add"].fire(); f.ui.leave(); assert.equal(f.nodes["mcp-dialog"].open, false);
});
test("registration failure stays in the modal and restores Register and Cancel", async () => {
  const f = fixture(); await f.ui.open(); await f.nodes["mcp-add"].fire(); f.nodes["mcp-profile"].value = "local-echo";
  f.handlers["/mcp/servers"] = (options) => options.method === "POST" ? packet({ error: { code: "mcp_duplicate", message: "already registered" } }, 409) : packet({ servers: [f.server] });
  await f.nodes["mcp-register"].fire("submit"); assert.equal(f.nodes["mcp-dialog"].open, true);
  assert.match(f.nodes["mcp-register-message"].textContent, /409.*already registered/);
  assert.equal(f.nodes["mcp-submit"].disabled, false); assert.equal(f.nodes["mcp-cancel"].disabled, false);
});
test("duplicate submit and Escape during registration cannot interrupt or repeat the POST", async () => {
  const f = fixture(), wait = deferred(); await f.ui.open(); await f.nodes["mcp-add"].fire(); f.nodes["mcp-profile"].value = "local-echo";
  f.handlers["/mcp/servers"] = (options) => options.method === "POST" ? wait.promise : packet({ servers: [f.server] });
  const first = f.nodes["mcp-register"].fire("submit"); await flush();
  await f.nodes["mcp-register"].fire("submit"); await f.nodes["mcp-cancel"].fire(); await f.nodes["mcp-dialog"].fire("cancel");
  assert.equal(f.nodes["mcp-dialog"].open, true); assert.equal(f.nodes["mcp-submit"].disabled, true);
  wait.resolve(packet({ server: f.server }, 201)); await first;
  assert.equal(f.requests.filter((r) => r.url === "/mcp/servers" && r.options.method === "POST").length, 1);
  assert.equal(f.nodes["mcp-dialog"].open, false);
});
test("successful POST waits for list refresh before closing the modal", async () => {
  const f = fixture(), wait = deferred(); await f.ui.open(); await f.nodes["mcp-add"].fire(); f.nodes["mcp-profile"].value = "local-echo";
  f.handlers["/mcp/servers"] = (options) => options.method === "POST" ? packet({ server: f.server }, 201) : wait.promise;
  const run = f.nodes["mcp-register"].fire("submit"); await flush(); assert.equal(f.nodes["mcp-dialog"].open, true);
  wait.resolve(packet({ servers: [{ ...f.server, name: "Newly registered" }] })); await run;
  assert.equal(f.nodes["mcp-dialog"].open, false); assert.match(f.nodes["mcp-servers"].textContent, /Newly registered/);
});
test("refresh failure after successful registration blocks a duplicate until resynchronization", async () => {
  const f = fixture(); await f.ui.open(); await f.nodes["mcp-add"].fire(); f.nodes["mcp-profile"].value = "local-echo";
  f.handlers["/mcp/servers"] = (options) => options.method === "POST" ? packet({ server: f.server }, 201) : packet({ error: { code: "mcp_storage_error", message: "list failed" } }, 503);
  await f.nodes["mcp-register"].fire("submit"); assert.equal(f.nodes["mcp-dialog"].open, true);
  assert.equal(f.nodes["mcp-submit"].disabled, true); await f.nodes["mcp-register"].fire("submit");
  assert.equal(f.requests.filter((r) => r.options.method === "POST").length, 1);
  await f.nodes["mcp-cancel"].fire(); delete f.handlers["/mcp/servers"]; await f.ui.refresh();
  await f.nodes["mcp-add"].fire(); f.nodes["mcp-profile"].value = "local-echo"; await f.nodes["mcp-profile"].fire("change");
  assert.equal(f.nodes["mcp-submit"].disabled, false);
});
test("tool discovery updates the small hint and never replaces the section title", async () => {
  const f = fixture(); f.nodes["mcp-tools-title"].textContent = "Selected MCP Tools";
  await f.ui.open(); assert.equal(f.nodes["mcp-tools-hint"].textContent, "Select a running server to discover tools.");
  await f.button("servers", "Tools").fire(); assert.match(f.nodes["mcp-tools-hint"].textContent, /Server: Local echo/);
  assert.equal(f.nodes["mcp-tools-title"].textContent, "Selected MCP Tools");
  await f.ui.refresh(); assert.equal(f.nodes["mcp-tools-hint"].textContent, "Select a running server to discover tools.");
  assert.equal(f.nodes["mcp-tools-title"].textContent, "Selected MCP Tools");
});
(async () => {
  let failures = 0;
  for (const { name, run } of tests) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { failures += 1; console.error(`FAIL ${name}\n${error.stack}`); }
  }
  console.log(`${tests.length - failures}/${tests.length} MCP UI tests passed (DOM/Fetch doubles)`);
  process.exitCode = failures ? 1 : 0;
})();

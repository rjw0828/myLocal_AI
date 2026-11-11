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
const schemaEditor = require("../static/js/mcp-schema.js");
class Element {
  constructor(tag) { this.tag = tag; this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.value = ""; this.hidden = false; this.disabled = false; this.open = false; this._text = ""; this.checked = false; }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(""); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text = ""; this.children = items; }
  addEventListener(name, handler) { (this.listeners[name] = this.listeners[name] || []).push(handler); }
  querySelectorAll(tag) { return this.children.flatMap((item) => [...(item.tag === tag ? [item] : []), ...item.querySelectorAll(tag)]); }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
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
  const requests = [], prompts = [], handlers = {}, stored = new Map(), timers = [], copied = [];
  const clipboard = { writeText: async (value) => { copied.push(value); } };
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
    confirm: (message) => { prompts.push(message); return allowConfirmation; }, clipboard,
    storage: { getItem: (key) => stored.get(key), setItem: (key, value) => stored.set(key, value) },
    setTimer: (callback) => { timers.push(callback); return timers.length; }, clearTimer() {} });
  const button = (container, label) => {
    const item = nodes[`mcp-${container}`].querySelectorAll("button").find((item) => item.textContent === label);
    assert.ok(item, `Missing button: ${label}; status=${nodes["mcp-message"].textContent}`); return item;
  };
  const ready = async () => { await ui.open(); await button("servers", "Tools").fire(); await button("tools", "echo").fire(); await button("tools", "Allow tool").fire(); await button("tools", "echo").fire(); nodes["mcp-raw-mode"].checked = true; await nodes["mcp-raw-mode"].fire("change"); nodes["mcp-arguments"].value = '{"text":"hi"}'; };
  return { ui, nodes, requests, prompts, handlers, server, info, stored, timers, clipboard, copied, button, ready, confirm: (value) => { allowConfirmation = value; } };
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
  for (const text of [/Returns text/, /properties/, /Blocked/]) assert.match(f.nodes["mcp-tools"].textContent, text);
  await f.button("tools", "echo").fire(); assert.equal(f.nodes["mcp-execute"].disabled, true);
  await f.button("tools", "Allow tool").fire(); assert.equal(f.info.allowed, true);
  await f.button("tools", "echo").fire();
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
  await f.nodes["mcp-execute"].fire(); await f.ui.refresh(); await f.button("servers", "Tools").fire(); await f.button("tools", "echo").fire();
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
const autoReady = async (f) => { await f.ready(); f.nodes["mcp-raw-mode"].checked = false; await f.nodes["mcp-raw-mode"].fire("change"); };
const field = (f, name) => ["input", "select", "textarea"].flatMap((tag) => f.nodes["mcp-auto-form"].querySelectorAll(tag)).find((item) => item.getAttribute("aria-label") === name);
test("automatic form sends timezone default without reading stale raw JSON", async () => {
  const f = fixture(); f.info.input_schema = { type: "object", properties: { timezone: { type: "string", default: "Asia/Seoul" } } };
  await autoReady(f); f.nodes["mcp-arguments"].value = "not JSON";
  assert.equal(field(f, "timezone").value, "Asia/Seoul"); await f.nodes["mcp-execute"].fire();
  assert.deepEqual(calls(f, "/call")[0].data.arguments, { timezone: "Asia/Seoul" });
});
test("automatic number/integer/boolean/enum retain JSON types", async () => {
  const f = fixture(); f.info.input_schema = { type: "object", properties: { count: { type: "integer", default: 2, minimum: 1 }, ratio: { type: "number", default: .5 }, enabled: { type: "boolean", default: false }, zone: { type: "string", enum: ["ko", "en"], default: "en" } } };
  await autoReady(f); await f.nodes["mcp-execute"].fire();
  assert.deepEqual(calls(f, "/call")[0].data.arguments, { count: 2, ratio: .5, enabled: false, zone: "en" });
});
test("invalid integer, number range and required text block approval", async () => {
  const f = fixture(); f.info.input_schema = { type: "object", properties: { count: { type: "integer", minimum: 0 }, text: { type: "string", minLength: 1 } }, required: ["count", "text"] };
  await autoReady(f);
  for (const [number, text] of [["1.5", "ok"], ["-1", "ok"], ["", "ok"], ["1", ""]]) {
    field(f, "count").value = number; field(f, "text").value = text; await f.nodes["mcp-execute"].fire();
  }
  assert.equal(calls(f, "/approve").length, 0);
});
test("optional values are omitted unless included; defaults are included", async () => {
  const f = fixture(); f.info.input_schema = { type: "object", properties: { optional: { type: "string" }, included: { type: "boolean", default: false } } };
  await autoReady(f); await f.nodes["mcp-execute"].fire();
  assert.deepEqual(calls(f, "/call")[0].data.arguments, { included: false });
});
test("nested objects and typed JSON arrays produce structured arguments", async () => {
  const f = fixture(); f.info.input_schema = { type: "object", properties: { nested: { type: "object", properties: { label: { type: "string", default: "x" } }, required: ["label"] }, numbers: { type: "array", items: { type: "integer" } } }, required: ["nested", "numbers"] };
  await autoReady(f); field(f, "numbers").value = "[1,2]"; await f.nodes["mcp-execute"].fire();
  assert.deepEqual(calls(f, "/call")[0].data.arguments, { nested: { label: "x" }, numbers: [1, 2] });
  field(f, "numbers").value = '["wrong"]'; await f.nodes["mcp-execute"].fire(); assert.equal(calls(f, "/call").length, 1);
});
test("unsupported union/ref schemas fall back to advanced JSON", async () => {
  const f = fixture(); f.info.input_schema = { type: "object", oneOf: [{ required: ["text"] }, { required: ["number"] }] };
  await f.ready(); assert.equal(f.nodes["mcp-raw-mode"].checked, true); assert.equal(f.nodes["mcp-raw-mode"].disabled, true);
  assert.equal(f.nodes["mcp-raw-panel"].hidden, false); assert.match(f.nodes["mcp-form-notice"].textContent, /Advanced Raw JSON/);
  assert.equal(schemaEditor.supported({ type: "object", properties: { x: { $ref: "#/x" } } }), false);
});
test("tool name toggles details and selecting another closes the previous tool", async () => {
  const f = fixture(); f.info.allowed = true;
  f.handlers["/mcp/servers/s1/tools"] = () => packet({ tools: [f.info, { ...f.info, name: "second" }] });
  await f.ui.open(); await f.button("servers", "Tools").fire(); const rows = f.nodes["mcp-tools"].querySelectorAll("article");
  assert.ok(rows.every((row) => row.querySelectorAll("div")[0].hidden));
  await f.button("tools", "echo").fire(); assert.equal(f.button("tools", "echo").getAttribute("aria-expanded"), "true");
  await f.button("tools", "second").fire(); assert.equal(rows[0].querySelectorAll("div")[0].hidden, true);
  await f.button("tools", "second").fire(); assert.equal(f.nodes["mcp-call-panel"].hidden, true); assert.equal(f.nodes["mcp-execute"].disabled, true);
});
test("schema refresh invalidates previous form data and current permission display", async () => {
  const f = fixture(); await autoReady(f); field(f, "text").value = "old value";
  f.info.input_schema = { type: "object", properties: { timezone: { type: "string", default: "UTC" } } }; f.info.allowed = false;
  await f.ui.refresh(); assert.equal(f.nodes["mcp-auto-form"].children.length, 0); assert.equal(f.nodes["mcp-execute"].disabled, true);
  await f.button("servers", "Tools").fire(); await f.button("tools", "echo").fire();
  assert.equal(field(f, "text"), undefined); assert.equal(field(f, "timezone").value, "UTC"); assert.equal(f.nodes["mcp-execute"].disabled, true);
  assert.equal(calls(f, "/approve").length, 0);
});
test("general and per-tool Help show safe descriptions, required inputs and examples", async () => {
  const f = fixture(); await f.ui.open(); const before = f.requests.length;
  await f.nodes["mcp-help"].fire(); assert.equal(f.nodes["mcp-help-dialog"].open, true); assert.match(f.nodes["mcp-help-content"].textContent, /120 seconds/);
  await f.nodes["mcp-help-close"].fire(); assert.equal(f.nodes["mcp-help-dialog"].open, false); assert.equal(f.requests.length, before);
  f.info.description = "<script>not executed</script>"; f.info.input_schema.required = ["text"];
  await f.button("servers", "Tools").fire();
  assert.equal(f.nodes["mcp-argument-help"].disabled, true);
  assert.equal(f.nodes["mcp-tools"].querySelectorAll("button").filter((item) => item.textContent === "?").length, 0);
  await f.button("tools", "echo").fire(); await f.nodes["mcp-argument-help"].fire();
  assert.match(f.nodes["mcp-help-content"].textContent, /Required arguments: text/); assert.match(f.nodes["mcp-help-content"].textContent, /Example only/);
  assert.equal(f.nodes["mcp-help-content"].querySelectorAll("script").length, 0);
});
test("execution presents a text summary with full JSON collapsed", async () => {
  const f = fixture(); await f.ready(); await f.nodes["mcp-execute"].fire();
  assert.equal(f.nodes["mcp-result-summary"].textContent, "echo result"); assert.equal(f.nodes["mcp-result-details"].open, false);
  assert.match(f.nodes["mcp-result"].textContent, /content/);
});
const requestId = "a".repeat(32);
const requestRecord = { request_id: requestId, profile_id: "local-" + requestId, status: "pending_local_approval", approval_command: [".\\.venv\\Scripts\\python.exe", "scripts\\approve_mcp_profile.py", requestId] };
const newProfile = async (f) => {
  await f.ui.open(); await f.nodes["mcp-add"].fire(); f.nodes["mcp-registration-mode"].value = "new"; await f.nodes["mcp-registration-mode"].fire("change");
  f.nodes["mcp-name"].value = "Time test"; f.nodes["mcp-transport"].value = "stdio"; f.nodes["mcp-executable"].value = "F:\\MCP\\python.exe";
  f.nodes["mcp-launch-arguments"].value = "-m\nmcp_server_time\n--local-timezone=Asia/Seoul";
  f.handlers["/mcp/profile-requests"] = () => packet({ request: requestRecord }, 201);
};
test("new-profile argument edge whitespace blocks submission and identifies original lines", async () => {
  for (const value of ["index.js ", " index.js", "\tindex.js", "index.js\u00a0", "   "]) {
    const f = fixture(); await newProfile(f);
    f.nodes["mcp-launch-arguments"].value = "\n" + value + "\n--read-only";
    await f.nodes["mcp-register"].fire("submit");
    assert.match(f.nodes["mcp-register-message"].textContent, /Arguments lines 2: remove leading\/trailing whitespace/);
    assert.equal(calls(f, "/profile-requests").length, 0);
    assert.equal(f.nodes["mcp-dialog"].open, true);
    assert.equal(f.nodes["mcp-launch-arguments"].value, "\n" + value + "\n--read-only");
  }
});
test("new-profile internal spaces survive and corrected edge whitespace can be resubmitted", async () => {
  const f = fixture(); await newProfile(f);
  f.nodes["mcp-launch-arguments"].value = "F:\\Folder With Spaces\\index.js ";
  await f.nodes["mcp-register"].fire("submit");
  assert.equal(calls(f, "/profile-requests").length, 0);
  f.nodes["mcp-launch-arguments"].value = "F:\\Folder With Spaces\\index.js\n\nhello world";
  await f.nodes["mcp-register"].fire("submit");
  assert.deepEqual(calls(f, "/profile-requests")[0].data.arguments, ["F:\\Folder With Spaces\\index.js", "hello world"]);
});
test("approval copy is explicit and copies only the quoted review command without approving", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  assert.equal(f.nodes["mcp-approval-help"].hidden, false);
  assert.equal(f.copied.length, 0);
  const count = f.requests.length;
  await f.nodes["mcp-copy-approval"].fire();
  assert.deepEqual(f.copied, [f.nodes["mcp-local-approval"].textContent]);
  assert.match(f.copied[0], /^& '\.\\.venv/);
  assert.match(f.nodes["mcp-copy-status"].textContent, /복사되었습니다/);
  assert.equal(f.requests.length, count);
  assert.equal(f.nodes["mcp-submit"].textContent, "승인 확인 및 등록");
});
test("APPROVE copy uses the current request ID and never sends approval or Start", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  assert.equal(f.copied.length, 0);
  assert.equal(f.nodes["mcp-approval-confirmation"].textContent, "APPROVE " + requestId);
  const count = f.requests.length;
  await f.nodes["mcp-copy-confirmation"].fire();
  assert.deepEqual(f.copied, ["APPROVE " + requestId]);
  assert.match(f.nodes["mcp-copy-status"].textContent, /APPROVE 문구가 복사되었습니다/);
  assert.equal(f.requests.length, count);
  assert.equal(calls(f, "/start").length, 0);
});
test("APPROVE copy failure can be retried and duplicate clipboard writes are blocked", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  f.clipboard.writeText = async () => { throw new Error("denied"); };
  await f.nodes["mcp-copy-confirmation"].fire();
  assert.match(f.nodes["mcp-copy-status"].textContent, /직접 선택/);
  const pending = deferred(); let writes = 0;
  f.clipboard.writeText = () => { writes += 1; return pending.promise; };
  const copy = f.nodes["mcp-copy-confirmation"].fire();
  await f.nodes["mcp-copy-approval"].fire("click", true);
  assert.equal(writes, 1);
  pending.resolve(); await copy;
  assert.equal(f.nodes["mcp-copy-confirmation"].disabled, false);
});
test("clipboard rejection offers manual copy and keeps registration pending", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  f.clipboard.writeText = async () => { throw new Error("denied"); };
  await f.nodes["mcp-copy-approval"].fire();
  assert.match(f.nodes["mcp-copy-status"].textContent, /직접 선택/);
  assert.equal(f.nodes["mcp-copy-approval"].disabled, false);
  assert.equal(calls(f, "/start").length, 0);
});
test("late clipboard completion cannot report success after proposal cancellation", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  const pending = deferred(); f.clipboard.writeText = () => pending.promise;
  const copy = f.nodes["mcp-copy-approval"].fire();
  assert.equal(f.nodes["mcp-copy-approval"].disabled, true);
  await f.nodes["mcp-cancel"].fire(); pending.resolve(); await copy;
  assert.equal(f.nodes["mcp-approval-help"].hidden, true);
  assert.doesNotMatch(f.nodes["mcp-copy-status"].textContent, /복사되었습니다/);
});
test("new-profile proposal cannot register or start before local approval", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  assert.deepEqual(calls(f, "/profile-requests")[0].data.arguments, ["-m", "mcp_server_time", "--local-timezone=Asia/Seoul"]);
  assert.match(f.nodes["mcp-local-approval"].textContent, /approve_mcp_profile/); assert.equal(f.nodes["mcp-executable"].disabled, true);
  f.handlers["/mcp/profile-requests/" + requestId] = () => packet({ request: requestRecord });
  await f.nodes["mcp-register"].fire("submit");
  assert.match(f.nodes["mcp-register-message"].textContent, /still pending/);
  assert.equal(f.requests.filter((r) => r.url === "/mcp/servers" && r.options.method === "POST").length, 0);
  assert.equal(calls(f, "/start").length, 0);
});
test("locally approved profile uses the existing register API and no automatic Start", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  f.handlers["/mcp/profile-requests/" + requestId] = () => packet({ request: { ...requestRecord, status: "approved" } });
  await f.nodes["mcp-register"].fire("submit");
  assert.deepEqual(f.requests.find((r) => r.url === "/mcp/servers" && r.options.method === "POST").data, { profile_id: requestRecord.profile_id, name: "Time test" });
  assert.equal(f.nodes["mcp-dialog"].open, false); assert.equal(calls(f, "/start").length, 0);
});
test("cancel new-profile review never submits a proposal", async () => {
  const f = fixture(); await newProfile(f); f.confirm(false); await f.nodes["mcp-register"].fire("submit");
  assert.equal(calls(f, "/profile-requests").length, 0);
});
test("cancelling a queued profile removes only that proposal", async () => {
  const f = fixture(); await newProfile(f); await f.nodes["mcp-register"].fire("submit");
  f.handlers["/mcp/profile-requests/" + requestId] = () => packet({ status: "cancelled" });
  await f.nodes["mcp-cancel"].fire(); assert.equal(f.nodes["mcp-dialog"].open, false);
  assert.ok(f.requests.some((r) => r.url.endsWith(requestId) && r.options.method === "DELETE"));
  assert.equal(f.requests.filter((r) => r.url.startsWith("/mcp/servers") && r.options.method === "DELETE").length, 0);
});
test("new-profile validation errors stay visible without bypassing owner approval", async () => {
  const f = fixture(); await newProfile(f);
  f.handlers["/mcp/profile-requests"] = () => packet({ error: { code: "mcp_invalid_execution", message: "shell prohibited" } }, 400);
  await f.nodes["mcp-register"].fire("submit"); assert.match(f.nodes["mcp-register-message"].textContent, /shell prohibited/);
  assert.equal(f.nodes["mcp-dialog"].open, true); assert.equal(calls(f, "/approve").length, 0);
});
test("schema editor bounds depth and field count and rejects unsupported vocabulary", async () => {
  assert.equal(schemaEditor.supported({ type: "object", additionalProperties: { type: "string" } }), false);
  assert.equal(schemaEditor.supported({ type: "object", allOf: [] }), false);
  const properties = {}; for (let i = 0; i < 61; i += 1) properties["p" + i] = { type: "string" };
  assert.equal(schemaEditor.supported({ type: "object", properties }), false);
});
test("schema validator counts Unicode code points and finite numbers", async () => {
  assert.equal(schemaEditor.validate({ type: "string", maxLength: 1 }, "😀"), "😀");
  assert.throws(() => schemaEditor.validate({ type: "string", maxLength: 1 }, "😀가"));
  for (const number of [NaN, Infinity, -Infinity]) assert.throws(() => schemaEditor.validate({ type: "number" }, number));
});
test("prototype-shaped property names remain own JSON properties", async () => {
  const definition = JSON.parse('{"type":"object","properties":{"__proto__":{"type":"string","default":"safe"}},"required":["__proto__"]}');
  const container = new Element("div"); const form = schemaEditor.render({ createElement: (tag) => new Element(tag) }, container, definition);
  const value = form.read(); assert.equal(Object.prototype.hasOwnProperty.call(value, "__proto__"), true);
  assert.equal(value.__proto__, "safe"); assert.equal({}.safe, undefined);
});
test("schema auto fields respect required/default and optional boolean false", async () => {
  const container = new Element("div"), form = schemaEditor.render({ createElement: (tag) => new Element(tag) }, container,
    { type: "object", properties: { flag: { type: "boolean", default: false }, omitted: { type: "string" } } });
  assert.deepEqual(form.read(), { flag: false }); form.setDisabled(true);
  assert.ok(container.querySelectorAll("input").every((input) => input.disabled));
});
test("array item validation and object unknown-field rules are enforced", async () => {
  assert.throws(() => schemaEditor.validate({ type: "array", items: { type: "integer" } }, [1.5]));
  assert.throws(() => schemaEditor.validate({ type: "object", properties: {}, additionalProperties: false }, { extra: true }));
});
test("enum choices preserve numeric and boolean values instead of strings", async () => {
  const container = new Element("div"), form = schemaEditor.render({ createElement: (tag) => new Element(tag) }, container,
    { type: "object", properties: { value: { type: "number", enum: [0, 2], default: 0 } } });
  assert.deepEqual(form.read(), { value: 0 });
});
test("raw JSON cannot silently convert overflowing numbers to null", async () => {
  const f = fixture(); await f.ready(); f.nodes["mcp-arguments"].value = '{"text":1e309}';
  await f.nodes["mcp-execute"].fire(); assert.equal(calls(f, "/approve").length, 0); assert.match(f.nodes["mcp-message"].textContent, /finite numbers/);
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

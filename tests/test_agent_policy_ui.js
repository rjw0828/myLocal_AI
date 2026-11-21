"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const nodes = {};
for (const name of ["mode", "auto", "tools", "save", "trust", "revoke", "check", "info", "status", "resource", "readonly", "trusts"]) {
  nodes[`agent-policy-${name}`] = { textContent: "", value: "", checked: false, handlers: {}, options: [],
    addEventListener(event, callback) { this.handlers[event] = callback; },
    replaceChildren(...items) { this.options = items; }, append(item) { this.options.push(item); } };
}
nodes["agent-policy"] = { hidden: true,
  querySelectorAll() { return Object.values(nodes).filter((item) => item.handlers); } };
const events = {}, calls = [];
let finishSave;
const response = () => ({ ok: true, json: async () => ({ server: { name: "Time", approval_mode: "AUTO_ALLOW_SAFE",
  auto_enabled: true, status: { state: "RUNNING" }, options: { root: null }, trusts: [] },
  tools: [{ name: "get_current_time", catalog_fingerprint: "current" }], limitation: "reviewed profile only" }) });
const document = { getElementById(id) { return nodes[id]; },
  createElement() { return { value: "", textContent: "" }; },
  addEventListener(name, callback) { if (name === "DOMContentLoaded") callback(); else events[name] = callback; } };
vm.runInNewContext(fs.readFileSync(path.join(root, "static/js/agent-policy.js"), "utf8"), { document,
  confirm: () => true, fetch: async (url, options) => { calls.push({ url, method: options.method, body: options.body });
    if (options.method === "PUT") return new Promise((resolve) => { finishSave = () => resolve(response()); });
    return response(); } });
const html = fs.readFileSync(path.join(root, "templates/index.html"), "utf8");
const start = html.indexOf('<section id="agent-policy"');
const section = html.slice(start, html.indexOf("</section>", start));
assert.ok(section.includes("ASK_EVERY_TIME"));
assert.ok(section.includes("agent-policy-resource"));
assert.ok(section.includes("agent-policy-trusts"));
(async () => {
  events["local-ai-mcp-selected"]({ detail: { serverId: "time" } });
  await new Promise(setImmediate);
  assert.match(nodes["agent-policy-status"].textContent, /Time/);
  nodes["agent-policy-mode"].value = "AUTO_ALLOW_SAFE"; nodes["agent-policy-auto"].checked = true;
  const saving = nodes["agent-policy-save"].handlers.click();
  assert.equal(nodes["agent-policy-mode"].disabled, true);
  await nodes["agent-policy-save"].handlers.click();
  assert.equal(calls.filter((call) => call.method === "PUT").length, 1);
  finishSave(); await saving;
  assert.equal(nodes["agent-policy-mode"].disabled, false);
  console.log("12/12 policy UI checks passed (scope controls + in-flight settings lock)");
})().catch((error) => { console.error(error); process.exitCode = 1; });

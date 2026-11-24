"use strict";
const assert = require("assert").strict;
const { ModelSelector } = require("../static/js/model-selector.js");
const { fixture, flush, deferred, packet } = require("./test_voice_ui.js");
const tests = [], test = (name, run) => tests.push({ name, run });

class NodeDouble {
  constructor() { this.handlers = {}; this.children = []; this.value = ""; this.textContent = ""; }
  addEventListener(name, run) { this.handlers[name] = run; }
  append(node) { this.children.push(node); }
  replaceChildren() { this.children = []; }
}
global.document = { createElement: () => new NodeDouble() };
function setup() {
  const calls = [], selected = [];
  let data = { models: ["a:latest", "b:latest"], selected: "a:latest", busy: false };
  let handler = async (path, options) => path === "/models" ? data : { selected: JSON.parse(options.body).model };
  const select = new NodeDouble(), refresh = new NodeDouble(), message = new NodeDouble();
  const control = new ModelSelector({ select, refresh, message,
    request: async (path, options) => { calls.push({ path, options }); return handler(path, options); },
    onSelected: value => selected.push(value) });
  return { control, select, refresh, message, calls, selected, setData: next => { data = next; },
    handle: next => { handler = next; } };
}

test("refresh discovers newly installed models without changing the selection", async () => {
  const f = setup(); await f.control.refresh();
  assert.equal(f.select.value, "a:latest");
  f.setData({ models: ["a:latest", "b:latest", "new:8b"], selected: "a:latest", busy: false });
  await f.control.refresh();
  assert.deepEqual(f.select.children.map(o => o.value), ["a:latest", "b:latest", "new:8b"]);
  assert.ok(f.calls.every(c => c.options.method === "GET"));
});
test("selection sends one PUT and uses the confirmed server value", async () => {
  const f = setup(); await f.control.refresh(); f.select.value = "b:latest";
  await f.control.change();
  assert.equal(f.control.selected, "b:latest");
  assert.deepEqual(JSON.parse(f.calls[1].options.body), { model: "b:latest" });
});
test("duplicate change, refresh and Chat/Voice busy states cannot race a save", async () => {
  const f = setup(); await f.control.refresh();
  f.control.setBlocked(true); f.select.value = "b:latest"; await f.control.change();
  assert.equal(f.calls.length, 1); f.control.setBlocked(false);
  const pending = deferred(); f.handle(() => pending.promise);
  f.select.value = "b:latest"; const save = f.control.change();
  await f.control.change(); await f.control.refresh();
  assert.equal(f.calls.length, 2); assert.equal(f.select.disabled, true);
  pending.resolve({ selected: "b:latest" }); await save;
  assert.equal(f.select.disabled, false);
});
test("lost save response reconciles with GET instead of repeating PUT", async () => {
  const f = setup(); await f.control.refresh();
  f.handle(async (path) => {
    if (path === "/models/selection") throw new Error("lost response");
    return { models: ["a:latest", "b:latest"], selected: "b:latest", busy: false };
  });
  f.select.value = "b:latest"; await f.control.change();
  assert.equal(f.calls.filter(c => c.options.method === "PUT").length, 1);
  assert.equal(f.control.selected, "b:latest"); assert.equal(f.select.value, "b:latest");
});
test("missing selected model and empty installation never silently choose a replacement", async () => {
  const f = setup(); f.setData({ models: ["b:latest"], selected: "removed:latest", busy: false });
  await f.control.refresh(); assert.equal(f.select.value, "removed:latest");
  assert.equal(f.select.children[0].disabled, true);
  f.setData({ models: [], selected: "removed:latest", busy: false });
  await f.control.refresh(); assert.equal(f.select.disabled, true);
  assert.equal(f.calls.filter(c => c.options.method === "PUT").length, 0);
});
test("offline refresh and disposed late responses preserve state safely", async () => {
  const f = setup(); await f.control.refresh(); f.handle(() => { throw new Error("offline"); });
  await f.control.refresh(); assert.equal(f.control.selected, "a:latest");
  assert.match(f.message.textContent, /offline/);
  const pending = deferred(); f.handle(() => pending.promise); const read = f.control.refresh();
  f.control.dispose(); pending.resolve({ models: ["b:latest"], selected: "b:latest", busy: false });
  await read; assert.equal(f.control.selected, "a:latest"); assert.equal(f.select.disabled, true);
});
test("model names are rendered as text and remote busy disables selection", async () => {
  const f = setup(); const name = "<img src=x onerror=alert(1)>";
  f.setData({ models: [name], selected: name, busy: true }); await f.control.refresh();
  assert.equal(f.select.children[0].textContent, name); assert.equal(f.select.disabled, true);
});
test("actual main.js wiring preserves local headers and updates the configured model", async () => {
  let selected = "qwen3.5:9b";
  const f = await fixture({ modelsEnabled: true, initialHandlers: {
    "/models": () => packet({ models: ["qwen3.5:9b", "new:8b"], selected, busy: false }),
    "/models/selection": options => { selected = JSON.parse(options.body).model; return packet({ selected }); },
  } });
  f.nodes["refresh-models"].click(); await flush();
  f.nodes["model-select"].value = "new:8b";
  f.nodes["model-select"].dispatchEvent({ type: "change" }); await flush();
  assert.equal(f.nodes["configured-model"].textContent, "new:8b");
  for (const call of f.requests.filter(r => r.url.startsWith("/models"))) {
    assert.equal(call.options.headers["X-Local-AI-Control"], "web-console");
    assert.equal(call.options.cache, "no-store");
  }
});

(async () => {
  let failures = 0;
  for (const { name, run } of tests) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { failures++; console.error(`FAIL ${name}\n${error.stack}`); }
  }
  console.log(`${tests.length - failures}/${tests.length} model selector tests passed`);
  process.exitCode = failures ? 1 : 0;
})();

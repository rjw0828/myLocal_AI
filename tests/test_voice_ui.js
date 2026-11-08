"use strict";

// Lightweight browser API doubles, not a real DOM/audio test. Runs the actual
// shipped main.js event handlers and fetch/media cleanup without new packages.
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { VoiceTurnManager, ChatSessionGuard } = require("../static/js/voice-turn.js");
const flush = async () => { for (let i = 0; i < 150; i += 1) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
class Element {
  constructor(tag = "div") {
    this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.value = ""; this.textContent = ""; this.className = "";
    this.disabled = false; this.hidden = false; this.style = {};
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(" "), ...names])].join(" "); },
      remove: (...names) => { this.className = this.className.split(" ").filter((name) => !names.includes(name)).join(" "); },
      toggle: (name, enabled) => enabled ? this.classList.add(name) : this.classList.remove(name),
    };
  }
  append(...nodes) { nodes.forEach((node) => { node.parentElement = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(event, handler) { (this.listeners[event] = this.listeners[event] || []).push(handler); }
  dispatchEvent(event) {
    event.target = event.target || this;
    (this.listeners[event.type] || []).forEach((handler) => handler(event));
  }
  click() { if (!this.disabled) this.dispatchEvent({ type: "click", stopPropagation() {} }); }
  focus() {}
  getBoundingClientRect() { return { top: 500, left: 400, width: 140 }; }
  matches(selector) {
    if (selector.startsWith(".")) return this.className.split(" ").includes(selector.slice(1));
    const match = selector.match(/^\[data-message-id="(\d+)"\]$/);
    return Boolean(match && this.dataset.messageId === match[1]) || this.tag === selector;
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement && this.parentElement.closest(selector); }
  querySelectorAll(selector) {
    return this.children.reduce((all, child) => all.concat(child.matches(selector) ? [child] : [], child.querySelectorAll(selector)), []);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
}
class BlobDouble {
  constructor(parts, options = {}) {
    this.bytes = Buffer.concat(parts.map((part) => part instanceof BlobDouble ? part.bytes : Buffer.from(part)));
    this.size = this.bytes.length; this.type = options.type || "";
  }
  slice(start, end) { return new BlobDouble([this.bytes.slice(start, end)], { type: this.type }); }
  async arrayBuffer() { return Uint8Array.from(this.bytes).buffer; }
}
class AbortControllerDouble {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
}
const wav = Buffer.alloc(48);
wav.write("RIFF"); wav.write("WAVE", 8);
function packet(data, status = 200) {
  return { ok: status < 400, status,
    headers: { get: (name) => name === "Content-Type" ? "application/json" : "total;dur=1" },
    text: async () => JSON.stringify(data), json: async () => data,
  };
}
async function fixture({ emptySessions = false, savedSelection = null, extraSessions = [], savedDeleteAll = false } = {}) {
  const root = path.resolve(__dirname, "..");
  const html = fs.readFileSync(path.join(root, "templates/index.html"), "utf8");
  const nodes = {};
  for (const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Element(match[1]);
    node.disabled = /\bdisabled\b/.test(match[0]);
    node.hidden = /\bhidden\b/.test(match[0]);
    const pressed = match[0].match(/aria-pressed="([^"]+)"/);
    if (pressed) node.setAttribute("aria-pressed", pressed[1]);
    nodes[match[2]] = node;
  }
  nodes["session-id"].value = "";
  nodes["stt-language"].value = "auto"; nodes["tts-language"].value = "ko";
  nodes["session-popup"].append(nodes["delete-all-sessions"], nodes["session-popup-items"]);
  const tools = [...html.matchAll(/data-tool="([^"]+)"/g)].map((match) => {
    const node = new Element("button"); node.dataset.tool = match[1]; return node;
  });
  const requests = [], audios = [], revoked = [], tracks = [], recorders = [], confirmations = [];
  const timers = new Map(); let timer = 0, domReady, confirm = true;
  const handlers = {}, documentHandlers = {};
  const serverSessions = emptySessions ? [] : [{ session_id: "default", number: 1, title: "새 대화" }];
  const serverHistories = new Map(emptySessions ? [] : [["default", []]]);
  for (const detail of extraSessions) { serverSessions.push(detail); serverHistories.set(detail.session_id, []); }
  let nextServerNumber = Math.max(0, ...serverSessions.map((item) => item.number)) + 1;
  let nextServerId = nextServerNumber;
  let microphone = () => Promise.resolve({ getTracks: () => {
    if (!tracks.length) tracks.push({ stopped: 0, stop() { this.stopped += 1; }, addEventListener() {} });
    return tracks;
  } });
  class Recorder {
    constructor(stream) { this.stream = stream; this.state = "inactive"; this.mimeType = "audio/wav"; this.handlers = {}; recorders.push(this); }
    static isTypeSupported() { return false; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      if (this.handlers.dataavailable) this.handlers.dataavailable({ data: new BlobDouble([wav], { type: "audio/wav" }) });
      if (this.handlers.stop) this.handlers.stop();
    }
  }
  class AudioDouble {
    constructor(url) { this.url = url; this.paused = 0; audios.push(this); }
    async play() { if (handlers.play) await handlers.play(); }
    pause() { this.paused += 1; }
    removeAttribute() {}
    load() {}
  }
  const values = new Map();
  if (savedSelection) values.set("local-ai-selected-session", savedSelection);
  if (savedDeleteAll) values.set("local-ai-delete-all-uncertain", "true");
  const window = {
    innerWidth: 1280, innerHeight: 800,
    MediaRecorder: Recorder,
    sessionStorage: { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) },
    setTimeout: (callback, ms) => { timers.set(++timer, { callback, ms }); return timer; },
    clearTimeout: (id) => timers.delete(id),
    setInterval: (callback, ms) => { timers.set(++timer, { callback, ms }); return timer; },
    clearInterval: (id) => timers.delete(id),
    addEventListener: (name, handler) => { window[name] = handler; },
    confirm: (message) => { confirmations.push(message); return confirm; },
  };
  const fetch = async (url, options) => {
    requests.push({ url, options });
    if (handlers[url]) return handlers[url](options);
    if (url === "/voice/transcribe") return packet({ text: "recognized", language: "ko", elapsed_seconds: .01 });
    if (url === "/chat") return packet({ message: "AI answer", elapsed_seconds: .01, metrics: {} });
    if (url === "/sessions" && options.method === "GET") return packet({ sessions: serverSessions.slice() });
    if (url === "/sessions" && options.method === "DELETE") {
      const ids = serverSessions.map((item) => item.session_id);
      serverSessions.length = 0; serverHistories.clear(); nextServerNumber = 1;
      return packet({ status: "deleted_all", deleted_count: ids.length, deleted_session_ids: ids });
    }
    if (url === "/sessions" && options.method === "POST") {
      const number = nextServerNumber++;
      const detail = { session_id: `new-session-${nextServerId++}`, number, title: "새 대화" };
      serverSessions.push(detail); serverHistories.set(detail.session_id, []);
      return packet(detail, 201);
    }
    if (options.method === "DELETE" && url.startsWith("/sessions/")) {
      const id = decodeURIComponent(url.split("/")[2]);
      const index = serverSessions.findIndex((item) => item.session_id === id);
      if (index < 0) return packet({ error: { code: "session_not_found", message: "missing" } }, 404);
      serverSessions.splice(index, 1); serverHistories.delete(id);
      return packet({ session_id: id, status: "deleted" });
    }
    if (url.endsWith("/history")) {
      const id = decodeURIComponent(url.split("/")[2]);
      const detail = serverSessions.find((item) => item.session_id === id);
      return detail ? packet({ ...detail, messages: serverHistories.get(id) || [] }) : packet({ error: { code: "session_not_found", message: "missing" } }, 404);
    }
    if (url.endsWith("/reset")) return packet({ session_id: "default", status: "reset" });
    if (url === "/voice/synthesize") return { ok: true, status: 200,
      headers: { get: (name) => name === "Content-Type" ? "audio/wav" : "piper;dur=1" },
      blob: async () => new BlobDouble([wav], { type: "audio/wav" }),
    };
    return packet({ controller: "ok", ollama_reachable: true, model: "qwen3.5:9b" });
  };
  window.fetch = fetch;
  const sandbox = { window, fetch, VoiceTurnManager, ChatSessionGuard,
    document: { addEventListener: (name, handler) => {
      if (name === "DOMContentLoaded") domReady = handler;
      else (documentHandlers[name] = documentHandlers[name] || []).push(handler);
    },
      querySelector: (selector) => nodes[selector.slice(1)],
      querySelectorAll: () => tools, createElement: (tag) => new Element(tag) },
    navigator: { mediaDevices: { getUserMedia: () => microphone() } },
    Audio: AudioDouble, Blob: BlobDouble, FormData: class { constructor() { this.values = {}; } append(k, v) { this.values[k] = v; } },
    AbortController: AbortControllerDouble, Event: class { constructor(type) { this.type = type; } },
    URL: { createObjectURL: () => "blob:test", revokeObjectURL: (url) => revoked.push(url) },
    performance: { now: () => Date.now() }, console, Intl, Uint8Array, ArrayBuffer, DataView,
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, "static/js/main.js"), "utf8"), sandbox);
  domReady();
  await flush();
  requests.length = 0; // Initial read-only Session List/History bootstrap is not a user action.
  return { nodes, tools, requests, audios, revoked, tracks, recorders, handlers, window, timers, confirmations,
    serverSessions, serverHistories,
    documentEvent(name, event) { (documentHandlers[name] || []).forEach((handler) => handler(event)); },
    microphone: (fn) => { microphone = fn; }, confirm: (value) => { confirm = value; },
    unlock() { nodes["console-lock"].click(); },
    enable() { nodes["voice-mode"].click(); },
    input(id, value) { nodes[id].value = value; nodes[id].dispatchEvent({ type: "input" }); },
    async recordStop() { nodes["record-voice"].click(); await flush(); nodes["record-voice"].click(); await flush(); },
  };
}
const tests = [];
const test = (name, run) => tests.push({ name, run });
const count = (f, url) => f.requests.filter((request) => request.url === url).length;
const openSessions = async (f) => { f.nodes["session-list-button"].click(); await flush(); };
const chooseSession = async (f, id) => {
  if (f.nodes["session-popup"].hidden) await openSessions(f);
  const item = f.nodes["session-popup-items"].children.find((node) => node.dataset.sessionId === id);
  assert.ok(item, `missing Session List item ${id}`);
  item.querySelector(".session-choice").click(); await flush();
};
const deleteFromList = async (f, id) => {
  if (f.nodes["session-popup"].hidden) await openSessions(f);
  const item = f.nodes["session-popup-items"].children.find((node) => node.dataset.sessionId === id);
  assert.ok(item, `missing delete row ${id}`);
  item.querySelector(".session-delete").click(); await flush();
};
const deleteAllFromList = async (f) => {
  if (f.nodes["session-popup"].hidden) await openSessions(f);
  f.nodes["delete-all-sessions"].click(); await flush();
};
const bulkCount = (f) => f.requests.filter((r) => r.url === "/sessions" && r.options.method === "DELETE").length;

test("popup internal scroll, scrollbar click/drag and wheel stay open; external scroll closes", async () => {
  const f = await fixture(); await openSessions(f);
  for (const target of [f.nodes["session-popup"], f.nodes["session-popup-items"], f.nodes["session-popup-items"].children[0]]) {
    f.window.scroll({ target });
    f.documentEvent("click", { target });
    f.documentEvent("pointerdown", { target });
    f.documentEvent("pointermove", { target });
    f.documentEvent("wheel", { target });
    assert.equal(f.nodes["session-popup"].hidden, false);
  }
  f.window.innerHeight = 180; f.window.innerWidth = 240;
  f.nodes["session-list-button"].getBoundingClientRect = () => ({ top: 35, left: 210, width: 140 });
  f.window.resize();
  assert.ok(parseInt(f.nodes["session-popup"].style.maxHeight, 10) <= 164);
  assert.ok(parseInt(f.nodes["session-popup-items"].style.maxHeight, 10) < 164);
  for (const target of [f.nodes.message, f.window]) {
    if (f.nodes["session-popup"].hidden) await openSessions(f);
    f.window.scroll({ target });
    assert.equal(f.nodes["session-popup"].hidden, true);
  }
});

test("Delete All confirmation shows count; cancellation changes no session or draft", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] });
  f.input("message", "keep draft"); f.confirm(false);
  await deleteAllFromList(f);
  assert.match(f.confirmations[0], /2개/);
  assert.equal(bulkCount(f), 0); assert.equal(f.serverSessions.length, 2);
  assert.equal(f.nodes["session-id"].value, "default"); assert.equal(f.nodes.message.value, "keep draft");
  assert.equal(f.nodes["new-chat"].disabled, false);
});

test("Delete All clears E and list, preserves D draft, never creates automatically, and New Chat is Session 1", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] });
  f.input("message", "keep draft"); await deleteAllFromList(f);
  assert.equal(bulkCount(f), 1); assert.equal(f.serverSessions.length, 0);
  assert.equal(f.nodes["session-id"].value, ""); assert.equal(f.nodes.message.value, "keep draft");
  assert.equal(f.nodes["conversation-output"].querySelectorAll(".tts-play-button").length, 0);
  assert.equal(f.requests.filter((r) => r.url === "/sessions" && r.options.method === "POST").length, 0);
  f.nodes["new-chat"].click(); await flush();
  assert.equal(f.serverSessions[0].number, 1);
  assert.notEqual(f.serverSessions[0].session_id, "default");
  assert.match(f.nodes["current-session"].textContent, /Session 1/);
});

test("known Delete All HTTP failures restore server History and leave individual Delete available", async () => {
  for (const status of [500, 503]) {
    const f = await fixture();
    f.serverHistories.set("default", [{ role: "user", content: "retained" }, { role: "assistant", content: "answer", language: "en" }]);
    f.handlers["/sessions"] = (options) => options.method === "DELETE"
      ? packet({ error: { code: "storage_error", message: "failed" } }, status)
      : packet({ sessions: f.serverSessions.slice() });
    await deleteAllFromList(f);
    assert.equal(f.serverSessions.length, 1); assert.equal(f.nodes["session-id"].value, "default");
    assert.equal(f.nodes["conversation-output"].children.length, 2);
    assert.equal(f.nodes["new-chat"].disabled, false);
    assert.equal(f.window.sessionStorage.getItem("local-ai-delete-all-uncertain"), "false");
    delete f.handlers["/sessions"]; await deleteFromList(f, "default");
    assert.equal(f.serverSessions.length, 0);
  }
});

test("unknown Delete All completion blocks mutation across reload and never retries", async () => {
  for (const invalidResponse of [false, true]) {
    const f = await fixture();
    f.handlers["/sessions"] = (options) => options.method !== "DELETE"
      ? packet({ sessions: f.serverSessions.slice() })
      : invalidResponse ? packet({ status: "bad" }) : Promise.reject(new Error("connection lost"));
    await deleteAllFromList(f);
    assert.equal(bulkCount(f), 1);
    assert.equal(f.nodes["new-chat"].disabled, true);
    assert.equal(f.nodes["reset-session"].disabled, true);
    assert.equal(f.window.sessionStorage.getItem("local-ai-delete-all-uncertain"), "true");
    f.nodes["new-chat"].click(); await deleteAllFromList(f);
    assert.equal(bulkCount(f), 1);
    const reloaded = await fixture({ savedDeleteAll: true });
    assert.equal(reloaded.nodes["new-chat"].disabled, true);
    assert.equal(reloaded.nodes["delete-all-sessions"].disabled, true);
  }
});

test("Delete All waits for manual/automatic Chat, ignores late answer and sends no TTS", async () => {
  for (const automatic of [false, true]) {
    const f = await fixture(), pending = deferred();
    f.handlers["/chat"] = () => pending.promise; f.unlock();
    if (automatic) { f.enable(); await f.recordStop(); }
    else { f.input("message", "draft"); f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} }); await flush(); }
    await deleteAllFromList(f); assert.equal(bulkCount(f), 0);
    pending.resolve(packet({ message: "late answer", metrics: {} })); await flush();
    assert.equal(bulkCount(f), 1); assert.equal(f.serverSessions.length, 0);
    assert.equal(f.nodes["session-id"].value, ""); assert.equal(count(f, "/voice/synthesize"), 0);
    assert.equal(f.nodes["conversation-output"].children.length, 1);
  }
});

test("Delete All cancels recording, manual/automatic STT, synthesis and speaking with resource cleanup", async () => {
  for (const stage of ["recording", "manual-stt", "auto-stt", "tts", "speaking"]) {
    const f = await fixture(), pending = deferred();
    if (stage.includes("stt")) f.handlers["/voice/transcribe"] = () => pending.promise;
    if (stage === "tts") f.handlers["/voice/synthesize"] = () => pending.promise;
    f.unlock(); if (stage !== "manual-stt") f.enable();
    if (stage === "recording") { f.nodes["record-voice"].click(); await flush(); }
    else await f.recordStop();
    await deleteAllFromList(f);
    if (stage.includes("stt")) pending.resolve(packet({ text: "late transcript", language: "ko" }));
    if (stage === "tts") pending.resolve({ ok: true, status: 200,
      headers: { get: () => "audio/wav" }, blob: async () => new BlobDouble([wav], { type: "audio/wav" }) });
    await flush();
    assert.equal(f.nodes["session-id"].value, ""); assert.equal(f.nodes.message.value, "");
    assert.ok(f.tracks.every((track) => track.stopped));
    if (stage === "recording") assert.equal(count(f, "/voice/transcribe"), 0);
    if (stage.includes("stt")) assert.equal(count(f, "/chat"), 0);
    assert.equal(f.audios.length, stage === "speaking" ? 1 : 0);
    if (stage === "speaking") { assert.ok(f.audios[0].paused); assert.equal(f.revoked.length, 1); }
  }
});

test("Delete All invalidates History taken before deletion, even after new Session 1 selection", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] }), history = deferred();
  f.handlers["/sessions/B/history"] = () => history.promise;
  await chooseSession(f, "B"); await deleteAllFromList(f);
  f.nodes["new-chat"].click(); await flush();
  history.resolve(packet({ session_id: "B", number: 2, title: "stale", messages: [{ role: "assistant", content: "stale", language: "en" }] }));
  await flush();
  assert.notEqual(f.nodes["session-id"].value, "B"); assert.match(f.nodes["current-session"].textContent, /Session 1/);
  assert.equal(f.nodes["conversation-output"].querySelectorAll(".tts-play-button").length, 0);
});

test("Delete All waits for Reset and blocks duplicate bulk/Create while server response is pending", async () => {
  const f = await fixture(), reset = deferred(), bulk = deferred();
  f.handlers["/sessions/default/reset"] = () => reset.promise;
  f.handlers["/sessions"] = (options) => options.method === "DELETE" ? bulk.promise : packet({ sessions: f.serverSessions.slice() });
  f.nodes["reset-session"].click(); await flush(); await deleteAllFromList(f);
  assert.equal(bulkCount(f), 0);
  reset.resolve(packet({ session_id: "default", status: "reset" })); await flush();
  assert.equal(bulkCount(f), 1);
  f.nodes["delete-all-sessions"].click(); f.nodes["new-chat"].click(); await flush();
  assert.equal(bulkCount(f), 1);
  assert.equal(f.requests.filter((r) => r.url === "/sessions" && r.options.method === "POST").length, 0);
  f.serverSessions.length = 0;
  bulk.resolve(packet({ status: "deleted_all", deleted_count: 1, deleted_session_ids: ["default"] })); await flush();
  assert.equal(f.nodes["session-id"].value, "");
});

test("uncertain existing Chat prevents Delete All request without weakening its session block", async () => {
  const f = await fixture(); f.unlock();
  f.handlers["/chat"] = () => Promise.reject(new Error("unknown completion"));
  f.input("message", "draft"); f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} }); await flush();
  await deleteAllFromList(f);
  assert.equal(bulkCount(f), 0); assert.equal(f.serverSessions.length, 1);
  assert.equal(f.nodes["record-voice"].disabled, true);
});

test("cancelled Delete All preview does not discard an in-flight successful Chat", async () => {
  const f = await fixture(), chat = deferred(), preview = deferred(); f.unlock(); f.confirm(false);
  f.handlers["/chat"] = () => chat.promise;
  f.input("message", "question"); f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} }); await flush();
  await openSessions(f);
  let previewRequested = false;
  f.handlers["/sessions"] = () => { previewRequested = true; return preview.promise; };
  f.nodes["delete-all-sessions"].click(); await flush(); assert.equal(previewRequested, true);
  chat.resolve(packet({ message: "keep successful answer", metrics: {} })); await flush();
  assert.equal(f.nodes["conversation-output"].children.length, 2);
  preview.resolve(packet({ sessions: f.serverSessions.slice() })); await flush();
  assert.equal(bulkCount(f), 0); assert.equal(f.nodes["conversation-output"].children.length, 2);
});

test("Delete selects next then previous, leaves last empty, and preserves draft", async () => {
  const f = await fixture({ extraSessions: [
    { session_id: "B", number: 2, title: "B title" }, { session_id: "C", number: 3, title: "C title" },
  ] });
  await chooseSession(f, "B"); f.input("message", "keep draft");
  await deleteFromList(f, "B");
  assert.equal(f.nodes["session-id"].value, "C");
  await deleteFromList(f, "C");
  assert.equal(f.nodes["session-id"].value, "default");
  await deleteFromList(f, "default");
  assert.equal(f.nodes["session-id"].value, "");
  assert.equal(f.nodes["record-voice"].disabled, true);
  assert.equal(f.nodes["reset-session"].disabled, true);
  assert.equal(f.nodes.message.value, "keep draft");
  assert.equal(f.requests.filter((r) => r.url === "/sessions" && r.options.method === "POST").length, 0);
  f.nodes["new-chat"].click(); await flush();
  assert.equal(f.nodes["session-id"].value, "new-session-4");
});

test("Delete confirmation names target; cancel and Delete clicks never select the row", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B title" }] });
  await openSessions(f); f.confirm(false);
  const row = f.nodes["session-popup-items"].children.find((node) => node.dataset.sessionId === "B");
  let stopped = false;
  row.querySelector(".session-delete").dispatchEvent({ type: "click", stopPropagation() { stopped = true; } });
  await flush();
  assert.equal(stopped, true);
  assert.match(f.confirmations[0], /Session 2 — B title/);
  assert.equal(f.nodes["session-id"].value, "default");
  assert.equal(count(f, "/sessions/B/history"), 0);
  assert.equal(f.requests.filter((r) => r.options.method === "DELETE").length, 0);
  assert.equal(f.serverSessions.length, 2);
});

test("deleting another session leaves current automatic playback running", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "other" }] });
  f.unlock(); f.enable(); await f.recordStop();
  await deleteFromList(f, "B");
  assert.equal(f.nodes["session-id"].value, "default");
  assert.match(f.nodes["voice-status"].textContent, /Speaking/);
  assert.equal(f.audios[0].paused, 0);
  assert.equal(f.revoked.length, 0);
  assert.equal(f.nodes["conversation-output"].children.length, 2);
});

test("Delete failure restores server History; unknown response remains blocked without retry", async () => {
  for (const mode of ["http", "network", "invalid"]) {
    const f = await fixture();
    f.serverHistories.set("default", [{ role: "user", content: "original" },
      { role: "assistant", content: "preserved", language: "en" }]);
    await chooseSession(f, "default");
    f.handlers["/sessions/default"] = () => {
      if (mode === "network") throw new Error("lost");
      if (mode === "invalid") return packet({ unexpected: true });
      return packet({ error: { code: "internal_error", message: "failed" } }, 500);
    };
    await deleteFromList(f, "default");
    assert.equal(f.nodes["session-id"].value, "default");
    assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "preserved");
    assert.equal(f.serverSessions.length, 1);
    if (mode !== "http") {
      await deleteFromList(f, "default");
      assert.equal(count(f, "/sessions/default"), 1);
      assert.equal(f.nodes["record-voice"].disabled, true);
    }
  }
});

test("Delete waits for manual or automatic Chat; late answer never restores deleted E", async () => {
  for (const automatic of [false, true]) {
    const f = await fixture(), pending = deferred();
    f.handlers["/chat"] = () => pending.promise;
    f.unlock();
    if (automatic) { f.enable(); await f.recordStop(); }
    else {
      f.input("message", "manual draft");
      f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
      await flush();
    }
    await deleteFromList(f, "default");
    assert.equal(count(f, "/sessions/default"), 0);
    pending.resolve(packet({ message: "late answer", metrics: {} })); await flush();
    assert.equal(count(f, "/sessions/default"), 1);
    assert.equal(f.nodes["session-id"].value, "");
    assert.equal(f.nodes["conversation-output"].children.length, 1);
    assert.equal(count(f, "/voice/synthesize"), 0);
    if (!automatic) assert.equal(f.nodes.message.value, "manual draft");
  }
});

test("Delete waits for an observed Reset before removing the same session", async () => {
  const f = await fixture(), reset = deferred();
  f.handlers["/sessions/default/reset"] = () => reset.promise;
  f.nodes["reset-session"].click(); await flush();
  await deleteFromList(f, "default");
  assert.equal(count(f, "/sessions/default"), 0);
  reset.resolve(packet({ session_id: "default", status: "reset" })); await flush();
  assert.equal(count(f, "/sessions/default"), 1);
  assert.equal(f.nodes["session-id"].value, "");
  assert.equal(f.serverSessions.length, 0);
});

test("Delete cancels recording, manual/automatic STT and late TTS without follow-up", async () => {
  for (const stage of ["recording", "manual-stt", "auto-stt", "tts", "speaking"]) {
    const f = await fixture(), pending = deferred();
    if (stage.includes("stt")) f.handlers["/voice/transcribe"] = () => pending.promise;
    if (stage === "tts") f.handlers["/voice/synthesize"] = () => pending.promise;
    f.unlock(); if (stage !== "manual-stt") f.enable();
    if (stage === "recording") { f.nodes["record-voice"].click(); await flush(); }
    else await f.recordStop();
    await deleteFromList(f, "default");
    if (stage.includes("stt")) pending.resolve(packet({ text: "late transcript", language: "ko", elapsed_seconds: 1 }));
    if (stage === "tts") pending.resolve({ ok: true, status: 200,
      headers: { get: () => "audio/wav" }, blob: async () => new BlobDouble([wav], { type: "audio/wav" }) });
    await flush();
    assert.equal(f.nodes["session-id"].value, "");
    assert.equal(f.nodes.message.value, "");
    assert.ok(f.tracks.every((track) => track.stopped));
    assert.equal(f.audios.length, stage === "speaking" ? 1 : 0);
    if (stage === "speaking") { assert.ok(f.audios[0].paused); assert.equal(f.revoked.length, 1); }
    if (stage === "recording") assert.equal(count(f, "/voice/transcribe"), 0);
    if (stage.includes("stt")) assert.equal(count(f, "/chat"), 0);
  }
});

test("A→B→A late History and stale List cannot revive a deleted session", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B" }] });
  const history = deferred();
  await chooseSession(f, "B");
  f.handlers["/sessions/default/history"] = () => history.promise;
  await chooseSession(f, "default");
  await deleteFromList(f, "default");
  history.resolve(packet({ session_id: "default", number: 1, title: "old A", messages: [
    { role: "user", content: "stale" }, { role: "assistant", content: "old A", language: "en" },
  ] })); await flush();
  f.handlers["/sessions"] = () => packet({ sessions: [
    { session_id: "default", number: 1, title: "stale" }, { session_id: "B", number: 2, title: "B" },
  ] });
  if (!f.nodes["session-popup"].hidden) f.nodes["session-list-button"].click();
  await openSessions(f);
  assert.equal(f.nodes["session-id"].value, "B");
  assert.equal(f.nodes["session-popup-items"].children.some((row) => row.dataset.sessionId === "default"), false);
});

test("unknown Chat blocks Delete too; known missing Delete reconciles selection", async () => {
  const f = await fixture();
  f.handlers["/chat"] = () => { throw new Error("unknown Chat completion"); };
  f.unlock(); f.enable(); await f.recordStop();
  await deleteFromList(f, "default");
  assert.equal(count(f, "/sessions/default"), 0);
  assert.equal(f.nodes["session-id"].value, "default");
  const g = await fixture();
  g.handlers["/sessions/default"] = () => packet({ error: { code: "session_not_found", message: "already absent" } }, 404);
  await deleteFromList(g, "default");
  assert.equal(g.nodes["session-id"].value, "");
});

test("D unlock and Enter send Chat without a C Chat menu, including after Health", async () => {
  const f = await fixture();
  assert.deepEqual(f.tools.map((button) => button.dataset.tool), ["health"]);
  assert.equal(f.nodes.message.disabled, true);
  f.unlock(); assert.equal(f.nodes.message.disabled, false);
  f.tools[0].click(); await flush();
  assert.equal(count(f, "/health"), 1);
  assert.equal(f.nodes.message.disabled, false);
  f.input("message", "direct Chat after Health");
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", shiftKey: true, preventDefault() {} });
  await flush(); assert.equal(count(f, "/chat"), 0);
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
  await flush(); assert.equal(count(f, "/chat"), 1); assert.equal(count(f, "/health"), 1);
  f.nodes["console-lock"].click();
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
  await flush(); assert.equal(count(f, "/chat"), 1);
});

test("E Reset targets the selected session while Locked and preserves other history", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B question" }] });
  f.serverSessions[0].title = "A question";
  f.serverHistories.set("default", [{ role: "user", content: "A question" },
    { role: "assistant", content: "A answer", language: "en" }]);
  f.serverHistories.set("B", [{ role: "user", content: "B question" },
    { role: "assistant", content: "B answer", language: "ko" }]);
  await chooseSession(f, "B");
  f.input("message", "draft kept");
  f.handlers["/sessions/B/reset"] = () => {
    f.serverHistories.set("B", []); f.serverSessions[1].title = "새 대화";
    return packet({ session_id: "B", status: "reset" });
  };
  assert.equal(f.nodes["reset-session"].disabled, false);
  f.nodes["reset-session"].click(); await flush();
  assert.equal(count(f, "/sessions/B/reset"), 1);
  assert.equal(count(f, "/sessions/default/reset"), 0);
  assert.equal(f.nodes["session-id"].value, "B");
  assert.match(f.nodes["current-session"].textContent, /Session 2 — 새 대화/);
  assert.equal(f.nodes.message.value, "draft kept");
  await chooseSession(f, "default");
  assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "A answer");
  await chooseSession(f, "B");
  assert.equal(f.nodes["conversation-output"].children.length, 1);
});

test("New Chat is in D, uses server ID, and preserves an existing draft", async () => {
  const f = await fixture();
  f.input("message", "unsent draft");
  f.nodes["new-chat"].click(); await flush();
  assert.equal(count(f, "/sessions"), 1);
  assert.equal(f.nodes["session-id"].value, "new-session-2");
  assert.match(f.nodes["current-session"].textContent, /Session 2/);
  assert.equal(f.nodes.message.value, "unsent draft");
  assert.equal(count(f, "/chat"), 0);
});

test("reload with no RAM sessions stays empty until New Chat; stale selected ID is ignored", async () => {
  const f = await fixture({ emptySessions: true, savedSelection: "from-before-restart" });
  assert.equal(f.nodes["session-id"].value, "");
  assert.equal(f.nodes["record-voice"].disabled, true);
  assert.equal(f.serverSessions.length, 0);
  assert.equal(f.nodes["conversation-output"].children.length, 1);
  f.nodes["new-chat"].click(); await flush();
  assert.equal(f.nodes["session-id"].value, "new-session-1");
});

test("reload restores a still-existing selected server session", async () => {
  const f = await fixture({ savedSelection: "B", extraSessions: [
    { session_id: "B", number: 2, title: "restored title" },
  ] });
  assert.equal(f.nodes["session-id"].value, "B");
  assert.match(f.nodes["current-session"].textContent, /Session 2 — restored title/);
  assert.equal(f.serverSessions.length, 2);
});

test("late History from a previous selection cannot overwrite the current session", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "second" }] });
  const slow = deferred();
  f.handlers["/sessions/B/history"] = () => slow.promise;
  await chooseSession(f, "B");
  await chooseSession(f, "default");
  slow.resolve(packet({ session_id: "B", number: 2, title: "second", messages: [
    { role: "user", content: "should stay hidden" },
  ] }));
  await flush();
  assert.equal(f.nodes["session-id"].value, "default");
  assert.equal(f.nodes["conversation-output"].children.length, 1);
});

test("History started before Reset cannot restore cleared messages or the old title", async () => {
  const f = await fixture(), history = deferred();
  const old = { session_id: "default", number: 1, title: "old title", messages: [
    { role: "user", content: "old question" },
    { role: "assistant", content: "old answer", language: "en" },
  ] };
  f.serverSessions[0].title = old.title;
  f.serverHistories.set("default", old.messages);
  await chooseSession(f, "default");
  f.handlers["/sessions/default/history"] = () => history.promise;
  await chooseSession(f, "default");
  f.handlers["/sessions/default/reset"] = () => {
    f.serverHistories.set("default", []); f.serverSessions[0].title = "새 대화";
    return packet({ session_id: "default", status: "reset" });
  };
  f.nodes["reset-session"].click(); await flush();
  assert.equal(f.nodes["conversation-output"].children.length, 1);
  history.resolve(packet(old)); await flush();
  assert.equal(f.nodes["conversation-output"].children.length, 1, "pre-Reset History must remain invalid after Reset finishes");
  assert.match(f.nodes["current-session"].textContent, /새 대화/);
  delete f.handlers["/sessions/default/history"];
  f.serverHistories.set("default", [{ role: "user", content: "new question after Reset" },
    { role: "assistant", content: "new server answer", language: "ko" }]);
  f.serverSessions[0].title = "new question after Reset";
  await chooseSession(f, "default");
  assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "new server answer");
});

test("History started during Reset across A→B→A is invalid after Reset success", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B title" }] });
  const reset = deferred(), history = deferred();
  const old = { session_id: "default", number: 1, title: "A old", messages: [
    { role: "user", content: "A before Reset" },
    { role: "assistant", content: "A old reply", language: "ko" },
  ] };
  f.handlers["/sessions/default/reset"] = () => reset.promise;
  f.nodes["reset-session"].click(); await flush();
  await chooseSession(f, "B");
  f.handlers["/sessions/default/history"] = () => history.promise;
  await chooseSession(f, "default");
  reset.resolve(packet({ session_id: "default", status: "reset" })); await flush();
  history.resolve(packet(old)); await flush();
  assert.equal(f.nodes["conversation-output"].children.length, 1);
  assert.match(f.nodes["current-session"].textContent, /새 대화/);
});

test("Reset A does not invalidate a pending B History or overwrite its draft", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B title" }] });
  const reset = deferred(), history = deferred();
  f.handlers["/sessions/default/reset"] = () => reset.promise;
  f.nodes["reset-session"].click(); await flush();
  f.handlers["/sessions/B/history"] = () => history.promise;
  await chooseSession(f, "B");
  f.input("message", "B unsent draft");
  reset.resolve(packet({ session_id: "default", status: "reset" })); await flush();
  history.resolve(packet({ session_id: "B", number: 2, title: "B title", messages: [
    { role: "user", content: "B question" },
    { role: "assistant", content: "B preserved", language: "en" },
  ] })); await flush();
  assert.equal(f.nodes["session-id"].value, "B");
  assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "B preserved");
  assert.equal(f.nodes.message.value, "B unsent draft");
});

test("cancelled Reset leaves a pending History valid and sends no Reset", async () => {
  const f = await fixture(), history = deferred();
  f.handlers["/sessions/default/history"] = () => history.promise;
  await chooseSession(f, "default");
  f.confirm(false); f.nodes["reset-session"].click(); await flush();
  history.resolve(packet({ session_id: "default", number: 1, title: "still valid", messages: [
    { role: "user", content: "question preserved" },
    { role: "assistant", content: "answer preserved", language: "en" },
  ] })); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 0);
  assert.equal(f.nodes["conversation-output"].children.length, 2);
  assert.match(f.nodes["current-session"].textContent, /still valid/);
});

test("failed Reset keeps cached messages; an unknown Reset remains blocked", async () => {
  for (const transportFailure of [false, true]) {
    const f = await fixture();
    f.serverSessions[0].title = "original";
    f.serverHistories.set("default", [{ role: "user", content: "original question" },
      { role: "assistant", content: "original answer", language: "en" }]);
    await chooseSession(f, "default");
    f.handlers["/sessions/default/reset"] = () => {
      if (transportFailure) throw new Error("connection lost");
      return packet({ error: { code: "test_failure", message: "Reset failed" } }, 503);
    };
    f.nodes["reset-session"].click(); await flush();
    assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "original answer");
    assert.match(f.nodes["current-session"].textContent, /original/);
    if (transportFailure) {
      f.nodes["reset-session"].click(); await flush();
      assert.equal(count(f, "/sessions/default/reset"), 1, "uncertain Reset must not be retried");
    }
  }
});

test("Reset and A→B→A discard late automatic STT, Chat and TTS results", async () => {
  for (const stage of ["transcribing", "thinking", "synthesizing"]) {
    const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B intact" }] });
    const pending = deferred();
    f.serverHistories.set("B", [{ role: "user", content: "B question" },
      { role: "assistant", content: "B answer", language: "en" }]);
    const path = stage === "transcribing" ? "/voice/transcribe" : stage === "thinking" ? "/chat" : "/voice/synthesize";
    f.handlers[path] = () => pending.promise;
    f.unlock(); f.enable(); await f.recordStop();
    f.nodes["reset-session"].click(); await flush();
    await chooseSession(f, "B"); await chooseSession(f, "default");
    if (stage === "transcribing") pending.resolve(packet({ text: "late A", language: "ko", elapsed_seconds: 1 }));
    else if (stage === "thinking") pending.resolve(packet({ message: "late A answer", metrics: {} }));
    else pending.resolve({ ok: true, status: 200,
      headers: { get: () => "audio/wav" }, blob: async () => new BlobDouble([wav], { type: "audio/wav" }) });
    await flush();
    assert.equal(count(f, "/sessions/default/reset"), 1);
    assert.equal(count(f, "/chat"), stage === "transcribing" ? 0 : 1);
    assert.equal(count(f, "/voice/synthesize"), stage === "synthesizing" ? 1 : 0);
    assert.equal(f.audios.length, 0);
    assert.ok(f.tracks.every((track) => track.stopped));
    assert.equal(f.nodes["conversation-output"].children.length, 1);
    await chooseSession(f, "B");
    assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "B answer");
  }
});

test("Reset during Speaking releases Audio and URL without clearing another session", async () => {
  const f = await fixture({ extraSessions: [{ session_id: "B", number: 2, title: "B intact" }] });
  f.serverHistories.set("B", [{ role: "user", content: "B question" },
    { role: "assistant", content: "B answer", language: "en" }]);
  f.unlock(); f.enable(); await f.recordStop();
  assert.equal(f.audios.length, 1);
  f.nodes["reset-session"].click(); await flush();
  assert.ok(f.audios[0].paused);
  assert.equal(f.revoked.length, 1);
  assert.equal(f.nodes["conversation-output"].children.length, 1);
  await chooseSession(f, "B");
  assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "B answer");
});

test("Session List opens upward, bounds height, and closes on click/outside/Escape", async () => {
  const f = await fixture();
  f.serverSessions.push({ session_id: "B", number: 2, title: "second question" });
  f.serverHistories.set("B", [{ role: "user", content: "second question" },
    { role: "assistant", content: "B answer", language: "en" }]);
  await openSessions(f);
  assert.equal(f.nodes["session-popup"].hidden, false);
  assert.equal(f.nodes["session-list-button"].getAttribute("aria-expanded"), "true");
  assert.ok(parseInt(f.nodes["session-popup"].style.maxHeight, 10) <= 320);
  assert.ok(parseInt(f.nodes["session-popup"].style.bottom, 10) > 0);
  f.nodes["session-list-button"].getBoundingClientRect = () => ({ top: 40, left: 400, width: 140 });
  f.window.resize();
  assert.equal(f.nodes["session-popup"].style.top, "8px");
  assert.equal(f.nodes["session-popup"].style.bottom, "auto");
  f.nodes["session-list-button"].click(); assert.equal(f.nodes["session-popup"].hidden, true);
  await openSessions(f);
  f.documentEvent("click", { target: f.nodes["message"] });
  assert.equal(f.nodes["session-popup"].hidden, true);
  await openSessions(f);
  f.documentEvent("keydown", { key: "Escape" });
  assert.equal(f.nodes["session-popup"].hidden, true);
  await chooseSession(f, "B");
  assert.equal(f.nodes["session-popup"].hidden, true);
  assert.equal(f.nodes["session-id"].value, "B");
  assert.equal(f.nodes["conversation-output"].children.length, 2);
  assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "B answer");
  assert.match(f.nodes["current-session"].textContent, /Session 2 — second question/);
});

test("Session List reads never create and A→B→A cancels a late automatic STT", async () => {
  const f = await fixture(), stt = deferred();
  f.serverSessions.push({ session_id: "B", number: 2, title: "other" });
  f.serverHistories.set("B", []);
  f.handlers["/voice/transcribe"] = () => stt.promise;
  f.unlock(); f.enable(); await f.recordStop();
  await chooseSession(f, "B");
  await chooseSession(f, "default");
  stt.resolve(packet({ text: "late transcript", language: "ko", elapsed_seconds: 1 }));
  await flush();
  assert.equal(count(f, "/chat"), 0);
  assert.equal(count(f, "/sessions"), 2);
  assert.equal(f.nodes.message.value, "");
  assert.equal(f.nodes["session-id"].value, "default");
});

test("default OFF; enabling mode does not request microphone or fetch", async () => {
  const f = await fixture(); let permissionRequests = 0;
  f.microphone(() => { permissionRequests += 1; return new Promise(() => {}); });
  assert.equal(f.nodes["voice-mode"].getAttribute("aria-pressed"), "false");
  f.unlock(); f.enable(); await flush();
  assert.equal(permissionRequests, 0); assert.equal(f.requests.length, 0);
});
test("actual Record/Stop handlers call each API once; preserve D; release Audio URL", async () => {
  const f = await fixture(); f.unlock(); f.enable();
  f.nodes["record-voice"].click(); await flush();
  assert.equal(f.nodes["record-voice"].textContent, "Stop & Send");
  f.nodes["record-voice"].click(); await flush();
  for (const url of ["/voice/transcribe", "/chat", "/voice/synthesize"]) assert.equal(count(f, url), 1);
  assert.equal(f.nodes.message.value, ""); assert.equal(f.audios.length, 1);
  assert.match(f.nodes["voice-status"].textContent, /Speaking/);
  assert.equal(f.nodes["conversation-output"].children.length, 2);
  f.audios[0].onended(); await flush();
  assert.match(f.nodes["voice-status"].textContent, /Idle/);
  assert.equal(f.revoked.length, 1); assert.ok(f.tracks[0].stopped);
});
test("manual mode only places recognized text in D; Enter still sends Chat", async () => {
  const f = await fixture(); f.unlock(); await f.recordStop();
  assert.equal(f.nodes.message.value, "recognized"); assert.equal(count(f, "/chat"), 0);
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", shiftKey: false, preventDefault() {} });
  await flush(); assert.equal(count(f, "/chat"), 1); assert.equal(count(f, "/voice/synthesize"), 0);
  const ai = f.nodes["conversation-output"].querySelector(".tts-play-button");
  f.nodes["conversation-output"].dispatchEvent({ type: "click", target: ai }); await flush();
  assert.equal(count(f, "/voice/synthesize"), 1);
});
test("existing draft blocks automatic recording and remains untouched", async () => {
  const f = await fixture(); f.unlock(); f.enable(); f.input("message", "draft");
  f.nodes["record-voice"].click(); await flush();
  assert.equal(f.recorders.length, 0); assert.equal(f.nodes.message.value, "draft"); assert.equal(f.requests.length, 0);
});
test("recording Cancel never uploads; late recorder event cannot submit", async () => {
  const f = await fixture(); f.unlock(); f.enable();
  f.nodes["record-voice"].click(); await flush(); f.nodes["cancel-voice-turn"].click(); await flush();
  assert.equal(f.requests.length, 0); assert.ok(f.tracks[0].stopped);
});
test("STT empty/error never sends Chat", async () => {
  for (const response of [packet({ text: "", language: "ko", elapsed_seconds: .01 }),
    packet({ error: { code: "stt_busy", message: "busy" } }, 429)]) {
    const f = await fixture(); f.handlers["/voice/transcribe"] = () => response;
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(count(f, "/chat"), 0); assert.match(f.nodes["voice-status"].textContent, /Error/);
  }
});
test("Thinking Cancel observes server response and blocks new Chat until completion", async () => {
  const f = await fixture(), chat = deferred(); f.handlers["/chat"] = () => chat.promise;
  f.unlock(); f.enable(); await f.recordStop(); f.nodes["cancel-voice-turn"].click(); await flush();
  assert.equal(f.nodes["record-voice"].disabled, true);
  chat.resolve(packet({ message: "late answer", metrics: {} })); await flush();
  assert.equal(count(f, "/voice/synthesize"), 0); assert.equal(f.nodes["conversation-output"].children.length, 1);
  assert.equal(f.nodes["record-voice"].disabled, false);
});
test("Reset confirms then waits for Thinking Chat; cancellation does not reset", async () => {
  const f = await fixture(), chat = deferred(); f.handlers["/chat"] = () => chat.promise;
  f.unlock(); f.enable(); await f.recordStop();
  f.confirm(false); f.nodes["reset-session"].click(); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 0); assert.match(f.nodes["voice-status"].textContent, /Thinking/);
  f.confirm(true); f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
  chat.resolve(packet({ message: "late answer" })); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 1); assert.equal(count(f, "/voice/synthesize"), 0);
});
test("unknown Chat completion blocks Record, manual Chat and Reset", async () => {
  const f = await fixture(); f.handlers["/chat"] = () => { throw new Error("network failure"); };
  f.unlock(); f.enable(); await f.recordStop(); assert.equal(f.nodes["record-voice"].disabled, true);
  f.enable(); f.input("message", "retry");
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} }); await flush();
  assert.equal(count(f, "/chat"), 1);
  f.nodes["reset-session"].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
});
test("A→B→A, Lock, mode OFF or menu change prevent late STT from sending Chat", async () => {
  for (const action of [
    (f) => { f.input("session-id", "B"); f.input("session-id", "default"); },
    (f) => f.nodes["console-lock"].click(),
    (f) => f.nodes["voice-mode"].click(),
    (f) => f.nodes["guide-button"].click(),
  ]) {
    const f = await fixture(), stt = deferred(); f.handlers["/voice/transcribe"] = () => stt.promise;
    f.unlock(); f.enable(); await f.recordStop(); action(f);
    stt.resolve(packet({ text: "late", language: "ko", elapsed_seconds: .01 })); await flush();
    assert.equal(count(f, "/chat"), 0);
  }
});
test("Voice and AI Language are fixed across STT/Chat/TTS and style header is sent", async () => {
  const f = await fixture(); f.unlock(); f.enable();
  f.nodes["stt-language"].value = "en"; f.nodes["tts-language"].value = "en";
  await f.recordStop();
  const stt = f.requests.find((r) => r.url === "/voice/transcribe");
  const chat = f.requests.find((r) => r.url === "/chat"); const tts = f.requests.find((r) => r.url === "/voice/synthesize");
  assert.equal(stt.options.body.values.language, "en");
  assert.equal(JSON.parse(chat.options.body).response_language, "en");
  assert.equal(chat.options.headers["X-Local-AI-Response-Style"], "voice");
  assert.equal(JSON.parse(tts.options.body).language, "en");
});
test("text, manual STT and automatic voice use AI Language independently of input language", async () => {
  for (const mode of ["text", "manual-stt", "automatic"]) {
    for (const inputLanguage of ["ko", "en"]) {
      for (const outputLanguage of ["ko", "en"]) {
        const f = await fixture();
        const question = inputLanguage === "ko" ? "안녕하세요" : "Hello";
        f.handlers["/voice/transcribe"] = () => packet({ text: question, language: inputLanguage, elapsed_seconds: .01 });
        f.unlock();
        f.nodes["stt-language"].value = inputLanguage;
        f.nodes["tts-language"].value = outputLanguage;
        if (mode === "automatic") { f.enable(); await f.recordStop(); }
        else {
          if (mode === "manual-stt") await f.recordStop();
          else f.input("message", question);
          f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
          await flush();
          const play = f.nodes["conversation-output"].querySelector(".tts-play-button");
          f.nodes["conversation-output"].dispatchEvent({ type: "click", target: play });
          await flush();
        }
        const body = JSON.parse(f.requests.find((r) => r.url === "/chat").options.body);
        assert.equal(body.message, question);
        assert.equal(body.response_language, outputLanguage);
        const synthesis = JSON.parse(f.requests.find((r) => r.url === "/voice/synthesize").options.body);
        assert.equal(synthesis.language, outputLanguage);
        if (mode !== "text") {
          assert.equal(f.requests.find((r) => r.url === "/voice/transcribe").options.body.values.language, inputLanguage);
        }
      }
    }
  }
});
test("automatic turn captures input Auto and output English before late UI changes", async () => {
  const f = await fixture(), stt = deferred();
  f.handlers["/voice/transcribe"] = () => stt.promise;
  f.unlock(); f.enable(); f.nodes["tts-language"].value = "en";
  await f.recordStop();
  assert.equal(f.nodes["stt-language"].disabled, true);
  assert.equal(f.nodes["tts-language"].disabled, true);
  f.nodes["stt-language"].value = "en"; f.nodes["tts-language"].value = "ko";
  stt.resolve(packet({ text: "한국어 질문", language: "ko", elapsed_seconds: .01 }));
  await flush();
  assert.equal(f.requests.find((r) => r.url === "/voice/transcribe").options.body.values.language, "auto");
  assert.equal(JSON.parse(f.requests.find((r) => r.url === "/chat").options.body).response_language, "en");
  assert.equal(JSON.parse(f.requests.find((r) => r.url === "/voice/synthesize").options.body).language, "en");
});
test("501 code point answer stays in E without synthesis; 500 is synthesized", async () => {
  for (const size of [500, 501]) {
    const f = await fixture(); f.handlers["/chat"] = () => packet({ message: "😀".repeat(size), metrics: {} });
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(count(f, "/voice/synthesize"), size === 500 ? 1 : 0);
    assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "😀".repeat(size));
  }
});
test("TTS Busy/invalid WAV/Audio denial preserve answer and recover controls", async () => {
  for (const failure of ["busy", "wav", "play"]) {
    const f = await fixture();
    if (failure === "busy") f.handlers["/voice/synthesize"] = () => packet({ error: { code: "tts_busy", message: "busy" } }, 429);
    if (failure === "wav") f.handlers["/voice/synthesize"] = () => packet({ invalid: true });
    if (failure === "play") f.handlers.play = () => { const error = new Error("denied"); error.name = "NotAllowedError"; throw error; };
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(f.nodes["conversation-output"].children.length, 2);
    assert.match(f.nodes["voice-status"].textContent, /Error/);
    assert.equal(f.nodes["record-voice"].disabled, false);
    if (failure === "play") assert.equal(f.revoked.length, 1);
  }
});
test("manual Play cannot start another TTS during automatic Speaking; Cancel revokes URL", async () => {
  const f = await fixture(); f.unlock(); f.enable(); await f.recordStop();
  const button = f.nodes["conversation-output"].querySelector(".tts-play-button");
  f.nodes["conversation-output"].dispatchEvent({ type: "click", target: button }); await flush();
  assert.equal(count(f, "/voice/synthesize"), 1);
  f.nodes["cancel-voice-turn"].click(); await flush();
  assert.equal(f.revoked.length, 1); assert.ok(f.audios[0].paused);
});
test("permission failure and late permission after Cancel clean up without uploading", async () => {
  const f = await fixture(); f.microphone(() => { const e = new Error("denied"); e.name = "NotAllowedError"; return Promise.reject(e); });
  f.unlock(); f.enable(); f.nodes["record-voice"].click(); await flush();
  assert.match(f.nodes["voice-status"].textContent, /Error/); assert.equal(f.requests.length, 0);
  const g = await fixture(), permission = deferred(); let stops = 0;
  g.microphone(() => permission.promise); g.unlock(); g.enable(); g.nodes["record-voice"].click(); await flush();
  g.nodes["cancel-voice-turn"].click(); permission.resolve({ getTracks: () => [{ stop: () => { stops += 1; } }] }); await flush();
  assert.equal(stops, 1); assert.equal(g.requests.length, 0);
});

test("30-second timer stops and sends once; playback does not reopen microphone", async () => {
  const f = await fixture(); f.unlock(); f.enable(); f.nodes["record-voice"].click(); await flush();
  const limit = [...f.timers.values()].find((entry) => entry.ms === 30000);
  assert.ok(limit); limit.callback(); await flush();
  assert.equal(count(f, "/chat"), 1); f.audios[0].onended(); await flush();
  assert.equal(f.recorders.length, 1); assert.match(f.nodes["voice-status"].textContent, /Idle/);
});
test("late rejected permission cannot stop a newer recording", async () => {
  const f = await fixture(), first = deferred(); f.microphone(() => first.promise);
  f.unlock(); f.enable(); f.nodes["record-voice"].click(); await flush(); f.nodes["cancel-voice-turn"].click();
  const track = { stopped: 0, stop() { this.stopped += 1; }, addEventListener() {} };
  f.microphone(async () => ({ getTracks: () => [track] }));
  f.nodes["record-voice"].click(); await flush(); first.reject(new Error("old permission rejected")); await flush();
  assert.equal(track.stopped, 0); assert.equal(f.nodes["record-voice"].textContent, "Stop & Send");
});
test("synthesis Cancel aborts signal and discards late WAV without audio", async () => {
  const f = await fixture(), tts = deferred(); f.handlers["/voice/synthesize"] = () => tts.promise;
  f.unlock(); f.enable(); await f.recordStop();
  const request = f.requests.find((entry) => entry.url === "/voice/synthesize");
  f.nodes["cancel-voice-turn"].click(); assert.equal(request.options.signal.aborted, true);
  tts.resolve({ ok: true, headers: { get: () => "audio/wav" }, blob: async () => new BlobDouble([wav]) }); await flush();
  assert.equal(f.audios.length, 0);
});
test("Chat HTTP failures stop TTS without an uncertain-session block", async () => {
  for (const status of [400, 429, 503, 504]) {
    const f = await fixture(); f.handlers["/chat"] = () => packet({ error: { code: "chat_failed", message: "failed" } }, status);
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(count(f, "/voice/synthesize"), 0); assert.equal(f.nodes["record-voice"].disabled, false);
  }
});
test("page exit releases Audio and URL; manual Stop also releases them", async () => {
  const f = await fixture(); f.unlock(); f.enable(); await f.recordStop(); f.window.beforeunload(); await flush();
  assert.equal(f.revoked.length, 1); assert.ok(f.audios[0].paused);
  const g = await fixture(); g.unlock(); g.input("message", "question");
  g.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} }); await flush();
  const button = g.nodes["conversation-output"].querySelector(".tts-play-button");
  g.nodes["conversation-output"].dispatchEvent({ type: "click", target: button }); await flush();
  g.nodes["conversation-output"].dispatchEvent({ type: "click", target: button }); await flush();
  assert.equal(g.revoked.length, 1); assert.equal(button.textContent, "▶ Play");
});

(async () => {
  let failures = 0;
  for (const { name, run } of tests) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { failures += 1; console.error(`FAIL ${name}\n${error.stack}`); }
  }
  console.log(`${tests.length - failures}/${tests.length} UI event tests passed (browser API doubles)`);
  process.exitCode = failures ? 1 : 0;
})();

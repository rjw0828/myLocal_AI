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
    this.disabled = false;
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
  click() { if (!this.disabled) this.dispatchEvent({ type: "click" }); }
  focus() {}
  matches(selector) {
    if (selector.startsWith(".")) return this.className.split(" ").includes(selector.slice(1));
    const match = selector.match(/^\[data-message-id="(\d+)"\]$/);
    return Boolean(match && this.dataset.messageId === match[1]);
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
function fixture() {
  const root = path.resolve(__dirname, "..");
  const html = fs.readFileSync(path.join(root, "templates/index.html"), "utf8");
  const nodes = {};
  for (const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Element(match[1]);
    node.disabled = /\bdisabled\b/.test(match[0]);
    const pressed = match[0].match(/aria-pressed="([^"]+)"/);
    if (pressed) node.setAttribute("aria-pressed", pressed[1]);
    nodes[match[2]] = node;
  }
  nodes["session-id"].value = "default";
  nodes["stt-language"].value = "auto"; nodes["tts-language"].value = "ko";
  const tools = ["health", "chat", "reset"].map((tool) => {
    const node = new Element("button"); node.dataset.tool = tool; return node;
  });
  const requests = [], audios = [], revoked = [], tracks = [], recorders = [];
  const timers = new Map(); let timer = 0, domReady, confirm = true;
  const handlers = {};
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
  const window = {
    MediaRecorder: Recorder,
    sessionStorage: { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) },
    setTimeout: (callback, ms) => { timers.set(++timer, { callback, ms }); return timer; },
    clearTimeout: (id) => timers.delete(id),
    setInterval: (callback, ms) => { timers.set(++timer, { callback, ms }); return timer; },
    clearInterval: (id) => timers.delete(id),
    addEventListener: (name, handler) => { window[name] = handler; },
    confirm: () => confirm,
  };
  const fetch = async (url, options) => {
    requests.push({ url, options });
    if (handlers[url]) return handlers[url](options);
    if (url === "/voice/transcribe") return packet({ text: "recognized", language: "ko", elapsed_seconds: .01 });
    if (url === "/chat") return packet({ message: "AI answer", elapsed_seconds: .01, metrics: {} });
    if (url.endsWith("/reset")) return packet({ session_id: "default", status: "reset" });
    if (url === "/voice/synthesize") return { ok: true, status: 200,
      headers: { get: (name) => name === "Content-Type" ? "audio/wav" : "piper;dur=1" },
      blob: async () => new BlobDouble([wav], { type: "audio/wav" }),
    };
    return packet({ controller: "ok", ollama_reachable: true, model: "qwen3.5:9b" });
  };
  window.fetch = fetch;
  const sandbox = { window, fetch, VoiceTurnManager, ChatSessionGuard,
    document: { addEventListener: (_name, handler) => { domReady = handler; },
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
  return { nodes, tools, requests, audios, revoked, tracks, recorders, handlers, window, timers,
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

test("default OFF; enabling mode does not request microphone or fetch", async () => {
  const f = fixture(); let permissionRequests = 0;
  f.microphone(() => { permissionRequests += 1; return new Promise(() => {}); });
  assert.equal(f.nodes["voice-mode"].getAttribute("aria-pressed"), "false");
  f.unlock(); f.enable(); await flush();
  assert.equal(permissionRequests, 0); assert.equal(f.requests.length, 0);
});
test("actual Record/Stop handlers call each API once; preserve D; release Audio URL", async () => {
  const f = fixture(); f.unlock(); f.enable();
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
  const f = fixture(); f.unlock(); f.tools[1].click(); await f.recordStop();
  assert.equal(f.nodes.message.value, "recognized"); assert.equal(count(f, "/chat"), 0);
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", shiftKey: false, preventDefault() {} });
  await flush(); assert.equal(count(f, "/chat"), 1); assert.equal(count(f, "/voice/synthesize"), 0);
  const ai = f.nodes["conversation-output"].querySelector(".tts-play-button");
  f.nodes["conversation-output"].dispatchEvent({ type: "click", target: ai }); await flush();
  assert.equal(count(f, "/voice/synthesize"), 1);
});
test("existing draft blocks automatic recording and remains untouched", async () => {
  const f = fixture(); f.unlock(); f.enable(); f.input("message", "draft");
  f.nodes["record-voice"].click(); await flush();
  assert.equal(f.recorders.length, 0); assert.equal(f.nodes.message.value, "draft"); assert.equal(f.requests.length, 0);
});
test("recording Cancel never uploads; late recorder event cannot submit", async () => {
  const f = fixture(); f.unlock(); f.enable();
  f.nodes["record-voice"].click(); await flush(); f.nodes["cancel-voice-turn"].click(); await flush();
  assert.equal(f.requests.length, 0); assert.ok(f.tracks[0].stopped);
});
test("STT empty/error never sends Chat", async () => {
  for (const response of [packet({ text: "", language: "ko", elapsed_seconds: .01 }),
    packet({ error: { code: "stt_busy", message: "busy" } }, 429)]) {
    const f = fixture(); f.handlers["/voice/transcribe"] = () => response;
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(count(f, "/chat"), 0); assert.match(f.nodes["voice-status"].textContent, /Error/);
  }
});
test("Thinking Cancel observes server response and blocks new Chat until completion", async () => {
  const f = fixture(), chat = deferred(); f.handlers["/chat"] = () => chat.promise;
  f.unlock(); f.enable(); await f.recordStop(); f.nodes["cancel-voice-turn"].click(); await flush();
  assert.equal(f.nodes["record-voice"].disabled, true);
  chat.resolve(packet({ message: "late answer", metrics: {} })); await flush();
  assert.equal(count(f, "/voice/synthesize"), 0); assert.equal(f.nodes["conversation-output"].children.length, 1);
  assert.equal(f.nodes["record-voice"].disabled, false);
});
test("Reset confirms then waits for Thinking Chat; cancellation does not reset", async () => {
  const f = fixture(), chat = deferred(); f.handlers["/chat"] = () => chat.promise;
  f.unlock(); f.enable(); await f.recordStop();
  f.confirm(false); f.tools[2].click(); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 0); assert.match(f.nodes["voice-status"].textContent, /Thinking/);
  f.confirm(true); f.tools[2].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
  chat.resolve(packet({ message: "late answer" })); await flush();
  assert.equal(count(f, "/sessions/default/reset"), 1); assert.equal(count(f, "/voice/synthesize"), 0);
});
test("unknown Chat completion blocks Record, manual Chat and Reset", async () => {
  const f = fixture(); f.handlers["/chat"] = () => { throw new Error("network failure"); };
  f.unlock(); f.enable(); await f.recordStop(); assert.equal(f.nodes["record-voice"].disabled, true);
  f.enable(); f.tools[1].click(); f.input("message", "retry");
  f.nodes.message.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} }); await flush();
  assert.equal(count(f, "/chat"), 1);
  f.tools[2].click(); await flush(); assert.equal(count(f, "/sessions/default/reset"), 0);
});
test("A→B→A, Lock, mode OFF or menu change prevent late STT from sending Chat", async () => {
  for (const action of [
    (f) => { f.input("session-id", "B"); f.input("session-id", "default"); },
    (f) => f.nodes["console-lock"].click(),
    (f) => f.nodes["voice-mode"].click(),
    (f) => f.nodes["guide-button"].click(),
  ]) {
    const f = fixture(), stt = deferred(); f.handlers["/voice/transcribe"] = () => stt.promise;
    f.unlock(); f.enable(); await f.recordStop(); action(f);
    stt.resolve(packet({ text: "late", language: "ko", elapsed_seconds: .01 })); await flush();
    assert.equal(count(f, "/chat"), 0);
  }
});
test("voice language preset is fixed across STT/Chat/TTS and style header is sent", async () => {
  const f = fixture(); f.unlock(); f.enable(); f.nodes["voice-preset"].value = "en";
  f.nodes["voice-preset"].dispatchEvent({ type: "change" }); await f.recordStop();
  const stt = f.requests.find((r) => r.url === "/voice/transcribe");
  const chat = f.requests.find((r) => r.url === "/chat"); const tts = f.requests.find((r) => r.url === "/voice/synthesize");
  assert.equal(stt.options.body.values.language, "en");
  assert.equal(JSON.parse(chat.options.body).response_language, "en");
  assert.equal(chat.options.headers["X-Local-AI-Response-Style"], "voice");
  assert.equal(JSON.parse(tts.options.body).language, "en");
});
test("501 code point answer stays in E without synthesis; 500 is synthesized", async () => {
  for (const size of [500, 501]) {
    const f = fixture(); f.handlers["/chat"] = () => packet({ message: "😀".repeat(size), metrics: {} });
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(count(f, "/voice/synthesize"), size === 500 ? 1 : 0);
    assert.equal(f.nodes["conversation-output"].children[1].children[1].textContent, "😀".repeat(size));
  }
});
test("TTS Busy/invalid WAV/Audio denial preserve answer and recover controls", async () => {
  for (const failure of ["busy", "wav", "play"]) {
    const f = fixture();
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
  const f = fixture(); f.unlock(); f.enable(); await f.recordStop();
  const button = f.nodes["conversation-output"].querySelector(".tts-play-button");
  f.nodes["conversation-output"].dispatchEvent({ type: "click", target: button }); await flush();
  assert.equal(count(f, "/voice/synthesize"), 1);
  f.nodes["cancel-voice-turn"].click(); await flush();
  assert.equal(f.revoked.length, 1); assert.ok(f.audios[0].paused);
});
test("permission failure and late permission after Cancel clean up without uploading", async () => {
  const f = fixture(); f.microphone(() => { const e = new Error("denied"); e.name = "NotAllowedError"; return Promise.reject(e); });
  f.unlock(); f.enable(); f.nodes["record-voice"].click(); await flush();
  assert.match(f.nodes["voice-status"].textContent, /Error/); assert.equal(f.requests.length, 0);
  const g = fixture(), permission = deferred(); let stops = 0;
  g.microphone(() => permission.promise); g.unlock(); g.enable(); g.nodes["record-voice"].click(); await flush();
  g.nodes["cancel-voice-turn"].click(); permission.resolve({ getTracks: () => [{ stop: () => { stops += 1; } }] }); await flush();
  assert.equal(stops, 1); assert.equal(g.requests.length, 0);
});

test("30-second timer stops and sends once; playback does not reopen microphone", async () => {
  const f = fixture(); f.unlock(); f.enable(); f.nodes["record-voice"].click(); await flush();
  const limit = [...f.timers.values()].find((entry) => entry.ms === 30000);
  assert.ok(limit); limit.callback(); await flush();
  assert.equal(count(f, "/chat"), 1); f.audios[0].onended(); await flush();
  assert.equal(f.recorders.length, 1); assert.match(f.nodes["voice-status"].textContent, /Idle/);
});
test("late rejected permission cannot stop a newer recording", async () => {
  const f = fixture(), first = deferred(); f.microphone(() => first.promise);
  f.unlock(); f.enable(); f.nodes["record-voice"].click(); await flush(); f.nodes["cancel-voice-turn"].click();
  const track = { stopped: 0, stop() { this.stopped += 1; }, addEventListener() {} };
  f.microphone(async () => ({ getTracks: () => [track] }));
  f.nodes["record-voice"].click(); await flush(); first.reject(new Error("old permission rejected")); await flush();
  assert.equal(track.stopped, 0); assert.equal(f.nodes["record-voice"].textContent, "Stop & Send");
});
test("synthesis Cancel aborts signal and discards late WAV without audio", async () => {
  const f = fixture(), tts = deferred(); f.handlers["/voice/synthesize"] = () => tts.promise;
  f.unlock(); f.enable(); await f.recordStop();
  const request = f.requests.find((entry) => entry.url === "/voice/synthesize");
  f.nodes["cancel-voice-turn"].click(); assert.equal(request.options.signal.aborted, true);
  tts.resolve({ ok: true, headers: { get: () => "audio/wav" }, blob: async () => new BlobDouble([wav]) }); await flush();
  assert.equal(f.audios.length, 0);
});
test("Chat HTTP failures stop TTS without an uncertain-session block", async () => {
  for (const status of [400, 429, 503, 504]) {
    const f = fixture(); f.handlers["/chat"] = () => packet({ error: { code: "chat_failed", message: "failed" } }, status);
    f.unlock(); f.enable(); await f.recordStop();
    assert.equal(count(f, "/voice/synthesize"), 0); assert.equal(f.nodes["record-voice"].disabled, false);
  }
});
test("page exit releases Audio and URL; manual Stop also releases them", async () => {
  const f = fixture(); f.unlock(); f.enable(); await f.recordStop(); f.window.beforeunload(); await flush();
  assert.equal(f.revoked.length, 1); assert.ok(f.audios[0].paused);
  const g = fixture(); g.unlock(); g.tools[1].click(); g.input("message", "question");
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

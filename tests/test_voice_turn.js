"use strict";

// No DOM library: exercise the real orchestrator with controlled async adapters.
const assert = require("assert").strict;
const { VoiceTurnManager, ChatSessionGuard } = require("../static/js/voice-turn.js");
const tests = [];
const test = (name, run) => tests.push({ name, run });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
const storage = () => {
  const values = new Map();
  return { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
};
function fixture() {
  const context = { enabled: true, locked: false, sessionId: "A", revision: 0,
    draft: "", sttLanguage: "ko", responseLanguage: "ko" };
  const calls = { chat: 0, speak: 0, cleanup: 0, questions: [], answers: [], states: [], notices: [], errors: [] };
  const adapters = {
    context: () => context,
    state: (state) => calls.states.push(state),
    cleanup: (turn) => {
      calls.cleanup += 1;
      turn.controllers.forEach((controller) => controller.abort());
      if (turn.mediaStream) turn.mediaStream.getTracks().forEach((track) => track.stop());
      if (turn.audio) turn.audio.pause();
      turn.mediaStream = null; turn.audio = null; turn.objectUrl = null;
    },
    question: (_turn, text) => calls.questions.push(text),
    chat: async () => { calls.chat += 1; return { message: "answer" }; },
    answer: (turn, text) => { calls.answers.push(text); return { content: text, language: turn.responseLanguage }; },
    speak: async (_turn, _message, speaking) => { calls.speak += 1; speaking(); },
    notice: (message) => calls.notices.push(message),
    error: (error) => calls.errors.push(error),
  };
  const manager = new VoiceTurnManager(adapters);
  return { manager, context, calls, adapters };
}

test("OFF blocks begin; changing to ON itself sends nothing", async () => {
  const f = fixture(); f.context.enabled = false;
  assert.throws(() => f.manager.begin(), { code: "voice_disabled" });
  f.context.enabled = true;
  await flush(); assert.equal(f.manager.active, null); assert.equal(f.calls.chat, 0);
});
test("locked, invalid session, and any existing draft prevent automatic turn", () => {
  const f = fixture();
  for (const draft of ["draft", " ", "\n"]) {
    f.context.draft = draft; assert.throws(() => f.manager.begin(), { code: "draft_present" });
  }
  f.context.draft = ""; f.context.locked = true;
  assert.throws(() => f.manager.begin(), { code: "voice_disabled" });
  f.context.locked = false; f.context.sessionId = "";
  assert.throws(() => f.manager.begin(), { code: "invalid_session_id" });
});
test("one turn executes STT→Chat→TTS once and all normal states", async () => {
  const f = fixture(); const turn = f.manager.begin(); let stt = 0;
  assert.throws(() => f.manager.begin(), { code: "voice_busy" });
  await f.manager.run(turn, async () => { stt += 1; return { text: "question" }; });
  assert.equal(stt, 1); assert.equal(f.calls.chat, 1); assert.equal(f.calls.speak, 1);
  assert.deepEqual(f.calls.questions, ["question"]); assert.deepEqual(f.calls.answers, ["answer"]);
  assert.deepEqual(f.calls.states, ["Recording", "Transcribing", "Thinking", "Synthesizing", "Speaking", "Idle"]);
});
test("STT empty result and STT error never invoke Chat", async () => {
  for (const value of ["", "  ", null, new Error("STT HTTP 503")]) {
    const f = fixture(); const turn = f.manager.begin();
    await f.manager.run(turn, async () => {
      if (value instanceof Error) throw value;
      return { text: value };
    });
    assert.equal(f.calls.chat, 0); assert.equal(f.calls.speak, 0); assert.equal(f.manager.state, "Error");
  }
});
test("cancel during recording means no STT upload", async () => {
  const f = fixture(); const turn = f.manager.begin(); let stt = 0;
  f.manager.cancel();
  await f.manager.run(turn, async () => { stt += 1; return { text: "late" }; });
  assert.equal(stt, 0); assert.equal(f.calls.chat, 0);
});
test("cancel while transcribing ignores a late successful transcript", async () => {
  const f = fixture(), stt = deferred(); const turn = f.manager.begin();
  const run = f.manager.run(turn, () => stt.promise); f.manager.cancel();
  stt.resolve({ text: "late" }); await run;
  assert.equal(f.calls.chat, 0); assert.equal(f.manager.state, "Cancelled");
});
test("Thinking cancel lets observed Chat finish but never adds answer or TTS", async () => {
  const f = fixture(), chat = deferred(); f.adapters.chat = () => chat.promise;
  const run = f.manager.run(f.manager.begin(), async () => ({ text: "question" }));
  await flush(); assert.equal(f.manager.state, "Thinking"); f.manager.cancel();
  chat.resolve({ message: "late answer" }); await run;
  assert.deepEqual(f.calls.answers, []); assert.equal(f.calls.speak, 0);
});
test("A→B→A revision invalidates old result even when final ID matches", async () => {
  const f = fixture(), stt = deferred(); const turn = f.manager.begin();
  const run = f.manager.run(turn, () => stt.promise);
  f.context.sessionId = "B"; f.context.revision += 1;
  f.context.sessionId = "A"; f.context.revision += 1;
  stt.resolve({ text: "late" }); await run;
  assert.equal(f.calls.chat, 0); assert.equal(f.manager.state, "Cancelled");
});
test("old finally cannot clean up or change a newer turn", async () => {
  const f = fixture(), stt = deferred();
  const oldRun = f.manager.run(f.manager.begin(), () => stt.promise);
  f.manager.cancel(); const next = f.manager.begin(); const cleaned = f.calls.cleanup;
  stt.reject(new Error("late failure")); await oldRun;
  assert.equal(f.manager.active, next); assert.equal(f.manager.state, "Recording");
  assert.equal(f.calls.cleanup, cleaned);
});
test("draft changed in-flight remains untouched and blocks Chat", async () => {
  const f = fixture(), stt = deferred(); const run = f.manager.run(f.manager.begin(), () => stt.promise);
  f.context.draft = "keep this"; stt.resolve({ text: "recognized" }); await run;
  assert.equal(f.context.draft, "keep this"); assert.equal(f.calls.chat, 0);
});
test("turn snapshots STT and answer language", async () => {
  const f = fixture(); f.context.sttLanguage = "en"; f.context.responseLanguage = "en";
  const turn = f.manager.begin(); f.context.sttLanguage = "ko"; f.context.responseLanguage = "ko";
  f.adapters.speak = async (owner, message) => {
    assert.equal(owner.sttLanguage, "en"); assert.equal(message.language, "en");
  };
  await f.manager.run(turn, async () => ({ text: "hello" }));
});
test("500 Unicode code points speak, 501 preserve complete answer without TTS", async () => {
  for (const count of [500, 501]) {
    const f = fixture(); f.adapters.chat = async () => ({ message: "😀".repeat(count) });
    await f.manager.run(f.manager.begin(), async () => ({ text: "question" }));
    assert.equal(Array.from(f.calls.answers[0]).length, count);
    assert.equal(f.calls.speak, count === 500 ? 1 : 0); assert.equal(f.manager.state, "Idle");
  }
});
test("Chat failure never calls TTS; TTS failure keeps successful answer", async () => {
  const f = fixture(); f.adapters.chat = async () => { throw new Error("HTTP 504"); };
  await f.manager.run(f.manager.begin(), async () => ({ text: "question" }));
  assert.equal(f.calls.speak, 0); assert.equal(f.calls.answers.length, 0);
  const g = fixture(); g.adapters.speak = async () => { throw new Error("tts_busy"); };
  await g.manager.run(g.manager.begin(), async () => ({ text: "question" }));
  assert.deepEqual(g.calls.answers, ["answer"]); assert.equal(g.manager.state, "Error");
});
test("cancel in synthesis or speaking cleans owned resources once", async () => {
  for (const speaking of [false, true]) {
    const f = fixture(), end = deferred(); let stopped = 0, paused = 0, aborted = 0;
    f.adapters.speak = async (turn, _message, onSpeaking) => {
      turn.controllers.add({ abort: () => { aborted += 1; } });
      turn.mediaStream = { getTracks: () => [{ stop: () => { stopped += 1; } }] };
      turn.audio = { pause: () => { paused += 1; } }; turn.objectUrl = "blob:test";
      if (speaking) onSpeaking(); await end.promise;
    };
    const run = f.manager.run(f.manager.begin(), async () => ({ text: "question" }));
    await flush(); f.manager.cancel(); end.resolve(); await run;
    assert.equal(stopped, 1); assert.equal(paused, 1); assert.equal(aborted, 1);
    assert.equal(f.calls.cleanup, 1); assert.equal(f.manager.state, "Cancelled");
  }
});
test("Lock or OFF during an outstanding stage prevents next request", async () => {
  for (const field of ["locked", "enabled"]) {
    const f = fixture(), stt = deferred(); const run = f.manager.run(f.manager.begin(), () => stt.promise);
    f.context[field] = field === "locked"; stt.resolve({ text: "question" }); await run;
    assert.equal(f.calls.chat, 0);
  }
});
test("guard prevents same-session overlap and allows other sessions", async () => {
  const guard = new ChatSessionGuard(), task = deferred();
  const first = guard.run("A", async (confirm) => { await task.promise; confirm(); });
  assert.throws(() => guard.run("A", async () => {}), { code: "chat_pending" });
  await guard.run("B", async (confirm) => confirm()); task.resolve(); await first;
  assert.equal(guard.reason("A"), null);
});
test("unknown completion blocks permanently across reload, including Reset", async () => {
  const saved = storage(), guard = new ChatSessionGuard(saved);
  await assert.rejects(guard.run("A", async () => { throw new Error("network failed"); }));
  assert.equal(guard.reason("A"), "chat_state_uncertain");
  const restored = new ChatSessionGuard(saved);
  assert.throws(() => restored.assertAvailable("A"), { code: "chat_state_uncertain" });
  await assert.rejects(restored.beforeReset("A"), { code: "chat_state_uncertain" });
});
test("pending at page close restores uncertain; no clock-based unblock", async () => {
  const saved = storage(), guard = new ChatSessionGuard(saved), task = deferred();
  const pending = guard.run("A", async (confirm) => { await task.promise; confirm(); });
  const restored = new ChatSessionGuard(saved);
  assert.equal(restored.reason("A"), "chat_state_uncertain");
  task.resolve(); await pending;
  assert.equal(restored.reason("A"), "chat_state_uncertain");
});
test("Reset waits for Chat completion; confirmed HTTP errors release slot", async () => {
  const guard = new ChatSessionGuard(), task = deferred(), order = [];
  const chat = guard.run("A", async (confirm) => { await task.promise; confirm(); order.push("chat"); });
  const reset = guard.beforeReset("A").then(() => order.push("reset"));
  await flush(); assert.deepEqual(order, []);
  task.resolve(); await Promise.all([chat, reset]); assert.deepEqual(order, ["chat", "reset"]);
  await assert.rejects(guard.run("A", async (confirm) => { confirm(); throw new Error("HTTP 503"); }));
  assert.equal(guard.reason("A"), null);
});

(async () => {
  let failures = 0;
  for (const { name, run } of tests) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { failures += 1; console.error(`FAIL ${name}\n${error.stack}`); }
  }
  console.log(`${tests.length - failures}/${tests.length} voice orchestration tests passed`);
  process.exitCode = failures ? 1 : 0;
})();

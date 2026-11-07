"use strict";

// Browser-independent orchestration. Adapters own media/HTTP and render only
// after this manager checks that the originating turn still owns the context.
((root) => {
  class VoiceTurnError extends Error {
    constructor(code, message) {
      super(message);
      this.code = code;
    }
  }

  class ChatSessionGuard {
    constructor(storage = null) {
      this.pending = new Map();
      this.uncertain = new Set();
      this.storage = storage;
      this.key = "local-ai-unsettled-chat-sessions";
      try {
        const saved = JSON.parse(storage && storage.getItem(this.key) || "[]");
        if (Array.isArray(saved)) saved.forEach((id) => {
          if (typeof id === "string") this.uncertain.add(id);
        });
      } catch (_error) { /* Storage may be unavailable; in-tab protection remains. */ }
    }

    persist() {
      try {
        if (this.storage) this.storage.setItem(this.key,
          JSON.stringify([...new Set([...this.uncertain, ...this.pending.keys()])]));
      } catch (_error) { /* Never release an in-memory block on storage failure. */ }
    }

    reason(sessionId) {
      if (this.uncertain.has(sessionId)) return "chat_state_uncertain";
      if (this.pending.has(sessionId)) return "chat_pending";
      return null;
    }

    assertAvailable(sessionId) {
      const reason = this.reason(sessionId);
      if (reason) throw new VoiceTurnError(reason, reason === "chat_pending"
        ? "Previous Chat is still finishing. Wait for its confirmed response."
        : "Server Chat completion is unknown. This session is blocked until verified resynchronization. Use a new Session ID; Health or elapsed time cannot clear this block.");
    }

    run(sessionId, work) {
      this.assertAvailable(sessionId);
      let confirmed = false;
      // Schedule work after the pending entry has been registered.
      const promise = Promise.resolve().then(() => work(() => { confirmed = true; }));
      const tracked = promise.finally(() => {
        if (!confirmed) this.uncertain.add(sessionId);
        if (this.pending.get(sessionId) === tracked) this.pending.delete(sessionId);
        this.persist();
      });
      this.pending.set(sessionId, tracked);
      this.persist();
      return tracked;
    }

    async beforeReset(sessionId) {
      const pending = this.pending.get(sessionId);
      if (pending) {
        try { await pending; } catch (_error) { /* Check certainty below. */ }
      }
      this.assertAvailable(sessionId);
    }
  }

  class VoiceTurnManager {
    constructor(adapters) {
      this.adapters = adapters;
      this.active = null;
      this.generation = 0;
      this.state = "Idle";
    }

    valid(turn) {
      const context = this.adapters.context();
      return this.active === turn && !turn.cancelled
        && turn.id === this.generation && context.enabled && !context.locked
        && turn.sessionId === context.sessionId
        && turn.revision === context.revision && context.draft === turn.initialDraft;
    }

    setState(turn, state, detail = "") {
      if (this.active !== turn) return;
      turn.state = state;
      this.state = state;
      this.adapters.state(state, detail, turn);
    }

    begin() {
      const context = this.adapters.context();
      if (this.active) throw new VoiceTurnError("voice_busy", "Another voice turn is running.");
      if (!context.enabled || context.locked) throw new VoiceTurnError("voice_disabled", "Enable Voice Conversation and unlock Input first.");
      if (!context.sessionId || Array.from(context.sessionId).length > 128) throw new VoiceTurnError("invalid_session_id", "Enter a Session ID of 1–128 characters.");
      if (context.draft !== "") throw new VoiceTurnError("draft_present", "Keep your draft: send it manually or clear it before starting a voice turn.");
      const turn = {
        id: ++this.generation, sessionId: context.sessionId, revision: context.revision,
        sttLanguage: context.sttLanguage, responseLanguage: context.responseLanguage,
        initialDraft: context.draft, cancelled: false, state: "Recording",
        controllers: new Set(), mediaRecorder: null, mediaStream: null,
        audio: null, objectUrl: null,
      };
      this.active = turn;
      this.setState(turn, "Recording", "Requesting permission");
      return turn;
    }

    cancel(reason = "Cancelled by the user") {
      const turn = this.active;
      if (!turn) return;
      turn.cancelled = true;
      this.generation += 1;
      this.active = null;
      this.state = "Cancelled";
      this.adapters.cleanup(turn);
      this.adapters.state("Cancelled", reason, turn);
    }

    fail(turn, error) {
      if (this.active !== turn) return;
      this.active = null;
      this.state = "Error";
      this.adapters.cleanup(turn);
      this.adapters.state("Error", error.message, turn);
      this.adapters.error(error);
    }

    check(turn) {
      if (this.valid(turn)) return true;
      if (this.active === turn) this.cancel("Voice context changed; no further request was sent.");
      return false;
    }

    async run(turn, transcribe) {
      if (!this.check(turn)) return;
      try {
        this.setState(turn, "Transcribing");
        const transcript = await transcribe();
        if (!this.check(turn)) return;
        const text = transcript && transcript.text;
        if (typeof text !== "string" || !text.trim()) throw new VoiceTurnError("stt_empty_result", "No speech could be recognized.");
        if (Array.from(text).length > 10_000) throw new VoiceTurnError("invalid_message", "Recognized text exceeds the Chat limit.");
        this.setState(turn, "Thinking");
        this.adapters.question(turn, text);
        const answer = await this.adapters.chat(turn, text);
        if (!this.check(turn)) return;
        if (!answer || typeof answer.message !== "string" || !answer.message.trim()) throw new VoiceTurnError("invalid_chat_response", "Chat returned an empty or invalid answer.");
        const message = this.adapters.answer(turn, answer.message);
        if (Array.from(answer.message).length > 500) {
          this.adapters.notice("답변이 길어 자동 음성 출력을 생략했습니다. 전체 답변은 E에 보존됩니다.");
        } else {
          this.setState(turn, "Synthesizing");
          await this.adapters.speak(turn, message, () => {
            if (this.check(turn)) this.setState(turn, "Speaking");
          });
          if (!this.check(turn)) return;
        }
        this.active = null;
        this.state = "Idle";
        this.adapters.cleanup(turn);
        this.adapters.state("Idle", "Turn complete. Press Record for another turn.", turn);
      } catch (error) {
        if (this.check(turn)) this.fail(turn, error);
      }
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { VoiceTurnManager, ChatSessionGuard, VoiceTurnError };
  } else {
    Object.assign(root, { VoiceTurnManager, ChatSessionGuard, VoiceTurnError });
  }
})(globalThis);

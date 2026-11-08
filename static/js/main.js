"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const REQUEST_TIMEOUT_MS = 190_000;
  const $ = (selector) => document.querySelector(selector);
  const guideButton = $("#guide-button");
  const serverButton = $("#server-button");
  const guideView = $("#guide-view");
  const serverView = $("#server-view");
  const mainContent = $("#main-content");
  const refreshHealthButton = $("#refresh-health");
  const controllerStatus = $("#controller-status");
  const ollamaStatus = $("#ollama-status");
  const configuredModel = $("#configured-model");
  const serverModel = $("#server-model");
  const serverToggle = $("#server-toggle");
  const serverStatus = serverToggle;
  const healthMessage = $("#health-message");
  const toolButtons = [...document.querySelectorAll(".tool-option")];
  const lockButton = $("#console-lock");
  const lockLabel = $("#lock-label");
  const lockIcon = $("#lock-icon");
  const inputPanel = $("#input-panel");
  const sessionInput = $("#session-id");
  const newChatButton = $("#new-chat");
  const currentSessionLabel = $("#current-session");
  const sessionListButton = $("#session-list-button");
  const resetSessionButton = $("#reset-session");
  const sessionPopup = $("#session-popup");
  const sessionPopupItems = $("#session-popup-items");
  const deleteAllSessionsButton = $("#delete-all-sessions");
  const messageInput = $("#message");
  const lockNote = $("#lock-note");
  const clearOutputButton = $("#clear-output");
  const outputLog = $("#output-log");
  const conversationSession = $("#conversation-session");
  const conversationOutput = $("#conversation-output");
  const conversationEmpty = $("#conversation-empty");
  const ttsLanguage = $("#tts-language");
  const recordVoiceButton = $("#record-voice");
  const sttLanguage = $("#stt-language");
  const voiceStatus = $("#voice-status");
  const voiceTimer = $("#voice-timer");
  const voiceModeButton = $("#voice-mode");
  const cancelVoiceTurnButton = $("#cancel-voice-turn");
  let voiceModeEnabled = false;
  let sessionRevision = 0;
  let recordingTurn = null;
  let voiceTurns = null;
  let chatStorage = null;
  try { chatStorage = window.sessionStorage; } catch (_error) { /* In-tab guard remains. */ }
  const chatGuard = new ChatSessionGuard(chatStorage);
  let deletingAll = false;
  let preparingDeleteAll = false;
  let deleteAllUncertain = false;
  try { deleteAllUncertain = Boolean(chatStorage && chatStorage.getItem("local-ai-delete-all-uncertain") === "true"); }
  catch (_error) { /* In-tab protection remains. */ }
  const persistDeleteAll = (uncertain) => {
    deleteAllUncertain = uncertain;
    try { if (chatStorage) chatStorage.setItem("local-ai-delete-all-uncertain", String(uncertain)); }
    catch (_error) { /* Never release the in-memory block on storage failure. */ }
  };
  const VOICE_MAX_BYTES = 10 * 1024 * 1024;
  const VOICE_MAX_MS = 30_000;
  const VOICE_MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm"];
  const TTS_MAX_CODE_POINTS = 500;
  let requestRunning = false;
  let voiceState = "idle";
  let voiceOperationId = 0;
  let mediaRecorder = null;
  let mediaStream = null;
  let recordingChunks = [];
  let recordingTimerId = null;
  let recordingStopId = null;
  let recordingStartedAt = 0;
  let recordingSessionId = "";
  let recordingInitialMessage = "";
  let recordingLanguage = "auto";
  let recordingCancelled = false;
  let recordingFailure = null;
  const conversations = new Map();
  const sessionDetails = new Map();
  const sessionResetVersions = new Map();
  const deletingSessions = new Set();
  const deletedSessions = new Set();
  let manualSttController = null;
  let currentConversationSession = "";
  let historyLoadId = 0;
  let sessionListLoadId = 0;
  let sessionCreating = false;
  let conversationMessageId = 0;
  let ttsGeneration = 0;
  let ttsState = "idle";
  let ttsController = null;
  let ttsAudio = null;
  let ttsObjectUrl = null;
  let activeTtsButton = null;
  let activeTtsStatus = null;
  let ttsPlaybackCompletion = null;

  class ApiError extends Error {
    constructor(message, status = null, code = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
    }
  }

  const setStatus = (element, text, state) => {
    element.textContent = text;
    element.classList.remove("is-pending", "is-online", "is-offline");
    element.classList.add(`is-${state}`);
  };

  const requestJson = async (path, options = {}, externalController = null) => {
    const controller = externalController || new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(path, {
        ...options,
        headers: { Accept: "application/json", ...(options.headers || {}) },
        cache: "no-store",
        signal: controller.signal,
      });
      const body = await response.text();
      let data;
      try {
        data = body ? JSON.parse(body) : {};
      } catch (error) {
        throw new ApiError("서버 응답을 JSON으로 해석할 수 없습니다.", response.status);
      }
      return { response, data };
    } catch (error) {
      if (error.name === "AbortError") throw new ApiError("브라우저 요청 시간이 초과되었습니다.", 0, "browser_timeout");
      if (error instanceof ApiError) throw error;
      throw new ApiError("Flask API에 연결할 수 없습니다.", 0, "connection_failed");
    } finally {
      window.clearTimeout(timeoutId);
    }
  };

  const requireSuccess = ({ response, data }) => {
    if (!response.ok) {
      const apiError = data && data.error;
      throw new ApiError(
        apiError && typeof apiError.message === "string" ? apiError.message : `HTTP ${response.status} 요청 실패`,
        response.status,
        apiError && typeof apiError.code === "string" ? apiError.code : null,
      );
    }
    return data;
  };

  const refreshHealth = async () => {
    if (voiceTurns && voiceTurns.active) return;
    refreshHealthButton.disabled = true;
    refreshHealthButton.textContent = "Checking…";
    setStatus(controllerStatus, "확인 중", "pending");
    setStatus(ollamaStatus, "확인 중", "pending");
    setStatus(serverStatus, "확인 중", "pending");
    healthMessage.textContent = "Flask Controller와 Ollama 상태를 확인하고 있습니다.";
    try {
      const { data } = await requestJson("/health", { method: "GET" });
      if (!data || data.controller !== "ok" || typeof data.ollama_reachable !== "boolean" || typeof data.model !== "string") {
        throw new ApiError("상태 응답 형식이 올바르지 않습니다.");
      }
      const ollamaState = data.ollama_reachable ? "online" : "offline";
      setStatus(controllerStatus, "Online", "online");
      setStatus(ollamaStatus, data.ollama_reachable ? "Online" : "Offline", ollamaState);
      setStatus(serverStatus, data.ollama_reachable ? "Online" : "Offline", ollamaState);
      serverToggle.disabled = false;
      serverToggle.dataset.online = String(data.ollama_reachable);
      configuredModel.textContent = data.model;
      serverModel.textContent = data.model;
      healthMessage.textContent = data.ollama_reachable
        ? "Flask Controller와 Ollama 서버가 정상적으로 응답했습니다."
        : "Flask Controller는 Online이지만 Ollama 서버는 Offline입니다.";
    } catch (error) {
      setStatus(controllerStatus, "Offline", "offline");
      setStatus(ollamaStatus, "확인 불가", "pending");
      setStatus(serverStatus, "확인 불가", "pending");
      serverToggle.disabled = true;
      delete serverToggle.dataset.online;
      healthMessage.textContent = "Flask Controller 상태를 조회할 수 없습니다. 서버 실행 상태를 확인해 주세요.";
    } finally {
      refreshHealthButton.disabled = false;
      refreshHealthButton.textContent = "Refresh";
    }
  };

  const showView = (view) => {
    if (voiceTurns) voiceTurns.cancel("Screen changed.");
    if (["requesting", "recording", "transcribing"].includes(voiceState)) discardRecording();
    stopTts("Screen changed. TTS playback was stopped.");
    const showServers = view === "servers";
    guideView.hidden = showServers;
    serverView.hidden = !showServers;
    serverButton.classList.toggle("is-active", showServers);
    serverButton.setAttribute("aria-current", showServers ? "page" : "false");
    mainContent.scrollTop = 0;
    mainContent.focus({ preventScroll: true });
    if (showServers) refreshHealth();
  };

  const timestamp = () => new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());

  const appendLog = (level, api, message) => {
    const entry = document.createElement("div");
    entry.className = "log-entry";
    const head = document.createElement("div");
    head.className = "log-head";
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = timestamp();
    const badge = document.createElement("span");
    badge.className = `log-${level.toLowerCase()}`;
    badge.textContent = `[${level}]`;
    const apiName = document.createElement("span");
    apiName.className = "log-info";
    apiName.textContent = api;
    const body = document.createElement("div");
    body.className = "log-body";
    body.textContent = message;
    head.append(time, badge, apiName);
    entry.append(head, body);
    outputLog.append(entry);
    outputLog.scrollTop = outputLog.scrollHeight;
  };

  const parseServerTiming = (response) => {
    const timings = {};
    const header = response.headers.get("Server-Timing") || "";
    header.split(",").forEach((entry) => {
      const [namePart, ...parameters] = entry.trim().split(";");
      if (!namePart) return;
      const durationPart = parameters.find((part) => part.trim().startsWith("dur="));
      const duration = durationPart ? Number(durationPart.trim().slice(4)) : NaN;
      if (Number.isFinite(duration) && duration >= 0) timings[namePart] = duration / 1000;
    });
    return timings;
  };

  const timingValue = (seconds) => Number.isFinite(seconds) ? `${seconds.toFixed(3)}s` : "unavailable";

  const ttsTextLength = (text) => Array.from(text).length;

  const setTtsControlState = (button, status, state, message = "") => {
    if (button) {
      button.textContent = state === "idle" ? "▶ Play" : "■ Stop";
      button.classList.toggle("is-active", state !== "idle");
      button.setAttribute("aria-pressed", String(state !== "idle"));
    }
    if (status) {
      status.textContent = message;
      status.classList.toggle("is-error", state === "error");
    }
  };

  const releaseTtsResources = (playbackError = null) => {
    if (ttsController) ttsController.abort();
    ttsController = null;
    if (ttsAudio) {
      ttsAudio.onended = null;
      ttsAudio.onerror = null;
      ttsAudio.pause();
      ttsAudio.removeAttribute("src");
      ttsAudio.load();
    }
    ttsAudio = null;
    if (ttsObjectUrl) URL.revokeObjectURL(ttsObjectUrl);
    ttsObjectUrl = null;
    setTtsControlState(activeTtsButton, activeTtsStatus, "idle");
    activeTtsButton = null;
    activeTtsStatus = null;
    ttsState = "idle";
    const completion = ttsPlaybackCompletion;
    ttsPlaybackCompletion = null;
    if (completion) {
      if (playbackError) completion.reject(playbackError);
      else completion.resolve();
    }
  };

  const stopTts = (reason, writeLog = true) => {
    if (ttsState === "idle") return;
    ttsGeneration += 1;
    releaseTtsResources();
    if (writeLog) appendLog("INFO", "TTS PLAYBACK", reason);
  };

  const requestTtsWav = async (text, language, controller) => {
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    try {
      const response = await window.fetch("/voice/synthesize", {
        method: "POST",
        headers: { Accept: "audio/wav", "Content-Type": "application/json" },
        body: JSON.stringify({ text, language }),
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) {
        let data = {};
        try { data = await response.json(); } catch (error) { data = {}; }
        const apiError = data && data.error;
        throw new ApiError(
          apiError && typeof apiError.message === "string" ? apiError.message : `HTTP ${response.status} TTS request failed.`,
          response.status,
          apiError && typeof apiError.code === "string" ? apiError.code : null,
        );
      }
      const contentType = response.headers.get("Content-Type") || "";
      if (!contentType.toLowerCase().startsWith("audio/wav")) {
        throw new ApiError("The TTS response is not WAV audio.", 0, "invalid_tts_audio");
      }
      const blob = await response.blob();
      const receivedAt = performance.now();
      if (blob.size < 44) throw new ApiError("The TTS WAV response is empty.", 0, "invalid_tts_audio");
      const signature = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
      const ascii = String.fromCharCode(...signature);
      if (!ascii.startsWith("RIFF") || ascii.slice(8, 12) !== "WAVE") {
        throw new ApiError("The TTS response has an invalid WAV header.", 0, "invalid_tts_audio");
      }
      return { blob, receivedAt, serverTimings: parseServerTiming(response) };
    } catch (error) {
      if (error.name === "AbortError") {
        if (timedOut) throw new ApiError("TTS synthesis timed out.", 0, "browser_timeout");
        throw new ApiError("TTS playback was cancelled.", 0, "tts_cancelled");
      }
      if (error instanceof ApiError) throw error;
      throw new ApiError("The TTS server could not be reached.", 0, "connection_failed");
    } finally {
      window.clearTimeout(timeoutId);
    }
  };

  const startTts = async (button, status, message, turn = null, onSpeaking = null) => {
    if (deletingAll || deleteAllUncertain) return;
    if (deletingSessions.has(currentConversationSession) || deletedSessions.has(currentConversationSession)) return;
    if (turn && !voiceTurns.valid(turn)) return;
    if (ttsTextLength(message.content) > TTS_MAX_CODE_POINTS) {
      status.textContent = `TTS supports up to ${TTS_MAX_CODE_POINTS} characters.`;
      status.classList.add("is-error");
      appendLog("INFO", "POST /voice/synthesize", "The AI response exceeds the 500-character TTS limit and was not sent.");
      return;
    }
    if (ttsState !== "idle") stopTts("Previous TTS playback was stopped before starting another response.");

    const generation = ++ttsGeneration;
    const language = message.language || ttsLanguage.value;
    const startedAt = performance.now();
    ttsController = new AbortController();
    if (turn) turn.controllers.add(ttsController);
    activeTtsButton = button;
    activeTtsStatus = status;
    ttsState = "loading";
    setTtsControlState(button, status, "loading", "Loading…");
    appendLog("RUNNING", "POST /voice/synthesize", `Synthesizing AI response · Language: ${language}.`);

    try {
      const { blob, receivedAt, serverTimings } = await requestTtsWav(message.content, language, ttsController);
      if (generation !== ttsGeneration) return;
      ttsController = null;
      const elapsedSeconds = (performance.now() - startedAt) / 1000;
      appendLog(
        "SUCCESS",
        "POST /voice/synthesize",
        `Synthesis complete · Language: ${language} · Browser round trip: ${elapsedSeconds.toFixed(3)}s · Piper model load: ${timingValue(serverTimings.piper_model)} · Piper synthesis: ${timingValue(serverTimings.piper)} · WAV: ${(blob.size / 1024).toFixed(1)} KiB.`,
      );

      ttsObjectUrl = URL.createObjectURL(blob);
      ttsAudio = new Audio(ttsObjectUrl);
      if (turn) {
        turn.audio = ttsAudio;
        turn.objectUrl = ttsObjectUrl;
      }
      const finished = new Promise((resolve, reject) => {
        ttsPlaybackCompletion = { resolve, reject };
      });
      // A media error can precede resolution of play(). Handle the promise now.
      finished.catch(() => {});
      ttsState = "playing";
      setTtsControlState(button, status, "playing", "Playing…");
      ttsAudio.onended = () => {
        if (generation !== ttsGeneration) return;
        appendLog("INFO", "TTS PLAYBACK", `Playback completed · Language: ${language}.`);
        releaseTtsResources();
      };
      ttsAudio.onerror = () => {
        if (generation !== ttsGeneration) return;
        releaseTtsResources(new ApiError("Browser audio playback failed.", 0, "audio_playback_failed"));
      };
      await ttsAudio.play();
      if (generation === ttsGeneration) {
        if (onSpeaking) onSpeaking();
        const playbackStartupSeconds = (performance.now() - receivedAt) / 1000;
        appendLog("INFO", "TTS PLAYBACK", `Playback started · Language: ${language} · WAV received → Audio.play(): ${playbackStartupSeconds.toFixed(3)}s.`);
      }
      await finished;
    } catch (error) {
      if (generation !== ttsGeneration || error.code === "tts_cancelled") return;
      releaseTtsResources();
      const playbackBlocked = error && error.name === "NotAllowedError";
      if (playbackBlocked) {
        error = new ApiError("Browser playback was blocked. Use the answer's Play button to listen manually.", 0, "audio_playback_denied");
      }
      appendLog(
        "ERROR",
        playbackBlocked ? "TTS PLAYBACK" : "POST /voice/synthesize",
        playbackBlocked ? "The browser blocked audio playback. Press Play again and check site audio permission." : errorMessage(error),
      );
      if (turn) throw error;
    }
  };

  const renderSessionHeading = (sessionId) => {
    const detail = sessionDetails.get(sessionId);
    const uncertain = sessionId && chatGuard.reason(sessionId) === "chat_state_uncertain";
    const caption = detail ? `Session ${detail.number} — ${detail.title}`
      : uncertain ? "세션 상태 확인 필요 · + New Chat" : "선택된 세션 없음 · + New Chat";
    conversationSession.textContent = detail ? `SESSION ${detail.number}` : uncertain ? "SESSION · 확인 필요" : "SESSION · 없음";
    currentSessionLabel.textContent = caption;
    currentSessionLabel.title = caption;
  };
  const renderConversation = (sessionId) => {
    currentConversationSession = sessionId;
    renderSessionHeading(sessionId);
    conversationOutput.replaceChildren();
    const messages = conversations.get(sessionId) || [];
    if (messages.length === 0) {
      conversationEmpty.textContent = sessionId
        ? "Chat을 실행하면 현재 세션의 대화가 여기에 표시됩니다."
        : "세션이 없습니다. D의 + New Chat으로 새 대화를 시작하세요.";
      conversationOutput.append(conversationEmpty);
      return;
    }
    messages.forEach((message) => {
      const { role, content, id } = message;
      const entry = document.createElement("article");
      entry.className = `conversation-message is-${role}`;
      const label = document.createElement("span");
      label.className = "conversation-role";
      label.textContent = role === "user" ? "YOU" : "AI";
      const text = document.createElement("p");
      text.className = "conversation-text";
      text.textContent = content;
      entry.append(label, text);
      if (role === "assistant") {
        const actions = document.createElement("div");
        actions.className = "conversation-message-actions";
        const playButton = document.createElement("button");
        playButton.type = "button";
        playButton.className = "tts-play-button";
        playButton.dataset.messageId = String(id);
        playButton.textContent = "▶ Play";
        playButton.setAttribute("aria-pressed", "false");
        playButton.setAttribute("aria-label", "AI 답변 음성 재생");
        const playStatus = document.createElement("span");
        playStatus.className = "tts-message-status";
        if (ttsTextLength(content) > TTS_MAX_CODE_POINTS) {
          playButton.disabled = true;
          playStatus.classList.add("is-error");
          playStatus.textContent = `재생 불가 · ${TTS_MAX_CODE_POINTS}자 초과`;
        }
        actions.append(playButton, playStatus);
        entry.append(actions);
      }
      conversationOutput.append(entry);
    });
    conversationOutput.scrollTop = conversationOutput.scrollHeight;
  };

  const setConversationSession = (sessionId) => {
    if (sessionId !== currentConversationSession) stopTts("Session changed. Previous TTS playback was stopped.");
    renderConversation(sessionId || "");
  };

  const appendConversation = (sessionId, role, content, language = null) => {
    if (deletingAll || deleteAllUncertain) return null;
    if (deletedSessions.has(sessionId) || deletingSessions.has(sessionId)) return null;
    const messages = conversations.get(sessionId) || [];
    if (currentConversationSession === sessionId && ttsState !== "idle") {
      stopTts("Conversation changed. Previous TTS playback was stopped.");
    }
    messages.push({ id: ++conversationMessageId, role, content, language });
    conversations.set(sessionId, messages);
    if (currentConversationSession === sessionId) renderConversation(sessionId);
    return messages[messages.length - 1];
  };

  const sessionCaption = (detail) => `Session ${detail.number} — ${detail.title}`;
  const rememberSelection = (sessionId) => {
    try { if (chatStorage) chatStorage.setItem("local-ai-selected-session", sessionId); }
    catch (_error) { /* Selection remains in memory. */ }
  };
  const closeSessionPopup = () => {
    sessionPopup.hidden = true;
    sessionListButton.setAttribute("aria-expanded", "false");
  };
  const positionSessionPopup = () => {
    if (sessionPopup.hidden) return;
    let rect = sessionListButton.getBoundingClientRect();
    if (rect.top < 100 && typeof sessionListButton.scrollIntoView === "function") {
      sessionListButton.scrollIntoView({ block: "end" });
      rect = sessionListButton.getBoundingClientRect();
    }
    const room = Math.max(0, rect.top - 12);
    const above = room >= 80;
    const height = Math.min(320, above ? room : Math.max(0, window.innerHeight - 16));
    sessionPopup.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - Math.max(rect.width, 230) - 8))}px`;
    sessionPopup.style.width = `${Math.min(Math.max(rect.width, 230), window.innerWidth - 16)}px`;
    sessionPopup.style.maxHeight = `${height}px`;
    // Reserve header space; only the list scrolls, including in small windows.
    sessionPopupItems.style.maxHeight = `${Math.max(0, height - 46)}px`;
    sessionPopup.style.bottom = above ? `${Math.max(8, window.innerHeight - rect.top + 4)}px` : "auto";
    sessionPopup.style.top = above ? "auto" : "8px";
  };
  const refreshSessionList = async () => {
    const loadId = ++sessionListLoadId;
    const result = requireSuccess(await requestJson("/sessions", { method: "GET" }));
    if (!Array.isArray(result.sessions)) throw new ApiError("Session List 응답이 올바르지 않습니다.");
    if (loadId !== sessionListLoadId) return [];
    const latest = new Map();
    const items = result.sessions.filter((detail) => !deletedSessions.has(detail.session_id));
    for (const detail of items) {
      if (typeof detail.session_id !== "string" || !Number.isInteger(detail.number)
          || typeof detail.title !== "string") throw new ApiError("Session List 응답이 올바르지 않습니다.");
      latest.set(detail.session_id, detail);
    }
    sessionDetails.clear();
    latest.forEach((value, key) => sessionDetails.set(key, value));
    if (currentConversationSession) renderSessionHeading(currentConversationSession);
    sessionPopupItems.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.textContent = "저장된 세션이 없습니다.";
      sessionPopupItems.append(empty);
    }
    items.forEach((detail) => {
      const row = document.createElement("div");
      row.className = "session-row";
      row.dataset.sessionId = detail.session_id;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "session-choice";
      button.textContent = sessionCaption(detail);
      button.dataset.sessionId = detail.session_id;
      button.setAttribute("aria-current", String(detail.session_id === sessionInput.value));
      button.disabled = deletingAll || deleteAllUncertain || deletingSessions.has(detail.session_id);
      button.addEventListener("click", () => { closeSessionPopup(); selectSession(detail.session_id); });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "session-delete";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `${sessionCaption(detail)} 삭제`);
      remove.disabled = deletingAll || deleteAllUncertain || deletingSessions.has(detail.session_id);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteSession(detail.session_id);
      });
      row.append(button, remove);
      sessionPopupItems.append(row);
    });
    return items;
  };
  const loadSessionHistory = async (sessionId, revision) => {
    const loadId = ++historyLoadId;
    const resetVersion = sessionResetVersions.get(sessionId) || 0;
    const isCurrent = () => loadId === historyLoadId && revision === sessionRevision
      && sessionInput.value === sessionId
      && !deletingAll && !deleteAllUncertain && !deletedSessions.has(sessionId) && !deletingSessions.has(sessionId)
      && resetVersion === (sessionResetVersions.get(sessionId) || 0);
    try {
      const result = requireSuccess(await requestJson(`/sessions/${encodeURIComponent(sessionId)}/history`, { method: "GET" }));
      if (!isCurrent()) return;
      if (result.session_id !== sessionId || !Array.isArray(result.messages)) throw new ApiError("History 응답이 올바르지 않습니다.");
      if (chatGuard.reason(sessionId)) return; // Never overwrite a pending local Chat.
      const messages = result.messages.map((item) => {
        if (!["user", "assistant"].includes(item.role) || typeof item.content !== "string") {
          throw new ApiError("History 응답이 올바르지 않습니다.");
        }
        return { id: ++conversationMessageId, role: item.role, content: item.content,
          language: ["ko", "en"].includes(item.language) ? item.language : null };
      });
      conversations.set(sessionId, messages);
      sessionDetails.set(sessionId, { session_id: sessionId, number: result.number, title: result.title });
      renderConversation(sessionId);
      updateInputState();
    } catch (error) {
      if (isCurrent()) appendLog("ERROR", "GET /sessions/{id}/history", errorMessage(error));
    }
  };
  const selectSession = (sessionId) => {
    if (deletingAll || deleteAllUncertain) return;
    if (!sessionDetails.has(sessionId) || deletedSessions.has(sessionId) || deletingSessions.has(sessionId)) return;
    closeSessionPopup();
    sessionInput.value = sessionId;
    sessionInput.dispatchEvent(new Event("input"));
    rememberSelection(sessionId);
    loadSessionHistory(sessionId, sessionRevision);
  };
  const clearSelectedSession = () => {
    sessionInput.value = "";
    sessionInput.dispatchEvent(new Event("input"));
    rememberSelection("");
  };
  const deleteSession = async (sessionId) => {
    if (preparingDeleteAll || deletingAll || deleteAllUncertain) return;
    const detail = sessionDetails.get(sessionId);
    if (!detail || deletingSessions.has(sessionId) || deletedSessions.has(sessionId)) return;
    if (!window.confirm(`${sessionCaption(detail)}\n이 세션과 대화 기록을 완전히 삭제할까요?`)) return;
    const order = [...sessionDetails.keys()];
    const index = order.indexOf(sessionId);
    const fallbackOrder = [...order.slice(index + 1), ...order.slice(0, index).reverse()];
    const removeLocalSession = () => {
      deletedSessions.add(sessionId);
      sessionListLoadId += 1;
      sessionResetVersions.set(sessionId, (sessionResetVersions.get(sessionId) || 0) + 1);
      conversations.delete(sessionId);
      sessionDetails.delete(sessionId);
      if (sessionInput.value === sessionId) {
        const next = fallbackOrder.find((id) => sessionDetails.has(id) && !deletingSessions.has(id));
        if (next) selectSession(next);
        else clearSelectedSession();
      }
    };
    deletingSessions.add(sessionId);
    sessionListLoadId += 1;
    sessionResetVersions.set(sessionId, (sessionResetVersions.get(sessionId) || 0) + 1);
    if (sessionInput.value === sessionId) {
      sessionRevision += 1;
      voiceTurns.cancel("Session deletion approved.");
      if (["requesting", "recording", "transcribing"].includes(voiceState)) discardRecording();
      stopTts("Session deletion approved. Playback stopped.");
    }
    sessionPopupItems.querySelectorAll(".session-row").forEach((row) => {
      if (row.dataset.sessionId === sessionId) row.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    });
    updateInputState();
    appendLog("RUNNING", "DELETE /sessions/{id}", `Deleting Session ${detail.number}; waiting for observed work to finish.`);
    try {
      await chatGuard.beforeReset(sessionId);
      const packet = await chatGuard.run(sessionId, async (confirmCompletion) => {
        const response = await requestJson(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
        if (response.response.ok && (response.data.session_id !== sessionId || response.data.status !== "deleted")) {
          throw new ApiError("Invalid Delete response; server completion is unknown.");
        }
        confirmCompletion();
        return response;
      });
      const result = requireSuccess(packet);
      if (result.session_id !== sessionId || result.status !== "deleted") throw new ApiError("Invalid Delete response.");
      removeLocalSession();
      appendLog("SUCCESS", "DELETE /sessions/{id}", `Session ${detail.number} deleted.`);
    } catch (error) {
      appendLog("ERROR", "DELETE /sessions/{id}", errorMessage(error));
      if ([404, 410].includes(error.status)) removeLocalSession();
      if (chatGuard.reason(sessionId) === "chat_state_uncertain") {
        appendLog("INFO", "SESSION STATE", "Server completion is unknown. This session stays blocked; no automatic retry or new session was created.");
      }
    } finally {
      deletingSessions.delete(sessionId);
      updateInputState();
      refreshSessionList().then(() => {
        if (!deletedSessions.has(sessionId) && !chatGuard.reason(sessionId) && sessionInput.value === sessionId) {
          loadSessionHistory(sessionId, sessionRevision);
        }
      }).catch((error) => appendLog("ERROR", "GET /sessions", errorMessage(error)));
    }
  };
  const deleteAllSessions = async () => {
    if (preparingDeleteAll || deletingAll || deleteAllUncertain || sessionCreating || deletingSessions.size) return;
    preparingDeleteAll = true;
    updateInputState();
    const affected = new Set();
    let approved = false, sent = false, confirmed = false;
    try {
      // A confirmation preview must not be invalidated by a concurrent Chat's
      // list refresh, nor mutate the Conversation if the user cancels.
      const preview = requireSuccess(await requestJson("/sessions", { method: "GET" }));
      const items = preview.sessions;
      if (!Array.isArray(items) || !items.every((item) => item && typeof item.session_id === "string")) {
        throw new ApiError("Invalid Delete All session list.");
      }
      if (!window.confirm(`전체 ${items.length}개 세션과 모든 대화 기록을 삭제할까요?\n성공하면 다음 Session 번호가 1부터 시작합니다.`)) return;
      approved = true;
      deletingAll = true;
      [...items.map((item) => item.session_id), ...sessionDetails.keys(), ...chatGuard.pending.keys(), ...chatGuard.uncertain].forEach((id) => affected.add(id));
      affected.forEach((id) => {
        deletingSessions.add(id);
        sessionResetVersions.set(id, (sessionResetVersions.get(id) || 0) + 1);
      });
      sessionRevision += 1;
      historyLoadId += 1;
      sessionListLoadId += 1;
      closeSessionPopup();
      voiceTurns.cancel("Delete All approved.");
      if (["requesting", "recording", "transcribing"].includes(voiceState)) discardRecording();
      stopTts("Delete All approved. Playback stopped.");
      appendLog("RUNNING", "DELETE /sessions", "Waiting for observed Chat/Reset operations before Delete All.");
      // Do not abort Chat transport: observe its completion, then delete.
      // An uncertain Chat prevents the bulk request altogether.
      await Promise.all([...affected].map((id) => chatGuard.beforeReset(id)));
      sent = true;
      persistDeleteAll(true); // A reload during the request must remain blocked.
      const packet = await requestJson("/sessions", { method: "DELETE" });
      const data = packet.data;
      if (packet.response.ok && (data.status !== "deleted_all"
          || !Number.isInteger(data.deleted_count) || !Array.isArray(data.deleted_session_ids)
          || data.deleted_count !== data.deleted_session_ids.length
          || !data.deleted_session_ids.every((id) => typeof id === "string"))) {
        throw new ApiError("Invalid Delete All response; server completion is unknown.");
      }
      confirmed = true;
      persistDeleteAll(false);
      requireSuccess(packet);
      [...affected, ...data.deleted_session_ids].forEach((id) => deletedSessions.add(id));
      conversations.clear();
      sessionDetails.clear();
      clearSelectedSession(); // Keeps the user's D draft unchanged.
      sessionPopupItems.replaceChildren();
      appendLog("SUCCESS", "DELETE /sessions", `${data.deleted_count} sessions deleted. Next Session number: 1.`);
    } catch (error) {
      appendLog("ERROR", "DELETE /sessions", errorMessage(error));
      if (sent && !confirmed) {
        appendLog("INFO", "SESSION STATE", "Delete All completion is unknown. Session changes remain blocked across reload; verify server state before resynchronization. No automatic retry.");
      }
    } finally {
      if (approved) {
        sessionRevision += 1;
        historyLoadId += 1;
        sessionListLoadId += 1;
      }
      affected.forEach((id) => deletingSessions.delete(id));
      deletingAll = false;
      preparingDeleteAll = false;
      updateInputState();
      if (approved && !deleteAllUncertain) {
        refreshSessionList().then(() => {
          const id = sessionInput.value;
          if (id && sessionDetails.has(id) && !chatGuard.reason(id)) loadSessionHistory(id, sessionRevision);
          updateInputState();
        }).catch((error) => appendLog("ERROR", "GET /sessions", errorMessage(error)));
      }
    }
  };
  const createNewSession = async () => {
    if (preparingDeleteAll || deletingAll || deleteAllUncertain) return;
    if (sessionCreating || requestRunning) return;
    sessionCreating = true;
    newChatButton.disabled = true;
    try {
      const detail = requireSuccess(await requestJson("/sessions", { method: "POST" }));
      if (typeof detail.session_id !== "string" || !Number.isInteger(detail.number)) throw new ApiError("새 세션 응답이 올바르지 않습니다.");
      sessionListLoadId += 1; // Ignore a list snapshot taken before this creation.
      sessionDetails.set(detail.session_id, detail);
      selectSession(detail.session_id);
      appendLog("SUCCESS", "POST /sessions", `Session ${detail.number} created.`);
    } catch (error) {
      appendLog("ERROR", "POST /sessions", errorMessage(error));
    } finally {
      sessionCreating = false;
      updateInputState();
    }
  };
  const initializeSessions = async () => {
    try {
      const items = await refreshSessionList();
      if (!items.length) { clearSelectedSession(); return; }
      let selected = null;
      try { selected = chatStorage && chatStorage.getItem("local-ai-selected-session"); } catch (_error) { /* Use first session. */ }
      const existing = items.find((item) => item.session_id === selected) || items[0];
      selectSession(existing.session_id);
    } catch (error) {
      appendLog("ERROR", "GET /sessions", errorMessage(error));
      updateInputState();
    }
  };

  const secondsFromNanoseconds = (value) => `${(value / 1_000_000_000).toFixed(3)}s`;
  const formatChatMetrics = (data, browserSeconds = NaN, serverTimings = {}) => {
    const lines = [];
    if (Number.isFinite(browserSeconds)) lines.push(`Browser Chat round trip: ${browserSeconds.toFixed(3)}s`);
    if (Number.isFinite(serverTimings.ollama)) lines.push(`Qwen request → complete response: ${serverTimings.ollama.toFixed(3)}s`);
    lines.push("Qwen first token: unavailable (non-streaming /chat contract)");
    if (typeof data.elapsed_seconds === "number") lines.push(`Elapsed: ${data.elapsed_seconds.toFixed(3)}s`);
    const metrics = data.metrics && typeof data.metrics === "object" ? data.metrics : {};
    if (typeof metrics.eval_count === "number") lines.push(`Generated tokens: ${metrics.eval_count}`);
    if (typeof metrics.prompt_eval_count === "number") lines.push(`Prompt tokens: ${metrics.prompt_eval_count}`);
    if (typeof metrics.tokens_per_second === "number") lines.push(`Speed: ${metrics.tokens_per_second.toFixed(2)} tokens/s`);
    for (const [key, label] of [
      ["total_duration", "Ollama total"], ["load_duration", "Model load"],
      ["prompt_eval_duration", "Prompt eval"], ["eval_duration", "Generation"],
    ]) {
      if (typeof metrics[key] === "number") lines.push(`${label}: ${secondsFromNanoseconds(metrics[key])}`);
    }
    return lines.join("\n");
  };

  const errorMessage = (error) => {
    const parts = [];
    if (error.status) parts.push(`HTTP ${error.status}`);
    if (error.code) parts.push(error.code);
    parts.push(error.message || "알 수 없는 오류가 발생했습니다.");
    return parts.join(" · ");
  };

  const isVoiceBusy = () => Boolean(voiceTurns && voiceTurns.active)
    || ["requesting", "recording", "transcribing"].includes(voiceState);

  const formatRecordingTime = (milliseconds) => {
    const totalSeconds = Math.min(Math.floor(milliseconds / 1000), 30);
    return `00:${String(totalSeconds).padStart(2, "0")}`;
  };

  const clearRecordingTimers = () => {
    if (recordingTimerId !== null) window.clearInterval(recordingTimerId);
    if (recordingStopId !== null) window.clearTimeout(recordingStopId);
    recordingTimerId = null;
    recordingStopId = null;
  };

  const stopMediaTracks = () => {
    if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  };

  const setVoiceState = (state, message) => {
    voiceState = state;
    voiceStatus.dataset.state = state;
    voiceStatus.textContent = message;
    voiceStatus.title = message;
    recordVoiceButton.classList.toggle("is-recording", state === "recording");
    recordVoiceButton.setAttribute("aria-pressed", String(state === "recording"));
    recordVoiceButton.textContent = state === "recording" ? "Stop" : "Record";
    updateInputState();
  };

  const selectedRecordingMimeType = () => {
    if (typeof window.MediaRecorder.isTypeSupported !== "function") return "";
    return VOICE_MIME_TYPES.find((type) => window.MediaRecorder.isTypeSupported(type)) || "";
  };

  const microphoneErrorMessage = (error) => {
    if (error && ["NotAllowedError", "SecurityError"].includes(error.name)) {
      return "Microphone permission was denied.";
    }
    if (error && error.name === "NotFoundError") return "No microphone input device was found.";
    if (error && ["NotReadableError", "AbortError"].includes(error.name)) {
      return "The microphone is unavailable or was disconnected.";
    }
    return "The microphone could not be started.";
  };

  const applyTranscription = (text, sessionId, initialMessage) => {
    if (sessionInput.value.trim() !== sessionId) {
      appendLog("INFO", "POST /voice/transcribe", "Transcription was not applied because the Session ID changed.");
      return false;
    }
    if (lockButton.getAttribute("aria-pressed") === "true") {
      appendLog("INFO", "POST /voice/transcribe", "Transcription was not applied because Input is locked.");
      return false;
    }
    const currentMessage = messageInput.value;
    const changed = currentMessage !== initialMessage;
    if ((changed || currentMessage.trim()) && !window.confirm(
      changed
        ? "The message changed while transcribing. Replace it with the new transcription?"
        : "Replace the existing message with the new transcription?",
    )) {
      appendLog("INFO", "POST /voice/transcribe", "The existing message was kept.");
      return false;
    }
    messageInput.value = text;
    messageInput.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  };

  const recordingToWav = async (blob) => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (typeof AudioContextClass !== "function") {
      throw new ApiError("This browser cannot convert microphone audio to WAV.", 0, "audio_conversion_unsupported");
    }
    const context = new AudioContextClass();
    try {
      const audioBuffer = await context.decodeAudioData(await blob.arrayBuffer());
      if (!audioBuffer.length || !audioBuffer.numberOfChannels) {
        throw new Error("Decoded recording is empty");
      }
      const wav = new ArrayBuffer(44 + audioBuffer.length * 2);
      const view = new DataView(wav);
      const writeText = (offset, value) => {
        for (let index = 0; index < value.length; index += 1) {
          view.setUint8(offset + index, value.charCodeAt(index));
        }
      };
      writeText(0, "RIFF");
      view.setUint32(4, wav.byteLength - 8, true);
      writeText(8, "WAVE");
      writeText(12, "fmt ");
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, audioBuffer.sampleRate, true);
      view.setUint32(28, audioBuffer.sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeText(36, "data");
      view.setUint32(40, audioBuffer.length * 2, true);
      const channels = Array.from(
        { length: audioBuffer.numberOfChannels },
        (_unused, index) => audioBuffer.getChannelData(index),
      );
      for (let frame = 0; frame < audioBuffer.length; frame += 1) {
        const sample = channels.reduce((sum, channel) => sum + channel[frame], 0) / channels.length;
        const clipped = Math.max(-1, Math.min(1, sample));
        view.setInt16(44 + frame * 2, clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff, true);
      }
      return new Blob([wav], { type: "audio/wav" });
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError("The browser could not decode the microphone recording.", 0, "audio_conversion_failed");
    } finally {
      await context.close().catch(() => {});
    }
  };

  const uploadRecording = async (blob, sessionId, initialMessage, language, recordingStoppedAt, turn = null, operationId = voiceOperationId) => {
    const revision = sessionRevision;
    const stillCurrent = () => turn ? voiceTurns.valid(turn)
      : operationId === voiceOperationId && revision === sessionRevision
        && sessionInput.value.trim() === sessionId && lockButton.getAttribute("aria-pressed") !== "true";
    const requireCurrent = () => {
      if (!stillCurrent()) throw new ApiError("Recording was cancelled or its session changed.", 0, "voice_cancelled");
    };
    requireCurrent();
    if (!blob.size) throw new ApiError("The recording is empty.", 0, "audio_empty");
    if (blob.size > VOICE_MAX_BYTES) {
      throw new ApiError("The recording exceeds 10 MiB and was not uploaded.", 0, "audio_too_large");
    }
    const supportedType = !blob.type || ["audio/webm", "audio/mp4", "audio/wav", "audio/x-wav"]
      .some((type) => blob.type.toLowerCase().startsWith(type));
    if (!supportedType) {
      throw new ApiError(`Unsupported browser recording format: ${blob.type}`, 0, "unsupported_audio_format");
    }
    const uploadBlob = blob.type.toLowerCase().startsWith("audio/wav")
      ? blob
      : await recordingToWav(blob);
    const wavReadyAt = performance.now();
    requireCurrent();
    if (uploadBlob.size > VOICE_MAX_BYTES) {
      throw new ApiError("The WAV recording exceeds 10 MiB and was not uploaded.", 0, "audio_too_large");
    }
    const formData = new FormData();
    formData.append("audio", uploadBlob, "recording.wav");
    formData.append("language", language);
    appendLog(
      "RUNNING",
      "POST /voice/transcribe",
      `Browser WAV ready · ${(uploadBlob.size / 1024).toFixed(1)} KiB · Transcribing locally.`,
    );
    const requestStartedAt = performance.now();
    const controller = new AbortController();
    if (turn) turn.controllers.add(controller);
    else manualSttController = controller;
    let requestResult;
    try {
      requestResult = await requestJson("/voice/transcribe", {
        method: "POST", body: formData,
      }, controller);
    } finally {
      if (turn) turn.controllers.delete(controller);
      if (manualSttController === controller) manualSttController = null;
    }
    requireCurrent();
    const responseReceivedAt = performance.now();
    const result = requireSuccess(requestResult);
    if (!result || typeof result.text !== "string" || !result.text.trim()
        || typeof result.language !== "string" || typeof result.elapsed_seconds !== "number") {
      throw new ApiError("The STT response format is invalid.", 0, "invalid_stt_response");
    }
    const applied = turn ? false : applyTranscription(result.text, sessionId, initialMessage);
    const serverTimings = parseServerTiming(requestResult.response);
    const browserRequestSeconds = (responseReceivedAt - requestStartedAt) / 1000;
    const serverTotal = serverTimings.total;
    const transportOverhead = Number.isFinite(serverTotal)
      ? Math.max(0, browserRequestSeconds - serverTotal)
      : NaN;
    appendLog(
      "SUCCESS",
      "POST /voice/transcribe",
      `Language: ${result.language}\nRecording stop → WAV ready: ${((wavReadyAt - recordingStoppedAt) / 1000).toFixed(3)}s\nBrowser STT round trip: ${browserRequestSeconds.toFixed(3)}s\nClient/network overhead (upload + response): ${timingValue(transportOverhead)}\nFlask multipart parse/read: ${timingValue(serverTimings.upload)}\nAudio decode: ${timingValue(serverTimings.decode)}\nWhisper model load: ${timingValue(serverTimings.stt_model)}\nWhisper inference: ${timingValue(serverTimings.whisper)}\nServer total: ${result.elapsed_seconds.toFixed(3)}s\n${applied ? "Result placed in D Input for review." : "Result was not placed in D Input."}`,
    );
    return result;
  };

  const finishRecording = async (operationId, turn) => {
    // A cancelled recorder's late stop event must not touch a newer recorder.
    if (operationId !== voiceOperationId) return;
    const recordingStoppedAt = performance.now();
    clearRecordingTimers();
    stopMediaTracks();
    let chunks = recordingChunks;
    recordingChunks = [];
    const recorder = mediaRecorder;
    mediaRecorder = null;
    if (recordingFailure) {
      const failure = recordingFailure;
      recordingFailure = null;
      setVoiceState("error", failure);
      appendLog("ERROR", "MICROPHONE", failure);
      if (turn) voiceTurns.fail(turn, new ApiError(failure, 0, "microphone_failed"));
      return;
    }
    if (operationId !== voiceOperationId || recordingCancelled) {
      setVoiceState("idle", "Idle · Recording cancelled; no audio was uploaded.");
      return;
    }
    let blob = new Blob(chunks, { type: recorder && recorder.mimeType ? recorder.mimeType : "" });
    chunks = null;
    if (turn) turn.recordingStoppedAt = recordingStoppedAt;
    setVoiceState("transcribing", "Transcribing · Local STT is processing the recording.");
    if (turn) {
      await voiceTurns.run(turn, async () => {
        try {
          return await uploadRecording(
            blob, turn.sessionId, turn.initialDraft, turn.sttLanguage, recordingStoppedAt, turn, operationId,
          );
        } finally { blob = null; }
      });
      return;
    }
    try {
      await uploadRecording(
        blob,
        recordingSessionId,
        recordingInitialMessage,
        recordingLanguage,
        recordingStoppedAt,
      );
      if (operationId === voiceOperationId) setVoiceState("idle", "Idle · Review or edit the recognized text before sending Chat.");
    } catch (error) {
      if (operationId !== voiceOperationId || error.code === "voice_cancelled") return;
      setVoiceState("error", `Error · ${errorMessage(error)}`);
      appendLog("ERROR", "POST /voice/transcribe", errorMessage(error));
    } finally {
      if (operationId === voiceOperationId) recordingChunks = [];
    }
  };

  const stopRecording = (cancel = false) => {
    if (voiceState === "requesting") {
      voiceOperationId += 1;
      recordingCancelled = true;
      clearRecordingTimers();
      stopMediaTracks();
      setVoiceState("idle", "Idle · Microphone request cancelled.");
      return;
    }
    if (voiceState !== "recording" || !mediaRecorder) return;
    recordingCancelled = recordingCancelled || cancel;
    clearRecordingTimers();
    setVoiceState(
      cancel ? "idle" : "transcribing",
      cancel ? "Idle · Recording cancelled; no audio was uploaded." : "Transcribing · Preparing the recording.",
    );
    if (mediaRecorder.state !== "inactive") mediaRecorder.stop();
  };

  const startRecording = async () => {
    if (deletingAll || deleteAllUncertain) return;
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    if (!sessionInput.value || deletingSessions.has(sessionInput.value) || deletedSessions.has(sessionInput.value)) return;
    if (locked || requestRunning || isVoiceBusy()) return;
    if (voiceTurns.active) return;
    try {
      chatGuard.assertAvailable(sessionInput.value.trim());
    } catch (error) {
      appendLog("ERROR", "CHAT SESSION", errorMessage(error));
      return;
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function"
        || typeof window.MediaRecorder !== "function") {
      const message = "Microphone recording is not supported by this browser or context.";
      setVoiceState("error", `Error · ${message}`);
      appendLog("ERROR", "MICROPHONE", message);
      return;
    }
    let turn = null;
    if (voiceModeEnabled) {
      try {
        turn = voiceTurns.begin();
      } catch (error) {
        appendLog("ERROR", "VOICE CONVERSATION", errorMessage(error));
        return;
      }
    }
    stopTts("Microphone recording started. Previous playback stopped.");
    recordingTurn = turn;
    const operationId = ++voiceOperationId;
    recordingCancelled = false;
    recordingFailure = null;
    recordingSessionId = sessionInput.value.trim();
    recordingInitialMessage = messageInput.value;
    recordingLanguage = turn ? turn.sttLanguage : sttLanguage.value;
    voiceTimer.textContent = "00:00";
    setVoiceState("requesting", "Requesting Permission · Allow microphone access in the browser.");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (operationId !== voiceOperationId || voiceState !== "requesting") {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      mediaStream = stream;
      if (turn) turn.mediaStream = stream;
      const mimeType = selectedRecordingMimeType();
      mediaRecorder = mimeType
        ? new window.MediaRecorder(stream, { mimeType })
        : new window.MediaRecorder(stream);
      recordingChunks = [];
      if (turn) turn.mediaRecorder = mediaRecorder;
      mediaRecorder.addEventListener("dataavailable", (event) => {
        if (operationId !== voiceOperationId) return;
        if (event.data && event.data.size) recordingChunks.push(event.data);
      });
      mediaRecorder.addEventListener("error", () => {
        if (operationId !== voiceOperationId) return;
        recordingFailure = "The recording device reported an error.";
        stopRecording(true);
      });
      mediaRecorder.addEventListener("stop", () => { finishRecording(operationId, turn); }, { once: true });
      stream.getTracks().forEach((track) => track.addEventListener("ended", () => {
        if (operationId !== voiceOperationId) return;
        if (voiceState === "recording") {
          recordingFailure = "The microphone was disconnected during recording.";
          stopRecording(true);
        }
      }, { once: true }));
      // Emit one complete media container when recording stops. Short
      // timeslices can produce fragments that some FFmpeg/PyAV builds reject.
      mediaRecorder.start();
      recordingStartedAt = performance.now();
      recordingTimerId = window.setInterval(() => {
        voiceTimer.textContent = formatRecordingTime(performance.now() - recordingStartedAt);
      }, 250);
      recordingStopId = window.setTimeout(() => {
        voiceTimer.textContent = "00:30";
        stopRecording(false);
      }, VOICE_MAX_MS);
      setVoiceState("recording", "Recording · Press Stop or wait for the 30-second limit.");
    } catch (error) {
      if (operationId !== voiceOperationId) return;
      stopMediaTracks();
      const message = microphoneErrorMessage(error);
      setVoiceState("error", `Error · ${message}`);
      appendLog("ERROR", "MICROPHONE", message);
      if (turn) voiceTurns.fail(turn, new ApiError(message, 0, "microphone_failed"));
    }
  };

  const updateInputState = () => {
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    const voiceBusy = isVoiceBusy();
    const automatic = Boolean(voiceTurns && voiceTurns.active);
    const sessionId = sessionInput.value.trim();
    const blocked = sessionId ? chatGuard.reason(sessionId) : null;
    const unavailable = deletingAll || deleteAllUncertain || deletingSessions.has(sessionId) || deletedSessions.has(sessionId);
    newChatButton.disabled = preparingDeleteAll || deletingAll || deleteAllUncertain || sessionCreating || requestRunning;
    deleteAllSessionsButton.disabled = preparingDeleteAll || deletingAll || deleteAllUncertain || sessionCreating || deletingSessions.size > 0;
    deleteAllSessionsButton.textContent = deletingAll ? "Deleting…" : preparingDeleteAll ? "Loading…" : "Delete All";
    sessionPopupItems.querySelectorAll("button").forEach((button) => {
      const row = button.closest(".session-row");
      button.disabled = deletingAll || deleteAllUncertain || Boolean(row && deletingSessions.has(row.dataset.sessionId));
    });
    messageInput.disabled = locked || !sessionId || unavailable || requestRunning || voiceBusy;
    sttLanguage.disabled = locked || requestRunning || voiceBusy;
    recordVoiceButton.disabled = locked || !sessionId || unavailable || requestRunning
      || Boolean(blocked) || (automatic && voiceState !== "recording")
      || (["requesting", "transcribing"].includes(voiceState));
    recordVoiceButton.textContent = voiceState === "recording"
      ? (automatic ? "Stop & Send" : "Stop") : "Record";
    toolButtons.forEach((button) => {
      button.disabled = requestRunning || voiceBusy;
    });
    resetSessionButton.disabled = !sessionId || unavailable || requestRunning || (voiceBusy && !automatic);
    ttsLanguage.disabled = voiceBusy;
    voiceModeButton.disabled = requestRunning || (voiceBusy && !automatic);
    cancelVoiceTurnButton.disabled = !automatic;
    const messages = conversations.get(currentConversationSession) || [];
    conversationOutput.querySelectorAll(".tts-play-button").forEach((button) => {
      const message = messages.find((item) => String(item.id) === button.dataset.messageId);
      button.disabled = unavailable || automatic || !message || ttsTextLength(message.content) > TTS_MAX_CODE_POINTS;
    });
    refreshHealthButton.disabled = automatic;
    if (blocked && !automatic && !requestRunning) {
      voiceStatus.dataset.state = "error";
      voiceStatus.textContent = blocked === "chat_pending"
        ? "Previous Chat is finishing. No new Chat can be sent for this session."
        : "Chat state uncertain · 이 세션은 차단되었습니다. 서버 기록 재동기화가 필요합니다. 새 Session ID를 사용하세요.";
    }
    voiceStatus.title = voiceStatus.textContent;
    lockNote.title = lockNote.textContent;
  };

  const sendChat = async (sessionId, message, responseLanguage, automatic = false) => {
    const startedAt = performance.now();
    let chatRequest;
    try {
      chatRequest = await chatGuard.run(sessionId, async (confirmCompletion) => {
        const packet = await requestJson("/chat", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(automatic ? { "X-Local-AI-Response-Style": "voice" } : {}),
          },
          body: JSON.stringify({ session_id: sessionId, message, response_language: responseLanguage }),
        });
        // A parsed response from the controller confirms the HTTP handler ended,
        // including structured HTTP errors. Transport/parse failure stays blocked.
        confirmCompletion();
        return packet;
      });
      const result = requireSuccess(chatRequest);
      if (typeof result.message !== "string" || !result.message.trim()) {
        throw new ApiError("AI response is empty or invalid.", 502, "invalid_ollama_response");
      }
      appendLog("SUCCESS", "POST /chat", formatChatMetrics(
        result, (performance.now() - startedAt) / 1000, parseServerTiming(chatRequest.response),
      ));
      refreshSessionList().catch((error) => appendLog("ERROR", "GET /sessions", errorMessage(error)));
      return result;
    } finally {
      if (chatGuard.reason(sessionId) === "chat_state_uncertain") {
        appendLog("ERROR", "CHAT SESSION", "Server completion is unknown; this session remains blocked. No automatic retry or Reset was sent. Use a new Session ID or verify server-side resynchronization.");
      }
      updateInputState();
    }
  };

  const resetCurrentSession = async () => {
    if (deletingAll || deleteAllUncertain) return;
    if (requestRunning) return;
    const sessionId = sessionInput.value.trim();
    if (deletingSessions.has(sessionId) || deletedSessions.has(sessionId)) return;
    if (!sessionId) return;
    if (!window.confirm(`세션 '${sessionId}'의 대화 기록을 초기화할까요?`)) {
      appendLog("INFO", "POST /sessions/{id}/reset", "사용자가 세션 초기화를 취소했습니다.");
      return;
    }
    // Invalidate snapshots taken before Reset, only for its target session.
    sessionResetVersions.set(sessionId, (sessionResetVersions.get(sessionId) || 0) + 1);
    voiceTurns.cancel("Reset approved.");
    if (["requesting", "recording"].includes(voiceState)) stopRecording(true);
    stopTts("Session reset. TTS playback was stopped.");
    const apiLabel = `POST /sessions/${sessionId}/reset`;
    appendLog("RUNNING", apiLabel, "Waiting for any observed Chat to finish before Reset.");
    setRequestRunning(true);
    try {
      await chatGuard.beforeReset(sessionId);
      const packet = await chatGuard.run(sessionId, async (confirmCompletion) => {
        const resetPacket = await requestJson(`/sessions/${encodeURIComponent(sessionId)}/reset`, { method: "POST" });
        confirmCompletion();
        return resetPacket;
      });
      const result = requireSuccess(packet);
      if (result.session_id !== sessionId || result.status !== "reset") throw new ApiError("Invalid Reset response.");
      // Also reject snapshots taken while the server Reset was still pending.
      sessionResetVersions.set(sessionId, (sessionResetVersions.get(sessionId) || 0) + 1);
      conversations.delete(sessionId);
      if (currentConversationSession === sessionId) renderConversation(sessionId);
      refreshSessionList().catch((error) => appendLog("ERROR", "GET /sessions", errorMessage(error)));
      appendLog("SUCCESS", apiLabel, `Session '${result.session_id}' reset complete.`);
    } catch (error) {
      appendLog("ERROR", apiLabel, errorMessage(error));
    } finally {
      setRequestRunning(false);
    }
  };

  const selectTool = (tool) => {
    if (requestRunning) return;
    toolButtons.forEach((button) => {
      const selected = button.dataset.tool === tool;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    updateInputState();
  };

  const setRequestRunning = (running) => {
    requestRunning = running;
    toolButtons.forEach((button) => { button.disabled = running; });
    lockButton.disabled = running;
    updateInputState();
  };

  const runSelectedTool = async (tool = "chat") => {
    if (tool !== "health" && (deletingAll || deleteAllUncertain)) return;
    if (tool === "reset") return resetCurrentSession();
    if (requestRunning) return;
    if (isVoiceBusy()) {
      appendLog("ERROR", "SYSTEM", "Wait for microphone recording or transcription to finish.");
      return;
    }
    const sessionId = sessionInput.value.trim();
    const message = messageInput.value.trim();
    if (tool === "chat" && (deletingSessions.has(sessionId) || deletedSessions.has(sessionId))) return;
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    if (tool === "chat" && locked) {
      appendLog("ERROR", "POST /chat", "Chat을 실행하려면 Input을 Unlock해 주세요.");
      return;
    }
    if ((tool === "chat" || tool === "reset") && !sessionId) {
      appendLog("ERROR", tool === "chat" ? "POST /chat" : "POST /sessions/{id}/reset", "세션이 준비될 때까지 기다려 주세요.");
      return;
    }
    if (tool === "chat" && !message) {
      appendLog("ERROR", "POST /chat", "빈 메시지는 전송할 수 없습니다.");
      return;
    }
    if (tool === "chat") {
      try { chatGuard.assertAvailable(sessionId); }
      catch (error) { appendLog("ERROR", "CHAT SESSION", errorMessage(error)); return; }
    }
    const apiLabel = tool === "health" ? "GET /health" : tool === "chat" ? "POST /chat" : `POST /sessions/${sessionId}/reset`;
    if (tool === "chat") {
      setConversationSession(sessionId);
      appendConversation(sessionId, "user", message);
    }
    appendLog("RUNNING", apiLabel, "요청을 전송했습니다.");
    setRequestRunning(true);
    try {
      let result;
      if (tool === "health") {
        result = requireSuccess(await requestJson("/health", { method: "GET" }));
        appendLog("SUCCESS", apiLabel, `Controller: ${result.controller}\nOllama: ${result.ollama_reachable ? "Online" : "Offline"}\nModel: ${result.model}`);
      } else if (tool === "chat") {
        const responseLanguage = ttsLanguage.value;
        result = await sendChat(sessionId, message, responseLanguage);
        appendConversation(sessionId, "assistant", result.message, responseLanguage);
      }
    } catch (error) {
      appendLog("ERROR", apiLabel, errorMessage(error));
    } finally {
      setRequestRunning(false);
    }
  };

  const activateTool = async (tool) => {
    if (voiceTurns.active) return;
    selectTool(tool);
    await runSelectedTool(tool);
  };

  const toggleOllamaServer = async () => {
    if (serverToggle.disabled || requestRunning || voiceTurns.active) return;
    const online = serverToggle.dataset.online === "true";
    if (online && !window.confirm("Ollama 서버를 종료할까요? 이 콘솔뿐 아니라 다른 프로그램에서 진행 중인 Ollama 작업도 중단될 수 있습니다.")) return;
    serverToggle.disabled = true;
    const apiLabel = `POST /ollama/${online ? "stop" : "start"}`;
    appendLog("RUNNING", apiLabel, online ? "Stopping Ollama server." : "Starting Ollama server.");
    setStatus(serverStatus, online ? "Stopping…" : "Starting…", "pending");
    try {
      requireSuccess(await requestJson(`/ollama/${online ? "stop" : "start"}`, {
        method: "POST",
        headers: { "X-Local-AI-Control": "web-console" },
      }));
      appendLog("SUCCESS", apiLabel, online ? "Ollama stop request completed." : "Ollama start request completed.");
    } catch (error) {
      healthMessage.textContent = errorMessage(error);
      appendLog("ERROR", apiLabel, errorMessage(error));
    } finally {
      await refreshHealth();
    }
  };

  const discardRecording = () => {
    voiceOperationId += 1;
    recordingCancelled = true;
    if (manualSttController) manualSttController.abort();
    manualSttController = null;
    clearRecordingTimers();
    const recorder = mediaRecorder;
    mediaRecorder = null;
    recordingChunks = [];
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch (_error) { /* May already be stopping. */ }
    }
    stopMediaTracks();
    recordingTurn = null;
    voiceState = "idle";
  };

  voiceTurns = new VoiceTurnManager({
    context: () => ({
      enabled: voiceModeEnabled,
      locked: lockButton.getAttribute("aria-pressed") === "true",
      sessionId: sessionInput.value.trim(), revision: sessionRevision,
      draft: messageInput.value, sttLanguage: sttLanguage.value,
      responseLanguage: ttsLanguage.value,
    }),
    state: (state, detail, turn) => {
      voiceState = state.toLowerCase();
      voiceStatus.dataset.state = voiceState;
      voiceStatus.textContent = `Voice Conversation · ${state}${detail ? ` · ${detail}` : ""}`;
      appendLog(state === "Error" ? "ERROR" : "INFO", "VOICE CONVERSATION", `${state}${detail ? ` · ${detail}` : ""}`);
      if (state === "Cancelled" && chatGuard.reason(turn.sessionId)) {
        appendLog("INFO", "CHAT SESSION", "Chat may still be saved on the server. Its response is being observed; cancellation does not roll back session history.");
      }
      updateInputState();
    },
    cleanup: (turn) => {
      turn.controllers.forEach((controller) => controller.abort());
      turn.controllers.clear();
      if (recordingTurn === turn) discardRecording();
      if (turn.mediaStream) turn.mediaStream.getTracks().forEach((track) => track.stop());
      // This manager owns the only TTS slot while an automatic turn is active.
      stopTts("Voice turn resources released.", false);
      turn.mediaRecorder = null;
      turn.mediaStream = null;
      turn.audio = null;
      turn.objectUrl = null;
    },
    question: (turn, text) => {
      appendConversation(turn.sessionId, "user", text);
      appendLog("RUNNING", "POST /chat", `Voice turn · Language: ${turn.responseLanguage}.`);
    },
    chat: (turn, text) => sendChat(turn.sessionId, text, turn.responseLanguage, true),
    answer: (turn, text) => appendConversation(turn.sessionId, "assistant", text, turn.responseLanguage),
    speak: async (turn, message, onSpeaking) => {
      const button = conversationOutput.querySelector(`[data-message-id="${message.id}"]`);
      const status = button ? button.parentElement.querySelector(".tts-message-status") : null;
      const startedAt = performance.now();
      await startTts(button, status, message, turn, () => {
        onSpeaking();
        if (turn.recordingStoppedAt) appendLog("INFO", "VOICE LATENCY",
          `Record stop → playback: ${((performance.now() - turn.recordingStoppedAt) / 1000).toFixed(3)}s.`);
      });
      if (voiceTurns.valid(turn)) appendLog("INFO", "VOICE LATENCY",
        `Synthesis and playback completed in ${((performance.now() - startedAt) / 1000).toFixed(3)}s.`);
    },
    notice: (message) => appendLog("INFO", "VOICE CONVERSATION", message),
    error: (error) => appendLog("ERROR", "VOICE CONVERSATION", errorMessage(error)),
  });

  voiceModeButton.addEventListener("click", () => {
    if (requestRunning || (isVoiceBusy() && !voiceTurns.active)) return;
    voiceModeEnabled = !voiceModeEnabled;
    if (!voiceModeEnabled) voiceTurns.cancel("Voice Conversation switched OFF.");
    voiceModeButton.setAttribute("aria-pressed", String(voiceModeEnabled));
    voiceModeButton.textContent = `Voice Conversation ${voiceModeEnabled ? "ON" : "OFF"}`;
    if (!voiceTurns.active) {
      voiceState = "idle";
      voiceStatus.dataset.state = "idle";
      voiceStatus.textContent = voiceModeEnabled
        ? "Idle · Record starts one automatic STT → Chat → TTS turn."
        : "Idle · Manual STT places text in D; send Chat yourself.";
    }
    updateInputState();
  });
  cancelVoiceTurnButton.addEventListener("click", () => voiceTurns.cancel("Cancelled by the user."));

  guideButton.addEventListener("click", () => showView("guide"));
  serverButton.addEventListener("click", () => showView("servers"));
  refreshHealthButton.addEventListener("click", refreshHealth);
  toolButtons.forEach((button) => button.addEventListener("click", () => activateTool(button.dataset.tool)));
  serverToggle.addEventListener("click", toggleOllamaServer);
  recordVoiceButton.addEventListener("click", () => {
    if (voiceState === "recording") stopRecording(false);
    else startRecording();
  });
  conversationOutput.addEventListener("click", (event) => {
    if (voiceTurns.active) return;
    const button = event.target.closest(".tts-play-button");
    if (!button || button.disabled || !conversationOutput.contains(button)) return;
    if (button === activeTtsButton && ttsState !== "idle") {
      stopTts(ttsState === "loading"
        ? "The browser cancelled the TTS request. Server synthesis may still finish in the background."
        : "Playback was stopped by the user.");
      return;
    }
    const messages = conversations.get(currentConversationSession) || [];
    const message = messages.find((item) => String(item.id) === button.dataset.messageId && item.role === "assistant");
    if (!message) {
      appendLog("ERROR", "TTS PLAYBACK", "The selected AI response is no longer available.");
      return;
    }
    const status = button.parentElement.querySelector(".tts-message-status");
    startTts(button, status, message);
  });
  ttsLanguage.addEventListener("change", () => {
    stopTts("Response language changed. Previous playback was stopped.");
    appendLog("INFO", "CHAT / TTS", `Next AI response language: ${ttsLanguage.value}. Existing answers keep their original language; changing this setting does not translate them.`);
  });
  messageInput.addEventListener("input", updateInputState);
  sessionInput.addEventListener("input", () => {
    sessionRevision += 1;
    voiceTurns.cancel("Session changed.");
    if (["requesting", "recording", "transcribing"].includes(voiceState)) discardRecording();
    updateInputState();
    setConversationSession(sessionInput.value.trim());
  });
  newChatButton.addEventListener("click", createNewSession);
  deleteAllSessionsButton.addEventListener("click", deleteAllSessions);
  resetSessionButton.addEventListener("click", resetCurrentSession);
  sessionListButton.addEventListener("click", async () => {
    if (deletingAll || deleteAllUncertain) return;
    if (!sessionPopup.hidden) { closeSessionPopup(); return; }
    try {
      await refreshSessionList();
      sessionPopup.hidden = false;
      sessionListButton.setAttribute("aria-expanded", "true");
      positionSessionPopup();
    } catch (error) { appendLog("ERROR", "GET /sessions", errorMessage(error)); }
  });
  document.addEventListener("click", (event) => {
    if (!sessionPopup.hidden && !sessionPopup.contains(event.target)
        && !sessionListButton.contains(event.target)) closeSessionPopup();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !sessionPopup.hidden) closeSessionPopup();
  });
  window.addEventListener("resize", positionSessionPopup);
  window.addEventListener("scroll", (event) => {
    // Captured scroll events include non-bubbling scrolls from the list and
    // its native scrollbar. Only page/other-panel scrolling closes the popup.
    if (!sessionPopup.hidden && (event.target === window || !sessionPopup.contains(event.target))) closeSessionPopup();
  }, true);
  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!messageInput.value.trim() || requestRunning || isVoiceBusy()) return;
      runSelectedTool();
    }
  });
  clearOutputButton.addEventListener("click", () => { outputLog.replaceChildren(); });

  lockButton.addEventListener("click", () => {
    if (requestRunning) return;
    const nextLocked = lockButton.getAttribute("aria-pressed") !== "true";
    if (nextLocked) voiceTurns.cancel("Input locked.");
    if (nextLocked && ["requesting", "recording"].includes(voiceState)) stopRecording(true);
    if (nextLocked && voiceState === "transcribing") discardRecording();
    if (nextLocked) stopTts("Input was locked. TTS playback was stopped.");
    lockButton.setAttribute("aria-pressed", String(nextLocked));
    inputPanel.classList.toggle("is-locked", nextLocked);
    lockLabel.textContent = nextLocked ? "Unlock" : "Lock";
    lockIcon.textContent = nextLocked ? "▣" : "□";
    lockNote.textContent = nextLocked
      ? "입력 보호가 활성화되어 있습니다. Health와 Reset은 실행할 수 있습니다."
      : "입력 보호가 해제되었습니다. 잠금은 보안 인증 기능이 아닙니다.";
    updateInputState();
    if (!nextLocked) messageInput.focus();
  });

  appendLog("READY", "SYSTEM", "Local controller console initialized. Unlock D to send Chat; session controls are in E.");
  if (deleteAllUncertain) appendLog("ERROR", "SESSION STATE", "Previous Delete All completion is unknown. Session changes remain blocked; verified server resynchronization is required.");
  renderConversation(currentConversationSession);
  updateInputState();
  initializeSessions();
  window.addEventListener("beforeunload", () => {
    voiceTurns.cancel("Page closed.");
    stopTts("Page closed. TTS playback was stopped.", false);
    clearRecordingTimers();
    stopMediaTracks();
  });
});

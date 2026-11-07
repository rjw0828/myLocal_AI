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
  const messageInput = $("#message");
  const lockNote = $("#lock-note");
  const clearOutputButton = $("#clear-output");
  const outputLog = $("#output-log");
  const conversationSession = $("#conversation-session");
  const conversationOutput = $("#conversation-output");
  const conversationEmpty = $("#conversation-empty");
  const recordVoiceButton = $("#record-voice");
  const sttLanguage = $("#stt-language");
  const voiceStatus = $("#voice-status");
  const voiceTimer = $("#voice-timer");
  const VOICE_MAX_BYTES = 10 * 1024 * 1024;
  const VOICE_MAX_MS = 30_000;
  const VOICE_MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm"];
  let selectedTool = "health";
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
  let currentConversationSession = sessionInput.value.trim() || "default";

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

  const requestJson = async (path, options = {}) => {
    const controller = new AbortController();
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
    if (["requesting", "recording"].includes(voiceState)) stopRecording(true);
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

  const renderConversation = (sessionId) => {
    currentConversationSession = sessionId;
    conversationSession.textContent = `SESSION · ${sessionId}`;
    conversationOutput.replaceChildren();
    const messages = conversations.get(sessionId) || [];
    if (messages.length === 0) {
      conversationOutput.append(conversationEmpty);
      return;
    }
    messages.forEach(({ role, content }) => {
      const entry = document.createElement("article");
      entry.className = `conversation-message is-${role}`;
      const label = document.createElement("span");
      label.className = "conversation-role";
      label.textContent = role === "user" ? "YOU" : "AI";
      const text = document.createElement("p");
      text.className = "conversation-text";
      text.textContent = content;
      entry.append(label, text);
      conversationOutput.append(entry);
    });
    conversationOutput.scrollTop = conversationOutput.scrollHeight;
  };

  const setConversationSession = (sessionId) => {
    renderConversation(sessionId || "(empty)");
  };

  const appendConversation = (sessionId, role, content) => {
    const messages = conversations.get(sessionId) || [];
    messages.push({ role, content });
    conversations.set(sessionId, messages);
    if (currentConversationSession === sessionId) renderConversation(sessionId);
  };

  const secondsFromNanoseconds = (value) => `${(value / 1_000_000_000).toFixed(3)}s`;
  const formatChatMetrics = (data) => {
    const lines = [];
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

  const isVoiceBusy = () => ["requesting", "recording", "transcribing"].includes(voiceState);

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

  const uploadRecording = async (blob, sessionId, initialMessage, language) => {
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
    const result = requireSuccess(await requestJson("/voice/transcribe", {
      method: "POST",
      body: formData,
    }));
    if (!result || typeof result.text !== "string" || !result.text.trim()
        || typeof result.language !== "string" || typeof result.elapsed_seconds !== "number") {
      throw new ApiError("The STT response format is invalid.", 0, "invalid_stt_response");
    }
    const applied = applyTranscription(result.text, sessionId, initialMessage);
    appendLog(
      "SUCCESS",
      "POST /voice/transcribe",
      `Language: ${result.language}\nElapsed: ${result.elapsed_seconds.toFixed(3)}s\n${applied ? "Result placed in D Input for review." : "Result was not placed in D Input."}`,
    );
  };

  const finishRecording = async (operationId) => {
    clearRecordingTimers();
    stopMediaTracks();
    const chunks = recordingChunks;
    recordingChunks = [];
    const recorder = mediaRecorder;
    mediaRecorder = null;
    if (recordingFailure) {
      const failure = recordingFailure;
      recordingFailure = null;
      setVoiceState("error", failure);
      appendLog("ERROR", "MICROPHONE", failure);
      return;
    }
    if (operationId !== voiceOperationId || recordingCancelled) {
      setVoiceState("idle", "Idle · Recording cancelled; no audio was uploaded.");
      return;
    }
    const blob = new Blob(chunks, { type: recorder && recorder.mimeType ? recorder.mimeType : "" });
    setVoiceState("transcribing", "Transcribing · Local STT is processing the recording.");
    try {
      await uploadRecording(blob, recordingSessionId, recordingInitialMessage, recordingLanguage);
      setVoiceState("idle", "Idle · Review or edit the recognized text before sending Chat.");
    } catch (error) {
      setVoiceState("error", `Error · ${errorMessage(error)}`);
      appendLog("ERROR", "POST /voice/transcribe", errorMessage(error));
    } finally {
      recordingChunks = [];
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
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    if (locked || requestRunning || isVoiceBusy()) return;
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function"
        || typeof window.MediaRecorder !== "function") {
      const message = "Microphone recording is not supported by this browser or context.";
      setVoiceState("error", `Error · ${message}`);
      appendLog("ERROR", "MICROPHONE", message);
      return;
    }
    const operationId = ++voiceOperationId;
    recordingCancelled = false;
    recordingFailure = null;
    recordingSessionId = sessionInput.value.trim();
    recordingInitialMessage = messageInput.value;
    recordingLanguage = sttLanguage.value;
    voiceTimer.textContent = "00:00";
    setVoiceState("requesting", "Requesting Permission · Allow microphone access in the browser.");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (operationId !== voiceOperationId || voiceState !== "requesting") {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      mediaStream = stream;
      const mimeType = selectedRecordingMimeType();
      mediaRecorder = mimeType
        ? new window.MediaRecorder(stream, { mimeType })
        : new window.MediaRecorder(stream);
      recordingChunks = [];
      mediaRecorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size) recordingChunks.push(event.data);
      });
      mediaRecorder.addEventListener("error", () => {
        recordingFailure = "The recording device reported an error.";
        stopRecording(true);
      });
      mediaRecorder.addEventListener("stop", () => { finishRecording(operationId); }, { once: true });
      stream.getTracks().forEach((track) => track.addEventListener("ended", () => {
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
      stopMediaTracks();
      if (operationId !== voiceOperationId) return;
      const message = microphoneErrorMessage(error);
      setVoiceState("error", `Error · ${message}`);
      appendLog("ERROR", "MICROPHONE", message);
    }
  };

  const updateInputState = () => {
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    const voiceBusy = isVoiceBusy();
    sessionInput.disabled = locked || selectedTool === "health" || requestRunning;
    messageInput.disabled = locked || selectedTool !== "chat" || requestRunning || voiceBusy;
    sttLanguage.disabled = locked || requestRunning || voiceBusy;
    recordVoiceButton.disabled = locked || requestRunning
      || (["requesting", "transcribing"].includes(voiceState));
    toolButtons.forEach((button) => { button.disabled = requestRunning || voiceBusy; });
  };

  const selectTool = (tool) => {
    if (requestRunning) return;
    selectedTool = tool;
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

  const runSelectedTool = async () => {
    if (requestRunning) return;
    if (isVoiceBusy()) {
      appendLog("ERROR", "SYSTEM", "Wait for microphone recording or transcription to finish.");
      return;
    }
    const sessionId = sessionInput.value.trim();
    const message = messageInput.value.trim();
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    if (selectedTool === "chat" && locked) {
      appendLog("ERROR", "POST /chat", "Chat을 실행하려면 Input을 Unlock해 주세요.");
      return;
    }
    if ((selectedTool === "chat" || selectedTool === "reset") && !sessionId) {
      appendLog("ERROR", selectedTool === "chat" ? "POST /chat" : "POST /sessions/{id}/reset", "Session ID를 입력해 주세요.");
      return;
    }
    if (selectedTool === "chat" && !message) {
      appendLog("ERROR", "POST /chat", "빈 메시지는 전송할 수 없습니다.");
      return;
    }
    if (selectedTool === "reset" && !window.confirm(`세션 '${sessionId}'의 대화 기록을 초기화할까요?`)) {
      appendLog("INFO", "POST /sessions/{id}/reset", "사용자가 세션 초기화를 취소했습니다.");
      return;
    }

    const apiLabel = selectedTool === "health" ? "GET /health" : selectedTool === "chat" ? "POST /chat" : `POST /sessions/${sessionId}/reset`;
    if (selectedTool === "chat") {
      setConversationSession(sessionId);
      appendConversation(sessionId, "user", message);
    }
    appendLog("RUNNING", apiLabel, "요청을 전송했습니다.");
    setRequestRunning(true);
    try {
      let result;
      if (selectedTool === "health") {
        result = requireSuccess(await requestJson("/health", { method: "GET" }));
        appendLog("SUCCESS", apiLabel, `Controller: ${result.controller}\nOllama: ${result.ollama_reachable ? "Online" : "Offline"}\nModel: ${result.model}`);
      } else if (selectedTool === "chat") {
        result = requireSuccess(await requestJson("/chat", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, message }),
        }));
        if (typeof result.message !== "string" || !result.message.trim()) {
          throw new ApiError("AI response is empty or invalid.", 502, "invalid_ollama_response");
        }
        appendConversation(sessionId, "assistant", result.message);
        appendLog("SUCCESS", apiLabel, formatChatMetrics(result));
      } else {
        result = requireSuccess(await requestJson(`/sessions/${encodeURIComponent(sessionId)}/reset`, { method: "POST" }));
        conversations.delete(sessionId);
        if (currentConversationSession === sessionId) renderConversation(sessionId);
        appendLog("SUCCESS", apiLabel, `Session '${result.session_id}' reset complete.`);
      }
    } catch (error) {
      appendLog("ERROR", apiLabel, errorMessage(error));
    } finally {
      setRequestRunning(false);
    }
  };

  const activateTool = async (tool) => {
    selectTool(tool);
    if (tool === "chat") {
      setConversationSession(sessionInput.value.trim() || "(empty)");
      if (lockButton.getAttribute("aria-pressed") !== "true") messageInput.focus();
      return;
    }
    await runSelectedTool();
  };

  const toggleOllamaServer = async () => {
    if (serverToggle.disabled || requestRunning) return;
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

  guideButton.addEventListener("click", () => showView("guide"));
  serverButton.addEventListener("click", () => showView("servers"));
  refreshHealthButton.addEventListener("click", refreshHealth);
  toolButtons.forEach((button) => button.addEventListener("click", () => activateTool(button.dataset.tool)));
  serverToggle.addEventListener("click", toggleOllamaServer);
  recordVoiceButton.addEventListener("click", () => {
    if (voiceState === "recording") stopRecording(false);
    else startRecording();
  });
  messageInput.addEventListener("input", updateInputState);
  sessionInput.addEventListener("input", () => {
    updateInputState();
    setConversationSession(sessionInput.value.trim() || "(empty)");
  });
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
    if (nextLocked && ["requesting", "recording"].includes(voiceState)) stopRecording(true);
    lockButton.setAttribute("aria-pressed", String(nextLocked));
    inputPanel.classList.toggle("is-locked", nextLocked);
    lockLabel.textContent = nextLocked ? "Unlock" : "Lock";
    lockIcon.textContent = nextLocked ? "▣" : "□";
    lockNote.textContent = nextLocked
      ? "입력 보호가 활성화되어 있습니다. Health와 Reset은 실행할 수 있습니다."
      : "입력 보호가 해제되었습니다. 잠금은 보안 인증 기능이 아닙니다.";
    updateInputState();
    if (!nextLocked && selectedTool !== "health") sessionInput.focus();
  });

  appendLog("READY", "SYSTEM", "Local controller console initialized. Health Check is selected.");
  renderConversation(currentConversationSession);
  updateInputState();
  window.addEventListener("beforeunload", () => {
    clearRecordingTimers();
    stopMediaTracks();
  });
});

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
  let selectedTool = "health";
  let requestRunning = false;

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

  const secondsFromNanoseconds = (value) => `${(value / 1_000_000_000).toFixed(3)}s`;
  const formatChatResult = (data) => {
    const lines = [];
    if (typeof data.message === "string") lines.push(`AI: ${data.message}`);
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

  const updateInputState = () => {
    const locked = lockButton.getAttribute("aria-pressed") === "true";
    sessionInput.disabled = locked || selectedTool === "health" || requestRunning;
    messageInput.disabled = locked || selectedTool !== "chat" || requestRunning;
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
        appendLog("SUCCESS", apiLabel, formatChatResult(result));
      } else {
        result = requireSuccess(await requestJson(`/sessions/${encodeURIComponent(sessionId)}/reset`, { method: "POST" }));
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
    setStatus(serverStatus, online ? "Stopping…" : "Starting…", "pending");
    try {
      requireSuccess(await requestJson(`/ollama/${online ? "stop" : "start"}`, {
        method: "POST",
        headers: { "X-Local-AI-Control": "web-console" },
      }));
    } catch (error) {
      healthMessage.textContent = errorMessage(error);
    } finally {
      await refreshHealth();
    }
  };

  guideButton.addEventListener("click", () => showView("guide"));
  serverButton.addEventListener("click", () => showView("servers"));
  refreshHealthButton.addEventListener("click", refreshHealth);
  toolButtons.forEach((button) => button.addEventListener("click", () => activateTool(button.dataset.tool)));
  serverToggle.addEventListener("click", toggleOllamaServer);
  messageInput.addEventListener("input", updateInputState);
  sessionInput.addEventListener("input", updateInputState);
  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!messageInput.value.trim() || requestRunning) return;
      runSelectedTool();
    }
  });
  clearOutputButton.addEventListener("click", () => { outputLog.replaceChildren(); });

  lockButton.addEventListener("click", () => {
    if (requestRunning) return;
    const nextLocked = lockButton.getAttribute("aria-pressed") !== "true";
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
  updateInputState();
});

"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const guideButton = document.querySelector("#guide-button");
  const serverButton = document.querySelector("#server-button");
  const guideView = document.querySelector("#guide-view");
  const serverView = document.querySelector("#server-view");
  const mainContent = document.querySelector("#main-content");
  const lockButton = document.querySelector("#console-lock");
  const lockLabel = document.querySelector("#lock-label");
  const lockIcon = document.querySelector("#lock-icon");
  const inputPanel = document.querySelector("#input-panel");
  const sessionInput = document.querySelector("#session-id");
  const messageInput = document.querySelector("#message");
  const lockNote = document.querySelector("#lock-note");
  const refreshHealthButton = document.querySelector("#refresh-health");
  const controllerStatus = document.querySelector("#controller-status");
  const ollamaStatus = document.querySelector("#ollama-status");
  const configuredModel = document.querySelector("#configured-model");
  const serverModel = document.querySelector("#server-model");
  const serverStatus = document.querySelector("#server-status");
  const healthMessage = document.querySelector("#health-message");

  const setStatus = (element, text, state) => {
    element.textContent = text;
    element.classList.remove("is-pending", "is-online", "is-offline");
    element.classList.add(`is-${state}`);
  };

  const refreshHealth = async () => {
    refreshHealthButton.disabled = true;
    refreshHealthButton.textContent = "Checking…";
    setStatus(controllerStatus, "확인 중", "pending");
    setStatus(ollamaStatus, "확인 중", "pending");
    setStatus(serverStatus, "확인 중", "pending");
    healthMessage.textContent = "Flask Controller와 Ollama 상태를 확인하고 있습니다.";

    try {
      const response = await fetch("/health", {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const data = await response.json();
      if (!data || data.controller !== "ok" || typeof data.ollama_reachable !== "boolean" || typeof data.model !== "string") {
        throw new Error("Invalid health response");
      }

      setStatus(controllerStatus, "Online", "online");
      setStatus(ollamaStatus, data.ollama_reachable ? "Online" : "Offline", data.ollama_reachable ? "online" : "offline");
      setStatus(serverStatus, data.ollama_reachable ? "Online" : "Offline", data.ollama_reachable ? "online" : "offline");
      configuredModel.textContent = data.model;
      serverModel.textContent = data.model;
      healthMessage.textContent = data.ollama_reachable
        ? "Flask Controller와 Ollama 서버가 정상적으로 응답했습니다."
        : "Flask Controller는 Online이지만 Ollama 서버는 Offline입니다.";
    } catch (error) {
      setStatus(controllerStatus, "Offline", "offline");
      setStatus(ollamaStatus, "확인 불가", "pending");
      setStatus(serverStatus, "확인 불가", "pending");
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

  guideButton.addEventListener("click", () => showView("guide"));
  serverButton.addEventListener("click", () => showView("servers"));
  refreshHealthButton.addEventListener("click", refreshHealth);

  lockButton.addEventListener("click", () => {
    const nextLocked = lockButton.getAttribute("aria-pressed") !== "true";
    lockButton.setAttribute("aria-pressed", String(nextLocked));
    inputPanel.classList.toggle("is-locked", nextLocked);
    sessionInput.disabled = nextLocked;
    messageInput.disabled = nextLocked;
    lockLabel.textContent = nextLocked ? "Unlock" : "Lock";
    lockIcon.textContent = nextLocked ? "▣" : "□";
    lockNote.textContent = nextLocked ? "입력 보호가 활성화되어 있습니다." : "입력 보호가 해제되었습니다. API 전송은 아직 비활성입니다.";
    if (!nextLocked) sessionInput.focus();
  });
});

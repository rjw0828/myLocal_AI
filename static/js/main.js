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

  const showView = (view) => {
    const showServers = view === "servers";
    guideView.hidden = showServers;
    serverView.hidden = !showServers;
    serverButton.classList.toggle("is-active", showServers);
    serverButton.setAttribute("aria-current", showServers ? "page" : "false");
    mainContent.scrollTop = 0;
    mainContent.focus({ preventScroll: true });
  };

  guideButton.addEventListener("click", () => showView("guide"));
  serverButton.addEventListener("click", () => showView("servers"));

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

"use strict";
(function (root) {
  class ModelSelector {
    constructor({ select, refresh, message, request, onSelected, onChanging = () => {}, scope = 'chat_voice' }) {
      Object.assign(this, { select, refreshButton: refresh, message, request, onSelected, onChanging });
      this.scope = scope;
      this.label = '일반 Chat·Voice·Tool Chat';
      this.fixed = false;
      this.models = []; this.selected = ""; this.version = 0;
      this.loading = false; this.saving = false; this.blocked = false; this.remoteBusy = false; this.disposed = false;
      select.addEventListener("change", () => this.change());
      refresh.addEventListener("click", () => this.refresh());
      this.update();
    }
    update() {
      this.select.disabled = this.fixed || this.loading || this.saving || this.blocked || this.remoteBusy || !this.models.length;
      this.refreshButton.disabled = this.loading || this.saving;
    }
    setBlocked(value) { this.blocked = Boolean(value); this.update(); }
    async refresh() {
      if (this.disposed || this.saving || this.loading) return;
      const version = ++this.version;
      this.loading = true; this.update();
      try {
        const data = await this.request(this.scope === 'agent' ? '/models?scope=agent' : '/models', { method: "GET" });
        if (this.disposed || version !== this.version) return;
        if (!Array.isArray(data.models) || !data.models.every(m => typeof m === "string" && m.length)
            || typeof data.selected !== "string" || typeof data.busy !== "boolean") throw new Error("모델 목록 응답이 올바르지 않습니다.");
        this.models = data.models; this.selected = data.selected; this.remoteBusy = data.busy;
        this.fixed = data.configurable === false;
        this.select.replaceChildren();
        for (const name of [...new Set([data.selected, ...data.models])]) {
          const option = document.createElement("option");
          option.value = name; option.textContent = name + (data.models.includes(name) ? "" : " (설치되지 않음)");
          option.disabled = !data.models.includes(name); this.select.append(option);
        }
        this.select.value = data.selected; this.onSelected(data.selected);
        this.message.textContent = data.models.includes(data.selected)
          ? `${this.label}에 적용 · 설치 모델 ${data.models.length}개 · 새 모델은 30초마다 확인`
          : "선택 모델이 설치 목록에 없습니다. 설치된 다른 모델을 선택해주세요.";
        if (this.fixed) this.message.textContent = '이 별도 호스트의 Agent 모델은 실행 시 지정됩니다.';
      } catch (error) {
        if (!this.disposed && version === this.version) this.message.textContent = error.message || "설치 모델 목록을 읽지 못했습니다.";
      } finally {
        if (version === this.version) { this.loading = false; this.update(); }
      }
    }
    async change() {
      if (this.disposed || this.fixed || this.saving || this.loading || this.blocked || this.remoteBusy) {
        this.select.value = this.selected; return;
      }
      const model = this.select.value;
      if (!this.models.includes(model)) { this.select.value = this.selected; return; }
      const version = ++this.version;
      this.saving = true; this.update(); this.onChanging();
      let reconcile = false;
      try {
        const data = await this.request(this.scope === 'agent' ? '/models/selection?scope=agent' : '/models/selection', { method: "PUT", body: JSON.stringify({ model }),
          headers: { "Content-Type": "application/json" } });
        if (this.disposed || version !== this.version) return;
        if (data.selected !== model) throw new Error("선택 저장을 확인하지 못했습니다. 목록을 새로고침해주세요.");
        this.selected = data.selected; this.onSelected(data.selected);
        this.message.textContent = `저장했습니다. 다음 ${this.label} 요청부터 적용됩니다. 진행 중인 요청과 History는 유지됩니다.`;
      } catch (error) {
        if (!this.disposed && version === this.version) this.message.textContent = error.message || "모델 선택을 저장하지 못했습니다.";
        reconcile = true; // Lost POST responses require a read, never an automatic retry.
      } finally {
        if (version === this.version) {
          this.select.value = this.selected; this.saving = false; this.update(); this.onChanging();
        }
        if (reconcile && !this.disposed) await this.refresh();
      }
    }
    dispose() { this.disposed = true; this.version++; this.select.disabled = true; this.refreshButton.disabled = true; }
  }
  const api = { ModelSelector };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.LocalAIModelSelector = api;
})(globalThis);

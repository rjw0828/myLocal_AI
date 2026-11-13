"use strict";

// DOM adapter only. Runtime, approval and reservation authority stay on the server.
((root) => {
  const { ToolChatError } = typeof module !== "undefined" && module.exports ? require("./tool-chat.js") : root.LocalAIToolChat;
  class ToolChatConsole {
    constructor({ nodes, tracker, context, canStart, question, answer, syncHistory, log, update }) {
      Object.assign(this, { nodes, tracker, context, canStart, question, answer, syncHistory, log, update });
      this.enabled = false; this.startingSession = null; this.intent = 0; this.dialogTicket = null; this.languages = new Map(); this.lastStates = new Map();
      this.lastErrors = new Map(); this.localReports = new WeakSet();
      const n = nodes;
      n.mode.addEventListener("click", () => {
        if (!this.enabled && !canStart(true)) return;
        this.enabled = !this.enabled;
        n.mode.setAttribute("aria-pressed", String(this.enabled));
        n.mode.textContent = `Tool Chat ${this.enabled ? "ON" : "OFF"}`;
        if (!this.enabled) this.cancel();
        this.render();
      });
      n.cancel.addEventListener("click", () => this.cancel());
      n.check.addEventListener("click", () => this.check());
      n.reconcile.addEventListener("click", () => this.reconcile());
      n.allow.addEventListener("click", () => this.decide(true));
      n.deny.addEventListener("click", () => this.decide(false));
      n.close.addEventListener("click", () => this.closeDialog());
      n.dialog.addEventListener("cancel", (event) => { event.preventDefault(); this.closeDialog(); });
      for (const name of ["created", "state", "approval", "completed", "failed", "uncertain", "cancelled", "reservation_wait", "communication_error", "request_error"]) {
        tracker.on(name, (event) => this.receive(name, event));
      }
    }
    current() { const c = this.context(); return c?.sessionId ? this.tracker.get(c.sessionId) : null; }
    blocked(id) { return Boolean(id && (this.startingSession === id || this.tracker.isBlocked(id))); }
    anyBlocked() { return Boolean(this.startingSession) || [...this.tracker.records.keys()].some((id) => this.blocked(id)); }
    wasReported(error) { return this.tracker.wasReported(error) || Boolean(error && typeof error === "object" && this.localReports.has(error)); }
    reportError(error) {
      if (this.wasReported(error)) return;
      if (error && typeof error === "object") this.localReports.add(error);
      this.log("ERROR", this.errorText(error));
    }
    async restoreKnownJobs() {
      // Read-only recovery. Never replay a POST or present a stored approval.
      const ids = [...this.tracker.records.keys()].filter((id) => {
        const r = this.tracker.get(id); return r.restored && r.jobId;
      });
      await Promise.allSettled(ids.map(async (id) => {
        try {
          const r = await this.tracker.check(id);
          if (!r.blocked) this.syncHistory(id);
        } catch (error) { this.reportError(error); }
      }));
      this.render();
    }
    async prepareMutation(ids = null) {
      this.intent += 1; this.closeDialog();
      const targets = [...new Set(ids || [...this.tracker.records.keys(), this.startingSession].filter(Boolean))];
      const results = await Promise.allSettled(targets.map(async (id) => {
        let r = this.tracker.get(id);
        if (this.startingSession === id && !r) throw new ToolChatError("session_unsettled");
        if (!this.blocked(id)) return;
        if (!r?.jobId) throw new ToolChatError("creation_uncertain");
        await this.tracker.cancel(id); // A failed Cancel aborts this attempt.
        r = this.tracker.get(id);
        if (r.approving || r.reconciling || r.creating) throw new ToolChatError("request_in_progress");
        // An older GET can predate Cancel. Drain it before the fresh observation.
        if (r.polling) await this.tracker.check(id);
        r = await this.tracker.check(id, { verifyRelease: true });
        if (r.blocked || r.reservation_owned !== false || r.unsettled !== false) {
          throw new ToolChatError("session_unsettled");
        }
      }));
      this.render();
      const failed = results.find((result) => result.status === "rejected");
      if (failed) throw failed.reason;
    }
    matches(event) {
      const c = this.context();
      return c && event.sessionId === c.sessionId && event.viewGeneration === c.generation
        && event.resetVersion === c.resetVersion && event.visible;
    }
    async syncContext() {
      this.intent += 1;
      this.closeDialog();
      // setContext invalidates synchronously before waiting for server cancellation.
      const pending = this.tracker.setContext(this.context());
      this.render();
      await pending; this.render();
    }
    describe(r) {
      if (!r) return "Idle";
      if (!r.jobId && r.blocked) return r.creating ? "Creating Job…" : "Status unknown · Job ID unavailable. Session blocked; server verification required. Do not resend.";
      const note = r.blocked ? " · Session reserved" : "";
      const names = { RUNNING: "Running", WAITING_APPROVAL: "Waiting for approval", COMPLETED: "Completed",
        FAILED: "Failed", CANCELLED: "Cancelled", UNCERTAIN: "UNCERTAIN · Server completion must be verified" };
      return `${names[r.state] || r.localState || "Unknown"}${r.restored && r.blocked ? " · Restored job; approval not restored. Cancel or check status" : ""}${r.cancelIntent ? " · Cancel requested" : ""}${note}${r.errorCode ? ` · ${r.errorCode}` : ""}${r.verificationRequired ? " · Check status required" : ""}`;
    }
    render() {
      const r = this.current(), n = this.nodes;
      n.status.textContent = this.startingSession && !r ? "Creating Job…" : this.describe(r);
      n.status.title = n.status.textContent;
      n.output.textContent = r ? `Tool Chat · ${this.describe(r)}` : "";
      const last = r && this.lastErrors.get(r.sessionId);
      if (last && last.jobId === r.jobId) n.output.textContent += ` · Last request error: ${this.errorText(last.error)}`;
      n.cancel.disabled = !this.startingSession && (!r || !r.blocked || r.cancelling);
      n.cancel.textContent = r?.cancelIntent ? "Retry Cancel" : "Cancel";
      n.check.disabled = !r || !r.jobId || r.polling || r.approving || r.cancelling || r.reconciling;
      n.check.textContent = r?.approval ? "Review approval" : "Check status";
      n.reconcile.hidden = !(r && ["UNCERTAIN", "CANCELLED", "FAILED"].includes(r.state) && r.unsettled && r.operationId);
      n.reconcile.disabled = !r || r.polling || r.approving || r.cancelling || r.reconciling;
      if (this.dialogTicket && (!r || !this.matches(r) || !r.approval
          || r.approvalVersion !== this.dialogTicket.version || r.jobId !== this.dialogTicket.jobId)) this.closeDialog();
      const busy = !r || r.approving || r.cancelling || r.cancelIntent;
      n.allow.disabled = busy; n.deny.disabled = busy;
      this.update();
    }
    receive(name, event) {
      if (name === "state") {
        if (event.creating) this.lastErrors.delete(event.sessionId);
        const key = `${event.state}:${event.reservation_owned}:${event.unsettled}:${event.localState}`;
        if (this.lastStates.get(event.jobId || event.sessionId) !== key) {
          this.lastStates.set(event.jobId || event.sessionId, key);
          this.log("INFO", `State: ${event.state || event.localState} · reservation=${event.reservation_owned} · unsettled=${event.unsettled}`);
        }
      } else if (name === "communication_error" || name === "request_error") {
        this.lastErrors.set(event.sessionId, { jobId: event.jobId, error: event.error });
        this.log("ERROR", this.errorText(event.error));
      } else if (name === "completed") {
        this.log("SUCCESS", "Completed · final answer received.");
        if (this.matches(event)) {
          this.answer(event.sessionId, event.result, this.languages.get(event.sessionId));
          this.tracker.acknowledgeResult(event.sessionId, event.jobId);
          this.syncHistory(event.sessionId);
        }
      } else if (name !== "approval") {
        this.log(name === "failed" || name === "uncertain" ? "ERROR" : "INFO", `${name}${event.code ? ` · ${event.code}` : ""}`);
      }
      this.render();
      if (name === "approval" && this.matches(event)) {
        // A replacement must be deliberately reopened: a pointer click aimed at
        // the previous dialog must never approve a newly substituted proposal.
        if (this.presentedJob === event.jobId && this.presentedVersion !== event.approvalVersion) {
          this.closeDialog(); this.nodes.allow.disabled = true; this.nodes.deny.disabled = true;
          this.nodes.output.textContent = "Approval changed. Use Review approval to inspect the new request.";
        } else this.openApproval(event);
      }
    }
    errorText(error = {}) {
      const hints = {
        session_reserved: "Another request owns this session. Wait and check status.",
        session_unsettled: "Server completion is unresolved. This session remains blocked.",
        stale_approval: "Approval changed. Check status and review the new proposal.",
        operation_unresolved: "MCP completion is not confirmed. No replay or automatic recovery.",
        job_timeout: "Job timed out. Check whether its reservation is still owned.",
        network_failure: "Network failure. Do not resend the Job; check status.",
        request_timeout: "Request timed out. Server work may still continue.",
        creation_uncertain: "Job ID unavailable. Server verification required; no recovery lookup is supported.",
        filesystem_invalid_path: "An absolute permitted path is required. No tool was executed by this rejected approval. Review, Deny or Cancel; the session stays reserved while approval waits.",
        filesystem_outside_roots: "The requested path is outside the approved root. Do not widen approval. Deny or Cancel the job.",
      };
      return `${error.status ? `HTTP ${error.status} · ` : ""}${error.code || "unknown_error"} · ${hints[error.code] || "Request did not complete normally. Check status before proceeding."}`;
    }
    async start(message, language) {
      const c = this.context();
      if (!this.enabled || !c || !this.canStart(false) || this.blocked(c.sessionId)) return;
      const intent = ++this.intent;
      this.startingSession = c.sessionId; this.render();
      try {
        await this.tracker.setContext(c);
        // Context can change during the await; never start against its replacement.
        const now = this.context();
        if (intent !== this.intent || !this.enabled || !this.canStart(false) || !now
            || now.sessionId !== c.sessionId || now.generation !== c.generation || now.resetVersion !== c.resetVersion) return;
        this.languages.set(c.sessionId, language);
        this.question(c.sessionId, message);
        await this.tracker.start({ message, responseLanguage: language });
      }
      catch (error) { this.reportError(error); }
      finally {
        this.startingSession = null; this.render();
        const r = this.current();
        if (r?.state === "COMPLETED" && !r.blocked && this.matches(r)) this.syncHistory(r.sessionId);
      }
    }
    async cancel() {
      this.intent += 1;
      const r = this.current(); if (!r || !r.blocked) return;
      this.closeDialog(); this.log("INFO", "Cancel requested; cancellation is not rollback.");
      try { const pending = this.tracker.cancel(r.sessionId); this.render(); await pending; }
      catch (error) { this.reportError(error); }
      finally { this.render(); }
    }
    async check() {
      const r = this.current(); if (!r) return;
      if (r.approval) { this.openApproval(r); return; }
      try {
        const pending = this.tracker.check(r.sessionId); this.render(); await pending;
        const updated = this.current();
        if (updated && !updated.blocked && updated.sessionId === r.sessionId) this.syncHistory(r.sessionId);
      } catch (error) { this.reportError(error); }
      finally { this.render(); }
    }
    async reconcile() {
      const r = this.current(); if (!r) return;
      try { const pending = this.tracker.reconcile(r.sessionId); this.render(); await pending; }
      catch (error) { this.reportError(error); }
      finally { this.render(); }
    }
    openApproval(event) {
      const r = this.current();
      if (!r || !r.approval || !this.matches(r) || r.approving || r.cancelIntent) return;
      const p = r.approval, n = this.nodes;
      this.presentedJob = r.jobId; this.presentedVersion = r.approvalVersion;
      this.dialogTicket = { jobId: r.jobId, sessionId: r.sessionId, proposalId: p.proposal_id, version: r.approvalVersion };
      n.server.textContent = p.server_name || p.server_id; n.tool.textContent = p.tool_name;
      n.description.textContent = p.description || ""; n.arguments.textContent = JSON.stringify(p.arguments, null, 2);
      n.path.textContent = typeof p.arguments.path === "string" ? p.arguments.path : "See requested arguments above.";
      n.scope.textContent = p.scope ? JSON.stringify(p.scope, null, 2) : "No filesystem scope.";
      n.notice.textContent = p.scope
        ? `ROOT-WIDE approval: ${p.scope.path} · includes subdirectories: ${p.scope.recursive ? "YES" : "NO"}. Even a single-file request grants this entire displayed root for this AI response only.`
        : "Approval applies only to this AI response, never to the next question.";
      n.allow.disabled = false; n.deny.disabled = false;
      if (!n.dialog.open) n.dialog.showModal();
      this.log("INFO", "Approval required · Review server, tool, arguments and scope in the dialog.");
    }
    closeDialog() {
      this.dialogTicket = null;
      if (this.nodes.dialog.open) this.nodes.dialog.close();
    }
    async decide(allow) {
      const ticket = this.dialogTicket, r = this.current();
      if (!ticket || !r || !r.approval || ticket.jobId !== r.jobId || ticket.version !== r.approvalVersion || r.approving) return;
      this.nodes.allow.disabled = true; this.nodes.deny.disabled = true;
      this.log("INFO", allow ? "Approval decision: Allow for this response only." : "Approval decision: Deny.");
      try {
        const pending = this.tracker.decide(ticket.sessionId, { proposalId: ticket.proposalId,
          approvalVersion: ticket.version, allow, ...(allow && r.approval.scope ? { scope: r.approval.scope } : {}) });
        this.closeDialog(); this.render(); await pending;
      } catch (error) { this.closeDialog(); this.reportError(error); }
      finally { this.render(); }
    }
    dispose() { this.cancel(); this.closeDialog(); this.tracker.dispose(); }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { ToolChatConsole };
  else root.LocalAIToolChatConsole = ToolChatConsole;
})(typeof globalThis !== "undefined" ? globalThis : this);

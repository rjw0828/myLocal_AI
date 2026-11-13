"use strict";

// MCP-5D-2: no DOM, startup requests, Chat/Voice integration or model execution.
((root) => {
  const STATES = new Set(["RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED", "UNCERTAIN"]);
  const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "UNCERTAIN"]);
  const STORAGE_KEY = "local-ai-tool-chat-jobs-v1";
  const copy = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const identifier = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
  const text = (value, max) => typeof value === "string" && Boolean(value.trim()) && Array.from(value).length <= max;
  const stable = (value) => JSON.stringify(value, function (_key, item) {
    if (!object(item)) return item;
    const sorted = Object.create(null);
    Object.keys(item).sort().forEach((key) => { sorted[key] = item[key]; });
    return sorted;
  });

  class ToolChatError extends Error {
    constructor(code, message = code, status = 0, uncertain = false) {
      super(message); this.name = "ToolChatError";
      this.code = code; this.status = status; this.uncertain = uncertain;
    }
  }
  const invalid = () => { throw new ToolChatError("invalid_job_response", "Invalid Tool Chat response.", 0, true); };
  function finite(value, depth = 0) {
    if (depth > 64 || typeof value === "number" && !Number.isFinite(value)) invalid();
    if (value && typeof value === "object") Object.values(value).forEach((item) => finite(item, depth + 1));
  }
  function scopeValid(scope) {
    return object(scope) && text(scope.path, 32768) && ["file", "directory"].includes(scope.kind)
      && typeof scope.recursive === "boolean" && Object.keys(scope).sort().join(",") === "kind,path,recursive";
  }
  function validateApproval(approval) {
    if (!object(approval) || !identifier(approval.proposal_id) || !identifier(approval.server_id)
        || !text(approval.tool_name, 256) || !object(approval.arguments) || approval.turn_scoped !== true
        || approval.scope !== null && !scopeValid(approval.scope)) invalid();
    finite(approval);
    return approval;
  }
  function validateJob(job, expectedId = null) {
    if (!object(job) || !identifier(job.job_id) || expectedId && job.job_id !== expectedId
        || !text(job.session_id, 128) || !integer(job.revision) || !integer(job.browser_generation)
        || !STATES.has(job.state) || typeof job.reservation_owned !== "boolean"
        || typeof job.unsettled !== "boolean" || typeof job.cancelled !== "boolean"
        || job.operation_id !== null && !identifier(job.operation_id)) invalid();
    if (job.state === "WAITING_APPROVAL") validateApproval(job.pending);
    else if (job.pending !== null) invalid();
    if (job.state === "COMPLETED") {
      const result = job.result;
      if (job.reservation_owned || job.unsettled || !object(result) || result.session_id !== job.session_id
          || !text(result.message, 1024 * 1024) || !text(result.model, 256) || !integer(result.revision)
          || !object(result.metrics) || typeof result.elapsed_seconds !== "number" || !Number.isFinite(result.elapsed_seconds)
          || result.elapsed_seconds < 0) invalid();
    } else if (job.result !== null) invalid();
    finite(job);
    return copy(job);
  }

  class ToolChatClient {
    constructor({ fetch, timeoutMs = 10000, setTimer = setTimeout, clearTimer = clearTimeout,
      controllerFactory = () => new AbortController() } = {}) {
      if (typeof fetch !== "function" || !Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new TypeError("Fetch and a positive timeout are required.");
      Object.assign(this, { fetch, timeoutMs, setTimer, clearTimer, controllerFactory });
    }
    async _request(path, method, data, { signal } = {}) {
      const controller = this.controllerFactory();
      const abort = () => controller.abort();
      if (signal) { if (signal.aborted) abort(); else signal.addEventListener("abort", abort, { once: true }); }
      let timedOut = false;
      const timer = this.setTimer(() => { timedOut = true; controller.abort(); }, this.timeoutMs);
      try {
        let response, body;
        try {
          response = await this.fetch("/tool-chat" + path, {
            method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
            headers: { Accept: "application/json", "X-Local-AI-Control": "web-console", ...(data === undefined ? {} : { "Content-Type": "application/json" }) },
            ...(data === undefined ? {} : { body: JSON.stringify(data) }),
          });
        } catch (error) {
          throw new ToolChatError(timedOut ? "request_timeout" : error?.name === "AbortError" ? "request_aborted" : "network_failure",
            "Tool Chat connection did not complete.", 0, true);
        }
        try { body = await response.json(); }
        catch (_) { throw new ToolChatError("invalid_json_response", "Tool Chat returned invalid JSON.", response.status, true); }
        if (!response.ok) {
          const error = object(body) && object(body.error) ? body.error : {};
          throw new ToolChatError(typeof error.code === "string" ? error.code : "http_error",
            typeof error.message === "string" ? error.message : "Tool Chat HTTP request failed.", response.status, response.status >= 500);
        }
        if (!object(body)) invalid();
        return body;
      } finally {
        this.clearTimer(timer);
        if (signal) signal.removeEventListener("abort", abort);
      }
    }
    _id(jobId) {
      if (!identifier(jobId)) throw new ToolChatError("invalid_job_id");
      return "/jobs/" + encodeURIComponent(jobId);
    }
    async create({ sessionId, message, browserGeneration, responseLanguage }) {
      if (!text(sessionId, 128) || !text(message, 10000) || !integer(browserGeneration)
          || responseLanguage !== undefined && !["ko", "en"].includes(responseLanguage)) throw new ToolChatError("invalid_tool_chat_input");
      const data = { session_id: sessionId, message, browser_generation: browserGeneration,
        ...(responseLanguage === undefined ? {} : { response_language: responseLanguage }) };
      return validateJob((await this._request("/jobs", "POST", data)).job);
    }
    async get(jobId, options) { return validateJob((await this._request(this._id(jobId), "GET", undefined, options)).job, jobId); }
    async approval(jobId) {
      const body = await this._request(this._id(jobId) + "/approval", "GET");
      if (body.job_id !== jobId || !STATES.has(body.state)) invalid();
      if (body.state === "WAITING_APPROVAL") validateApproval(body.approval);
      else if (body.approval !== null) invalid();
      return copy(body);
    }
    async decide(jobId, { proposalId, browserGeneration, allow, scope }) {
      if (!identifier(proposalId) || !integer(browserGeneration) || typeof allow !== "boolean"
          || scope !== undefined && scope !== null && !scopeValid(scope)) throw new ToolChatError("invalid_tool_chat_input");
      return validateJob((await this._request(this._id(jobId) + "/approval", "POST", {
        proposal_id: proposalId, browser_generation: browserGeneration, allow,
        ...(allow && scope != null ? { scope: copy(scope) } : {}),
      })).job, jobId);
    }
    async cancel(jobId) { return validateJob((await this._request(this._id(jobId) + "/cancel", "POST", {})).job, jobId); }
    async reconcile(jobId) { return validateJob((await this._request(this._id(jobId) + "/reconcile", "POST", {})).job, jobId); }
  }

  class ToolChatTracker {
    constructor({ client, storage = null, pollMs = 1000, retryDelays = [300, 1200], maxRecords = 128,
      setTimer = setTimeout, clearTimer = clearTimeout, controllerFactory = () => new AbortController(), onListenerError = () => {} } = {}) {
      if (!client || !Number.isFinite(pollMs) || pollMs <= 0 || !Array.isArray(retryDelays)
          || retryDelays.length > 5 || retryDelays.some((n) => !Number.isFinite(n) || n < 0)
          || !integer(maxRecords) || maxRecords === 0) throw new TypeError("Invalid tracker configuration.");
      Object.assign(this, { client, storage, pollMs, maxRecords, setTimer, clearTimer, controllerFactory, onListenerError });
      this.retryDelays = retryDelays.slice(); this.records = new Map(); this.listeners = new Map();
      this.context = null; this.closed = false; this.reportedErrors = new WeakSet(); this._restore();
    }
    on(name, listener) {
      if (typeof listener !== "function") throw new TypeError("Listener must be a function.");
      if (!this.listeners.has(name)) this.listeners.set(name, new Set());
      this.listeners.get(name).add(listener);
      return () => this.listeners.get(name)?.delete(listener);
    }
    _emit(name, record, details = {}) {
      if (this.closed) return;
      for (const listener of this.listeners.get(name) || []) {
        try { listener({ ...this._snapshot(record), ...copy(details) }); }
        catch (error) { try { this.onListenerError(error); } catch (_) {} }
      }
    }
    _visible(record) {
      return !this.closed && this.records.get(record.sessionId) === record && !record.restored && !record.cancelIntent && this.context
        && record.sessionId === this.context.sessionId && record.viewGeneration === this.context.generation
        && record.resetVersion === this.context.resetVersion;
    }
    _blocked(record) {
      return record.creating || record.verificationRequired || record.reservationOwned !== false || record.unsettled !== false
        || record.state !== null && !TERMINAL.has(record.state);
    }
    _snapshot(record) {
      return { sessionId: record.sessionId, jobId: record.jobId, browserGeneration: record.browserGeneration,
        viewGeneration: record.viewGeneration, backendRevision: record.backendRevision, resetVersion: record.resetVersion,
        state: record.state, phase: record.phase, localState: record.localState,
        reservation_owned: record.reservationOwned, unsettled: record.unsettled, operationId: record.operationId,
        approval: this._visible(record) ? copy(record.approval) : null, approvalVersion: record.approvalVersion,
        cancelIntent: record.cancelIntent, verificationRequired: record.verificationRequired,
        creating: record.creating, polling: Boolean(record.readPromise), approving: record.approvalBusy,
        cancelling: record.cancelBusy, reconciling: record.reconcileBusy, blocked: this._blocked(record),
        resultDelivered: record.resultDelivered, resultApplied: record.resultApplied, visible: Boolean(this._visible(record)),
        restored: record.restored,
        errorCode: record.errorCode };
    }
    get(sessionId) { const r = this.records.get(sessionId); return r ? this._snapshot(r) : null; }
    isBlocked(sessionId) { const r = this.records.get(sessionId); return Boolean(r && this._blocked(r)); }
    _record(sessionId) {
      const r = this.records.get(sessionId);
      if (!r) throw new ToolChatError("job_not_found");
      return r;
    }
    _open() { if (this.closed) throw new ToolChatError("tracker_closed"); }
    _busy(r) { return r.creating || r.approvalBusy || r.cancelBusy || r.reconcileBusy; }
    _stopTimer(r) { if (r.timer !== null) this.clearTimer(r.timer); r.timer = null; }
    _schedule(r, delay = this.pollMs) {
      this._stopTimer(r);
      if (this.closed || this.records.get(r.sessionId) !== r || !r.jobId || this._busy(r) || r.readPromise
          || !this._blocked(r) || r.unsettled && TERMINAL.has(r.state)) return;
      r.timer = this.setTimer(() => { r.timer = null; this.check(r.sessionId).catch(() => {}); }, delay);
    }
    _persist() {
      try {
        const rows = [...this.records.values()].filter((r) => this._blocked(r)).map((r) => ({
          sessionId: r.sessionId, jobId: r.jobId, browserGeneration: r.browserGeneration,
          viewGeneration: r.viewGeneration, backendRevision: r.backendRevision, resetVersion: r.resetVersion,
          state: r.state, reservationOwned: r.reservationOwned, unsettled: r.unsettled,
          cancelIntent: r.cancelIntent, cancelAttempted: r.cancelAttempted, operationId: r.operationId,
        }));
        this.storage?.setItem(STORAGE_KEY, JSON.stringify(rows));
      } catch (_) { /* In-memory guards remain; never release on storage failure. */ }
    }
    _new(context) {
      return { sessionId: context.sessionId, jobId: null, browserGeneration: context.generation, viewGeneration: context.generation,
        resetVersion: context.resetVersion, backendRevision: null, state: null, phase: null, localState: "IDLE",
        reservationOwned: null, unsettled: null, operationId: null, verificationRequired: true,
        creating: false, approvalBusy: false, cancelBusy: false, reconcileBusy: false, cancelIntent: false, cancelAttempted: false,
        resultDelivered: false, resultApplied: false, restored: false, approval: null, approvalVersion: 0, approvalStamp: "",
        epoch: 0, retries: 0, timer: null, readPromise: null, readAbort: null, errorCode: null };
    }
    _restore() {
      try {
        const rows = JSON.parse(this.storage?.getItem(STORAGE_KEY) || "[]");
        if (!Array.isArray(rows) || rows.length > this.maxRecords) return;
        for (const row of rows) {
          if (!object(row) || !text(row.sessionId, 128) || !integer(row.browserGeneration) || !integer(row.resetVersion)
              || row.jobId !== null && !identifier(row.jobId)) continue;
          const r = this._new({ sessionId: row.sessionId, generation: row.browserGeneration, resetVersion: row.resetVersion });
          r.jobId = row.jobId; r.restored = true; r.localState = "VERIFY_REQUIRED";
          r.cancelIntent = row.cancelIntent === true; r.cancelAttempted = row.cancelAttempted === true;
          r.backendRevision = integer(row.backendRevision) ? row.backendRevision : null;
          this.records.set(r.sessionId, r);
        }
      } catch (_) { /* Corrupt storage never authorizes execution. */ }
    }
    async setContext(context) {
      this._open();
      if (context !== null && (!text(context.sessionId, 128) || !integer(context.generation) || !integer(context.resetVersion))) throw new ToolChatError("invalid_view_context");
      const previous = this.context;
      this.context = context === null ? null : { ...context };
      if (previous && (!context || previous.sessionId !== context.sessionId || previous.generation !== context.generation || previous.resetVersion !== context.resetVersion)) {
        const r = this.records.get(previous.sessionId);
        if (r && this._blocked(r)) {
          r.epoch += 1; r.approval = null; this._stopTimer(r);
          r.cancelIntent = true; this._persist(); this._emit("state", r);
          if (r.jobId && !r.cancelAttempted) {
            try { await this._cancelKnown(r); } catch (_) { /* Recorded, never auto-retried. */ }
          }
        }
      }
    }
    async start({ message, responseLanguage } = {}) {
      this._open();
      if (!this.context) throw new ToolChatError("invalid_view_context");
      const old = this.records.get(this.context.sessionId);
      if (old && this._blocked(old)) throw new ToolChatError("session_unsettled");
      // Validate before installing a reservation; malformed input never sends POST.
      if (!text(message, 10000) || responseLanguage !== undefined && !["ko", "en"].includes(responseLanguage)) throw new ToolChatError("invalid_tool_chat_input");
      if (!old && this.records.size >= this.maxRecords) {
        for (const [id, r] of this.records) if (!this._blocked(r)) { this.records.delete(id); break; }
        if (this.records.size >= this.maxRecords) throw new ToolChatError("tracker_capacity");
      }
      const r = this._new(this.context); r.creating = true; r.localState = "CREATING";
      this.records.set(r.sessionId, r); this._persist(); this._emit("state", r);
      try {
        const job = await this.client.create({ sessionId: r.sessionId, message, browserGeneration: r.browserGeneration, responseLanguage });
        r.creating = false;
        this._accept(r, job, true);
        // Do not abort/lose the create receipt when the screen changes.
        if (r.cancelIntent && !r.cancelAttempted && this._blocked(r) && !this.closed) await this._cancelKnown(r);
        return this._snapshot(r);
      } catch (error) {
        if (!r.jobId) {
          r.creating = false; r.errorCode = error.code || "unknown_error";
          const rejected = error instanceof ToolChatError && !error.uncertain && [400, 403, 404, 409, 413, 415, 429].includes(error.status);
          r.localState = rejected ? "REJECTED" : "CREATION_UNCERTAIN";
          r.verificationRequired = !rejected || ["session_reserved", "session_unsettled"].includes(error.code);
          r.reservationOwned = false; r.unsettled = false;
          this._error(r, error); this._emit("failed", r, { code: r.errorCode }); this._persist();
        }
        throw error;
      } finally { r.creating = false; this._schedule(r); }
    }
    _accept(r, job, created = false) {
      if (this.records.get(r.sessionId) !== r) return;
      if (job.session_id !== r.sessionId || job.browser_generation !== r.browserGeneration || r.jobId && job.job_id !== r.jobId) invalid();
      if (r.state && TERMINAL.has(r.state) && job.state !== r.state
          || r.backendRevision !== null && job.revision !== r.backendRevision) invalid();
      const oldState = r.state;
      r.jobId = job.job_id; r.backendRevision = job.revision; r.state = job.state; r.phase = job.phase || job.state;
      r.reservationOwned = job.reservation_owned; r.unsettled = job.unsettled; r.operationId = job.operation_id;
      r.verificationRequired = false; r.localState = "TRACKING"; r.errorCode = job.error_code || null; r.retries = 0;
      const proposal = job.state === "WAITING_APPROVAL" ? job.pending : null;
      const stamp = proposal ? stable(proposal) : "";
      const changed = stamp !== r.approvalStamp;
      if (changed) { r.approvalVersion += 1; r.approvalStamp = stamp; }
      r.approval = copy(proposal);
      this._persist();
      if (created) this._emit("created", r);
      this._emit("state", r);
      if (changed && proposal && this._visible(r)) this._emit("approval", r, { proposal: copy(proposal) });
      if (oldState !== r.state) {
        if (r.state === "FAILED") this._emit("failed", r, { code: r.errorCode });
        if (r.state === "UNCERTAIN") this._emit("uncertain", r, { code: r.errorCode });
        if (r.state === "CANCELLED") this._emit("cancelled", r);
      }
      if (TERMINAL.has(r.state) && this._blocked(r)) this._emit("reservation_wait", r);
      if (r.state === "COMPLETED" && this._visible(r) && !r.resultDelivered) {
        r.resultDelivered = true;
        this._emit("completed", r, { result: copy(job.result) });
      }
    }
    wasReported(error) { return Boolean(error && typeof error === "object" && this.reportedErrors.has(error)); }
    _error(r, error) {
      if (error && typeof error === "object") this.reportedErrors.add(error);
      const communication = !error.status || ["invalid_job_response", "invalid_json_response"].includes(error.code);
      this._emit(communication ? "communication_error" : "request_error", r,
        { error: { code: error.code || "unknown_error", status: error.status || 0, message: error.message, uncertain: Boolean(error.uncertain) } });
    }
    check(sessionId, { verifyRelease = false } = {}) {
      this._open(); const r = this._record(sessionId);
      if (!r.jobId) return Promise.reject(new ToolChatError("creation_uncertain"));
      if (verifyRelease) { r.verificationRequired = true; this._persist(); }
      if (r.readPromise) return r.readPromise;
      if (this._busy(r)) return Promise.resolve(this._snapshot(r));
      this._stopTimer(r);
      const epoch = r.epoch; r.readAbort = this.controllerFactory();
      let nextDelay = null;
      r.readPromise = Promise.resolve().then(() => this.client.get(r.jobId, { signal: r.readAbort.signal })).then((job) => {
        if (this.closed || epoch !== r.epoch || this.records.get(sessionId) !== r) return this._snapshot(r);
        this._accept(r, job); nextDelay = this.pollMs; return this._snapshot(r);
      }).catch((error) => {
        if (this.closed || epoch !== r.epoch) return this._snapshot(r);
        if (this._blocked(r)) r.verificationRequired = true;
        r.errorCode = error.code || "unknown_error"; this._persist(); this._error(r, error);
        const transient = !error.status || [500, 502, 503, 504].includes(error.status);
        if (transient && r.retries < this.retryDelays.length) nextDelay = this.retryDelays[r.retries++];
        throw error;
      }).finally(() => {
        r.readPromise = null; r.readAbort = null;
        // Automatic polling has no UI caller's finally block. Publish its idle
        // snapshot so Review/Check controls are not left permanently disabled.
        if (this.records.get(sessionId) === r) this._emit("state", r);
        if (nextDelay !== null) this._schedule(r, nextDelay);
        else if (epoch !== r.epoch) this._schedule(r);
      });
      return r.readPromise;
    }
    async decide(sessionId, { proposalId, approvalVersion, allow, scope } = {}) {
      this._open(); const r = this._record(sessionId);
      if (r.approvalBusy || r.cancelBusy || r.reconcileBusy) throw new ToolChatError("request_in_progress");
      if (!this._visible(r) || r.state !== "WAITING_APPROVAL" || !r.approval || r.approval.proposal_id !== proposalId
          || r.approvalVersion !== approvalVersion || typeof allow !== "boolean") throw new ToolChatError("stale_approval");
      if (allow && (r.approval.scope !== null ? !scopeValid(scope) || stable(scope) !== stable(r.approval.scope) : scope != null)) throw new ToolChatError("filesystem_invalid_scope");
      const epoch = ++r.epoch; this._stopTimer(r); r.approvalBusy = true;
      try {
        const job = await this.client.decide(r.jobId, { proposalId, browserGeneration: r.browserGeneration, allow, scope });
        if (epoch === r.epoch && !this.closed) this._accept(r, job);
        return this._snapshot(r);
      } catch (error) {
        if (epoch === r.epoch) {
          r.verificationRequired = true; r.approval = null; r.approvalStamp = ""; r.approvalVersion += 1;
          this._persist(); this._error(r, error);
        }
        throw error;
      } finally { r.approvalBusy = false; this._schedule(r); }
    }
    cancel(sessionId) {
      this._open(); const r = this._record(sessionId);
      r.cancelIntent = true; r.approval = null; this._persist();
      if (!r.jobId) { this._emit("reservation_wait", r); return Promise.resolve(this._snapshot(r)); }
      if (!this._blocked(r)) return Promise.resolve(this._snapshot(r));
      return this._cancelKnown(r);
    }
    _cancelKnown(r) {
      if (r.cancelBusy) return r.cancelPromise;
      r.cancelIntent = true; r.cancelAttempted = true; r.cancelBusy = true; r.approval = null;
      const epoch = ++r.epoch; this._stopTimer(r); this._persist();
      r.cancelPromise = Promise.resolve().then(() => this.client.cancel(r.jobId)).then((job) => {
        if (epoch === r.epoch && !this.closed) this._accept(r, job);
        return this._snapshot(r);
      }).catch((error) => {
        if (epoch === r.epoch) { r.verificationRequired = true; this._persist(); this._error(r, error); }
        throw error;
      }).finally(() => { r.cancelBusy = false; this._schedule(r); });
      return r.cancelPromise;
    }
    async reconcile(sessionId) {
      this._open(); const r = this._record(sessionId);
      if (r.reconcileBusy || r.cancelBusy || r.approvalBusy) throw new ToolChatError("request_in_progress");
      if (!r.jobId || !TERMINAL.has(r.state) || !r.operationId || !r.unsettled) throw new ToolChatError("operation_unresolved");
      const epoch = ++r.epoch; this._stopTimer(r); r.reconcileBusy = true;
      try {
        const job = await this.client.reconcile(r.jobId);
        if (epoch === r.epoch && !this.closed) this._accept(r, job);
        return this._snapshot(r);
      } catch (error) { this._error(r, error); throw error; }
      finally { r.reconcileBusy = false; this._schedule(r); }
    }
    acknowledgeResult(sessionId, jobId) {
      const r = this._record(sessionId);
      if (!this._visible(r) || r.jobId !== jobId || !r.resultDelivered) throw new ToolChatError("stale_result");
      r.resultApplied = true;
    }
    dispose() {
      if (this.closed) return;
      this.closed = true; this.context = null;
      for (const r of this.records.values()) {
        r.epoch += 1; this._stopTimer(r); r.readAbort?.abort(); r.approval = null;
      }
      this._persist(); this.listeners.clear();
    }
  }

  const exported = { ToolChatClient, ToolChatTracker, ToolChatError };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else root.LocalAIToolChat = exported;
})(typeof globalThis !== "undefined" ? globalThis : this);

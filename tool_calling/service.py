"""Synchronous core primitives; MCP-5C will own inference and API adapters.

No background work starts on import. Session hooks must be wired by a future
Controller adapter; this module intentionally does not change ordinary /chat.
"""
from contextlib import contextmanager
from copy import deepcopy
from threading import Lock
import time
from uuid import uuid4

from mcp_manager.models import MCPRuntimeError
from mcp_manager.registry import canonical_json

from .approvals import TurnGrant, revoke_all
from .catalog import ToolCatalog, arguments_valid, identity
from .models import Job, JobState, Limits, TERMINAL, ToolChatError


class SessionReservations:
    """Shared arbitration interface for future ordinary Chat/Reset adapters."""
    def __init__(self):
        self._lock, self._owners = Lock(), {}

    def reserve(self, session_id, owner):
        with self._lock:
            if session_id in self._owners:
                raise ToolChatError("session_reserved")
            self._owners[session_id] = owner

    def release(self, session_id, owner):
        with self._lock:
            if self._owners.get(session_id) == owner:
                del self._owners[session_id]

    def transfer(self, session_id, old_owner, new_owner):
        with self._lock:
            if self._owners.get(session_id) != old_owner:
                raise ToolChatError("session_reserved")
            self._owners[session_id] = new_owner

    def owners(self):
        with self._lock:
            return dict(self._owners)

    def owner(self, session_id):
        with self._lock:
            return self._owners.get(session_id)

    @contextmanager
    def ordinary(self, session_id):
        owner = uuid4().hex
        self.reserve(session_id, owner)
        try:
            yield
        finally:
            self.release(session_id, owner)


class ToolChatService:
    def __init__(self, mcp, bindings, *, session_revision, reservations=None, limits=None, clock=time.monotonic):
        self.mcp, self.catalog = mcp, ToolCatalog(mcp, bindings)
        self.session_revision = session_revision
        self.reservations = reservations or SessionReservations()
        self.limits, self.clock = limits or Limits(), clock
        self._lock, self._jobs = Lock(), {}
        self._closed = False

    def _job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ToolChatError("job_not_found")
        return job

    def _release(self, job):
        if job.reservation_owned and not job.in_flight and not job.unsettled:
            self.reservations.release(job.session_id, job.job_id)
            job.reservation_owned = False

    def _end(self, job, state, code=None):
        job.state, job.error_code = state, code
        job.cancelled = job.cancelled or state == JobState.CANCELLED
        job.updated = job.ended = self.clock()
        job.waiting_since = None
        revoke_all(job)
        job.queue.clear()
        job.pending = None
        self._release(job)

    def _check(self, job):
        now = self.clock()
        if job.state in TERMINAL:
            raise ToolChatError("job_ended")
        if self._closed:
            self._end(job, JobState.CANCELLED, "service_closed")
            raise ToolChatError("service_closed")
        if job.state != JobState.WAITING_APPROVAL:
            job.active_seconds += max(0, now - job.updated)
        job.updated = now
        if (now - job.created >= self.limits.lifetime_seconds
                or job.active_seconds >= self.limits.active_seconds
                or job.waiting_since is not None and now - job.waiting_since >= self.limits.approval_seconds):
            self._end(job, JobState.FAILED, "job_timeout")
            raise ToolChatError("job_timeout")
        try:
            revision = self.session_revision(job.session_id)
        except Exception:
            revision = None
        if revision != job.revision:
            self._end(job, JobState.CANCELLED, "session_changed")
            raise ToolChatError("session_changed")

    def sweep(self):
        """Caller schedules this in MCP-5C; every entry also enforces expiry."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                if job.state not in TERMINAL:
                    try:
                        self._check(job)
                    except ToolChatError:
                        pass

    def create(self, session_id, revision, browser_generation, *, catalog_entries=None, reservation_owner=None):
        with self._lock:
            if self._closed:
                raise ToolChatError("service_closed")
        if (not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128
                or type(revision) is not int or revision < 0
                or type(browser_generation) is not int or browser_generation < 0):
            raise ToolChatError("invalid_job_context")
        self.sweep()
        catalog = self.catalog.discover() if catalog_entries is None else catalog_entries
        now = self.clock()
        job = Job(session_id, revision, browser_generation, now, now, catalog)
        if reservation_owner is None:
            self.reservations.reserve(session_id, job.job_id)
        else:
            self.reservations.transfer(session_id, reservation_owner, job.job_id)
        try:
            with job.lock:
                self._check(job)
            with self._lock:
                if self._closed:
                    raise ToolChatError("service_closed")
                # Bound completed as well as live jobs. Never evict unsettled work.
                if len(self._jobs) >= self.limits.max_jobs:
                    for key, old in list(self._jobs.items()):
                        if old.state in TERMINAL and not old.reservation_owned:
                            del self._jobs[key]
                            break
                if len(self._jobs) >= self.limits.max_jobs:
                    raise ToolChatError("job_capacity")
                self._jobs[job.job_id] = job
        except BaseException:
            self.reservations.release(session_id, job.job_id)
            raise
        return job.job_id

    def snapshot(self, job_id):
        job = self._job(job_id)
        with job.lock:
            if job.state not in TERMINAL:
                try:
                    self._check(job)
                except ToolChatError:
                    pass
            return {"job_id": job.job_id, "session_id": job.session_id, "revision": job.revision,
                    "browser_generation": job.browser_generation, "state": job.state.value,
                    "created": job.created, "updated": job.updated, "ended": job.ended,
                    "tool_count": job.tool_count, "ollama_count": job.ollama_count,
                    "operation_id": job.operation_id, "cancelled": job.cancelled,
                    "ollama_in_flight": job.ollama_in_flight,
                    "unsettled": job.unsettled, "reservation_owned": job.reservation_owned,
                    "error_code": job.error_code, "pending": deepcopy(job.pending)}

    def tools(self, job_id):
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            return [deepcopy(entry["schema"]) for entry in job.catalog.values()]

    def begin_ollama_request(self, job_id):
        """Reserve inference admission; caller MUST report completion below."""
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            if job.in_flight or job.queue or job.state == JobState.WAITING_APPROVAL:
                raise ToolChatError("job_busy")
            if job.ollama_count >= self.limits.ollama_requests:
                self._end(job, JobState.FAILED, "ollama_limit")
                raise ToolChatError("ollama_limit")
            job.ollama_count += 1
            job.state = JobState.RUNNING
            job.in_flight = job.ollama_in_flight = True
            job.operation_id = job.operation_server = None

    def end_ollama_request(self, job_id, *, completion_known=True):
        """Called after the server request actually ends, not merely UI abort."""
        job = self._job(job_id)
        with job.lock:
            if type(completion_known) is not bool:
                raise ToolChatError("invalid_completion_status")
            if not job.ollama_in_flight:
                raise ToolChatError("no_ollama_request")
            job.in_flight = job.ollama_in_flight = False
            if not completion_known:
                job.unsettled = True
                if job.state not in TERMINAL:
                    self._end(job, JobState.UNCERTAIN, "ollama_completion_unknown")
                return
            if job.state in TERMINAL:
                self._release(job)
                return
            self._check(job)

    def enqueue(self, job_id, calls):
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            if job.in_flight or job.state == JobState.WAITING_APPROVAL:
                raise ToolChatError("job_busy")
            try:
                if not isinstance(calls, list) or not calls:
                    raise ToolChatError("invalid_tool_calls")
                if job.tool_count + len(job.queue) + len(calls) > self.limits.tool_calls:
                    raise ToolChatError("tool_limit")
                validated = []
                for call in calls:
                    if not isinstance(call, dict) or set(call) != {"name", "arguments"} or call["name"] not in job.catalog:
                        raise ToolChatError("tool_alias_denied")
                    entry = job.catalog[call["name"]]
                    args = arguments_valid(entry["context"], call["arguments"])
                    validated.append({"name": call["name"], "arguments": args})
                job.queue.extend(validated)
                job.state = JobState.RUNNING
            except Exception:
                self._end(job, JobState.FAILED, "invalid_tool_calls")
                raise

    def approve(self, job_id, proposal_id, browser_generation, *, allow, scope=None):
        """Trusted user-action adapter only; never pass this method to an LLM."""
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            if (job.state != JobState.WAITING_APPROVAL or not job.pending
                    or job.pending["proposal_id"] != proposal_id or browser_generation != job.browser_generation
                    or type(allow) is not bool):
                raise ToolChatError("stale_approval")
            if not allow:
                self._end(job, JobState.CANCELLED, "approval_denied")
                return
            call = deepcopy(job.queue[0])
            entry = job.catalog[call["name"]]
        # Never keep the job lock while MCP discovery blocks.
        try:
            context = self.catalog.current(entry)
        except Exception:
            with job.lock:
                if job.state not in TERMINAL:
                    self._end(job, JobState.FAILED, "tool_policy_changed")
            raise
        approved_scope = None
        if entry["kind"] == "filesystem":
            approved_scope = entry["policy"].scope(scope)
            entry["policy"].validate(context["tool"]["name"], call["arguments"], approved_scope, dispatch=True)
        elif scope is not None:
            raise ToolChatError("unexpected_filesystem_scope")
        with job.lock:
            self._check(job)
            if not job.pending or job.pending["proposal_id"] != proposal_id:
                raise ToolChatError("stale_approval")
            job.grants.append(TurnGrant(job.job_id, job.session_id, job.revision, identity(context), approved_scope))
            job.state, job.waiting_since, job.pending = JobState.RUNNING, None, None

    def drain(self, job_id):
        job = self._job(job_id)
        if not job.runner.acquire(blocking=False):
            raise ToolChatError("job_busy")
        results = []
        try:
            while True:
                with job.lock:
                    self._check(job)
                    if not job.queue:
                        return results
                    call = deepcopy(job.queue[0])
                    entry = job.catalog[call["name"]]
                context = self.catalog.current(entry)
                with job.lock:
                    self._check(job)
                    grant = self._grant(job, entry, context, call["arguments"])
                    if grant is None:
                        if not job.pending:
                            job.pending = {"proposal_id": uuid4().hex, "server_id": context["server_id"],
                                           "tool_name": context["tool"]["name"], "arguments": deepcopy(call["arguments"])}
                            job.waiting_since = self.clock()
                        job.state = JobState.WAITING_APPROVAL
                        return results
                    job.in_flight = True  # Admission tracked before any blocking call.
                    job.operation_id = None
                    job.operation_server = context["server_id"]
                    job.state = JobState.EXECUTING_TOOL
                token, admitted, received = None, False, False
                try:
                    token = self.mcp.approve_call(context["server_id"], context["tool"]["name"], call["arguments"])["approval_token"]
                    def before_execute(latest, args):
                        nonlocal admitted
                        with job.lock:
                            self._check(job)
                            if identity(latest) != identity(context) or not grant.matches(job, latest):
                                raise ToolChatError("tool_policy_changed")
                            if entry["kind"] == "filesystem":
                                entry["policy"].verify_context(latest)
                                entry["policy"].validate(latest["tool"]["name"], args, grant.filesystem_scope, dispatch=True)
                            # Linearization point: cancel before here prevents dispatch;
                            # after here the operation is admitted, never assumed rolled back.
                            job.tool_count += 1
                            admitted = True
                    outcome = self.mcp.call_tool(context["server_id"], context["tool"]["name"],
                                                 call["arguments"], token, before_execute=before_execute)
                    received = True
                    with job.lock:
                        job.operation_id = outcome["operation_id"]
                        job.in_flight = False
                        if job.state in TERMINAL:
                            self._release(job)
                            return results  # A cancelled job never publishes a late result.
                        self._check(job)
                        if outcome["result"].get("is_error"):
                            raise ToolChatError("tool_result_error")
                        if len(canonical_json(outcome["result"]).encode()) > self.limits.result_bytes:
                            raise ToolChatError("tool_result_too_large")
                        results.append(deepcopy(outcome))
                        job.queue.popleft()
                        job.state = JobState.RUNNING
                except MCPRuntimeError as error:
                    with job.lock:
                        job.operation_id = error.operation_id
                        job.unsettled = bool(error.operation_id) or error.code in {"mcp_call_uncertain", "mcp_wait_timeout"}
                    raise
                except BaseException:
                    if admitted and not received:
                        with job.lock:
                            job.unsettled = True
                    raise
                finally:
                    if token is not None:
                        self.mcp.revoke_call_approval(token)
                    with job.lock:
                        job.in_flight = False
                        if job.state in TERMINAL:
                            self._release(job)
        except BaseException:
            with job.lock:
                if job.state not in TERMINAL:
                    self._end(job, JobState.UNCERTAIN if job.unsettled else JobState.FAILED, "tool_execution_failed")
            raise
        finally:
            job.runner.release()

    def _grant(self, job, entry, context, arguments):
        for grant in job.grants:
            if not grant.matches(job, context):
                continue
            if entry["kind"] == "filesystem":
                try:
                    entry["policy"].validate(context["tool"]["name"], arguments, grant.filesystem_scope, dispatch=True)
                except ToolChatError as error:
                    if error.code == "filesystem_outside_grant":
                        continue
                    raise
            return grant
        return None

    def finish(self, job_id, *, success=True):
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            if job.in_flight or job.queue or job.state == JobState.WAITING_APPROVAL:
                raise ToolChatError("job_busy")
            self._end(job, JobState.COMPLETED if success else JobState.FAILED)

    def finalize(self, job_id, commit):
        """Serialize cancellation with the short revision-checked DB commit."""
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            if job.in_flight or job.queue or job.state == JobState.WAITING_APPROVAL:
                raise ToolChatError("job_busy")
            try:
                result = commit()
            except Exception:
                self._end(job, JobState.FAILED, "session_commit_failed")
                raise
            self._end(job, JobState.COMPLETED)
            return result

    def fail(self, job_id, code):
        job = self._job(job_id)
        with job.lock:
            if job.state not in TERMINAL:
                self._end(job, JobState.UNCERTAIN if job.unsettled else JobState.FAILED, code)

    def remaining_seconds(self, job_id):
        job = self._job(job_id)
        with job.lock:
            self._check(job)
            return min(self.limits.active_seconds - job.active_seconds,
                       self.limits.lifetime_seconds - (self.clock() - job.created))

    def cancel(self, job_id):
        job = self._job(job_id)
        with job.lock:
            if job.state in TERMINAL:
                return
            self._end(job, JobState.CANCELLED, "user_cancelled")

    def invalidate_session(self, session_id):
        with self._lock:
            ids = [job.job_id for job in self._jobs.values() if job.session_id == session_id]
        for job_id in ids:
            self.cancel(job_id)

    def invalidate_all(self):
        with self._lock:
            ids = list(self._jobs)
        for job_id in ids:
            self.cancel(job_id)

    def reconcile(self, job_id):
        """Only confirmed operation completion releases an unsettled reservation."""
        job = self._job(job_id)
        with job.lock:
            if not job.unsettled or job.in_flight or not job.operation_id:
                raise ToolChatError("operation_unresolved")
            server_id, operation_id = job.operation_server, job.operation_id
        outcome = self.mcp.operation(server_id, operation_id)
        if outcome["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ToolChatError("operation_unresolved")
        with job.lock:
            job.unsettled = False
            self._release(job)
        return outcome["state"]

    def shutdown(self):
        """Revoke jobs; does not stop shared MCP servers or pretend to cancel I/O."""
        with self._lock:
            self._closed = True
        self.invalidate_all()

    def acknowledge_stopped(self, job_id):
        """Explicit operator recovery after confirmed stop; no restart or replay."""
        job = self._job(job_id)
        with job.lock:
            if not job.unsettled or job.in_flight or not job.operation_server:
                raise ToolChatError("operation_unresolved")
            server_id = job.operation_server
        if self.mcp.get_server(server_id)["status"]["state"] != "STOPPED":
            raise ToolChatError("operation_unresolved")
        with job.lock:
            job.unsettled = False
            self._release(job)

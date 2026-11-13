"""MCP-5C APIs: Fake Ollama/SDK, real Service/Runtime, isolated SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import create_app
from mcp_manager import MCPManager, ServerConfig
from mcp_manager.launch import filesystem_artifact
from storage.sqlite_store import StorageUnavailable
from test_mcp_runtime import Scenario
from tool_calling import Limits, ToolChatError
from tool_calling.filesystem_policy import FileScope, FilesystemPolicy
from tool_calling.root_adapter import RootAdapter

HEADERS = {"X-Local-AI-Control": "web-console"}


class ToolChatBackendTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="local-ai-tool-api-"); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.data = self.root / "files"; self.data.mkdir()
        self.note = self.data / "note.txt"; self.note.write_text("fixture only", encoding="utf-8")
        self.time_scenario, self.fs_scenario = Scenario(), Scenario()
        self.schemas = {"get_current_time": {"type": "object", "properties": {"timezone": {"type": "string"}}},
                        "read_file": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                        "list_directory": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        async def time_tools(**_kwargs):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_current_time", description="get_current_time", input_schema=deepcopy(self.schemas["get_current_time"]))])
        async def fs_tools(**_kwargs):
            return SimpleNamespace(tools=[SimpleNamespace(name=name, description=name, input_schema=deepcopy(schema)) for name, schema in self.schemas.items() if name != "get_current_time"])
        self.time_scenario.list_tools, self.fs_scenario.list_tools = time_tools, fs_tools
        self.mode, self.ollama_calls, self.gates = "plain", [], []
        self.mock_ollama = patch("ai_controller.ask_ollama", side_effect=self.ollama).start(); self.addCleanup(patch.stopall)
        self.app = create_app(database_path=self.root / "sessions.sqlite3", mcp_database_path=self.root / "mcp.sqlite3",
                              mcp_approved_configs=[ServerConfig("time-profile", sys.executable, ("time-fixture",)),
                                                    ServerConfig("fs-profile", sys.executable, ("fs-fixture", "--read-only", str(self.data)))],
                              mcp_runtime_factory=lambda: MCPManager(call_timeout=0.08,
                                  _client_factory=lambda config, err: (self.time_scenario if config.args[0] == "time-fixture" else self.fs_scenario).client(config, err)),
                              tool_bindings_factory=self.bindings)
        self.app.testing = True; self.backend = self.app.extensions["tool_chat"]; self.mcp = self.app.extensions["mcp_service"]
        self.addCleanup(self.cleanup)
        self.time_id = self.mcp.create_server("time-profile", "Time fixture")["server_id"]
        self.fs_id = self.mcp.create_server("fs-profile", "Filesystem fixture")["server_id"]
        self.mcp.start_server(self.time_id); self.mcp.start_server(self.fs_id)
        self.mcp.set_permission(self.time_id, "get_current_time", True)
        for name in ("read_file", "list_directory"): self.mcp.set_permission(self.fs_id, name, True)
        self.session = self.http("POST", "/sessions").json["session_id"]

    def cleanup(self):
        for gate in self.gates: gate.set()
        self.time_scenario.call_gate.set(); self.fs_scenario.call_gate.set()
        self.backend.shutdown()
        self.backend._pool.shutdown(wait=True, cancel_futures=True)
        self.mcp.shutdown()

    def bindings(self, mcp):
        result = {self.time_id: ("time", None)}
        if os.name == "nt":
            context = mcp.inspect_server(self.fs_id)
            policy = FilesystemPolicy([self.data], verified=True,
                server_identity=(self.fs_id, context["config_hash"], context["connection_generation"]))
            result[self.fs_id] = ("filesystem", policy)
        return result

    def http(self, method, path, data=None, headers=None):
        with self.app.test_client() as client:
            return client.open(path, method=method, json=data, headers=HEADERS if headers is None else headers)

    def start(self, **changes):
        data = {"session_id": self.session, "message": "test question", "browser_generation": 1, "response_language": "en"}
        data.update(changes)
        response = self.http("POST", "/tool-chat/jobs", data)
        self.assertEqual(response.status_code, 202, response.json)
        return response.json["job"]["job_id"]

    def wait(self, job, states):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.http("GET", "/tool-chat/jobs/" + job)
            self.assertEqual(response.status_code, 200, response.json)
            if response.json["job"]["state"] in states: return response.json["job"]
            time.sleep(0.01)
        self.fail("job did not reach " + repr(states) + ": " + repr(response.json))

    def approve(self, job, *, allow=True, generation=1, scope=True):
        status = self.wait(job, {"WAITING_APPROVAL"}); pending = status["pending"]
        body = {"proposal_id": pending["proposal_id"], "browser_generation": generation, "allow": allow}
        if scope and pending["scope"]: body["scope"] = pending["scope"]
        return self.http("POST", "/tool-chat/jobs/" + job + "/approval", body)

    def history(self, session=None):
        return self.http("GET", "/sessions/" + (session or self.session) + "/history").json["messages"]

    def ollama(self, messages, *, tools=None, **_kwargs):
        self.ollama_calls.append(deepcopy(messages))
        if self.mode == "block":
            self.entered.set(); self.release.wait(5)
        if self.mode == "timeout": raise TimeoutError("private upstream detail")
        if self.mode == "invalid": return {"message": {"role": "assistant", "content": ""}}
        if self.mode == "bad_metrics": return {"message": {"role": "assistant", "content": "answer"}, "eval_duration": float("nan")}
        tool_messages = sum(m["role"] == "tool" for m in messages)
        names = {t["function"]["description"]: t["function"]["name"] for t in tools or []}
        def call(name, args): return {"function": {"name": names.get(name, "unexposed_alias"), "arguments": args}}
        calls = []
        if self.mode in {"time", "repeat"} and not tool_messages:
            calls = [call("get_current_time", {"timezone": "Asia/Seoul"})] * (2 if self.mode == "repeat" else 1)
        elif self.mode == "loop": calls = [call("get_current_time", {})]
        elif self.mode == "unknown": calls = [{"function": {"name": "unexposed_alias", "arguments": {}}}]
        elif self.mode == "bad_args": calls = [call("get_current_time", {"timezone": 123})]
        elif self.mode == "malformed": calls = [{"function": {"name": names.get("get_current_time"), "arguments": "{}"}}]
        elif self.mode == "mixed":
            sequence = [("get_current_time", {}), ("list_directory", {"path": str(self.data)}),
                        ("read_file", {"path": str(self.note)}), ("get_current_time", {})]
            if tool_messages < len(sequence):
                name, args = sequence[tool_messages]; calls = [call(name, args)]
        if calls: return {"message": {"role": "assistant", "content": "", "tool_calls": calls}}
        return {"message": {"role": "assistant", "content": "final answer"}, "eval_count": 5, "eval_duration": 1000000000}

    def test_plain_job_commits_only_original_question_and_final_answer(self):
        job = self.start(); status = self.wait(job, {"COMPLETED"})
        self.assertEqual(status["result"]["metrics"]["tokens_per_second"], 5)
        self.assertEqual([m["content"] for m in self.history()], ["test question", "final answer"])
        self.assertEqual(self.history()[1]["language"], "en")
        self.assertIn("English", self.ollama_calls[0][0]["content"])
        self.assertEqual(status["result"]["revision"], 1)

    def test_time_call_waits_for_approval_and_reuses_grant_for_same_response(self):
        self.mode = "repeat"; job = self.start()
        pending = self.wait(job, {"WAITING_APPROVAL"})["pending"]
        self.assertTrue(pending["turn_scoped"]); self.assertEqual(pending["server_name"], "Time fixture")
        self.assertEqual(self.time_scenario.calls, []); self.assertEqual(self.history(), [])
        self.assertEqual(self.approve(job).status_code, 202)
        self.wait(job, {"COMPLETED"}); self.assertEqual(len(self.time_scenario.calls), 2)
        self.assertEqual(len(self.history()), 2)
        new = self.start(); self.wait(new, {"WAITING_APPROVAL"})
        self.assertEqual(len(self.time_scenario.calls), 2)

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_time_filesystem_sequential_rounds_and_tool_history_not_persisted(self):
        self.mode = "mixed"; job = self.start()
        for _ in range(3): self.assertEqual(self.approve(job).status_code, 202)
        status = self.wait(job, {"COMPLETED"})
        self.assertEqual(status["tool_count"], 4); self.assertEqual(status["ollama_count"], 5)
        self.assertEqual([name for name, _args in self.fs_scenario.calls], ["list_directory", "read_file"])
        self.assertEqual([m["role"] for m in self.history()], ["user", "assistant"])
        self.assertEqual(sum(m["role"] == "tool" for m in self.ollama_calls[-1]), 4)

    def test_deny_and_cancel_do_not_commit_or_execute(self):
        self.mode = "time"; job = self.start(); self.approve(job, allow=False)
        self.wait(job, {"CANCELLED"}); self.assertEqual(self.history(), [])
        new = self.start(); self.wait(new, {"WAITING_APPROVAL"})
        self.http("POST", "/tool-chat/jobs/" + new + "/cancel", {})
        self.wait(new, {"CANCELLED"}); self.assertEqual(self.time_scenario.calls, [])

    def test_general_chat_conflict_and_history_read_during_approval(self):
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        response = self.http("POST", "/chat", {"session_id": self.session, "message": "ordinary"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"]["code"], "session_reserved")
        self.assertEqual(self.history(), [])

    def test_job_id_returns_before_discovery_and_cancel_blocks_followup_inference(self):
        entered, release = Event(), Event(); self.gates.append(release)
        original = self.backend.bindings_factory
        def slow():
            entered.set(); release.wait(3); return original()
        self.backend.bindings_factory = slow
        job = self.start(); self.assertTrue(entered.wait(2))
        self.assertEqual(self.http("POST", "/tool-chat/jobs/"+job+"/cancel", {}).status_code, 200)
        self.assertFalse(self.backend.status(job)["reservation_owned"])
        release.set()
        self.backend._work[job].future.result(3)
        self.assertEqual(self.ollama_calls, []); self.assertEqual(self.history(), [])

    def test_global_admission_cap_covers_running_and_queued_jobs(self):
        self.mode = "block"; self.entered, self.release = Event(), Event(); self.gates.append(self.release)
        self.backend.bindings_factory = lambda: {}
        for i in range(8):
            session = self.session if i == 0 else self.http("POST", "/sessions").json["session_id"]
            self.start(session_id=session)
        ninth = self.http("POST", "/sessions").json["session_id"]
        response = self.http("POST", "/tool-chat/jobs", {"session_id": ninth, "message": "over cap", "browser_generation": 1})
        self.assertEqual(response.status_code, 429)

    def test_discovery_time_is_included_in_processing_budget(self):
        now = [0.0]; self.backend.core.clock = lambda: now[0]
        def slow():
            now[0] = 400.0; return {}
        self.backend.bindings_factory = slow
        job = self.start(); status = self.wait(job, {"FAILED"})
        self.assertEqual(status["error_code"], "job_timeout")
        self.assertEqual(self.ollama_calls, []); self.assertEqual(self.history(), [])

    def test_ordinary_chat_running_prevents_tool_chat_creation(self):
        self.mode = "block"; self.entered, self.release = Event(), Event(); self.gates.append(self.release)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.http, "POST", "/chat", {"session_id": self.session, "message": "ordinary"})
            try:
                self.assertTrue(self.entered.wait(3))
                response = self.http("POST", "/tool-chat/jobs", {"session_id": self.session, "message": "job", "browser_generation": 1})
                self.assertEqual(response.status_code, 409)
            finally: self.release.set()
            self.assertEqual(future.result(3).status_code, 200)

    def test_reset_waiting_job_and_other_session_isolation(self):
        other = self.http("POST", "/sessions").json["session_id"]
        self.http("POST", "/chat", {"session_id": other, "message": "keep"})
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        self.assertEqual(self.http("POST", "/sessions/" + self.session + "/reset", {}).status_code, 200)
        self.wait(job, {"CANCELLED"}); self.assertEqual(len(self.history(other)), 2)
        self.assertEqual(self.time_scenario.calls, [])

    def test_delete_and_delete_all_cancel_waiting_jobs_without_recreation(self):
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        self.assertEqual(self.http("DELETE", "/sessions/" + self.session).status_code, 200)
        self.wait(job, {"CANCELLED"}); self.assertEqual(self.http("GET", "/sessions").json["sessions"], [])
        self.session = self.http("POST", "/sessions").json["session_id"]
        job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        self.assertEqual(self.http("DELETE", "/sessions").status_code, 200)
        self.wait(job, {"CANCELLED"}); self.assertEqual(self.http("GET", "/sessions").json["sessions"], [])

    def test_inflight_cancel_and_reset_wait_for_confirmed_ollama_completion(self):
        self.mode = "block"; self.entered, self.release = Event(), Event(); self.gates.append(self.release)
        job = self.start(); self.assertTrue(self.entered.wait(3))
        response = self.http("POST", "/sessions/" + self.session + "/reset", {})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.wait(job, {"CANCELLED"})["reservation_owned"])
        self.release.set()
        deadline = time.monotonic() + 3
        while self.backend.status(job)["reservation_owned"] and time.monotonic() < deadline: time.sleep(.01)
        self.assertFalse(self.backend.status(job)["reservation_owned"])
        self.assertEqual(self.history(), [])
        self.assertEqual(self.http("POST", "/sessions/" + self.session + "/reset", {}).status_code, 200)

    def test_delete_all_with_multiple_waiting_jobs_cancels_without_executing(self):
        other = self.http("POST", "/sessions").json["session_id"]
        self.mode = "time"
        # Finish Discovery before starting the second job: this case tests two
        # retained approval reservations, not same-server Discovery admission.
        first = self.start(); self.wait(first, {"WAITING_APPROVAL"})
        second = self.start(session_id=other); self.wait(second, {"WAITING_APPROVAL"})
        self.assertEqual(self.http("DELETE", "/sessions").status_code, 200)
        for job in (first, second):
            status = self.wait(job, {"CANCELLED"})
            self.assertFalse(status["reservation_owned"])
            self.assertFalse(status["unsettled"])
        self.assertEqual(self.http("GET", "/sessions").json["sessions"], [])
        self.assertEqual(self.time_scenario.calls, [])

    def test_cancel_acknowledgement_does_not_allow_partial_delete_all(self):
        other = self.http("POST", "/sessions").json["session_id"]
        self.assertEqual(self.http("POST", "/chat", {"session_id": other, "message": "preserve"}).status_code, 200)
        before = self.http("GET", "/sessions").json
        history = self.history(other)
        self.mode = "block"; self.entered, self.release = Event(), Event(); self.gates.append(self.release)
        job = self.start(); self.assertTrue(self.entered.wait(3))
        cancel = self.http("POST", "/tool-chat/jobs/" + job + "/cancel", {})
        self.assertEqual(cancel.status_code, 200)
        self.assertTrue(cancel.json["job"]["reservation_owned"])
        for method, path in (("DELETE", "/sessions"), ("DELETE", "/sessions/" + self.session),
                             ("POST", "/sessions/" + self.session + "/reset")):
            response = self.http(method, path, {} if method == "POST" else None)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["error"]["code"], "session_unsettled")
        self.assertEqual(self.http("GET", "/sessions").json, before)
        self.assertEqual(self.history(other), history)
        self.release.set()
        deadline = time.monotonic() + 3
        while self.backend.status(job)["reservation_owned"] and time.monotonic() < deadline: time.sleep(.01)
        self.assertFalse(self.backend.status(job)["reservation_owned"])
        self.assertEqual(self.http("DELETE", "/sessions").status_code, 200)

    def test_lost_final_response_recovers_via_reads_without_duplicate_commit(self):
        job = self.start(); self.wait(job, {"COMPLETED"})
        calls = len(self.ollama_calls)
        for _ in range(3):
            status = self.http("GET", "/tool-chat/jobs/" + job).json["job"]
            self.assertEqual(status["state"], "COMPLETED")
            self.assertFalse(status["reservation_owned"])
            self.assertIsNone(self.http("GET", "/tool-chat/jobs/" + job + "/approval").json["approval"])
            self.assertEqual(len(self.history()), 2)
        self.assertEqual(len(self.ollama_calls), calls)
        self.assertEqual(self.time_scenario.calls, [])

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_invalid_relative_path_approval_keeps_safe_wait_then_cancel_or_expiry_releases(self):
        def relative(messages, *, tools=None, **_kwargs):
            self.ollama_calls.append(deepcopy(messages))
            alias = next(t["function"]["name"] for t in tools if t["function"]["description"] == "read_file")
            return {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": alias, "arguments": {"path": "note.txt"}}}]}}
        self.mock_ollama.side_effect = relative
        for ending in ("cancel", "expiry"):
            job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
            response = self.approve(job)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["error"]["code"], "filesystem_invalid_path")
            for _ in range(2):
                status = self.http("GET", "/tool-chat/jobs/" + job).json["job"]
                self.assertEqual(status["state"], "WAITING_APPROVAL")
                self.assertTrue(status["reservation_owned"])
            self.assertEqual(self.fs_scenario.calls, [])
            self.assertEqual(self.history(), [])
            if ending == "cancel":
                self.assertEqual(self.http("POST", "/tool-chat/jobs/" + job + "/cancel", {}).status_code, 200)
                status = self.wait(job, {"CANCELLED"})
            else:
                self.backend.core.limits = Limits(approval_seconds=.1)
                status = self.wait(job, {"FAILED"})
                self.assertEqual(status["error_code"], "job_timeout")
            self.assertFalse(status["reservation_owned"])
            self.assertFalse(status["unsettled"])
        self.assertEqual(len(self.ollama_calls), 2)  # No automatic model or approval retry.

    def test_revision_change_prevents_approval_and_history_commit(self):
        self.mode = "time"; job = self.start(); status = self.wait(job, {"WAITING_APPROVAL"})
        store = self.app.extensions["session_store"]; store.reset(self.session, 0)
        response = self.http("POST", "/tool-chat/jobs/"+job+"/approval", {"proposal_id": status["pending"]["proposal_id"], "browser_generation": 1, "allow": True})
        self.assertEqual(response.status_code, 409); self.assertEqual(self.history(), [])

    def test_old_generation_approval_and_cancelled_job_cannot_affect_new_job(self):
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        self.assertEqual(self.approve(job, generation=2).status_code, 409)
        old = self.backend.status(job)["pending"]["proposal_id"]
        self.http("POST", "/tool-chat/jobs/"+job+"/cancel", {})
        new = self.start(browser_generation=3); self.wait(new, {"WAITING_APPROVAL"})
        response = self.http("POST", "/tool-chat/jobs/"+job+"/approval", {"proposal_id": old, "browser_generation": 1, "allow": True})
        self.assertEqual(response.status_code, 409); self.assertEqual(self.time_scenario.calls, [])

    def test_restart_permission_and_schema_changes_reject_stale_approval(self):
        for change in ("restart", "block", "schema"):
            self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
            if change == "restart": self.mcp.stop_server(self.time_id); self.mcp.start_server(self.time_id)
            elif change == "block": self.mcp.set_permission(self.time_id, "get_current_time", False)
            else: self.schemas["get_current_time"]["description"] = "changed"
            response = self.approve(job)
            self.assertIn(response.status_code, {400, 403, 409})
            self.wait(job, {"FAILED"})
            self.mcp.set_permission(self.time_id, "get_current_time", True)
        self.assertEqual(self.time_scenario.calls, [])

    def test_mcp_timeout_retains_reservation_and_reconcile_does_not_replay(self):
        self.mode = "time"; job = self.start(); self.time_scenario.call_gate.clear()
        self.approve(job); status = self.wait(job, {"UNCERTAIN"})
        self.assertTrue(status["reservation_owned"]); self.assertTrue(status["operation_id"])
        self.assertEqual(self.http("POST", "/sessions/"+self.session+"/reset", {}).status_code, 409)
        self.assertEqual(self.http("POST", "/tool-chat/jobs/"+job+"/reconcile", {}).status_code, 409)
        self.time_scenario.call_gate.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.http("POST", "/tool-chat/jobs/"+job+"/reconcile", {}).status_code == 200: break
            time.sleep(.01)
        self.assertFalse(self.backend.status(job)["reservation_owned"])
        self.assertEqual(len(self.time_scenario.calls), 1); self.assertEqual(self.history(), [])

    def test_unknown_ollama_completion_never_auto_releases_session(self):
        self.mode = "timeout"; job = self.start(); status = self.wait(job, {"UNCERTAIN"})
        self.assertTrue(status["reservation_owned"])
        self.assertEqual(self.http("POST", "/sessions/"+self.session+"/reset", {}).status_code, 409)
        self.assertNotIn("private upstream", json.dumps(status))

    def test_response_parse_failure_does_not_claim_ollama_completed(self):
        self.mock_ollama.side_effect = ValueError("private malformed upstream response")
        job = self.start(); status = self.wait(job, {"UNCERTAIN"})
        self.assertTrue(status["reservation_owned"]); self.assertEqual(self.history(), [])
        self.assertNotIn("private malformed", json.dumps(status))

    @unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
    def test_missing_or_narrow_filesystem_scope_cannot_auto_approve_root(self):
        self.mode = "mixed"; job = self.start(); self.approve(job)
        pending = self.wait(job, {"WAITING_APPROVAL"})["pending"]
        self.assertEqual(pending["scope"]["path"], str(self.data))
        self.assertEqual(self.approve(job, scope=False).status_code, 400)
        response = self.http("POST", "/tool-chat/jobs/"+job+"/approval", {
            "proposal_id": pending["proposal_id"], "browser_generation": 1, "allow": True,
            "scope": {"path": str(self.data), "kind": "directory", "recursive": False}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fs_scenario.calls, [])

    def test_duplicate_jobs_and_unknown_status_do_not_create_sessions(self):
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        response = self.http("POST", "/tool-chat/jobs", {"session_id": self.session, "message": "duplicate", "browser_generation": 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.http("GET", "/tool-chat/jobs/unknown").status_code, 404)
        self.assertEqual(len(self.http("GET", "/sessions").json["sessions"]), 1)

    def test_status_is_private_and_explicit_approval_endpoint_is_read_only(self):
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        response = self.http("GET", "/tool-chat/jobs/"+job+"/approval")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.json["approval"]["turn_scoped"])
        self.assertEqual(self.time_scenario.calls, [])

    def test_scope_wrong_field_types_are_400_without_internal_traceback(self):
        self.mode = "time"; job = self.start(); self.wait(job, {"WAITING_APPROVAL"})
        proposal = self.backend.status(job)["pending"]["proposal_id"]
        for fields in ({"kind": []}, {"path": []}, {"recursive": "true"}):
            scope = {"path": str(self.data), "kind": "directory", "recursive": True} | fields
            response = self.http("POST", "/tool-chat/jobs/"+job+"/approval", {
                "proposal_id": proposal, "browser_generation": 1, "allow": True, "scope": scope})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("Traceback", response.get_data(as_text=True))
        self.assertEqual(self.time_scenario.calls, [])

    def test_invalid_alias_arguments_messages_and_metrics_never_execute(self):
        for mode in ("unknown", "bad_args", "malformed", "invalid", "bad_metrics"):
            self.mode = mode; job = self.start(); self.wait(job, {"FAILED"})
            self.assertEqual(self.history(), [])
        self.assertEqual(self.time_scenario.calls, [])

    def test_session_commit_failure_is_atomic_and_sanitized(self):
        with patch.object(self.app.extensions["session_store"], "commit_turn", side_effect=StorageUnavailable("SECRET path")):
            job = self.start(); status = self.wait(job, {"FAILED"})
        self.assertEqual(status["error_code"], "session_commit_failed")
        self.assertNotIn("SECRET", json.dumps(status)); self.assertEqual(self.history(), [])

    def test_job_input_origin_guard_and_general_request_limit(self):
        self.assertEqual(self.http("POST", "/tool-chat/jobs", {}, headers={}).status_code, 403)
        self.assertEqual(self.http("POST", "/tool-chat/jobs", {}, headers=HEADERS | {"Origin": "https://outside.invalid"}).status_code, 403)
        base = {"session_id": self.session, "message": "x", "browser_generation": 1}
        for changes in ({"response_language": []}, {"browser_generation": True}, {"message": " "}, {"scope": "unauthorized"}):
            self.assertEqual(self.http("POST", "/tool-chat/jobs", base | changes).status_code, 400)
        self.assertEqual(self.http("POST", "/tool-chat/jobs", base | {"session_id": "missing"}).status_code, 404)
        with self.app.test_client() as client:
            response = client.post("/tool-chat/jobs", data=b'x'*(1024*1024+1), content_type="application/json", headers=HEADERS)
        self.assertEqual(response.status_code, 413)

    def test_model_request_limit_prevents_infinite_tool_loop(self):
        self.backend.core.limits = Limits(ollama_requests=2)
        self.mode = "loop"; job = self.start(); self.approve(job)
        status = self.wait(job, {"FAILED"})
        self.assertEqual(status["error_code"], "ollama_limit")
        self.assertEqual(status["ollama_count"], 2); self.assertEqual(len(self.time_scenario.calls), 2)

    def test_approval_wait_timeout_releases_lease_and_no_store_write(self):
        self.backend.core.limits = Limits(approval_seconds=.05)
        self.mode = "time"; job = self.start(); self.wait(job, {"FAILED"})
        self.assertFalse(self.backend.status(job)["reservation_owned"]); self.assertEqual(self.history(), [])

    def test_manual_mcp_call_remains_compatible(self):
        base = "/mcp/servers/"+self.time_id+"/tools/get_current_time"
        token = self.http("POST", base+"/approve", {"arguments": {}}).json["approval_token"]
        response = self.http("POST", base+"/call", {"approval_token": token, "arguments": {}})
        self.assertEqual(response.status_code, 200); self.assertFalse(response.json["result"]["is_error"])


class RootAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="local-ai-root-attest-"); self.addCleanup(temp.cleanup)
        root = Path(temp.name); self.root = root / "files"; self.root.mkdir()
        self.node = root / "node.exe"; self.node.touch()
        package = root / "server-filesystem"; (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "@modelcontextprotocol/server-filesystem", "version": "2.2.0"}))
        self.script = package / "dist/index.js"; self.script.write_text("// fixture, never executed\n")
        self.config = ServerConfig("fs", str(self.node), [str(self.script), "--read-only", str(self.root)])
        self.artifact = filesystem_artifact(self.config)
        self.context = {"server_id": "fs", "config_hash": "config", "connection_generation": "generation",
                        "config": {"executable": str(self.node), "args": list(self.config.args), "cwd": self.config.cwd},
                        "connection_info": {"default_stdio": True, "roots_disabled": True, "protocol_version": "2026-07-28", "filesystem_artifact": self.artifact}}
        self.mcp = SimpleNamespace(inspect_server=lambda _sid: deepcopy(self.context))
        self.adapter = RootAdapter(self.mcp)
        self.pin = patch("tool_calling.root_adapter.REVIEWED_FILESYSTEM", (self.artifact["version"], self.artifact["digest"])); self.pin.start(); self.addCleanup(self.pin.stop)

    @unittest.skipUnless(os.name == "nt", "Windows roots")
    def test_reviewed_static_root_is_attested_and_revalidated(self):
        policy = self.adapter.attest("fs"); policy.verify_context(self.context)
        self.assertEqual(policy.roots[0][0], self.root)
        scope = policy.scope(FileScope(str(self.root), "directory", True))
        policy.validate("list_directory", {"path": str(self.root)}, scope, dispatch=True)

    def test_unreviewed_implementation_or_dynamic_roots_are_denied(self):
        for key, value in (("roots_disabled", False), ("default_stdio", False), ("protocol_version", "unknown")):
            original = self.context["connection_info"][key]; self.context["connection_info"][key] = value
            with self.assertRaises(ToolChatError): self.adapter.attest("fs")
            self.context["connection_info"][key] = original
        self.script.write_text("// changed implementation")
        with self.assertRaisesRegex(ToolChatError, "filesystem_implementation_changed"): self.adapter.attest("fs")

    def test_wrong_start_snapshot_or_multiple_roots_are_denied(self):
        self.context["connection_info"]["filesystem_artifact"] = {"digest": "wrong", "version": "2.2.0"}
        with self.assertRaises(ToolChatError): self.adapter.attest("fs")
        self.context["config"]["args"].append(str(self.root))
        with self.assertRaises(ToolChatError): self.adapter.attest("fs")

    @unittest.skipUnless(os.name == "nt", "Windows roots")
    def test_root_replacement_and_connection_change_are_denied(self):
        policy = self.adapter.attest("fs")
        self.context["connection_generation"] = "next"
        with self.assertRaises(ToolChatError): policy.verify_context(self.context)
        self.context["connection_generation"] = "generation"
        self.root.rename(self.root.with_name("old")); self.root.mkdir()
        with self.assertRaises(ToolChatError): self.adapter.attest("fs")


class OllamaTransportTests(unittest.TestCase):
    def test_optional_tools_preserve_stream_and_think_defaults(self):
        from ai_controller import ask_ollama
        output = io.BytesIO(b'{"message":{"role":"assistant","content":"ok"}}')
        with patch("urllib.request.urlopen", return_value=output) as urlopen:
            ask_ollama([], tools=[{"type": "function"}], timeout=3, response_limit=1024)
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertFalse(body["stream"]); self.assertFalse(body["think"])
        self.assertIn("tools", body); self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)

    def test_tool_response_size_limit(self):
        from ai_controller import ask_ollama
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'x'*100)):
            with self.assertRaises(ValueError): ask_ollama([], tools=[], response_limit=20)

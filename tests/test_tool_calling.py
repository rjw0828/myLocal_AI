"""MCP-5B core with fake MCP, real temp paths and a real Service/fake SDK bridge.

No Ollama, operating DB, user files or installed MCP servers are used.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest

from mcp_manager import MCPManager, MCPTimeoutError, ServerConfig
from mcp_manager.errors import MCPServiceError
from mcp_manager.service import MCPService
from tool_calling import Limits, SessionReservations, ToolChatError, ToolChatService
from tool_calling.catalog import ToolCatalog
from tool_calling.filesystem_policy import FileScope, FilesystemPolicy, checked_path
from test_mcp_runtime import Scenario


class FakeMCP:
    def __init__(self):
        self.contexts = {}
        self.allowed = True
        self.state = "RUNNING"
        self.tokens = []
        self.issued = []
        self.calls = []
        self.before_issue = lambda: None
        self.before_dispatch = lambda: None
        self.during_call = lambda: None
        self.error = None
        self.operation_state = "RUNNING"
        self.add("time", "get_current_time", {"type": "object", "properties": {"timezone": {"type": "string"}}})

    def add(self, sid, name, schema):
        self.contexts[sid, name] = {"server_id": sid, "config_hash": "config", "connection_generation": "connection-1",
                                   "config": {"args": ["--read-only"]},
                                   "permission_generation": 0, "schema_hash": "schema-" + name,
                                   "tool": {"name": name, "description": "local tool", "input_schema": schema}}

    def list_servers(self):
        return [{"server_id": sid, "execution_approved": True, "status": {"state": self.state}}
                for sid in sorted({key[0] for key in self.contexts})]

    def get_server(self, _sid):
        return {"status": {"state": self.state}}

    def list_tools(self, sid):
        return [deepcopy(value["tool"]) | {"allowed": self.allowed} for (server_id, _name), value in self.contexts.items() if server_id == sid]

    def inspect_tool(self, sid, name):
        if not self.allowed:
            raise MCPServiceError("mcp_tool_permission_required", "blocked", 403)
        if self.state != "RUNNING":
            raise MCPServiceError("mcp_not_ready", "stopped", 409)
        return deepcopy(self.contexts[sid, name])

    def approve_call(self, _sid, _name, _args):
        self.before_issue()
        token = str(len(self.issued)); self.tokens.append(token); self.issued.append(token)
        return {"approval_token": token}

    def revoke_call_approval(self, token):
        self.tokens.remove(token)

    def call_tool(self, sid, name, args, token, *, before_execute=None):
        self.before_dispatch()
        before_execute(self.inspect_tool(sid, name), args)
        self.calls.append((sid, name, deepcopy(args)))
        self.during_call()
        if self.error:
            raise self.error
        return {"operation_id": "operation-1", "result": {"is_error": False, "content": [{"text": "local result"}]}}

    def operation(self, _sid, _operation):
        return {"state": self.operation_state}


class ToolChatCoreTests(unittest.TestCase):
    def setUp(self):
        self.mcp, self.revisions, self.now = FakeMCP(), {"a": 0, "b": 0}, 0.0
        self.service = self.make_service()
        self.addCleanup(self.service.shutdown)

    def make_service(self, **options):
        return ToolChatService(self.mcp, {"time": ("time", None)}, session_revision=self.revisions.get,
                               clock=lambda: self.now, **options)

    def job(self, session="a"):
        return self.service.create(session, self.revisions[session], 1)

    def alias(self, job):
        return self.service.tools(job)[0]["function"]["name"]

    def queue(self, job, count=1):
        self.service.enqueue(job, [{"name": self.alias(job), "arguments": {"timezone": "Asia/Seoul"}} for _ in range(count)])

    def approve(self, job):
        self.service.drain(job)
        self.service.approve(job, self.service.snapshot(job)["pending"]["proposal_id"], 1, allow=True)

    def test_created_running_completed_and_reservation_release(self):
        job = self.job(); self.assertEqual(self.service.snapshot(job)["state"], "CREATED")
        self.service.begin_ollama_request(job)
        self.assertTrue(self.service.snapshot(job)["ollama_in_flight"])
        self.service.end_ollama_request(job); self.service.finish(job)
        self.assertEqual(self.service.snapshot(job)["state"], "COMPLETED")
        self.assertIsNone(self.service.reservations.owner("a"))
        self.job()

    def test_duplicate_session_rejected_and_other_session_independent(self):
        self.job()
        with self.assertRaisesRegex(ToolChatError, "session_reserved"): self.job()
        self.job("b")

    def test_shutdown_revokes_jobs_and_prevents_new_reservations(self):
        job = self.job(); self.queue(job); self.approve(job); self.service.shutdown()
        self.assertEqual(self.service.snapshot(job)["state"], "CANCELLED")
        with self.assertRaisesRegex(ToolChatError, "service_closed"): self.job()
        self.assertIsNone(self.service.reservations.owner("a"))

    def test_a_b_a_new_job_cannot_accept_old_approval(self):
        a = self.job(); self.queue(a); self.service.drain(a)
        proposal = self.service.snapshot(a)["pending"]["proposal_id"]
        self.service.invalidate_session("a")
        b = self.job("b"); self.service.invalidate_session("b")
        new_a = self.service.create("a", 0, 3)
        with self.assertRaises(ToolChatError): self.service.approve(a, proposal, 1, allow=True)
        self.assertEqual(self.service.snapshot(new_a)["state"], "CREATED")
        self.assertEqual(self.service.snapshot(b)["state"], "CANCELLED")
        self.assertEqual(self.mcp.calls, [])

    def test_ordinary_lease_excludes_jobs_and_releases_on_exception(self):
        with self.assertRaisesRegex(ValueError, "expected"):
            with self.service.reservations.ordinary("a"):
                with self.assertRaisesRegex(ToolChatError, "session_reserved"): self.job()
                raise ValueError("expected")
        job = self.job()
        with self.assertRaisesRegex(ToolChatError, "session_reserved"):
            with self.service.reservations.ordinary("a"): pass
        self.service.cancel(job)

    def test_registry_allow_alone_never_executes(self):
        job = self.job(); self.queue(job); self.assertEqual(self.service.drain(job), [])
        self.assertEqual(self.service.snapshot(job)["state"], "WAITING_APPROVAL")
        self.assertEqual(self.mcp.calls, [])

    def test_grant_reused_same_job_tokens_are_fresh_and_consumed(self):
        job = self.job(); self.queue(job, 2); self.approve(job)
        self.assertEqual(len(self.service.drain(job)), 2)
        self.assertEqual(len(self.mcp.calls), 2); self.assertEqual(self.mcp.tokens, [])
        self.assertEqual(len(set(self.mcp.issued)), 2)
        self.assertEqual(self.service.snapshot(job)["tool_count"], 2)

    def test_grant_not_reused_next_job_or_wrong_proposal(self):
        job = self.job(); self.queue(job); self.approve(job); self.service.drain(job); self.service.finish(job)
        new = self.job(); self.queue(new); self.service.drain(new)
        with self.assertRaisesRegex(ToolChatError, "stale_approval"): self.service.approve(new, "old", 1, allow=True)
        self.assertEqual(len(self.mcp.calls), 1)

    def test_different_tool_requires_new_approval(self):
        self.mcp.add("time", "convert_time", {"type": "object"})
        job = self.job(); self.queue(job); self.approve(job); self.service.drain(job)
        other = next(alias for alias, entry in self.service._job(job).catalog.items() if entry["context"]["tool"]["name"] == "convert_time")
        self.service.enqueue(job, [{"name": other, "arguments": {}}]); self.service.drain(job)
        self.assertEqual(self.service.snapshot(job)["state"], "WAITING_APPROVAL")
        self.assertEqual(len(self.mcp.calls), 1)

    def test_denial_and_old_browser_generation(self):
        job = self.job(); self.queue(job); self.service.drain(job); proposal = self.service.snapshot(job)["pending"]["proposal_id"]
        with self.assertRaisesRegex(ToolChatError, "stale_approval"): self.service.approve(job, proposal, 2, allow=True)
        self.service.approve(job, proposal, 1, allow=False)
        self.assertEqual(self.service.snapshot(job)["state"], "CANCELLED"); self.assertEqual(self.mcp.calls, [])

    def test_cancel_before_approve_and_ended_job_cannot_resume(self):
        job = self.job(); self.queue(job); self.service.drain(job); proposal = self.service.snapshot(job)["pending"]["proposal_id"]
        self.service.cancel(job)
        for work in (lambda: self.service.approve(job, proposal, 1, allow=True), lambda: self.service.drain(job), lambda: self.service.begin_ollama_request(job)):
            with self.assertRaisesRegex(ToolChatError, "job_ended"): work()

    def test_cancel_wins_while_approval_is_revalidating_policy(self):
        job = self.job(); self.queue(job); self.service.drain(job)
        proposal = self.service.snapshot(job)["pending"]["proposal_id"]
        inspect = self.mcp.inspect_tool; entered, release = Event(), Event()
        def blocked(*args):
            entered.set(); self.assertTrue(release.wait(3)); return inspect(*args)
        self.mcp.inspect_tool = blocked
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.approve, job, proposal, 1, allow=True)
            try:
                self.assertTrue(entered.wait(3)); self.service.cancel(job)
                self.assertEqual(self.service.snapshot(job)["state"], "CANCELLED")
            finally: release.set()
            with self.assertRaises(ToolChatError): future.result(3)
        self.assertEqual(self.service._job(job).grants, [])

    def test_policy_change_during_approval_ends_job_and_releases_reservation(self):
        job = self.job(); self.queue(job); self.service.drain(job)
        proposal = self.service.snapshot(job)["pending"]["proposal_id"]
        self.mcp.allowed = False
        with self.assertRaises(MCPServiceError): self.service.approve(job, proposal, 1, allow=True)
        self.assertFalse(self.service.snapshot(job)["reservation_owned"])

    def test_policy_changes_revoke_grants_and_prevent_execution(self):
        for key, value in [("connection_generation", "new"), ("schema_hash", "new"), ("config_hash", "new"), ("permission_generation", 2)]:
            with self.subTest(key=key):
                job = self.job(); self.queue(job); self.approve(job)
                original = self.mcp.contexts["time", "get_current_time"][key]
                self.mcp.contexts["time", "get_current_time"][key] = value
                with self.assertRaisesRegex(ToolChatError, "tool_policy_changed"): self.service.drain(job)
                self.assertEqual(self.service._job(job).grants, [])
                self.mcp.contexts["time", "get_current_time"][key] = original
        self.assertEqual(self.mcp.calls, [])

    def test_registry_block_after_grant(self):
        job = self.job(); self.queue(job); self.approve(job); self.mcp.allowed = False
        with self.assertRaises(MCPServiceError): self.service.drain(job)
        self.assertEqual(self.service.snapshot(job)["state"], "FAILED"); self.assertEqual(self.mcp.calls, [])

    def test_cancel_between_token_issue_and_dispatch(self):
        job = self.job(); self.queue(job); self.approve(job)
        self.mcp.before_dispatch = lambda: self.service.cancel(job)
        with self.assertRaises(ToolChatError): self.service.drain(job)
        self.assertEqual(self.mcp.calls, []); self.assertEqual(self.mcp.tokens, [])
        self.assertFalse(self.service.snapshot(job)["reservation_owned"])

    def test_policy_change_between_token_issue_and_dispatch(self):
        job = self.job(); self.queue(job); self.approve(job)
        self.mcp.before_dispatch = lambda: self.mcp.contexts["time", "get_current_time"].update(connection_generation="other")
        with self.assertRaisesRegex(ToolChatError, "tool_policy_changed"): self.service.drain(job)
        self.assertEqual(self.mcp.calls, []); self.assertEqual(self.mcp.tokens, [])

    def test_cancel_during_call_holds_reservation_until_late_completion(self):
        job = self.job(); self.queue(job, 2); self.approve(job)
        entered, release = Event(), Event()
        def work():
            entered.set(); self.assertTrue(release.wait(3))
        self.mcp.during_call = work
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.drain, job)
            try:
                self.assertTrue(entered.wait(3)); self.service.cancel(job)
                self.assertTrue(self.service.snapshot(job)["reservation_owned"])
                with self.assertRaisesRegex(ToolChatError, "session_reserved"): self.job()
            finally: release.set()
            self.assertEqual(future.result(3), [])
        self.assertEqual(len(self.mcp.calls), 1)
        self.assertFalse(self.service.snapshot(job)["reservation_owned"])

    def test_same_job_concurrent_drain_is_rejected(self):
        job = self.job(); self.queue(job); self.approve(job)
        entered, release = Event(), Event()
        self.mcp.during_call = lambda: (entered.set(), release.wait(3))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.drain, job)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(ToolChatError, "job_busy"): self.service.drain(job)
            finally: release.set()
            future.result(3)
        self.assertEqual(len(self.mcp.calls), 1)

    def test_timeout_keeps_reservation_until_confirmed_operation_end(self):
        job = self.job(); self.queue(job); self.approve(job)
        self.mcp.error = MCPTimeoutError("mcp_wait_timeout", "timeout", operation_id="operation-1")
        with self.assertRaises(MCPTimeoutError): self.service.drain(job)
        self.assertEqual(self.service.snapshot(job)["state"], "UNCERTAIN")
        with self.assertRaisesRegex(ToolChatError, "operation_unresolved"): self.service.reconcile(job)
        self.now = 10000; self.service.sweep()
        self.assertTrue(self.service.snapshot(job)["reservation_owned"])
        self.mcp.operation_state = "SUCCEEDED"; self.service.reconcile(job)
        self.assertFalse(self.service.snapshot(job)["reservation_owned"])
        self.assertEqual(len(self.mcp.calls), 1)

    def test_unknown_exception_after_dispatch_fails_closed(self):
        job = self.job(); self.queue(job); self.approve(job); self.mcp.error = OSError("failure")
        with self.assertRaises(OSError): self.service.drain(job)
        self.assertTrue(self.service.snapshot(job)["unsettled"])
        with self.assertRaises(ToolChatError): self.service.acknowledge_stopped(job)
        self.mcp.state = "STOPPED"; self.service.acknowledge_stopped(job)
        self.assertFalse(self.service.snapshot(job)["reservation_owned"])

    def test_reset_delete_revision_and_explicit_invalidation_hooks(self):
        job = self.job(); self.queue(job); self.approve(job); self.revisions["a"] += 1
        with self.assertRaisesRegex(ToolChatError, "session_changed"): self.service.drain(job)
        job2 = self.job(); self.service.invalidate_session("a")
        self.assertEqual(self.service.snapshot(job2)["state"], "CANCELLED")
        a, b = self.job(), self.job("b"); self.service.invalidate_all()
        self.assertEqual(self.service.snapshot(a)["state"], "CANCELLED")
        self.assertEqual(self.service.snapshot(b)["state"], "CANCELLED")

    def test_cancelled_inference_keeps_lease_until_server_completion(self):
        job = self.job(); self.service.begin_ollama_request(job); self.service.cancel(job)
        self.assertTrue(self.service.snapshot(job)["reservation_owned"])
        self.service.end_ollama_request(job)
        self.assertFalse(self.service.snapshot(job)["reservation_owned"])

    def test_unknown_inference_completion_retains_reservation_and_cannot_use_mcp_recovery(self):
        job = self.job(); self.service.begin_ollama_request(job)
        self.service.end_ollama_request(job, completion_known=False)
        self.assertEqual(self.service.snapshot(job)["state"], "UNCERTAIN")
        with self.assertRaises(ToolChatError): self.service.reconcile(job)
        with self.assertRaises(ToolChatError): self.service.acknowledge_stopped(job)
        self.assertTrue(self.service.snapshot(job)["reservation_owned"])

    def test_separate_approval_active_and_lifetime_limits(self):
        self.service = self.make_service(limits=Limits(active_seconds=10, approval_seconds=100, lifetime_seconds=200))
        job = self.job(); self.queue(job); self.service.drain(job)
        self.now = 50; self.approve(job)
        self.assertEqual(self.service.snapshot(job)["state"], "RUNNING")
        self.now = 61; self.service.sweep()
        self.assertEqual(self.service.snapshot(job)["error_code"], "job_timeout")
        job2 = self.job(); self.queue(job2); self.service.drain(job2); self.now = 162
        self.service.sweep(); self.assertEqual(self.service.snapshot(job2)["state"], "FAILED")

    def test_job_lifetime_finite_even_when_approval_limit_longer(self):
        self.service = self.make_service(limits=Limits(approval_seconds=1000, lifetime_seconds=20))
        job = self.job(); self.queue(job); self.service.drain(job); self.now = 21
        self.service.sweep(); self.assertEqual(self.service.snapshot(job)["state"], "FAILED")

    def test_tool_and_ollama_count_limits(self):
        self.service = self.make_service(limits=Limits(tool_calls=1, ollama_requests=1))
        job = self.job()
        with self.assertRaisesRegex(ToolChatError, "tool_limit"): self.queue(job, 2)
        job = self.job(); self.service.begin_ollama_request(job); self.service.end_ollama_request(job)
        with self.assertRaisesRegex(ToolChatError, "ollama_limit"): self.service.begin_ollama_request(job)

    def test_result_error_and_oversize_fail_without_retaining_grants(self):
        original = self.mcp.call_tool
        for result in ({"is_error": True}, {"is_error": False, "content": "x" * 70000}):
            job = self.job(); self.queue(job); self.approve(job)
            def changed(*args, **kwargs):
                return original(*args, **kwargs) | {"result": result}
            self.mcp.call_tool = changed
            with self.assertRaises(ToolChatError): self.service.drain(job)
            self.assertEqual(self.service.snapshot(job)["state"], "FAILED")
            self.assertFalse(self.service.snapshot(job)["reservation_owned"])
            self.assertEqual(self.service._job(job).grants, [])
            self.mcp.call_tool = original

    def test_invalid_alias_and_bad_arguments_never_execute(self):
        for invalid in [{"name": "unexposed", "arguments": {}}, {"arguments": {}}, {"name": None, "arguments": {}}]:
            job = self.job()
            with self.assertRaises(ToolChatError): self.service.enqueue(job, [invalid])
        job = self.job()
        with self.assertRaises(MCPServiceError): self.service.enqueue(job, [{"name": self.alias(job), "arguments": {"timezone": 1}}])
        self.assertEqual(self.mcp.calls, [])

    def test_catalog_filters_blocked_stopped_unsupported_and_external_refs(self):
        self.mcp.add("time", "shell", {"type": "object"})
        self.assertEqual(len(self.service.catalog.discover()), 1)
        self.mcp.allowed = False; self.assertEqual(self.service.catalog.discover(), {})
        self.mcp.allowed = True; self.mcp.state = "STOPPED"; self.assertEqual(self.service.catalog.discover(), {})
        self.mcp.state = "RUNNING"; self.mcp.contexts["time", "get_current_time"]["tool"]["input_schema"] = {"$ref": "https://invalid/schema"}
        self.assertEqual(self.service.catalog.discover(), {})

    def test_aliases_are_unique_for_two_servers_with_same_tool_name(self):
        self.mcp.add("other", "get_current_time", {"type": "object"})
        catalog = ToolCatalog(self.mcp, {"time": ("time", None), "other": ("time", None)}).discover()
        self.assertEqual(len(catalog), 2); self.assertTrue(all(len(alias) <= 64 for alias in catalog))

    def test_discovery_validates_fresh_schema_instead_of_earlier_listing(self):
        listing = self.mcp.list_tools
        def changed(sid):
            earlier = listing(sid)
            self.mcp.contexts["time", "get_current_time"]["tool"]["input_schema"] = {"type": "not-a-json-type"}
            return earlier
        self.mcp.list_tools = changed
        self.assertEqual(self.service.catalog.discover(), {})

    def test_job_capacity_never_evicts_live_or_unsettled_reservations(self):
        self.service = self.make_service(limits=Limits(max_jobs=1)); job = self.job()
        with self.assertRaisesRegex(ToolChatError, "job_capacity"): self.job("b")
        self.assertIsNone(self.service.reservations.owner("b"))
        self.service.cancel(job); self.job("b")


@unittest.skipUnless(os.name == "nt", "Windows filesystem policy")
class FilesystemPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="local-ai-tool-path-"); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base / "root"; self.root.mkdir()
        self.other = self.base / "root-sibling"; self.other.mkdir()
        self.file = self.root / "note.txt"; self.file.write_text("test fixture", encoding="utf-8")
        self.sub = self.root / "sub"; self.sub.mkdir(); (self.sub / "sub.txt").write_text("fixture")
        self.policy = FilesystemPolicy([self.root], verified=True)

    def test_absolute_existing_file_and_directory_scopes(self):
        file_scope = self.policy.scope(FileScope(str(self.file), "file"))
        self.assertEqual(self.policy.validate("read_file", {"path": str(self.file)}, file_scope), str(self.file))
        directory = self.policy.scope(FileScope(str(self.root), "directory", True))
        self.policy.validate("list_directory", {"path": str(self.sub)}, directory, dispatch=True)
        self.policy.validate("read_file", {"path": str(self.file)}, directory, dispatch=True)

    def test_relative_parent_unc_drive_ads_and_device_paths_rejected(self):
        for path in ["note.txt", "C:note.txt", "\\note.txt", str(self.root / ".." / "root-sibling"),
                     r"\\server\share\note.txt", r"\\?\C:\file", str(self.file)+":secret", str(self.root / "NUL")]:
            with self.subTest(path=path), self.assertRaises(ToolChatError): checked_path(path)

    def test_sibling_prefix_and_other_drive_not_covered(self):
        scope = self.policy.scope(FileScope(str(self.root), "directory", True))
        with self.assertRaises(ToolChatError): self.policy.validate("list_directory", {"path": str(self.other)}, scope)
        other_drive = ("Z" if self.root.drive[0].upper() != "Z" else "Y") + ":\\unlikely-mcp5b-path"
        with self.assertRaises(ToolChatError): self.policy.validate("read_file", {"path": other_drive}, scope)

    def test_file_scope_does_not_authorize_sibling_or_directory(self):
        scope = self.policy.scope(FileScope(str(self.file), "file"))
        with self.assertRaises(ToolChatError): self.policy.validate("list_directory", {"path": str(self.root)}, scope)
        with self.assertRaisesRegex(ToolChatError, "filesystem_outside_grant"):
            self.policy.validate("read_file", {"path": str(self.sub / 'sub.txt')}, scope)

    def test_nonrecursive_scope_excludes_nested_children(self):
        scope = self.policy.scope(FileScope(str(self.root), "directory", False))
        self.policy.validate("read_file", {"path": str(self.file)}, scope)
        with self.assertRaises(ToolChatError): self.policy.validate("read_file", {"path": str(self.sub / 'sub.txt')}, scope)

    def test_narrow_grants_fail_dispatch_instead_of_claiming_atomic_isolation(self):
        for scope in [FileScope(str(self.file), "file"), FileScope(str(self.sub), "directory", True), FileScope(str(self.root), "directory")]:
            approved = self.policy.scope(scope)
            name = "read_file" if scope.kind == "file" else "list_directory"
            with self.assertRaisesRegex(ToolChatError, "filesystem_scope_not_server_enforced"):
                self.policy.validate(name, {"path": scope.path}, approved, dispatch=True)

    def test_multiple_server_roots_cannot_claim_a_single_root_is_server_enforced(self):
        policy = FilesystemPolicy([self.root, self.other], verified=True)
        scope = policy.scope(FileScope(str(self.root), "directory", True))
        with self.assertRaisesRegex(ToolChatError, "filesystem_scope_not_server_enforced"):
            policy.validate("read_file", {"path": str(self.file)}, scope, dispatch=True)

    def test_unknown_roots_and_unsupported_tools_fail_closed(self):
        with self.assertRaisesRegex(ToolChatError, "filesystem_policy_unverified"): FilesystemPolicy([self.root])
        scope = self.policy.scope(FileScope(str(self.root), "directory", True))
        with self.assertRaisesRegex(ToolChatError, "filesystem_tool_denied"): self.policy.validate("write_file", {"path": str(self.file)}, scope)

    def test_deleted_or_replaced_root_rejected(self):
        self.root.rename(self.base / "old-root"); self.root.mkdir()
        with self.assertRaisesRegex(ToolChatError, "filesystem_root_changed"):
            self.policy.scope(FileScope(str(self.root), "directory", True))

    def test_junction_escape_rejected(self):
        link = self.root / "junction"
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(self.other)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaisesRegex(ToolChatError, "filesystem_reparse_point"): checked_path(str(link))
        finally: os.rmdir(link)

    def test_symlink_escape_rejected_when_windows_permits_creation(self):
        link = self.root / "link"
        try: link.symlink_to(self.other, target_is_directory=True)
        except OSError as error: self.skipTest("Windows symlink privilege unavailable: " + str(error.winerror))
        try:
            with self.assertRaisesRegex(ToolChatError, "filesystem_reparse_point"): checked_path(str(link))
        finally: link.unlink()

    def test_filesystem_core_reuses_root_grant_only_within_that_root(self):
        mcp = FakeMCP()
        for name in ("read_file", "list_directory", "write_file"):
            mcp.add("fs", name, {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
        policy = FilesystemPolicy([self.root], verified=True, server_identity=("fs", "config", "connection-1"))
        service = ToolChatService(mcp, {"fs": ("filesystem", policy)}, session_revision=lambda _sid: 0)
        self.addCleanup(service.shutdown)
        job = service.create("a", 0, 1)
        entries = service._job(job).catalog
        self.assertEqual({entry["context"]["tool"]["name"] for entry in entries.values()}, {"read_file", "list_directory"})
        alias = next(alias for alias, entry in entries.items() if entry["context"]["tool"]["name"] == "read_file")
        service.enqueue(job, [{"name": alias, "arguments": {"path": str(self.file)}}] * 2)
        service.drain(job); proposal = service.snapshot(job)["pending"]["proposal_id"]
        with self.assertRaisesRegex(ToolChatError, "filesystem_scope_not_server_enforced"):
            service.approve(job, proposal, 1, allow=True, scope=FileScope(str(self.file), "file"))
        service.approve(job, proposal, 1, allow=True, scope=FileScope(str(self.root), "directory", True))
        self.assertEqual(len(service.drain(job)), 2)

    def test_time_and_filesystem_queue_pause_for_each_new_tool_then_resume_in_order(self):
        mcp = FakeMCP()
        mcp.add("fs", "list_directory", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
        policy = FilesystemPolicy([self.root], verified=True, server_identity=("fs", "config", "connection-1"))
        service = ToolChatService(mcp, {"time": ("time", None), "fs": ("filesystem", policy)}, session_revision=lambda _sid: 0)
        self.addCleanup(service.shutdown)
        job = service.create("a", 0, 1)
        aliases = {entry["context"]["tool"]["name"]: alias for alias, entry in service._job(job).catalog.items()}
        service.enqueue(job, [{"name": aliases["get_current_time"], "arguments": {}},
                              {"name": aliases["list_directory"], "arguments": {"path": str(self.root)}},
                              {"name": aliases["get_current_time"], "arguments": {}}])
        service.drain(job); proposal = service.snapshot(job)["pending"]["proposal_id"]
        service.approve(job, proposal, 1, allow=True)
        self.assertEqual(len(service.drain(job)), 1)
        self.assertEqual(service.snapshot(job)["pending"]["tool_name"], "list_directory")
        service.approve(job, service.snapshot(job)["pending"]["proposal_id"], 1, allow=True,
                        scope=FileScope(str(self.root), "directory", True))
        self.assertEqual(len(service.drain(job)), 2)
        self.assertEqual([name for _sid, name, _args in mcp.calls], ["get_current_time", "list_directory", "get_current_time"])

    def test_reattested_filesystem_policy_required_after_reconnect(self):
        mcp = FakeMCP(); mcp.add("fs", "list_directory", {"type": "object"})
        policy = FilesystemPolicy([self.root], verified=True, server_identity=("fs", "config", "connection-1"))
        catalog = ToolCatalog(mcp, {"fs": ("filesystem", policy)})
        self.assertEqual(len(catalog.discover()), 1)
        mcp.contexts["fs", "list_directory"]["connection_generation"] = "connection-2"
        self.assertEqual(catalog.discover(), {})


class ActualServiceBridgeTests(unittest.TestCase):
    def test_context_reconnect_permission_generation_and_dispatch_callback(self):
        with TemporaryDirectory(prefix="local-ai-tool-bridge-") as directory:
            scenario = Scenario()
            async def listing(**_kwargs):
                return SimpleNamespace(tools=[SimpleNamespace(name="get_current_time", description="test", input_schema={"type": "object"})])
            scenario.list_tools = listing
            mcp = MCPService(session_database_path=Path(directory)/"unused.sqlite3", database_path=Path(directory)/"mcp.sqlite3",
                             approved_configs=[ServerConfig("profile", sys.executable)],
                             runtime_factory=lambda: MCPManager(_client_factory=scenario.client))
            try:
                sid = mcp.create_server("profile", "fixture")["server_id"]
                mcp.start_server(sid); mcp.set_permission(sid, "get_current_time", True)
                old = mcp.inspect_tool(sid, "get_current_time")
                mcp.set_permission(sid, "get_current_time", False); mcp.set_permission(sid, "get_current_time", True)
                current = mcp.inspect_tool(sid, "get_current_time")
                self.assertNotEqual(old["permission_generation"], current["permission_generation"])
                mcp.stop_server(sid); mcp.start_server(sid)
                self.assertNotEqual(current["connection_generation"], mcp.inspect_tool(sid, "get_current_time")["connection_generation"])
                token = mcp.approve_call(sid, "get_current_time", {})["approval_token"]
                def reject(_context, _args): raise ToolChatError("cancelled")
                with self.assertRaisesRegex(ToolChatError, "cancelled"):
                    mcp.call_tool(sid, "get_current_time", {}, token, before_execute=reject)
                mcp.revoke_call_approval(token); self.assertEqual(scenario.calls, [])
                service = ToolChatService(mcp, {sid: ("time", None)}, session_revision=lambda _sid: 0)
                job = service.create("a", 0, 1); alias = service.tools(job)[0]["function"]["name"]
                service.enqueue(job, [{"name": alias, "arguments": {}}]); service.drain(job)
                service.approve(job, service.snapshot(job)["pending"]["proposal_id"], 1, allow=True)
                self.assertEqual(len(service.drain(job)), 1); service.finish(job); service.shutdown()
                self.assertEqual(len(scenario.calls), 1)
            finally: mcp.shutdown()


if __name__ == "__main__":
    unittest.main()

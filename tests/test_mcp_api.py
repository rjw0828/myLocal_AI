"""Flask MCP contracts with a real temporary registry and fake SDK clients."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import create_app
from mcp_manager import MCPManager, ServerConfig
from mcp_manager.errors import MCPServiceError
from mcp_manager.service import MCPService
from sqlite_test_support import isolated_database
from test_mcp_runtime import Scenario, wait_for


HEADERS = {"X-Local-AI-Control": "web-console"}


class MCPApiTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="local-ai-mcp-api-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.session_path = self.directory / "sessions.sqlite3"
        self.mcp_path = self.directory / "mcp.sqlite3"
        self.profile = ServerConfig("echo-profile", sys.executable)
        self.scenario = Scenario()
        self.schema = {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"], "additionalProperties": False}
        self.description = "local echo"

        async def listing(*, cache_mode="use"):
            self.scenario.last_cache_mode = cache_mode
            return SimpleNamespace(tools=[SimpleNamespace(name="echo", description=self.description,
                                                         input_schema=deepcopy(self.schema))])
        self.scenario.list_tools = listing
        self.app = self.new_app()
        self.client = self.app.test_client()
        self.service = self.app.extensions["mcp_service"]
        self.addCleanup(self.scenario.start_gate.set)
        self.addCleanup(self.scenario.call_gate.set)

    def new_app(self, **overrides):
        options = {"database_path": self.session_path, "mcp_database_path": self.mcp_path,
                   "mcp_approved_configs": [self.profile],
                   "mcp_runtime_factory": lambda: MCPManager(stop_timeout=0.05,
                       _client_factory=lambda config, err: self.scenario.client(config, err))}
        options.update(overrides)
        app = create_app(**options)
        app.testing = True
        self.addCleanup(app.extensions["mcp_service"].shutdown)
        return app

    def request(self, path, method="GET", data=None, **kwargs):
        return self.client.open(path, method=method, headers=HEADERS, json=data, **kwargs)

    def register(self):
        response = self.request("/mcp/servers", "POST", {"profile_id": "echo-profile", "name": "테스트 서버"})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["server"]["server_id"]

    def start(self, sid):
        response = self.request(f"/mcp/servers/{sid}/start", "POST", {})
        self.assertEqual(response.status_code, 200, response.json)

    def permit(self, sid, allowed=True):
        response = self.request(f"/mcp/servers/{sid}/tools/echo/permission", "PUT", {"allowed": allowed})
        self.assertEqual(response.status_code, 200, response.json)

    def approve(self, sid, arguments=None):
        response = self.request(f"/mcp/servers/{sid}/tools/echo/approve", "POST",
                                {"arguments": arguments or {"text": "hello"}})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["approval_token"]

    def call(self, sid, token, text="hello"):
        return self.request(f"/mcp/servers/{sid}/tools/echo/call", "POST",
                            {"arguments": {"text": text}, "approval_token": token})

    def ready(self):
        sid = self.register()
        self.start(sid)
        self.permit(sid)
        return sid

    def test_registration_and_list_do_not_start_runtime_or_expose_command(self):
        self.assertFalse(self.mcp_path.exists())
        sid = self.register()
        response = self.request("/mcp/servers")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.service._runtime)
        self.assertEqual(self.scenario.starts, 0)
        row = response.json["servers"][0]
        self.assertEqual(row["server_id"], sid)
        self.assertEqual(row["status"]["state"], "STOPPED")
        self.assertNotIn("executable", row)
        self.assertNotIn("args", row)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_complete_manual_flow_and_session_database_unchanged(self):
        store = self.app.extensions["session_store"]
        store.create("private", explicit=True)
        with store.connection(transaction=False) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = hashlib.sha256(self.session_path.read_bytes()).hexdigest()
        sid = self.register()
        self.start(sid)
        tools = self.request(f"/mcp/servers/{sid}/tools").json["tools"]
        self.assertFalse(tools[0]["allowed"])
        self.assertEqual(self.scenario.last_cache_mode, "refresh")
        denied = self.request(f"/mcp/servers/{sid}/tools/echo/approve", "POST", {"arguments": {"text": "hello"}})
        self.assertEqual(denied.status_code, 403)
        self.permit(sid)
        token = self.approve(sid)
        self.assertEqual(len(self.scenario.calls), 0)
        response = self.call(sid, token)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["result"]["content"][0]["text"], "hello")
        self.assertFalse(response.json["result"]["is_error"])
        self.assertIsInstance(response.json["elapsed_seconds"], float)
        self.assertEqual(self.request(f"/mcp/servers/{sid}/stop", "POST", {}).status_code, 200)
        self.assertEqual(self.scenario.live, 0)
        self.assertEqual(hashlib.sha256(self.session_path.read_bytes()).hexdigest(), before)
        self.assertEqual(store.load("private")["messages"], [])

    def test_one_use_token_and_argument_binding_prevent_replay(self):
        sid = self.ready()
        token = self.approve(sid)
        self.assertEqual(self.call(sid, token, "changed").status_code, 403)
        self.assertEqual(self.call(sid, token).status_code, 403)
        token = self.approve(sid)
        self.assertEqual(self.call(sid, token).status_code, 200)
        self.assertEqual(self.call(sid, token).status_code, 403)
        self.assertEqual(len(self.scenario.calls), 1)

    def test_expired_approval_is_not_executed(self):
        sid = self.ready()
        self.service.clock = lambda: 0
        token = self.approve(sid)
        self.service.clock = lambda: 121
        self.assertEqual(self.call(sid, token).status_code, 403)
        self.assertEqual(len(self.scenario.calls), 0)

    def test_revoke_and_stop_invalidate_outstanding_approvals(self):
        sid = self.ready()
        old = self.approve(sid)
        self.permit(sid, False)
        self.permit(sid)
        self.assertEqual(self.call(sid, old).status_code, 403)
        old = self.approve(sid)
        self.request(f"/mcp/servers/{sid}/stop", "POST", {})
        self.start(sid)
        self.assertEqual(self.call(sid, old).status_code, 403)

    def test_changed_tool_descriptor_revokes_effective_permission(self):
        sid = self.ready()
        token = self.approve(sid)
        self.description = "changed tool behavior description"
        tools = self.request(f"/mcp/servers/{sid}/tools").json["tools"]
        self.assertFalse(tools[0]["allowed"])
        self.assertEqual(self.call(sid, token).status_code, 403)
        self.assertEqual(len(self.scenario.calls), 0)

    def test_restart_restores_registration_and_grants_but_not_runtime_or_tokens(self):
        sid = self.ready()
        token = self.approve(sid)
        self.service.shutdown()
        restarted = self.new_app()
        self.client = restarted.test_client()
        service = restarted.extensions["mcp_service"]
        response = self.request("/mcp/servers")
        self.assertEqual(response.json["servers"][0]["server_id"], sid)
        self.assertEqual(response.json["servers"][0]["status"]["state"], "STOPPED")
        self.assertIsNone(service._runtime)
        self.assertEqual(service.registry.permission(sid, "echo")["allowed"], 1)
        self.assertTrue(self.request(f"/mcp/servers/{sid}/permissions").json["permissions"][0]["allowed"])
        self.assertEqual(self.scenario.starts, 1)
        self.start(sid)
        self.assertTrue(self.request(f"/mcp/servers/{sid}/tools").json["tools"][0]["allowed"])
        self.assertEqual(self.call(sid, token).status_code, 403)

    def test_changed_local_execution_approval_blocks_saved_registration(self):
        sid = self.register()
        self.service.shutdown()
        changed = ServerConfig("echo-profile", sys.executable, ("changed-arguments",))
        self.client = self.new_app(mcp_approved_configs=[changed]).test_client()
        row = self.request("/mcp/servers").json["servers"][0]
        self.assertFalse(row["execution_approved"])
        self.assertEqual(self.request(f"/mcp/servers/{sid}/start", "POST", {}).status_code, 403)
        self.assertEqual(self.scenario.starts, 0)

    def test_delete_requires_stop_and_does_not_recreate_deleted_id(self):
        sid = self.ready()
        self.assertEqual(self.request(f"/mcp/servers/{sid}", "DELETE").status_code, 409)
        self.request(f"/mcp/servers/{sid}/stop", "POST", {})
        self.assertEqual(self.request(f"/mcp/servers/{sid}", "DELETE").status_code, 200)
        self.assertEqual(self.request(f"/mcp/servers/{sid}/start", "POST", {}).status_code, 404)
        self.assertEqual(self.request(f"/mcp/servers/{sid}", "DELETE").status_code, 404)
        self.assertEqual(self.request("/mcp/servers").json["servers"], [])
        self.assertIsNone(self.service.registry.permission(sid, "echo"))

    def test_delete_database_failure_keeps_stopped_registration_and_grant(self):
        sid = self.ready()
        self.request(f"/mcp/servers/{sid}/stop", "POST", {})
        with self.service.registry.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON mcp_servers BEGIN SELECT RAISE(ABORT,'SECRET'); END")
        response = self.request(f"/mcp/servers/{sid}", "DELETE")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.get_data(as_text=True))
        self.assertEqual(self.request(f"/mcp/servers/{sid}").json["server"]["status"]["state"], "STOPPED")
        self.assertEqual(self.service.registry.permission(sid, "echo")["allowed"], 1)
        self.start(sid)

    def test_timeout_exposes_operation_id_and_late_result_without_retry(self):
        sid = self.ready()
        token = self.approve(sid, {"text": "late"})
        self.service._runtime.call_timeout = 0.03
        self.scenario.call_gate.clear()
        response = self.call(sid, token, "late")
        self.assertEqual(response.status_code, 504, response.json)
        op_id = response.json["error"]["operation_id"]
        status = self.request(f"/mcp/servers/{sid}/status").json["server"]["status"]
        self.assertEqual(status["state"], "UNCERTAIN")
        self.assertNotIn("result", status["last_operation"])
        self.assertEqual(self.call(sid, token, "late").status_code, 409)
        self.scenario.call_gate.set()
        wait_for(lambda: self.service._runtime.get_status(sid).state == "RUNNING")
        result = self.request(f"/mcp/servers/{sid}/operations/{op_id}")
        self.assertEqual(result.json["operation"]["result"]["content"][0]["text"], "late")
        self.assertEqual(len(self.scenario.calls), 1)

    def test_revocation_prevents_fetching_late_private_result(self):
        sid = self.ready()
        token = self.approve(sid)
        self.service._runtime.call_timeout = 0.03
        self.scenario.call_gate.clear()
        response = self.call(sid, token)
        op_id = response.json["error"]["operation_id"]
        self.permit(sid, False)
        self.scenario.call_gate.set()
        wait_for(lambda: self.service._runtime.get_status(sid).state == "RUNNING")
        self.assertEqual(self.request(f"/mcp/servers/{sid}/operations/{op_id}").status_code, 403)

    def test_overlapping_actions_are_busy_and_shutdown_cleans_active_call(self):
        sid = self.ready()
        token = self.approve(sid)
        self.scenario.call_gate.clear()
        with ThreadPoolExecutor(1) as pool:
            outstanding = pool.submit(self.call, sid, token)
            self.assertTrue(self.scenario.call_started.wait(2))
            self.assertEqual(self.request(f"/mcp/servers/{sid}/tools").status_code, 429)
            self.service.shutdown()
            self.assertEqual(outstanding.result(3).status_code, 503)
        self.assertEqual(self.scenario.live, 0)
        self.assertFalse(self.service._runtime._host.running)

    def test_explicit_stop_applies_core_policy_to_inflight_http_call(self):
        sid = self.ready()
        token = self.approve(sid)
        self.scenario.call_gate.clear()
        with ThreadPoolExecutor(1) as pool:
            outstanding = pool.submit(self.call, sid, token)
            self.assertTrue(self.scenario.call_started.wait(2))
            stopped = self.request(f"/mcp/servers/{sid}/stop", "POST", {})
            self.assertEqual(stopped.status_code, 200, stopped.json)
            self.assertEqual(stopped.json["server"]["status"]["state"], "STOPPED")
            self.assertEqual(outstanding.result(3).status_code, 503)
        self.assertEqual(self.scenario.live, 0)

    def test_start_failure_and_lost_call_response_are_sanitized(self):
        sid = self.register()
        self.scenario.fail_start = True
        response = self.request(f"/mcp/servers/{sid}/start", "POST", {})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.get_data(as_text=True))
        self.scenario.fail_start = False
        self.request(f"/mcp/servers/{sid}/stop", "POST", {})
        self.start(sid)
        self.permit(sid)
        token = self.approve(sid)
        self.scenario.fail_call = True
        response = self.call(sid, token)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["error"]["code"], "mcp_call_uncertain")
        self.assertNotIn("SECRET", response.get_data(as_text=True))

    def test_http_caller_cannot_approve_arbitrary_executables(self):
        for payload in ({"profile_id": "unknown"}, {"profile_id": "echo-profile", "executable": sys.executable},
                        {"command": "powershell.exe -Command anything"}):
            response = self.request("/mcp/servers", "POST", payload)
            self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.scenario.starts, 0)
        self.assertEqual(self.request("/mcp/profiles").json, {"profiles": [{"profile_id": "echo-profile"}]})

    def test_security_guards_reject_missing_header_cross_origin_host_and_peer(self):
        self.assertEqual(self.client.get("/mcp/servers").status_code, 403)
        for headers, options in ((HEADERS | {"Origin": "https://evil.invalid"}, {}),
                                 (HEADERS | {"Sec-Fetch-Site": "cross-site"}, {}),
                                 (HEADERS, {"base_url": "http://evil.invalid"}),
                                 (HEADERS, {"environ_overrides": {"REMOTE_ADDR": "192.0.2.1"}})):
            self.assertEqual(self.client.get("/mcp/servers", headers=headers, **options).status_code, 403)
        self.assertFalse(self.mcp_path.exists())
        self.assertEqual(self.client.get("/mcp/profiles", headers=HEADERS | {"Origin": "http://localhost"}).status_code, 200)

    def test_invalid_json_fields_arguments_and_request_limit(self):
        self.assertEqual(self.client.post("/mcp/servers", headers=HEADERS, data="{}", content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.post("/mcp/servers", headers=HEADERS, data="{", content_type="application/json").status_code, 400)
        sid = self.ready()
        for arguments in ([], {"text": 1}, {"text": "x" * 65536}):
            response = self.request(f"/mcp/servers/{sid}/tools/echo/approve", "POST", {"arguments": arguments})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.request(f"/mcp/servers/{sid}/tools/echo/permission", "PUT", {"allowed": 1}).status_code, 400)
        self.assertEqual(self.client.post("/chat", json={"message": "x" * (1024 * 1024)}).status_code, 413)
        self.assertEqual(self.client.post("/mcp/servers", headers=HEADERS, data="x" * (1024 * 1024 + 1),
                                         content_type="application/json").status_code, 413)

    def test_external_schema_references_are_rejected_without_network(self):
        sid = self.ready()
        self.schema = {"$ref": "https://example.invalid/schema.json"}
        self.permit(sid)
        response = self.request(f"/mcp/servers/{sid}/tools/echo/approve", "POST", {"arguments": {"text": "hello"}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["error"]["code"], "mcp_tool_schema_unsupported")
        self.assertEqual(len(self.scenario.calls), 0)

    def test_session_database_path_conflict_does_not_touch_session_file(self):
        before = self.session_path.read_bytes()
        self.client = self.new_app(mcp_database_path=self.session_path).test_client()
        response = self.request("/mcp/servers")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "mcp_registry_path_conflict")
        self.assertEqual(self.session_path.read_bytes(), before)

    def test_registry_error_does_not_disable_existing_health(self):
        self.mcp_path.write_bytes(b"invalid sqlite file")
        self.assertEqual(self.request("/mcp/servers").status_code, 503)
        with patch("ai_controller.check_ollama"):
            self.assertEqual(self.client.get("/health").status_code, 200)


class MCPConfigurationTests(unittest.TestCase):
    def test_local_manifest_is_the_only_source_of_execution_approval(self):
        with TemporaryDirectory(prefix="local-ai-mcp-manifest-") as directory:
            path = Path(directory) / "approvals.json"
            path.write_text(json.dumps({"profiles": [{"profile_id": "local", "executable": sys.executable}]}), encoding="utf-8")
            with patch.dict(os.environ, {"LOCAL_AI_MCP_APPROVALS": str(path)}):
                service = MCPService(session_database_path=Path(directory) / "session.sqlite3")
                self.assertEqual(list(service.profiles()), ["local"])
                self.assertIsNone(service._runtime)
                self.assertIsNone(service._registry)
                service.shutdown()
            path.write_text('{"profiles":[{"command":"arbitrary shell"}]}', encoding="utf-8")
            with patch.dict(os.environ, {"LOCAL_AI_MCP_APPROVALS": str(path)}):
                service = MCPService(session_database_path=Path(directory) / "session.sqlite3")
                with self.assertRaises(MCPServiceError):
                    service.profiles()
                service.shutdown()

    def test_entry_point_shutdown_runs_even_when_flask_is_interrupted(self):
        isolated_database(self)
        with patch("mcp_manager.api.MCPService") as service, \
                patch("flask.Flask.run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runpy.run_path(str(Path(__file__).resolve().parents[1] / "app.py"), run_name="__main__")
        service.return_value.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

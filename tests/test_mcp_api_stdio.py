"""Real Flask test client -> independent SQLite -> official SDK stdio fixture."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from app import create_app
from mcp_manager import MCPManager, ServerConfig
from test_mcp_stdio import FIXTURE, ProcessProbe, wait_for


HEADERS = {"X-Local-AI-Control": "web-console"}


@unittest.skipUnless(os.name == "nt", "Actual Windows stdio API integration")
class MCPApiStdioTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="local-ai-mcp2-api-")
        self.root = Path(self.directory.name)
        self.apps, self.probes = [], []
        self.pid_file, self.events_file = self.root / "pid.json", self.root / "events.jsonl"
        self.token = uuid4().hex
        self.addCleanup(self.cleanup_runtime)

    def cleanup_runtime(self):
        errors = []
        for app in reversed(self.apps):
            try:
                app.extensions["mcp_service"].shutdown()
            except Exception as error:
                errors.append(error)
        for probe in self.probes:
            try:
                wait_for(probe.exited)
            except AssertionError as error:
                errors.append(error)
                if getattr(probe, "can_terminate", False):
                    probe.terminate()  # Retained, verified fixture child only.
            finally:
                probe.close()
        self.directory.cleanup()
        self.assertEqual(errors, [], "MCP API fixture process cleanup failed")

    def make_app(self, *extra, **options):
        config = ServerConfig("local-echo-add", sys.executable, (
            str(FIXTURE.resolve()), "--pid-file", str(self.pid_file), "--events-file", str(self.events_file),
            "--token", self.token, *extra,
        ), str(self.root))
        self.config = config
        app = create_app(database_path=self.root / "sessions.sqlite3", mcp_database_path=self.root / "mcp.sqlite3",
                         mcp_approved_configs=[config], **options)
        app.testing = True
        self.apps.append(app)
        self.app = app
        return app

    def request(self, path, method="GET", data=None):
        with self.app.test_client() as client:
            return client.open(path, method=method, json=data, headers=HEADERS)

    def register_start(self):
        response = self.request("/mcp/servers", "POST", {"profile_id": "local-echo-add", "name": "Local fixture"})
        self.assertEqual(response.status_code, 201, response.json)
        sid = response.json["server"]["server_id"]
        self.assertEqual(self.request(f"/mcp/servers/{sid}/start", "POST", {}).status_code, 200)
        self.observe()
        return sid

    def observe(self):
        record = json.loads(self.pid_file.read_text(encoding="utf-8"))
        self.assertEqual(record["token"], self.token)
        child = ProcessProbe(record["pid"], terminate=True,
                             expected_images=[sys.executable, sys._base_executable])
        self.probes.append(child)
        if record["parent_pid"] != os.getpid():
            self.probes.append(ProcessProbe(record["parent_pid"], expected_images=[sys.executable]))
        return child

    def permit_approve(self, sid, tool, arguments):
        grant = self.request(f"/mcp/servers/{sid}/tools/{tool}/permission", "PUT", {"allowed": True})
        self.assertEqual(grant.status_code, 200, grant.json)
        approved = self.request(f"/mcp/servers/{sid}/tools/{tool}/approve", "POST", {"arguments": arguments})
        self.assertEqual(approved.status_code, 200, approved.json)
        return approved.json["approval_token"]

    def test_real_registration_start_discovery_permission_call_stop_delete(self):
        self.make_app()
        session_before = hashlib.sha256((self.root / "sessions.sqlite3").read_bytes()).hexdigest()
        sid = self.register_start()
        tools = self.request(f"/mcp/servers/{sid}/tools")
        self.assertEqual([item["name"] for item in tools.json["tools"]], ["echo", "add"])
        self.assertTrue(all(not item["allowed"] for item in tools.json["tools"]))
        for name, args, expected in (("echo", {"text": "로컬 MCP 테스트"}, "로컬 MCP 테스트"),
                                      ("add", {"a": 19, "b": 23}, "42")):
            token = self.permit_approve(sid, name, args)
            result = self.request(f"/mcp/servers/{sid}/tools/{name}/call", "POST",
                                  {"arguments": args, "approval_token": token})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json["result"]["content"][0]["text"], expected)
            self.assertFalse(result.json["result"]["is_error"])
        self.assertEqual(self.request(f"/mcp/servers/{sid}/stop", "POST", {}).status_code, 200)
        self.assertEqual(self.request(f"/mcp/servers/{sid}", "DELETE").status_code, 200)
        self.assertEqual(self.request("/mcp/servers").json["servers"], [])
        self.assertEqual(hashlib.sha256((self.root / "sessions.sqlite3").read_bytes()).hexdigest(), session_before)

    def test_real_controller_recreation_preserves_grant_without_autostart(self):
        first_app = self.make_app()
        sid = self.register_start()
        old_token = self.permit_approve(sid, "echo", {"text": "hello"})
        first_app.extensions["mcp_service"].shutdown()
        for probe in self.probes:
            wait_for(probe.exited)
        # A fresh Python/Flask process must restore solely from disk, without
        # inheriting this test process's service objects or starting a server.
        manifest = self.root / "approved-profiles.json"
        manifest.write_text(json.dumps({"profiles": [{"profile_id": self.config.server_id,
            "executable": self.config.executable, "args": list(self.config.args), "cwd": self.config.cwd}]}),
            encoding="utf-8")
        code = """
import json, sys
from app import create_app
app = create_app(sys.argv[1], mcp_database_path=sys.argv[2])
try:
    client = app.test_client()
    headers = {'X-Local-AI-Control': 'web-console'}
    servers = client.get('/mcp/servers', headers=headers)
    permissions = client.get('/mcp/servers/' + sys.argv[3] + '/permissions', headers=headers)
    print(json.dumps({'servers_status': servers.status_code, 'servers': servers.json,
        'permissions_status': permissions.status_code, 'permissions': permissions.json,
        'runtime_created': app.extensions['mcp_service']._runtime is not None}))
finally:
    app.extensions['mcp_service'].shutdown()
"""
        process = subprocess.run([sys.executable, "-c", code, str(self.root / "sessions.sqlite3"),
            str(self.root / "mcp.sqlite3"), sid], cwd=str(Path(__file__).resolve().parents[1]),
            env=os.environ | {"LOCAL_AI_MCP_APPROVALS": str(manifest)},
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
        restored = json.loads(process.stdout)
        self.assertEqual(restored["servers_status"], 200)
        self.assertEqual(restored["permissions_status"], 200)
        self.assertEqual(restored["servers"]["servers"][0]["server_id"], sid)
        self.assertEqual(restored["servers"]["servers"][0]["status"]["state"], "STOPPED")
        self.assertTrue(restored["permissions"]["permissions"][0]["allowed"])
        self.assertFalse(restored["runtime_created"])
        self.make_app()
        service = self.app.extensions["mcp_service"]
        row = self.request("/mcp/servers").json["servers"][0]
        self.assertEqual(row["server_id"], sid)
        self.assertEqual(row["status"]["state"], "STOPPED")
        self.assertIsNone(service._runtime)
        self.assertEqual(service.registry.permission(sid, "echo")["allowed"], 1)
        self.assertEqual(len(self.events_file.read_text().splitlines()), 1)  # Only the first boot.
        self.assertEqual(self.request(f"/mcp/servers/{sid}/start", "POST", {}).status_code, 200)
        self.observe()
        self.assertTrue(self.request(f"/mcp/servers/{sid}/tools").json["tools"][0]["allowed"])
        replay = self.request(f"/mcp/servers/{sid}/tools/echo/call", "POST",
                              {"arguments": {"text": "hello"}, "approval_token": old_token})
        self.assertEqual(replay.status_code, 403)

    def test_real_timeout_retains_late_manual_result(self):
        self.make_app("--delay", "0.3")
        sid = self.register_start()
        token = self.permit_approve(sid, "echo", {"text": "late"})
        service = self.app.extensions["mcp_service"]
        service._runtime.call_timeout = 0.03
        result = self.request(f"/mcp/servers/{sid}/tools/echo/call", "POST",
                              {"arguments": {"text": "late"}, "approval_token": token})
        self.assertEqual(result.status_code, 504, result.json)
        op_id = result.json["error"]["operation_id"]
        self.assertEqual(self.request(f"/mcp/servers/{sid}/status").json["server"]["status"]["state"], "UNCERTAIN")
        wait_for(lambda: service._runtime.get_status(sid).state == "RUNNING")
        late = self.request(f"/mcp/servers/{sid}/operations/{op_id}")
        self.assertEqual(late.json["operation"]["result"]["content"][0]["text"], "late")

    def test_real_service_shutdown_during_tool_call_reaps_child(self):
        self.make_app("--delay", "10", mcp_runtime_factory=lambda: MCPManager(stop_timeout=0.05))
        sid = self.register_start()
        token = self.permit_approve(sid, "echo", {"text": "in flight"})
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.request, f"/mcp/servers/{sid}/tools/echo/call", "POST",
                                  {"arguments": {"text": "in flight"}, "approval_token": token})
            wait_for(lambda: len(self.events_file.read_text().splitlines()) >= 2)
            self.app.extensions["mcp_service"].shutdown()
            self.assertEqual(pending.result(5).status_code, 503)
        self.assertTrue(all(probe.exited() for probe in self.probes))

    def test_real_stop_endpoint_during_http_call_reaps_child(self):
        self.make_app("--delay", "10", mcp_runtime_factory=lambda: MCPManager(stop_timeout=0.05))
        sid = self.register_start()
        token = self.permit_approve(sid, "echo", {"text": "in flight"})
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.request, f"/mcp/servers/{sid}/tools/echo/call", "POST",
                                  {"arguments": {"text": "in flight"}, "approval_token": token})
            wait_for(lambda: len(self.events_file.read_text().splitlines()) >= 2)
            stopped = self.request(f"/mcp/servers/{sid}/stop", "POST", {})
            self.assertEqual(stopped.status_code, 200, stopped.json)
            self.assertEqual(stopped.json["server"]["status"]["state"], "STOPPED")
            self.assertEqual(pending.result(5).status_code, 503)
        self.assertTrue(all(probe.exited() for probe in self.probes))


if __name__ == "__main__":
    unittest.main()

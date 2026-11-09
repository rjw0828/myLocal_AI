"""Owner-terminal boundary with real temporary files; no executable is started."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
from mcp_manager import ServerConfig
from mcp_manager.profile_requests import ProfileRequests, approve_request, manifest_lock

HEADERS = {"X-Local-AI-Control": "web-console"}
ROOT = Path(__file__).resolve().parents[1]


class MCPProfileRequestTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="local-ai-profile-review-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manifest = self.root / "mcp_approved_profiles.json"
        self.old = {"profile_id": "existing-time", "executable": sys.executable, "args": [], "cwd": str(self.root)}
        self.manifest.write_text(json.dumps({"profiles": [self.old]}), encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LOCAL_AI_MCP_APPROVALS": str(self.manifest)}).start()
        self.app = self.make_app()
        self.client = self.app.test_client()
        self.service = self.app.extensions["mcp_service"]
        self.queue = self.service.profile_requests()

    def make_app(self, **options):
        app = create_app(database_path=self.root / "sessions.sqlite3", mcp_database_path=self.root / "mcp.sqlite3", **options)
        app.testing = True
        self.addCleanup(app.extensions["mcp_service"].shutdown)
        return app

    def propose(self, **changes):
        body = {"name": "New local server", "transport": "stdio", "executable": sys.executable, "arguments": ["-m", "not_executed"]}
        body.update(changes)
        return self.client.post("/mcp/profile-requests", json=body, headers=HEADERS)

    def record(self):
        response = self.propose()
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["request"]

    def cli(self, record, confirmation):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "approve_mcp_profile.py"), record["request_id"],
                               "--requests-dir", str(self.queue.directory), "--approvals-file", str(self.manifest)],
                              input=confirmation + "\n", text=True, capture_output=True, timeout=15)

    def test_web_proposal_cannot_self_approve_register_or_start_a_process(self):
        before = self.manifest.read_bytes()
        record = self.record()
        self.assertEqual(record["status"], "pending_local_approval")
        sid = record["request_id"]
        self.assertEqual(self.client.get(f"/mcp/profile-requests/{sid}", headers=HEADERS).json["request"]["status"], "pending_local_approval")
        self.assertEqual(self.client.post(f"/mcp/profile-requests/{sid}/approve", json={}, headers=HEADERS).status_code, 404)
        self.assertEqual(self.client.post("/mcp/servers", json={"profile_id": record["profile_id"]}, headers=HEADERS).status_code, 403)
        self.assertEqual(self.propose(approved=True).status_code, 400)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertIsNone(self.service._runtime)

    def test_actual_cli_preserves_profiles_and_registration_remains_stopped(self):
        self.service.profiles()  # Populate the old cache before local approval.
        record = self.record()
        result = self.cli(record, "APPROVE " + record["request_id"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No command is executed", result.stdout)
        saved = json.loads(self.manifest.read_text(encoding="utf-8"))["profiles"]
        self.assertEqual(saved[0], self.old)
        self.assertEqual(len(saved), 2)
        response = self.client.get("/mcp/profile-requests/" + record["request_id"], headers=HEADERS)
        self.assertEqual(response.json["request"]["status"], "approved")
        created = self.client.post("/mcp/servers", headers=HEADERS, json={"profile_id": record["profile_id"], "name": record["name"]})
        self.assertEqual(created.status_code, 201, created.json)
        self.assertEqual(created.json["server"]["status"]["state"], "STOPPED")
        server_id = created.json["server"]["server_id"]
        self.assertEqual(self.client.get(f"/mcp/servers/{server_id}/permissions", headers=HEADERS).json["permissions"], [])
        restarted = self.make_app().test_client().get("/mcp/profiles", headers=HEADERS)
        self.assertIn({"profile_id": record["profile_id"]}, restarted.json["profiles"])
        self.assertIsNone(self.service._runtime)

    def test_cli_rejection_does_not_change_approvals(self):
        before = self.manifest.read_bytes()
        result = self.cli(self.record(), "NO")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_shell_relative_path_non_stdio_and_string_arguments_are_rejected(self):
        shell = self.root / "cmd.exe"
        shell.touch()
        for fields in ({"executable": str(shell)}, {"executable": "python.exe"}, {"transport": "http"}, {"arguments": "-m server"}, {"arguments": [1]}):
            with self.subTest(fields=fields):
                self.assertEqual(self.propose(**fields).status_code, 400)
        self.assertFalse(self.queue.directory.exists())

    def test_existing_request_security_guards_apply_to_new_routes(self):
        self.assertEqual(self.client.post("/mcp/profile-requests", json={}).status_code, 403)
        self.assertEqual(self.client.get("/mcp/profile-requests/invalid", headers=HEADERS).status_code, 404)
        self.assertEqual(self.client.get("/mcp/profile-requests/" + "a" * 32, headers=HEADERS | {"Origin": "https://evil.example"}).status_code, 403)

    def test_tampered_request_is_not_approved_after_terminal_review(self):
        record = self.record()
        before = self.manifest.read_bytes()
        def tamper(_record):
            path = self.queue.path(record["request_id"])
            data = json.loads(path.read_text(encoding="utf-8"))
            data["args"] = ["changed"]
            path.write_text(json.dumps(data), encoding="utf-8")
            return True
        with self.assertRaises(ValueError):
            approve_request(self.queue, record["request_id"], tamper)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_atomic_replace_failure_preserves_original_file_and_cleans_lock(self):
        record = self.record()
        before = self.manifest.read_bytes()
        with patch("mcp_manager.profile_requests.os.replace", side_effect=OSError("test write failure")):
            with self.assertRaises(OSError):
                approve_request(self.queue, record["request_id"], lambda _: True)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse(self.manifest.with_name(self.manifest.name + ".lock").exists())

    def test_owner_lock_prevents_concurrent_lost_updates_and_retry_preserves_both(self):
        first, second = self.record(), self.record()
        with manifest_lock(self.manifest):
            with self.assertRaises(FileExistsError):
                approve_request(self.queue, first["request_id"], lambda _: True)
        for item in (first, second):
            self.assertTrue(approve_request(self.queue, item["request_id"], lambda _: True))
        self.assertEqual(len(json.loads(self.manifest.read_text(encoding="utf-8"))["profiles"]), 3)

    def test_corrupt_existing_manifest_is_never_overwritten(self):
        record = self.record()
        self.manifest.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            approve_request(self.queue, record["request_id"], lambda _: True)
        self.assertEqual(self.manifest.read_text(), "not JSON")

    def test_cancel_removes_only_proposal_and_cannot_remove_approved_profiles(self):
        record = self.record()
        approve_request(self.queue, record["request_id"], lambda _: True)
        before = self.manifest.read_bytes()
        response = self.client.delete("/mcp/profile-requests/" + record["request_id"], headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.queue.path(record["request_id"]).exists())
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_expired_requests_cannot_be_approved(self):
        record = self.record()
        path = self.queue.path(record["request_id"])
        data = json.loads(path.read_text(encoding="utf-8")); data["created_at"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self.cli(record, "APPROVE " + record["request_id"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(json.loads(self.manifest.read_text())["profiles"]), 1)

    def test_fixed_python_profiles_cannot_be_modified_by_web(self):
        client = self.make_app(mcp_approved_configs=[ServerConfig("fixed", sys.executable)]).test_client()
        response = client.post("/mcp/profile-requests", json={"name": "x", "transport": "stdio", "executable": sys.executable, "arguments": []}, headers=HEADERS)
        self.assertEqual(response.status_code, 403)

    def test_default_manifest_needs_no_environment_variable(self):
        with patch.dict(os.environ):
            os.environ.pop("LOCAL_AI_MCP_APPROVALS", None)
            service = self.make_app().extensions["mcp_service"]
            self.assertEqual(service.profile_requests().manifest, self.manifest)
            self.assertIn("existing-time", service.profiles())

    def test_same_profile_is_idempotent_but_conflicting_profile_is_not_overwritten(self):
        record = self.record()
        approve_request(self.queue, record["request_id"], lambda _: True)
        self.assertTrue(approve_request(self.queue, record["request_id"], lambda _: True))
        document = json.loads(self.manifest.read_text())
        self.assertEqual(len(document["profiles"]), 2)
        document["profiles"][-1]["args"] = ["other"]
        self.manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError):
            approve_request(self.queue, record["request_id"], lambda _: True)

    def test_explicit_refresh_can_discover_terminal_approval_after_cancelled_proposal(self):
        self.service.profiles()
        record = self.record()
        approve_request(self.queue, record["request_id"], lambda _: True)
        self.client.delete("/mcp/profile-requests/" + record["request_id"], headers=HEADERS)
        profiles = self.client.get("/mcp/profiles", headers=HEADERS).json["profiles"]
        self.assertIn({"profile_id": record["profile_id"]}, profiles)

    def test_request_queue_is_bounded_and_cancel_recovers_a_slot(self):
        records = [self.record() for _ in range(64)]
        self.assertEqual(self.propose().status_code, 429)
        self.client.delete("/mcp/profile-requests/" + records[0]["request_id"], headers=HEADERS)
        self.assertEqual(self.propose().status_code, 201)


if __name__ == "__main__":
    unittest.main()

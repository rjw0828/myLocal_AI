"""Run the real Windows launcher against a temporary, non-server Python stub."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app


REPOSITORY = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows batch launcher integration")
class MCPStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="local-ai-launcher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project with spaces"
        scripts = self.root / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(REPOSITORY / "start_app.bat", self.root / "start_app.bat")
        # Reuse only the existing venv launcher/config; no environment installation.
        shutil.copy2(REPOSITORY / ".venv" / "Scripts" / "python.exe", scripts / "python.exe")
        shutil.copy2(REPOSITORY / ".venv" / "pyvenv.cfg", self.root / ".venv" / "pyvenv.cfg")
        (self.root / "app.py").write_text(
            "import json, os, sys\n"
            "print('LAUNCH_RESULT=' + json.dumps({\n"
            "    'approval': os.environ.get('LOCAL_AI_MCP_APPROVALS'),\n"
            "    'cwd': os.getcwd(),\n"
            "}))\n"
            "sys.exit(int(os.environ.get('LOCAL_AI_LAUNCH_TEST_EXIT', '0')))\n",
            encoding="utf-8",
        )
        self.default = self.root / "data" / "mcp_approved_profiles.json"

    def write_profile(self, path, name):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"profiles": [{"profile_id": name, "executable": sys.executable,
                                                  "args": [], "cwd": str(self.root)}]}), encoding="utf-8")

    def launch(self, approval=None, exit_code=0):
        environment = os.environ.copy()
        environment.pop("LOCAL_AI_MCP_APPROVALS", None)
        environment["LOCAL_AI_LAUNCH_TEST_EXIT"] = str(exit_code)
        if approval is not None:
            environment["LOCAL_AI_MCP_APPROVALS"] = str(approval)
        completed = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", str(self.root / "start_app.bat")],
            cwd=self.root.parent, env=environment, input="\n\n", text=True,
            capture_output=True, timeout=15,
        )
        lines = [line for line in completed.stdout.splitlines() if line.startswith("LAUNCH_RESULT=")]
        self.assertEqual(completed.returncode, exit_code, completed.stdout + completed.stderr)
        self.assertEqual(len(lines), 1, completed.stdout + completed.stderr)
        return json.loads(lines[0].partition("=")[2])

    def assert_flask_profiles(self, approval, expected):
        with patch.dict(os.environ, {"LOCAL_AI_MCP_APPROVALS": str(approval)}):
            app = create_app(database_path=self.root / "test_sessions.sqlite3",
                             mcp_database_path=self.root / "test_mcp.sqlite3")
            service = app.extensions["mcp_service"]
            try:
                response = app.test_client().get("/mcp/profiles", headers={"X-Local-AI-Control": "web-console"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["profiles"], [{"profile_id": expected}])
                self.assertIsNone(service._runtime)
                self.assertIsNone(service._registry)
                self.assertFalse(service.database_path.exists())
            finally:
                service.shutdown()

    def test_local_file_fallback_reaches_flask_profiles_without_starting_servers(self):
        self.write_profile(self.default, "fallback-test")
        result = self.launch()
        self.assertEqual(Path(result["approval"]), self.default)
        self.assert_flask_profiles(result["approval"], "fallback-test")

    def test_explicit_environment_takes_precedence_over_local_file(self):
        self.write_profile(self.default, "fallback-test")
        custom = self.root / "custom settings" / "approved.json"
        self.write_profile(custom, "explicit-test")
        result = self.launch(custom)
        self.assertEqual(result["approval"], str(custom))
        self.assert_flask_profiles(result["approval"], "explicit-test")

    def test_no_file_and_no_environment_starts_without_an_approval_path(self):
        self.assertIsNone(self.launch()["approval"])

    def test_missing_explicit_file_does_not_silently_fall_back(self):
        self.write_profile(self.default, "fallback-test")
        missing = self.root / "missing.json"
        self.assertEqual(self.launch(missing)["approval"], str(missing))

    def test_launcher_preserves_project_directory_and_python_exit_code(self):
        result = self.launch(exit_code=7)
        self.assertEqual(Path(result["cwd"]), self.root)


if __name__ == "__main__":
    unittest.main()

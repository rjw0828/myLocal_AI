"""Actual temporary SQLite registry transactions, never the Session database."""
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mcp_manager.errors import MCPServiceError
from mcp_manager.registry import MCPRegistry


class MCPRegistryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="local-ai-mcp-registry-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "mcp.sqlite3"
        self.protected = Path(directory.name) / "sessions.sqlite3"
        with closing(sqlite3.connect(self.protected)) as db, db:
            db.execute("CREATE TABLE private_session(content TEXT)")
            db.execute("INSERT INTO private_session VALUES ('private conversation')")
        self.before = hashlib.sha256(self.protected.read_bytes()).hexdigest()
        self.registry = MCPRegistry(self.path, protected_path=self.protected)
        self.config = {"executable": "approved.exe", "args": ["server.py"], "cwd": "approved"}

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.protected.read_bytes()).hexdigest(), self.before)

    def test_registration_and_permissions_survive_reopen_without_runtime_state(self):
        row = self.registry.create("local", "로컬 서버", self.config)
        self.registry.set_permission(row["server_id"], "echo", "schema-hash", True)
        reopened = MCPRegistry(self.path, protected_path=self.protected)
        self.assertEqual(reopened.get(row["server_id"])["config"], self.config)
        self.assertEqual(reopened.permission(row["server_id"], "echo")["allowed"], 1)
        with reopened.connection() as db:
            columns = [item[1] for item in db.execute("PRAGMA table_info(mcp_servers)")]
            self.assertNotIn("state", columns)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_default_permission_is_absent_and_revoke_is_persistent(self):
        sid = self.registry.create("local", "local", self.config)["server_id"]
        self.assertIsNone(self.registry.permission(sid, "echo"))
        self.registry.set_permission(sid, "echo", "hash", True)
        self.registry.set_permission(sid, "echo", "", False)
        self.assertEqual(self.registry.permission(sid, "echo")["allowed"], 0)

    def test_duplicate_profile_or_identical_execution_is_rejected(self):
        self.registry.create("local", "local", self.config)
        for profile in ("local", "alias"):
            with self.assertRaises(MCPServiceError) as error:
                self.registry.create(profile, "duplicate", self.config)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.registry.list_servers()), 1)

    def test_delete_cascades_only_target_and_missing_id_stays_missing(self):
        first = self.registry.create("a", "A", self.config)["server_id"]
        second = self.registry.create("b", "B", self.config | {"args": ["different"]})["server_id"]
        self.registry.set_permission(first, "echo", "a", True)
        self.registry.set_permission(second, "echo", "b", True)
        self.registry.delete(first)
        self.assertIsNone(self.registry.permission(first, "echo"))
        self.assertEqual(self.registry.permission(second, "echo")["schema_hash"], "b")
        with self.assertRaises(MCPServiceError) as error:
            self.registry.delete(first)
        self.assertEqual(error.exception.status, 404)

    def test_sql_failure_rolls_back_delete_and_permissions(self):
        sid = self.registry.create("a", "A", self.config)["server_id"]
        self.registry.set_permission(sid, "echo", "hash", True)
        with self.registry.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON mcp_servers "
                       "BEGIN SELECT RAISE(ABORT, 'SECRET simulated failure'); END")
        with self.assertRaises(MCPServiceError) as error:
            self.registry.delete(sid)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(self.registry.get(sid)["name"], "A")
        self.assertEqual(self.registry.permission(sid, "echo")["allowed"], 1)

    def test_session_file_and_sidecars_cannot_be_registry(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.assertRaises(MCPServiceError) as error:
                MCPRegistry(str(self.protected) + suffix, protected_path=self.protected)
            self.assertEqual(error.exception.code, "mcp_registry_path_conflict")
        if __import__("os").name == "nt":
            with self.assertRaises(MCPServiceError):
                MCPRegistry(str(self.protected).upper() + "-WAL", protected_path=self.protected)

    def test_session_hardlink_cannot_be_registry(self):
        link = self.protected.with_name("session-link.sqlite3")
        try:
            link.hardlink_to(self.protected)
        except OSError:
            self.skipTest("filesystem does not support hard links")
        with self.assertRaises(MCPServiceError):
            MCPRegistry(link, protected_path=self.protected)

    def test_unrelated_database_is_not_repurposed(self):
        other = self.path.with_name("other.sqlite3")
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        before = other.read_bytes()
        with self.assertRaises(MCPServiceError):
            MCPRegistry(other, protected_path=self.protected)
        self.assertEqual(other.read_bytes(), before)

    def test_missing_live_registry_is_not_silently_recreated(self):
        row = self.registry.create("a", "A", self.config)
        self.path.unlink()
        with self.assertRaises(MCPServiceError):
            self.registry.get(row["server_id"])
        self.assertFalse(self.path.exists())

    def test_busy_error_is_bounded_and_sanitized(self):
        other = MCPRegistry(self.path, protected_path=self.protected, timeout=0.01)
        with self.registry.connection(write=True):
            with self.assertRaises(MCPServiceError) as error:
                other.create("a", "A", self.config)
        self.assertEqual(error.exception.code, "mcp_registry_busy")


if __name__ == "__main__":
    unittest.main()

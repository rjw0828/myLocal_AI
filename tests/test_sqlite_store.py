"""Real isolated SQLite files; model calls mocked, no personal DB or audio."""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from app import create_app
from sqlite_test_support import isolated_database
from storage.sqlite_store import (SQLiteStore, StorageBusy, StorageConflict,
                                 StorageError, StorageUnavailable, DeletedSession)


class SQLitePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.path = isolated_database(self)
        self.app = create_app()
        self.client = self.app.test_client()
        self.store = self.app.extensions["session_store"]
        mocked = patch("ai_controller.ask_ollama", return_value={
            "message": {"role": "assistant", "content": "stored answer"}})
        self.ask = mocked.start()
        self.addCleanup(mocked.stop)

    def create(self, client=None):
        response = (client or self.client).post("/sessions")
        self.assertEqual(response.status_code, 201)
        return response.json

    def chat(self, sid, language="ko", message="첫 질문 😀"):
        return self.client.post("/chat", json={"session_id": sid, "message": message,
                                                "response_language": language})

    def history(self, sid, client=None):
        return (client or self.client).get(f"/sessions/{sid}/history")

    def test_restart_restores_history_metadata_and_prompt_without_transient_instructions(self):
        a, b = self.create(), self.create()
        for item, language in ((a, "en"), (b, "ko")):
            self.assertEqual(self.chat(item["session_id"], language).status_code, 200)
        expected = [self.history(item["session_id"]).json for item in (a, b)]
        restarted = create_app(self.path).test_client()
        self.assertEqual([self.history(item["session_id"], restarted).json for item in (a, b)], expected)
        self.assertEqual(len(restarted.get("/sessions").json["sessions"]), 2)
        restarted.post("/chat", json={"session_id": a["session_id"], "message": "next"})
        payload = self.ask.call_args.args[0]
        self.assertIn("English", payload[0]["content"])
        self.assertEqual(payload[1]["content"], "첫 질문 😀")
        self.assertNotIn("[Response requirements]", str(expected))

    def test_independent_new_database_starts_empty(self):
        self.create()
        other = create_app(self.path.with_name("other.sqlite3")).test_client()
        self.assertEqual(other.get("/sessions").json, {"sessions": []})

    def test_restart_reset_persists_and_preserves_other_session_and_number(self):
        a, b = self.create(), self.create()
        for item in (a, b):
            self.chat(item["session_id"], "en")
        before_b = self.history(b["session_id"]).json
        self.assertEqual(self.client.post(f"/sessions/{a['session_id']}/reset").status_code, 200)
        restarted = create_app(self.path).test_client()
        self.assertEqual(self.history(a["session_id"], restarted).json, {**a, "messages": []})
        self.assertEqual(self.history(b["session_id"], restarted).json, before_b)
        row = self.store.load(a["session_id"])
        self.assertIsNone(row["system_prompt"])
        self.assertIsNone(row["last_response_language"])
        self.assertEqual(row["revision"], 2)

    def test_delete_cascades_and_tombstone_survives_restart_without_retaining_text(self):
        a, b = self.create(), self.create()
        for item in (a, b):
            self.chat(item["session_id"])
        before_b = self.history(b["session_id"]).json
        self.assertEqual(self.client.delete(f"/sessions/{a['session_id']}").status_code, 200)
        restarted = create_app(self.path).test_client()
        for response in (
                restarted.post("/chat", json={"session_id": a["session_id"], "message": "late"}),
                restarted.get(f"/sessions/{a['session_id']}/history"),
                restarted.post(f"/sessions/{a['session_id']}/reset")):
            self.assertEqual(response.status_code, 410)
        self.assertEqual(restarted.delete(f"/sessions/{a['session_id']}").status_code, 404)
        self.assertEqual(self.history(b["session_id"], restarted).json, before_b)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE session_id=?", (a["session_id"],)).fetchone()[0], 0)
            tombstone = dict(db.execute("SELECT * FROM deleted_sessions").fetchone())
            self.assertEqual(set(tombstone), {"session_id", "deleted_at"})

    def test_persistent_cap_survives_restart_and_delete_frees_slot_without_number_reuse(self):
        items = [self.create() for _ in range(60)]
        restarted = create_app(self.path).test_client()
        self.assertEqual(restarted.post("/sessions").status_code, 429)
        self.assertEqual(restarted.post("/chat", json={"session_id": "new", "message": "no"}).status_code, 429)
        self.assertEqual(restarted.post("/chat", json={"session_id": items[0]["session_id"], "message": "yes"}).status_code, 200)
        restarted.delete(f"/sessions/{items[0]['session_id']}")
        self.assertEqual(self.create(restarted)["number"], 61)

    def test_atomic_pair_and_metadata_rollback_when_second_insert_fails(self):
        a = self.create()
        self.chat(a["session_id"], "ko")
        before = self.store.load(a["session_id"])
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER reject_answer BEFORE INSERT ON messages WHEN NEW.role='assistant' "
                       "BEGIN SELECT RAISE(ABORT, 'private SQL details'); END")
        response = self.chat(a["session_id"], "en", "must roll back")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"]["code"], "storage_error")
        self.assertNotIn("private SQL", response.get_data(as_text=True))
        self.assertEqual(self.store.load(a["session_id"]), before)
        self.assertEqual(create_app(self.path).test_client().get(f"/sessions/{a['session_id']}/history").json,
                         self.history(a["session_id"]).json)

    def test_failed_upstream_does_not_change_persisted_history(self):
        a = self.create()
        self.chat(a["session_id"])
        before = self.store.load(a["session_id"])
        self.ask.side_effect = TimeoutError()
        self.assertEqual(self.chat(a["session_id"]).status_code, 504)
        self.assertEqual(self.store.load(a["session_id"]), before)
        self.assertEqual(self.chat("failed-implicit").status_code, 504)
        self.assertIsNone(self.store.load("failed-implicit"))

    def test_stale_revision_cannot_commit_after_reset_or_delete(self):
        for delete in (False, True):
            a = self.create()
            sid = a["session_id"]
            old = self.store.load(sid)
            if delete:
                self.store.delete(sid, old["revision"])
            else:
                self.store.reset(sid, old["revision"])
            with self.assertRaises(DeletedSession if delete else StorageConflict):
                self.store.commit_turn(sid, old["revision"], user="late", answer="late",
                                       title="late", system_prompt="system", language="en")
            if not delete:
                self.assertEqual(self.store.load(sid)["messages"], [])

    def test_inference_does_not_hold_sqlite_transaction(self):
        a = self.create()
        def inference(_messages):
            # Independent writer would time out if Chat held a transaction.
            other = SQLiteStore(self.path, timeout=0.05)
            other.create("independent", explicit=True)
            return {"message": {"role": "assistant", "content": "answer"}}
        self.ask.side_effect = inference
        self.assertEqual(self.chat(a["session_id"]).status_code, 200)
        self.assertIsNotNone(self.store.load("independent"))

    def test_real_busy_error_is_bounded_and_has_no_ram_fallback(self):
        a = self.create()
        self.store.timeout = 0.05
        with self.store.connection(write=True):
            result = self.chat(a["session_id"])
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json["error"]["code"], "storage_busy")
        self.assertEqual(self.history(a["session_id"]).json["messages"], [])

    def test_reset_and_delete_failure_do_not_confirm_or_change_history(self):
        a = self.create()
        self.chat(a["session_id"])
        before = self.history(a["session_id"]).json
        for operation in ("reset", "delete"):
            with patch.object(self.store, operation, side_effect=StorageUnavailable("Session storage is unavailable.")):
                response = (self.client.post(f"/sessions/{a['session_id']}/reset") if operation == "reset"
                            else self.client.delete(f"/sessions/{a['session_id']}"))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(self.history(a["session_id"]).json, before)

    def test_corrupt_and_future_schema_fail_closed_without_replacing_original(self):
        for content in (b"not a database", None):
            path = self.path.with_name("invalid-" + str(content is None) + ".sqlite3")
            if content is not None:
                path.write_bytes(content)
            else:
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA user_version=99")
            before = path.read_bytes()
            with self.assertRaises(StorageError):
                create_app(path)
            # Opening WAL can legitimately change SQLite header flags; the
            # unknown schema version and existing data must remain untouched.
            if content is not None:
                self.assertEqual(path.read_bytes(), before)
            else:
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
                    self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_unknown_existing_schema_is_not_repurposed(self):
        path = self.path.with_name("unrelated.sqlite3")
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE personal(value TEXT)")
        with self.assertRaises(StorageError):
            SQLiteStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchone()[0], "personal")

    def test_removed_live_database_is_not_silently_recreated(self):
        self.path.unlink()
        with self.assertRaises(StorageUnavailable):
            self.store.list_sessions()
        self.assertFalse(self.path.exists())

    def test_sql_parameters_preserve_unicode_and_sql_looking_text(self):
        sid = "quote'; DROP TABLE sessions;--"
        text = "<script>alert('x')</script> 😀 한국어"
        self.assertEqual(self.chat(sid, message=text).status_code, 200)
        self.assertEqual(self.history(sid).json["messages"][0]["content"], text)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_backup_restore_retains_history_number_and_deleted_ids(self):
        a, b = self.create(), self.create()
        self.chat(a["session_id"], "en")
        self.client.delete(f"/sessions/{b['session_id']}")
        backup = self.path.with_name("backup.sqlite3")
        self.store.backup(backup)
        restored = create_app(backup).test_client()
        self.assertEqual(self.history(a["session_id"], restored).json, self.history(a["session_id"]).json)
        self.assertEqual(self.create(restored)["number"], 3)
        self.assertEqual(restored.post("/chat", json={"session_id": b["session_id"], "message": "late"}).status_code, 410)
        with self.assertRaises(StorageError):
            self.store.backup(backup)

    def test_real_subprocess_restart_reads_committed_data(self):
        a = self.create()
        self.chat(a["session_id"])
        code = ("import sys; from app import create_app; c=create_app(sys.argv[1]).test_client(); "
                "d=c.get('/sessions/'+sys.argv[2]+'/history').json; "
                "assert len(d['messages'])==2 and d['messages'][1]['language']=='ko'; print('RESTORED')")
        result = subprocess.run([sys.executable, "-c", code, str(self.path), a["session_id"]],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "RESTORED")

    def test_abrupt_exit_rolls_back_uncommitted_turn(self):
        a = self.create()
        self.chat(a["session_id"])
        before = self.store.load(a["session_id"])
        code = ("import sqlite3,os,sys; d=sqlite3.connect(sys.argv[1]); d.execute('BEGIN IMMEDIATE'); "
                "d.execute('UPDATE sessions SET title=? WHERE session_id=?',('partial',sys.argv[2])); "
                "d.execute('INSERT INTO messages(session_id,ordinal,role,content) VALUES (?,?,?,?)',"
                "(sys.argv[2],2,'user','partial')); os._exit(23)")
        result = subprocess.run([sys.executable, "-c", code, str(self.path), a["session_id"]], timeout=15)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(SQLiteStore(self.path).load(a["session_id"]), before)

    def test_foreign_keys_and_wal_enabled_and_connections_released(self):
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
        renamed = self.path.with_name("renamed.sqlite3")
        self.path.rename(renamed)
        renamed.rename(self.path)

    def test_storage_unavailable_does_not_disable_independent_health(self):
        with patch.object(self.store, "list_sessions", side_effect=StorageUnavailable("Session storage is unavailable.")):
            self.assertEqual(self.client.get("/sessions").status_code, 503)
            with patch("ai_controller.check_ollama"):
                self.assertEqual(self.client.get("/health").status_code, 200)

    def test_readonly_full_and_io_errors_are_sanitized_without_ram_fallback(self):
        # Fault injection only: do not fill the user's disk or change ACLs.
        for code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR,
                     sqlite3.SQLITE_CANTOPEN):
            with self.subTest(code=code):
                failure = sqlite3.OperationalError("private path and transcript")
                failure.sqlite_errorcode = code
                with patch("storage.sqlite_store.sqlite3.connect", side_effect=failure):
                    response = self.client.post("/sessions")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["error"]["code"], "storage_unavailable")
                self.assertNotIn("private", response.get_data(as_text=True))
                self.assertEqual(self.client.get("/sessions").json, {"sessions": []})

    def test_startup_does_not_load_all_history_and_idle_requests_release_history(self):
        a = self.create()
        self.chat(a["session_id"])
        with patch.object(SQLiteStore, "load", side_effect=AssertionError("eager history read")):
            restarted = create_app(self.path)
            self.assertEqual(len(restarted.test_client().get("/sessions").json["sessions"]), 1)
        # Capture runtime state using the same public factory seam as existing
        # concurrency tests. Only metadata and operation locks stay resident.
        from app import Session
        captured = []
        def capture(**kwargs):
            session = Session(**kwargs)
            captured.append(session)
            return session
        with patch("app.Session", side_effect=capture):
            restarted = create_app(self.path)
        client = restarted.test_client()
        self.assertEqual(len(self.history(a["session_id"], client).json["messages"]), 2)
        self.assertEqual(captured[0].messages, [])
        self.assertEqual(captured[0].answer_languages, [])

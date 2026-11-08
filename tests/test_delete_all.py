"""Delete All uses real temporary SQLite files; inference is mocked."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
import unittest
from unittest.mock import patch

from app import Session, create_app
from sqlite_test_support import isolated_database


class DeleteAllTests(unittest.TestCase):
    def setUp(self):
        self.path = isolated_database(self)
        self.app = create_app(self.path)
        self.client = self.app.test_client()
        self.store = self.app.extensions["session_store"]
        mocked = patch("ai_controller.ask_ollama", return_value={
            "message": {"role": "assistant", "content": "answer"}})
        self.ask = mocked.start()
        self.addCleanup(mocked.stop)

    def create(self):
        response = self.client.post("/sessions")
        self.assertEqual(response.status_code, 201)
        return response.json

    def chat(self, sid, language="ko"):
        return self.client.post("/chat", json={"session_id": sid, "message": "question", "response_language": language})

    def test_deletes_histories_metadata_keeps_all_tombstones_and_restarts_numbering(self):
        a, b, old = self.create(), self.create(), self.create()
        for item in (a, b):
            self.assertEqual(self.chat(item["session_id"], "en").status_code, 200)
        self.client.delete(f"/sessions/{old['session_id']}")
        response = self.client.delete("/sessions")
        self.assertEqual(response.json, {"status": "deleted_all", "deleted_count": 2,
                                         "deleted_session_ids": [a["session_id"], b["session_id"]]})
        self.assertEqual(self.client.get("/sessions").json, {"sessions": []})
        with self.store.connection() as db:
            for table in ("sessions", "messages"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM deleted_sessions").fetchone()[0], 3)
        restarted = create_app(self.path).test_client()
        for item in (a, b, old):
            self.assertEqual(restarted.post("/chat", json={"session_id": item["session_id"], "message": "late"}).status_code, 410)
            self.assertEqual(restarted.get(f"/sessions/{item['session_id']}/history").status_code, 410)
        # A genuinely new Python/Flask process must observe the sequence reset.
        code = ("import sys,json; from app import create_app; "
                "c=create_app(sys.argv[1]).test_client(); r=c.post('/sessions'); "
                "assert r.status_code==201; print(json.dumps(r.json))")
        process = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                 text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        first = json.loads(process.stdout)
        self.assertEqual(first["number"], 1)
        self.assertNotIn(first["session_id"], [item["session_id"] for item in (a, b, old)])
        # Normal restart and individual Delete continue the counter.
        again = create_app(self.path).test_client()
        again.delete(f"/sessions/{first['session_id']}")
        self.assertEqual(again.post("/sessions").json["number"], 2)

    def test_empty_bulk_success_resets_counter_but_does_not_create_session(self):
        a = self.create()
        self.client.delete(f"/sessions/{a['session_id']}")
        self.assertEqual(self.client.delete("/sessions").json["deleted_count"], 0)
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.create()["number"], 1)

    def test_sixty_sessions_are_freed_and_next_number_is_one(self):
        ids = [self.create()["session_id"] for _ in range(60)]
        self.assertEqual(self.client.post("/sessions").status_code, 429)
        self.assertEqual(self.client.delete("/sessions").json["deleted_count"], 60)
        new = self.create()
        self.assertEqual(new["number"], 1)
        self.assertNotIn(new["session_id"], ids)

    def test_mid_delete_sql_failure_rolls_back_rows_tombstones_and_ui_runtime_flags(self):
        a, b = self.create(), self.create()
        for item in (a, b):
            self.chat(item["session_id"])
        before = [self.store.load(item["session_id"]) for item in (a, b)]
        with self.store.connection(write=True) as db:
            # The first row may already have cascaded before the second fails.
            db.execute("CREATE TRIGGER fail_bulk BEFORE DELETE ON sessions WHEN OLD.session_id='" + b["session_id"] +
                       "' BEGIN SELECT RAISE(ABORT, 'private SQL failure'); END")
        response = self.client.delete("/sessions")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"]["code"], "storage_error")
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertEqual([self.store.load(item["session_id"]) for item in (a, b)], before)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deleted_sessions").fetchone()[0], 0)
        self.assertEqual(self.create()["number"], 3)
        self.assertEqual(self.chat(a["session_id"]).status_code, 200)
        self.assertEqual(create_app(self.path).test_client().get("/sessions").status_code, 200)

    def test_failure_after_sequence_reset_rolls_back_entire_transaction(self):
        a = self.create()
        self.chat(a["session_id"])
        before = self.store.load(a["session_id"])
        original = self.store.connection
        @contextmanager
        def failing_connection(**options):
            with original(**options) as db:
                class Proxy:
                    def execute(self, sql, *args):
                        result = db.execute(sql, *args)
                        if sql.startswith("DELETE FROM sqlite_sequence"):
                            raise sqlite3.OperationalError("injected failure after sequence write")
                        return result
                yield Proxy()
        with patch.object(self.store, "connection", failing_connection):
            self.assertEqual(self.client.delete("/sessions").status_code, 500)
        self.assertEqual(self.store.load(a["session_id"]), before)
        self.assertEqual(self.create()["number"], 2)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deleted_sessions").fetchone()[0], 0)

    def test_running_chat_finishes_before_bulk_queued_and_new_operations_are_rejected(self):
        for fail_chat in (False, True):
            with self.subTest(fail_chat=fail_chat):
                captured = []
                def capture(**kwargs):
                    state = Session(**kwargs)
                    captured.append(state)
                    return state
                with patch("app.Session", side_effect=capture):
                    a = self.create()
                b = self.create()
                state = captured[0]
                entered, release, queued, bulk_waiting = Event(), Event(), Event(), Event()
                base_lock = state.lock
                class ObservedLock:
                    def __enter__(self):
                        if state.deleting:
                            bulk_waiting.set()
                        elif entered.is_set():
                            queued.set()
                        base_lock.acquire()
                    def __exit__(self, *_args):
                        base_lock.release()
                state.lock = ObservedLock()
                def delayed(_messages):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release inference")
                    if fail_chat:
                        raise TimeoutError()
                    return {"message": {"role": "assistant", "content": "completed"}}
                def request(method, url, **kwargs):
                    with self.app.test_client() as client:
                        return getattr(client, method)(url, **kwargs)
                with patch("ai_controller.ask_ollama", side_effect=delayed):
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        chat = pool.submit(request, "post", "/chat", json={"session_id": a["session_id"], "message": "in flight"})
                        try:
                            self.assertTrue(entered.wait(5))
                            history = pool.submit(request, "get", f"/sessions/{a['session_id']}/history")
                            self.assertTrue(queued.wait(5))
                            bulk = pool.submit(request, "delete", "/sessions")
                            self.assertTrue(bulk_waiting.wait(5))
                            self.assertFalse(bulk.done())
                            for method, url, kwargs in (
                                    ("post", "/chat", {"json": {"session_id": a["session_id"], "message": "late"}}),
                                    ("post", f"/sessions/{b['session_id']}/reset", {}),
                                    ("get", f"/sessions/{b['session_id']}/history", {}),
                                    ("post", "/sessions", {}), ("get", "/sessions", {}),
                                    ("delete", f"/sessions/{b['session_id']}", {}), ("delete", "/sessions", {})):
                                self.assertEqual(request(method, url, **kwargs).status_code, 409)
                        finally:
                            release.set()
                        self.assertEqual(chat.result(timeout=5).status_code, 504 if fail_chat else 200)
                        self.assertIn(history.result(timeout=5).status_code, (409, 410))
                        self.assertEqual(bulk.result(timeout=5).status_code, 200)
                restarted = create_app(self.path).test_client()
                self.assertEqual(restarted.get("/sessions").json, {"sessions": []})
                self.assertEqual(restarted.post("/sessions").json["number"], 1)

    def test_bulk_waits_for_reset_then_deletes_without_reset_resurrecting_history(self):
        a = self.create()
        self.chat(a["session_id"])
        entered, release = Event(), Event()
        original = self.store.reset
        def delayed_reset(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release Reset")
            return original(*args)
        def post_reset():
            with self.app.test_client() as client:
                return client.post(f"/sessions/{a['session_id']}/reset")
        def delete_all():
            with self.app.test_client() as client:
                return client.delete("/sessions")
        with patch.object(self.store, "reset", delayed_reset):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reset = pool.submit(post_reset)
                try:
                    self.assertTrue(entered.wait(5))
                    bulk = pool.submit(delete_all)
                finally:
                    release.set()
                self.assertEqual(reset.result(timeout=5).status_code, 200)
                self.assertEqual(bulk.result(timeout=5).status_code, 200)
        self.assertEqual(self.client.get(f"/sessions/{a['session_id']}/history").status_code, 410)

    def test_bulk_rejected_during_individual_delete_without_deleting_other_rows_or_resetting_counter(self):
        a, b = self.create(), self.create()
        self.chat(b["session_id"], "en")
        before_b = self.store.load(b["session_id"])
        entered, release = Event(), Event()
        original = self.store.delete
        def delayed_delete(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release Delete")
            return original(*args)
        def remove():
            with self.app.test_client() as client:
                return client.delete(f"/sessions/{a['session_id']}")
        with patch.object(self.store, "delete", delayed_delete):
            with ThreadPoolExecutor(max_workers=1) as pool:
                deleting = pool.submit(remove)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(self.client.delete("/sessions").status_code, 409)
                finally:
                    release.set()
                self.assertEqual(deleting.result(timeout=5).status_code, 200)
        self.assertEqual(self.store.load(b["session_id"]), before_b)
        self.assertEqual(self.create()["number"], 3)

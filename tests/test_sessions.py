"""Persistent session API tests; Ollama calls are mocked."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import patch
from sqlite_test_support import isolated_database

from app import Session, create_app


def answer():
    return {"message": {"role": "assistant", "content": "reply"}}


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.database_path = isolated_database(self)
        self.app = create_app()
        self.client = self.app.test_client()
        mocked = patch("ai_controller.ask_ollama", return_value=answer())
        self.ask = mocked.start()
        self.addCleanup(mocked.stop)

    def create(self):
        response = self.client.post("/sessions")
        self.assertEqual(response.status_code, 201)
        return response.json

    def test_list_is_read_only_and_restart_restores_sessions(self):
        self.assertEqual(self.client.get("/sessions").json, {"sessions": []})
        first = self.create()
        self.assertEqual(first["number"], 1)
        self.assertNotEqual(first["session_id"], "1")
        self.assertEqual(self.client.get("/sessions").json["sessions"], [first])
        self.assertEqual(create_app().test_client().get("/sessions").json, {"sessions": [first]})

    def test_numbers_titles_history_and_separation(self):
        a, b = self.create(), self.create()
        self.assertEqual((a["number"], b["number"]), (1, 2))
        self.assertNotEqual(a["session_id"], b["session_id"])
        question = "가" * 41
        self.assertEqual(self.client.post("/chat", json={
            "session_id": a["session_id"], "message": question, "response_language": "ko",
        }).status_code, 200)
        self.client.post("/chat", json={"session_id": a["session_id"], "message": "later"})
        self.client.post("/chat", json={"session_id": b["session_id"], "message": "other"})
        listing = self.client.get("/sessions").json["sessions"]
        self.assertEqual(listing[0]["title"], "가" * 40 + "...")
        self.assertEqual(listing[1]["title"], "other")
        history = self.client.get(f"/sessions/{a['session_id']}/history").json
        self.assertEqual(history["number"], 1)
        self.assertEqual([x["role"] for x in history["messages"]],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual(history["messages"][1]["language"], "ko")
        self.assertIsNone(history["messages"][3]["language"])
        self.assertEqual(len(self.client.get(f"/sessions/{b['session_id']}/history").json["messages"]), 2)

    def test_reset_is_scoped_and_explicit_session_remains(self):
        a, b = self.create(), self.create()
        for item in (a, b):
            self.client.post("/chat", json={"session_id": item["session_id"], "message": "first"})
        self.assertEqual(self.client.post(f"/sessions/{a['session_id']}/reset").json,
                         {"session_id": a["session_id"], "status": "reset"})
        history_a = self.client.get(f"/sessions/{a['session_id']}/history").json
        history_b = self.client.get(f"/sessions/{b['session_id']}/history").json
        self.assertEqual(history_a["messages"], [])
        self.assertEqual(history_a["title"], "새 대화")
        self.assertEqual(len(history_b["messages"]), 2)
        self.assertEqual(len(self.client.get("/sessions").json["sessions"]), 2)

    def test_cap_existing_chat_and_no_unwanted_creation(self):
        items = [self.create() for _ in range(60)]
        self.assertEqual(self.client.post("/sessions").status_code, 429)
        self.assertEqual(self.client.get("/sessions/missing/history").status_code, 404)
        self.assertEqual(self.client.post("/sessions/missing/reset").status_code, 200)
        self.assertEqual(len(self.client.get("/sessions").json["sessions"]), 60)
        self.assertEqual(self.client.post("/chat", json={
            "session_id": items[0]["session_id"], "message": "hello",
        }).status_code, 200)
        self.assertEqual(self.client.post("/chat", json={
            "session_id": "legacy", "message": "hello",
        }).status_code, 429)

    def test_parallel_creation_numbers_are_unique_and_capped(self):
        def create_one(_):
            with self.app.test_client() as client:
                return client.post("/sessions")
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(create_one, range(72)))
        created = [response.json for response in results if response.status_code == 201]
        self.assertEqual(len(created), 60)
        self.assertEqual(len({item["number"] for item in created}), 60)
        self.assertEqual(len({item["session_id"] for item in created}), 60)
        self.assertEqual(sum(response.status_code == 429 for response in results), 12)

    def test_legacy_chat_compatibility_and_failure_cleanup(self):
        self.assertEqual(self.client.post("/chat", json={
            "session_id": "legacy", "message": "hello",
        }).status_code, 200)
        listing = self.client.get("/sessions").json["sessions"]
        self.assertEqual(listing[0]["title"], "hello")
        self.assertEqual(self.client.post("/sessions/legacy/reset").json,
                         {"session_id": "legacy", "status": "reset"})
        self.assertEqual(self.client.get("/sessions").json["sessions"], [])
        with patch("ai_controller.ask_ollama", side_effect=TimeoutError):
            self.assertEqual(self.client.post("/chat", json={
                "session_id": "failed", "message": "hello",
            }).status_code, 504)
        self.assertEqual(self.client.get("/sessions").json["sessions"], [])

    def test_first_question_title_is_not_replaced_when_it_matches_placeholder(self):
        item = self.create()
        self.client.post("/chat", json={"session_id": item["session_id"], "message": "새 대화"})
        self.client.post("/chat", json={"session_id": item["session_id"], "message": "second title"})
        self.assertEqual(self.client.get("/sessions").json["sessions"][0]["title"], "새 대화")

    def test_reset_preserves_other_history_title_number_and_language_metadata(self):
        a, b = self.create(), self.create()
        for item, language, question in ((a, "ko", "A question"), (b, "en", "B question")):
            self.client.post("/chat", json={
                "session_id": item["session_id"], "message": question,
                "response_language": language,
            })
        before_b = self.client.get(f"/sessions/{b['session_id']}/history").json
        before_list = self.client.get("/sessions").json["sessions"]
        for _ in range(2):
            result = self.client.post(f"/sessions/{a['session_id']}/reset")
            self.assertEqual(result.json, {"session_id": a["session_id"], "status": "reset"})
            after_a = self.client.get(f"/sessions/{a['session_id']}/history").json
            self.assertEqual(after_a, {**a, "messages": []})
            self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, before_b)
            after_list = self.client.get("/sessions").json["sessions"]
            self.assertEqual(after_list[1], before_list[1])
            self.assertEqual(len(after_list), 2)
        self.client.post("/chat", json={
            "session_id": a["session_id"], "message": "new A question", "response_language": "en",
        })
        after_a = self.client.get(f"/sessions/{a['session_id']}/history").json
        self.assertEqual(after_a["number"], a["number"])
        self.assertEqual(after_a["title"], "new A question")
        self.assertEqual(after_a["messages"], [
            {"role": "user", "content": "new A question"},
            {"role": "assistant", "content": "reply", "language": "en"},
        ])
        self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, before_b)

    def test_reset_waits_for_same_session_chat_success_or_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                a, b = self.create(), self.create()
                self.client.post("/chat", json={"session_id": b["session_id"], "message": "B intact", "response_language": "en"})
                before_b = self.client.get(f"/sessions/{b['session_id']}/history").json
                entered, release, reset_entered = Event(), Event(), Event()

                def delayed_answer(_messages):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release Chat")
                    if failed:
                        raise TimeoutError()
                    return answer()

                def chat():
                    with self.app.test_client() as client:
                        return client.post("/chat", json={"session_id": a["session_id"], "message": "A pending", "response_language": "ko"})

                def reset():
                    with self.app.test_client() as client:
                        reset_entered.set()
                        return client.post(f"/sessions/{a['session_id']}/reset")

                with patch("ai_controller.ask_ollama", side_effect=delayed_answer):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        pending_chat = pool.submit(chat)
                        try:
                            self.assertTrue(entered.wait(5))
                            pending_reset = pool.submit(reset)
                            self.assertTrue(reset_entered.wait(5))
                            self.assertFalse(pending_reset.done())
                            self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, before_b)
                        finally:
                            release.set()
                        self.assertEqual(pending_chat.result(timeout=5).status_code, 504 if failed else 200)
                        self.assertEqual(pending_reset.result(timeout=5).status_code, 200)
                self.assertEqual(self.client.get(f"/sessions/{a['session_id']}/history").json, {**a, "messages": []})
                self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, before_b)

    def test_reset_of_another_session_does_not_wait_for_pending_chat(self):
        a, b = self.create(), self.create()
        self.client.post("/chat", json={"session_id": b["session_id"], "message": "B question", "response_language": "en"})
        entered, release = Event(), Event()

        def delayed_answer(_messages):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release Chat")
            return answer()

        def chat():
            with self.app.test_client() as client:
                return client.post("/chat", json={"session_id": a["session_id"], "message": "A question", "response_language": "ko"})

        def reset():
            with self.app.test_client() as client:
                return client.post(f"/sessions/{b['session_id']}/reset")

        with patch("ai_controller.ask_ollama", side_effect=delayed_answer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending_chat = pool.submit(chat)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(pool.submit(reset).result(timeout=2).status_code, 200)
                    self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, {**b, "messages": []})
                    self.assertFalse(pending_chat.done())
                finally:
                    release.set()
                self.assertEqual(pending_chat.result(timeout=5).status_code, 200)
        history_a = self.client.get(f"/sessions/{a['session_id']}/history").json
        self.assertEqual(history_a["title"], "A question")
        self.assertEqual(history_a["messages"][1]["language"], "ko")

    def test_delete_removes_only_target_and_blocks_recreation(self):
        a, b = self.create(), self.create()
        for item in (a, b):
            self.client.post("/chat", json={"session_id": item["session_id"], "message": "question", "response_language": "en"})
        before_b = self.client.get(f"/sessions/{b['session_id']}/history").json
        self.client.post(f"/sessions/{a['session_id']}/reset")
        self.assertEqual(len(self.client.get("/sessions").json["sessions"]), 2)
        response = self.client.delete(f"/sessions/{a['session_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"session_id": a["session_id"], "status": "deleted"})
        self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, before_b)
        self.assertEqual(self.client.get("/sessions").json["sessions"], [{k: before_b[k] for k in ("session_id", "number", "title")}])
        for response in (
            self.client.get(f"/sessions/{a['session_id']}/history"),
            self.client.post(f"/sessions/{a['session_id']}/reset"),
            self.client.post("/chat", json={"session_id": a["session_id"], "message": "late"}),
        ):
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.json["error"]["code"], "session_deleted")
        self.assertEqual(len(self.client.get("/sessions").json["sessions"]), 1)

    def test_delete_missing_invalid_and_repeat_do_not_create(self):
        self.assertEqual(self.client.delete("/sessions/missing").status_code, 404)
        self.assertEqual(self.client.delete("/sessions/" + "x" * 129).status_code, 400)
        item = self.create()
        self.assertEqual(self.client.delete(f"/sessions/{item['session_id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/sessions/{item['session_id']}").status_code, 404)
        self.assertEqual(self.client.get("/sessions").json, {"sessions": []})

    def test_delete_restores_capacity_without_reusing_numbers(self):
        items = [self.create() for _ in range(60)]
        self.assertEqual(self.client.post("/sessions").status_code, 429)
        self.client.delete(f"/sessions/{items[20]['session_id']}")
        new = self.create()
        self.assertEqual(new["number"], 61)
        listing = self.client.get("/sessions").json["sessions"]
        self.assertEqual(len(listing), 60)
        self.assertEqual(len({item["number"] for item in listing}), 60)

    def test_delete_waits_for_running_chat_and_rejects_queued_operations(self):
        for failed, queued_kind in ((False, "chat"), (True, "chat"), (False, "history"), (False, "reset")):
            with self.subTest(failed=failed, queued=queued_kind):
                captured = []

                def new_session(**kwargs):
                    result = Session(**kwargs)
                    captured.append(result)
                    return result

                with patch("app.Session", side_effect=new_session):
                    a = self.create()
                state = captured[0]
                b = self.create()
                before_b = self.client.get(f"/sessions/{b['session_id']}/history").json
                entered, release, queued, marked = Event(), Event(), Event(), Event()
                base_lock = state.lock
                attempts_lock = Lock()
                attempts = [0]

                class ObservedLock:
                    def __enter__(self):
                        with attempts_lock:
                            attempts[0] += 1
                            if attempts[0] == 2:
                                queued.set()
                            if state.deleting:
                                marked.set()
                        base_lock.acquire()

                    def __exit__(self, *_args):
                        base_lock.release()

                state.lock = ObservedLock()

                def delayed(_messages):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release Chat")
                    if failed:
                        raise TimeoutError()
                    return answer()

                def post_chat(text):
                    with self.app.test_client() as client:
                        return client.post("/chat", json={"session_id": a["session_id"], "message": text})

                def remove():
                    with self.app.test_client() as client:
                        return client.delete(f"/sessions/{a['session_id']}")

                def queued_request():
                    if queued_kind == "chat":
                        return post_chat("queued")
                    with self.app.test_client() as client:
                        if queued_kind == "history":
                            return client.get(f"/sessions/{a['session_id']}/history")
                        return client.post(f"/sessions/{a['session_id']}/reset")

                with patch("ai_controller.ask_ollama", side_effect=delayed):
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        first = pool.submit(post_chat, "running")
                        try:
                            self.assertTrue(entered.wait(5))
                            second = pool.submit(queued_request)
                            self.assertTrue(queued.wait(5))
                            deletion = pool.submit(remove)
                            self.assertTrue(marked.wait(5))
                            self.assertFalse(deletion.done())
                            for response in (
                                self.client.get(f"/sessions/{a['session_id']}/history"),
                                self.client.post(f"/sessions/{a['session_id']}/reset"),
                                self.client.post("/chat", json={"session_id": a["session_id"], "message": "new"}),
                            ):
                                self.assertEqual(response.status_code, 409)
                            self.assertEqual(self.client.get(f"/sessions/{b['session_id']}/history").json, before_b)
                        finally:
                            release.set()
                        self.assertEqual(first.result(timeout=5).status_code, 504 if failed else 200)
                        self.assertIn(second.result(timeout=5).status_code, (409, 410))
                        self.assertEqual(deletion.result(timeout=5).status_code, 200)
                self.assertEqual(state.messages, [])
                self.assertEqual(state.answer_languages, [])
                self.assertEqual(state.title, "")
                self.assertEqual(self.client.get(f"/sessions/{a['session_id']}/history").status_code, 410)

    def test_deleted_legacy_id_cannot_be_recreated_by_late_chat(self):
        self.client.post("/chat", json={"session_id": "legacy-delete", "message": "hello"})
        self.client.delete("/sessions/legacy-delete")
        self.assertEqual(self.client.post("/chat", json={"session_id": "legacy-delete", "message": "late"}).status_code, 410)
        self.assertEqual(self.client.get("/sessions").json["sessions"], [])


if __name__ == "__main__":
    unittest.main()

import io
import json
from pathlib import Path
import runpy
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from unittest.mock import MagicMock, patch

import ai_controller
from app import create_app


def chat_result() -> dict:
    return {
        "message": {"role": "assistant", "content": "안녕하세요"},
        "eval_count": 20, "eval_duration": 2_000_000_000,
        "prompt_eval_count": 5, "prompt_eval_duration": 100_000_000,
        "total_duration": 2_200_000_000, "load_duration": 100_000_000,
    }


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = self.app.test_client()
        self.ask = patch("ai_controller.ask_ollama", return_value=chat_result()).start()
        self.addCleanup(patch.stopall)

    def test_index_renders_web_console(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        page = response.get_data(as_text=True)
        self.assertIn("Local AI Control Console", page)
        self.assertIn('id="guide-view"', page)
        self.assertIn('id="server-view"', page)
        self.assertIn('id="console-lock"', page)
        self.assertIn('id="refresh-health"', page)

    def test_ui_static_assets_are_available(self) -> None:
        for path, mime_types in (
                ("/static/css/style.css", {"text/css"}),
                ("/static/js/main.js", {"text/javascript", "application/javascript"})):
            with self.subTest(path=path):
                response = self.client.get(path)
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(response.mimetype, mime_types)
                finally:
                    response.close()

    def test_desktop_console_fits_viewport(self) -> None:
        response = self.client.get("/static/css/style.css")
        try:
            css = response.get_data(as_text=True)
            self.assertIn("height: calc(100vh - 28px)", css)
            self.assertIn("grid-template-rows: 64px minmax(0, 1fr)", css)
            self.assertIn("clamp(210px, 20vh, 240px)", css)
        finally:
            response.close()

    def test_server_status_ui_uses_health_only(self) -> None:
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            self.assertEqual(script.count('fetch("/health"'), 1)
            self.assertNotIn('fetch("/chat"', script)
            self.assertNotIn("/sessions/", script)
            self.assertIn("ollama_reachable", script)
            self.assertIn("refreshHealthButton.addEventListener", script)
        finally:
            response.close()

    def test_server_status_markup_supports_independent_states(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for element_id in ("controller-status", "ollama-status", "configured-model",
                           "local-server-list", "server-status", "health-message"):
            self.assertIn(f'id="{element_id}"', page)

    def chat(self, session_id: str = "a", message: str = "질문"):
        return self.client.post("/chat", json={"session_id": session_id, "message": message})

    def test_health_connected(self) -> None:
        with patch("ai_controller.check_ollama"):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "controller": "ok", "ollama_reachable": True, "model": ai_controller.MODEL,
        })
        self.ask.assert_not_called()

    def test_health_disconnected(self) -> None:
        with patch("ai_controller.check_ollama", side_effect=urllib.error.URLError("private")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["controller"], "ok")
        self.assertFalse(response.json["ollama_reachable"])
        self.assertNotIn("private", response.get_data(as_text=True))

    def test_chat_metrics(self) -> None:
        response = self.chat()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["message"], "안녕하세요")
        self.assertEqual(response.json["metrics"]["tokens_per_second"], 10)
        self.assertEqual(response.json["metrics"]["prompt_eval_count"], 5)
        self.assertGreaterEqual(response.json["elapsed_seconds"], 0)

    def test_missing_metrics(self) -> None:
        self.ask.return_value = {"message": {"role": "assistant", "content": "ok"}}
        self.assertEqual(self.chat().json["metrics"], {"tokens_per_second": None})

    def test_zero_duration(self) -> None:
        self.ask.return_value["eval_duration"] = 0
        self.assertIsNone(self.chat().json["metrics"]["tokens_per_second"])

    def test_history_separation_and_reset(self) -> None:
        self.chat("a", "first")
        self.chat("a", "second")
        self.assertEqual([m["role"] for m in self.ask.call_args.args[0]],
                         ["system", "user", "assistant", "user"])
        self.chat("b", "independent")
        self.assertEqual(len(self.ask.call_args.args[0]), 2)
        response = self.client.post("/sessions/a/reset")
        self.assertEqual(response.status_code, 200)
        self.chat("a", "new")
        self.assertEqual(len(self.ask.call_args.args[0]), 2)
        self.chat("b", "still here")
        self.assertEqual(len(self.ask.call_args.args[0]), 4)

    def test_reset_unknown_session_is_idempotent(self) -> None:
        for _ in range(2):
            self.assertEqual(self.client.post("/sessions/unknown/reset").status_code, 200)

    def test_invalid_json(self) -> None:
        for body, content_type in [("{", "application/json"), ("null", "application/json"),
                                   ("[]", "application/json"), ("hello", "text/plain")]:
            with self.subTest(body=body):
                response = self.client.post("/chat", data=body, content_type=content_type)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["error"]["code"], "invalid_json")
        self.ask.assert_not_called()

    def test_invalid_fields(self) -> None:
        for field in ("session_id", "message"):
            for value in (None, "", "  ", 1, [], {}):
                with self.subTest(field=field, value=value):
                    data = {"session_id": "a", "message": "hi", field: value}
                    self.assertEqual(self.client.post("/chat", json=data).status_code, 400)
            data = {"session_id": "a", "message": "hi"}
            del data[field]
            self.assertEqual(self.client.post("/chat", json=data).status_code, 400)
        self.ask.assert_not_called()

    def test_upstream_errors_do_not_commit_history(self) -> None:
        errors = [
            (urllib.error.URLError("private"), 503, "ollama_unavailable"),
            (TimeoutError("private"), 504, "ollama_timeout"),
            (urllib.error.URLError(TimeoutError("private")), 504, "ollama_timeout"),
            (urllib.error.HTTPError("local", 404, "private", {}, io.BytesIO(b"private")),
             502, "ollama_model_error"),
            (ValueError("private"), 502, "invalid_ollama_response"),
        ]
        for error, status, code in errors:
            with self.subTest(error=type(error), code=code):
                self.ask.side_effect = error
                response = self.chat()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json["error"]["code"], code)
                self.assertNotIn("private", response.get_data(as_text=True))
        self.ask.side_effect = None
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(len(self.ask.call_args.args[0]), 2)

    def test_invalid_upstream_results(self) -> None:
        for result in ([], {}, {"error": "private"}, {"message": {}},
                       {"message": {"role": "user", "content": "bad"}},
                       {**chat_result(), "eval_count": "bad"},
                       {**chat_result(), "eval_duration": -1}):
            with self.subTest(result=result):
                self.ask.return_value = result
                self.assertEqual(self.chat().status_code, 502)

    def test_internal_error_is_sanitized(self) -> None:
        self.ask.side_effect = RuntimeError("private detail")
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.chat()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"]["code"], "internal_error")
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertNotIn("Traceback", response.get_data(as_text=True))

    def test_http_errors_are_json(self) -> None:
        self.assertEqual(self.client.get("/missing").status_code, 404)
        self.assertEqual(self.client.get("/chat").status_code, 405)
        response = self.client.post("/chat", data="x" * (1024 * 1024 + 1),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 413)
        self.assertIsInstance(response.json["error"], dict)

    def test_concurrent_same_session_keeps_both_turns(self) -> None:
        entered, release = Event(), Event()
        calls = []

        def answer(messages):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Test did not release first request")
            return chat_result()

        def post(message):
            with self.app.test_client() as client:
                return client.post("/chat", json={"session_id": "a", "message": message})

        self.ask.side_effect = answer
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post, "first")
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(post, "second")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)
            self.assertEqual(second.result(timeout=5).status_code, 200)
        self.assertEqual([len(messages) for messages in calls], [2, 4])

    def test_reset_during_chat_clears_completed_turn(self) -> None:
        entered, release, reset_started = Event(), Event(), Event()

        def answer(messages):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release request")
            return chat_result()

        def post_chat():
            with self.app.test_client() as client:
                return client.post("/chat", json={"session_id": "a", "message": "first"})

        def post_reset():
            with self.app.test_client() as client:
                reset_started.set()
                return client.post("/sessions/a/reset")

        self.ask.side_effect = answer
        with ThreadPoolExecutor(max_workers=2) as pool:
            chat = pool.submit(post_chat)
            try:
                self.assertTrue(entered.wait(5))
                reset = pool.submit(post_reset)
                self.assertTrue(reset_started.wait(5))
            finally:
                release.set()
            self.assertEqual(chat.result(timeout=5).status_code, 200)
            self.assertEqual(reset.result(timeout=5).status_code, 200)
        self.ask.side_effect = None
        self.chat()
        self.assertEqual(len(self.ask.call_args.args[0]), 2)


class TransportTests(unittest.TestCase):
    @patch("flask.Flask.run")
    def test_entry_point_binds_loopback_without_debug(self, run) -> None:
        path = Path(__file__).resolve().parents[1] / "app.py"
        runpy.run_path(str(path), run_name="__main__")
        run.assert_called_once_with(host="127.0.0.1", port=5000,
                                    debug=False, use_reloader=False)

    @patch("ai_controller.urllib.request.urlopen")
    def test_existing_chat_transport(self, urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(chat_result()).encode())
        urlopen.return_value = response
        messages = [{"role": "user", "content": "안녕"}]
        self.assertEqual(ai_controller.ask_ollama(messages)["eval_count"], 20)
        req = urlopen.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(req.method, "POST")
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["model"], ai_controller.MODEL)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)

    @patch("ai_controller.urllib.request.urlopen")
    def test_health_transport(self, urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"version": "test"}')
        urlopen.return_value = response
        ai_controller.check_ollama()
        urlopen.assert_called_once_with("http://127.0.0.1:11434/api/version", timeout=3)


if __name__ == "__main__":
    unittest.main()

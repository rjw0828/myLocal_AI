import io
import json
from pathlib import Path
import runpy
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event
from unittest.mock import MagicMock, patch

import ai_controller
from app import (MAX_ACTIVE_SESSIONS, MAX_MESSAGE_LENGTH,
                 MAX_SESSION_ID_LENGTH, create_app)


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

    def test_server_status_ui_keeps_health_integration(self) -> None:
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            self.assertGreaterEqual(script.count('requestJson("/health"'), 2)
            self.assertIn("ollama_reachable", script)
            self.assertIn("refreshHealthButton.addEventListener", script)
        finally:
            response.close()

    def test_server_status_markup_supports_independent_states(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for element_id in ("controller-status", "ollama-status", "configured-model",
                           "local-server-list", "server-toggle", "health-message"):
            self.assertIn(f'id="{element_id}"', page)

    def test_tool_console_markup_has_controls_and_locked_defaults(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for value in ('data-tool="health"', 'data-tool="chat"', 'data-tool="reset"',
                      'id="clear-output"', 'id="output-log"'):
            self.assertIn(value, page)
        self.assertNotIn('id="execute-tool"', page)
        self.assertIn('id="console-lock" type="button" aria-pressed="true"', page)
        self.assertIn('id="session-id" name="session_id" value="default"', page)
        self.assertIn('id="message" name="message"', page)
        self.assertIn('maxlength="128"', page)
        self.assertIn('maxlength="10000"', page)

    def test_conversation_and_system_log_panels_are_independent(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        for value in ('id="conversation-output"', 'id="conversation-session"',
                      'class="session-list-placeholder"', 'id="output-log"',
                      '>Conversation<', '>Status / System Log<', '>Clear Log<'):
            self.assertIn(value, page)

        response = self.client.get("/static/css/style.css")
        try:
            css = response.get_data(as_text=True)
            self.assertIn("grid-template-columns: 18fr 23fr 39fr 20fr", css)
            self.assertIn(".conversation-output { flex: 1; min-height: 0;", css)
            self.assertIn(".terminal { flex: 1; min-height: 0;", css)
            self.assertIn("@media (max-width: 1200px)", css)
        finally:
            response.close()

    def test_conversation_output_is_session_scoped_and_text_only(self) -> None:
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            self.assertIn("const conversations = new Map()", script)
            self.assertIn("appendConversation(sessionId, \"user\", message)", script)
            self.assertIn("appendConversation(sessionId, \"assistant\", result.message)", script)
            self.assertIn("conversationSession.textContent", script)
            self.assertIn("text.textContent = content", script)
            self.assertIn("conversations.delete(sessionId)", script)
            self.assertNotIn("innerHTML", script)
        finally:
            response.close()

    def test_clear_log_only_targets_system_log(self) -> None:
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            self.assertIn(
                'clearOutputButton.addEventListener("click", () => { outputLog.replaceChildren(); })',
                script,
            )
            self.assertNotIn(
                'clearOutputButton.addEventListener("click", () => { conversationOutput',
                script,
            )
        finally:
            response.close()

    def test_tool_console_script_covers_safety_and_api_contracts(self) -> None:
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            self.assertEqual(script.count("fetch(path"), 1)
            self.assertIn('requestJson("/chat"', script)
            self.assertIn("/sessions/${encodeURIComponent(sessionId)}/reset", script)
            self.assertIn("window.confirm", script)
            self.assertIn("REQUEST_TIMEOUT_MS = 190_000", script)
            self.assertIn("messageInput.value.trim()", script)
            self.assertIn('event.key === "Enter"', script)
            self.assertIn("activateTool(button.dataset.tool)", script)
            self.assertIn("requestRunning", script)
            self.assertIn("outputLog.replaceChildren()", script)
            self.assertIn("body.textContent = message", script)
            self.assertNotIn("innerHTML", script)
        finally:
            response.close()

    def test_ollama_control_requires_console_header(self) -> None:
        response = self.client.post("/ollama/start")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["error"]["code"], "control_header_required")

    def test_ollama_control_start_and_stop(self) -> None:
        headers = {"X-Local-AI-Control": "web-console"}
        for action, status in (("start", "started"), ("stop", "stopped")):
            with self.subTest(action=action), \
                    patch(f"ollama_runtime.{action}", return_value=status) as control, \
                    patch("ollama_runtime.is_online", return_value=action == "start"):
                response = self.client.post(f"/ollama/{action}", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["action"], action)
                self.assertEqual(response.json["status"], status)
                control.assert_called_once_with()

    def test_ollama_control_failure_is_structured(self) -> None:
        import ollama_runtime
        headers = {"X-Local-AI-Control": "web-console"}
        with patch("ollama_runtime.start",
                   side_effect=ollama_runtime.OllamaControlError("test failure")):
            response = self.client.post("/ollama/start", headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "ollama_control_failed")

    def test_ollama_control_rejects_unknown_action(self) -> None:
        response = self.client.post(
            "/ollama/restart", headers={"X-Local-AI-Control": "web-console"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"]["code"], "invalid_ollama_action")

    def test_tool_console_formats_chat_metrics(self) -> None:
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            for field in ("elapsed_seconds", "eval_count", "prompt_eval_count",
                          "tokens_per_second", "total_duration", "load_duration",
                          "prompt_eval_duration", "eval_duration"):
                self.assertIn(field, script)
            self.assertIn("value / 1_000_000_000", script)
        finally:
            response.close()

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

    def test_history_growth_remains_untrimmed(self) -> None:
        turns = 25
        for index in range(turns):
            self.assertEqual(self.chat("growing", f"turn-{index}").status_code, 200)
        self.assertEqual(len(self.ask.call_args.args[0]), turns * 2)

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

    def test_input_length_boundaries_use_python_character_count(self) -> None:
        for session_id in ("s" * MAX_SESSION_ID_LENGTH,
                           "한" * MAX_SESSION_ID_LENGTH):
            with self.subTest(field="session_id", length=len(session_id)):
                self.assertEqual(self.chat(session_id=session_id).status_code, 200)
        for message in ("m" * MAX_MESSAGE_LENGTH,
                        "한" * MAX_MESSAGE_LENGTH):
            with self.subTest(field="message", length=len(message)):
                self.assertEqual(self.chat(message=message).status_code, 200)

    def test_overlong_inputs_are_rejected_without_ollama_call(self) -> None:
        cases = (
            ({"session_id": "s" * (MAX_SESSION_ID_LENGTH + 1), "message": "ok"},
             "invalid_session_id"),
            ({"session_id": "한" * (MAX_SESSION_ID_LENGTH + 1), "message": "ok"},
             "invalid_session_id"),
            ({"session_id": "a", "message": "m" * (MAX_MESSAGE_LENGTH + 1)},
             "invalid_message"),
            ({"session_id": "a", "message": "한" * (MAX_MESSAGE_LENGTH + 1)},
             "invalid_message"),
        )
        for data, code in cases:
            with self.subTest(code=code, length=max(map(len, data.values()))):
                response = self.client.post("/chat", json=data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["error"]["code"], code)
        self.ask.assert_not_called()

    def test_session_limit_allows_sixtieth_and_existing_session(self) -> None:
        for index in range(MAX_ACTIVE_SESSIONS - 1):
            self.assertEqual(self.chat(f"session-{index}").status_code, 200)
        self.assertEqual(self.chat("session-59").status_code, 200)
        rejected = self.chat("session-60")
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json["error"]["code"], "session_limit_reached")
        self.assertEqual(self.chat("session-0", "still allowed").status_code, 200)

    def test_concurrent_new_sessions_never_exceed_limit(self) -> None:
        def post(index):
            with self.app.test_client() as client:
                return client.post("/chat", json={
                    "session_id": f"concurrent-{index}", "message": "hi",
                }).status_code

        with ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(post, range(MAX_ACTIVE_SESSIONS + 10)))
        self.assertEqual(statuses.count(200), MAX_ACTIVE_SESSIONS)
        self.assertEqual(statuses.count(429), 10)

    def test_failed_new_sessions_do_not_consume_slots(self) -> None:
        self.ask.side_effect = urllib.error.URLError("offline")
        for index in range(MAX_ACTIVE_SESSIONS + 5):
            self.assertEqual(self.chat(f"failed-{index}").status_code, 503)
        self.ask.side_effect = None
        self.ask.return_value = chat_result()
        for index in range(MAX_ACTIVE_SESSIONS):
            self.assertEqual(self.chat(f"success-{index}").status_code, 200)

    def test_unknown_reset_does_not_consume_session_slots(self) -> None:
        for index in range(MAX_ACTIVE_SESSIONS + 5):
            response = self.client.post(f"/sessions/unknown-{index}/reset")
            self.assertEqual(response.status_code, 200)
        for index in range(MAX_ACTIVE_SESSIONS):
            self.assertEqual(self.chat(f"real-{index}").status_code, 200)

    def test_reset_releases_inactive_session_slot(self) -> None:
        for index in range(MAX_ACTIVE_SESSIONS):
            self.assertEqual(self.chat(f"session-{index}").status_code, 200)
        self.assertEqual(self.client.post("/sessions/session-0/reset").status_code, 200)
        self.assertEqual(self.chat("replacement").status_code, 200)

    def test_reset_rejects_overlong_session_id_without_creating_session(self) -> None:
        session_id = "s" * (MAX_SESSION_ID_LENGTH + 1)
        response = self.client.post(f"/sessions/{session_id}/reset")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"]["code"], "invalid_session_id")

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

    def test_ollama_http_errors_are_sanitized(self) -> None:
        for status in (401, 404, 500):
            with self.subTest(status=status):
                self.ask.side_effect = urllib.error.HTTPError(
                    "local", status, "private", {}, io.BytesIO(b"private"))
                response = self.chat(session_id=f"http-{status}")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json["error"]["code"], "ollama_model_error")
                self.assertNotIn("private", response.get_data(as_text=True))

    def test_health_rejects_invalid_ollama_version_response(self) -> None:
        with patch("ai_controller.check_ollama", side_effect=ValueError("private")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ollama_reachable"])
        self.assertNotIn("private", response.get_data(as_text=True))

    def test_non_finite_metrics_are_rejected(self) -> None:
        for field in ("eval_count", "eval_duration", "total_duration"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    self.ask.return_value = {**chat_result(), field: value}
                    self.assertEqual(self.chat(session_id=f"{field}-{value}").status_code, 502)

    def test_empty_assistant_response_is_rejected(self) -> None:
        for content in ("", "   "):
            with self.subTest(content=repr(content)):
                self.ask.return_value = {
                    "message": {"role": "assistant", "content": content},
                }
                response = self.chat(session_id=f"empty-{len(content)}")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json["error"]["code"], "invalid_ollama_response")

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

    def test_different_sessions_can_run_independently(self) -> None:
        entered = Barrier(2, timeout=5)

        def answer(messages):
            entered.wait()
            return chat_result()

        def post(session_id):
            with self.app.test_client() as client:
                return client.post("/chat", json={
                    "session_id": session_id, "message": "hello",
                })

        self.ask.side_effect = answer
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(post, ("independent-a", "independent-b")))
        self.assertEqual([response.status_code for response in responses], [200, 200])

    def test_ollama_stop_warning_mentions_other_programs(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        response = self.client.get("/static/js/main.js")
        try:
            script = response.get_data(as_text=True)
            self.assertIn("다른 프로그램", page)
            self.assertIn("다른 프로그램", script)
        finally:
            response.close()

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

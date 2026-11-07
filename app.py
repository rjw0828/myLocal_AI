"""Local-only HTTP interface for the existing Ollama controller.

Session history lives in this process and is discarded on restart.
Run with `python app.py` to use the fixed loopback binding.
"""
import math
import time
import urllib.error
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import ai_controller
import ollama_runtime

HOST = "127.0.0.1"
PORT = 5000
MAX_CONTENT_LENGTH = 1024 * 1024
MAX_SESSION_ID_LENGTH = 128
MAX_MESSAGE_LENGTH = 10_000
MAX_ACTIVE_SESSIONS = 60
METRIC_FIELDS = (
    "total_duration", "load_duration", "prompt_eval_count",
    "prompt_eval_duration", "eval_count", "eval_duration",
)


@dataclass
class Session:
    messages: list[dict[str, str]] = field(default_factory=list)
    lock: Any = field(default_factory=Lock)
    active_requests: int = 0
    remove_when_unused: bool = False


class InvalidOllamaResponse(ValueError):
    """The upstream returned an error or an unusable chat response."""


class SessionLimitReached(RuntimeError):
    """The in-memory active session limit has been reached."""


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
    sessions: dict[str, Session] = {}
    sessions_lock = Lock()

    def acquire_session(session_id: str, create: bool = True) -> tuple[Session | None, bool]:
        with sessions_lock:
            session = sessions.get(session_id)
            created = False
            if session is None:
                if not create:
                    return None, False
                if len(sessions) >= MAX_ACTIVE_SESSIONS:
                    raise SessionLimitReached()
                session = Session()
                sessions[session_id] = session
                created = True
            session.active_requests += 1
            return session, created

    def release_session(session_id: str, session: Session, *,
                        failed_creation: bool = False, reset: bool = False,
                        committed: bool = False) -> None:
        with sessions_lock:
            if failed_creation or reset:
                session.remove_when_unused = True
            if committed:
                session.remove_when_unused = False
            session.active_requests -= 1
            if (session.active_requests == 0 and session.remove_when_unused
                    and not session.messages and sessions.get(session_id) is session):
                del sessions[session_id]

    def error_response(code: str, message: str, status: int) -> tuple[Response, int]:
        return jsonify(error={"code": code, "message": message}), status

    def require_local_control_header() -> tuple[Response, int] | None:
        if request.headers.get("X-Local-AI-Control") != "web-console":
            return error_response("control_header_required", "로컬 콘솔 제어 헤더가 필요합니다.", 403)
        return None

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/health")
    def health() -> tuple[Response, int]:
        reachable = True
        try:
            ai_controller.check_ollama()
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            reachable = False
        return jsonify(
            controller="ok", ollama_reachable=reachable, model=ai_controller.MODEL,
        ), 200 if reachable else 503

    @app.post("/chat")
    def chat() -> tuple[Response, int]:
        if not request.is_json:
            return error_response("invalid_json", "JSON 본문이 필요합니다.", 400)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return error_response("invalid_json", "유효한 JSON 객체가 필요합니다.", 400)
        session_id = data.get("session_id")
        message = data.get("message")
        if not isinstance(session_id, str) or not session_id.strip():
            return error_response("invalid_session_id", "session_id는 비어 있지 않은 문자열이어야 합니다.", 400)
        if len(session_id) > MAX_SESSION_ID_LENGTH:
            return error_response("invalid_session_id", f"session_id는 {MAX_SESSION_ID_LENGTH}자 이하여야 합니다.", 400)
        if not isinstance(message, str) or not message.strip():
            return error_response("invalid_message", "message는 비어 있지 않은 문자열이어야 합니다.", 400)
        if len(message) > MAX_MESSAGE_LENGTH:
            return error_response("invalid_message", f"message는 {MAX_MESSAGE_LENGTH:,}자 이하여야 합니다.", 400)

        started = time.perf_counter()
        try:
            session, created = acquire_session(session_id)
        except SessionLimitReached:
            return error_response(
                "session_limit_reached",
                f"활성 세션은 최대 {MAX_ACTIVE_SESSIONS}개까지 사용할 수 있습니다.", 429,
            )
        assert session is not None
        committed = False
        try:
            # Keep a chat and a concurrent reset on the same session ordered.
            with session.lock:
                messages = session.messages or [
                    {"role": "system", "content": ai_controller.SYSTEM_PROMPT},
                ]
                user_message = {"role": "user", "content": message}
                result = ai_controller.ask_ollama(messages + [user_message])
                if not isinstance(result, dict) or result.get("error"):
                    raise InvalidOllamaResponse()
                assistant = result.get("message")
                if (not isinstance(assistant, dict)
                        or assistant.get("role") != "assistant"
                        or not isinstance(assistant.get("content"), str)
                        or not assistant.get("content").strip()):
                    raise InvalidOllamaResponse()
                metrics = {key: result[key] for key in METRIC_FIELDS if key in result}
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in metrics.values()):
                    raise InvalidOllamaResponse()
                duration = metrics.get("eval_duration", 0)
                metrics["tokens_per_second"] = (
                    metrics["eval_count"] / (duration / 1e9)
                    if duration and "eval_count" in metrics else None
                )
                answer = {"role": "assistant", "content": assistant["content"]}
                session.messages = messages + [user_message, answer]
                committed = True
        except urllib.error.HTTPError:
            return error_response("ollama_model_error", "Ollama 요청이 거부되었습니다. 모델명과 Ollama 상태를 확인하세요.", 502)
        except TimeoutError:
            return error_response("ollama_timeout", "Ollama 응답 시간이 초과되었습니다.", 504)
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                return error_response("ollama_timeout", "Ollama 응답 시간이 초과되었습니다.", 504)
            return error_response("ollama_unavailable", "Ollama에 연결할 수 없습니다.", 503)
        except (ValueError, UnicodeError):
            return error_response("invalid_ollama_response", "Ollama 응답 형식이 올바르지 않습니다.", 502)
        except OSError:
            return error_response("ollama_unavailable", "Ollama에 연결할 수 없습니다.", 503)
        finally:
            release_session(session_id, session,
                            failed_creation=created and not committed,
                            committed=committed)
        return jsonify(
            session_id=session_id, model=ai_controller.MODEL,
            message=answer["content"], elapsed_seconds=time.perf_counter() - started,
            metrics=metrics,
        ), 200

    @app.post("/sessions/<session_id>/reset")
    def reset(session_id: str) -> Response | tuple[Response, int]:
        if not session_id.strip() or len(session_id) > MAX_SESSION_ID_LENGTH:
            return error_response("invalid_session_id", f"session_id는 {MAX_SESSION_ID_LENGTH}자 이하의 문자열이어야 합니다.", 400)
        session, _ = acquire_session(session_id, create=False)
        if session is not None:
            try:
                with session.lock:
                    session.messages.clear()
            finally:
                release_session(session_id, session, reset=True)
        return jsonify(session_id=session_id, status="reset")

    @app.post("/ollama/<action>")
    def control_ollama(action: str) -> tuple[Response, int]:
        denied = require_local_control_header()
        if denied:
            return denied
        if action not in {"start", "stop"}:
            return error_response("invalid_ollama_action", "지원하지 않는 Ollama 제어 요청입니다.", 404)
        try:
            status = ollama_runtime.start() if action == "start" else ollama_runtime.stop()
        except ollama_runtime.OllamaControlError as error:
            return error_response("ollama_control_failed", str(error), 503)
        return jsonify(action=action, status=status, ollama_reachable=ollama_runtime.is_online()), 200

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int]:
        return error_response("http_error", error.name, error.code or 500)

    @app.errorhandler(Exception)
    def internal_error(error: Exception) -> tuple[Response, int]:
        app.logger.exception("Unhandled controller error")
        return error_response("internal_error", "내부 처리 오류가 발생했습니다.", 500)

    return app


if __name__ == "__main__":
    create_app().run(host=HOST, port=PORT, debug=False, use_reloader=False)

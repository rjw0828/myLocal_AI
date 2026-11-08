"""Local-only HTTP interface for the existing Ollama controller.

Session history is persisted in the local SQLite database.
Run with `python app.py` to use the fixed loopback binding.
"""
import math
import os
import time
import urllib.error
from contextlib import ExitStack
from uuid import uuid4
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import ai_controller
import ollama_runtime
from voice import stt_service, tts_service
from storage.sqlite_store import DEFAULT_DB_PATH, SQLiteStore, StorageError

HOST = "127.0.0.1"
PORT = 5000
MAX_CONTENT_LENGTH = 1024 * 1024
MAX_SESSION_ID_LENGTH = 128
MAX_MESSAGE_LENGTH = 10_000
MAX_ACTIVE_SESSIONS = 60
SESSION_TITLE_LENGTH = 40
MAX_STT_AUDIO_BYTES = 10 * 1024 * 1024
# Multipart boundaries and headers sit outside the audio field itself.
MAX_STT_REQUEST_LENGTH = MAX_STT_AUDIO_BYTES + 64 * 1024
MAX_TTS_TEXT_LENGTH = 500
RESPONSE_LANGUAGE_PROMPTS = {
    "ko": "You are a personal local AI assistant. Answer clearly and concisely in Korean, using Hangul. Use Korean for this response even if the question or earlier conversation is in English. Do not answer in English or Chinese.",
    "en": "You are a personal local AI assistant. Answer clearly and concisely in English. Use English for this response, even if earlier conversation is in Korean.",
}
RESPONSE_LANGUAGE_TURN_INSTRUCTIONS = {
    "ko": "이번 답변은 질문이나 이전 대화의 언어와 관계없이 한국어(한글)로만 작성하세요. 영어 또는 중국어로 답변하지 마세요.",
    "en": "Write your entire answer in English, regardless of the language of the question or earlier conversation. Do not answer in Korean or Chinese.",
}
VOICE_RESPONSE_STYLE = (
    " This turn is a spoken conversation. Reply naturally in 1–3 short sentences, "
    "aiming for at most 400 Unicode characters. Avoid tables and long lists."
)
METRIC_FIELDS = (
    "total_duration", "load_duration", "prompt_eval_count",
    "prompt_eval_duration", "eval_count", "eval_duration",
)


@dataclass
class Session:
    messages: list[dict[str, str]] = field(default_factory=list)
    number: int = 0
    title: str = "새 대화"
    titled: bool = False
    explicit: bool = False
    answer_languages: list[str | None] = field(default_factory=list)
    lock: Any = field(default_factory=Lock)
    active_requests: int = 0
    remove_when_unused: bool = False
    deleting: bool = False
    deleted: bool = False
    revision: int = 0


class InvalidOllamaResponse(ValueError):
    """The upstream returned an error or an unusable chat response."""


class SessionDeleted(RuntimeError):
    """A deleted ID must never implicitly create another session."""


class SessionDeleting(RuntimeError):
    """The session is being removed after an already running operation."""


def create_app(database_path=None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
    store = SQLiteStore(database_path or os.environ.get("LOCAL_AI_DB_PATH") or DEFAULT_DB_PATH,
                        limit=MAX_ACTIVE_SESSIONS)
    app.extensions["session_store"] = store
    sessions: dict[str, Session] = {}
    deleted_session_ids: set[str] = set()
    sessions_lock = Lock()
    deleting_all = False

    def session_from_row(row) -> Session:
        return Session(number=row["number"], title=row["title"], titled=bool(row["titled"]),
                       explicit=bool(row["explicit"]), revision=row["revision"])

    # Restore metadata only. Full histories are read only for an operation and
    # released when its last runtime reference finishes.
    for row in store.list_sessions():
        sessions[row["session_id"]] = session_from_row(row)

    def refresh_session(session_id: str, session: Session) -> None:
        row = store.load(session_id)
        if row is None:
            raise SessionDeleted()
        session.title, session.titled = row["title"], bool(row["titled"])
        session.revision = row["revision"]
        session.messages = []
        if row["system_prompt"] is not None:
            session.messages.append({"role": "system", "content": row["system_prompt"]})
        session.messages.extend({"role": item["role"], "content": item["content"]}
                                for item in row["messages"])
        session.answer_languages = [item["language"] for item in row["messages"]
                                    if item["role"] == "assistant"]

    @app.before_request
    def apply_endpoint_upload_limit() -> None:
        if request.path == "/voice/transcribe":
            request.max_content_length = MAX_STT_REQUEST_LENGTH

    @app.teardown_request
    def close_voice_upload(_error: BaseException | None) -> None:
        if request.path == "/voice/transcribe":
            request.close()

    def acquire_session(session_id: str, create: bool = True) -> tuple[Session | None, bool]:
        with sessions_lock:
            if deleting_all:
                raise SessionDeleting()
            if session_id in deleted_session_ids:
                raise SessionDeleted()
            session = sessions.get(session_id)
            if session is not None and session.deleting:
                raise SessionDeleting()
            created = False
            if session is None:
                row = store.load(session_id)
                if row is None:
                    if not create:
                        return None, False
                    row = store.create(session_id)
                    created = True
                session = session_from_row(row)
                sessions[session_id] = session
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
                    and not session.explicit
                    and not session.messages and sessions.get(session_id) is session):
                store.discard_empty_implicit(session_id)
                del sessions[session_id]
            if session.active_requests == 0:
                session.messages = []
                session.answer_languages = []

    def session_summary(session_id: str, session: Session) -> dict[str, Any]:
        return {"session_id": session_id, "number": session.number, "title": session.title}

    def require_live_session(session: Session) -> None:
        # Also check after taking the per-session lock: a queued request may
        # have acquired its reference before Delete marked the session.
        if session.deleted:
            raise SessionDeleted()
        if session.deleting:
            raise SessionDeleting()

    @app.get("/sessions")
    def list_sessions() -> Response:
        with sessions_lock:
            if deleting_all:
                raise SessionDeleting()
            items = [{key: row[key] for key in ("session_id", "number", "title")}
                     for row in store.list_sessions()
                     if row["session_id"] not in sessions or not sessions[row["session_id"]].deleting]
        return jsonify(sessions=items)

    @app.post("/sessions")
    def create_session() -> Response | tuple[Response, int]:
        with sessions_lock:
            if deleting_all:
                raise SessionDeleting()
            if len(sessions) >= MAX_ACTIVE_SESSIONS:
                return error_response(
                    "session_limit_reached",
                    f"활성 세션은 최대 {MAX_ACTIVE_SESSIONS}개까지 사용할 수 있습니다.", 429,
                )
            session_id = f"s-{uuid4().hex}"
            while session_id in sessions or session_id in deleted_session_ids:
                session_id = f"s-{uuid4().hex}"
            session = session_from_row(store.create(session_id, explicit=True))
            sessions[session_id] = session
            summary = session_summary(session_id, session)
        return jsonify(summary), 201

    @app.delete("/sessions")
    def delete_all_sessions() -> Response:
        nonlocal deleting_all
        with sessions_lock:
            # Do not race an individual Delete or another bulk operation.
            if deleting_all or any(session.deleting for session in sessions.values()):
                raise SessionDeleting()
            deleting_all = True
            snapshot = list(sessions.items())
            for _, session in snapshot:
                session.deleting = True
                session.active_requests += 1
        try:
            # Never wait for operation locks while holding sessions_lock.
            # A Chat already running finishes first; queued work is rejected.
            with ExitStack() as locks:
                for _, session in snapshot:
                    locks.enter_context(session.lock)
                deleted_ids = store.delete_all()
                with sessions_lock:
                    deleted_session_ids.update(deleted_ids)
                    sessions.clear()
                    for _, session in snapshot:
                        session.deleted = True
                        session.messages.clear()
                        session.answer_languages.clear()
                        session.title = ""
                        session.titled = False
            return jsonify(status="deleted_all", deleted_count=len(deleted_ids),
                           deleted_session_ids=deleted_ids)
        finally:
            with sessions_lock:
                for _, session in snapshot:
                    if not session.deleted:
                        session.deleting = False
                deleting_all = False
            for session_id, session in snapshot:
                release_session(session_id, session)

    @app.get("/sessions/<session_id>/history")
    def session_history(session_id: str) -> Response | tuple[Response, int]:
        if not session_id.strip() or len(session_id) > MAX_SESSION_ID_LENGTH:
            return error_response("invalid_session_id", "유효한 session_id가 필요합니다.", 400)
        session, _ = acquire_session(session_id, create=False)
        if session is None:
            return error_response("session_not_found", "세션을 찾을 수 없습니다.", 404)
        try:
            with session.lock:
                require_live_session(session)
                refresh_session(session_id, session)
                summary = session_summary(session_id, session)
                answer_index = 0
                messages = []
                for item in session.messages:
                    if item["role"] not in {"user", "assistant"}:
                        continue
                    entry = {"role": item["role"], "content": item["content"]}
                    if item["role"] == "assistant":
                        language = (session.answer_languages[answer_index]
                                    if answer_index < len(session.answer_languages) else None)
                        entry["language"] = language
                        answer_index += 1
                    messages.append(entry)
                return jsonify(**summary, messages=messages)
        finally:
            release_session(session_id, session)

    @app.delete("/sessions/<session_id>")
    def delete_session(session_id: str) -> Response | tuple[Response, int]:
        if not session_id.strip() or len(session_id) > MAX_SESSION_ID_LENGTH:
            return error_response("invalid_session_id", "유효한 session_id가 필요합니다.", 400)
        with sessions_lock:
            if deleting_all:
                raise SessionDeleting()
            session = sessions.get(session_id)
            if session is None:
                return error_response("session_not_found", "세션을 찾을 수 없습니다.", 404)
            if session.deleting:
                raise SessionDeleting()
            session.deleting = True
            session.active_requests += 1  # Pin legacy sessions until removal.
        try:
            # A Chat already owning the lock finishes first. All queued/new
            # operations are rejected; no old request can recreate this ID.
            with session.lock:
                # The Chat holding this lock has now committed or failed.
                row = store.load(session_id)
                if row is None:
                    raise SessionDeleted()
                store.delete(session_id, row["revision"])
                with sessions_lock:
                    deleted_session_ids.add(session_id)
                    sessions.pop(session_id, None)
                    session.deleted = True
                session.messages.clear()
                session.answer_languages.clear()
                session.title = ""
                session.titled = False
            return jsonify(session_id=session_id, status="deleted")
        finally:
            if not session.deleted:
                with sessions_lock:
                    session.deleting = False
            release_session(session_id, session)

    @app.errorhandler(SessionDeleted)
    def deleted_session_error(_error: SessionDeleted) -> tuple[Response, int]:
        return error_response("session_deleted", "삭제된 세션입니다. 새 세션을 생성하세요.", 410)

    @app.errorhandler(SessionDeleting)
    def deleting_session_error(_error: SessionDeleting) -> tuple[Response, int]:
        return error_response("session_deleting", "세션 삭제가 진행 중입니다.", 409)

    def error_response(code: str, message: str, status: int) -> tuple[Response, int]:
        return jsonify(error={"code": code, "message": message}), status

    @app.errorhandler(StorageError)
    def storage_error(error: StorageError) -> tuple[Response, int]:
        # Storage exceptions contain only fixed, public messages, never paths,
        # SQL, transcript content or the underlying exception text.
        return error_response(error.code, str(error), error.status)

    def server_timing_header(**durations: float) -> str:
        return ", ".join(
            f"{name};dur={max(0.0, seconds) * 1000:.3f}"
            for name, seconds in durations.items()
        )

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
        response_language = data.get("response_language")
        if "response_language" in data and (
                not isinstance(response_language, str)
                or response_language not in RESPONSE_LANGUAGE_PROMPTS):
            return error_response(
                "invalid_response_language", "response_language must be ko or en.", 400,
            )

        started = time.perf_counter()
        session, created = acquire_session(session_id)
        assert session is not None
        committed = False
        try:
            # Keep a chat and a concurrent reset on the same session ordered.
            with session.lock:
                require_live_session(session)
                refresh_session(session_id, session)
                messages = session.messages or [
                    {"role": "system", "content": ai_controller.SYSTEM_PROMPT},
                ]
                if response_language is not None:
                    # Replace the existing system instruction without mutating stored
                    # history until the request succeeds. Never accumulate prompts.
                    messages = [
                        {"role": "system", "content": RESPONSE_LANGUAGE_PROMPTS[response_language]},
                        *[item for item in messages if item["role"] != "system"],
                    ]
                user_message = {"role": "user", "content": message}
                request_messages = messages
                if request.headers.get("X-Local-AI-Response-Style") == "voice":
                    request_messages = [
                        {"role": "system", "content": messages[0]["content"] + VOICE_RESPONSE_STYLE},
                        *messages[1:],
                    ]
                request_user_message = user_message
                if response_language is not None:
                    # Imported models may use {{ .Prompt }} and omit system roles.
                    # Reinforce the selected output language in the current prompt
                    # without storing these instructions as conversation history.
                    instruction = RESPONSE_LANGUAGE_TURN_INSTRUCTIONS[response_language]
                    if request.headers.get("X-Local-AI-Response-Style") == "voice":
                        instruction += VOICE_RESPONSE_STYLE
                    request_user_message = {
                        "role": "user",
                        "content": f"{message}\n\n[Response requirements]\n{instruction}",
                    }
                ollama_started = time.perf_counter()
                result = ai_controller.ask_ollama(request_messages + [request_user_message])
                ollama_seconds = time.perf_counter() - ollama_started
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
                title = session.title
                if not session.titled:
                    first = message.strip().replace("\r", " ").replace("\n", " ")
                    title = first[:SESSION_TITLE_LENGTH] + ("..." if len(first) > SESSION_TITLE_LENGTH else "")
                store.commit_turn(session_id, session.revision, user=message,
                                  answer=answer["content"], title=title,
                                  system_prompt=messages[0]["content"], language=response_language)
                session.revision += 1
                session.title, session.titled = title, True
                session.messages = messages + [user_message, answer]
                session.answer_languages.append(response_language)
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
        elapsed_seconds = time.perf_counter() - started
        response = jsonify(
            session_id=session_id, model=ai_controller.MODEL,
            message=answer["content"], elapsed_seconds=elapsed_seconds,
            metrics=metrics,
        )
        response.headers["Server-Timing"] = server_timing_header(
            ollama=ollama_seconds,
            total=elapsed_seconds,
        )
        return response, 200

    @app.post("/voice/transcribe")
    def transcribe_voice() -> tuple[Response, int]:
        started = time.perf_counter()
        language = request.form.get("language", "auto")
        if language not in stt_service.ALLOWED_LANGUAGES:
            return error_response(
                "invalid_stt_language", "language must be one of: auto, ko, en.", 400,
            )
        audio = request.files.get("audio")
        if audio is None:
            return error_response("audio_missing", "The audio field is required.", 400)
        try:
            audio_bytes = audio.stream.read(MAX_STT_AUDIO_BYTES + 1)
        finally:
            audio.close()
        upload_read_seconds = time.perf_counter() - started
        if not audio_bytes:
            return error_response("audio_empty", "The audio file is empty.", 400)
        if len(audio_bytes) > MAX_STT_AUDIO_BYTES:
            return error_response(
                "audio_too_large", "The audio file must not exceed 10 MiB.", 413,
            )
        try:
            result = stt_service.transcribe_audio(audio_bytes, language)
        except stt_service.SttBusyError:
            return error_response(
                "stt_busy", "Another speech recognition request is running.", 429,
            )
        except stt_service.SttAudioTooLongError:
            return error_response(
                "audio_too_long", "Decoded audio must not exceed 30 seconds.", 413,
            )
        except stt_service.SttAudioError:
            return error_response(
                "invalid_audio", "Audio must be a valid WAV, M4A, or WebM file.", 415,
            )
        except stt_service.SttModelMissingError:
            return error_response(
                "stt_model_missing", "The local speech model is unavailable.", 503,
            )
        except stt_service.SttInitializationError:
            return error_response(
                "stt_initialization_failed", "Speech recognition could not initialize.", 503,
            )
        except stt_service.SttInferenceError:
            return error_response(
                "stt_inference_failed", "Speech recognition failed.", 500,
            )
        except stt_service.SttEmptyResultError:
            return error_response(
                "stt_empty_result", "No speech could be recognized.", 422,
            )
        elapsed_seconds = time.perf_counter() - started
        response = jsonify(
            text=result.text,
            language=result.language,
            elapsed_seconds=elapsed_seconds,
        )
        response.headers["Server-Timing"] = server_timing_header(
            upload=upload_read_seconds,
            decode=result.decode_seconds,
            stt_model=result.model_load_seconds,
            whisper=result.inference_seconds,
            total=elapsed_seconds,
        )
        return response, 200

    @app.post("/voice/synthesize")
    def synthesize_voice() -> Response | tuple[Response, int]:
        started = time.perf_counter()
        if not request.is_json:
            return error_response(
                "unsupported_media_type", "Content-Type must be application/json.", 415,
            )
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return error_response("invalid_json", "A valid JSON object is required.", 400)
        if "text" not in data:
            return error_response("text_missing", "The text field is required.", 400)
        text = data["text"]
        if not isinstance(text, str):
            return error_response("invalid_tts_text", "text must be a string.", 400)
        if not text.strip():
            return error_response("text_empty", "text must not be empty.", 400)
        if len(text) > MAX_TTS_TEXT_LENGTH:
            return error_response(
                "text_too_long", f"text must not exceed {MAX_TTS_TEXT_LENGTH} characters.", 413,
            )
        language = data.get("language")
        if not isinstance(language, str) or language not in tts_service.VOICE_NAMES:
            return error_response(
                "invalid_tts_language", "language must be one of: ko, en.", 400,
            )
        try:
            result = tts_service.synthesize_text(text, language)
        except tts_service.TtsBusyError:
            return error_response(
                "tts_busy", "Another speech synthesis request is running.", 429,
            )
        except tts_service.TtsModelMissingError:
            return error_response(
                "tts_model_missing", "The local voice model is unavailable.", 503,
            )
        except tts_service.TtsInitializationError:
            return error_response(
                "tts_initialization_failed", "Speech synthesis could not initialize.", 503,
            )
        except tts_service.TtsInferenceError:
            return error_response("tts_inference_failed", "Speech synthesis failed.", 500)
        response = Response(result.audio, status=200, mimetype="audio/wav")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Length"] = str(len(result.audio))
        response.headers["Server-Timing"] = server_timing_header(
            piper_model=result.model_load_seconds,
            piper=result.synthesis_seconds,
            total=time.perf_counter() - started,
        )
        return response

    @app.post("/sessions/<session_id>/reset")
    def reset(session_id: str) -> Response | tuple[Response, int]:
        if not session_id.strip() or len(session_id) > MAX_SESSION_ID_LENGTH:
            return error_response("invalid_session_id", f"session_id는 {MAX_SESSION_ID_LENGTH}자 이하의 문자열이어야 합니다.", 400)
        session, _ = acquire_session(session_id, create=False)
        if session is not None:
            reset_committed = False
            try:
                with session.lock:
                    require_live_session(session)
                    refresh_session(session_id, session)
                    store.reset(session_id, session.revision)
                    reset_committed = True
                    session.revision += 1
                    session.messages.clear()
                    session.answer_languages.clear()
                    session.title = "새 대화"
                    session.titled = False
            finally:
                release_session(session_id, session, reset=reset_committed)
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

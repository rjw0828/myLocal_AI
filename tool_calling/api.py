"""Loopback-only job APIs. Existing /chat and manual /mcp APIs are unchanged."""
from flask import Blueprint, jsonify, request
from mcp_manager.errors import MCPServiceError
from mcp_manager.models import MCPRuntimeError, MCPTimeoutError
from .models import ToolChatError


def install_tool_chat(app, backend, *, guard):
    api = Blueprint("tool_chat", __name__, url_prefix="/tool-chat")
    api.before_request(guard)

    @api.after_request
    def private(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def body(required, optional=()):
        if not request.is_json:
            raise MCPServiceError("unsupported_media_type", "JSON is required.", 415)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not set(required) <= set(data) or set(data) - set(required) - set(optional):
            raise ToolChatError("invalid_tool_chat_input")
        return data

    @api.post("/jobs")
    def create():
        data = body(("session_id", "message", "browser_generation"), ("response_language",))
        for name, maximum in (("session_id", 128), ("message", 10000)):
            value = data[name]
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ToolChatError("invalid_tool_chat_input")
        if type(data["browser_generation"]) is not int or data["browser_generation"] < 0:
            raise ToolChatError("invalid_tool_chat_input")
        language = data.get("response_language")
        if "response_language" in data and (not isinstance(language, str) or language not in {"ko", "en"}):
            raise ToolChatError("invalid_tool_chat_input")
        job_id = backend.create(data["session_id"], data["message"], language, data["browser_generation"])
        return jsonify(job=backend.status(job_id)), 202

    @api.get("/jobs/<job_id>")
    def status(job_id):
        return jsonify(job=backend.status(job_id))

    @api.get("/jobs/<job_id>/approval")
    def approval(job_id):
        job = backend.status(job_id)
        return jsonify(job_id=job_id, state=job["state"], approval=job["pending"])

    @api.post("/jobs/<job_id>/approval")
    def approve(job_id):
        data = body(("proposal_id", "browser_generation", "allow"), ("scope",))
        if (not isinstance(data["proposal_id"], str) or type(data["browser_generation"]) is not int
                or type(data["allow"]) is not bool):
            raise ToolChatError("invalid_tool_chat_input")
        backend.approve(job_id, data["proposal_id"], data["browser_generation"], data["allow"], data.get("scope"))
        return jsonify(job=backend.status(job_id)), 202

    @api.post("/jobs/<job_id>/cancel")
    def cancel(job_id):
        body(())
        backend.cancel(job_id)
        return jsonify(job=backend.status(job_id))

    @api.post("/jobs/<job_id>/reconcile")
    def reconcile(job_id):
        body(())
        return jsonify(job=backend.reconcile(job_id))

    @app.errorhandler(ToolChatError)
    def core_error(error):
        code = error.code
        status_code = (504 if code == "job_timeout" else
                       503 if code == "service_closed" else
                       404 if code in {"job_not_found", "session_not_found"} else
                       429 if code == "job_capacity" else
                       409 if code in {"session_reserved", "session_unsettled", "job_busy", "stale_approval", "job_ended", "session_changed", "operation_unresolved"} else 400)
        return jsonify(error={"code": code, "message": "Tool Chat request could not be accepted: " + code}), status_code

    @api.errorhandler(MCPServiceError)
    def mcp_error(error):
        return jsonify(error={"code": error.code, "message": "MCP policy could not be verified."}), error.status

    @api.errorhandler(MCPRuntimeError)
    def runtime_error(error):
        status_code = 504 if isinstance(error, MCPTimeoutError) else 409 if error.code == "mcp_not_ready" else 503
        return jsonify(error={"code": error.code, "message": "MCP discovery could not finish safely."}), status_code

    app.register_blueprint(api)

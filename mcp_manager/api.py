"""Local-only Flask adapter; no UI and no automatic LLM tool calls."""
import ipaddress
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

from .errors import MCPServiceError
from .models import MCPRuntimeError, MCPTimeoutError
from .service import MCPService


def install_mcp(app, *, session_database_path, control_guard, database_path=None,
                approved_configs=None, runtime_factory=None):
    service = MCPService(session_database_path=session_database_path, database_path=database_path,
                         approved_configs=approved_configs, runtime_factory=runtime_factory)
    app.extensions["mcp_service"] = service
    api = Blueprint("mcp", __name__, url_prefix="/mcp")

    @api.before_request
    def local_request_only():
        denied = control_guard()
        if denied:
            return denied
        try:
            address = ipaddress.ip_address(request.remote_addr or "")
            local = address.is_loopback or bool(getattr(address, "ipv4_mapped", None)
                                               and address.ipv4_mapped.is_loopback)
        except ValueError:
            local = False
        host = urlsplit(request.host_url)
        if not local or host.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise MCPServiceError("mcp_local_only", "MCP management is restricted to loopback clients and hosts.", 403)
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme, parsed.netloc) != (host.scheme, host.netloc) or parsed.path or parsed.query or parsed.fragment:
                raise MCPServiceError("mcp_origin_denied", "MCP management requires the same origin.", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise MCPServiceError("mcp_origin_denied", "Cross-site MCP management is not permitted.", 403)

    @api.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.errorhandler(MCPServiceError)
    def service_error(error):
        return jsonify(error={"code": error.code, "message": str(error)}), error.status

    @api.errorhandler(MCPRuntimeError)
    def runtime_error(error):
        code = error.code
        status = (504 if isinstance(error, MCPTimeoutError) else
                  404 if code in {"mcp_not_registered", "mcp_operation_not_found"} else
                  429 if code == "mcp_busy" else
                  409 if code in {"mcp_not_ready", "mcp_server_owned", "mcp_config_conflict"} else
                  502 if code in {"mcp_connection_failed", "mcp_call_uncertain"} else 503)
        body = {"code": code, "message": str(error)}
        if error.operation_id:
            body["operation_id"] = error.operation_id
        return jsonify(error=body), status

    @api.errorhandler(ValueError)
    def invalid_value(_error):
        return jsonify(error={"code": "mcp_invalid_input", "message": "Invalid MCP input."}), 400

    def body(required=(), optional=()):
        if not request.is_json:
            raise MCPServiceError("unsupported_media_type", "A JSON request is required.", 415)
        data = request.get_json(silent=True)
        if (not isinstance(data, dict) or not set(required) <= set(data)
                or set(data) - set(required) - set(optional)):
            raise MCPServiceError("mcp_invalid_input", "A valid JSON object with the documented fields is required.")
        return data

    def text(value, limit=128):
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
            raise MCPServiceError("mcp_invalid_input", "A nonempty string within the length limit is required.")
        return value

    @api.get("/profiles")
    def profiles():
        return jsonify(profiles=[{"profile_id": key} for key in service.profiles()])

    @api.route("/servers", methods=["GET", "POST"])
    def servers():
        if request.method == "GET":
            return jsonify(servers=service.list_servers())
        data = body(required=("profile_id",), optional=("name",))
        profile_id = text(data["profile_id"])
        return jsonify(server=service.create_server(profile_id, text(data.get("name", profile_id)))), 201

    @api.route("/servers/<server_id>", methods=["GET", "DELETE"])
    def server(server_id):
        if request.method == "GET":
            return jsonify(server=service.get_server(server_id))
        service.delete_server(server_id)
        return jsonify(server_id=server_id, status="deleted")

    @api.post("/servers/<server_id>/start")
    def start(server_id):
        body()
        return jsonify(server=service.start_server(server_id))

    @api.post("/servers/<server_id>/stop")
    def stop(server_id):
        body()
        return jsonify(server=service.stop_server(server_id))

    @api.get("/servers/<server_id>/status")
    def status(server_id):
        return jsonify(server=service.get_server(server_id))

    @api.get("/servers/<server_id>/tools")
    def tools(server_id):
        return jsonify(tools=service.list_tools(server_id))

    @api.get("/servers/<server_id>/permissions")
    def permissions(server_id):
        return jsonify(permissions=service.permissions(server_id),
                       validation="Live tool/schema match is required before execution.")

    @api.put("/servers/<server_id>/tools/<path:tool_name>/permission")
    def permission(server_id, tool_name):
        data = body(required=("allowed",))
        if not isinstance(data["allowed"], bool):
            raise MCPServiceError("mcp_invalid_input", "allowed must be a boolean.")
        return jsonify(service.set_permission(server_id, text(tool_name, 256), data["allowed"]))

    @api.post("/servers/<server_id>/tools/<path:tool_name>/approve")
    def approve(server_id, tool_name):
        data = body(optional=("arguments",))
        return jsonify(service.approve_call(server_id, text(tool_name, 256), data.get("arguments", {})))

    @api.post("/servers/<server_id>/tools/<path:tool_name>/call")
    def call(server_id, tool_name):
        data = body(required=("approval_token",), optional=("arguments",))
        return jsonify(service.call_tool(server_id, text(tool_name, 256), data.get("arguments", {}),
                                         text(data["approval_token"], 256)))

    @api.get("/servers/<server_id>/operations/<operation_id>")
    def operation(server_id, operation_id):
        return jsonify(operation=service.operation(server_id, operation_id))

    app.register_blueprint(api)
    return service

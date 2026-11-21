"""Loopback-only job APIs. Existing /chat and manual /mcp APIs are unchanged."""
from flask import Blueprint, jsonify, request
from mcp_manager.errors import MCPServiceError
from mcp_manager.models import MCPRuntimeError, MCPTimeoutError
from .web_contract import ToolChatError


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

    # Management mutations use the existing local-owner facade.  Connection
    # testing starts and discovers a server but never calls a Tool.
    @api.post('/management/servers/<server_id>/connect')
    def management_connect(server_id):
        data=body(('confirmed',))
        if data['confirmed'] is not True:raise ToolChatError('invalid_tool_chat_input')
        return jsonify(result=backend.manager.test_connection(server_id,confirm=lambda _:True))

    @api.post('/management/servers/<server_id>/stop')
    def management_stop(server_id):
        body(())
        return jsonify(server=backend.manager.stop_server(server_id))

    @api.delete('/management/servers/<server_id>')
    def management_delete(server_id):
        backend.manager.delete_server(server_id)
        return jsonify(server_id=server_id,status='deleted')

    @api.put('/management/servers/<server_id>/tools/<path:tool>/permission')
    def management_permission(server_id,tool):
        data=body(('allowed',))
        if type(data['allowed']) is not bool:raise ToolChatError('invalid_tool_chat_input')
        return jsonify(backend.manager.set_permission(server_id,tool,data['allowed']))

    @api.get('/policy/<server_id>')
    def policy_view(server_id):
        manager=backend.manager
        tools=[]
        for entry in manager.catalog.discover():
            if entry.server_id==server_id:
                tools.append({'name':entry.original_name,'catalog_fingerprint':entry.identity,
                    'input_schema':entry.descriptor['input_schema']})
        return jsonify(server=manager.details(server_id),tools=tools,
            limitation='Execution configuration changes require stop, delete and registration of a reviewed profile.')

    @api.put('/policy/<server_id>/mode')
    def policy_mode(server_id):
        data=body(('mode','enable_auto'))
        if type(data['enable_auto']) is not bool:raise ToolChatError('invalid_tool_chat_input')
        backend.manager.set_mode(data['mode'],server_id=server_id,enable_auto=data['enable_auto'])
        if not data['enable_auto']:backend.manager.policy.disable_auto()
        return policy_view(server_id)

    @api.post('/policy/<server_id>/trust')
    def trust(server_id):
        data=body(('tool','catalog_fingerprint','confirmed','verified_read_only'),('resource',))
        if data['confirmed'] is not True or type(data['verified_read_only']) is not bool:
            raise ToolChatError('invalid_tool_chat_input')
        entry=backend.manager.entry(server_id,data['tool'])
        if data['catalog_fingerprint']!=entry.identity:raise ToolChatError('stale_approval')
        resource=data.get('resource')
        if resource is not None and (not isinstance(resource,str) or not resource.strip() or len(resource)>32767):
            raise ToolChatError('invalid_tool_chat_input')
        tid=backend.manager.trust(server_id,data['tool'],resource=resource,
            verified_read_only=data['verified_read_only'],
            confirm=lambda r:r['catalog_fingerprint']==data['catalog_fingerprint'])
        return jsonify(trust_id=tid)

    @api.delete('/policy/<server_id>/trust/<trust_id>')
    def revoke(server_id,trust_id):
        if trust_id not in {r['trust_id'] for r in backend.manager.details(server_id)['trusts']}:
            raise ToolChatError('stale_approval')
        backend.manager.revoke_trust(trust_id)
        return jsonify(revoked=True)

    @api.post('/policy/<server_id>/decision')
    def decision(server_id):
        data=body(('tool','arguments'))
        return jsonify(decision=backend.manager.decision(server_id,data['tool'],data['arguments']))

    from .approval_core import ApprovalError
    from .approval_policy import PolicyError
    from .mcp_management import ManagementError
    @api.errorhandler(ManagementError)
    def management_error(error):
        return jsonify(error={'code':'tool_blocked_or_unavailable','message':'The current server/tool is unavailable or not allowed.'}),409
    @api.errorhandler(ApprovalError)
    def approval_error(error):
        return jsonify(error={'code':error.code,'message':'Approval is no longer valid; check job status.'}),409
    @api.errorhandler(PolicyError)
    @api.errorhandler(ValueError)
    def invalid_policy(error):
        return jsonify(error={'code':'invalid_policy','message':'Policy settings could not be accepted.'}),400

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

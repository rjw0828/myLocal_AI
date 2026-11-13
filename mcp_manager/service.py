"""Registry/runtime coordination and explicit, one-use manual-call approval."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
from threading import Lock, RLock
import time

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry as ReferenceRegistry

from .errors import MCPServiceError
from .manager import MCPManager
from .models import MCPRuntimeError, ServerConfig, ServerState, ServerStatus
from .registry import DEFAULT_MCP_DB_PATH, MCPRegistry, canonical_json, config_data, fingerprint
from .profile_requests import ProfileRequests


def public_status(status):
    data = asdict(status)
    if data["last_operation"] is not None:
        data["last_operation"].pop("result", None)
    return data


class MCPService:
    """Single Controller process, independent DB, no startup process launch.

    Approvals are trusted local configuration, never supplied by an HTTP caller.
    SQLite stores registrations/grants only. Tokens, outcomes and runtime state
    disappear on restart. The registry and loop are both lazy.
    """

    def __init__(self, *, session_database_path, database_path=None,
                 approved_configs=None, runtime_factory=None):
        self.database_path = Path(database_path or os.environ.get("LOCAL_AI_MCP_DB_PATH")
                                  or DEFAULT_MCP_DB_PATH).resolve()
        self.session_database_path = Path(session_database_path).resolve()
        self._approved_configs = approved_configs
        self._profiles = None
        self._registry = None
        self._runtime = None
        self._runtime_factory = runtime_factory or MCPManager
        self._registered = set()
        self._lock, self._shutdown_lock = RLock(), Lock()
        self._gates, self._tokens, self._bindings = {}, {}, {}
        self._actions, self._stopping = {}, set()
        self._permission_generations = {}
        self._closing = False
        self.clock = time.monotonic

    def _open(self):
        if self._closing:
            raise MCPServiceError("mcp_service_closed", "MCP service is shutting down.", 503)

    @property
    def registry(self):
        with self._lock:
            self._open()
            if self._registry is None:
                self._registry = MCPRegistry(self.database_path, protected_path=self.session_database_path)
            return self._registry

    def profiles(self):
        with self._lock:
            self._open()
            if self._profiles is not None:
                return self._profiles
            configs = self._approved_configs
            try:
                if configs is None:
                    manifest = os.environ.get("LOCAL_AI_MCP_APPROVALS")
                    if not manifest:
                        local_manifest = self.database_path.parent / "mcp_approved_profiles.json"
                        if local_manifest.exists():
                            manifest = str(local_manifest)
                    entries = []
                    if manifest:
                        path = Path(manifest)
                        if path.stat().st_size > 1024 * 1024:
                            raise ValueError()
                        contents = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(contents, dict) or set(contents) != {"profiles"}:
                            raise ValueError()
                        entries = contents["profiles"]
                    if not isinstance(entries, list):
                        raise ValueError()
                    configs = []
                    for item in entries:
                        if (not isinstance(item, dict) or not {"profile_id", "executable"} <= set(item)
                                or set(item) - {"profile_id", "executable", "args", "cwd"}):
                            raise ValueError()
                        configs.append(ServerConfig(item["profile_id"], item["executable"],
                                                    item.get("args", ()), item.get("cwd")))
                profiles = {}
                for config in configs:
                    if not isinstance(config, ServerConfig) or config.server_id in profiles:
                        raise ValueError()
                    profiles[config.server_id] = config
            except (OSError, ValueError, TypeError, KeyError):
                raise MCPServiceError("mcp_approval_config_invalid", "Local MCP execution approvals are invalid.", 503) from None
            self._profiles = profiles
            return profiles

    def _approved(self, row):
        profile = self.profiles().get(row["profile_id"])
        try:
            return (profile is not None and fingerprint(config_data(profile)) == row["config_hash"]
                    and fingerprint(row["config"]) == row["config_hash"])
        except (ValueError, TypeError, RecursionError):
            return False

    def _require_approved(self, row):
        if not self._approved(row):
            raise MCPServiceError("mcp_execution_not_approved", "This local execution profile is not approved.", 403)

    def _status(self, server_id):
        with self._lock:
            if server_id in self._registered:
                return self._runtime.get_status(server_id)
            return ServerStatus(server_id, ServerState.STOPPED)

    def _view(self, row):
        return {key: row[key] for key in ("server_id", "profile_id", "name", "created_at")} | {
            "transport": "stdio", "execution_approved": self._approved(row),
            "status": public_status(self._status(row["server_id"])),
        }

    def list_servers(self):
        return [self._view(row) for row in self.registry.list_servers()]

    def get_server(self, server_id):
        return self._view(self.registry.get(server_id))

    def create_server(self, profile_id, name):
        profile = self.profiles().get(profile_id)
        if profile is None:
            raise MCPServiceError("mcp_execution_not_approved", "Select an approved local execution profile.", 403)
        return self._view(self.registry.create(profile_id, name, config_data(profile)))

    def profile_requests(self):
        self._open()
        if self._approved_configs is not None:
            raise MCPServiceError("mcp_profile_management_disabled", "This Controller uses fixed in-memory profiles.", 403)
        return ProfileRequests(self.database_path.parent / "mcp_profile_requests",
                               os.environ.get("LOCAL_AI_MCP_APPROVALS") or self.database_path.parent / "mcp_approved_profiles.json")

    def refresh_profiles(self):
        with self._lock:
            if self._approved_configs is None:
                self._profiles = None
            return self.profiles()

    def propose_profile(self, name, transport, executable, arguments):
        requests = self.profile_requests()
        with self._lock:
            return requests.view(requests.create(name, transport, executable, arguments), False)

    def profile_request_status(self, request_id):
        requests = self.profile_requests()
        record = requests.read(request_id)
        with self._lock:
            self._profiles = None  # Explicitly reload only owner-written approvals; never trust a web proposal.
            profile = self.profiles().get(record["profile_id"])
            approved = profile is not None and config_data(profile) == {key: record[key] for key in ("executable", "args", "cwd")}
        return requests.view(record, approved)

    @contextmanager
    def _guard(self, server_id, kind="action"):
        self.registry.get(server_id)  # Unknown IDs do not allocate locks.
        with self._lock:
            self._open()
            if server_id in self._stopping:
                raise MCPServiceError("mcp_busy", "MCP server is stopping.", 429)
            gate = self._gates.setdefault(server_id, Lock())
            if not gate.acquire(blocking=False):
                raise MCPServiceError("mcp_busy", "Another MCP action is in progress for this server.", 429)
            self._actions[server_id] = kind
        try:
            yield self.registry.get(server_id)
        finally:
            with self._lock:
                self._actions.pop(server_id, None)
                gate.release()

    def _clear_tokens(self, server_id, tool_name=None):
        with self._lock:
            for key, value in list(self._tokens.items()):
                if value["server_id"] == server_id and (tool_name is None or value["tool_name"] == tool_name):
                    del self._tokens[key]

    def start_server(self, server_id):
        with self._guard(server_id, "start") as row:
            self._require_approved(row)
            profile = self.profiles()[row["profile_id"]]
            try:
                config = ServerConfig(server_id, profile.executable, profile.args, profile.cwd)
            except (ValueError, OSError):
                raise MCPServiceError("mcp_execution_unavailable", "Approved local executable is unavailable.", 503) from None
            with self._lock:
                self._open()
                if self._runtime is None:
                    self._runtime = self._runtime_factory()
                self._runtime.register_server(config)
                self._registered.add(server_id)
            self._clear_tokens(server_id)
            self._runtime.start_server(server_id)
            return self._view(row)

    def stop_server(self, server_id):
        self.registry.get(server_id)
        with self._lock:
            self._open()
            if server_id in self._stopping or self._actions.get(server_id) in {"start", "delete"}:
                raise MCPServiceError("mcp_busy", "MCP lifecycle action is already in progress.", 429)
            self._stopping.add(server_id)
        try:
            row = self.registry.get(server_id)
            self._clear_tokens(server_id)
            if server_id in self._registered:
                self._runtime.stop_server(server_id)
            return self._view(row)
        finally:
            # An already admitted Approve may have issued a token while Stop
            # drained its discovery. Invalidate those tokens as well.
            self._clear_tokens(server_id)
            with self._lock:
                self._stopping.discard(server_id)

    def delete_server(self, server_id):
        with self._guard(server_id, "delete"):
            if self._status(server_id).state != ServerState.STOPPED:
                raise MCPServiceError("mcp_server_not_stopped", "Explicitly stop the server before deleting it.", 409)
            with self._lock:
                if server_id in self._registered:
                    self._runtime.unregister_server(server_id)
                    self._registered.remove(server_id)
            # If SQL fails, the registration/grants remain, stopped. A future
            # Start can safely register its unchanged config again.
            self.registry.delete(server_id)
            self._clear_tokens(server_id)
            with self._lock:
                self._bindings.pop(server_id, None)
                self._gates.pop(server_id, None)

    def _tools(self, row, timeout=None):
        self._require_approved(row)
        if row["server_id"] not in self._registered:
            raise MCPRuntimeError("mcp_not_ready", "MCP server is stopped.")
        return self._runtime.list_tools(row["server_id"], refresh=True, timeout=timeout)

    @staticmethod
    def _schema_hash(tool):
        try:
            return fingerprint(asdict(tool))
        except (ValueError, TypeError, RecursionError):
            raise MCPServiceError("mcp_tool_schema_invalid", "MCP tool schema is invalid.", 502) from None

    def _tool(self, row, name, timeout=None):
        for tool in self._tools(row, timeout=timeout):
            if tool.name == name:
                return tool
        raise MCPServiceError("mcp_tool_not_found", "MCP tool was not found.", 404)

    def _permission(self, server_id, name, schema_hash):
        grant = self.registry.permission(server_id, name)
        return bool(grant and grant["allowed"] and grant["schema_hash"] == schema_hash)

    def list_tools(self, server_id, *, timeout=None):
        with self._guard(server_id) as row:
            return [asdict(tool) | {"schema_hash": self._schema_hash(tool),
                    "allowed": self._permission(server_id, tool.name, self._schema_hash(tool))}
                    for tool in self._tools(row, timeout=timeout)]

    def set_permission(self, server_id, name, allowed):
        with self._guard(server_id) as row:
            schema_hash = self._schema_hash(self._tool(row, name)) if allowed else ""
            self.registry.set_permission(server_id, name, schema_hash, allowed)
            with self._lock:
                key = (server_id, name)
                self._permission_generations[key] = self._permission_generations.get(key, 0) + 1
            self._clear_tokens(server_id, name)
            return {"tool_name": name, "allowed": allowed, "schema_hash": schema_hash}

    def permissions(self, server_id):
        return [row | {"allowed": bool(row["allowed"])} for row in self.registry.list_permissions(server_id)]

    def _authorized_tool(self, row, name, timeout=None):
        tool = self._tool(row, name, timeout=timeout)
        schema_hash = self._schema_hash(tool)
        if not self._permission(row["server_id"], name, schema_hash):
            raise MCPServiceError("mcp_tool_permission_required", "This tool/schema has not been permitted.", 403)
        return tool, schema_hash

    @staticmethod
    def _arguments(tool, arguments):
        if not isinstance(arguments, dict):
            raise MCPServiceError("mcp_invalid_arguments", "Tool arguments must be a JSON object.")
        try:
            encoded = canonical_json(arguments)
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise ValueError()
            arguments = json.loads(encoded)

            def local_refs_only(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                            raise MCPServiceError("mcp_tool_schema_unsupported", "External schema references are not supported.", 502)
                        local_refs_only(item)
                elif isinstance(value, list):
                    for item in value:
                        local_refs_only(item)

            local_refs_only(tool.input_schema)
            Draft202012Validator.check_schema(tool.input_schema)
            # An empty referencing registry cannot download external schemas.
            Draft202012Validator(tool.input_schema, registry=ReferenceRegistry()).validate(arguments)
            return arguments
        except ValidationError:
            raise MCPServiceError("mcp_invalid_arguments", "Tool arguments do not match the input schema.") from None
        except MCPServiceError:
            raise
        except (TypeError, ValueError, RecursionError):
            raise MCPServiceError("mcp_invalid_arguments", "Tool arguments must be finite JSON up to 64 KiB.") from None
        except Exception:
            raise MCPServiceError("mcp_tool_schema_invalid", "MCP tool schema could not be validated.", 502) from None

    def approve_call(self, server_id, name, arguments):
        with self._guard(server_id) as row:
            tool, schema_hash = self._authorized_tool(row, name)
            arguments = self._arguments(tool, arguments)
            token = secrets.token_urlsafe(32)
            with self._lock:
                self._open()
                now = self.clock()
                self._tokens = {key: value for key, value in self._tokens.items() if value["expires"] > now}
                if len(self._tokens) >= 256:
                    raise MCPServiceError("mcp_approval_busy", "Too many pending MCP approvals.", 429)
                self._tokens[fingerprint(token)] = {"server_id": server_id, "tool_name": name,
                    "schema_hash": schema_hash, "arguments_hash": fingerprint(arguments), "expires": now + 120}
            return {"approval_token": token, "expires_in_seconds": 120, "server_id": server_id, "tool_name": name}

    def _consume(self, server_id, name, schema_hash, arguments, token):
        with self._lock:
            record = self._tokens.pop(fingerprint(token), None)
            valid = (record is not None and record["expires"] > self.clock()
                     and record["server_id"] == server_id and record["tool_name"] == name
                     and secrets.compare_digest(record["schema_hash"], schema_hash)
                     and secrets.compare_digest(record["arguments_hash"], fingerprint(arguments)))
            if not valid:
                raise MCPServiceError("mcp_call_approval_required", "A current one-use approval for these exact arguments is required.", 403)

    def _bind(self, server_id, operation_id, name, schema_hash):
        with self._lock:
            records = self._bindings.setdefault(server_id, OrderedDict())
            records[operation_id] = (name, schema_hash)
            while len(records) > 32:
                records.popitem(last=False)

    def _call_context(self, row, tool, schema_hash):
        """Internal Python adapter data, never added to public HTTP responses."""
        server_id = row["server_id"]
        if self._status(server_id).state != ServerState.RUNNING:
            raise MCPRuntimeError("mcp_not_ready", "MCP server is not running.")
        with self._lock:
            permission_generation = self._permission_generations.get((server_id, tool.name), 0)
        return {"server_id": server_id, "config_hash": row["config_hash"],
                "config": json.loads(canonical_json(row["config"])),
                "connection_generation": self._runtime.connection_generation(server_id),
                "connection_info": self._runtime.connection_info(server_id),
                "permission_generation": permission_generation,
                "schema_hash": schema_hash, "tool": asdict(tool)}

    def inspect_tool(self, server_id, name, *, timeout=None):
        with self._guard(server_id) as row:
            self.refresh_profiles()
            tool, schema_hash = self._authorized_tool(row, name, timeout=timeout)
            return self._call_context(row, tool, schema_hash)

    def inspect_server(self, server_id):
        """Trusted backend launch metadata, not a public management response."""
        with self._guard(server_id) as row:
            self.refresh_profiles()
            self._require_approved(row)
            if self._status(server_id).state != ServerState.RUNNING:
                raise MCPRuntimeError("mcp_not_ready", "MCP server is not running.")
            return {"server_id": server_id, "config_hash": row["config_hash"],
                    "config": json.loads(canonical_json(row["config"])),
                    "connection_generation": self._runtime.connection_generation(server_id),
                    "connection_info": self._runtime.connection_info(server_id)}

    def revoke_call_approval(self, token):
        with self._lock:
            self._tokens.pop(fingerprint(token), None)

    def call_tool(self, server_id, name, arguments, token, *, before_execute=None):
        with self._guard(server_id) as row:
            if before_execute is not None:
                self.refresh_profiles()
            tool, schema_hash = self._authorized_tool(row, name)
            arguments = self._arguments(tool, arguments)
            if before_execute is not None:
                before_execute(self._call_context(row, tool, schema_hash), arguments)
            self._consume(server_id, name, schema_hash, arguments, token)
            try:
                result = self._runtime.call_tool(server_id, name, arguments)
            except MCPRuntimeError as error:
                if error.operation_id:
                    self._bind(server_id, error.operation_id, name, schema_hash)
                raise
            outcome = self._runtime.get_status(server_id).last_operation
            self._bind(server_id, outcome.operation_id, name, schema_hash)
            return {"server_id": server_id, "operation_id": outcome.operation_id,
                    "result": asdict(result), "elapsed_seconds": outcome.elapsed_seconds}

    def operation(self, server_id, operation_id):
        row = self.registry.get(server_id)
        if server_id not in self._registered:
            raise MCPRuntimeError("mcp_operation_not_found", "No runtime operation is retained for this server.")
        outcome = self._runtime.get_operation(server_id, operation_id)
        result = asdict(outcome)
        result.pop("result", None)
        with self._lock:
            binding = self._bindings.get(server_id, {}).get(operation_id)
        if binding is not None and outcome.result is not None:
            self._require_approved(row)
            if not self._permission(server_id, *binding):
                raise MCPServiceError("mcp_tool_permission_required", "This tool result is no longer permitted.", 403)
            result["result"] = asdict(outcome.result)
        return result

    def shutdown(self):
        with self._shutdown_lock:
            with self._lock:
                self._closing = True
                self._tokens.clear()
                runtime = self._runtime
            if runtime is not None:
                runtime.shutdown()

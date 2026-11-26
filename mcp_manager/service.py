"""Registry/runtime coordination and explicit, one-use manual-call approval."""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import logging
from threading import Lock, RLock
from types import SimpleNamespace
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
        self._lifecycle_gates, self._connection_attempts, self._connection_errors, self._discovered = {}, {}, {}, set()
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
                                or set(item) - {"profile_id", "executable", "args", "cwd", "env"}):
                            raise ValueError()
                        try:
                            configs.append(ServerConfig(item["profile_id"], item["executable"],
                                                        item.get("args", ()), item.get("cwd"),item.get('env',())))
                        except (OSError,ValueError):
                            logging.getLogger('local_ai.mcp').warning('approved_profile_unavailable')
                profiles = {}
                for config in configs:
                    if not isinstance(config, ServerConfig) or config.server_id in profiles:
                        raise ValueError()
                    profiles[config.server_id] = config
            except (OSError, ValueError, TypeError, KeyError):
                raise MCPServiceError("mcp_approval_config_invalid", "Local MCP execution approvals are invalid.", 503) from None
            self._profiles = profiles
            return profiles

    def replace_local_profiles(self, configs):
        """Trusted local-owner entry point; deliberately not exposed over HTTP.

        Replacing a profile never updates a registration's pinned config hash.
        Existing tokens are discarded; active registrations must remain exact.
        """
        configs = tuple(configs)
        if any(type(c) is not ServerConfig for c in configs) or len({c.server_id for c in configs}) != len(configs):
            raise ValueError('Unique validated local profiles required')
        profiles = {c.server_id:c for c in configs}
        with self._lock:
            self._open()
            for row in self.registry.list_servers():
                if self._status(row['server_id']).state != ServerState.STOPPED:
                    candidate = profiles.get(row['profile_id'])
                    if candidate is None or fingerprint(config_data(candidate)) != row['config_hash']:
                        raise ValueError('Stop server before replacing its profile')
            self._approved_configs, self._profiles = configs, profiles
            self._tokens.clear()

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
        status = self._status(row['server_id'])
        operation = status.last_operation
        if status.active_operation_id:
            try:operation=self._runtime.get_operation(row['server_id'],status.active_operation_id)
            except (AttributeError,MCPRuntimeError):operation=None
        return {key: row[key] for key in ("server_id", "profile_id", "name", "created_at")} | {
            "transport": "stdio", "execution_approved": self._approved(row),
            "enabled": row['enabled'],
            "status": public_status(status),
            "connected": status.state==ServerState.RUNNING,
            "executing": bool(operation and operation.kind in {'call','call_once'} and operation.state=='RUNNING'),
            "connection_error": self._connection_errors.get(row['server_id']),
        }

    def _lifecycle_gate(self, server_id):
        self.registry.get(server_id)
        with self._lock:return self._lifecycle_gates.setdefault(server_id,RLock())

    def ensure_connection(self, server_id):
        """At most one connection attempt per server; never replay a Tool call."""
        with self._lifecycle_gate(server_id):
            row=self.registry.get(server_id)
            if not row['enabled']:return False
            identity=(row['config_hash'],row['enabled_generation'])
            status=self._status(server_id)
            if status.state==ServerState.RUNNING and server_id in self._discovered:return True
            previous=self._connection_attempts.get(server_id)
            if previous and previous[0]==identity and (previous[2]>=3 or self.clock()-previous[1]<5):return False
            count=previous[2] if previous and previous[0]==identity else 0
            self._connection_attempts[server_id]=(identity,self.clock(),count+1)
            try:
                self.refresh_profiles();self._require_approved(row)
                from .attestation import validate_filesystem_configuration
                validate_filesystem_configuration(self.profiles()[row['profile_id']])
                if status.state==ServerState.UNCERTAIN:
                    raise MCPServiceError('mcp_recovery_required','Disable this server to finish uncertain cleanup.',409)
                if status.state!=ServerState.RUNNING:
                    if status.state!=ServerState.STOPPED:self.stop_server(server_id)
                    self.start_server(server_id)
                self.list_tools(server_id)
                self._discovered.add(server_id)
                self._connection_attempts.pop(server_id,None)
                self._connection_errors.pop(server_id,None)
                return True
            except Exception as error:
                self._connection_errors[server_id]=getattr(error,'code','mcp_connection_failed')
                logging.getLogger('local_ai.mcp').warning('connection_failed server_id=%s error_type=%s code=%s',
                    server_id,type(error).__name__,self._connection_errors[server_id])
                return False

    def ensure_enabled_connections(self):
        for row in self.registry.list_servers():
            if row['enabled']:self.ensure_connection(row['server_id'])

    def configuration(self, server_id):
        """Only reviewed profile choices; never expose secret env/argument values."""
        row=self.registry.get(server_id)
        profiles=self.refresh_profiles()
        def view(profile):
            from .attestation import filesystem_artifact
            proof=filesystem_artifact(profile)
            return {'profile_id':profile.server_id,'executable':profile.executable,'argument_count':len(profile.args),
                    'cwd':profile.cwd,'environment_keys':[key for key,_ in profile.env],
                    'root':proof['root'] if proof else None}
        return {'server_id':server_id,'profile_id':row['profile_id'],
                'profiles':[view(p) for p in profiles.values()],
                'limitation':'Execution changes require a locally approved profile. Root follows its reviewed read-only arguments.'}

    def configure(self, server_id, profile_id):
        with self._lifecycle_gate(server_id):
            with self._guard(server_id,'configuration') as row:
                profiles=self.refresh_profiles()
                if profile_id not in profiles:raise MCPServiceError('mcp_execution_not_approved','Select a locally approved execution profile.',403)
                config=config_data(profiles[profile_id])
                from .attestation import validate_filesystem_configuration
                try:validate_filesystem_configuration(profiles[profile_id])
                except ValueError:raise MCPServiceError('mcp_configuration_invalid','Reviewed read-only Filesystem settings and a valid Root are required.',400) from None
                if row['profile_id']==profile_id and row['config']==config:return self._view(row)
                if any(r['server_id']!=server_id and (r['profile_id']==profile_id or r['config_hash']==fingerprint(config)) for r in self.registry.list_servers()):
                    raise MCPServiceError('mcp_registry_conflict','This profile is already registered.',409)
                # An active Tool owns the service gate, so this edit is rejected
                # before saving. Idle connections are stopped, never overwritten.
                self.stop_server(server_id)
                with self._lock:
                    if server_id in self._registered:
                        self._runtime.unregister_server(server_id);self._registered.remove(server_id)
                updated=self.registry.configure(server_id,profile_id,config)
                self._discovered.discard(server_id);self._clear_tokens(server_id)
                self._connection_attempts.pop(server_id,None);self._connection_errors.pop(server_id,None)
            if updated['enabled']:self.ensure_connection(server_id)
            return self.get_server(server_id)

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
            approved = profile is not None and config_data(profile) == {key: record[key] for key in ("executable", "args", "cwd", "env") if key in record}
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
                config = ServerConfig(server_id, profile.executable, profile.args, profile.cwd,profile.env)
            except (ValueError, OSError):
                raise MCPServiceError("mcp_execution_unavailable", "Approved local executable is unavailable.", 503) from None
            with self._lock:
                self._open()
                if self._runtime is None:
                    self._runtime = self._runtime_factory()
                self._runtime.register_server(config)
                self._registered.add(server_id)
            self._clear_tokens(server_id)
            self._discovered.discard(server_id)
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
            self._discovered.discard(server_id)
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
        with self._lifecycle_gate(server_id), self._guard(server_id, "delete"):
            if server_id in self._registered:self._runtime.stop_server(server_id)
            self._discovered.discard(server_id)
            with self._lock:
                if server_id in self._registered:
                    self._runtime.unregister_server(server_id)
                    self._registered.remove(server_id)
            # If SQL fails, the registration/grants remain, stopped. A future
            # Start can safely register its unchanged config again.
            self.registry.delete(server_id)
            self._clear_tokens(server_id)
            self._connection_attempts.pop(server_id,None)
            self._connection_errors.pop(server_id,None)
            with self._lock:
                self._bindings.pop(server_id, None)
                self._gates.pop(server_id, None)

    def _tools(self, row):
        self._require_approved(row)
        if row["server_id"] not in self._registered:
            raise MCPRuntimeError("mcp_not_ready", "MCP server is stopped.")
        return self._runtime.list_tools(row["server_id"], refresh=True)

    @staticmethod
    def _legacy_tool(tool):
        return deepcopy({key: getattr(tool, key) for key in ("name", "description", "input_schema")})

    @staticmethod
    def _schema_hash(tool):
        try:
            return fingerprint(MCPService._legacy_tool(tool))
        except (ValueError, TypeError, RecursionError):
            raise MCPServiceError("mcp_tool_schema_invalid", "MCP tool schema is invalid.", 502) from None

    def _tool(self, row, name):
        for tool in self._tools(row):
            if tool.name == name:
                return tool
        raise MCPServiceError("mcp_tool_not_found", "MCP tool was not found.", 404)

    def _permission(self, server_id, name, schema_hash):
        # Legacy descriptor field retained for ApprovalPolicy compatibility.
        # Schema/config/connection identity is checked separately at dispatch.
        return self.registry.get(server_id)['enabled']

    def set_enabled(self, server_id, enabled):
        with self._lifecycle_gate(server_id):
            row = self.registry.set_enabled(server_id, enabled)
            self._clear_tokens(server_id)
            self._connection_attempts.pop(server_id,None)
            if enabled:self.ensure_connection(server_id)
            else:
                try:self.stop_server(server_id);self._connection_errors.pop(server_id,None)
                except Exception as error:self._connection_errors[server_id]=getattr(error,'code','mcp_cleanup_failed')
            return self.get_server(server_id)

    def list_tools(self, server_id):
        with self._guard(server_id) as row:
            return [self._legacy_tool(tool) | {"schema_hash": self._schema_hash(tool),
                    "allowed": self._permission(server_id, tool.name, self._schema_hash(tool))}
                    for tool in self._tools(row)]

    def set_permission(self, server_id, name, allowed):
        raise MCPServiceError('mcp_tool_permissions_retired', 'Use server Enabled instead of per-tool permissions.', 409)

    def permissions(self, server_id):
        return [row | {"allowed": bool(row["allowed"])} for row in self.registry.list_permissions(server_id)]

    def catalog_tools(self, server_id):
        """Read-only Agent catalog, separate from the unchanged manual REST view."""
        with self._guard(server_id) as row:
            tools = self._tools(row)
            return [self._catalog_descriptor(row, tool) for tool in tools]

    def execution_context(self, server_id):
        """Trusted local metadata only; never returned by the public REST API."""
        row=self.registry.get(server_id)
        self.refresh_profiles();self._require_approved(row)
        if self._status(server_id).state!=ServerState.RUNNING:
            raise MCPRuntimeError('mcp_not_ready','Server is not running.')
        runtime=self._runtime
        owner=getattr(runtime,'manager',runtime)
        return {'config_hash':row['config_hash'],'config':json.loads(canonical_json(row['config'])),
            'connection_generation':runtime.connection_generation(server_id),
            'connection_info':owner.connection_info(server_id) if hasattr(owner,'connection_info') else None}

    def _catalog_descriptor(self, row, tool, *, connection_generation=None):
        server_id = row["server_id"]
        return asdict(tool) | {
            "allowed": self._permission(server_id, tool.name, self._schema_hash(tool)),
            "source": {"config_hash": row["config_hash"],
                       "connection_generation": (self._runtime.connection_generation(server_id)
                                                 if connection_generation is None else connection_generation),
                       "permission_generation": row['enabled_generation']},
        }

    def validate_catalog_arguments(self, server_id, name, arguments, expected_fingerprint):
        """Validate current mapping/permission/arguments without approving or executing."""
        rows = self.catalog_tools(server_id)
        tool = next((item for item in rows if item["name"] == name), None)
        if tool is None or not tool["allowed"] or fingerprint(tool) != expected_fingerprint:
            raise MCPServiceError("mcp_catalog_stale", "MCP catalog changed; rediscovery is required.", 409)
        return self._arguments(SimpleNamespace(input_schema=tool["input_schema"]), arguments)

    def _authorized_tool(self, row, name):
        tool = self._tool(row, name)
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

    def revoke_call_approval(self, token):
        with self._lock:
            self._tokens.pop(fingerprint(token), None)

    def call_tool(self, server_id, name, arguments, token, *, operation_id=None, before_execute=None):
        if operation_id is not None or before_execute is not None:
            return self._call_tool_once(server_id, name, arguments, token, operation_id, before_execute)
        from agent_poc.owner_runtime import OwnerFakeRuntime
        from agent_poc.live_transport import ReviewedStdioRuntime
        if type(self._runtime) in (OwnerFakeRuntime, ReviewedStdioRuntime):
            # The existing manual UI already requires its explicit one-use
            # MCPService token. Preserve that contract while using the reviewed
            # owner's single-dispatch path; no Agent proposal/auto grant is minted.
            with self._guard(server_id):
                tool,_schema_hash=self._authorized_tool(self.registry.get(server_id),name)
                arguments=self._arguments(tool,arguments)
            return self._call_tool_once(server_id,name,arguments,token,secrets.token_hex(16),lambda context,args:None)
        with self._guard(server_id) as row:
            tool, schema_hash = self._authorized_tool(row, name)
            arguments = self._arguments(tool, arguments)
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

    def _call_tool_once(self, server_id, name, arguments, token, operation_id, before_execute):
        """C-3B-1 guarded admission; only the exact in-memory Fake Runtime.

        The ordinary manual path is unchanged. Never enable an arbitrary runtime
        here just because it implements a similarly named method.
        """
        from agent_poc.fake_runtime import FakeRuntime
        from agent_poc.owner_runtime import OwnerFakeRuntime
        from agent_poc.live_transport import ReviewedStdioRuntime
        from .manager import MCPManager
        if (type(self._runtime) not in (FakeRuntime, OwnerFakeRuntime, ReviewedStdioRuntime, MCPManager) or not isinstance(operation_id, str)
                or not operation_id or not callable(before_execute)):
            raise MCPServiceError("mcp_execution_bridge_disabled", "Real MCP execution is disabled for this bridge.", 403)
        with self._guard(server_id):
            arguments = json.loads(canonical_json(arguments))
            binding = None
            approval_error = None

            def authorize_send(owner_tool=None, owner_generation=None):
                nonlocal binding, approval_error
                # Fake Runtime owns its connection/schema lock here; this
                # service guard serializes Allow/Block and other service actions.
                row = self.registry.get(server_id)
                self.refresh_profiles()
                if owner_tool is None:
                    tool, schema_hash = self._authorized_tool(row, name)
                else:
                    # Owner provides fresh discovery under its connection lock;
                    # never synchronously queue Discovery from that same loop.
                    self._require_approved(row)
                    tool, schema_hash = owner_tool, self._schema_hash(owner_tool)
                    if tool.name != name or not self._permission(server_id, name, schema_hash):
                        raise MCPServiceError("mcp_tool_permission_required", "Latest tool permission is required.", 403)
                args = self._arguments(tool, arguments)
                try:self._consume(server_id, name, schema_hash, args, token)
                except MCPServiceError as error:
                    approval_error=error
                    raise
                before_execute(self._catalog_descriptor(row, tool, connection_generation=owner_generation), args)
                binding = (name, schema_hash)

            try:
                result = self._runtime.call_tool_once(server_id, name, arguments,
                    operation_id=operation_id, before_send=authorize_send)
            except Exception as error:
                if binding is not None:
                    self._bind(server_id, operation_id, *binding)
                if approval_error is not None:raise approval_error
                raise
            self._bind(server_id, operation_id, *binding)
            outcome = self._runtime.get_operation(server_id, operation_id)
            return {"server_id": server_id, "operation_id": operation_id,
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

"""Explicit operator bindings, not guesses based on a server's display name."""
from copy import deepcopy
from hashlib import sha256
import time

from jsonschema import Draft202012Validator, SchemaError

from .models import ToolChatError

TIME_TOOLS = {"get_current_time", "convert_time", "get_time_ago"}
FILESYSTEM_TOOLS = {"read_file", "list_directory"}


def identity(context):
    return tuple(context[key] for key in ("server_id", "config_hash", "connection_generation",
                                          "permission_generation", "schema_hash"))


def arguments_valid(context, arguments):
    from mcp_manager.models import ToolInfo
    from mcp_manager.service import MCPService
    tool = context["tool"]
    return MCPService._arguments(ToolInfo(tool["name"], tool["description"], tool["input_schema"]), arguments)


class ToolCatalog:
    def __init__(self, mcp, bindings):
        # {server_id: ("time", None) or ("filesystem", verified FilesystemPolicy)}
        self.mcp = mcp
        self.bindings = dict(bindings)
        if any(kind not in {"time", "filesystem"} for kind, _policy in self.bindings.values()):
            raise ValueError("Unsupported server kind")

    def discover(self, *, deadline=None, clock=time.monotonic):
        def budget():
            if deadline is None:
                return {}
            remaining = deadline - clock()
            if remaining <= 0:
                raise ToolChatError("job_timeout")
            return {"timeout": min(30, remaining)}
        result = {}
        for server in self.mcp.list_servers():
            budget()
            server_id = server["server_id"]
            if (server_id not in self.bindings or not server["execution_approved"]
                    or server["status"]["state"] != "RUNNING"):
                continue
            kind, policy = self.bindings[server_id]
            if kind == "filesystem" and policy is None:
                continue  # No attested roots: fail closed.
            supported = TIME_TOOLS if kind == "time" else FILESYSTEM_TOOLS
            for tool in self.mcp.list_tools(server_id, **budget()):
                if tool["name"] not in supported or not tool["allowed"]:
                    continue
                context = self.mcp.inspect_tool(server_id, tool["name"], **budget())
                try:
                    schema = context["tool"]["input_schema"]
                    Draft202012Validator.check_schema(schema)
                    def check_refs(value):
                        if isinstance(value, dict):
                            for key, item in value.items():
                                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                                    raise ValueError()
                                check_refs(item)
                        elif isinstance(value, list):
                            for item in value:
                                check_refs(item)
                    check_refs(schema)
                except (ValueError, TypeError, RecursionError, SchemaError):
                    continue
                if not context["connection_generation"]:
                    continue
                if kind == "filesystem":
                    try:
                        policy.verify_context(context)
                    except ToolChatError:
                        continue
                alias = "mcp_" + sha256((server_id + "\0" + tool["name"]).encode()).hexdigest()[:40]
                if alias in result:
                    raise ToolChatError("tool_alias_collision")
                # Names are fixed-length ASCII; real IDs/names never come from a model alias.
                result[alias] = {"context": context, "kind": kind, "policy": policy, "server_name": server.get("name", server_id),
                                 "schema": {"type": "function", "function": {"name": alias,
                                            "description": context["tool"]["description"], "parameters": deepcopy(schema)}}}
        return result

    def current(self, entry):
        original = entry["context"]
        current = self.mcp.inspect_tool(original["server_id"], original["tool"]["name"])
        if identity(current) != identity(original):
            raise ToolChatError("tool_policy_changed")
        return current

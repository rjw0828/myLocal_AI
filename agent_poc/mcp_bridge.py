"""Official dynamic FunctionToolset; all actual execution disabled until C-3."""
from pydantic_ai import Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.wrapper import WrapperToolset
from dataclasses import dataclass
from copy import deepcopy

from .mcp_catalog import CatalogError


class BridgeExecutionDisabled(PermissionError):
    pass


@dataclass
class CatalogGuard(WrapperToolset):
    """One fresh catalog per step; no private provider or Toolset APIs."""
    bridge: 'MCPBridge'

    async def get_tools(self, ctx):
        current = {entry.alias: entry.identity for entry in self.bridge.catalog.discover()}
        tools = await super().get_tools(ctx)
        for entry in self.bridge.entries:
            if current.get(entry.alias) != entry.identity:
                self.bridge.invalidated.add(entry.alias)
        tools = {name: tool for name, tool in tools.items() if name not in self.bridge.invalidated}
        originals = {entry.alias: entry.descriptor['input_schema'] for entry in self.bridge.entries}
        if any(tool.tool_def.parameters_json_schema != originals[name] for name, tool in tools.items()):
            raise CatalogError('Toolset transformed the original MCP input schema')
        self.bridge.check_model_schema(ctx.model, [tool.tool_def for tool in tools.values()])
        return tools


class MCPBridge:
    def __init__(self, catalog):
        self.catalog = catalog
        self.entries = catalog.discover()
        self.blocked_attempts = 0
        self.invalidated = set()
        self.functions = FunctionToolset(max_retries=0)
        for entry in self.entries:
            self.functions.add_tool(self._tool(entry))
        # Official wrapper owns deferred approval. C-2 does not trust its
        # boolean as server authorization, so even approved calls hit a deny gate.
        self.toolset = CatalogGuard(self.functions.approval_required(), self)

    def _tool(self, entry):
        async def validate(ctx, **arguments):
            self.catalog.validate(entry, arguments)

        async def blocked(**arguments):
            self.catalog.validate(entry, arguments)
            self.blocked_attempts += 1
            raise BridgeExecutionDisabled('C-2: MCP execution and approval integration are disabled')

        descriptor = entry.descriptor
        tool = Tool.from_schema(blocked, name=entry.alias, description=descriptor['description'],
                                json_schema=descriptor['input_schema'], sequential=True,
                                args_validator=validate)
        tool.metadata = {'mcp_server_id': entry.server_id, 'mcp_tool_name': entry.original_name,
                         'catalog_identity': entry.identity,
                         'output_schema': descriptor.get('output_schema'),
                         'annotations': descriptor.get('annotations')}
        if tool.tool_def.parameters_json_schema != descriptor['input_schema']:
            raise CatalogError('Model schema differs from original MCP schema')
        return tool

    def check_model_schema(self, model, definitions=None):
        """Public provider preparation check, no HTTP request or private APIs.

        Also called automatically by CatalogGuard before every model step.
        Refuse provider schema transformations instead of silently repairing.
        """
        from pydantic_ai.models import ModelRequestParameters
        original = definitions if definitions is not None else [tool.tool_def for tool in self.functions.tools.values()]
        expected = deepcopy({tool.name: tool.parameters_json_schema for tool in original})
        _, prepared = model.prepare_request(None, ModelRequestParameters(function_tools=original))
        actual = {tool.name: tool.parameters_json_schema for tool in prepared.function_tools}
        if actual != expected:
            raise CatalogError('Provider transformed the original MCP input schema')

"""C-2 catalog/Agent integration; fake model and fake SDK, no MCP processes."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from agent_poc.mcp_catalog import MCPCatalog, CatalogError, validate_schema
from agent_poc.mcp_bridge import MCPBridge, BridgeExecutionDisabled
from mcp_manager.async_host import discover_tools
from agent_mcp_fakes import FakeMCPService, SCHEMA


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeMCPService()
        self.tool = self.service.add()
        self.catalog = MCPCatalog(self.service)

    def test_multiple_servers_same_name_and_new_tool(self):
        self.service.add('beta')
        self.service.add('new-kind', 'never_seen_before')
        entries = self.catalog.discover()
        self.assertEqual(len(entries), 3)
        self.assertEqual(len({t.alias for t in entries}), 3)
        self.assertEqual(entries[2].original_name, 'never_seen_before')

    def test_alias_collision_fails_closed(self):
        self.service.add('beta')
        with patch('agent_poc.mcp_catalog.safe_alias', return_value='collision'):
            with self.assertRaises(CatalogError): self.catalog.discover()

    def test_safe_alias(self):
        self.tool['name'] = 'odd name/한국어?'
        self.assertRegex(self.catalog.discover()[0].alias, r'^[A-Za-z0-9_]{1,64}$')

    def test_catalog_limits_fail_instead_of_truncating(self):
        for limits in ({'max_tools': 0}, {'max_bytes': 1}):
            with self.subTest(limits=limits):
                with self.assertRaises(CatalogError): MCPCatalog(self.service, **limits).discover()

    def test_schemas_annotations_preserved_immutable(self):
        self.tool['input_schema'] = {'type': 'object', '$defs': {'v': {'type': 'integer'}},
                                    'properties': {'value': {'$ref': '#/$defs/v'}}}
        entry = self.catalog.discover()[0]
        self.assertEqual(entry.descriptor, self.tool)
        entry.descriptor['input_schema']['type'] = 'array'
        self.assertEqual(entry.descriptor['input_schema']['type'], 'object')

    def test_invalid_schema(self):
        self.tool['input_schema'] = {'type': 'object', 'properties': {'value': {'type': 'madeup'}}}
        with self.assertRaises(CatalogError): self.catalog.discover()
        self.service.add('healthy')
        isolated=MCPCatalog(self.service,isolate_failures=True)
        self.assertEqual([entry.server_id for entry in isolated.discover()],['healthy'])
        self.assertEqual(self.service._connection_errors['alpha'],'mcp_catalog_unavailable')

    def test_external_ref_rejected(self):
        for key in ('$ref', '$dynamicRef'):
            with self.subTest(key=key):
                self.tool['input_schema'] = {'type': 'object', key: 'https://invalid.local/schema'}
                with self.assertRaises(CatalogError): self.catalog.discover()

    def test_unsupported_dialect_and_missing_local_ref(self):
        for change in ({'$schema': 'http://json-schema.org/draft-07/schema#'}, {'$ref': '#/$defs/missing'}):
            with self.subTest(change=change):
                with self.assertRaises(CatalogError): validate_schema({'type': 'object', **change})

    def test_blocked_and_stopped_not_exposed(self):
        self.tool['allowed'] = False
        self.service.add('stopped'); self.service.servers['stopped']['state'] = 'STOPPED'
        self.service.add('unapproved'); self.service.servers['unapproved']['approved'] = False
        self.assertEqual(self.catalog.discover(), ())

    def test_schema_change_invalidates_old_mapping(self):
        entry = self.catalog.discover()[0]
        self.tool['input_schema']['description'] = 'updated'
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})

    def test_permission_regrant_invalidates_old_mapping(self):
        entry = self.catalog.discover()[0]
        self.tool['source']['permission_generation'] += 2
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})

    def test_reconnect_invalidates_old_mapping(self):
        entry = self.catalog.discover()[0]
        self.tool['source']['connection_generation'] = 'connection-2'
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})

    def test_block_or_stop_invalidates_mapping(self):
        entry = self.catalog.discover()[0]
        self.tool['allowed'] = False
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})
        self.tool['allowed'] = True; self.service.servers['alpha']['state'] = 'STOPPED'
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})

    def test_additional_service_argument_boundary(self):
        entry = self.catalog.discover()[0]
        self.assertEqual(self.catalog.validate(entry, {'value': 2}), {'value': 2})
        for args in ({}, {'value': '2'}, {'value': 2, 'approval': 'forged'}):
            with self.subTest(args=args):
                with self.assertRaises(Exception): self.catalog.validate(entry, args)
        self.assertEqual(self.service.validations, 4)
        self.assertEqual(self.service.executions, 0)

    def test_output_schema_changes_invalidate(self):
        entry = self.catalog.discover()[0]
        self.tool['output_schema']['description'] = 'changed'
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})

    def test_schema_version_change_invalidates(self):
        entry = self.catalog.discover()[0]
        self.tool['source']['schema_version'] += 1
        with self.assertRaises(PermissionError): self.catalog.validate(entry, {'value': 1})

    def test_schema_keyword_names_can_be_property_names(self):
        validate_schema({'type': 'object', 'properties': {
            '$ref': {'type': 'string'}, 'format': {'type': 'string'},
            '$id': {'type': 'string'}, 'default': {'const': {'$ref': 'literal-data'}}}})

    def test_nested_schema_external_ref_rejected(self):
        with self.assertRaises(CatalogError):
            validate_schema({'type': 'object', 'properties': {'items': {
                'type': 'array', 'items': {'$ref': 'https://invalid.example/schema'}}}})

    def test_nested_objects_arrays_defaults_and_local_refs(self):
        self.tool['input_schema'] = {'type': 'object', '$defs': {'v': {'type': 'integer'}},
            'properties': {'rows': {'type': 'array', 'items': {'type': 'object',
                'properties': {'value': {'$ref': '#/$defs/v'}}, 'required': ['value']}},
                'optional': {'type': 'string', 'default': 'not inserted'}}, 'required': ['rows']}
        entry = self.catalog.discover()[0]
        args = {'rows': [{'value': 3}]}
        self.assertEqual(self.catalog.validate(entry, args), args)
        with self.assertRaises(Exception): self.catalog.validate(entry, {'rows': [{'value': 'bad'}]})

    def test_two_ollama_profiles_share_bridge_schema(self):
        bridge = MCPBridge(self.catalog)
        for name in ('gemma4:12b', 'qwen3.5:9b'):
            model = OllamaModel(name, provider=OllamaProvider(base_url='http://127.0.0.1:11434/v1'))
            bridge.check_model_schema(model)  # request preparation only, no HTTP
        self.assertEqual(self.service.executions, 0)

    def test_provider_schema_change_refused(self):
        bridge = MCPBridge(self.catalog)
        class Changed:
            def prepare_request(self, settings, params):
                from dataclasses import replace
                return settings, replace(params, function_tools=[])
        with self.assertRaises(CatalogError): bridge.check_model_schema(Changed())

    def test_provider_inplace_schema_change_refused(self):
        bridge = MCPBridge(self.catalog)
        class Changed:
            def prepare_request(self, settings, params):
                params.function_tools[0].parameters_json_schema['description'] = 'provider rewrite'
                return settings, params
        with self.assertRaises(CatalogError): bridge.check_model_schema(Changed())


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, name='one'):
        return SimpleNamespace(name=name, description='fixture', input_schema=deepcopy(SCHEMA),
                               output_schema={'type': 'object'}, annotations={'readOnlyHint': True})

    async def test_multiple_pages(self):
        calls = []
        async def listing(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(tools=[self.tool('two' if kwargs.get('cursor') else 'one')],
                                   next_cursor=None if kwargs.get('cursor') else 'page-2')
        result = await discover_tools(SimpleNamespace(list_tools=listing), refresh=True)
        self.assertEqual([t.name for t in result], ['one', 'two'])
        self.assertEqual(calls, [{'cache_mode': 'refresh'}, {'cache_mode': 'refresh', 'cursor': 'page-2'}])
        self.assertEqual(result[1].annotations, {'readOnlyHint': True})
        self.assertEqual(result[1].output_schema, {'type': 'object'})

    async def test_invalid_cursors(self):
        for cursor in ('', 1, False, 'x\x00', 'x' * 4097):
            with self.subTest(cursor=repr(cursor)[:20]):
                async def listing(**kwargs): return SimpleNamespace(tools=[], next_cursor=cursor)
                with self.assertRaises(ValueError): await discover_tools(SimpleNamespace(list_tools=listing))

    async def test_repeated_cursor(self):
        async def listing(**kwargs): return SimpleNamespace(tools=[], next_cursor='same')
        with self.assertRaises(ValueError): await discover_tools(SimpleNamespace(list_tools=listing))

    async def test_page_limit(self):
        async def listing(**kwargs):
            return SimpleNamespace(tools=[], next_cursor=str(int(kwargs.get('cursor', '0')) + 1))
        with self.assertRaises(ValueError): await discover_tools(SimpleNamespace(list_tools=listing), max_pages=2)

    async def test_duplicate_names(self):
        async def listing(**kwargs): return SimpleNamespace(tools=[self.tool(), self.tool()], next_cursor=None)
        with self.assertRaises(ValueError): await discover_tools(SimpleNamespace(list_tools=listing))

    async def test_tool_limit(self):
        async def listing(**kwargs): return SimpleNamespace(tools=[self.tool('a'), self.tool('b')], next_cursor=None)
        with self.assertRaises(ValueError): await discover_tools(SimpleNamespace(list_tools=listing), max_tools=1)

    async def test_sdk_typed_metadata(self):
        from mcp.types import Tool, ListToolsResult, ToolAnnotations
        async def listing(**kwargs):
            return ListToolsResult(tools=[Tool(name='typed', input_schema=SCHEMA,
                output_schema={'type': 'object'}, annotations=ToolAnnotations(read_only_hint=True))])
        result = await discover_tools(SimpleNamespace(list_tools=listing))
        self.assertEqual(result[0].annotations['readOnlyHint'], True)
        self.assertEqual(result[0].output_schema, {'type': 'object'})


class AgentBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = FakeMCPService(); self.service.add()
        self.bridge = MCPBridge(MCPCatalog(self.service))
        alias = self.bridge.entries[0].alias
        def model(messages, info):
            if any(getattr(p, 'part_kind', '') == 'tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('denial acknowledged')])
            return ModelResponse(parts=[ToolCallPart(alias, {'value': 2}, tool_call_id='call-1')])
        self.agent = Agent(FunctionModel(model), toolsets=[self.bridge.toolset],
                           output_type=[str, DeferredToolRequests], retries=0)

    async def test_official_agent_defers_without_execution(self):
        result = await self.agent.run('generic operation')
        self.assertIsInstance(result.output, DeferredToolRequests)
        self.assertEqual(result.output.approvals[0].tool_name, self.bridge.entries[0].alias)
        self.assertEqual(self.service.executions, 0)

    async def test_approved_boolean_still_cannot_execute(self):
        result = await self.agent.run('generic operation')
        with self.assertRaises(BridgeExecutionDisabled):
            await self.agent.run(conversation=result.conversation,
                                 deferred_tool_results=DeferredToolResults(approvals={'call-1': True}))
        self.assertEqual(self.bridge.blocked_attempts, 1)
        self.assertEqual(self.service.executions, 0)

    async def test_denial_never_executes(self):
        result = await self.agent.run('generic operation')
        await self.agent.run(conversation=result.conversation,
                             deferred_tool_results=DeferredToolResults(approvals={'call-1': ToolDenied('denied')}))
        self.assertEqual(self.service.executions, 0)

    async def test_schema_change_during_deferred_wait(self):
        result = await self.agent.run('generic operation')
        self.service.servers['alpha']['tools'][0]['source']['permission_generation'] += 1
        with self.assertRaises(UnexpectedModelBehavior):
            await self.agent.run(conversation=result.conversation,
                                 deferred_tool_results=DeferredToolResults(approvals={'call-1': True}))
        self.assertEqual(self.bridge.invalidated, {self.bridge.entries[0].alias})
        self.assertEqual(self.service.executions, 0)

    async def test_expired_forged_service_approval_cannot_enable_dispatch(self):
        self.service.approval = {'token': 'forged', 'expires': -1}
        tool = self.bridge.functions.tools[self.bridge.entries[0].alias]
        with self.assertRaises(BridgeExecutionDisabled):
            await tool.function(value=3)
        self.assertEqual(self.service.executions, 0)

    async def test_changed_catalog_hidden_before_model_request(self):
        self.service.servers['alpha']['tools'][0]['allowed'] = False
        seen = []
        def model(messages, info):
            seen.extend(info.function_tools)
            return ModelResponse(parts=[TextPart('no callable tool')])
        agent = Agent(FunctionModel(model), toolsets=[self.bridge.toolset])
        await agent.run('operation')
        self.assertEqual(seen, [])
        self.assertEqual(self.bridge.invalidated, {self.bridge.entries[0].alias})
        self.assertEqual(self.service.executions, 0)

    async def test_toolset_schema_mutation_refused_before_request(self):
        tool = self.bridge.functions.tools[self.bridge.entries[0].alias]
        tool.function_schema.json_schema['description'] = 'unauthorized local rewrite'
        with self.assertRaises(CatalogError): await self.agent.run('operation')
        self.assertEqual(self.service.executions, 0)


class ServiceCatalogTests(unittest.TestCase):
    def setUp(self):
        # Reuse real service/temp registry/fake SDK fixture without inheriting
        # and accidentally rerunning its entire suite.
        from test_mcp_api import MCPApiTests
        self.fixture = MCPApiTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.sid = self.fixture.register(); self.fixture.start(self.sid); self.fixture.permit(self.sid)
        self.service = self.fixture.service

    def test_real_service_catalog_and_manual_contract(self):
        catalog = MCPCatalog(self.service); entry = catalog.discover()[0]
        self.assertEqual(catalog.validate(entry, {'text': 'fixture'}), {'text': 'fixture'})
        self.assertEqual(set(self.service.list_tools(self.sid)[0]),
                         {'name', 'description', 'input_schema', 'allowed', 'schema_hash'})
        self.assertEqual(self.fixture.scenario.calls, [])

    def test_real_service_regrant_invalidates(self):
        catalog = MCPCatalog(self.service); entry = catalog.discover()[0]
        self.fixture.permit(self.sid, False); self.fixture.permit(self.sid)
        with self.assertRaises(Exception): catalog.validate(entry, {'text': 'fixture'})

    def test_real_service_reconnect_invalidates(self):
        catalog = MCPCatalog(self.service); entry = catalog.discover()[0]
        self.service.stop_server(self.sid); self.service.start_server(self.sid)
        with self.assertRaises(Exception): catalog.validate(entry, {'text': 'fixture'})


if __name__ == '__main__':
    unittest.main()

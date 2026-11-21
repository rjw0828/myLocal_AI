"""C-4B public model configuration and no-execution selection checks."""
import unittest
from copy import deepcopy

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.providers.ollama import OllamaProvider

from agent_mcp_fakes import FakeMCPService
from agent_poc.c4b_selection import configured_model, MODELS, settings, SnapshotService, instructions
from agent_poc.mcp_catalog import MCPCatalog
from agent_poc.mcp_bridge import MCPBridge
from agent_poc.approval_core import ApprovalCore
from agent_poc.fixture_executor import FixtureExecutor
from agent_poc.approved_bridge import DeferredFixtureRun


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def catalog(self):
        service = FakeMCPService()
        service.add(input_schema={'$schema':'https://json-schema.org/draft/2020-12/schema',
            'title':'Preserve metadata','type':'object','properties':{
                'value':{'type':'integer','title':'Original value'}}, 'required':['value']})
        return MCPCatalog(service)

    async def test_both_models_preserve_full_original_schema(self):
        catalog = self.catalog()
        original = deepcopy(catalog.discover()[0].descriptor)
        provider = OllamaProvider(base_url='http://127.0.0.1:11434/v1')
        for name in MODELS:
            bridge = MCPBridge(catalog)
            bridge.check_model_schema(configured_model(name, provider))
            self.assertEqual(bridge.entries[0].descriptor, original)

    async def test_unknown_model_rejected(self):
        with self.assertRaises(ValueError):
            configured_model('unknown-cloud', OllamaProvider(base_url='http://127.0.0.1:11434/v1'))

    async def test_snapshot_has_no_execution_or_approval_path(self):
        service = SnapshotService({})
        with self.assertRaises(PermissionError): service.call_tool()
        with self.assertRaises(PermissionError): service.approve_call()

    async def test_selection_deferred_never_executes(self):
        bridge = MCPBridge(self.catalog()); entry = bridge.entries[0]
        agent = Agent(FunctionModel(lambda messages, info: ModelResponse(parts=[
            ToolCallPart(entry.alias, {'value':2}, tool_call_id='selection')])),
            toolsets=[bridge.toolset], output_type=[str, DeferredToolRequests], retries=0)
        result = await agent.run('generic test')
        self.assertIsInstance(result.output, DeferredToolRequests)
        self.assertEqual(bridge.blocked_attempts, 0)

    async def test_same_settings_for_both_models(self):
        self.assertEqual(settings(1), settings(1))
        self.assertEqual(settings(1)['openai_reasoning_effort'], 'none')
        self.assertNotIn('num_ctx', settings(1).get('extra_body', {}))

    async def test_existing_run_accepts_official_configuration_without_loop_change(self):
        catalog = self.catalog()
        executor = FixtureExecutor({('alpha','convert'):{'value':42}})
        core = ApprovalCore(catalog, executor); core.create_session('config')
        seen = []
        def model(messages, info):
            seen.extend(messages)
            return ModelResponse(parts=[TextPart('plain answer')])
        turn = DeferredFixtureRun(core, 'config', FunctionModel(model),
            instructions='Shared instructions', model_settings={'temperature':.2})
        await turn.start('plain question')
        self.assertEqual(turn.answer,'plain answer')
        self.assertEqual(executor.calls,0)
        self.assertTrue(any('Shared instructions' in str(m) for m in seen))

    async def test_live_start_still_requires_separate_permission(self):
        from agent_poc.c4b_live import run
        with self.assertRaises(PermissionError): await run()

    async def test_live_uses_exact_confined_profiles(self):
        from agent_poc.c4b_live import profiles, ROOT, CASES
        time_profile, filesystem_profile = profiles()
        self.assertEqual(time_profile.args,('-m','mcp_server_time','--local-timezone=Asia/Seoul'))
        self.assertEqual(filesystem_profile.args[1:],('--read-only','--confine-roots',str(ROOT)))
        self.assertEqual(len(CASES),4)


if __name__=='__main__': unittest.main()

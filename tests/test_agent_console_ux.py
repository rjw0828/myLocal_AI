"""Console model UX with isolated settings, FunctionModel and memory MCP only."""
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import create_app
import test_agent_web as web

HEADERS = web.HEADERS


class AgentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='local-ai-agent-ux-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.apps = []
        self.addCleanup(lambda: [a.extensions['mcp_service'].shutdown() for a in self.apps])
        self.models = patch('ai_controller.list_models', return_value=['qwen3.5:9b', 'new:8b']).start()
        self.addCleanup(patch.stopall)

    def app(self, **kwargs):
        app = create_app(self.base/'sessions.sqlite3', mcp_database_path=self.base/'mcp.sqlite3',
                         mcp_approved_configs=[], **kwargs)
        self.apps.append(app)
        return app

    def test_shared_selection_persistence_and_removed_model(self):
        app = self.app(); client = app.test_client()
        self.assertIs(app.extensions['agent_model_selection'], app.extensions['model_selection'])
        self.assertEqual(client.put('/models/selection?scope=agent', headers=HEADERS,
                                   json={'model':'new:8b'}).status_code, 200)
        restored = self.app()
        self.assertEqual(restored.extensions['agent_model_selection'].current(), 'new:8b')
        self.assertEqual(restored.extensions['model_selection'].current(), 'new:8b')
        self.models.return_value = ['qwen3.5:9b']
        data = client.get('/models?scope=agent', headers=HEADERS).json
        self.assertFalse(data['selected_available'])
        self.assertEqual(data['selected'], 'new:8b')
        self.assertEqual(client.put('/models/selection?scope=agent', headers=HEADERS,
                                   json={'model':'missing'}).status_code, 400)

    def test_default_disabled_and_scope_guards(self):
        app = self.app(); client = app.test_client()
        self.assertFalse(app.extensions['agent_status']['enabled'])
        self.assertIn('data-agent-enabled="false"', client.get('/').text)
        self.assertEqual(client.get('/models?scope=agent').status_code, 403)
        self.assertEqual(client.put('/models/selection?scope=invalid', headers=HEADERS,
                                   json={'model':'new:8b'}).status_code, 400)
        self.assertEqual(app.extensions['mcp_service']._registered, set())

    def test_opt_in_shares_service_without_starting_servers(self):
        app = self.app(enable_agent=True, agent_policy_path=self.base/'policy.sqlite3')
        backend = app.extensions['tool_chat']; self.addCleanup(backend.shutdown)
        self.assertTrue(app.extensions['agent_status']['enabled'])
        self.assertIs(backend.manager.service, app.extensions['mcp_service'])
        self.assertIsNone(app.extensions['mcp_service']._runtime)
        client=app.test_client();sid=client.post('/sessions',json={}).json['session_id']
        for question,answer in [('hello','Hello'),('write example code','def add(a, b): return a + b')]:
            async def respond(messages,info):return web.ModelResponse(parts=[web.TextPart(answer)])
            @asynccontextmanager
            async def models(initial):yield web.FunctionModel(respond),{}
            backend.model_factory=models
            reply=client.post('/tool-chat/jobs',headers=HEADERS,json={'session_id':sid,
                'message':question,'browser_generation':1,'response_language':'ko'})
            self.assertEqual(reply.status_code,202,reply.json)
            import time
            for _ in range(200):
                job=client.get('/tool-chat/jobs/'+reply.json['job']['job_id'],headers=HEADERS).json['job']
                if job['state'] in ('COMPLETED','FAILED'):break
                time.sleep(.01)
            self.assertEqual(job['state'],'COMPLETED',job)
            self.assertEqual(job['result']['message'],answer)
            self.assertEqual(job['result']['tool_calls'],[])
        self.assertIsNone(app.extensions['mcp_service']._runtime)
        self.assertEqual(len(client.get('/sessions/'+sid+'/history').json['messages']),4)
        self.assertTrue(app.test_client().get('/models?scope=agent', headers=HEADERS).json['configurable'])

    def test_startup_failure_keeps_ordinary_console_available(self):
        with patch('agent_poc.console_integration.backend_factory', return_value=lambda *a: (_ for _ in ()).throw(OSError('private path'))):
            app = self.app(enable_agent=True)
        self.assertEqual(app.extensions['agent_status']['reason'], 'startup_failed')
        self.assertEqual(app.test_client().get('/').status_code, 200)

    def test_missing_optional_dependency_keeps_chat_available(self):
        import builtins
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'agent_poc.console_integration': raise ImportError('optional package missing')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=importing):
            app = self.app(enable_agent=True)
        self.assertEqual(app.extensions['agent_status']['reason'], 'missing_dependencies')
        self.assertEqual(app.test_client().get('/').status_code, 200)


class AgentJobModelTests(unittest.TestCase):
    setUp = web.AgentWebTests.setUp
    create = web.AgentWebTests.create
    wait = web.AgentWebTests.wait
    decide = web.AgentWebTests.decide
    history = web.AgentWebTests.history

    def test_standard_console_selection_reaches_model_factory_and_history(self):
        original = self.backend.model_factory; seen = []
        @asynccontextmanager
        async def model(initial, **kwargs):
            seen.append((initial['agent_model'], kwargs['name'], kwargs['installed_only']))
            async with original(initial) as result: yield result
        with patch('ai_controller.list_models', return_value=['qwen3.5:9b','new:8b']), \
             patch('agent_poc.console_integration.local_model', side_effect=model):
            self.app = create_app(self.base/'sessions.sqlite3', mcp_existing_service=self.manager.service,
                                  enable_agent=True, agent_policy_path=self.base/'separate-policy.sqlite3')
            self.backend = self.app.extensions['tool_chat']; self.addCleanup(self.backend.shutdown)
            self.client = self.app.test_client()
            reply = self.client.put('/models/selection', headers=HEADERS, json={'model':'new:8b'})
            self.assertEqual(reply.status_code, 200)
            jid = self.create(); self.assertEqual(self.wait(jid)['state'], 'WAITING_APPROVAL')
            self.assertEqual(self.client.put('/models/selection', headers=HEADERS,
                                           json={'model':'qwen3.5:9b'}).status_code, 200)
            self.assertEqual(self.decide(jid).status_code, 202)
            job = self.wait(jid, ('COMPLETED','FAILED'))
            self.assertEqual(job['state'], 'COMPLETED', job)
            self.assertEqual(job['result']['model'], 'new:8b')
            self.assertEqual(seen, [('new:8b','new:8b',True)])
            self.assertEqual(len(self.history()['messages']), 2)
            self.assertEqual(self.app.extensions['model_selection'].current(), 'qwen3.5:9b')

    def test_model_setup_failure_releases_job_without_tool_execution(self):
        @asynccontextmanager
        async def failing(initial):
            raise ValueError('private setup path')
            yield
        self.backend.model_factory = failing
        job = self.wait(self.create(), ('FAILED',))
        self.assertFalse(job['reservation_owned'])
        self.assertEqual(job['operations'], [])
        self.assertEqual(self.runtime.sends, 0)

    def test_model_is_pinned_across_approval_and_next_run_uses_new_selection(self):
        selected = ['first:8b']; seen = []
        original = self.backend.model_factory
        @asynccontextmanager
        async def factory(initial):
            seen.append(initial['agent_model'])
            async with original(initial) as model: yield model
        self.backend.model_factory = factory
        self.backend.model_resolver = lambda: selected[0]
        jid = self.create(); job = self.wait(jid)
        self.assertEqual(job['model'], 'first:8b')
        self.assertEqual(job['phase'], 'WAITING_APPROVAL')
        selected[0] = 'second:9b'
        self.assertEqual(self.decide(jid).status_code, 202)
        completed = self.wait(jid, ('COMPLETED','FAILED'))
        self.assertEqual(completed['state'], 'COMPLETED', completed)
        self.assertEqual(completed['result']['model'], 'first:8b')
        next_id = self.create()
        self.assertEqual(self.wait(next_id)['model'], 'second:9b')
        self.assertEqual(seen, ['first:8b', 'second:9b'])
        self.decide(next_id, False)
        self.assertEqual(self.wait(next_id)['state'], 'COMPLETED')

    def test_failure_diagnostics_are_sanitized_and_no_retry(self):
        self.behavior = 'error'
        with self.assertLogs('local_ai.agent', level='INFO') as logs:
            jid = self.create(); job = self.wait(jid, ('FAILED',))
        text = '\n'.join(logs.output)
        self.assertIn(jid, text); self.assertIn('function-fixture', text)
        self.assertIn('error_type=ValueError', text)
        self.assertIn('stage=MODEL_RESPONSE', text)
        self.assertNotIn('private upstream failure', text)
        self.assertEqual(job['error_code'], 'agent_run_failed')
        self.assertFalse(job['reservation_owned'])
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(len(self.history()['messages']), 0)

    def test_model_rejection_precedes_reservation(self):
        from agent_poc.web_contract import ToolChatError
        def reject(): raise ToolChatError('agent_model_unavailable')
        self.backend.model_resolver = reject
        response = self.client.post('/tool-chat/jobs', headers=HEADERS, json={
            'session_id':self.session, 'message':'hello', 'browser_generation':1, 'response_language':'ko'})
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(self.backend.reservations.owner(self.session))
        self.assertEqual(self.runtime.sends, 0)

    def test_legacy_injected_host_cannot_claim_model_change(self):
        response = self.client.put('/models/selection?scope=agent', headers=HEADERS, json={'model':'anything'})
        self.assertEqual(response.status_code, 409)


class InstalledProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_arbitrary_installed_model_uses_its_exact_name_without_inference(self):
        from agent_poc.web_console import local_model
        with patch('ai_controller.list_models', return_value=['downloaded-any:8b']):
            async with local_model({}, name='downloaded-any:8b', installed_only=True) as (model, settings):
                self.assertEqual(model.model_name, 'downloaded-any:8b')
                self.assertFalse(settings['parallel_tool_calls'])

    async def test_model_deleted_before_setup_is_explicitly_rejected(self):
        from agent_poc.web_console import local_model
        from agent_poc.web_contract import ToolChatError
        with patch('ai_controller.list_models', return_value=[]):
            with self.assertRaises(ToolChatError) as error:
                async with local_model({}, name='removed:8b', installed_only=True): self.fail('must not start')
            self.assertEqual(error.exception.code, 'agent_model_unavailable')

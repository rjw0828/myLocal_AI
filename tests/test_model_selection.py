"""Model discovery/selection over mocked Ollama and isolated config/Session DBs."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch, MagicMock

import ai_controller
from app import create_app
from model_selection import ModelSelection
from test_app import chat_result

HEADERS = {'X-Local-AI-Control':'web-console'}


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='local-ai-model-selection-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.models=patch('ai_controller.list_models',return_value=['qwen3.5:9b','downloaded:latest']).start()
        self.addCleanup(patch.stopall)
        self.apps=[]
        self.app=self.make_app();self.client=self.app.test_client()
        self.addCleanup(lambda:[a.extensions['mcp_service'].shutdown() for a in self.apps])

    def make_app(self):
        app=create_app(self.base/'sessions.sqlite3',mcp_database_path=self.base/'mcp.sqlite3',mcp_approved_configs=[])
        self.apps.append(app);return app

    def select(self, model):
        return self.client.put('/models/selection',headers=HEADERS,json={'model':model})

    def test_installed_models_refresh_without_loading_or_inference(self):
        response=self.client.get('/models',headers=HEADERS)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['selected'],'qwen3.5:9b')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.models.return_value += ['new-download:8b']
        self.assertIn('new-download:8b',self.client.get('/models',headers=HEADERS).json['models'])
        self.assertFalse((self.base/'model_settings.json').exists())
        self.assertEqual(self.app.extensions['mcp_service']._registered,set())

    def test_selected_model_is_persisted_and_applied_to_chat_and_voice_chat(self):
        self.assertEqual(self.select('downloaded:latest').status_code,200)
        self.assertEqual(self.make_app().extensions['model_selection'].current(),'downloaded:latest')
        sid=self.client.post('/sessions',json={}).json['session_id']
        with patch('ai_controller.ask_ollama',return_value=chat_result()) as ask:
            for style in ({},{'X-Local-AI-Response-Style':'voice'}):
                reply=self.client.post('/chat',headers=style,json={'session_id':sid,'message':'hello'})
                self.assertEqual(reply.status_code,200,reply.json)
                self.assertEqual(reply.json['model'],'downloaded:latest')
                self.assertEqual(ask.call_args.kwargs['model'],'downloaded:latest')
        self.assertEqual(len(self.client.get('/sessions/'+sid+'/history').json['messages']),4)
        self.assertEqual(ai_controller.MODEL,'qwen3.5:9b')
        self.assertFalse(self.client.get('/').get_data(as_text=True).find('data-agent-enabled="false"')<0)

    def test_model_removed_from_disk_is_not_silently_replaced(self):
        self.select('downloaded:latest');self.models.return_value=['qwen3.5:9b']
        data=self.client.get('/models',headers=HEADERS).json
        self.assertFalse(data['selected_available']);self.assertEqual(data['selected'],'downloaded:latest')
        self.assertEqual(self.select('unknown:latest').status_code,400)
        self.assertEqual(self.app.extensions['model_selection'].current(),'downloaded:latest')

    def test_offline_or_malformed_list_does_not_change_selection(self):
        for error in (OSError('offline'),ValueError('bad list')):
            self.models.side_effect=error
            self.assertEqual(self.client.get('/models',headers=HEADERS).status_code,503)
            self.assertEqual(self.select('downloaded:latest').status_code,503)
            self.assertEqual(self.app.extensions['model_selection'].current(),'qwen3.5:9b')

    def test_local_origin_and_header_protection(self):
        for headers in ({},{**HEADERS,'Origin':'https://external.example'},
                        {**HEADERS,'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.client.get('/models',headers=headers).status_code,403)
            self.assertEqual(self.client.put('/models/selection',headers=headers,json={'model':'downloaded:latest'}).status_code,403)
        self.assertEqual(self.models.call_count,0)

    def test_inflight_request_pins_model_and_rejects_switch_until_finished(self):
        entered=Event();release=Event()
        sid=self.client.post('/sessions',json={}).json['session_id']
        def answer(messages,**kwargs):
            self.assertEqual(kwargs['model'],'qwen3.5:9b');entered.set()
            if not release.wait(5):raise RuntimeError('fixture timeout')
            return chat_result()
        with ThreadPoolExecutor() as pool,patch('ai_controller.ask_ollama',side_effect=answer):
            future=pool.submit(lambda:self.app.test_client().post('/chat',json={'session_id':sid,'message':'hello'}))
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.client.get('/models',headers=HEADERS).json['busy'])
                self.assertEqual(self.select('downloaded:latest').status_code,409)
            finally:release.set()
            self.assertEqual(future.result().json['model'],'qwen3.5:9b')
        self.assertEqual(self.select('downloaded:latest').status_code,200)

    def test_timeout_releases_selection_and_does_not_save_history(self):
        sid=self.client.post('/sessions',json={}).json['session_id']
        with patch('ai_controller.ask_ollama',side_effect=TimeoutError()):
            self.assertEqual(self.client.post('/chat',json={'session_id':sid,'message':'hello'}).status_code,504)
        self.assertEqual(self.select('downloaded:latest').status_code,200)
        self.assertEqual(self.client.get('/sessions/'+sid+'/history').json['messages'],[])

    def test_invalid_or_failed_setting_save_does_not_change_active_model(self):
        for model in (None,{},'','invalid\nname','x'*513):
            self.assertEqual(self.select(model).status_code,400)
        with patch('model_selection.os.replace',side_effect=OSError('fixture save failure')):
            self.assertEqual(self.select('downloaded:latest').status_code,503)
        self.assertEqual(self.app.extensions['model_selection'].current(),'qwen3.5:9b')
        self.assertEqual(list(self.base.glob('.model-settings-*')),[])
        (self.base/'model_settings.json').write_text('broken json')
        self.assertEqual(ModelSelection(self.base/'model_settings.json','default').current(),'default')


class ModelTransportTests(unittest.TestCase):
    @patch('ai_controller.urllib.request.urlopen')
    def test_tags_transport_is_local_bounded_and_no_pull(self,open_url):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"models":[{"name":"z:latest"},{"name":"a:latest"}]}'
        open_url.return_value=response
        self.assertEqual(ai_controller.list_models(),['a:latest','z:latest'])
        self.assertEqual(open_url.call_args.args[0],'http://127.0.0.1:11434/api/tags')
        self.assertEqual(response.read.call_args.args[0],1024*1024+1)
        response.read.return_value=b'x'*(1024*1024+1)
        with self.assertRaises(ValueError):ai_controller.list_models()

    @patch('ai_controller.urllib.request.urlopen')
    def test_selected_model_is_sent_in_actual_chat_request(self,open_url):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps(chat_result()).encode()
        open_url.return_value=response
        ai_controller.ask_ollama([{'role':'user','content':'hello'}],model='any-installed:9b')
        self.assertEqual(json.loads(open_url.call_args.args[0].data)['model'],'any-installed:9b')

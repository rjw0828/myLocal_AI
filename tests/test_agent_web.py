"""Pydantic FunctionModel + memory SDK + temporary Flask/Session databases."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from threading import Event
from unittest.mock import patch

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from app import create_app
from agent_poc.mcp_management import MCPManagement
from agent_poc.owner_runtime import OwnerFakeRuntime
from agent_poc.readiness_gate import ReadOnlyScope, aligned_read_only_scope
from agent_poc.web_backend import AgentWebBackend
from mcp_manager.models import ToolInfo, ToolCallResult

HEADERS={'X-Local-AI-Control':'web-console'}


class AgentWebTests(unittest.TestCase):
    def test_legacy_policy_migration_preserves_records_without_auto_trust(self):
        import json
        import sqlite3
        from contextlib import closing
        for legacy in ('ASK_EVERY_TIME','AUTO_ALLOW_SAFE'):
            with self.subTest(mode=legacy):
                policy=self.manager.policy
                with closing(sqlite3.connect(policy.path)) as db:
                    raw=db.execute('SELECT settings FROM policy WHERE id=1').fetchone()[0]
                    settings=json.loads(raw)
                    settings.update(project_mode=legacy,auto_enabled=True,
                        overrides={self.sid:'ALLOW_ALL'},trusts={'legacy-record':{'server_id':self.sid,'tool':'get_current_time'}})
                    settings.pop('legacy_project_mode',None)
                    db.execute('UPDATE policy SET settings=? WHERE id=1',(json.dumps(settings),));db.commit()
                    before=db.execute("SELECT count(*) FROM audit WHERE event='legacy_session_permission_migrated'").fetchone()[0]
                state=policy.snapshot()
                self.assertEqual(state['project_mode'],'MANUAL')
                self.assertEqual(state['legacy_project_mode'],legacy)
                self.assertFalse(state['auto_enabled'])
                self.assertEqual(state['trusts'],settings['trusts'])
                self.assertEqual(state['overrides'],settings['overrides'])
                self.assertEqual(policy.snapshot()['revision'],state['revision'])
                self.assertEqual(len([a for a in policy.audit_log() if a['event']=='legacy_session_permission_migrated']),before+1)
                entry=self.backend.catalog.discover()[0]
                self.assertEqual(policy.evaluate(entry,{},permission={'mode':'MANUAL','manual_generation':0}).action,'ASK')
                self.assertEqual(policy.evaluate(entry,{},permission={'mode':'ALLOW_ALL','manual_generation':0}).action,'AUTO_APPROVE')
                self.assertEqual(policy.evaluate(entry,{}).action,'ASK')
                self.assertEqual(self.runtime.sends,0)

    def setUp(self):
        self.temp=TemporaryDirectory(prefix='local-ai-agent-web-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.runtime=OwnerFakeRuntime(prepared=True)
        self.fixture_python=self.base/'python.exe'
        self.fixture_python.write_bytes(b'OwnerFakeRuntime fixture; never executed')
        self.runtime.tools=[ToolInfo('get_current_time','Get actual current time',{'type':'object','properties':{
            'timezone':{'type':'string','default':'Asia/Seoul'}}})]
        self.runtime.result=ToolCallResult(({'type':'text','text':'{"timezone":"Asia/Seoul","datetime":"2026-11-20T09:30:00+09:00"}'},),None,False)
        def bind(runtime,config,options):
            scope=aligned_read_only_scope(ReadOnlyScope(config.server_id,'time'),config)
            runtime.manager._client_factory.bind(scope);return scope
        self.manager=MCPManagement(self.base/'management',runtime_factory=lambda:self.runtime,contract_binder=bind,
            session_database_path=self.base/'sessions.sqlite3')
        self.addCleanup(self.manager.shutdown)
        self.sid=self.manager.add_server('Time',str(self.fixture_python),['-m','mcp_server_time','--local-timezone=Asia/Seoul'],confirm=lambda _:True)['server_id']
        self.manager.test_connection(self.sid,confirm=lambda _:True)
        self.manager.set_enabled(self.sid,True)
        self.behavior='tools';self.history_seen=[];self.model_started=Event();self.model_release=Event()
        async def model(messages,info):
            self.history_seen=messages
            if self.behavior=='slow':
                import asyncio
                self.model_started.set()
                while not self.model_release.is_set():await asyncio.sleep(.01)
            if self.behavior=='timeout':raise TimeoutError('mock transport timeout')
            if self.behavior=='error':raise ValueError('private upstream failure')
            if self.behavior=='chat':return ModelResponse(parts=[TextPart('Hello, how can I help?')])
            if self.behavior=='no_tools':return ModelResponse(parts=[TextPart('Invented current time')])
            if any(getattr(p,'part_kind','')=='tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('현재 한국 시간은 오전 9시 30분입니다.')])
            return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name,{'timezone':'Asia/Seoul'},tool_call_id='web-time')])
        @asynccontextmanager
        async def models(initial):yield FunctionModel(model),{}
        self.backends=[]
        def factory(service,revision,prepare,commit):
            assert service is self.manager.service
            backend=AgentWebBackend(self.manager,session_revision=revision,prepare=prepare,commit=commit,
                model_factory=models,exposed={(self.sid,'get_current_time')},model_name='function-fixture')
            self.backends.append(backend);return backend
        self.app=create_app(self.base/'sessions.sqlite3',mcp_existing_service=self.manager.service,agent_backend_factory=factory)
        self.backend=self.backends[0];self.addCleanup(self.backend.shutdown)
        self.addCleanup(self.model_release.set)
        self.client=self.app.test_client();self.session=self.client.post('/sessions',headers=HEADERS,json={}).json['session_id']

    def create(self,message='지금 몇 시야?',session=None):
        r=self.client.post('/tool-chat/jobs',headers=HEADERS,json={'session_id':session or self.session,
            'message':message,'browser_generation':1,'response_language':'ko'})
        self.assertEqual(r.status_code,202,r.json);return r.json['job']['job_id']

    def wait(self,jid,states=('WAITING_APPROVAL','COMPLETED','FAILED','CANCELLED')):
        for _ in range(200):
            r=self.client.get('/tool-chat/jobs/'+jid,headers=HEADERS)
            if r.json['job']['state'] in states:return r.json['job']
            time.sleep(.01)
        self.fail(r.json)

    def decide(self,jid,allow=True):
        job=self.wait(jid);self.assertEqual(job['state'],'WAITING_APPROVAL',job)
        return self.client.post('/tool-chat/jobs/'+jid+'/approval',headers=HEADERS,json={
            'proposal_id':job['pending']['proposal_id'],'browser_generation':1,'allow':allow})

    def history(self):return self.client.get('/sessions/'+self.session+'/history').json

    def test_ask_executes_only_after_allow_and_commits_history(self):
        jid=self.create();job=self.wait(jid)
        self.assertEqual(job['state'],'WAITING_APPROVAL');self.assertEqual(self.runtime.sends,0)
        self.assertEqual(self.decide(jid).status_code,202)
        job=self.wait(jid,('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job);self.assertFalse(job['reservation_owned'])
        self.assertEqual(self.runtime.sends,1)
        self.assertIn('9시 30분',job['result']['message'])
        self.assertIn('현재 한국',str(self.history()))

    def test_session_allow_all_needs_no_tool_trust_and_is_isolated(self):
        path='/sessions/'+self.session+'/permission'
        self.assertEqual(self.client.get(path,headers=HEADERS).json['mode'],'MANUAL')
        self.assertEqual(self.client.put(path,headers=HEADERS,json={'mode':'ALLOW_ALL'}).status_code,200)
        job=self.wait(self.create(),('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job);self.assertEqual(self.runtime.sends,1)
        self.assertTrue(any(a['event']=='operation_admitted' and a['method']=='AUTO' for a in self.manager.audit()))
        other=self.client.post('/sessions',json={}).json['session_id']
        self.assertEqual(self.client.get('/sessions/'+other+'/permission',headers=HEADERS).json['mode'],'MANUAL')
        jid=self.create(session=other);pending=self.wait(jid)
        self.assertEqual(pending['state'],'WAITING_APPROVAL');self.assertEqual(self.runtime.sends,1)
        self.decide(jid,False)

    def test_allow_response_consistent_before_worker_resumes(self):
        jid=self.create();pending=self.wait(jid)['pending']
        payload={'proposal_id':pending['proposal_id'],'browser_generation':1,'allow':True}
        # Hold the scheduler gate to capture the exact HTTP receipt before the
        # worker can advance; Flask uses the same thread's reentrant lock.
        with self.backend._lock:
            response=self.client.post('/tool-chat/jobs/'+jid+'/approval',headers=HEADERS,json=payload)
            self.assertEqual(response.status_code,202,response.json)
            job=response.json['job']
            self.assertEqual(job['state'],'RUNNING',response.json)
            self.assertIsNone(job['pending'])
            self.assertTrue(job['reservation_owned'])
            self.assertIsNone(job['result'])
            self.assertEqual(self.runtime.sends,0)
            duplicate=self.client.post('/tool-chat/jobs/'+jid+'/approval',headers=HEADERS,json=payload)
            self.assertEqual(duplicate.status_code,409)
        done=self.wait(jid,('COMPLETED','FAILED'))
        self.assertEqual(done['state'],'COMPLETED',done)
        self.assertEqual(self.runtime.sends,1)
        self.assertEqual(len(self.history()['messages']),2)

    def test_deny_never_executes_or_stores_answer(self):
        jid=self.create();self.decide(jid,False);job=self.wait(jid,('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job)
        self.assertEqual(job['result']['tool_calls'][0]['status'],'DENIED')
        self.assertEqual(self.runtime.sends,0)
        self.assertNotIn('9시',str(self.history()))

    def test_same_session_chat_and_second_job_blocked(self):
        jid=self.create();self.wait(jid)
        r=self.client.post('/chat',json={'session_id':self.session,'message':'hello'})
        self.assertEqual(r.status_code,409,r.json)
        r=self.client.post('/tool-chat/jobs',headers=HEADERS,json={'session_id':self.session,'message':'again','browser_generation':2})
        self.assertEqual(r.status_code,409)

    def test_reset_waiting_revokes_then_allows_data_reset(self):
        jid=self.create();self.wait(jid)
        for _ in range(30):
            r=self.client.post('/sessions/'+self.session+'/reset',json={})
            if r.status_code==200:break
            self.assertEqual(r.status_code,409);time.sleep(.01)
        self.assertEqual(r.status_code,200,r.json);self.assertEqual(self.runtime.sends,0)
        self.assertEqual(self.wait(jid,('CANCELLED',))['state'],'CANCELLED')

    def test_unknown_session_not_created(self):
        r=self.client.post('/tool-chat/jobs',headers=HEADERS,json={'session_id':'missing','message':'hello','browser_generation':0})
        self.assertEqual(r.status_code,404)

    def test_model_without_execution_evidence_not_saved(self):
        self.behavior='no_tools';job=self.wait(self.create(),('FAILED',))
        self.assertEqual(job['error_code'],'tool_evidence_missing');self.assertEqual(self.runtime.sends,0)
        self.assertNotIn('Invented',str(self.history()))

    def test_schema_or_permission_change_stales_proposal(self):
        jid=self.create();job=self.wait(jid)
        self.manager.set_enabled(self.sid,False)
        r=self.client.post('/tool-chat/jobs/'+jid+'/approval',headers=HEADERS,json={
            'proposal_id':job['pending']['proposal_id'],'browser_generation':1,'allow':True})
        self.assertEqual(r.status_code,409);self.assertEqual(self.runtime.sends,0)

    def test_cross_origin_and_missing_header_rejected(self):
        self.assertEqual(self.client.get('/tool-chat/policy/'+self.sid).status_code,403)
        r=self.client.get('/tool-chat/policy/'+self.sid,headers={**HEADERS,'Origin':'https://example.com'})
        self.assertEqual(r.status_code,403)

    def test_session_permission_api_retires_server_trust_and_rejects_invalid_modes(self):
        base='/tool-chat/policy/'+self.sid
        for path,method in ((base,'GET'),(base+'/mode','PUT'),(base+'/trust','POST')):
            self.assertEqual(self.client.open(path,method=method,headers=HEADERS,json={}).status_code,410)
        path='/sessions/'+self.session+'/permission'
        self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.client.put(path,headers=HEADERS,json={'mode':'AUTO_ALLOW_SAFE'}).status_code,400)
        self.assertEqual(self.client.put(path,headers={**HEADERS,'Origin':'https://evil.example'},json={'mode':'ALLOW_ALL'}).status_code,403)
        self.assertEqual(self.client.get(path,headers=HEADERS).json['mode'],'MANUAL')
        self.assertEqual(self.runtime.sends,0)

    def test_management_endpoints_reuse_owner_facade_without_tool_call(self):
        base='/tool-chat/management/servers/'+self.sid
        self.assertEqual(self.client.post(base+'/stop',headers=HEADERS,json={}).status_code,200)
        self.assertEqual(self.runtime.sends,0)
        response=self.client.post(base+'/connect',headers=HEADERS,json={'confirmed':True})
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['result']['started']);self.assertEqual(self.runtime.sends,0)
        response=self.client.put('/mcp/servers/'+self.sid+'/enabled',headers=HEADERS,json={'enabled':False})
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(self.runtime.sends,0)
        self.assertEqual(self.client.get('/tool-chat/policy/'+self.sid,headers=HEADERS).status_code,410)

    def test_running_manual_job_does_not_gain_allow_all_and_next_call_requires_approval(self):
        jid=self.create();first=self.wait(jid)['pending']['proposal_id']
        path='/sessions/'+self.session+'/permission'
        self.client.put(path,headers=HEADERS,json={'mode':'ALLOW_ALL'})
        self.assertEqual(self.wait(jid)['state'],'WAITING_APPROVAL')
        self.assertEqual(self.runtime.sends,0)
        self.decide(jid);self.assertEqual(self.wait(jid,('COMPLETED','FAILED'))['state'],'COMPLETED')
        self.behavior='slow'
        next_id=self.create()
        self.assertTrue(self.model_started.wait(2))
        self.client.put(path,headers=HEADERS,json={'mode':'MANUAL'})
        self.model_release.set()
        second=self.wait(next_id)['pending']['proposal_id']
        self.assertNotEqual(first,second);self.assertEqual(self.runtime.sends,1)
        self.decide(next_id,False)
        self.assertEqual(self.runtime.sends,1)

    def test_manual_mcp_api_uses_same_service(self):
        self.assertIs(self.app.extensions['mcp_service'],self.manager.service)
        self.assertEqual(self.client.get('/mcp/servers',headers=HEADERS).json['servers'][0]['server_id'],self.sid)

    def test_manual_execute_keeps_original_one_use_token_contract(self):
        path='/mcp/servers/'+self.sid+'/tools/get_current_time'
        response=self.client.post(path+'/approve',headers=HEADERS,json={'arguments':{'timezone':'Asia/Seoul'}})
        self.assertEqual(response.status_code,200,response.json)
        body={'arguments':{'timezone':'Asia/Seoul'},'approval_token':response.json['approval_token']}
        result=self.client.post(path+'/call',headers=HEADERS,json=body)
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(self.runtime.sends,1)
        self.assertEqual(self.client.post(path+'/call',headers=HEADERS,json=body).status_code,403)
        self.assertEqual(self.runtime.sends,1)

    def test_disabled_by_default(self):
        app=create_app(self.base/'disabled.sqlite3',mcp_database_path=self.base/'disabled-mcp.sqlite3',mcp_approved_configs=[])
        self.addCleanup(app.extensions['mcp_service'].shutdown)
        client=app.test_client()
        self.assertEqual(client.post('/tool-chat/jobs',headers=HEADERS,json={}).status_code,404)
        html=client.get('/').get_data(as_text=True)
        self.assertIn('data-agent-enabled="false"',html)

    def test_approval_expiry_no_execute_no_history(self):
        self.backend.approval_seconds=.05
        jid=self.create();job=self.wait(jid,('FAILED',))
        self.assertEqual(job['error_code'],'approval_expired',job)
        self.assertEqual(self.runtime.sends,0);self.assertFalse(job['reservation_owned'])

    def test_stale_browser_generation_cannot_approve(self):
        jid=self.create();p=self.wait(jid)['pending']
        r=self.client.post('/tool-chat/jobs/'+jid+'/approval',headers=HEADERS,json={
            'proposal_id':p['proposal_id'],'browser_generation':2,'allow':True})
        self.assertEqual(r.status_code,409);self.assertEqual(self.runtime.sends,0)

    def test_cancel_during_inference_holds_reset_until_observed_end(self):
        self.behavior='slow';jid=self.create();self.assertTrue(self.model_started.wait(2))
        r=self.client.post('/tool-chat/jobs/'+jid+'/cancel',headers=HEADERS,json={})
        self.assertTrue(r.json['job']['reservation_owned'])
        self.assertEqual(self.client.post('/sessions/'+self.session+'/reset',json={}).status_code,409)
        self.model_release.set();job=self.wait(jid,('CANCELLED',))
        self.assertFalse(job['reservation_owned']);self.assertEqual(self.runtime.sends,0)
        self.assertEqual(self.client.post('/sessions/'+self.session+'/reset',json={}).status_code,200)

    def test_unknown_model_timeout_keeps_reservation_and_reconcile_denied(self):
        self.behavior='timeout';jid=self.create();job=self.wait(jid,('UNCERTAIN',))
        self.assertTrue(job['reservation_owned']);self.assertTrue(job['unsettled'])
        self.assertEqual(self.client.post('/tool-chat/jobs/'+jid+'/reconcile',headers=HEADERS,json={}).status_code,409)

    def test_delete_waiting_job_and_delete_all(self):
        jid=self.create();self.wait(jid)
        r=self.client.delete('/sessions/'+self.session)
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(self.wait(jid,('CANCELLED',))['state'],'CANCELLED')
        self.assertEqual(self.client.delete('/sessions').status_code,200)

    def test_other_session_can_chat_while_first_waits(self):
        jid=self.create();self.wait(jid)
        other=self.client.post('/sessions',headers=HEADERS,json={}).json['session_id']
        from test_app import chat_result
        with patch('ai_controller.ask_ollama',return_value=chat_result()):
            response=self.client.post('/chat',json={'session_id':other,'message':'hello'})
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(self.runtime.sends,0)

    def test_unknown_job_and_unexposed_trust_fail(self):
        self.assertEqual(self.client.get('/tool-chat/jobs/missing',headers=HEADERS).status_code,404)
        r=self.client.post('/tool-chat/policy/'+self.sid+'/trust',headers=HEADERS,json={
            'tool':'unseen','catalog_fingerprint':'x','confirmed':True,'verified_read_only':True})
        self.assertEqual(r.status_code,410)

    def test_auto_permission_narrowed_at_final_admission_sends_zero(self):
        store=self.app.extensions['session_store'];store.set_permission(self.session,'ALLOW_ALL')
        original=self.manager.policy.consume
        def narrow(*args,**kwargs):
            store.set_permission(self.session,'MANUAL')
            store.set_permission(self.session,'ALLOW_ALL')  # ABA cannot revive this Job's grant.
            return original(*args,**kwargs)
        with patch.object(self.manager.policy,'consume',side_effect=narrow):
            job=self.wait(self.create(),('FAILED',))
        self.assertEqual(self.runtime.sends,0)
        self.assertEqual(self.history()['messages'],[])
        self.assertFalse(job['reservation_owned'])
        self.assertEqual(job['error_code'],'session_permission_requires_manual')

    def test_history_only_user_and_final_assistant_not_tool_messages(self):
        jid=self.create();self.decide(jid);self.assertEqual(self.wait(jid,('COMPLETED','FAILED'))['state'],'COMPLETED')
        data=self.history()
        self.assertEqual([m['role'] for m in data['messages']],['user','assistant'])

    def test_tool_chat_preserves_question_and_system_language(self):
        question='  original question\nwith formatting  '
        self.behavior='chat'
        jid=self.create(message=question)
        self.assertEqual(self.wait(jid,('COMPLETED','FAILED'))['state'],'COMPLETED')
        initial=self.backend._jobs[jid]['initial']
        self.assertEqual(initial['messages'][-1],{'role':'user','content':question})
        self.assertIn('Korean',initial['system_prompt'])
        self.assertNotIn('[Response requirements]',initial['messages'][-1]['content'])
        self.assertEqual(len(self.history()['messages']),2)
        self.assertEqual(self.runtime.sends,0)

    def test_allow_all_still_blocks_disabled_server_without_send(self):
        self.app.extensions['session_store'].set_permission(self.session,'ALLOW_ALL')
        self.manager.set_enabled(self.sid,False)
        self.behavior='no_tools'
        job=self.wait(self.create(),('FAILED',))
        self.assertEqual(job['error_code'],'tool_evidence_missing')
        self.assertEqual(self.runtime.sends,0)
        self.assertEqual(self.history()['messages'],[])

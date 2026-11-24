"""C-5E reproductions, promoted to C-5F regressions for the approved fixes.

All I/O is the C-5D memory SDK. Weekday/file semantics remain characterized
limitations; their successful completion is not a declaration of answer safety.
"""
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from mcp_manager.models import ToolCallResult
import test_c5d as dynamic_fixture
from agent_poc.managed_catalog import ManagedCatalog


class EvidenceCharacterizationTests(unittest.TestCase):
    setUp = dynamic_fixture.DynamicTests.setUp
    create = dynamic_fixture.DynamicTests.create
    wait = dynamic_fixture.DynamicTests.wait
    decide = dynamic_fixture.DynamicTests.decide
    history = dynamic_fixture.DynamicTests.history
    filesystem = dynamic_fixture.DynamicTests.filesystem

    def answer_after(self, tool, arguments, answer):
        self.backend.catalog = ManagedCatalog(self.manager)
        alias = next(e.alias for e in self.backend.catalog.discover()
                     if e.original_name == tool)

        async def respond(messages, info):
            if any(getattr(p, 'part_kind', '') == 'tool-return'
                   for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart(answer)])
            return ModelResponse(parts=[ToolCallPart(alias, arguments, tool_call_id='c5e')])

        @asynccontextmanager
        async def models(initial):
            yield FunctionModel(respond), {}
        self.backend.model_factory = models

    def complete(self, question):
        jid = self.create(question)
        pending = self.wait(jid)['pending']
        response = self.client.post('/tool-chat/jobs/' + jid + '/approval',
            headers={'X-Local-AI-Control': 'web-console'}, json={
                'proposal_id': pending['proposal_id'], 'browser_generation': 1,
                'allow': True, 'scope': pending['scope']})
        self.assertEqual(response.status_code, 202, response.json)
        return self.wait(jid, ('COMPLETED', 'FAILED'))

    def test_time_contradiction_is_not_saved(self):
        answer = '현재 시간은 오후 11시 59분입니다.'
        self.answer_after('get_current_time', {'timezone': 'Asia/Seoul'}, answer)
        job = self.complete('현재 한국 시간을 알려줘.')
        self.assertEqual(job['state'], 'FAILED', job)
        self.assertEqual(job['error_code'], 'tool_time_mismatch')
        self.assertIsNone(job['result'])
        self.assertFalse(job['reservation_owned'])
        self.assertEqual(self.history()['messages'], [])
        self.assertEqual(self.runtime.sends, 1)

    def test_gap_weekday_contradiction_is_currently_saved(self):
        self.answer_after('get_current_time', {'timezone': 'Asia/Seoul'},
                          '2026년 11월 20일 월요일 오전 9시 30분입니다.')
        self.assertEqual(self.complete('현재 한국 시간을 알려줘.')['state'], 'COMPLETED')

    def test_gap_file_content_contradiction_is_currently_saved(self):
        self.filesystem()
        self.runtime.result = ToolCallResult(({'type': 'text', 'text': 'fixture'},), None, False)
        self.answer_after('read_file', {'path': str(self.root / 'test.txt')},
                          'test.txt의 내용은 FABRICATED입니다.')
        self.assertEqual(self.complete('test.txt 읽어줘.')['state'], 'COMPLETED')
        self.assertEqual(self.runtime.sends, 1)

    def test_compound_request_rejects_time_only_evidence(self):
        self.filesystem()
        self.answer_after('get_current_time', {'timezone': 'Asia/Seoul'},
                          '현재 시간은 오전 9시 30분이고 test.txt 내용은 FABRICATED입니다.')
        job = self.complete('현재 한국 시간과 test.txt 내용을 읽어서 알려줘.')
        self.assertEqual(job['state'], 'FAILED', job)
        self.assertEqual(job['error_code'], 'tool_evidence_missing')
        self.assertIsNone(job['result'])
        self.assertFalse(job['reservation_owned'])
        self.assertEqual(self.history()['messages'], [])
        self.assertEqual(self.runtime.sends, 1)

    def test_failed_tool_cannot_save_fabricated_success(self):
        self.runtime.result=ToolCallResult(({'type':'text','text':'fixture failure'},),None,True)
        claimed='FABRICATED_SUCCESS'
        self.answer_after('get_current_time',{'timezone':'Asia/Seoul'},claimed)
        job=self.complete('current time')
        self.assertEqual(job['state'],'COMPLETED',job)
        self.assertEqual(job['result']['tool_calls'][0]['status'],'FAILED')
        self.assertNotIn(claimed,str(self.history()))
        self.assertEqual(len(self.history()['messages']),2)

    def test_filesystem_ui_deny_without_scope_and_late_allow(self):
        self.filesystem()
        self.answer_after('read_file', {'path': str(self.root / 'test.txt')}, 'unused')
        jid = self.create('test.txt 읽어줘.')
        pending = self.wait(jid)['pending']
        # The existing browser adapter includes scope only for Allow.
        response = self.client.post('/tool-chat/jobs/' + jid + '/approval',
            headers={'X-Local-AI-Control': 'web-console'}, json={
                'proposal_id': pending['proposal_id'], 'browser_generation': 1,
                'allow': False})
        self.assertEqual(response.status_code, 202, response.json)
        job = self.wait(jid, ('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job)
        self.assertEqual(job['result']['tool_calls'][0]['status'],'DENIED')
        self.assertIsNone(job['pending'])
        self.assertFalse(job['reservation_owned'])
        for allow in (False, True):
            repeated = self.client.post('/tool-chat/jobs/' + jid + '/approval',
                headers={'X-Local-AI-Control': 'web-console'}, json={
                    'proposal_id': pending['proposal_id'], 'browser_generation': 1,
                    'allow': allow, 'scope': pending['scope']})
            self.assertEqual(repeated.status_code, 409)
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(len(self.history()['messages']),2)

    def test_filesystem_allow_still_requires_scope_and_exact_proposal(self):
        self.filesystem()
        self.answer_after('read_file', {'path': str(self.root / 'test.txt')}, 'fixture')
        jid = self.create('test.txt 읽어줘.')
        pending = self.wait(jid)['pending']
        for proposal, allow, scope in (('other', False, None),
                (pending['proposal_id'], True, None), (pending['proposal_id'], True, {'path': 'wrong'})):
            response = self.client.post('/tool-chat/jobs/' + jid + '/approval',
                headers={'X-Local-AI-Control': 'web-console'}, json={
                    'proposal_id': proposal, 'browser_generation': 1, 'allow': allow, 'scope': scope})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wait(jid)['state'], 'WAITING_APPROVAL')
        self.assertEqual(self.runtime.sends, 0)

    def test_normal_file_reply_still_saves_original_turn(self):
        self.filesystem()
        self.runtime.result = ToolCallResult(({'type': 'text', 'text': 'fixture'},), None, False)
        self.answer_after('read_file', {'path': 'test.txt'}, 'fixture')
        job = self.complete('test.txt 읽어줘.')
        self.assertEqual(job['state'], 'COMPLETED', job)
        self.assertEqual(job['result']['evidence']['coverage'], 'matched')
        self.assertEqual(job['result']['evidence']['answer_semantics'], 'not_verified')
        self.assertEqual([m['content'] for m in self.history()['messages']], ['test.txt 읽어줘.', 'fixture'])

    def test_filesystem_expired_proposal_cannot_be_denied_or_allowed(self):
        self.filesystem()
        self.answer_after('read_file', {'path': str(self.root / 'test.txt')}, 'unused')
        jid = self.create('test.txt 읽어줘.')
        pending = self.wait(jid)['pending']
        with self.backend._lock:
            core = self.backend._jobs[jid]['core']
            expires = self.backend._jobs[jid]['turn'].proposals[0]['expires_at']
            core.clock = lambda: expires + 1
        for allow in (False, True):
            response = self.client.post('/tool-chat/jobs/' + jid + '/approval',
                headers={'X-Local-AI-Control': 'web-console'}, json={
                    'proposal_id': pending['proposal_id'], 'browser_generation': 1,
                    'allow': allow, 'scope': pending['scope']})
            self.assertEqual(response.status_code, 409)
        job = self.wait(jid, ('FAILED',))
        self.assertEqual(job['error_code'], 'approval_expired')
        self.assertFalse(job['reservation_owned'])
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.history()['messages'], [])

    def test_filesystem_reset_revokes_old_proposal_and_run(self):
        self.filesystem()
        self.answer_after('read_file', {'path': str(self.root / 'test.txt')}, 'fixture')
        jid = self.create('test.txt 읽어줘.')
        pending = self.wait(jid)['pending']
        response = self.client.post('/sessions/' + self.session + '/reset', json={})
        self.assertEqual(response.status_code, 200, response.json)
        for allow in (False, True):
            response = self.client.post('/tool-chat/jobs/' + jid + '/approval',
                headers={'X-Local-AI-Control': 'web-console'}, json={
                    'proposal_id': pending['proposal_id'], 'browser_generation': 1,
                    'allow': allow, 'scope': pending['scope']})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.history()['messages'], [])
        self.assertEqual(self.wait(jid, ('CANCELLED',))['state'], 'CANCELLED')
        self.runtime.result = ToolCallResult(({'type':'text','text':'fixture'},), None, False)
        job = self.complete('test.txt 읽어줘.')
        self.assertEqual(job['state'], 'COMPLETED', job)
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(len(self.history()['messages']), 2)

    def test_deny_cannot_target_another_sessions_proposal(self):
        self.filesystem()
        self.answer_after('read_file', {'path': str(self.root / 'test.txt')}, 'unused')
        first = self.create('test.txt 읽어줘.')
        first_pending = self.wait(first)['pending']
        other_session = self.client.post('/sessions', json={}).json['session_id']
        second = self.create('test.txt 읽어줘.', session=other_session)
        second_pending = self.wait(second)['pending']
        response = self.client.post('/tool-chat/jobs/' + first + '/approval',
            headers={'X-Local-AI-Control':'web-console'}, json={
                'proposal_id':second_pending['proposal_id'], 'browser_generation':1,'allow':False})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wait(first)['pending']['proposal_id'], first_pending['proposal_id'])
        self.assertEqual(self.wait(second)['pending']['proposal_id'], second_pending['proposal_id'])
        self.assertEqual(self.runtime.sends, 0)

    def test_successful_wrong_file_does_not_satisfy_requested_file(self):
        self.filesystem()
        self.runtime.result = ToolCallResult(({'type':'text','text':'fixture'},), None, False)
        self.answer_after('read_file', {'path':str(self.root/'test.txt')}, 'other.txt contains fixture')
        job = self.complete('other.txt 읽어줘.')
        self.assertEqual(job['state'], 'FAILED', job)
        self.assertEqual(job['error_code'], 'tool_evidence_missing')
        self.assertEqual(self.history()['messages'], [])
        self.assertEqual(self.runtime.sends, 1)

    def compound_model(self, *, batch=True, file_error=False, file_first=False, answer=None):
        fs=self.filesystem();self.backend.catalog=ManagedCatalog(self.manager)
        aliases={e.original_name:e.alias for e in self.backend.catalog.discover()}
        time_result=self.runtime.result
        file_result=ToolCallResult(({'type':'text','text':'fixture failure' if file_error else 'fixture'},),None,file_error)
        original=self.runtime.complete
        def complete(oid):
            saved=self.runtime.result
            self.runtime.result=file_result if self.runtime.records[oid]['server_id']==fs else time_result
            try:return original(oid)
            finally:self.runtime.result=saved
        patched=patch.object(self.runtime,'complete',side_effect=complete);patched.start();self.addCleanup(patched.stop)
        async def model(messages,info):
            returns=[p for m in messages for p in m.parts if getattr(p,'part_kind','')=='tool-return']
            calls=[ToolCallPart(aliases['get_current_time'],{'timezone':'Asia/Seoul'},tool_call_id='time'),
                   ToolCallPart(aliases['read_file'],{'path':str(self.root/'test.txt')},tool_call_id='file')]
            if file_first:calls.reverse()
            if not returns:return ModelResponse(parts=calls if batch else calls[:1])
            if not batch and len(returns)==1:return ModelResponse(parts=calls[1:])
            return ModelResponse(parts=[TextPart(answer or '2026-11-20T09:30:00+09:00; test.txt: fixture')])
        @asynccontextmanager
        async def models(initial):yield FunctionModel(model),{}
        self.backend.model_factory=models

    def decide_next(self,jid,allow):
        job=self.wait(jid);self.assertEqual(job['state'],'WAITING_APPROVAL',job)
        p=job['pending']
        response=self.client.post('/tool-chat/jobs/'+jid+'/approval',headers={'X-Local-AI-Control':'web-console'},
            json={'proposal_id':p['proposal_id'],'browser_generation':1,'allow':allow,'scope':p['scope']})
        self.assertEqual(response.status_code,202,response.json)
        return p,response.json['job']

    def test_multiple_manual_calls_have_distinct_approvals_and_operations(self):
        self.compound_model()
        jid=self.create('current time and read test.txt')
        first,receipt=self.decide_next(jid,True)
        self.assertEqual(receipt['state'],'WAITING_APPROVAL')
        second,_=self.decide_next(jid,True)
        self.assertNotEqual(first['proposal_id'],second['proposal_id'])
        job=self.wait(jid,('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job);self.assertEqual(self.runtime.sends,2)
        self.assertEqual(len({o['operation_id'] for o in job['operations']}),2)
        self.assertEqual([c['status'] for c in job['result']['tool_calls']],['SUCCEEDED','SUCCEEDED'])
        self.assertEqual(len(self.history()['messages']),2)
        response=self.client.post('/tool-chat/jobs/'+jid+'/approval',headers={'X-Local-AI-Control':'web-console'},
            json={'proposal_id':first['proposal_id'],'browser_generation':1,'allow':True,'scope':first['scope']})
        self.assertEqual(response.status_code,409);self.assertEqual(self.runtime.sends,2)

    def test_multiple_rounds_allow_all_without_trust_or_popups(self):
        self.compound_model(batch=False)
        self.app.extensions['session_store'].set_permission(self.session,'ALLOW_ALL')
        job=self.wait(self.create('current time and read test.txt'),('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job);self.assertEqual(self.runtime.sends,2)
        self.assertEqual(job['result']['evidence']['coverage'],'matched')
        self.assertEqual(len(self.history()['messages']),2)
        admitted=[a for a in self.manager.audit() if a['event']=='operation_admitted']
        self.assertEqual([a['method'] for a in admitted],['AUTO','AUTO'])

    def test_partial_deny_keeps_success_without_fabricated_claims(self):
        self.compound_model(answer='FABRICATED_ALL_SUCCESS')
        jid=self.create('current time and read test.txt')
        self.decide_next(jid,True);self.decide_next(jid,False)
        job=self.wait(jid,('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job);self.assertEqual(self.runtime.sends,1)
        self.assertEqual([c['status'] for c in job['result']['tool_calls']],['SUCCEEDED','DENIED'])
        self.assertNotIn('FABRICATED_ALL_SUCCESS',job['result']['message'])
        self.assertIn('2026-11-20T09:30:00+09:00',job['result']['message'])
        self.assertEqual(len(self.history()['messages']),2)

    def test_partial_tool_error_keeps_other_success_without_false_history(self):
        self.compound_model(file_error=True,file_first=True,answer='FABRICATED_ALL_SUCCESS')
        self.app.extensions['session_store'].set_permission(self.session,'ALLOW_ALL')
        job=self.wait(self.create('current time and read test.txt'),('COMPLETED','FAILED'))
        self.assertEqual(job['state'],'COMPLETED',job);self.assertEqual(self.runtime.sends,2)
        self.assertEqual([c['status'] for c in job['result']['tool_calls']],['FAILED','SUCCEEDED'])
        self.assertNotIn('FABRICATED_ALL_SUCCESS',str(self.history()))
        self.assertIn('fixture failure',job['result']['message'])
        self.assertEqual(job['result']['evidence']['coverage'],'partial')
        self.assertEqual(len(self.history()['messages']),2)

    def test_workspace_evidence_capture_failure_releases_reservation_without_inference(self):
        with patch('agent_poc.web_backend.workspace_roots', side_effect=ValueError('fixture stale root')):
            job = self.wait(self.create(), ('FAILED',))
        self.assertFalse(job['reservation_owned'])
        self.assertIsNone(job['result'])
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.history()['messages'], [])


if __name__ == '__main__':
    unittest.main()

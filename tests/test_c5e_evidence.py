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
        self.runtime.result = ToolCallResult(({'type': 'text', 'text': 'fixture failure'},), None, True)
        self.answer_after('get_current_time', {'timezone': 'Asia/Seoul'}, '조회에 성공했습니다.')
        job = self.complete('현재 한국 시간을 알려줘.')
        self.assertEqual(job['state'], 'FAILED', job)
        self.assertIsNone(job['result'])
        self.assertEqual(self.history()['messages'], [])

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
        job = self.wait(jid, ('FAILED',))
        self.assertEqual(job['error_code'], 'approval_denied')
        self.assertIsNone(job['pending'])
        self.assertFalse(job['reservation_owned'])
        for allow in (False, True):
            repeated = self.client.post('/tool-chat/jobs/' + jid + '/approval',
                headers={'X-Local-AI-Control': 'web-console'}, json={
                    'proposal_id': pending['proposal_id'], 'browser_generation': 1,
                    'allow': allow, 'scope': pending['scope']})
            self.assertEqual(repeated.status_code, 409)
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.history()['messages'], [])

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

    def test_existing_single_tool_budget_blocks_second_execution_and_partial_history(self):
        self.filesystem()
        self.backend.catalog = ManagedCatalog(self.manager)
        aliases = {e.original_name:e.alias for e in self.backend.catalog.discover()}
        async def model(messages, info):
            returned = any(getattr(p,'part_kind','') == 'tool-return' for m in messages for p in m.parts)
            name = 'read_file' if returned else 'get_current_time'
            args = {'path':str(self.root/'test.txt')} if returned else {'timezone':'Asia/Seoul'}
            return ModelResponse(parts=[ToolCallPart(aliases[name], args, tool_call_id=name)])
        @asynccontextmanager
        async def models(initial): yield FunctionModel(model), {}
        self.backend.model_factory = models
        job = self.complete('현재 한국 시간과 test.txt 내용을 읽어서 알려줘.')
        self.assertEqual(job['state'], 'FAILED', job)
        self.assertEqual(job['error_code'], 'tool_call_limit')
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(self.history()['messages'], [])

    def test_workspace_evidence_capture_failure_releases_reservation_without_inference(self):
        with patch('agent_poc.web_backend.workspace_roots', side_effect=ValueError('fixture stale root')):
            job = self.wait(self.create(), ('FAILED',))
        self.assertFalse(job['reservation_owned'])
        self.assertIsNone(job['result'])
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.history()['messages'], [])


if __name__ == '__main__':
    unittest.main()

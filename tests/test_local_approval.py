"""R2 stdin decisions against real ApprovalCore + official Deferred, no live MCP."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from agent_mcp_fakes import FakeMCPService
from agent_poc.approval_core import ApprovalCore, ApprovalError
from agent_poc.approved_bridge import DeferredFixtureRun
from agent_poc.fixture_executor import FixtureExecutor
from agent_poc.mcp_catalog import MCPCatalog
from agent_poc.local_approval import request_decision, console_line, discard_pending_console_input
from agent_poc.readiness_gate import ReadOnlyScope, ReadinessDenied


class LocalApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = FakeMCPService()
        self.tool = self.service.add()
        self.catalog = MCPCatalog(self.service)
        self.entry = self.catalog.discover()[0]
        self.executor = FixtureExecutor({('alpha', 'convert'): {'value': 42}})
        self.now = 100.
        self.core = ApprovalCore(self.catalog, self.executor, clock=lambda: self.now)
        self.core.create_session('local')
        def model(messages, info):
            if any(getattr(p, 'part_kind', '') == 'tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('fixture complete')])
            return ModelResponse(parts=[ToolCallPart(self.entry.alias, {'value': 2}, tool_call_id='local-1')])
        self.turn = DeferredFixtureRun(self.core, 'local', FunctionModel(model))
        await self.turn.start('fixture')
        self.pid = self.turn.proposals[0]['proposal_id']
        self.output = []

    async def asyncTearDown(self):
        self.executor.gate.set()
        await self.core.drain_fixtures()
        self.assertEqual(self.service.executions, 0)

    async def decide(self, text, effect=None):
        async def reader(seconds):
            self.assertGreater(seconds, 0)
            if effect: effect()
            return text
        return await request_decision(self.turn, self.pid, reader=reader, output=self.output.append)

    async def test_allow_does_not_execute_until_resume(self):
        self.assertTrue(await self.decide('ALLOW '+self.pid))
        self.assertEqual(self.executor.calls, 0)
        await self.turn.resume()
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.core.status(self.turn.run_id)['state'], 'COMPLETED')

    async def test_deny_no_execution_no_answer(self):
        self.assertFalse(await self.decide('DENY '+self.pid))
        await self.turn.resume()
        self.assertEqual(self.executor.calls, 0)
        self.assertIsNone(self.turn.answer)

    async def test_short_y_allows_exact_displayed_request_once(self):
        self.assertTrue(await self.decide('y'))
        self.assertIn(self.pid, '\n'.join(self.output))
        await self.turn.resume()
        self.assertEqual(self.executor.calls, 1)
        with self.assertRaises(ApprovalError): await self.decide('y')

    async def test_short_n_denies_without_execution(self):
        self.assertFalse(await self.decide('n'))
        await self.turn.resume()
        self.assertIsNone(self.turn.answer)
        self.assertEqual(self.executor.calls, 0)

    async def test_short_y_does_not_bypass_changed_arguments(self):
        def change(): self.core._proposals[self.pid]['arguments_json'] = '{"value":3}'
        with self.assertRaises(ApprovalError): await self.decide('y', change)
        self.assertEqual(self.executor.calls, 0)

    async def test_no_input_fail_closed(self):
        with self.assertRaises(ApprovalError): await self.decide(None)
        self.assertEqual(self.executor.calls, 0)

    async def test_lowercase_not_accepted(self):
        with self.assertRaises(ApprovalError): await self.decide('allow '+self.pid)

    async def test_blank_input(self):
        with self.assertRaises(ApprovalError): await self.decide('')
        self.assertEqual(self.core.status(self.turn.run_id)['state'], 'CANCELLED')

    async def test_wrong_id(self):
        with self.assertRaises(ApprovalError): await self.decide('ALLOW forged')

    async def test_expiry_while_typing(self):
        with self.assertRaises(ApprovalError):
            await self.decide('ALLOW '+self.pid, lambda: setattr(self, 'now', 220.))
        self.assertEqual(self.executor.calls, 0)

    async def test_revision_change_while_typing(self):
        with self.assertRaises(ApprovalError):
            await self.decide('ALLOW '+self.pid, lambda: self.core.reset_session('local'))

    async def test_permission_change_while_typing(self):
        def change(): self.tool['source']['permission_generation'] += 1
        with self.assertRaises(ApprovalError): await self.decide('ALLOW '+self.pid, change)

    async def test_duplicate_decision_blocked(self):
        await self.decide('ALLOW '+self.pid)
        with self.assertRaises(ApprovalError): await self.decide('ALLOW '+self.pid)
        self.assertEqual(self.executor.calls, 0)

    async def test_budget_insufficient(self):
        self.core._runs[self.turn.run_id]['deadline'] = 125.
        with self.assertRaisesRegex(ApprovalError, 'local_execution_budget_insufficient'):
            await self.decide('ALLOW '+self.pid)

    async def test_reader_timeout_no_execution(self):
        async def reader(seconds): raise asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            await request_decision(self.turn, self.pid, reader=reader, output=self.output.append)
        self.assertEqual(self.core.status(self.turn.run_id)['state'], 'CANCELLED')

    async def test_displays_exact_server_tool_arguments_and_expiry(self):
        await self.decide('DENY '+self.pid)
        text = '\n'.join(self.output)
        for value in (self.pid, self.turn.run_id, self.entry.server_id, self.entry.original_name,
                      '"value": 2', 'expires_utc_estimate'):
            self.assertIn(value, text)

    async def test_narrow_scope_cannot_expand_and_rejects_outside(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scope = ReadOnlyScope('x', 'filesystem', root, frozenset({'read_file'}))
            self.assertEqual(scope.tools, {'read_file'})
            with self.assertRaises(ReadinessDenied): scope.validate('list_directory', {'path': str(root)})
            with self.assertRaises(ReadinessDenied):
                ReadOnlyScope('x', 'filesystem', root, frozenset({'write_file'}))
            with self.assertRaises((ReadinessDenied, FileNotFoundError)):
                scope.validate('read_file', {'path': str(root.parent/'outside.txt')})

    async def test_unapproved_harness_never_starts(self):
        from agent_poc.c4_r2 import run
        with self.assertRaises(PermissionError): await run()

    async def test_redirected_input_rejected(self):
        with patch('sys.stdin.isatty', return_value=False):
            self.assertIsNone(await console_line(1))

    async def test_console_typed_allow(self):
        chars = iter('ALLOW example\r')
        with patch('sys.stdin.isatty', return_value=True), patch('sys.platform', 'win32'), \
             patch('msvcrt.kbhit', return_value=True), patch('msvcrt.getwch', side_effect=lambda: next(chars)), \
             patch('builtins.print'):
            self.assertEqual(await console_line(2), 'ALLOW example')

    async def test_console_eof(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.platform', 'win32'), \
             patch('msvcrt.kbhit', return_value=True), patch('msvcrt.getwch', return_value='\x1a'):
            self.assertIsNone(await console_line(1))

    async def test_console_timeout(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.platform', 'win32'), \
             patch('msvcrt.kbhit', return_value=False):
            self.assertIsNone(await console_line(.01))

    async def test_pre_prompt_paste_and_enter_are_discarded(self):
        chars = iter('\r\nALLOW old-proposal\r')
        pending = list(chars)
        with patch('sys.stdin.isatty', return_value=True), patch('sys.platform', 'win32'), \
             patch('msvcrt.kbhit', side_effect=lambda: bool(pending)), \
             patch('msvcrt.getwch', side_effect=lambda: pending.pop(0)):
            discard_pending_console_input()
        self.assertEqual(pending, [])
        self.assertEqual(self.executor.calls, 0)

    async def test_empty_enter_after_prompt_still_not_approval(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.platform', 'win32'), \
             patch('msvcrt.kbhit', return_value=True), patch('msvcrt.getwch', return_value='\r'), \
             patch('builtins.print'):
            self.assertEqual(await console_line(1), '')
        with self.assertRaises(ApprovalError): await self.decide('')

    async def test_pre_prompt_input_overflow_fails_closed(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.platform', 'win32'), \
             patch('msvcrt.kbhit', return_value=True), patch('msvcrt.getwch', return_value='x'):
            with self.assertRaisesRegex(ApprovalError, 'local_console_input_overflow'):
                discard_pending_console_input()

    async def test_argument_change_at_authoritative_server(self):
        def change():
            self.core._proposals[self.pid]['arguments_json'] = '{"value":3}'
        with self.assertRaisesRegex(ApprovalError, 'local_proposal_changed'):
            await self.decide('ALLOW '+self.pid, change)
        self.assertEqual(self.executor.calls, 0)


if __name__ == '__main__': unittest.main()

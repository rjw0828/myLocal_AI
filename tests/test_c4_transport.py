"""C-4 pre-approval checks. Every review here is memory-only, never live=True."""
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart

from agent_poc.live_transport import LaunchReview, ReviewedStdioRuntime, isolated_reviewed_service
from agent_poc.readiness_gate import IsolationLayout, ReadOnlyScope, ReadinessDenied
from agent_poc.mcp_catalog import MCPCatalog
from agent_poc.service_execution import FakeServiceExecutor
from agent_poc.service_approval import ServiceApprovalCore
from agent_poc.approved_bridge import DeferredFixtureRun
from agent_poc.approval_core import ApprovalError
from mcp_manager.models import ServerConfig, ToolInfo


class C4TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='local-ai-c4-memory-'); self.addCleanup(temp.cleanup)
        self.layout = IsolationLayout(Path(temp.name)); self.layout.root.mkdir()
        self.fixture = self.layout.root/'test.txt'; self.fixture.write_text('C4 harmless fixture 314159', encoding='utf-8')
        self.profile = ServerConfig('time-memory', sys.executable)
        self.review = LaunchReview([self.profile], base=self.layout.base)
        self.runtime = ReviewedStdioRuntime(self.review); self.addCleanup(self.runtime.shutdown)
        self.runtime.tools = [ToolInfo('get_current_time', 'Fixed time fixture',
            {'type': 'object', 'properties': {'timezone': {'type': 'string'}},
             'required': ['timezone'], 'additionalProperties': False}, {'type': 'object'})]
        self.service = isolated_reviewed_service(self.layout, self.runtime, [self.profile]); self.addCleanup(self.service.shutdown)
        self.sid = self.service.create_server('time-memory', 'C4 memory time')['server_id']
        self.runtime.manager._client_factory.bind(ReadOnlyScope(self.sid, 'time'))
        self.service.start_server(self.sid)
        self.service.set_permission(self.sid, 'get_current_time', True)
        self.catalog = MCPCatalog(self.service); self.entry = self.catalog.discover()[0]
        self.core = ServiceApprovalCore(self.catalog, FakeServiceExecutor(self.service))
        self.core.create_session('virtual')

    async def asyncTearDown(self):
        self.runtime.finish_gate.set(); self.runtime.dispatch_gate.set()
        await self.core.drain_fixtures()

    def turn(self):
        def model(messages, info):
            if any(getattr(p, 'part_kind', '') == 'tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('Fixture received')])
            return ModelResponse(parts=[ToolCallPart(self.entry.alias, {'timezone': 'Asia/Seoul'}, tool_call_id='c4-call')])
        return DeferredFixtureRun(self.core, 'virtual', FunctionModel(model))

    async def test_default_review_is_memory_not_live(self):
        self.assertFalse(self.review.live)
        self.assertEqual(self.runtime.handshakes[self.sid], 'memory-fixture')

    async def test_agent_bridge_approved_single_call(self):
        run = self.turn(); await run.start('fixed explicit lookup')
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.runtime.attempts, 0)
        p = run.proposals[0]; run.decide(p['proposal_id'], True); await run.resume()
        self.assertEqual(run.answer, 'Fixture received'); self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(self.runtime.attempts, 1)
        self.assertEqual(self.core.status(run.run_id)['state'], 'COMPLETED')

    async def test_agent_denied_zero_sends(self):
        run = self.turn(); await run.start('lookup')
        run.decide(run.proposals[0]['proposal_id'], False); await run.resume()
        self.assertIsNone(run.answer); self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.runtime.attempts, 0)

    async def test_no_tool_permission_after_launch_approval(self):
        self.service.set_permission(self.sid, 'get_current_time', False)
        self.assertEqual(self.catalog.discover(), ())
        self.assertEqual(self.runtime.sends, 0)

    async def test_duplicate_turn_resume_no_replay(self):
        run = self.turn(); await run.start('lookup'); run.decide(run.proposals[0]['proposal_id'], True)
        await run.resume()
        with self.assertRaises(ApprovalError): await run.resume()
        self.assertEqual(self.runtime.sends, 1)

    async def test_restarting_requires_new_explicit_launch_review(self):
        self.service.stop_server(self.sid)
        with self.assertRaises(Exception): self.service.start_server(self.sid)
        self.assertEqual(self.runtime.sends, 0)

    async def test_wrong_or_missing_review_is_rejected(self):
        with self.assertRaises(ReadinessDenied): ReviewedStdioRuntime(None)
        wrong = ServerConfig('wrong', sys.executable, ('--not-approved',))
        with self.assertRaises(ReadinessDenied): self.review.consume_start(wrong)

    async def test_reviewed_source_change_invalidates_start(self):
        review = LaunchReview([self.profile], base=self.layout.base, reviewed_files=[self.fixture])
        self.fixture.write_text('changed fixture', encoding='utf-8')
        with self.assertRaises(ReadinessDenied): review.consume_start(self.profile)

    async def test_expired_start_is_rejected(self):
        review = LaunchReview([self.profile], base=self.layout.base)
        with patch('agent_poc.live_transport.time.monotonic', return_value=review._deadline + 1):
            with self.assertRaises(ReadinessDenied): review.consume_start(self.profile)

    async def test_other_isolation_layout_is_rejected(self):
        other = self.layout.base/'other'; other.mkdir()
        with self.assertRaises(ReadinessDenied):
            isolated_reviewed_service(IsolationLayout(other), self.runtime, [self.profile])

    async def test_sdk_header_failure_not_retried(self):
        self.runtime.mode = 'sdk_header'; run = self.turn(); await run.start('lookup')
        run.decide(run.proposals[0]['proposal_id'], True)
        with self.assertRaises(ApprovalError): await run.resume()
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(self.core.status(run.run_id)['state'], 'UNCERTAIN')

    async def test_filesystem_root_must_match_review(self):
        from agent_poc.live_transport import ReviewedFactory
        other = self.layout.base/'other-root'; other.mkdir()
        factory = ReviewedFactory(self.runtime, LaunchReview([self.profile], base=self.layout.base))
        factory.bind(ReadOnlyScope('file-case', 'filesystem', other))
        with self.assertRaises(ReadinessDenied): factory(ServerConfig('file-case', sys.executable), None)


if __name__ == '__main__': unittest.main()

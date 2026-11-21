"""SDK owner-task preparation over memory fixtures; no stdio/network MCP server."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from agent_poc.approval_core import ApprovalError
from agent_poc.mcp_catalog import MCPCatalog
from agent_poc.owner_runtime import OwnerFakeRuntime
from agent_poc.service_execution import FakeServiceExecutor
from agent_poc.service_approval import ServiceApprovalCore
from mcp_manager.models import ServerConfig, MCPRuntimeError, ToolCallResult
from mcp_manager.manager import MCPManager


class OwnerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from mcp_manager.service import MCPService
        temp = TemporaryDirectory(prefix='local-ai-owner-memory-')
        self.addCleanup(temp.cleanup)
        self.runtime = OwnerFakeRuntime(wait_seconds=.5)
        self.addCleanup(self.runtime.shutdown)
        self.service = MCPService(session_database_path=Path(temp.name)/'session.sqlite3',
            database_path=Path(temp.name)/'registry.sqlite3',
            approved_configs=[ServerConfig('memory', sys.executable)], runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.sid = self.service.create_server('memory', 'SDK memory fixture')['server_id']
        self.service.start_server(self.sid)
        self.service.set_permission(self.sid, 'fixture_lookup', True)
        self.catalog = MCPCatalog(self.service); self.entry = self.catalog.discover()[0]
        self.executor = FakeServiceExecutor(self.service)
        self.core = ServiceApprovalCore(self.catalog, self.executor)
        self.core.create_session('virtual'); self.rid = self.core.start_run('virtual')

    async def asyncTearDown(self):
        self.runtime.dispatch_gate.set(); self.runtime.finish_gate.set()
        await self.core.drain_fixtures()

    def approve(self):
        p = self.core.issue(self.rid, 'owner-call', self.entry, {'value': 2})
        self.core.decide(self.rid, p['proposal_id'], True)
        return p

    async def call(self):
        return await self.core.execute(self.rid, 'owner-call', self.entry, {'value': 2})

    async def until(self, condition):
        for _ in range(500):
            if condition(): return
            await asyncio.sleep(.002)
        self.fail('Owner fixture checkpoint not reached')

    def oid(self): return next(iter(self.executor._records))

    async def test_single_sdk_dispatch_in_existing_owner(self):
        self.approve(); result = await self.call()
        self.assertEqual(result['structured_content'], {'value': 42})
        self.assertEqual(self.runtime.sends, 1)
        self.assertTrue(self.runtime.manager._host.running)
        self.assertEqual(self.core.operation(self.oid())['state'], 'COMPLETED')
        self.assertEqual(len(self.runtime.contexts), 1)

    async def test_no_approval_never_reaches_sdk(self):
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.call()
        self.assertEqual(self.runtime.sends, 0)

    async def test_duplicate_consumption_no_second_sdk_call(self):
        self.approve()
        result = await asyncio.gather(self.call(), self.call(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in result), 1)
        self.assertEqual(self.runtime.sends, 1)

    async def test_permission_change_at_owner_gate(self):
        self.approve(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        # Isolated DB mutation models a policy change not admitted through the
        # normal guarded API. The owner must still read current permission.
        self.service.registry.set_permission(self.sid, 'fixture_lookup', '', False)
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_permission_generation_change_at_owner_gate(self):
        self.approve(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        with self.service._lock: self.service._permission_generations[(self.sid, 'fixture_lookup')] += 1
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_cancel_before_owner_admission(self):
        self.approve(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        self.core.cancel(self.rid); self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)
        self.assertEqual(self.core.operation(self.oid())['state'], 'CANCELLED')

    async def test_schema_change_after_discovery_blocks_send(self):
        self.approve(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        with self.runtime.lock:
            self.runtime.tools[0] = replace(self.runtime.tools[0], description='changed after discovery')
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_connection_generation_change_blocks_send(self):
        self.approve(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        worker = self.runtime.manager._workers[self.sid]
        with worker.lock: worker.connection_generation = 'replaced-generation'
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_post_send_disconnect_uncertain_without_replay(self):
        self.approve(); self.runtime.mode = 'disconnect'
        with self.assertRaises(ApprovalError): await self.call()
        op = self.core.operation(self.oid())
        self.assertTrue(op['sent']); self.assertIsNone(op['accepted'])
        self.assertEqual(op['state'], 'UNCERTAIN')
        with self.assertRaises(ApprovalError): await self.call()
        self.assertEqual(self.runtime.sends, 1)

    async def test_sdk_header_mismatch_no_high_level_retry(self):
        self.approve(); self.runtime.mode = 'sdk_header'
        with self.assertRaises(ApprovalError): await self.call()
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_sdk_input_required_not_automatically_resumed(self):
        self.approve(); self.runtime.mode = 'sdk_input_required'
        with self.assertRaises(ApprovalError): await self.call()
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_duplicate_operation_stays_reserved_after_reconnect(self):
        self.approve(); await self.call(); oid = self.oid()
        self.runtime.stop_server(self.sid); self.runtime.start_server(self.sid)
        with self.assertRaises(MCPRuntimeError) as caught:
            self.runtime.manager.call_tool_once(self.sid, 'fixture_lookup', {'value': 2},
                operation_id=oid, before_send=lambda *_: self.fail('Duplicate may not be admitted'))
        self.assertEqual(caught.exception.code, 'mcp_duplicate_operation')
        self.assertEqual(self.runtime.sends, 1)

    async def test_single_dispatch_cannot_overwrite_existing_start_record(self):
        oid = self.runtime.manager._workers[self.sid].ready.operation_id
        with self.assertRaises(MCPRuntimeError) as caught:
            self.runtime.manager.call_tool_once(self.sid, 'fixture_lookup', {'value': 2},
                operation_id=oid, before_send=lambda *_: None)
        self.assertEqual(caught.exception.code, 'mcp_duplicate_operation')
        self.assertEqual(self.runtime.get_operation(self.sid, oid).kind, 'start')
        self.assertEqual(self.runtime.sends, 0)

    async def test_manager_callback_alone_cannot_bypass_service_admission(self):
        with self.assertRaises(MCPRuntimeError):
            await asyncio.to_thread(self.runtime.manager.call_tool_once, self.sid, 'fixture_lookup', {'value': 2},
                operation_id='not-a-service-operation', before_send=lambda *_: None)
        self.assertEqual(self.runtime.sends, 0)

    async def test_official_deferred_resumes_through_owner_sdk(self):
        from agent_poc.approved_bridge import DeferredFixtureRun
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        self.core.cancel(self.rid)
        def model(messages, info):
            returned = [p for m in messages for p in m.parts if getattr(p, 'part_kind', '') == 'tool-return']
            if returned:
                self.assertEqual(returned[-1].content['structured_content'], {'value': 42})
                return ModelResponse(parts=[TextPart('Fixture value 42')])
            return ModelResponse(parts=[ToolCallPart(self.entry.alias, {'value': 2}, tool_call_id='owner-deferred')])
        run = DeferredFixtureRun(self.core, 'virtual', FunctionModel(model))
        await run.start('fixture lookup'); self.assertEqual(self.runtime.sends, 0)
        run.decide(run.proposals[0]['proposal_id'], True); await run.resume()
        self.assertEqual(run.answer, 'Fixture value 42'); self.assertEqual(self.runtime.sends, 1)

    async def test_timeout_and_late_sdk_response_preserve_terminal_state(self):
        self.approve(); self.runtime.wait_seconds = .1; self.runtime.finish_gate.clear()
        with self.assertRaises(ApprovalError): await self.call()
        self.assertTrue(self.runtime.accepted_event.is_set())
        oid = self.oid(); self.assertEqual(self.core.operation(oid)['state'], 'TIMED_OUT')
        self.runtime.finish_gate.set()
        await self.until(lambda: self.runtime.details(self.sid, oid)['result'] is not None)
        self.assertEqual(self.core.operation(oid)['state'], 'TIMED_OUT')
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_owner_stop_before_send_resolves_queue(self):
        self.approve(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        await asyncio.to_thread(self.runtime.stop_server, self.sid)
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)
        self.assertTrue(all(c['closed'] and c['same_exit'] for c in self.runtime.contexts))

    async def test_owner_stop_after_send_never_claims_rollback(self):
        self.approve(); self.runtime.finish_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.accepted_event.is_set)
        await asyncio.to_thread(self.runtime.stop_server, self.sid)
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        self.assertTrue(all(c['closed'] and c['same_exit'] for c in self.runtime.contexts))

    async def test_result_is_error_remains_tool_failure(self):
        self.approve(); self.runtime.result = ToolCallResult((), None, True)
        with self.assertRaisesRegex(ApprovalError, 'tool_returned_error'): await self.call()
        self.assertEqual(self.core.operation(self.oid())['state'], 'FAILED')
        self.assertEqual(self.runtime.sends, 1)

    async def test_owner_context_closes_and_host_stops(self):
        self.approve(); await self.call()
        await asyncio.to_thread(self.runtime.shutdown)
        self.assertTrue(all(c['closed'] and c['same_exit'] for c in self.runtime.contexts))
        self.assertFalse(self.runtime.manager._host.running)

    async def test_default_real_factory_cannot_enter_single_dispatch(self):
        manager = MCPManager()
        try:
            with self.assertRaises(MCPRuntimeError) as caught:
                manager.call_tool_once('not-registered', 'fixture', {}, operation_id='blocked', before_send=lambda *_: None)
            self.assertEqual(caught.exception.code, 'mcp_execution_bridge_disabled')
        finally: manager.shutdown()
        self.assertEqual(self.runtime.sends, 0)


if __name__ == '__main__': unittest.main()

"""C-3B-1: real MCPService/temp registry, only in-memory Runtime/transport/model."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import inspect
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel

from agent_poc.approval_core import ApprovalCore, ApprovalError
from agent_poc.approved_bridge import DeferredFixtureRun, ApprovedFixtureBridge
from agent_poc.fake_runtime import FakeRuntime
from agent_poc.mcp_catalog import MCPCatalog
from agent_poc.service_approval import ServiceApprovalCore
from agent_poc.service_execution import FakeServiceExecutor, ExecutionError
from mcp_manager.models import MCPRuntimeError, ServerConfig, ToolCallResult
from mcp_manager.service import MCPService
from mcp_manager.sdk_policy import sdk_call_once


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='local-ai-c3b1-')
        self.addCleanup(directory.cleanup)
        self.runtime = FakeRuntime()
        self.service = MCPService(session_database_path=Path(directory.name)/'session.sqlite3',
            database_path=Path(directory.name)/'mcp.sqlite3',
            approved_configs=[ServerConfig('fixture-profile', sys.executable),
                              ServerConfig('independent-profile', sys.executable, ('--fake-fixture-only',))],
            runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.sid = self.service.create_server('fixture-profile', 'In-memory fixture')['server_id']
        self.service.start_server(self.sid)
        self.service.set_permission(self.sid, 'fixture_lookup', True)
        self.catalog = MCPCatalog(self.service)
        self.entry = self.catalog.discover()[0]
        self.executor = FakeServiceExecutor(self.service)
        self.now = 100.0
        self.core = ServiceApprovalCore(self.catalog, self.executor, clock=lambda: self.now)
        self.core.create_session('virtual')
        self.rid = self.core.start_run('virtual')

    async def asyncTearDown(self):
        self.runtime.dispatch_gate.set(); self.runtime.finish_gate.set()
        await self.core.drain_fixtures()

    def proposal(self, allow=True):
        p = self.core.issue(self.rid, 'call-1', self.entry, {'value': 2})
        if allow: self.core.decide(self.rid, p['proposal_id'], True)
        return p

    async def execute(self, args=None):
        return await self.core.execute(self.rid, 'call-1', self.entry, {'value': 2} if args is None else args)

    def oid(self): return next(iter(self.executor._records))

    async def until(self, predicate):
        for _ in range(200):
            if predicate(): return
            await asyncio.sleep(.002)
        self.fail('Fake operation did not reach expected checkpoint')

    async def test_service_approval_then_exactly_one_operation(self):
        p = self.proposal()
        self.assertEqual(len(self.service._tokens), 0)  # Proposal != service token
        result = await self.execute()
        self.assertEqual(result, {'content': [{'type': 'text', 'text': 'fixture result'}],
                                 'structured_content': {'value': 42}, 'is_error': False})
        op = self.core.operation(self.oid())
        self.assertEqual(op['state'], 'COMPLETED')
        self.assertEqual(op['history'], ['CREATED', 'VALIDATING', 'DISPATCHING', 'RUNNING', 'COMPLETED'])
        self.assertTrue(op['proposal_consumed']); self.assertTrue(op['service_token_consumed'])
        self.assertTrue(op['sent']); self.assertTrue(op['accepted'])
        self.assertEqual(self.runtime.sends, 1)
        self.assertEqual(len(self.runtime.records), 1)
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CONSUMED')
        self.assertEqual(len(self.service._tokens), 0)

    async def test_missing_or_pending_approval_blocks_execution(self):
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.execute()
        self.proposal(False)
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_deny_blocks_execution(self):
        p = self.proposal(False); self.core.decide(self.rid, p['proposal_id'], False)
        with self.assertRaisesRegex(ApprovalError, 'approval_denied'): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_duplicate_allow_and_execution(self):
        p = self.proposal(False)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda _: self.core.decide(self.rid, p['proposal_id'], True), range(4)))
        self.assertTrue(all(p['state'] == 'APPROVED' for p in outcomes))
        results = await asyncio.gather(self.execute(), self.execute(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(self.runtime.sends, 1)

    async def test_duplicate_tracker_operation_rejected(self):
        self.executor.register('fixed-id', self.sid)
        with self.assertRaisesRegex(ExecutionError, 'duplicate_operation'):
            self.executor.register('fixed-id', self.sid)
        self.assertEqual(self.runtime.sends, 0)

    async def test_duplicate_runtime_operation_does_not_send_twice(self):
        self.proposal(); await self.execute(); oid = self.oid()
        with self.assertRaisesRegex(MCPRuntimeError, 'already registered'):
            self.runtime.call_tool_once(self.sid, 'fixture_lookup', {'value': 2},
                operation_id=oid, before_send=lambda: self.fail('Duplicate may not reauthorize'))
        self.assertEqual(self.runtime.sends, 1)

    async def test_duplicate_core_operation_id_rejected(self):
        self.proposal(); await self.execute(); oid = self.oid()
        # A fresh call can never overwrite an existing operation, even if UUID
        # generation were to collide. Existing record is retained.
        self.core.issue(self.rid, 'call-2', self.entry, {'value': 2})
        pid, _ = self.core._calls[(self.rid, 'call-2')]; self.core.decide(self.rid, pid, True)
        with patch('agent_poc.service_approval.uuid4', return_value=SimpleNamespace(hex=oid)):
            with self.assertRaisesRegex(ApprovalError, 'duplicate_operation'):
                await self.core.execute(self.rid, 'call-2', self.entry, {'value': 2})
        self.assertEqual(self.runtime.sends, 1)

    async def test_arguments_tampering(self):
        self.proposal()
        with self.assertRaisesRegex(ApprovalError, 'approval_mismatch'): await self.execute({'value': 3})
        self.assertEqual(self.runtime.sends, 0)

    async def test_schema_permission_and_reconnect_before_execution(self):
        self.proposal()
        with self.runtime.lock:
            self.runtime.tools[0] = replace(self.runtime.tools[0], description='schema version changed')
        with self.assertRaisesRegex(ApprovalError, 'approval_stale'): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_block_before_execution(self):
        self.proposal(); self.service.set_permission(self.sid, 'fixture_lookup', False)
        with self.assertRaisesRegex(ApprovalError, 'approval_stale'): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_reconnect_before_execution(self):
        self.proposal(); self.service.stop_server(self.sid); self.service.start_server(self.sid)
        with self.assertRaisesRegex(ApprovalError, 'approval_stale'): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_expired_before_execution(self):
        p = self.proposal(); self.now = p['expires_at']
        with self.assertRaisesRegex(ApprovalError, 'approval_expired'): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_pre_send_failure_consumes_neither_authority(self):
        p = self.proposal(); self.runtime.mode = 'pre_fail'
        with self.assertRaisesRegex(ApprovalError, 'fake_pre_send_failure'): await self.execute()
        op = self.core.operation(self.oid())
        self.assertEqual(op['state'], 'FAILED'); self.assertFalse(op['sent'])
        self.assertFalse(op['proposal_consumed']); self.assertFalse(op['service_token_consumed'])
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CANCELLED')
        self.assertEqual(len(self.service._tokens), 0)

    async def test_forged_service_token_rejected_before_claim(self):
        with self.assertRaises(Exception):
            self.service.call_tool(self.sid, 'fixture_lookup', {'value': 2}, 'forged-token',
                operation_id='invalid-token', before_execute=lambda *_: self.fail('May not claim'))
        self.assertEqual(self.runtime.sends, 0)

    async def test_final_gate_rechecks_reconnect_during_validation(self):
        p = self.proposal(); self.runtime.dispatch_gate.clear()
        task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set)
        with self.runtime.lock: self.runtime.generations[self.sid] = 'new-connection'
        self.runtime.dispatch_gate.set()
        with self.assertRaisesRegex(ApprovalError, 'approval_stale'): await task
        op = self.core.operation(self.oid())
        self.assertTrue(op['service_token_consumed']); self.assertFalse(op['proposal_consumed'])
        self.assertFalse(op['sent']); self.assertEqual(self.runtime.sends, 0)

    async def test_final_gate_rechecks_schema_during_validation(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set)
        with self.runtime.lock: self.runtime.tools[0].input_schema['description'] = 'changed during wait'
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_service_guard_blocks_permission_race(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set)
        with self.assertRaises(Exception) as caught:
            self.service.set_permission(self.sid, 'fixture_lookup', False)
        self.assertEqual(caught.exception.code, 'mcp_busy')
        self.runtime.dispatch_gate.set(); await task
        self.assertEqual(self.runtime.sends, 1)

    async def test_expiry_at_final_gate_after_service_token_consumption(self):
        p = self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set); self.now = p['expires_at']
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)
        self.assertFalse(self.core.operation(self.oid())['proposal_consumed'])

    async def test_cancel_before_send_is_confirmed_unsent(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set); self.core.cancel(self.rid)
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        op = self.core.operation(self.oid())
        self.assertEqual(op['state'], 'CANCELLED'); self.assertTrue(op['cancel_confirmed'])
        self.assertFalse(op['sent']); self.assertEqual(self.runtime.sends, 0)

    async def test_cancelled_run_rejects_new_execution(self):
        self.proposal(); self.core.cancel(self.rid)
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.runtime.sends, 0)

    async def test_runtime_timeout_retains_operation_and_late_result(self):
        self.proposal(); self.runtime.finish_gate.clear(); self.runtime.wait_seconds = .01
        with self.assertRaisesRegex(ApprovalError, 'mcp_wait_timeout'): await self.execute()
        oid = self.oid(); op = self.core.operation(oid)
        self.assertEqual(op['state'], 'TIMED_OUT'); self.assertTrue(op['accepted']); self.assertTrue(op['unsettled'])
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        with self.assertRaises(ApprovalError): await self.execute()
        self.runtime.complete(oid); op = self.core.operation(oid)
        self.assertEqual(op['state'], 'TIMED_OUT'); self.assertEqual(op['observed_state'], 'SUCCEEDED')
        self.assertIsNotNone(op['observed_result'])
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        self.assertEqual(self.runtime.sends, 1)

    async def test_disconnect_after_send_is_uncertain_no_replay(self):
        self.proposal(); self.runtime.mode = 'disconnect'
        with self.assertRaisesRegex(ApprovalError, 'mcp_call_uncertain'): await self.execute()
        op = self.core.operation(self.oid())
        self.assertEqual(op['state'], 'UNCERTAIN'); self.assertTrue(op['sent']); self.assertIsNone(op['accepted'])
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.runtime.sends, 1)

    async def test_other_run_cannot_overlap_unresolved_operation_on_same_server(self):
        self.proposal(); self.runtime.finish_gate.clear(); self.runtime.wait_seconds = .01
        with self.assertRaises(ApprovalError): await self.execute()
        self.core.create_session('other'); other = self.core.start_run('other')
        p = self.core.issue(other, 'other-call', self.entry, {'value': 2})
        self.core.decide(other, p['proposal_id'], True)
        with self.assertRaisesRegex(ApprovalError, 'mcp_busy'):
            await self.core.execute(other, 'other-call', self.entry, {'value': 2})
        self.assertEqual(self.runtime.sends, 1)

    async def test_independent_server_not_blocked_by_uncertain_server(self):
        self.proposal(); self.runtime.finish_gate.clear(); self.runtime.wait_seconds = .01
        with self.assertRaises(ApprovalError): await self.execute()
        sid2 = self.service.create_server('independent-profile', 'Independent fake server')['server_id']
        self.service.start_server(sid2); self.service.set_permission(sid2, 'fixture_lookup', True)
        entry = next(entry for entry in self.catalog.discover() if entry.server_id == sid2)
        self.core.create_session('independent'); other = self.core.start_run('independent')
        p = self.core.issue(other, 'independent-call', entry, {'value': 2})
        self.core.decide(other, p['proposal_id'], True); self.runtime.finish_gate.set()
        result = await self.core.execute(other, 'independent-call', entry, {'value': 2})
        self.assertEqual(result['structured_content'], {'value': 42})
        self.assertEqual(self.runtime.sends, 2)

    async def test_unknown_transmission_is_not_reported_as_unsent(self):
        self.proposal(); self.runtime.mode = 'send_unknown'
        with self.assertRaises(ApprovalError): await self.execute()
        op = self.core.operation(self.oid())
        self.assertIsNone(op['sent']); self.assertIsNone(op['accepted'])
        self.assertEqual(op['state'], 'UNCERTAIN')
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.runtime.sends, 1)

    async def test_cancel_after_acceptance_discards_late_success(self):
        self.proposal(); self.runtime.finish_gate.clear(); self.runtime.wait_seconds = 1
        task = asyncio.create_task(self.execute()); await self.until(self.runtime.accepted_event.is_set)
        self.core.cancel(self.rid); self.runtime.finish_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        self.assertTrue(self.core.operation(self.oid())['cancel_requested'])
        self.assertEqual(self.core.operation(self.oid())['state'], 'UNCERTAIN')
        self.assertEqual(self.runtime.sends, 1)

    async def test_confirmed_cancel_is_distinct_from_request(self):
        self.proposal(); self.runtime.finish_gate.clear(); self.runtime.wait_seconds = 1
        task = asyncio.create_task(self.execute()); await self.until(self.runtime.accepted_event.is_set)
        oid = self.oid(); self.core.cancel(self.rid); self.runtime.confirm_cancel(oid)
        self.runtime.finish_gate.set()
        with self.assertRaises(ApprovalError): await task
        op = self.core.operation(oid)
        self.assertTrue(op['cancel_requested']); self.assertTrue(op['cancel_confirmed'])
        self.assertEqual(op['observed_state'], 'CANCELLED'); self.assertFalse(op['unsettled'])
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_runtime_confirmed_cancel_before_uncertainty_is_cancelled(self):
        self.proposal(); self.runtime.finish_gate.clear(); self.runtime.wait_seconds = 1
        task = asyncio.create_task(self.execute()); await self.until(self.runtime.accepted_event.is_set)
        oid = self.oid(); self.runtime.confirm_cancel(oid); self.runtime.finish_gate.set()
        with self.assertRaises(ApprovalError): await task
        op = self.core.operation(oid)
        self.assertEqual(op['state'], 'CANCELLED'); self.assertTrue(op['cancel_confirmed'])
        self.assertEqual(self.core.status(self.rid)['state'], 'CANCELLED')

    async def test_reset_before_final_send_cancels_proposal(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set)
        self.core.reset_session('virtual'); self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_expired_service_token_cannot_be_replaced_automatically(self):
        self.core.approval_seconds = self.core.run_seconds = 1000
        # Existing Run had an earlier deadline; extend it for this isolated case.
        self.core._runs[self.rid]['deadline'] = self.now + 1000
        self.service.clock = lambda: self.now
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.execute())
        await self.until(self.runtime.registered.is_set)
        self.now += 121; self.runtime.dispatch_gate.set()
        with self.assertRaisesRegex(ApprovalError, 'mcp_call_approval_required'): await task
        self.assertFalse(self.core.operation(self.oid())['proposal_consumed'])
        self.assertEqual(self.runtime.sends, 0); self.assertEqual(len(self.service._tokens), 0)

    async def test_late_result_read_is_rechecked_against_permissions(self):
        self.proposal(); await self.execute(); oid = self.oid()
        self.service.set_permission(self.sid, 'fixture_lookup', False)
        op = self.core.operation(oid)
        self.assertIsNone(op['observed_result']); self.assertIsNone(op['result'])
        self.assertTrue(op['result_access_denied'])

    async def test_cancel_request_with_unknown_operation_completion(self):
        self.proposal(); self.runtime.mode = 'disconnect'
        with self.assertRaises(ApprovalError): await self.execute()
        self.core.cancel(self.rid)
        op = self.core.operation(self.oid())
        self.assertTrue(op['cancel_requested']); self.assertFalse(op['cancel_confirmed'])
        self.assertTrue(op['unsettled'])

    async def test_outer_timeout_retains_thread_then_discards_late_result(self):
        self.proposal(); self.core.call_seconds = .2
        self.runtime.wait_seconds = 1; self.runtime.finish_gate.clear()
        task = asyncio.create_task(self.execute())
        await self.until(self.runtime.accepted_event.is_set)
        with self.assertRaisesRegex(ApprovalError, 'operation_timeout'): await task
        self.runtime.finish_gate.set(); await self.core.drain_fixtures()
        op = self.core.operation(self.oid())
        self.assertEqual(op['state'], 'TIMED_OUT'); self.assertEqual(op['observed_state'], 'SUCCEEDED')
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_error_tool_result_not_returned_as_success(self):
        self.proposal(); self.runtime.result = ToolCallResult(({'type': 'text', 'text': 'fixture denied'},), None, True)
        with self.assertRaisesRegex(ApprovalError, 'tool_returned_error'): await self.execute()
        self.assertEqual(self.core.operation(self.oid())['state'], 'FAILED')
        self.assertTrue(self.core.operation(self.oid())['observed_result']['is_error'])

    async def test_output_schema_invalid_result_fails(self):
        self.proposal(); self.runtime.result = ToolCallResult((), 'not an object', False)
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.core.operation(self.oid())['state'], 'FAILED')

    async def test_real_runtime_or_cross_service_injection_rejected(self):
        with self.assertRaises(TypeError): ApprovalCore(self.catalog, self.executor)
        with patch.object(self.service, '_runtime', object()):
            with self.assertRaises(TypeError): FakeServiceExecutor(self.service)
            with self.assertRaises(Exception) as caught:
                self.service.call_tool(self.sid, 'fixture_lookup', {'value': 2}, 'not-a-token',
                    operation_id='blocked', before_execute=lambda *_: None)
            self.assertEqual(caught.exception.code, 'mcp_execution_bridge_disabled')
        self.assertEqual(self.runtime.sends, 0)

    async def test_official_deferred_allow_and_normalized_result(self):
        self.core.cancel(self.rid)
        alias = self.entry.alias
        def model(messages, info):
            returns = [p for m in messages for p in m.parts if getattr(p, 'part_kind', '') == 'tool-return']
            if returns:
                self.assertEqual(returns[-1].content['structured_content']['value'], 42)
                return ModelResponse(parts=[TextPart('Fixture value is 42')])
            return ModelResponse(parts=[ToolCallPart(alias, {'value': 2}, tool_call_id='deferred-1')])
        run = DeferredFixtureRun(self.core, 'virtual', FunctionModel(model))
        await run.start('generic lookup')
        self.assertEqual(self.runtime.sends, 0)
        run.decide(run.proposals[0]['proposal_id'], True); await run.resume()
        self.assertEqual(run.answer, 'Fixture value is 42'); self.assertEqual(self.runtime.sends, 1)

    async def test_deferred_deny_drops_invented_answer(self):
        self.core.cancel(self.rid)
        def model(messages, info):
            if any(getattr(p, 'part_kind', '') == 'tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('Invented lookup value')])
            return ModelResponse(parts=[ToolCallPart(self.entry.alias, {'value': 2}, tool_call_id='denied-1')])
        run = DeferredFixtureRun(self.core, 'virtual', FunctionModel(model))
        await run.start('lookup'); run.decide(run.proposals[0]['proposal_id'], False); await run.resume()
        self.assertIsNone(run.answer); self.assertEqual(self.runtime.sends, 0)

    async def test_gemma_qwen_use_same_service_bridge(self):
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider
        bridge = ApprovedFixtureBridge(self.core, self.rid)
        for name in ('gemma4:12b', 'qwen3.5:9b'):
            bridge.check_model_schema(OllamaModel(name,
                provider=OllamaProvider(base_url='http://127.0.0.1:11434/v1')))
        self.assertEqual(self.runtime.sends, 0)


class SDKPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_public_session_method_single_request_on_header_mismatch(self):
        import anyio
        from unittest.mock import AsyncMock
        from mcp import ClientSession, MCPError
        from mcp.types import HEADER_MISMATCH
        read_send, read_receive = anyio.create_memory_object_stream(1)
        write_send, write_receive = anyio.create_memory_object_stream(1)
        try:
            session = ClientSession(read_receive, write_send)
            with patch.object(session, 'send_request', AsyncMock(side_effect=MCPError(HEADER_MISMATCH, 'fixture mismatch'))) as send:
                with self.assertRaises(MCPError):
                    await sdk_call_once(SimpleNamespace(session=session), 'fixture', {})
                self.assertEqual(send.await_count, 1)
        finally:
            for stream in (read_send, read_receive, write_send, write_receive): await stream.aclose()

    async def test_public_session_signature_supported(self):
        from mcp import ClientSession
        signature = inspect.signature(ClientSession.call_tool)
        self.assertIn('allow_input_required', signature.parameters)
        self.assertIn('allow_claimed', signature.parameters)

    async def test_single_session_request_not_high_level_retry(self):
        from mcp.types import CallToolResult
        calls = []
        async def send(name, arguments, **kwargs):
            calls.append(kwargs); return CallToolResult(content=[])
        result = await sdk_call_once(SimpleNamespace(session=SimpleNamespace(call_tool=send)), 'fixture', {})
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(calls, [{'allow_input_required': False, 'allow_claimed': False}])

    async def test_header_mismatch_never_retried(self):
        from mcp import MCPError
        from mcp.types import HEADER_MISMATCH
        calls = []
        async def send(*args, **kwargs):
            calls.append(1); raise MCPError(HEADER_MISMATCH, 'HEADER_MISMATCH fixture')
        with self.assertRaises(MCPError):
            await sdk_call_once(SimpleNamespace(session=SimpleNamespace(call_tool=send)), 'fixture', {})
        self.assertEqual(len(calls), 1)

    async def test_extra_protocol_round_not_automatically_driven(self):
        calls = []
        async def send(*args, **kwargs): calls.append(1); return object()
        with self.assertRaises(RuntimeError):
            await sdk_call_once(SimpleNamespace(session=SimpleNamespace(call_tool=send)), 'fixture', {})
        self.assertEqual(len(calls), 1)


if __name__ == '__main__': unittest.main()

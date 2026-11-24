"""C-3A policy/concurrency and official Deferred integration; fixtures only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

from pydantic_ai import DeferredToolResults
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel

from agent_poc.approval_core import ApprovalCore, ApprovalError
from agent_poc.approved_bridge import ApprovedFixtureBridge, DeferredFixtureRun
from agent_poc.fixture_executor import FixtureExecutor
from agent_poc.mcp_catalog import MCPCatalog
from agent_mcp_fakes import FakeMCPService


class Fixtures:
    def setup_core(self, **kwargs):
        self.service = FakeMCPService()
        self.tool = self.service.add()
        self.catalog = MCPCatalog(self.service)
        self.entry = self.catalog.discover()[0]
        self.executor = FixtureExecutor({('alpha', 'convert'): {'value': 42}})
        self.now = 100.0
        self.core = ApprovalCore(self.catalog, self.executor, clock=lambda: self.now, **kwargs)
        self.core.create_session('session-A')
        self.rid = self.core.start_run('session-A')

    def issue(self, call_id='call-1', rid=None):
        return self.core.issue(rid or self.rid, call_id, self.entry, {'value': 2})

    def approve(self):
        proposal = self.issue()
        self.core.decide(self.rid, proposal['proposal_id'], True)
        return proposal


class ProposalTests(Fixtures, unittest.TestCase):
    def setUp(self): self.setup_core()

    def test_server_generated_fields_and_pending(self):
        p = self.issue()
        for key in ('run_id', 'proposal_id', 'tool_call_id', 'server_id', 'tool_name',
                    'arguments_fingerprint', 'schema_fingerprint', 'source', 'expires_at', 'revision'):
            self.assertIn(key, p)
        self.assertEqual(p['state'], 'PENDING')
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.service.executions, 0)

    def test_forged_proposal(self):
        self.issue()
        with self.assertRaisesRegex(ApprovalError, 'proposal_unknown'):
            self.core.decide(self.rid, 'forged', True)

    def test_returned_proposal_mutation_does_not_authorize(self):
        p = self.issue(); pid = p['proposal_id']
        p['state'] = 'APPROVED'; p['source']['permission_generation'] = 999
        self.assertEqual(self.core.proposal(self.rid, pid)['state'], 'PENDING')
        self.assertNotEqual(self.core.proposal(self.rid, pid)['source'], p['source'])

    def test_proposal_cannot_be_used_by_another_run(self):
        p = self.issue()
        self.core.create_session('B'); other = self.core.start_run('B')
        with self.assertRaisesRegex(ApprovalError, 'proposal_unknown'):
            self.core.decide(other, p['proposal_id'], True)

    def test_duplicate_call_id_rejected(self):
        self.issue()
        with self.assertRaisesRegex(ApprovalError, 'duplicate_tool_call'): self.issue()

    def test_deny_is_terminal(self):
        p = self.issue(); self.core.decide(self.rid, p['proposal_id'], False)
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'DENIED')
        with self.assertRaisesRegex(ApprovalError, 'approval_denied'):
            self.core.decide(self.rid, p['proposal_id'], True)

    def test_expiry_before_allow(self):
        p = self.issue(); self.now = p['expires_at']
        with self.assertRaisesRegex(ApprovalError, 'approval_expired'):
            self.core.decide(self.rid, p['proposal_id'], True)
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'EXPIRED')

    def test_revision_change_invalidates(self):
        p = self.approve(); self.core.reset_session('session-A')
        self.assertEqual(self.core.status(self.rid)['state'], 'CANCELLED')
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CANCELLED')
        newer = self.core.start_run('session-A')
        self.assertEqual(self.core.status(newer)['revision'], 1)

    def test_permission_or_connection_or_schema_change_before_allow(self):
        changes = [('permission_generation', 2), ('connection_generation', 'reconnect'), ('config_hash', 'different')]
        for key, value in changes:
            with self.subTest(key=key):
                self.setup_core(); p = self.issue(); self.tool['source'][key] = value
                with self.assertRaisesRegex(ApprovalError, 'approval_stale'):
                    self.core.decide(self.rid, p['proposal_id'], True)
                self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CANCELLED')
        self.setup_core(); p = self.issue(); self.tool['input_schema']['description'] = 'changed'
        with self.assertRaisesRegex(ApprovalError, 'approval_stale'):
            self.core.decide(self.rid, p['proposal_id'], True)

    def test_simultaneous_allow_and_consumption_max_once(self):
        p = self.issue()
        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(lambda _: self.core.decide(self.rid, p['proposal_id'], True), range(8)))
        self.assertTrue(all(d['state'] == 'APPROVED' for d in decisions))
        def execute(_):
            try: return asyncio.run(self.core.execute(self.rid, 'call-1', self.entry, {'value': 2}))
            except ApprovalError as error: return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(execute, range(8)))
        self.assertEqual(outcomes.count({'value': 42}), 1)
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CONSUMED')

    def test_cancel_allow_race_never_executes(self):
        p = self.issue()
        def allow():
            try: self.core.decide(self.rid, p['proposal_id'], True)
            except ApprovalError: pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(allow); b = pool.submit(self.core.cancel, self.rid)
            a.result(); b.result()
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CANCELLED')
        self.assertEqual(self.executor.calls, 0)

    def test_non_boolean_decision(self):
        p = self.issue()
        with self.assertRaisesRegex(ApprovalError, 'invalid_decision'):
            self.core.decide(self.rid, p['proposal_id'], 'true')

    def test_forged_catalog_descriptor_not_a_server_proposal(self):
        from mcp_manager.registry import canonical_json
        descriptor = self.entry.descriptor
        descriptor['input_schema'] = {'type': 'object'}
        forged = replace(self.entry, descriptor_json=canonical_json(descriptor))
        with self.assertRaisesRegex(ApprovalError, 'catalog_entry_forged'):
            self.core.issue(self.rid, 'fake-call', forged, {'value': 2})

    def test_allow_deadline_rechecked_after_validation(self):
        p = self.issue()
        original = self.service.validate_catalog_arguments
        def slow(*args):
            result = original(*args); self.now = p['expires_at']; return result
        self.service.validate_catalog_arguments = slow
        with self.assertRaisesRegex(ApprovalError, 'approval_expired'):
            self.core.decide(self.rid, p['proposal_id'], True)
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'EXPIRED')

    def test_run_deadline_expires_without_proposal(self):
        self.now = self.core.status(self.rid)['deadline']
        self.assertEqual(self.core.status(self.rid)['state'], 'FAILED')
        self.assertEqual(self.core.status(self.rid)['error'], 'run_timeout')

    def test_deny_does_not_require_running_server(self):
        p = self.issue(); self.service.servers['alpha']['state'] = 'STOPPED'
        self.core.decide(self.rid, p['proposal_id'], False)
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'DENIED')

    def test_real_or_callable_executor_rejected(self):
        for executor in (self.service, lambda: 42, object()):
            with self.subTest(executor=type(executor).__name__):
                with self.assertRaises(TypeError): ApprovalCore(self.catalog, executor)

    def test_record_limits_and_unsettled_session(self):
        with self.assertRaisesRegex(ApprovalError, 'session_unsettled'): self.core.start_run('session-A')
        self.core.max_records = 1; self.issue()
        with self.assertRaisesRegex(ApprovalError, 'record_limit'): self.issue('second')


class ExecutionTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_core()

    async def asyncTearDown(self):
        self.executor.gate.set()
        await self.core.drain_fixtures()
        self.assertEqual(self.service.executions, 0)

    async def execute(self, arguments=None, call_id='call-1'):
        return await self.core.execute(self.rid, call_id, self.entry, {'value': 2} if arguments is None else arguments)

    async def test_no_proposal_or_pending_proposal_never_executes(self):
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.execute()
        self.issue()
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.execute()
        self.assertEqual(self.executor.calls, 0)

    async def test_exact_approved_call_returns_fixture_and_is_consumed(self):
        p = self.approve()
        self.assertEqual(await self.execute(), {'value': 42})
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CONSUMED')
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.execute()
        self.assertEqual(self.executor.calls, 1)
        self.core.finish(self.rid)
        self.assertEqual(self.core.status(self.rid)['state'], 'COMPLETED')

    async def test_arguments_tamper_invalidates_approval(self):
        self.approve()
        with self.assertRaisesRegex(ApprovalError, 'approval_mismatch'): await self.execute({'value': 3})
        self.assertEqual(self.executor.calls, 0)
        with self.assertRaises(ApprovalError): await self.execute()

    async def test_invalid_nonfinite_arguments_never_consume(self):
        self.approve()
        with self.assertRaisesRegex(ApprovalError, 'approval_mismatch'): await self.execute({'value': float('nan')})
        self.assertEqual(self.executor.calls, 0)

    async def test_expiry_after_allow_before_execute(self):
        p = self.approve(); self.now = p['expires_at']
        with self.assertRaisesRegex(ApprovalError, 'approval_expired'): await self.execute()
        self.assertEqual(self.executor.calls, 0)

    async def test_execute_deadline_rechecked_after_validation(self):
        p = self.approve(); original = self.service.validate_catalog_arguments
        def slow(*args):
            result = original(*args); self.now = p['expires_at']; return result
        self.service.validate_catalog_arguments = slow
        with self.assertRaisesRegex(ApprovalError, 'approval_expired'): await self.execute()
        self.assertEqual(self.executor.calls, 0)

    async def test_changes_after_allow_revalidated(self):
        for change in ('schema', 'permission', 'connection', 'config', 'blocked', 'stopped'):
            with self.subTest(change=change):
                self.setup_core(); self.approve()
                if change == 'schema': self.tool['input_schema']['description'] = 'new'
                elif change == 'permission': self.tool['source']['permission_generation'] += 1
                elif change == 'connection': self.tool['source']['connection_generation'] = 'new'
                elif change == 'config': self.tool['source']['config_hash'] = 'new'
                elif change == 'blocked': self.tool['allowed'] = False
                else: self.service.servers['alpha']['state'] = 'STOPPED'
                with self.assertRaisesRegex(ApprovalError, 'approval_stale'): await self.execute()
                self.assertEqual(self.executor.calls, 0)

    async def test_different_call_id_cannot_borrow_approval(self):
        self.approve()
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await self.execute(call_id='forged')
        self.assertEqual(self.executor.calls, 0)

    async def test_timeout_retains_operation_and_no_replay(self):
        self.core.call_seconds = .01; self.executor.gate.clear(); self.approve()
        with self.assertRaisesRegex(ApprovalError, 'operation_timeout'): await self.execute()
        state = self.core.status(self.rid); oid = state['active_operation']
        self.assertEqual(state['state'], 'UNCERTAIN'); self.assertTrue(state['unsettled'])
        self.assertEqual(self.core.operation(oid)['state'], 'RUNNING')
        with self.assertRaises(ApprovalError): await self.execute()
        self.executor.gate.set(); await self.core.drain_fixtures()
        self.assertEqual(self.core.operation(oid)['state'], 'SUCCEEDED')
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        with self.assertRaisesRegex(ApprovalError, 'session_unsettled'): self.core.start_run('session-A')
        self.assertEqual(self.executor.calls, 1)

    async def test_cancel_before_execution(self):
        p = self.approve(); self.core.cancel(self.rid)
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.core.proposal(self.rid, p['proposal_id'])['state'], 'CANCELLED')
        self.assertEqual(self.executor.calls, 0)

    async def test_cancel_after_admission_discards_late_result(self):
        self.executor.gate.clear(); self.approve()
        task = asyncio.create_task(self.execute())
        for _ in range(100):
            if self.executor.started.is_set(): break
            await asyncio.sleep(.002)
        self.assertTrue(self.executor.started.is_set())
        self.core.cancel(self.rid)
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        self.executor.gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_caller_task_cancel_retains_worker(self):
        self.executor.gate.clear(); self.approve()
        task = asyncio.create_task(self.execute())
        for _ in range(100):
            if self.executor.started.is_set(): break
            await asyncio.sleep(.002)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(self.core.status(self.rid)['unsettled'])
        self.executor.gate.set(); await self.core.drain_fixtures()
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')

    async def test_session_reset_after_admission_discards_result(self):
        self.executor.gate.clear(); self.approve(); task = asyncio.create_task(self.execute())
        for _ in range(100):
            if self.executor.started.is_set(): break
            await asyncio.sleep(.002)
        self.core.reset_session('session-A'); self.executor.gate.set()
        with self.assertRaisesRegex(ApprovalError, 'revision_changed'): await task

    async def test_sdk_style_second_send_cannot_reuse_consumed_proposal(self):
        self.approve(); await self.execute()
        # Model a hidden re-send, not an actual SDK/MCP connection.
        self.tool['input_schema']['description'] = 'schema changed during retry'
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.executor.calls, 1)

    async def test_fixture_failure_is_not_retried(self):
        self.executor.fail = True; self.approve()
        with self.assertRaisesRegex(ApprovalError, 'fixture_execution_failed'): await self.execute()
        with self.assertRaises(ApprovalError): await self.execute()
        self.assertEqual(self.executor.calls, 1)

    async def test_uncertain_run_does_not_block_other_session(self):
        self.core.call_seconds = .01; self.executor.gate.clear(); self.approve()
        with self.assertRaisesRegex(ApprovalError, 'operation_timeout'): await self.execute()
        self.core.create_session('independent')
        other = self.core.start_run('independent')
        self.assertEqual(self.core.status(other)['state'], 'RUNNING')
        self.core.reset_session('session-A')
        with self.assertRaisesRegex(ApprovalError, 'session_unsettled'): self.core.start_run('session-A')


class DeferredTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = FakeMCPService(); self.service.add()
        self.catalog = MCPCatalog(self.service)
        self.alias = self.catalog.discover()[0].alias
        self.executor = FixtureExecutor({('alpha', 'convert'): {'value': 42}})
        self.core = ApprovalCore(self.catalog, self.executor)
        self.core.create_session('virtual')
        self.model_calls = 0

    def model(self, messages, info):
        self.model_calls += 1
        returned = [p for m in messages for p in m.parts if getattr(p, 'part_kind', '') == 'tool-return']
        if returned:
            if 'denied' in str(returned[-1].content).lower():
                return ModelResponse(parts=[TextPart('Invented current time: 14:00')])
            return ModelResponse(parts=[TextPart('Fixture value is 42')])
        return ModelResponse(parts=[ToolCallPart(self.alias, {'value': 2}, tool_call_id='official-1')])

    def run_adapter(self): return DeferredFixtureRun(self.core, 'virtual', FunctionModel(self.model))

    async def asyncTearDown(self):
        self.executor.gate.set(); await self.core.drain_fixtures()
        self.assertEqual(self.service.executions, 0)

    async def test_deferred_proposal_allow_resume(self):
        run = self.run_adapter(); await run.start('generic question')
        self.assertEqual(self.executor.calls, 0)
        p = run.proposals[0]; run.decide(p['proposal_id'], True)
        await run.resume()
        self.assertEqual(run.answer, 'Fixture value is 42')
        self.assertEqual(self.core.status(run.run_id)['state'], 'COMPLETED')
        self.assertEqual(self.executor.calls, 1)

    async def test_deny_resumes_official_api_but_discards_hallucination(self):
        run = self.run_adapter(); await run.start('question')
        run.decide(run.proposals[0]['proposal_id'], False)
        await run.resume()
        self.assertEqual(self.model_calls, 2)
        self.assertIsNone(run.answer)
        self.assertEqual(self.core.status(run.run_id)['error'], 'approval_denied')
        self.assertEqual(self.executor.calls, 0)

    async def test_resume_without_decision(self):
        run = self.run_adapter(); await run.start('question')
        with self.assertRaisesRegex(ApprovalError, 'approval_required'): await run.resume()
        self.assertEqual(self.executor.calls, 0)

    async def test_boolean_injection_without_server_proposal_blocked(self):
        run = self.run_adapter()
        result = await run.agent.run('question')  # intentionally skip server proposal publication
        with self.assertRaisesRegex(ApprovalError, 'approval_required'):
            await run.agent.run(conversation=result.conversation,
                deferred_tool_results=DeferredToolResults(approvals={'official-1': True}))
        self.assertEqual(self.executor.calls, 0)

    async def test_cancel_while_waiting(self):
        run = self.run_adapter(); await run.start('question'); p = run.proposals[0]
        run.cancel()
        self.assertEqual(self.core.proposal(run.run_id, p['proposal_id'])['state'], 'CANCELLED')
        with self.assertRaises(ApprovalError): await run.resume()

    async def test_proposal_from_prior_turn_cannot_authorize_next(self):
        run = self.run_adapter(); await run.start('question'); p = run.proposals[0]
        run.decide(p['proposal_id'], True); await run.resume()
        other = self.run_adapter(); await other.start('next question')
        with self.assertRaisesRegex(ApprovalError, 'proposal_unknown'):
            other.decide(p['proposal_id'], True)
        self.assertEqual(self.executor.calls, 1)

    async def test_duplicate_resume_does_not_execute_again(self):
        run = self.run_adapter(); await run.start('question')
        run.decide(run.proposals[0]['proposal_id'], True); await run.resume()
        with self.assertRaisesRegex(ApprovalError, 'resume_not_available'): await run.resume()
        self.assertEqual(self.executor.calls, 1)

    async def test_normal_text_needs_no_proposals(self):
        run = DeferredFixtureRun(self.core, 'virtual', FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart('General explanation')])) )
        await run.start('knowledge question')
        self.assertEqual(run.answer, 'General explanation')
        self.assertEqual(run.proposals, [])
        self.assertEqual(self.executor.calls, 0)

    async def test_two_deferred_rounds_use_official_loop_and_fresh_proposals(self):
        def model(messages, info):
            returned = [p for m in messages for p in m.parts if getattr(p, 'part_kind', '') == 'tool-return']
            if len(returned) >= 2: return ModelResponse(parts=[TextPart('Both fixtures complete')])
            return ModelResponse(parts=[ToolCallPart(self.alias, {'value': 2},
                                                    tool_call_id='step-' + str(len(returned) + 1))])
        run = DeferredFixtureRun(self.core, 'virtual', FunctionModel(model))
        await run.start('two operations'); first = run.proposals[0]
        run.decide(first['proposal_id'], True); await run.resume()
        second = run.proposals[0]
        self.assertNotEqual(first['proposal_id'], second['proposal_id'])
        self.assertEqual(second['state'], 'PENDING')
        run.decide(second['proposal_id'], True); await run.resume()
        self.assertEqual(run.answer, 'Both fixtures complete')
        self.assertEqual(self.executor.calls, 2)

    async def test_batch_denial_cancels_other_proposals(self):
        def model(messages, info):
            if any(getattr(p, 'part_kind', '') == 'tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('Invented data after denial')])
            return ModelResponse(parts=[ToolCallPart(self.alias, {'value': 2}, tool_call_id='first'),
                                        ToolCallPart(self.alias, {'value': 3}, tool_call_id='second')])
        run = DeferredFixtureRun(self.core, 'virtual', FunctionModel(model))
        await run.start('two operations'); first, second = run.proposals
        run.decide(first['proposal_id'], False); await run.resume()
        self.assertEqual(self.core.proposal(run.run_id, second['proposal_id'])['state'], 'CANCELLED')
        self.assertIsNone(run.answer); self.assertEqual(self.executor.calls, 0)

    async def test_gemma_qwen_share_approved_bridge_preparation(self):
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider
        bridge = ApprovedFixtureBridge(self.core, self.core.start_run('virtual'))
        for name in ('gemma4:12b', 'qwen3.5:9b'):
            bridge.check_model_schema(OllamaModel(name,
                provider=OllamaProvider(base_url='http://127.0.0.1:11434/v1')))
        self.assertEqual(self.executor.calls, 0)


if __name__ == '__main__': unittest.main()

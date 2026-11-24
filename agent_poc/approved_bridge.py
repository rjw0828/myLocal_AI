"""C-3A official Deferred adapter with server proposals and fixture execution only."""
import asyncio

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, Tool, ToolDenied
from pydantic_ai.usage import UsageLimits

from .mcp_bridge import MCPBridge
from .approval_core import ApprovalCore, ApprovalError


class ApprovedFixtureBridge(MCPBridge):
    def __init__(self, core, run_id):
        from .service_approval import ServiceApprovalCore
        if type(core) not in (ApprovalCore, ServiceApprovalCore):
            raise TypeError('Server-owned ApprovalCore is required')
        self.core, self.run_id = core, run_id
        super().__init__(core.catalog)

    def _tool(self, entry):
        baseline = super()._tool(entry)

        async def validate(ctx, **arguments):
            self.catalog.validate(entry, arguments)

        async def dispatch(ctx, **arguments):
            return await self.core.execute(self.run_id, ctx.tool_call_id, entry, arguments)

        tool = Tool.from_schema(dispatch, name=entry.alias, description=baseline.description,
            json_schema=entry.descriptor['input_schema'], takes_ctx=True,
            sequential=True, args_validator=validate)
        tool.metadata = baseline.metadata
        return tool


class DeferredFixtureRun:
    """Pause/resume bookkeeping only. Official Agent performs every model/tool round.

    No auto-approval, polling loop, HTTP API or production MCP integration.
    The caller must present decisions via decide(), then explicitly resume().
    """
    def __init__(self, core, session_id, model, *, instructions=None, model_settings=None, message_history=None,context_factory=None):
        self.core = core
        self.run_id = core.start_run(session_id)
        self.bridge = ApprovedFixtureBridge(core, self.run_id)
        if context_factory is not None:
            def workspace_context():return context_factory(self.bridge.entries)
            instructions=[instructions or '',workspace_context]
        self.agent = Agent(model, toolsets=[self.bridge.toolset], instructions=instructions,
                           model_settings=model_settings,
                           output_type=[str, DeferredToolRequests], retries=0)
        self._conversation, self._pending, self._started, self._busy = None, [], False, False
        self.answer = None
        self.message_history = message_history
        self._limits = UsageLimits(request_limit=8, tool_calls_limit=16)

    @property
    def proposals(self):
        return [self.core.proposal(self.run_id, pid) for pid in self._pending]

    def decide(self, proposal_id, allow):
        return self.core.decide(self.run_id, proposal_id, allow)

    async def start(self, prompt):
        if self._started or self._busy: raise ApprovalError('run_already_started')
        self._started = True
        return await self._advance(prompt=prompt)

    async def resume(self):
        if not self._started or not self._pending or self._busy:
            raise ApprovalError('resume_not_available')
        proposals = self.proposals
        state = self.core.status(self.run_id)
        if state['state'] in {'FAILED', 'CANCELLED', 'UNCERTAIN', 'COMPLETED'}:
            raise ApprovalError(state['error'] or 'run_closed')
        if any(p['state'] not in {'APPROVED','DENIED'} for p in proposals):
            raise ApprovalError('approval_required')
        decisions = DeferredToolResults(approvals={p['tool_call_id']:
            ToolDenied('User denied; lookup did not execute') if p['state']=='DENIED' else True for p in proposals})
        return await self._advance(decisions=decisions)

    async def _advance(self, *, prompt=None, decisions=None):
        if self._busy: raise ApprovalError('run_busy')
        self._busy = True
        try:
            status = self.core.status(self.run_id)
            if status['state'] in {'FAILED', 'CANCELLED', 'UNCERTAIN', 'COMPLETED'}:
                raise ApprovalError(status['error'] or 'run_closed')
            timeout = max(.000001, status['deadline'] - self.core.clock())
            kwargs = {'usage_limits': self._limits, 'timeout': timeout}
            if decisions is None:
                result = await self.agent.run(prompt, message_history=self.message_history, **kwargs)
            else:
                result = await self.agent.run(conversation=self._conversation,
                                               deferred_tool_results=decisions, **kwargs)
            if self.core.status(self.run_id)['state'] in {'FAILED', 'CANCELLED', 'UNCERTAIN'}:
                raise ApprovalError('late_result_discarded')
            if isinstance(result.output, DeferredToolRequests):
                if result.output.calls or not result.output.approvals:
                    raise ApprovalError('unsupported_deferred_request')
                entries = {entry.alias: entry for entry in self.bridge.entries}
                pending = []
                for call in result.output.approvals:
                    if call.tool_name not in entries: raise ApprovalError('unknown_tool_alias')
                    proposal = self.core.issue(self.run_id, call.tool_call_id,
                                               entries[call.tool_name], call.args_as_dict())
                    pending.append(proposal['proposal_id'])
                self._pending, self._conversation = pending, result.conversation
            else:
                self.core.finish(self.run_id)
                self.answer, self._pending = result.output, []
            return self.core.status(self.run_id)
        except asyncio.CancelledError:
            self.core.cancel(self.run_id)
            raise
        except Exception:
            self.core.fail(self.run_id, 'agent_run_failed')
            self.answer = None
            raise
        finally:
            self._busy = False

    def cancel(self):
        self.answer = None
        return self.core.cancel(self.run_id)

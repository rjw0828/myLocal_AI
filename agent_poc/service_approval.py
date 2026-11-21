"""Reuse C-3A proposals; consume only inside the MCPService final Fake send gate."""
import asyncio
from copy import deepcopy
from uuid import uuid4

from mcp_manager.registry import fingerprint
from .approval_core import ApprovalCore, ApprovalError, TERMINAL
from .service_execution import FakeServiceExecutor, ExecutionError


class ServiceApprovalCore(ApprovalCore):
    _executor_type = FakeServiceExecutor

    def __init__(self, catalog, executor, *, policy=None, **kwargs):
        from .approval_policy import ApprovalPolicy
        if policy is not None and type(policy) is not ApprovalPolicy:
            raise TypeError('Explicit server-owned ApprovalPolicy required')
        self.policy=policy
        super().__init__(catalog, executor, **kwargs)
        if catalog.service is not executor.service:
            raise TypeError('Catalog and execution must share the same MCPService')

    def start_run(self, session_id):
        with self._lock:
            rid=super().start_run(session_id)
            if self.policy is not None:self._runs[rid]['policy_revision']=self.policy.revision()
            return rid

    def _refresh(self,run):
        super()._refresh(run)
        if self.policy is not None and run['state'] not in TERMINAL and 'policy_revision' in run:
            try:changed=run['policy_revision']!=self.policy.revision()
            except Exception:
                self._end(run,'UNCERTAIN' if run['unsettled'] else 'FAILED','approval_policy_unavailable')
                return
            if changed:self._end(run,'UNCERTAIN' if run['unsettled'] else 'FAILED','approval_policy_changed')

    def issue(self,rid,call_id,entry,arguments):
        with self._lock:
            proposal=super().issue(rid,call_id,entry,arguments)
            if self.policy is not None:
                self._proposals[proposal['proposal_id']]['policy_revision']=self._runs[rid]['policy_revision']
            return self.proposal(rid,proposal['proposal_id'])

    def policy_decision(self,rid,pid):
        if self.policy is None:raise ApprovalError('approval_policy_not_configured')
        with self._lock:
            self._open(rid);proposal=self._proposal(rid,pid)
            if proposal['state']!='PENDING':raise ApprovalError('proposal_not_pending')
            _,entry=self._calls[(rid,proposal['tool_call_id'])]
            import json
            args=json.loads(proposal['arguments_json'])
            self._validate(self._runs[rid],proposal,entry,args)
            decision=self.policy.evaluate(entry,args)
            self.policy.audit('policy_decision',run_id=rid,proposal_id=pid,server_id=entry.server_id,
                tool=entry.original_name,arguments_fingerprint=proposal['arguments_fingerprint'],**decision.to_dict())
            return decision

    def decide(self,rid,pid,allow):
        with self._lock:
            if self.policy is not None and allow:
                decision=self.policy_decision(rid,pid)
                if decision.action=='BLOCK':
                    self.fail(rid,decision.reason);raise ApprovalError(decision.reason)
            result=super().decide(rid,pid,allow)
            if self.policy is not None:
                self._proposals[pid]['approval_method']='MANUAL'
                try:self.policy.audit('manual_allow' if allow else 'manual_deny',run_id=rid,proposal_id=pid)
                except Exception:
                    self.fail(rid,'policy_audit_failed');raise
            return self.proposal(rid,pid)

    def apply_policy(self,rid,pid):
        with self._lock:
            decision=self.policy_decision(rid,pid)
            if decision.action=='AUTO_APPROVE':
                super().decide(rid,pid,True)
                self._proposals[pid]['approval_method']='AUTO'
                try:self.policy.audit('auto_approved',run_id=rid,proposal_id=pid,reason=decision.reason)
                except Exception:
                    self.fail(rid,'policy_audit_failed');raise
            elif decision.action=='BLOCK':self.fail(rid,decision.reason)
            return decision

    async def execute(self, rid, call_id, entry, arguments):
        with self._lock:
            run = self._open(rid)
            binding = self._calls.get((rid, call_id))
            if binding is None: raise ApprovalError('approval_required')
            pid, original = binding
            proposal = self._proposal(rid, pid)
            if proposal['state'] != 'APPROVED': raise ApprovalError('approval_required')
            if run['active_operation'] is not None: raise ApprovalError('operation_unresolved')
            if original != entry:
                self._end(run, 'FAILED', 'approval_mismatch')
                raise ApprovalError('approval_mismatch')
            self._validate(run, proposal, entry, arguments)
            oid = uuid4().hex
            if oid in self._operations: raise ApprovalError('duplicate_operation')
            self.executor.register(oid, entry.server_id)
            self._operations[oid] = {'operation_id': oid, 'run_id': rid}
            run['state'], run['active_operation'], run['unsettled'] = 'EXECUTING', oid, False

            def claim(current, args):
                # Called under MCPService server gate AND FakeRuntime's
                # connection/schema lock. Never rediscover/reenter service here.
                with self._lock:
                    self._open(rid)
                    if (proposal['state'] != 'APPROVED' or run['active_operation'] != oid
                            or fingerprint(current) != proposal['catalog_fingerprint']
                            or fingerprint(args) != proposal['arguments_fingerprint']):
                        self._end(run, 'FAILED', 'approval_stale')
                        raise ApprovalError('approval_stale')
                    def consume():
                        proposal['state'] = 'CONSUMED'
                        run['unsettled'] = True
                    if self.policy is None:consume()
                    else:self.policy.consume(proposal,entry,args,oid,consume)

            task = asyncio.create_task(self.executor.execute_authorized(oid, entry, deepcopy(arguments), claim))
            self._tasks.add(task)
            task.add_done_callback(lambda done: self._settle_service(done, oid, rid))
            wait = min(self.call_seconds, run['deadline'] - self.clock())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=max(wait, .000001))
        except (TimeoutError, asyncio.CancelledError) as error:
            self.executor.mark_timeout(oid)
            with self._lock:
                self._end(run, 'UNCERTAIN', 'caller_cancelled' if isinstance(error, asyncio.CancelledError) else 'operation_timeout')
            self.executor.request_cancel(oid)
            if isinstance(error, asyncio.CancelledError): raise
            raise ApprovalError('operation_timeout') from None
        except ExecutionError as error:
            info = self.executor.observe(oid)
            with self._lock:
                if info['unsettled'] or info['state'] in {'UNCERTAIN', 'TIMED_OUT'}:
                    self._end(run, 'UNCERTAIN', error.code)
                elif info['state'] == 'CANCELLED' and run['state'] not in TERMINAL:
                    self._end(run, 'CANCELLED', error.code)
                elif run['state'] not in TERMINAL:
                    self._end(run, 'FAILED', error.code)
            raise ApprovalError(error.code) from None
        with self._lock:
            self._open(rid)
            # Results, too, are withheld if permission/schema changed after
            # transport completion and before publication to the Agent.
            self._validate(run, proposal, entry, arguments)
            return deepcopy(result)

    def _settle_service(self, task, oid, rid):
        info = self.executor.observe(oid)  # no Core lock while entering Runtime
        with self._lock:
            self._tasks.discard(task)
            run = self._runs[rid]
            if task.cancelled():
                self._end(run, 'UNCERTAIN', 'operation_completion_unknown')
                run['unsettled'] = True
                return
            error = task.exception()  # retrieve even after caller abandoned its wait
            if self.policy is not None:
                try:self.policy.audit('operation_outcome',run_id=rid,operation_id=oid,
                    state=info['state'],unsettled=info['unsettled'],error=info['error'])
                except Exception:
                    run['policy_audit_error']='policy_audit_failed'
                    if run['state'] not in TERMINAL:
                        self._end(run,'UNCERTAIN' if info['unsettled'] else 'FAILED','policy_audit_failed')
            run['unsettled'] = info['unsettled']
            if not info['unsettled']: run['active_operation'] = None
            if info['state'] in {'UNCERTAIN', 'TIMED_OUT'}:
                self._end(run, 'UNCERTAIN', info['error'] or 'operation_unresolved')
            elif run['state'] not in TERMINAL:
                if info['state'] == 'CANCELLED': self._end(run, 'CANCELLED', info['error'])
                elif error is None: run['state'] = 'RUNNING'
                else: self._end(run, 'FAILED', getattr(error, 'code', 'operation_failed'))

    def operation(self, oid):
        with self._lock:
            if oid not in self._operations: raise ApprovalError('operation_unknown')
        return self.executor.observe(oid)

    def cancel(self, rid):
        state = super().cancel(rid)
        if state['active_operation'] is not None:
            self.executor.request_cancel(state['active_operation'])
        return state

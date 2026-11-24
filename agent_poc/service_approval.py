"""Reuse C-3A proposals; consume only inside the MCPService final Fake send gate."""
import asyncio
from copy import deepcopy
from uuid import uuid4

from mcp_manager.registry import fingerprint
from .approval_core import ApprovalCore, ApprovalError, TERMINAL
from .service_execution import FakeServiceExecutor, ExecutionError


class ServiceApprovalCore(ApprovalCore):
    _executor_type = FakeServiceExecutor

    def __init__(self, catalog, executor, *, policy=None, permission=None, permission_reader=None, permission_guard=None, **kwargs):
        from .approval_policy import ApprovalPolicy
        if policy is not None and type(policy) is not ApprovalPolicy:
            raise TypeError('Explicit server-owned ApprovalPolicy required')
        self.policy=policy
        self.permission, self.permission_reader, self.permission_guard = permission, permission_reader, permission_guard
        super().__init__(catalog, executor, **kwargs)
        if catalog.service is not executor.service:
            raise TypeError('Catalog and execution must share the same MCPService')

    def start_run(self, session_id):
        with self._lock:
            rid=super().start_run(session_id)
            self._runs[rid]['permission'] = deepcopy(self.permission if self.permission is not None else
                self.permission_reader(session_id) if self.permission_reader else {'mode':'MANUAL','manual_generation':0})
            if self.policy is not None:self._runs[rid]['policy_revision']=self.policy.revision()
            return rid

    def _refresh(self,run):
        super()._refresh(run)
        # Session permission is checked per proposal and at final admission.
        # Legacy project/server trust edits no longer govern other Sessions.

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
            decision=self.policy.evaluate(entry,args,permission=self._runs[rid]['permission'],
                permission_reader=(lambda:self.permission_reader(self._runs[rid]['session_id'])) if self.permission_reader else None,
                permission_guard=self.permission_guard)
            self.policy.audit('policy_decision',run_id=rid,proposal_id=pid,server_id=entry.server_id,
                tool=entry.original_name,arguments_fingerprint=proposal['arguments_fingerprint'],**decision.to_dict())
            return decision

    def decide(self,rid,pid,allow):
        if type(allow) is not bool:raise ApprovalError('invalid_decision')
        with self._lock:
            if self.policy is not None and allow:
                decision=self.policy_decision(rid,pid)
                if decision.action=='BLOCK':
                    self.fail(rid,decision.reason);raise ApprovalError(decision.reason)
            if allow:
                result=super().decide(rid,pid,allow)
            else:
                run=self._open(rid);proposal=self._proposal(rid,pid)
                if proposal['state']!='PENDING':raise ApprovalError('proposal_closed')
                proposal['state']='DENIED'
                run['state']='WAITING_APPROVAL' if any(p['run_id']==rid and p['state']=='PENDING' for p in self._proposals.values()) else 'RUNNING'
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
        manual_required = False
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
            self._operations[oid] = {'operation_id': oid, 'run_id': rid, 'proposal_id': pid}
            run['state'], run['active_operation'], run['unsettled'] = 'EXECUTING', oid, False

            def claim(current, args):
                nonlocal manual_required
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
                    else:
                        try:
                            self.policy.consume(proposal,entry,args,oid,consume,permission=run['permission'],
                                permission_reader=(lambda:self.permission_reader(run['session_id'])) if self.permission_reader else None,
                                permission_guard=self.permission_guard)
                        except Exception as error:
                            manual_required = getattr(error,'code',None) == 'session_permission_requires_manual'
                            if manual_required:
                                self._end(run,'FAILED','session_permission_requires_manual')
                            raise

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
            if manual_required:
                raise ApprovalError('session_permission_requires_manual') from None
            info = self.executor.observe(oid)
            if (error.code=='tool_returned_error' and not info['unsettled']
                    and info['sent'] is True and info['accepted'] is True
                    and (info.get('result') or {}).get('is_error') is True):
                with self._lock:
                    self._open(rid)
                    self._validate(run,proposal,entry,arguments)
                return deepcopy(info['result'])
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
                elif (getattr(error,'code',None)=='tool_returned_error' and info['sent'] is True
                      and info['accepted'] is True and (info.get('result') or {}).get('is_error') is True):
                    run['state']='RUNNING'  # A settled Tool error is data for Agent.run.
                else: self._end(run, 'FAILED', getattr(error, 'code', 'operation_failed'))

    def operation(self, oid):
        with self._lock:
            if oid not in self._operations: raise ApprovalError('operation_unknown')
        return self.executor.observe(oid)

    def operation_ids(self):
        # Snapshot under the Core gate; release it before observing Runtime to
        # preserve the owner Runtime -> Core admission lock order.
        with self._lock:return tuple(self._operations)

    def cancel(self, rid):
        state = super().cancel(rid)
        if state['active_operation'] is not None:
            self.executor.request_cancel(state['active_operation'])
        return state

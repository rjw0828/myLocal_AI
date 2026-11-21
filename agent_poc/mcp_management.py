"""Local-owner management facade; no HTTP, Agent loop or process auto-start.

Callbacks are trusted local UI decisions, never model-supplied functions.
The caller must inject the existing reviewed execution runtime for real tests.
"""
from copy import deepcopy
from pathlib import Path
import shutil
from threading import RLock
from uuid import uuid4

from mcp_manager.models import ServerConfig
from mcp_manager.profile_requests import ProfileRequests, approve_request, read_manifest
from mcp_manager.registry import config_data, fingerprint
from mcp_manager.service import MCPService
from .approval_policy import ApprovalPolicy
from .mcp_catalog import MCPCatalog
from .readiness_gate import local_path, no_reparse, ReadOnlyScope
from .service_approval import ServiceApprovalCore
from .service_execution import FakeServiceExecutor


class ManagementError(RuntimeError):
    pass


def experimental_path(value):
    path = local_path(value)
    protected = Path('F:/LLM/AI_Project/myLocalAI')
    if path == protected or path.is_relative_to(protected):
        raise ManagementError('Operating worktree is forbidden')
    for parent in (path, *path.parents):
        if parent.exists(): no_reparse(parent)
    return path


def public_config(config):
    """Normal views never reveal environment values or potentially secret args."""
    return {'executable': config.executable, 'argument_count': len(config.args),
            'cwd': config.cwd, 'environment_keys': [key for key, _ in config.env],
            'config_fingerprint': fingerprint(config_data(config))}


class MCPManagement:
    def __init__(self, base, *, runtime_factory, contract_binder=None, session_database_path=None):
        self.base = experimental_path(base)
        repository=Path(__file__).resolve().parents[1]
        if self.base.is_relative_to(repository) and not self.base.is_relative_to(repository/'data'):
            raise ManagementError('Private management data inside this worktree must be under ignored data/')
        self.base.mkdir(parents=True, exist_ok=True)
        # Existing filenames are fixed, explicit and never sourced from env.
        for name in ('mcp.sqlite3','virtual_sessions.sqlite3','approval_policy.sqlite3',
                     'mcp_approved_profiles.json','mcp_profile_requests'):
            experimental_path(self.base / name)
        self.manifest = self.base / 'mcp_approved_profiles.json'
        self.requests = ProfileRequests(self.base / 'mcp_profile_requests', self.manifest)
        self.policy = ApprovalPolicy(self.base / 'approval_policy.sqlite3')
        self._runtime_factory, self._binder = runtime_factory, contract_binder
        self._runtime, self._tests, self._scopes, self._lock = None, {}, {}, RLock()
        self.service = MCPService(session_database_path=experimental_path(session_database_path) if session_database_path else self.base / 'virtual_sessions.sqlite3',
            database_path=self.base / 'mcp.sqlite3', approved_configs=self._profiles(),
            runtime_factory=self._get_runtime)
        self.catalog = MCPCatalog(self.service)

    def _profiles(self):
        experimental_path(self.manifest)
        return [ServerConfig(p['profile_id'], p['executable'], p.get('args',()), p.get('cwd'), p.get('env',()))
                for p in read_manifest(self.manifest)['profiles']]

    def _get_runtime(self):
        if self._runtime is None: self._runtime = self._runtime_factory()
        return self._runtime

    def list_servers(self):
        return self.service.list_servers()

    def details(self, server_id):
        row = self.service.registry.get(server_id)
        config = ServerConfig(row['profile_id'], **row['config'])
        state = self.policy.snapshot()
        return {**self.service.get_server(server_id), **public_config(config),
                'options': state.get('server_options',{}).get(server_id, {'root':None,'timeout':30}),
                'approval_mode': state['overrides'].get(server_id,state['project_mode']),
                'auto_enabled': state['auto_enabled'],
                'trusts': [r for r in state['trusts'].values() if r['server_id']==server_id]}

    def add_server(self, name, executable, arguments, *, confirm, cwd=None, env=(),
                   root=None, timeout=30, mode=None):
        """confirm sees exact arguments; approves persistence ONLY, never launch."""
        with self._lock:
            if not isinstance(name,str) or not name.strip() or len(name)>128:
                raise ManagementError('Display name is required (max 128)')
            if any(s['name'].casefold()==name.strip().casefold() for s in self.list_servers()):
                raise ManagementError('Duplicate display name')
            resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
            if not resolved: raise ManagementError('Executable was not found')
            experimental_path(Path(resolved).absolute())
            if cwd is not None: experimental_path(cwd)
            if not Path(resolved).is_file():raise ManagementError('Executable was not found')
            config = ServerConfig('draft', resolved, arguments, cwd, env)
            # Validate optional metadata before persisting any profile.
            if type(timeout) not in (int,float) or not 0 < timeout <= 300:
                raise ManagementError('Call wait timeout must be positive and <=300')
            if root is not None:
                root=experimental_path(root)
                if not root.is_dir():raise ManagementError('Root must be an existing directory')
                no_reparse(root)
            if mode is not None:
                from .approval_policy import ApprovalMode
                mode=ApprovalMode(mode).value
            if any(s['config_hash']==fingerprint(config_data(config)) for s in self.service.registry.list_servers()):
                raise ManagementError('Execution profile already registered')
            request=self.requests.create(name.strip(),'stdio',config.executable,list(config.args),cwd=config.cwd,env=config.env)
            def review(record):
                return confirm({'purpose':'SAVE_PROFILE_ONLY','name':record['name'],
                    **public_config(config),'args':list(config.args),'root':str(root) if root else None,
                    'timeout':timeout,'mode':mode}) is True
            if not approve_request(self.requests,request['request_id'],review):
                self.requests.delete(request['request_id']);return None
            self.requests.delete(request['request_id'])
            self.service.replace_local_profiles(self._profiles())
            server=self.service.create_server(request['profile_id'],name.strip())
            sid=server['server_id']
            try:
                self.policy.set_server_options(sid,root=root,timeout=timeout)
                if mode is not None:self.policy.set_mode(mode,server_id=sid)
                self.policy.audit('server_registered',server_id=sid,config_fingerprint=fingerprint(config_data(config)))
            except Exception:
                self.service.delete_server(sid)
                raise ManagementError('Registration settings failed; stopped registration rolled back. Approved profile was preserved.') from None
            return server

    def test_connection(self, server_id, *, confirm):
        """Explicit owner consent: start/handshake/discovery, ZERO tool calls."""
        with self._lock:
            row=self.service.registry.get(server_id)
            config=self.service.profiles()[row['profile_id']]
            options=self.details(server_id)['options']
            review={**public_config(config),'args':list(config.args),'server_id':server_id,
                    'options':deepcopy(options),
                    'purpose':'START_AND_DISCOVER_ONLY','tool_execution':False}
            if confirm(review) is not True:return {'consented':False,'started':False}
            if self.service.registry.get(server_id)['config_hash']!=row['config_hash']:
                raise ManagementError('Configuration changed during consent')
            if self.details(server_id)['options']!=options:
                raise ManagementError('Root or timeout changed during consent')
            if self.service.get_server(server_id)['status']['state']!='STOPPED':
                raise ManagementError('Connection test requires a stopped server')
            runtime=self._get_runtime()
            actual=ServerConfig(server_id,config.executable,config.args,config.cwd,config.env)
            binding=fingerprint({'config':config_data(actual),'options':options})
            previous=self._scopes.get(server_id)
            if previous is not None and previous[0]!=binding:
                raise ManagementError('Execution contract changed; stop, delete and register a new server')
            scope=previous[1] if previous is not None else (
                self._binder(runtime,actual,deepcopy(options)) if self._binder else None)
            if scope is not None and (type(scope) is not ReadOnlyScope or scope.server_id!=server_id):
                raise ManagementError('Trusted binder returned an invalid contract')
            if scope is not None and str(scope.root or '')!=str(options['root'] or ''):
                raise ManagementError('Verified Root differs from declared Root')
            self._scopes[server_id]=(binding,scope)
            self.policy.bind_contract(server_id,scope)
            try:
                status=self.service.start_server(server_id)
                tools=self.service.list_tools(server_id)
            except Exception:
                try:self.service.stop_server(server_id)
                finally:self.policy.invalidate_server(server_id)
                raise ManagementError('Connection failed; no automatic retry') from None
            self.policy.audit('connection_discovered',server_id=server_id,tool_count=len(tools))
            return {'consented':True,'started':True,'status':status,'tools':tools,
                    'execution_contract_verified':scope is not None}

    def tools(self, server_id):return self.service.list_tools(server_id)

    def set_permission(self, server_id, tool, allowed):
        if type(allowed) is not bool:raise ManagementError('Explicit Allow/Block required')
        result=self.service.set_permission(server_id,tool,allowed)
        self.policy.invalidate_server(server_id,reason='permission_changed')
        return result

    def set_mode(self, mode, *, server_id=None, enable_auto=False):
        if server_id is not None:self.service.get_server(server_id)
        self.policy.set_mode(mode,server_id=server_id,enable_auto=enable_auto)

    def entry(self, server_id, tool):
        for entry in self.catalog.discover():
            if (entry.server_id,entry.original_name)==(server_id,tool):return entry
        raise ManagementError('Tool is blocked, unavailable or undiscovered')

    def trust(self, server_id, tool, *, resource=None, verified_read_only=False, confirm):
        entry=self.entry(server_id,tool)
        review={'purpose':'TRUST_EXACT_TOOL','server_id':server_id,'tool':tool,
                'resource':str(resource) if resource else None,'verified_read_only':verified_read_only,
                'catalog_fingerprint':entry.identity}
        if confirm(review) is not True:return None
        if self.entry(server_id,tool)!=entry:raise ManagementError('Tool changed during trust review')
        return self.policy.grant_trust(entry,resource=resource,verified_read_only=verified_read_only)

    def revoke_trust(self, trust_id):self.policy.revoke_trust(trust_id)

    def decision(self, server_id, tool, arguments):
        entry=self.entry(server_id,tool)
        self.catalog.validate(entry,arguments)
        return self.policy.evaluate(entry,arguments).to_dict()

    def prepare_tool_test(self, server_id, tool, arguments):
        """Build a fresh proposal without approval or execution (even in AUTO)."""
        with self._lock:
            if len(self._tests)>=128:raise ManagementError('Tool test limit; close manager to release records')
            entry=self.entry(server_id,tool)
            self.catalog.validate(entry,arguments)
            if self.details(server_id)['options']['root'] and server_id not in self.policy._contracts:
                raise ManagementError('Root confinement has not been verified by the trusted runtime adapter')
            try:executor=FakeServiceExecutor(self.service)
            except TypeError:raise ManagementError('Reviewed execution runtime required; connection-only runtime cannot call tools') from None
            core=ServiceApprovalCore(self.catalog,executor,policy=self.policy,
                call_seconds=self.details(server_id)['options']['timeout'])
            session=uuid4().hex;core.create_session(session);rid=core.start_run(session)
            proposal=core.issue(rid,'manual-test',entry,deepcopy(arguments))
            pid=proposal['proposal_id'];decision=core.policy_decision(rid,pid)
            tid=uuid4().hex
            self._tests[tid]={'core':core,'run':rid,'proposal':pid,'entry':entry,
                              'arguments':deepcopy(arguments),'submitted':False}
            return {'test_id':tid,'run_id':rid,'proposal':proposal,'decision':decision.to_dict()}

    async def run_tool_test(self, test_id, *, confirm):
        # Mark submitted before any prompt/await. No duplicate UI callback can
        # consume the same prepared test, even across local caller threads.
        with self._lock:
            test=self._tests[test_id]
            if test['submitted']:raise ManagementError('Tool test already submitted')
            test['submitted']=True
        core,rid,pid=test['core'],test['run'],test['proposal']
        decision=core.policy_decision(rid,pid)
        if decision.action=='BLOCK':
            core.apply_policy(rid,pid)
        else:
            review={'purpose':'EXECUTE_EXACT_TOOL_ONCE','server_id':test['entry'].server_id,
                    'tool':test['entry'].original_name,'arguments':deepcopy(test['arguments']),
                    'run_id':rid,'proposal':core.proposal(rid,pid),'decision':decision.to_dict()}
            if confirm(review) is not True:core.decide(rid,pid,False)
            else:
                decision=core.apply_policy(rid,pid)
                if decision.action=='ASK':core.decide(rid,pid,True)
                if decision.action!='BLOCK':
                    await core.execute(rid,'manual-test',test['entry'],test['arguments'])
                    core.finish(rid)
        return self.test_status(test_id)

    def test_status(self,test_id):
        test=self._tests[test_id];core=test['core']
        return {'run':core.status(test['run']),
                'operations':[core.operation(oid) for oid,r in core._operations.items() if r['run_id']==test['run']]}

    def cancel_test(self,test_id):
        test=self._tests[test_id];test['core'].cancel(test['run'])
        return self.test_status(test_id)

    def audit(self):return self.policy.audit_log()

    def stop_server(self,server_id):
        result=self.service.stop_server(server_id);self.policy.invalidate_server(server_id);return result

    def delete_server(self,server_id):
        result=self.service.delete_server(server_id);self.policy.forget_server(server_id)
        self._scopes.pop(server_id,None)
        return result

    def shutdown(self):
        try:
            for tid in tuple(self._tests):self.cancel_test(tid)
        finally:
            self.service.shutdown()
            if self._runtime is not None and self.service._runtime is None:self._runtime.shutdown()

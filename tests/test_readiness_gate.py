"""C-3B-3 gate: memory SDK, isolated fixture Root and exact scoped grants."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_poc.readiness_gate import (IsolationLayout, ReadOnlyScope, ReadinessDenied,
    PreparedStdioFactory, PreparedOwnerAdapter)
from agent_poc.owner_runtime import OwnerFakeRuntime
from agent_poc.service_execution import FakeServiceExecutor
from agent_poc.service_approval import ServiceApprovalCore
from agent_poc.approval_core import ApprovalError
from agent_poc.mcp_catalog import MCPCatalog
from mcp_manager.models import ServerConfig, ToolInfo


class ScopeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='local-ai-readiness-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name); self.root = self.base/'workspace'; self.root.mkdir()
        self.file = self.root/'test.txt'; self.file.write_text('Harmless fixture value=42', encoding='utf-8')
        self.scope = ReadOnlyScope('filesystem', 'filesystem', self.root)

    def test_only_two_filesystem_read_tools(self):
        self.scope.validate('read_file', {'path': str(self.file)})
        self.scope.validate('list_directory', {'path': str(self.root)})
        for tool in ('write_file', 'delete_file', 'execute', 'search_files', 'list_allowed_directories'):
            with self.subTest(tool=tool):
                with self.assertRaises(ReadinessDenied): self.scope.validate(tool, {'path': str(self.file)})

    def test_outside_root_existing_source_is_denied_without_read(self):
        with self.assertRaises(ReadinessDenied): self.scope.validate('read_file', {'path': str(Path(__file__).resolve())})

    def test_relative_traversal_remote_and_ads_denied(self):
        for path in ('test.txt', str(self.root/'..'/'test.txt'), r'\\remote\share\test.txt', str(self.file)+':stream'):
            with self.subTest(path=path):
                with self.assertRaises((ReadinessDenied, OSError)): self.scope.validate('read_file', {'path': path})

    def test_no_extra_or_coerced_arguments(self):
        for args in ({'path': str(self.file), 'head': 1}, {'path': 42}, {}):
            with self.subTest(args=args):
                with self.assertRaises(ReadinessDenied): self.scope.validate('read_file', args)

    def test_reparse_point_flag_fails_closed(self):
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == self.file: return SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', lstat):
            with self.assertRaises(ReadinessDenied): self.scope.validate('read_file', {'path': str(self.file)})

    def test_timezone_is_exact_and_time_tools_narrow(self):
        scope = ReadOnlyScope('time', 'time')
        scope.validate('get_current_time', {'timezone': 'Asia/Seoul'})
        for tool, args in (('convert_time', {}), ('get_current_time', {'timezone': 'UTC'}),
                           ('get_current_time', {}), ('get_current_time', {'timezone': 'Asia/Seoul', 'extra': True})):
            with self.assertRaises(ReadinessDenied): scope.validate(tool, args)

    def test_protected_project_paths_rejected(self):
        with self.assertRaises(ReadinessDenied): IsolationLayout(Path.cwd())

    def test_stdio_parameters_are_data_and_launch_is_disabled(self):
        config = ServerConfig('test', sys.executable, ('-m', 'not_executed'))
        factory = PreparedStdioFactory(); params = factory.parameters(config)
        self.assertEqual(params.command, config.executable)
        self.assertEqual(params.args, ['-m', 'not_executed'])
        with self.assertRaises(ReadinessDenied): factory(config, None)


class GateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='local-ai-c3b3-'); self.addCleanup(temp.cleanup)
        self.layout = IsolationLayout(Path(temp.name)); self.layout.root.mkdir()
        self.file = self.layout.root/'test.txt'; self.file.write_text('Fixture only', encoding='utf-8')
        self.runtime = OwnerFakeRuntime(prepared=True); self.addCleanup(self.runtime.shutdown)
        self.profile = ServerConfig('isolated', sys.executable)
        # Poison environment defaults: explicit layout/profiles must ignore them.
        with patch.dict(os.environ, {'LOCAL_AI_MCP_DB_PATH': str(Path(temp.name)/'must-not-use.sqlite3'),
                                     'LOCAL_AI_MCP_APPROVALS': str(Path(temp.name)/'must-not-read.json')}):
            self.service = self.layout.service(self.runtime, [self.profile])
            self.sid = self.service.create_server('isolated', 'Narrow memory server')['server_id']
        self.addCleanup(self.service.shutdown)
        schema = {'type': 'object', 'properties': {'path': {'type': 'string'}},
                  'required': ['path'], 'additionalProperties': False}
        self.runtime.tools = [ToolInfo(name, 'Fixture schema', schema, {'type': 'object'})
                              for name in ('read_file', 'list_directory', 'write_file')]
        self.rule = ReadOnlyScope(self.sid, 'filesystem', self.layout.root)
        self.runtime.manager._client_factory.bind(self.rule)
        self.service.start_server(self.sid)
        self.service.set_permission(self.sid, 'read_file', True)
        self.catalog = MCPCatalog(self.service); self.entry = self.catalog.discover()[0]
        self.executor = FakeServiceExecutor(self.service)
        self.now = 100.0
        self.core = ServiceApprovalCore(self.catalog, self.executor, clock=lambda: self.now)
        self.core.create_session('virtual'); self.rid = self.core.start_run('virtual')

    async def asyncTearDown(self):
        self.runtime.dispatch_gate.set(); self.runtime.finish_gate.set()
        await self.core.drain_fixtures()

    def proposal(self, allow=True, args=None):
        args = {'path': str(self.file)} if args is None else args
        p = self.core.issue(self.rid, 'scoped-call', self.entry, args)
        if allow: self.core.decide(self.rid, p['proposal_id'], True)
        return p

    async def call(self, args=None):
        return await self.core.execute(self.rid, 'scoped-call', self.entry,
                                       {'path': str(self.file)} if args is None else args)

    async def until(self, fn):
        for _ in range(500):
            if fn(): return
            await asyncio.sleep(.002)
        self.fail('Memory gate checkpoint missing')

    async def test_isolation_no_env_fallback(self):
        self.assertEqual(self.service.database_path, self.layout.base/'mcp.sqlite3')
        self.assertEqual(self.service.session_database_path, self.layout.base/'sessions.sqlite3')
        self.assertEqual(set(self.service.profiles()), {'isolated'})
        self.assertFalse((self.layout.base/'must-not-use.sqlite3').exists())
        with self.assertRaises(ReadinessDenied): self.layout.service(self.runtime, [])

    async def test_read_allow_then_single_scoped_send(self):
        self.proposal(); self.assertEqual(self.runtime.sends, 0)
        await self.call(); self.assertEqual(self.runtime.sends, 1)
        with self.assertRaises(ApprovalError): await self.call()

    async def test_deny_never_sends(self):
        p = self.proposal(False); self.core.decide(self.rid, p['proposal_id'], False)
        with self.assertRaises(ApprovalError): await self.call()
        self.assertEqual(self.runtime.sends, 0)

    async def test_directory_listing_allow_passes_same_service_boundary(self):
        self.service.set_permission(self.sid, 'list_directory', True)
        entry = next(entry for entry in self.catalog.discover() if entry.original_name == 'list_directory')
        args = {'path': str(self.layout.root)}
        p = self.core.issue(self.rid, 'directory-call', entry, args)
        self.core.decide(self.rid, p['proposal_id'], True)
        await self.core.execute(self.rid, 'directory-call', entry, args)
        self.assertEqual(self.runtime.sends, 1)

    async def test_time_seoul_only_through_prepared_owner(self):
        base = self.layout.base/'time-case'; base.mkdir()
        runtime = OwnerFakeRuntime(prepared=True); self.addCleanup(runtime.shutdown)
        runtime.tools = [ToolInfo('get_current_time', 'Fixed time fixture',
            {'type': 'object', 'properties': {'timezone': {'type': 'string'}},
             'required': ['timezone'], 'additionalProperties': False}, {'type': 'object'})]
        service = IsolationLayout(base).service(runtime, [self.profile]); self.addCleanup(service.shutdown)
        sid = service.create_server('isolated', 'Time fixture')['server_id']
        runtime.manager._client_factory.bind(ReadOnlyScope(sid, 'time'))
        service.start_server(sid); service.set_permission(sid, 'get_current_time', True)
        catalog = MCPCatalog(service); entry = catalog.discover()[0]
        core = ServiceApprovalCore(catalog, FakeServiceExecutor(service)); core.create_session('time')
        rid = core.start_run('time'); args = {'timezone': 'Asia/Seoul'}
        p = core.issue(rid, 'seoul', entry, args); core.decide(rid, p['proposal_id'], True)
        await core.execute(rid, 'seoul', entry, args); core.finish(rid)
        self.assertEqual(runtime.sends, 1)
        rid = core.start_run('time'); args = {'timezone': 'UTC'}
        p = core.issue(rid, 'not-seoul', entry, args); core.decide(rid, p['proposal_id'], True)
        with self.assertRaises(ApprovalError): await core.execute(rid, 'not-seoul', entry, args)
        await core.drain_fixtures(); self.assertEqual(runtime.sends, 1)

    async def test_outside_root_even_with_proposal_allow_never_sends(self):
        args = {'path': str(Path(__file__).resolve())}; self.proposal(args=args)
        with self.assertRaises(ApprovalError): await self.call(args)
        self.assertEqual(self.runtime.sends, 0)

    async def test_argument_change_before_execution_never_sends(self):
        self.proposal()
        with self.assertRaisesRegex(ApprovalError, 'approval_mismatch'):
            await self.call({'path': str(self.layout.root)})
        self.assertEqual(self.runtime.sends, 0)

    async def test_unapproved_and_write_tools_not_discovered(self):
        exposed = self.service.list_tools(self.sid)
        self.assertNotIn('write_file', {tool['name'] for tool in exposed})
        self.assertEqual({entry.original_name for entry in self.catalog.discover()}, {'read_file'})

    async def test_expiration_is_bound_to_run(self):
        p = self.proposal(); self.now = p['expires_at']
        with self.assertRaisesRegex(ApprovalError, 'approval_expired'): await self.call()
        self.assertEqual(self.runtime.sends, 0)

    async def test_cancel_at_final_prepared_gate(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set); self.core.cancel(self.rid)
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_permission_changed_at_final_prepared_gate(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        self.service.registry.set_permission(self.sid, 'read_file', '', False)
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_schema_changed_at_final_prepared_gate(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        with self.runtime.lock: self.runtime.tools[0] = replace(self.runtime.tools[0], description='new schema')
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_connection_changed_at_final_prepared_gate(self):
        self.proposal(); self.runtime.dispatch_gate.clear(); task = asyncio.create_task(self.call())
        await self.until(self.runtime.dispatch_waiting.is_set)
        worker = self.runtime.manager._workers[self.sid]
        with worker.lock: worker.connection_generation = 'new-source'
        self.runtime.dispatch_gate.set()
        with self.assertRaises(ApprovalError): await task
        self.assertEqual(self.runtime.sends, 0)

    async def test_timeout_late_result_never_auto_releases_run(self):
        self.proposal(); self.runtime.wait_seconds = .1; self.runtime.finish_gate.clear()
        with self.assertRaises(ApprovalError): await self.call()
        self.runtime.finish_gate.set()
        await self.until(lambda: next(iter(self.runtime.records.values()))['result'] is not None)
        self.assertEqual(self.core.status(self.rid)['state'], 'UNCERTAIN')
        self.assertEqual(self.runtime.sends, 1)

    async def test_sdk_header_does_not_retry(self):
        self.proposal(); self.runtime.mode = 'sdk_header'
        with self.assertRaises(ApprovalError): await self.call()
        self.assertEqual(self.runtime.sends, 1)

    async def test_real_client_entry_is_denied_before_context_enter(self):
        class MustNotEnter:
            async def __aenter__(self): raise AssertionError('Real client was entered')
        with self.assertRaises(ReadinessDenied):
            async with PreparedOwnerAdapter(MustNotEnter(), self.rule): pass

    async def test_official_stdio_candidate_is_lazy_and_entry_stays_denied(self):
        from mcp import Client
        factory = PreparedStdioFactory()
        config = ServerConfig(self.sid, sys.executable, ('-m', 'never_executed'))
        # A public SDK context-entry spy proves the deny happens before SDK
        # initialization/transport entry. No subprocess/network monkeypatch needed.
        with patch.object(Client, '__aenter__', side_effect=AssertionError('SDK must not enter')) as enter:
            candidate = factory.candidate(config, self.rule, lambda oid: None, None)
            with self.assertRaises(ReadinessDenied):
                async with candidate: pass
            enter.assert_not_called()

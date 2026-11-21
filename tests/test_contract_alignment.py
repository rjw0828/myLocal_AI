"""C-4C-2A: confined contracts + real owner/SDK boundary over memory transport."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_poc.readiness_gate import ReadOnlyScope, ReadinessDenied, IsolationLayout, aligned_read_only_scope
from agent_poc.live_transport import LaunchReview, ReviewedStdioRuntime, isolated_reviewed_service
from agent_poc.mcp_catalog import MCPCatalog
from agent_poc.service_approval import ServiceApprovalCore
from agent_poc.service_execution import FakeServiceExecutor
from agent_poc.approval_core import ApprovalError
from mcp_manager.models import ServerConfig, ToolInfo

TIME_SCHEMA = {'type':'object','properties':{'timezone':{'type':'string','default':'Asia/Seoul'}}}
PATH_SCHEMA = {'type':'object','properties':{'path':{'type':'string'}},'required':['path']}


class ContractTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory(prefix='local-ai-contract-');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'workspace';self.root.mkdir()
        self.file=self.root/'test.txt';self.file.write_text('fixture only',encoding='utf-8')
        self.config=ServerConfig('fs',sys.executable,(sys.executable,'--read-only','--confine-roots',str(self.root)))
        self.scope=aligned_read_only_scope(ReadOnlyScope('fs','filesystem',self.root),self.config)
        self.time=aligned_read_only_scope(ReadOnlyScope('time','time'),
            ServerConfig('time',sys.executable,('-m','mcp_server_time','--local-timezone=Asia/Seoul')))

    def test_relative_directory_and_file_preserve_arguments(self):
        for tool,args in [('list_directory',{'path':'.'}),('read_file',{'path':'test.txt'})]:
            original=deepcopy(args);self.scope.validate(tool,args);self.assertEqual(args,original)

    def test_absolute_read_regression(self):
        self.scope.validate('read_file',{'path':str(self.file)})

    def test_nested_relative_path(self):
        nested=self.root/'notes';nested.mkdir();(nested/'note.md').write_text('fixture',encoding='utf-8')
        self.scope.validate('read_file',{'path':r'notes\note.md'})

    def test_outside_and_traversal_denied(self):
        for path in (str(Path(__file__).resolve()),'../outside.txt',r'..\outside.txt',r'notes\..\test.txt'):
            with self.subTest(path=path),self.assertRaises(ReadinessDenied): self.scope.validate('read_file',{'path':path})

    def test_windows_special_paths_denied(self):
        for path in (r'\\server\share\file',r'\\?\C:\file',r'\\.\NUL',r'C:test.txt',
                     r'\test.txt','/test.txt','test.txt:stream','NUL','CON.txt',r'notes\COM1',
                     'test.txt.','test.txt ','~\\test.txt','test\x00.txt','te\nst.txt','*.txt'):
            with self.subTest(path=path),self.assertRaises(ReadinessDenied): self.scope.validate('read_file',{'path':path})

    def test_missing_file_is_not_invented(self):
        with self.assertRaises(FileNotFoundError): self.scope.validate('read_file',{'path':'missing.txt'})

    def test_reparse_file_is_denied(self):
        original=Path.lstat
        def lstat(path,*args,**kwargs):
            if path==self.file:return SimpleNamespace(st_mode=0o100644,st_file_attributes=0x400)
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',lstat),self.assertRaises(ReadinessDenied):
            self.scope.validate('read_file',{'path':'test.txt'})

    def test_reparse_root_is_rechecked(self):
        original=Path.lstat
        def lstat(path,*args,**kwargs):
            if path==self.root:return SimpleNamespace(st_mode=0o040755,st_file_attributes=0x400)
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',lstat),self.assertRaises(ReadinessDenied):
            self.scope.validate('list_directory',{'path':'.'})

    def test_multiple_roots_or_unconfined_configuration_cannot_align(self):
        for args in ((sys.executable,str(self.root)),self.config.args+(str(self.root.parent),),
                     (sys.executable,'--read-only','--confine-roots',str(self.root.parent))):
            config=ServerConfig('bad',sys.executable,args)
            with self.assertRaises(ReadinessDenied):aligned_read_only_scope(self.scope,config)

    def test_unverified_scope_keeps_deny_default(self):
        with self.assertRaises(ReadinessDenied):ReadOnlyScope('fs','filesystem',self.root).validate('read_file',{'path':'test.txt'})
        with self.assertRaises(ReadinessDenied):ReadOnlyScope('time','time').validate('get_current_time',{},TIME_SCHEMA)

    def test_time_empty_and_explicit_are_both_allowed_without_mutation(self):
        for args in ({},{'timezone':'Asia/Seoul'}):
            original=deepcopy(args);self.time.validate('get_current_time',args,TIME_SCHEMA);self.assertEqual(args,original)

    def test_time_default_must_be_current_optional_schema(self):
        for schema in (None,{'type':'object'},dict(TIME_SCHEMA,required=['timezone']),
                       {'type':'object','properties':{'timezone':{'type':'string','default':'UTC'}}}):
            with self.subTest(schema=schema),self.assertRaises(ReadinessDenied):
                self.time.validate('get_current_time',{},schema)

    def test_unreviewed_time_profile_cannot_align(self):
        for args in (('-m','mcp_server_time'),('-m','mcp_server_time','--local-timezone=UTC')):
            with self.assertRaises(ReadinessDenied):
                aligned_read_only_scope(ReadOnlyScope('time','time'),ServerConfig('time',sys.executable,args))

    def test_foreign_timezone_and_extra_args_remain_denied(self):
        for args in ({'timezone':'UTC'},{'timezone':None},{'timezone':''},{'extra':True}):
            with self.subTest(args=args),self.assertRaises(ReadinessDenied): self.time.validate('get_current_time',args,TIME_SCHEMA)


class ContractDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=TemporaryDirectory(prefix='local-ai-contract-sdk-');self.addCleanup(temp.cleanup)
        self.layout=IsolationLayout(Path(temp.name));self.layout.root.mkdir()
        self.file=self.layout.root/'test.txt';self.file.write_text('fixture',encoding='utf-8')
        self.configs=(ServerConfig('time',sys.executable,('-m','mcp_server_time','--local-timezone=Asia/Seoul')),
            ServerConfig('fs',sys.executable,(sys.executable,'--read-only','--confine-roots',str(self.layout.root))))
        self.runtime=ReviewedStdioRuntime(LaunchReview(self.configs,base=self.layout.base))
        self.addCleanup(self.runtime.shutdown)
        self.runtime.tools=[ToolInfo('get_current_time','Fixture time',deepcopy(TIME_SCHEMA),{'type':'object'}),
            ToolInfo('read_file','Fixture read',deepcopy(PATH_SCHEMA),{'type':'object'}),
            ToolInfo('list_directory','Fixture list',deepcopy(PATH_SCHEMA),{'type':'object'})]
        self.service=isolated_reviewed_service(self.layout,self.runtime,self.configs);self.addCleanup(self.service.shutdown)
        self.ids={}
        for config,kind in zip(self.configs,('time','filesystem')):
            sid=self.service.create_server(config.server_id,kind)['server_id'];self.ids[kind]=sid
            scope=aligned_read_only_scope(ReadOnlyScope(sid,kind,self.layout.root if kind=='filesystem' else None),config)
            self.runtime.manager._client_factory.bind(scope);self.service.start_server(sid)
            for name in scope.tools:self.service.set_permission(sid,name,True)
        self.catalog=MCPCatalog(self.service)
        self.core=ServiceApprovalCore(self.catalog,FakeServiceExecutor(self.service))
        self.core.create_session('virtual');self.rid=self.core.start_run('virtual')

    async def asyncTearDown(self):
        self.runtime.dispatch_gate.set();self.runtime.finish_gate.set();await self.core.drain_fixtures()

    def issue(self,name,args,allow=True):
        entry=next(e for e in self.catalog.discover() if e.original_name==name)
        proposal=self.core.issue(self.rid,'contract-call',entry,args)
        if allow:self.core.decide(self.rid,proposal['proposal_id'],True)
        return entry,proposal

    async def dispatch(self,name,args):
        entry,proposal=self.issue(name,args)
        await self.core.execute(self.rid,'contract-call',entry,args)
        operation=next(iter(self.core._operations))
        self.assertEqual(self.runtime.attempts,1)
        self.assertEqual(self.runtime.arguments_hash[operation],proposal['arguments_fingerprint'])
        self.assertTrue(self.core.operation(operation)['service_token_consumed'])
        return entry,proposal

    async def test_relative_file_exact_approved_arguments_once(self):
        args={'path':'test.txt'};entry,p=await self.dispatch('read_file',args)
        with self.assertRaises(ApprovalError): await self.core.execute(self.rid,'contract-call',entry,args)
        self.assertEqual(self.runtime.attempts,1)

    async def test_relative_directory_once(self): await self.dispatch('list_directory',{'path':'.'})
    async def test_absolute_file_once(self): await self.dispatch('read_file',{'path':str(self.file)})
    async def test_empty_time_exact_fingerprint_once(self): await self.dispatch('get_current_time',{})
    async def test_explicit_time_once(self): await self.dispatch('get_current_time',{'timezone':'Asia/Seoul'})

    async def test_no_approval_no_send(self):
        args={'path':'test.txt'};entry,p=self.issue('read_file',args,False)
        with self.assertRaises(ApprovalError): await self.core.execute(self.rid,'contract-call',entry,args)
        self.assertEqual(self.runtime.attempts,0)

    async def test_relative_and_absolute_cannot_share_approval(self):
        entry,p=self.issue('read_file',{'path':'test.txt'})
        with self.assertRaises(ApprovalError):
            await self.core.execute(self.rid,'contract-call',entry,{'path':str(self.file)})
        self.assertEqual(self.runtime.attempts,0)

    async def test_empty_and_explicit_time_cannot_share_approval(self):
        entry,p=self.issue('get_current_time',{})
        with self.assertRaises(ApprovalError):
            await self.core.execute(self.rid,'contract-call',entry,{'timezone':'Asia/Seoul'})
        self.assertEqual(self.runtime.attempts,0)

    async def test_schema_default_change_blocks_before_send(self):
        entry,p=self.issue('get_current_time',{})
        tool=self.runtime.tools[0]
        self.runtime.tools[0]=ToolInfo(tool.name,tool.description,
            {'type':'object','properties':{'timezone':{'type':'string','default':'UTC'}}},{'type':'object'})
        with self.assertRaises(ApprovalError): await self.core.execute(self.rid,'contract-call',entry,{})
        self.assertEqual(self.runtime.attempts,0)

    async def test_outside_root_never_sends(self):
        args={'path':str(Path(__file__).resolve())};entry,p=self.issue('read_file',args)
        with self.assertRaises(ApprovalError):await self.core.execute(self.rid,'contract-call',entry,args)
        self.assertEqual(self.runtime.attempts,0)

    async def test_permission_revoked_after_approval_never_sends(self):
        args={'path':'test.txt'};entry,p=self.issue('read_file',args)
        self.service.set_permission(entry.server_id,'read_file',False)
        with self.assertRaises(ApprovalError):await self.core.execute(self.rid,'contract-call',entry,args)
        self.assertEqual(self.runtime.attempts,0)

    async def test_reparse_swap_at_owner_checkpoint_never_sends(self):
        args={'path':'test.txt'};entry,p=self.issue('read_file',args)
        self.runtime.dispatch_gate.clear()
        task=asyncio.create_task(self.core.execute(self.rid,'contract-call',entry,args))
        for _ in range(500):
            if self.runtime.dispatch_waiting.is_set():break
            await asyncio.sleep(.002)
        else:self.fail('Owner checkpoint missing')
        original=Path.lstat
        def lstat(path,*a,**kw):
            if path==self.file:return SimpleNamespace(st_mode=0o100644,st_file_attributes=0x400)
            return original(path,*a,**kw)
        with patch.object(Path,'lstat',lstat):
            self.runtime.dispatch_gate.set()
            with self.assertRaises(ApprovalError):await task
        self.assertEqual(self.runtime.attempts,0)

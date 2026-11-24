"""Actual owner queue, synthetic SDK client and package; zero subprocesses."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp.types import ListToolsResult,Tool,CallToolResult,TextContent
from mcp_manager.attestation import filesystem_artifact,verified_filesystem
from mcp_manager.models import ServerConfig
from mcp_manager.registry import fingerprint,config_data
from mcp_manager.manager import MCPManager
from agent_poc.mcp_management import MCPManagement
from agent_poc.managed_catalog import ManagedCatalog
from agent_poc.service_execution import FakeServiceExecutor
from agent_poc.service_approval import ServiceApprovalCore

class SyntheticClient:
    calls=0
    lose=False
    protocol_version='2026-07-28'
    list_roots_callback=None
    def __init__(self,config):self.config=config;self.session=self
    async def __aenter__(self):self.owner=asyncio.current_task();return self
    async def __aexit__(self,*args):assert self.owner is asyncio.current_task()
    async def list_tools(self,**kwargs):
        assert self.owner is asyncio.current_task()
        if self.config.args==('--generic',):
            return ListToolsResult(tools=[Tool(name='convert_temperature',description='Generic fixture',input_schema={
                'type':'object','properties':{'value':{'type':'number'}},'required':['value']}),
                Tool(name='echo_text',input_schema={'type':'object','properties':{}})])
        return ListToolsResult(tools=[Tool(name='read_file',description='fixture read',input_schema={
            'type':'object','properties':{'path':{'type':'string'}},'required':['path']})])
    async def call_tool(self,name,arguments,**kwargs):
        assert self.owner is asyncio.current_task()
        type(self).calls+=1
        if type(self).lose:raise ConnectionError('fixture lost response')
        return CallToolResult(content=[TextContent(type='text',text='fixture text')],is_error=False)

class AttestationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='local-ai-c5d-');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'workspace';self.root.mkdir()
        (self.root/'test.txt').write_text('fixture text',encoding='utf-8')
        self.node=self.base/'node.exe';self.node.write_bytes(b'synthetic never executed')
        self.package=self.base/'package';(self.package/'dist').mkdir(parents=True)
        (self.package/'package.json').write_text(json.dumps({'name':'@modelcontextprotocol/server-filesystem','version':'2.2.0'}))
        self.script=self.package/'dist/index.js';self.script.write_text('// synthetic never executed')
        self.config=ServerConfig('fixture',str(self.node),(str(self.script),'--read-only','--confine-roots',str(self.root)))
        proof=filesystem_artifact(self.config)
        p=patch('mcp_manager.attestation.REVIEWED_FILESYSTEM',('2.2.0',proof['digest']));p.start();self.addCleanup(p.stop)
        factory=lambda config,errlog:SyntheticClient(config)
        p=patch('mcp_manager.async_host.sdk_client',factory);p.start();self.addCleanup(p.stop)
        p=patch('mcp.Client',SyntheticClient);p.start();self.addCleanup(p.stop)
        SyntheticClient.calls=0;SyntheticClient.lose=False
        self.runtime=MCPManager(_client_factory=factory)
        self.manager=MCPManagement(self.base/'management',runtime_factory=lambda:self.runtime)
        self.addCleanup(self.manager.shutdown);self.addCleanup(self.runtime.shutdown)
        self.sid=self.manager.add_server('registered fs',str(self.node),list(self.config.args),confirm=lambda _:True)['server_id']
        self.manager.test_connection(self.sid,confirm=lambda _:True)
        self.manager.set_enabled(self.sid,True)
        self.catalog=ManagedCatalog(self.manager)

    def test_connection_attests_root_without_granting_trust(self):
        entry=self.catalog.discover()[0]
        self.assertEqual(self.manager.details(self.sid)['options']['root'],str(self.root))
        self.assertEqual(self.manager.details(self.sid)['trusts'],[])
        self.assertIn('absolute_root',self.catalog.model_context([entry]))
        self.catalog.validate(entry,{'path':'test.txt'})
        with self.assertRaises(Exception):self.catalog.validate(entry,{'path':str(self.base/'outside')})
        for mode in ('MANUAL','ALLOW_ALL'):
            permission={'mode':mode,'manual_generation':0}
            self.assertEqual(self.manager.policy.evaluate(entry,{'path':str(self.base/'outside')},permission=permission).action,'BLOCK')
            self.assertEqual(self.manager.policy.evaluate(entry,{'path':'test.txt'},permission=permission).action,
                             'ASK' if mode=='MANUAL' else 'AUTO_APPROVE')
        self.assertEqual(SyntheticClient.calls,0)

    async def test_registered_owner_single_dispatch_and_duplicate_prevention(self):
        entry=self.catalog.discover()[0];core=ServiceApprovalCore(self.catalog,FakeServiceExecutor(self.manager.service),policy=self.manager.policy,
            permission={'mode':'ALLOW_ALL','manual_generation':0})
        core.create_session('fixture');rid=core.start_run('fixture');p=core.issue(rid,'call',entry,{'path':'test.txt'})
        self.assertEqual(core.apply_policy(rid,p['proposal_id']).action,'AUTO_APPROVE')
        result=await core.execute(rid,'call',entry,{'path':'test.txt'})
        self.assertEqual(result['content'][0]['text'],'fixture text')
        with self.assertRaises(Exception):await core.execute(rid,'call',entry,{'path':'test.txt'})
        await core.drain_fixtures()
        self.assertEqual(SyntheticClient.calls,1)

    def test_artifact_and_execution_config_changes_fail_closed(self):
        entry=self.catalog.discover()[0]
        self.script.write_text('// tampered')
        with self.assertRaises(Exception):self.catalog.model_context([entry])
        self.assertEqual(SyntheticClient.calls,0)

    def test_bad_protocol_callback_or_profile_never_attests(self):
        context=self.manager.service.execution_context(self.sid)
        config=ServerConfig(self.sid,**context['config'])
        for change in ({'roots_disabled':False},{'protocol_version':'unknown'},{'config_fingerprint':'changed'}):
            with self.assertRaises(ValueError):verified_filesystem(config,{**context['connection_info'],**change})
        with self.assertRaises(ValueError):verified_filesystem(replace(config,args=(str(self.script),'--read-only',str(self.base))),context['connection_info'])

    def test_legacy_absolute_root_contract_does_not_enable_relative_paths(self):
        config=replace(self.config,args=(str(self.script),'--read-only',str(self.root)))
        proof=filesystem_artifact(config)
        self.assertFalse(proof['allow_relative'])
        self.assertEqual(proof['root'],str(self.root))

    async def test_new_generic_server_needs_no_agent_code_or_automatic_trust(self):
        sid=self.manager.add_server('new generic',str(self.node),['--generic'],confirm=lambda _:True)['server_id']
        self.manager.test_connection(sid,confirm=lambda _:True)
        self.manager.set_enabled(sid,True)
        entries=[e for e in self.catalog.discover() if e.server_id==sid]
        self.assertEqual({e.original_name for e in entries}, {'convert_temperature','echo_text'})
        self.manager.service.registry.set_permission(sid,'convert_temperature','old-schema',False)
        entry=next(e for e in entries if e.original_name=='convert_temperature')
        self.catalog.validate(entry,{'value':25})
        self.assertEqual(self.manager.decision(sid,'convert_temperature',{'value':25})['action'],'ASK')
        core=ServiceApprovalCore(self.catalog,FakeServiceExecutor(self.manager.service),policy=self.manager.policy)
        core.create_session('disabled');rid=core.start_run('disabled')
        proposal=core.issue(rid,'generic-call',entry,{'value':25})
        core.decide(rid,proposal['proposal_id'],True)
        # Service API deliberately bypasses policy invalidation: final Enabled
        # admission itself must reject the previously approved call.
        self.manager.service.set_enabled(sid,False)
        with self.assertRaises(Exception):await core.execute(rid,'generic-call',entry,{'value':25})
        self.assertFalse(any(e.server_id==sid for e in self.catalog.discover()))
        self.manager.service.set_enabled(sid,True)
        with self.assertRaises(Exception):self.catalog.validate(entry,{'value':25})
        self.manager.stop_server(sid)
        self.assertFalse(any(e.server_id==sid for e in self.catalog.discover()))
        self.assertEqual(SyntheticClient.calls,0)

    def test_restart_requires_fresh_trust_and_root_replacement_is_rejected(self):
        permission={'mode':'ALLOW_ALL','manual_generation':0}
        before=self.catalog.discover()[0]
        self.manager.stop_server(self.sid);self.manager.test_connection(self.sid,confirm=lambda _:True)
        after=self.catalog.discover()[0]
        self.assertNotEqual(before.identity,after.identity)
        self.assertEqual(self.manager.policy.evaluate(after,{'path':'test.txt'},permission=permission).action,'AUTO_APPROVE')
        self.root.rename(self.base/'old-root');self.root.mkdir()
        with self.assertRaises(Exception):self.catalog.model_context([after])
        self.assertEqual(SyntheticClient.calls,0)

    async def test_tamper_after_allow_before_dispatch_sends_zero(self):
        entry=self.catalog.discover()[0]
        core=ServiceApprovalCore(self.catalog,FakeServiceExecutor(self.manager.service),policy=self.manager.policy)
        core.create_session('tamper');rid=core.start_run('tamper')
        p=core.issue(rid,'c',entry,{'path':'test.txt'});core.decide(rid,p['proposal_id'],True)
        self.script.write_text('// changed after Allow')
        with self.assertRaises(Exception):await core.execute(rid,'c',entry,{'path':'test.txt'})
        await core.drain_fixtures()
        self.assertEqual(SyntheticClient.calls,0)

    async def test_lost_response_keeps_uncertain_without_retry(self):
        entry=self.catalog.discover()[0];SyntheticClient.lose=True
        core=ServiceApprovalCore(self.catalog,FakeServiceExecutor(self.manager.service),policy=self.manager.policy)
        core.create_session('lost');rid=core.start_run('lost');p=core.issue(rid,'c',entry,{'path':'test.txt'});core.decide(rid,p['proposal_id'],True)
        with self.assertRaises(Exception):await core.execute(rid,'c',entry,{'path':'test.txt'})
        await core.drain_fixtures()
        self.assertEqual(core.status(rid)['state'],'UNCERTAIN')
        self.assertEqual(SyntheticClient.calls,1)

if __name__=='__main__':unittest.main()

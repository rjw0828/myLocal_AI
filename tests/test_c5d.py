"""Dynamic Agent catalogs and filesystem contracts; memory SDK, no MCP process."""
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse,TextPart,ToolCallPart
import test_agent_web as web_fixture
from agent_poc.managed_catalog import ManagedCatalog
from agent_poc.readiness_gate import ReadOnlyScope,aligned_read_only_scope
from mcp_manager.models import ToolInfo

class DynamicTests(unittest.TestCase):
    setUp=web_fixture.AgentWebTests.setUp
    create=web_fixture.AgentWebTests.create
    wait=web_fixture.AgentWebTests.wait
    decide=web_fixture.AgentWebTests.decide
    history=web_fixture.AgentWebTests.history

    def filesystem(self):
        self.root=self.base/'workspace';self.root.mkdir();(self.root/'test.txt').write_text('fixture',encoding='utf-8')
        self.runtime.tools.append(ToolInfo('read_file','Read actual text',{'type':'object','properties':{
            'path':{'type':'string'}},'required':['path']}))
        def binder(runtime,config,options):
            scope=aligned_read_only_scope(ReadOnlyScope(config.server_id,'filesystem',Path(options['root'])),config)
            runtime.manager._client_factory.bind(scope);return scope
        self.manager._binder=binder
        sid=self.manager.add_server('Filesystem',sys.executable,[sys.executable,'--read-only','--confine-roots',str(self.root)],
            root=self.root,confirm=lambda _:True)['server_id']
        self.manager.test_connection(sid,confirm=lambda _:True)
        self.manager.set_enabled(sid,True)
        return sid

    def test_catalog_add_stop_block_restart_and_delete(self):
        catalog=ManagedCatalog(self.manager)
        self.assertEqual(len(catalog.discover()),1)
        sid=self.filesystem();entries=catalog.discover()
        entry=next(e for e in entries if e.server_id==sid and e.original_name=='read_file')
        self.assertEqual(len(entries),2)
        self.assertIn(str(self.root).replace('\\','\\\\'),catalog.model_context(entries))
        catalog.validate(entry,{'path':'test.txt'})
        with self.assertRaises(Exception):catalog.validate(entry,{'path':'../test.txt'})
        self.manager.set_enabled(sid,False)
        self.assertFalse(any(e.server_id==sid for e in catalog.discover()))
        with self.assertRaises(Exception):catalog.validate(entry,{'path':'test.txt'})
        self.manager.set_enabled(sid,True)
        fresh=next(e for e in catalog.discover() if e.server_id==sid and e.original_name=='read_file')
        self.assertNotEqual(fresh.identity,entry.identity)
        self.manager.stop_server(sid)
        restarted=next(e for e in catalog.discover() if e.server_id==sid and e.original_name=='read_file')
        self.assertNotEqual(fresh.identity,restarted.identity)
        self.manager.delete_server(sid)
        self.assertFalse(any(e.server_id==sid for e in catalog.discover()))
        self.assertEqual(self.runtime.sends,0)

    def test_root_and_schema_changes_reject_pinned_model_context(self):
        self.filesystem();catalog=ManagedCatalog(self.manager);entries=catalog.discover()
        self.runtime.tools[-1]=replace(self.runtime.tools[-1],description='Changed schema identity')
        with self.assertRaises(Exception):catalog.model_context(entries)
        self.runtime.tools[-1]=replace(self.runtime.tools[-1],description='Read actual text')
        other=self.base/'other';other.mkdir()
        self.manager.policy.set_server_options(entries[-1].server_id,root=other)
        with self.assertRaises(Exception):catalog.model_context(entries)
        self.assertEqual(self.runtime.sends,0)

    def test_new_run_sees_filesystem_context_and_history_stays_clean(self):
        sid=self.filesystem();self.backend.catalog=ManagedCatalog(self.manager)
        seen=[]
        async def model(messages,info):
            seen.extend(messages)
            if any(getattr(p,'part_kind','')=='tool-return' for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart('fixture answer')])
            name=next(t.name for t in info.function_tools if 'read_file' in t.name)
            return ModelResponse(parts=[ToolCallPart(name,{'path':'test.txt'},tool_call_id='file')])
        @asynccontextmanager
        async def models(initial):yield FunctionModel(model),{}
        self.backend.model_factory=models
        jid=self.create('read the fixture');job=self.wait(jid)
        self.assertEqual(job['pending']['server_id'],sid)
        self.assertEqual(job['pending']['scope']['path'],str(self.root))
        self.assertIn('absolute_root',str(seen))
        self.assertNotIn('absolute_root',str(self.history()))
        self.assertEqual(self.decide_with_scope(jid,job['pending']).status_code,202)
        self.assertEqual(self.wait(jid,('COMPLETED','FAILED'))['state'],'COMPLETED')
        self.assertEqual(self.runtime.sends,1)
        self.assertEqual([m['content'] for m in self.history()['messages']],['read the fixture','fixture answer'])

    def decide_with_scope(self,jid,p):
        return self.client.post('/tool-chat/jobs/'+jid+'/approval',headers={'X-Local-AI-Control':'web-console'},json={
            'proposal_id':p['proposal_id'],'browser_generation':1,'allow':True,'scope':p['scope']})

    def test_unverified_filesystem_is_never_exposed(self):
        sid=self.filesystem()
        self.manager.policy.bind_contract(sid,None)
        catalog=ManagedCatalog(self.manager)
        # A time-only contract cannot attest a filesystem tool.
        self.assertFalse(any(e.original_name=='read_file' for e in catalog.discover()))

    def test_catalog_change_during_model_response_cannot_issue_old_proposal(self):
        sid=self.filesystem();self.backend.catalog=ManagedCatalog(self.manager)
        async def model(messages,info):
            name=next(t.name for t in info.function_tools if 'read_file' in t.name)
            self.manager.set_enabled(sid,False)
            return ModelResponse(parts=[ToolCallPart(name,{'path':'test.txt'},tool_call_id='stale')])
        @asynccontextmanager
        async def models(initial):yield FunctionModel(model),{}
        self.backend.model_factory=models
        done=self.wait(self.create('read fixture'),('FAILED',))
        self.assertIsNone(done['pending']);self.assertEqual(done['operations'],[])
        self.assertEqual(self.runtime.sends,0);self.assertEqual(self.history()['messages'],[])

if __name__=='__main__':unittest.main()

"""Scoped tests only; no operating app, MCP, files or real model inference."""
import asyncio
import json
import subprocess
import sys
import unittest

from agent_poc.fixtures import ROOT, FILE, approved_call, toolset
from agent_poc.runtime import Profile
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UserError, UsageLimitExceeded, RunTimedOut, ModelHTTPError, ModelTimeoutError

def time_model(messages, info):
    if any(getattr(p,'part_kind','')=='tool-return' or getattr(p,'part_kind','')=='retry-prompt'
           for m in messages for p in m.parts):
        return ModelResponse(parts=[TextPart('fixture result received')])
    return ModelResponse(parts=[ToolCallPart('get_current_time',{'timezone':'Asia/Seoul'},tool_call_id='clock-1')])

class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    def make(self, fn=time_model, extension=False):
        self.ledger=[]
        return Agent(FunctionModel(fn),output_type=[str,DeferredToolRequests],toolsets=[toolset(self.ledger,extension=extension)],retries=0)

    async def test_wait_does_not_execute(self):
        r=await self.make().run('clock')
        self.assertIsInstance(r.output,DeferredToolRequests)
        self.assertEqual(self.ledger,[])

    async def test_approval_resumes_framework(self):
        agent=self.make();r=await agent.run('clock')
        r=await agent.run(conversation=r.conversation,deferred_tool_results=DeferredToolResults(approvals={'clock-1':True}))
        self.assertEqual(len(self.ledger),1)
        self.assertEqual(r.output,'fixture result received')

    async def test_denial_does_not_execute(self):
        agent=self.make();r=await agent.run('clock')
        r=await agent.run(conversation=r.conversation,deferred_tool_results=DeferredToolResults(approvals={'clock-1':ToolDenied('denied')}))
        self.assertEqual(self.ledger,[])
        self.assertIsInstance(r.output,str)

    async def test_wrong_call_id_rejected(self):
        agent=self.make();r=await agent.run('clock')
        with self.assertRaises(UserError):
            await agent.run(conversation=r.conversation,deferred_tool_results=DeferredToolResults(approvals={'other':True}))
        self.assertEqual(self.ledger,[])

    async def test_request_limit(self):
        agent=self.make(lambda m,i:ModelResponse(parts=[TextPart('done')]))
        with self.assertRaises(UsageLimitExceeded):await agent.run('test',usage_limits=UsageLimits(request_limit=0))

    async def test_timeout(self):
        async def slow(m,i):
            await asyncio.sleep(.1)
            return ModelResponse(parts=[TextPart('late')])
        with self.assertRaises(RunTimedOut):await self.make(slow).run('test',timeout=.01)
        self.assertEqual(self.ledger,[])

    async def test_extension_schema(self):
        names=[]
        def inspect_tools(m,info):
            names.extend(t.name for t in info.function_tools)
            return ModelResponse(parts=[TextPart('done')])
        await self.make(inspect_tools,extension=True).run('inspect')
        self.assertEqual(set(names),{'get_current_time','list_directory','read_file','convert_temperature'})

    async def test_resume_keeps_cumulative_request_limit(self):
        agent=self.make();limits=UsageLimits(request_limit=1)
        r=await agent.run('clock',usage_limits=limits)
        with self.assertRaises(UsageLimitExceeded):
            await agent.run(conversation=r.conversation,usage_limits=limits,
                deferred_tool_results=DeferredToolResults(approvals={'clock-1':True}))
        self.assertEqual(r.conversation.usage.requests,1)

class PolicyTests(unittest.TestCase):
    def test_fixture_paths(self):
        self.assertTrue(approved_call('read_file',{'path':FILE}))
        self.assertTrue(approved_call('list_directory',{'path':ROOT}))
        self.assertFalse(approved_call('read_file',{'path':'test.txt'}))
        self.assertFalse(approved_call('read_file',{'path':ROOT+r'\other.txt'}))

    def test_unknown_and_extra_fields(self):
        self.assertFalse(approved_call('shell',{}))
        self.assertFalse(approved_call('get_current_time',{'timezone':'Asia/Seoul','command':'x'}))

    def test_temperature_domain(self):
        self.assertTrue(approved_call('convert_temperature',{'celsius':25}))
        self.assertFalse(approved_call('convert_temperature',{'celsius':True}))

    def test_model_switch_is_configuration(self):
        self.assertEqual(Profile('qwen3.5:9b').settings(1),Profile('gemma4:12b').settings(1))

    def test_outbound_guard(self):
        code='from agent_poc.offline import install,VIOLATIONS;install();import socket\ntry: socket.create_connection(("1.1.1.1",443),timeout=.1)\nexcept PermissionError: pass\ntry: socket.socket().connect(("1.1.1.1",443))\nexcept PermissionError: pass\nassert len(VIOLATIONS)==2'
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, handler):
        import httpx2
        from openai import AsyncOpenAI
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler),trust_env=False) as http:
            client=AsyncOpenAI(base_url='http://127.0.0.1:11434/v1',api_key='local',max_retries=0,http_client=http)
            return await Agent(OllamaModel('gemma4:12b',provider=OllamaProvider(openai_client=client)),retries=0).run('test')

    async def test_http_error_is_not_retried(self):
        import httpx2
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx2.Response(429,json={'error':{'message':'busy','type':'rate_limit_error'}})
        with self.assertRaises(ModelHTTPError):await self.invoke(handler)
        self.assertEqual(len(requests),1)

    async def test_http_timeout_is_not_retried(self):
        import httpx2
        requests=[]
        def handler(request):
            requests.append(request)
            raise httpx2.ReadTimeout('fixture timeout',request=request)
        with self.assertRaises(ModelTimeoutError):await self.invoke(handler)
        self.assertEqual(len(requests),1)

if __name__=='__main__':unittest.main()

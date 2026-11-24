"""Replay stored C-4B first calls over Fake SDK. No LLM or real MCP process.

Decisions here are simulated test inputs, never local-user approval evidence.
Original arguments/schemas are retained; only source aliases map to fake servers.
"""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel
from mcp_manager.models import ServerConfig, ToolInfo
from .approved_bridge import DeferredFixtureRun
from .c4b_selection import OUT
from .c4_r1 import ROOT
from .mcp_catalog import MCPCatalog
from .owner_runtime import OwnerFakeRuntime
from .readiness_gate import IsolationLayout, ReadOnlyScope, aligned_read_only_scope
from .service_approval import ServiceApprovalCore
from .service_execution import FakeServiceExecutor


async def run():
    target=OUT/'contract-c4c2a-replay.json'
    if target.exists():raise RuntimeError('Preserve existing replay results; refusing overwrite')
    rows=[json.loads(line) for line in (OUT/'selection.jsonl').read_text(encoding='utf-8').splitlines()]
    original={e['alias']:e for e in json.loads((OUT/'selection-catalog.json').read_text(encoding='utf-8'))}
    results=[]
    with TemporaryDirectory(prefix='local-ai-contract-replay-') as folder:
        layout=IsolationLayout(Path(folder));runtime=OwnerFakeRuntime(prepared=True)
        profiles=(ServerConfig('time',sys.executable,('-m','mcp_server_time','--local-timezone=Asia/Seoul')),
            ServerConfig('fs',sys.executable,(sys.executable,'--read-only','--confine-roots',str(ROOT))))
        service=None
        try:
            runtime.tools=[ToolInfo(e['original'],e['descriptor']['description'],e['descriptor']['input_schema'],{'type':'object'})
                           for e in original.values()]
            service=layout.service(runtime,profiles)
            for config,kind in zip(profiles,('time','filesystem')):
                sid=service.create_server(config.server_id,kind)['server_id']
                scope=aligned_read_only_scope(ReadOnlyScope(sid,kind,ROOT if kind=='filesystem' else None),config)
                runtime.manager._client_factory.bind(scope);service.start_server(sid)
                for name in scope.tools:service.set_permission(sid,name,True)
            catalog=MCPCatalog(service);mapping={e.original_name:e for e in catalog.discover()}
            for index,row in enumerate(rows):
                calls=row['responses'][0]['body']['choices'][0]['message'].get('tool_calls') or []
                def model(messages,info):
                    if not calls or any(getattr(p,'part_kind','')=='tool-return' for m in messages for p in m.parts):
                        return ModelResponse(parts=[TextPart('Synthetic replay conclusion; not a model answer')])
                    return ModelResponse(parts=[ToolCallPart(mapping[original[c['function']['name']]['original']].alias,
                        json.loads(c['function']['arguments']),tool_call_id=c['id']) for c in calls])
                core=ServiceApprovalCore(catalog,FakeServiceExecutor(service));session='replay-'+str(index)
                core.create_session(session);turn=DeferredFixtureRun(core,session,FunctionModel(model))
                before=runtime.sends
                await turn.start(row['question'])
                if runtime.sends!=before:raise AssertionError('Execution before simulated approval')
                for proposal in turn.proposals:turn.decide(proposal['proposal_id'],True)
                if calls:await turn.resume()
                operations=[core.operation(oid) for oid in core._operations]
                results.append({'source_model':row['model'],'question_id':row['question_id'],'repeat':row['repeat'],
                    'original_calls':calls,'simulated_decisions':len(calls),'run':core.status(turn.run_id),
                    'fake_sends':runtime.sends-before,'operations':operations,'actual_mcp_calls':0,'new_model_inference':False})
        finally:
            if service is not None:service.shutdown()
            else:runtime.shutdown()
    target.write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    for model in sorted({r['source_model'] for r in results}):
        rr=[r for r in results if r['source_model']==model]
        print(model,'rows',len(rr),'fake sends',sum(r['fake_sends'] for r in rr),
              'completed',sum(r['run']['state']=='COMPLETED' for r in rr),'real MCP 0; fresh inference 0')


if __name__=='__main__':asyncio.run(run())

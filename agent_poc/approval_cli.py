"""Fake-only local approval policy console. Never starts MCP or runs an LLM.

python -m agent_poc.approval_cli --policy <absolute experimental .sqlite3 path>
"""
import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from mcp_manager.models import ServerConfig, ToolInfo
from .approval_policy import ApprovalPolicy
from .approved_bridge import DeferredFixtureRun
from .local_approval import request_decision
from .mcp_catalog import MCPCatalog
from .owner_runtime import OwnerFakeRuntime
from .readiness_gate import IsolationLayout, ReadOnlyScope, aligned_read_only_scope
from .service_approval import ServiceApprovalCore
from .service_execution import FakeServiceExecutor

HELP='''Fake-only commands (no real tools/models):
show | tools | audit | quit
mode ASK_EVERY_TIME
mode AUTO_ALLOW_SAFE enable
mode ALLOW_ALL enable
disable-auto
override time|filesystem MODE [enable]
clear-override time|filesystem
trust get_current_time - safe
trust read_file ROOT|FILE safe
trust list_directory ROOT safe
revoke TRUST_ID
call TOOL [JSON arguments]
ASK prompt: y + Enter = Allow Once; n + Enter = Deny.
ROOT/FILE refer ONLY to the temporary fixture paths displayed at startup.
Trust modes do not create permissions, start a server, or bypass a Proposal.'''


@contextmanager
def fixture_console(path):
    with TemporaryDirectory(prefix='local-ai-policy-cli-') as folder:
        layout=IsolationLayout(Path(folder));layout.root.mkdir();file=layout.root/'test.txt'
        file.write_text('Synthetic approval console fixture',encoding='utf-8')
        configs=(ServerConfig('time',sys.executable,('-m','mcp_server_time','--local-timezone=Asia/Seoul')),
            ServerConfig('fs',sys.executable,(sys.executable,'--read-only','--confine-roots',str(layout.root))))
        runtime=OwnerFakeRuntime(prepared=True)
        service=None
        try:
            runtime.tools=[ToolInfo('get_current_time','Fake time only',{'type':'object','properties':{
                'timezone':{'type':'string','default':'Asia/Seoul'}}},{'type':'object'})]+[
                ToolInfo(n,'Fake filesystem only',{'type':'object','properties':{'path':{'type':'string'}},'required':['path']},{'type':'object'})
                for n in ('read_file','list_directory')]
            service=layout.service(runtime,configs);contracts={};ids={}
            for config,kind in zip(configs,('time','filesystem')):
                sid=service.create_server(config.server_id,kind)['server_id'];ids[kind]=sid
                scope=aligned_read_only_scope(ReadOnlyScope(sid,kind,layout.root if kind=='filesystem' else None),config)
                contracts[sid]=scope;runtime.manager._client_factory.bind(scope);service.start_server(sid)
                for tool in scope.tools:service.set_permission(sid,tool,True)
            policy=ApprovalPolicy(path,contracts=contracts)
            catalog=MCPCatalog(service)
            core=ServiceApprovalCore(catalog,FakeServiceExecutor(service),policy=policy)
            yield policy,catalog,core,runtime,ids,layout.root,file
        finally:
            if service is not None:service.shutdown()
            else:runtime.shutdown()


async def call_fixture(core,entry,args,*,reader=None,output=print):
    session=uuid4().hex;core.create_session(session)
    def model(messages,info):
        if any(getattr(p,'part_kind','')=='tool-return' for m in messages for p in m.parts):
            return ModelResponse(parts=[TextPart('Synthetic fixture result; not real time or file data')])
        return ModelResponse(parts=[ToolCallPart(entry.alias,args,tool_call_id='policy-cli')])
    turn=DeferredFixtureRun(core,session,FunctionModel(model));await turn.start('Fake approval test')
    proposal=turn.proposals[0]
    decision=core.apply_policy(turn.run_id,proposal['proposal_id']);output(json.dumps(decision.to_dict()))
    if decision.action=='ASK':
        kwargs={'output':output}
        if reader is not None:kwargs['reader']=reader
        await request_decision(turn,proposal['proposal_id'],**kwargs)
    if decision.action!='BLOCK':await turn.resume()
    result={'run':core.status(turn.run_id),'answer':turn.answer,
            'operations':[core.operation(oid) for oid,record in core._operations.items() if record['run_id']==turn.run_id]}
    output(json.dumps(result,default=str));return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path,required=True)
    args=parser.parse_args()
    with fixture_console(args.policy) as (policy,catalog,core,runtime,ids,root,file):
        print('FAKE ONLY. ROOT:',root,'FILE:',file);print(HELP)
        while True:
            try:
                line=input('policy> ').strip()
                if not line:continue
                command,_,rest=line.partition(' ')
                entries={e.original_name:e for e in catalog.discover()}
                parts=shlex.split(rest,posix=False)
                if command in ('quit','exit'):break
                if command=='show':print(json.dumps(policy.snapshot(),indent=2))
                elif command=='help':print(HELP)
                elif command=='audit':print(json.dumps(policy.audit_log(),indent=2))
                elif command=='mode':policy.set_mode(parts[0],enable_auto=len(parts)==2 and parts[1]=='enable')
                elif command=='disable-auto':policy.disable_auto()
                elif command=='override':policy.set_mode(parts[1],server_id=ids[parts[0]],enable_auto=len(parts)==3 and parts[2]=='enable')
                elif command=='clear-override':policy.clear_override(ids[parts[0]])
                elif command=='trust':
                    resource={'ROOT':root,'FILE':file,'-':None}[parts[1]]
                    if len(parts)!=3 or parts[2] not in ('safe','all'):raise ValueError('Explicit safe/all review required')
                    print('trust_id:',policy.grant_trust(entries[parts[0]],resource=resource,verified_read_only=parts[2]=='safe'))
                elif command=='revoke':policy.revoke_trust(parts[0])
                elif command=='tools':
                    for name,entry in entries.items():
                        sample={} if name=='get_current_time' else {'path':'.' if name=='list_directory' else 'test.txt'}
                        print(name,json.dumps(sample),json.dumps(policy.evaluate(entry,sample).to_dict()))
                elif command=='call':
                    name,_,raw=rest.partition(' ')
                    sample={} if name=='get_current_time' else {'path':'.' if name=='list_directory' else 'test.txt'}
                    asyncio.run(call_fixture(core,entries[name],json.loads(raw) if raw else sample))
                else:print('Unknown command; type help')
            except (EOFError,KeyboardInterrupt):break
            except Exception as error:print(type(error).__name__,str(error))


if __name__=='__main__':main()

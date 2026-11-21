"""Separately approved C-4B experiment; local user decides every real call."""
from .offline import install, VIOLATIONS
install()

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.providers.ollama import OllamaProvider

from mcp_manager.models import ServerConfig
from mcp_manager.service import MCPService
from .approved_bridge import DeferredFixtureRun
from .c4_r1 import BASE, ROOT, NODE, SCRIPT, FIXTURE_SHA, SOURCE_HASHES
from .c4b_selection import OUT, MODELS, QUESTIONS, api, configured_model, instructions, settings
from .live_transport import LaunchReview, ReviewedStdioRuntime
from .local_approval import request_decision
from .mcp_catalog import MCPCatalog
from .readiness_gate import ReadOnlyScope, IsolationLayout, no_reparse, ReadinessDenied, aligned_read_only_scope
from .service_approval import ServiceApprovalCore
from .service_execution import FakeServiceExecutor

TIME_PYTHON = Path(r'F:\LLM\MCP_Servers\time-env\Scripts\python.exe')
TIME_MODULE = TIME_PYTHON.parent.parent/'Lib/site-packages/mcp_server_time/__init__.py'
CASES = ((5, QUESTIONS[4]), (6, QUESTIONS[5]), (8, QUESTIONS[7]),
         (11, f'Filesystem MCP로 {ROOT / "test.txt"}를 읽어줘.'))


def profiles():
    return (ServerConfig('c4b-time', str(TIME_PYTHON),
                ('-m', 'mcp_server_time', '--local-timezone=Asia/Seoul'), str(TIME_PYTHON.parent.parent)),
            ServerConfig('c4b-filesystem', str(NODE),
                (str(SCRIPT), '--read-only', '--confine-roots', str(ROOT)), str(NODE.parent)))


def resource_snapshot():
    """Point-in-time system memory and Ollama allocation, not process attribution/peaks."""
    import ctypes
    class Memory(ctypes.Structure):
        _fields_ = [('length',ctypes.c_ulong),('load',ctypes.c_ulong)] + [
            (name,ctypes.c_ulonglong) for name in ('total','available','page_total','page_available',
                                               'virtual_total','virtual_available','extended')]
    memory=Memory(); memory.length=ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError('Cannot sample memory')
    return {'system_ram_used_bytes':memory.total-memory.available,
            'system_ram_total_bytes':memory.total, 'ollama':api('ps')}


async def run(*, approved_start=False, aligned_contract=False):
    if not approved_start: raise PermissionError('Separate Time/Filesystem start approval required')
    if sys.platform != 'win32' or not sys.stdin.isatty():
        raise PermissionError('Use your interactive local Windows PowerShell')
    IsolationLayout(BASE)
    for path, expected in SOURCE_HASHES.items():
        no_reparse(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected: raise RuntimeError('Server source changed')
    for path in (TIME_PYTHON, TIME_MODULE, ROOT/'test.txt'): no_reparse(path)
    if hashlib.sha256((ROOT/'test.txt').read_bytes()).hexdigest() != FIXTURE_SHA:
        raise RuntimeError('Fixture changed')
    data=OUT/('live-contract-c4c2a' if aligned_contract else 'live'); data.mkdir(exist_ok=False)
    events=[]
    def emit(event, **fields):
        row={'event':event,'utc':datetime.now(timezone.utc).isoformat(), 'monotonic':time.monotonic(),**fields}
        events.append(row)
        (data/'events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps(row,ensure_ascii=False,default=str),flush=True)
    config=profiles()
    review=LaunchReview(config,base=BASE,live=True,reviewed_files=(*SOURCE_HASHES,TIME_PYTHON,TIME_MODULE))
    runtime=ReviewedStdioRuntime(review,wait_seconds=30)
    service=None
    try:
        service=MCPService(database_path=data/'mcp.sqlite3',session_database_path=data/'sessions.sqlite3',
            approved_configs=config,runtime_factory=lambda:runtime)
        scopes={}
        emit('helper',pid=os.getpid(),profiles=[{'executable':p.executable,'args':p.args,'cwd':p.cwd} for p in config],
             root=str(ROOT),approval_seconds=120,run_seconds=180,call_seconds=30)
        for profile,kind in zip(config,('time','filesystem')):
            sid=service.create_server(profile.server_id,'C4B isolated '+kind)['server_id']
            scope=ReadOnlyScope(sid,kind,ROOT if kind=='filesystem' else None)
            if aligned_contract: scope=aligned_read_only_scope(scope,profile)
            scopes[sid]=scope; runtime.manager._client_factory.bind(scope)
            await asyncio.to_thread(service.start_server,sid)
            tools=await asyncio.to_thread(service.list_tools,sid)
            if {t['name'] for t in tools} != scope.tools: raise RuntimeError('Unexpected discovered tools')
            for tool in tools: await asyncio.to_thread(service.set_permission,sid,tool['name'],True)
            emit('discovery',server_id=sid,protocol=runtime.handshakes[sid],tools=tools,attempts=runtime.attempts)
        catalog=MCPCatalog(service)
        catalog.discover()
        for model_name in MODELS:
            try:
                for case_id,question in CASES:
                    requests,responses=[],[]
                    async def request_hook(request):
                        if str(request.url)!='http://127.0.0.1:11434/v1/chat/completions':
                            raise PermissionError('Unexpected endpoint')
                        requests.append(json.loads(request.content))
                    async def response_hook(response):
                        await response.aread(); responses.append({'http':response.status_code,'body':response.json()})
                    async with httpx2.AsyncClient(trust_env=False,timeout=120,
                        event_hooks={'request':[request_hook],'response':[response_hook]}) as http:
                        client=AsyncOpenAI(base_url='http://127.0.0.1:11434/v1',api_key='local-only-not-a-secret',
                            max_retries=0,http_client=http)
                        model=configured_model(model_name,OllamaProvider(openai_client=client))
                        core=ServiceApprovalCore(catalog,FakeServiceExecutor(service))
                        session=f'{model_name}-{case_id}';core.create_session(session)
                        turn=DeferredFixtureRun(core,session,model,instructions=instructions(ROOT),model_settings=settings(1))
                        started=time.monotonic(); waiting=0.;attempts=runtime.attempts
                        emit('case_started',model=model_name,case_id=case_id,question=question,run_id=turn.run_id)
                        try:
                            await turn.start(question)
                            active=[m for m in api('ps')['models'] if m['name'] in (model_name,model_name+':latest')]
                            if len(active)!=1 or active[0].get('context_length')!=8192:
                                raise RuntimeError('8K runtime context not confirmed')
                            # This supplies decisions only; the official Agent owns tool/model rounds.
                            for _ in range(8):
                                if core.status(turn.run_id)['state']!='WAITING_APPROVAL': break
                                proposals=turn.proposals
                                try:
                                    for proposal in proposals:
                                        entry=next(e for e in catalog.discover() if e.server_id==proposal['server_id']
                                                   and e.original_name==proposal['tool_name'])
                                        scopes[proposal['server_id']].validate(proposal['tool_name'],
                                            json.loads(proposal['arguments_json']),entry.descriptor['input_schema'])
                                except (ReadinessDenied,FileNotFoundError):
                                    core.fail(turn.run_id,'experiment_scope_rejected')
                                    emit('scope_rejected',case_id=case_id,model=model_name,proposals=proposals,
                                         new_sdk_attempts=runtime.attempts-attempts)
                                    break
                                for proposal in proposals:
                                    emit('proposal',case_id=case_id,model=model_name,proposal=proposal,attempts=runtime.attempts)
                                    before=time.monotonic()
                                    allow=await request_decision(turn,proposal['proposal_id'])
                                    waited=time.monotonic()-before; waiting+=waited
                                    emit('local_decision',proposal_id=proposal['proposal_id'],allow=allow,waiting_seconds=waited)
                                    if not allow: break
                                await turn.resume()
                            else: raise RuntimeError('Approval round budget exhausted')
                            operations=[core.operation(oid) for oid in core._operations]
                            emit('case_result',case_id=case_id,model=model_name,run=core.status(turn.run_id),
                                answer=turn.answer,operations=operations,requests=requests,responses=responses,
                                elapsed_seconds=time.monotonic()-started,approval_wait_seconds=waiting,
                                new_sdk_attempts=runtime.attempts-attempts,resources=resource_snapshot(),
                                network_violations=list(VIOLATIONS))
                            if core.status(turn.run_id)['unsettled'] or core.status(turn.run_id)['state']=='UNCERTAIN':
                                raise RuntimeError('Unsettled operation; no further model/tool runs permitted')
                        except BaseException as error:
                            turn.cancel()
                            emit('case_error',case_id=case_id,model=model_name,run=core.status(turn.run_id),
                                code=getattr(error,'code',type(error).__name__),message=str(error),
                                requests=requests,responses=responses,attempts=runtime.attempts)
                            raise
            finally: api('generate',{'model':model_name,'keep_alive':0})
    finally:
        if service is not None: await asyncio.to_thread(service.shutdown)
        else: await asyncio.to_thread(runtime.shutdown)
        emit('closed',attempts=runtime.attempts,host_running=runtime.manager._host.running,
             network_violations=list(VIOLATIONS))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-start',action='store_true')
    parser.add_argument('--aligned-contract',action='store_true')
    args=parser.parse_args()
    if not args.approved_start: parser.error('Separate server-start approval required')
    asyncio.run(run(approved_start=True,aligned_contract=args.aligned_contract))


if __name__=='__main__': main()

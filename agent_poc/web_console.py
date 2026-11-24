"""Explicit isolated Console host. No start on import; no operating fallback."""
import argparse
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path

from .live_transport import LaunchReview, ReviewedStdioRuntime
from .mcp_management import MCPManagement, experimental_path
from .readiness_gate import IsolationLayout, ReadOnlyScope, aligned_read_only_scope
from .web_backend import AgentWebBackend
from .c4d_smoke import process_snapshot, descendants
from mcp_manager.models import ServerConfig
from mcp_manager.registry import fingerprint, config_data

MODELS={'local-ai-c4b-gemma4-8k':'gemma4:12b','local-ai-c4b-qwen35-8k':'qwen3.5:9b'}


@asynccontextmanager
async def local_model(initial,*,name,diagnostic_path=None):
    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.providers.ollama import OllamaProvider
    if name not in MODELS:raise ValueError('Reviewed installed model profile required')
    async def check(request):
        if str(request.url)!='http://127.0.0.1:11434/v1/chat/completions':
            raise PermissionError('Only local Ollama inference is allowed')
        if diagnostic_path is not None:
            with diagnostic_path.open('a',encoding='utf-8') as log:
                log.write(json.dumps({'event':'model_request','model':name,
                    'body':json.loads(request.content)},ensure_ascii=False)+'\n')
    async def record_response(response):
        if diagnostic_path is not None:
            await response.aread()
            with diagnostic_path.open('a',encoding='utf-8') as log:
                log.write(json.dumps({'event':'model_response','status':response.status_code,
                    'body':response.text},ensure_ascii=False)+'\n')
    async with httpx2.AsyncClient(trust_env=False,timeout=120,event_hooks={'request':[check],'response':[record_response]}) as http:
        client=AsyncOpenAI(base_url='http://127.0.0.1:11434/v1',api_key='local-only',max_retries=0,http_client=http)
        provider=OllamaProvider(openai_client=client)
        source=OllamaModel(MODELS[name],provider=provider)
        profile=dict(source.profile);profile['json_schema_transformer']=None
        model=OllamaModel(name,provider=provider,profile=profile)
        yield model,{'temperature':.2,'top_p':.9,'seed':1,'max_tokens':512,
            'openai_reasoning_effort':'none','extra_body':{'max_tokens':512},'parallel_tool_calls':False}


def registered_console(args,base):
    """Existing Console, empty Registry, no automatic server registration/start."""
    from mcp_manager.manager import MCPManager
    from app import create_app
    manager=MCPManagement(base/'management',runtime_factory=MCPManager,
        session_database_path=base/'sessions.sqlite3')
    backend=None
    try:
        def factory(service,revision,prepare,commit):
            nonlocal backend
            if service is not manager.service:raise ValueError('Shared service required')
            backend=AgentWebBackend(manager,session_revision=revision,prepare=prepare,commit=commit,
                model_factory=lambda initial:local_model(initial,name=args.model,diagnostic_path=base/'model-diagnostic.jsonl'),
                model_name=args.model)
            return backend
        app=create_app(base/'sessions.sqlite3',mcp_existing_service=manager.service,agent_backend_factory=factory)
        print(json.dumps({'event':'ready','url':f'http://127.0.0.1:{args.port}',
            'mode':'registered-console','model':args.model,'base':str(base),'servers_started':0}),flush=True)
        app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
    finally:
        try:
            if backend is not None:backend.shutdown()
        finally:
            try:(base/'audit.json').write_text(json.dumps(manager.audit(),indent=2),encoding='utf-8')
            finally:manager.shutdown()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--time-python',type=Path)
    parser.add_argument('--model',choices=tuple(MODELS),default='local-ai-c4b-gemma4-8k')
    parser.add_argument('--port',type=int,default=5002)
    parser.add_argument('--approved-time-start',action='store_true')
    parser.add_argument('--registered-console',action='store_true',help='Empty dynamic Console; explicit profile approval and Start are still required')
    args=parser.parse_args()
    if args.registered_console and args.approved_time_start:parser.error('Choose registered Console or the bounded Time pilot')
    if not args.registered_console and (not args.approved_time_start or args.time_python is None):
        parser.error('Separate explicit Time process-start approval and --time-python required')
    if not 1024<=args.port<=65535:parser.error('Use an unprivileged local port')
    base=experimental_path(args.base);base.mkdir(exist_ok=False)
    if args.registered_console:
        registered_console(args,base);return
    layout=IsolationLayout(base)
    config=ServerConfig('c5a-time',str(args.time_python),('-m','mcp_server_time','--local-timezone=Asia/Seoul'),str(args.time_python.parent))
    review=LaunchReview([config],base=layout.base,live=True,reviewed_files=(args.time_python,))
    runtime=ReviewedStdioRuntime(review,wait_seconds=30)
    manager=backend=None;owned=set()
    try:
        def bind(runtime,actual,options):
            scope=aligned_read_only_scope(ReadOnlyScope(actual.server_id,'time',allowed_tools=frozenset({'get_current_time'})),actual)
            runtime.manager._client_factory.bind(scope);return scope
        manager=MCPManagement(base/'management',runtime_factory=lambda:runtime,contract_binder=bind,
            session_database_path=base/'sessions.sqlite3')
        sid=manager.add_server('C5A Time',config.executable,list(config.args),cwd=config.cwd,
            confirm=lambda r:r['purpose']=='SAVE_PROFILE_ONLY' and r['config_fingerprint']==fingerprint(config_data(config)))['server_id']
        manager.test_connection(sid,confirm=lambda r:r['purpose']=='START_AND_DISCOVER_ONLY')
        owned=descendants(process_snapshot(),os.getpid())
        manager.set_permission(sid,'get_current_time',True)
        manager.set_mode('ASK_EVERY_TIME',server_id=sid)
        from app import create_app
        def factory(service,revision,prepare,commit):
            nonlocal backend
            if service is not manager.service:raise ValueError('Shared MCPService required')
            backend=AgentWebBackend(manager,session_revision=revision,prepare=prepare,commit=commit,
                model_factory=lambda initial:local_model(initial,name=args.model,diagnostic_path=base/'model-diagnostic.jsonl'),exposed={(sid,'get_current_time')},model_name=args.model,
                max_executions=2)
            return backend
        app=create_app(base/'sessions.sqlite3',mcp_existing_service=manager.service,agent_backend_factory=factory)
        print(json.dumps({'event':'ready','url':f'http://127.0.0.1:{args.port}','server_id':sid,
            'model':args.model,'sdk_attempts':runtime.attempts,'base':str(base)},ensure_ascii=False),flush=True)
        app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
    finally:
        try:
            if backend is not None:backend.shutdown()
        finally:
            try:
                if manager is not None:
                    (base/'audit.json').write_text(json.dumps(manager.audit(),ensure_ascii=False,indent=2),encoding='utf-8')
            finally:
                try:
                    if manager is not None:manager.shutdown()
                finally:runtime.shutdown()
                (base/'cleanup.json').write_text(json.dumps({'sdk_attempts':runtime.attempts,
                    'observed_owned_pids':sorted(owned),'remaining_owned_pids':sorted(owned & set(process_snapshot()))}),encoding='utf-8')


if __name__=='__main__':main()

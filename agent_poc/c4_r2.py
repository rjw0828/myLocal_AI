"""R2 local interactive read_file retest. Start only after separate user approval."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import time

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from mcp_manager.models import ServerConfig
from mcp_manager.service import MCPService
from .approved_bridge import DeferredFixtureRun
from .c4_r1 import BASE, ROOT, NODE, SCRIPT, FIXTURE_SHA, SOURCE_HASHES
from .live_transport import LaunchReview, ReviewedStdioRuntime
from .local_approval import request_decision
from .mcp_catalog import MCPCatalog
from .readiness_gate import IsolationLayout, ReadOnlyScope, no_reparse
from .service_approval import ServiceApprovalCore
from .service_execution import FakeServiceExecutor

DATA = BASE / 'r2-20261119-filesystem'


async def run(*, approved_start=False, retry=False, retry_number=1):
    if not approved_start:
        raise PermissionError('Separate user server-start approval required')
    # Reject redirected or automated stdin before any process or metadata creation.
    import sys
    if sys.platform != 'win32' or not sys.stdin.isatty():
        raise PermissionError('Run from your interactive Windows PowerShell console')
    IsolationLayout(BASE)
    for path, expected in SOURCE_HASHES.items():
        no_reparse(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Reviewed server source changed')
    no_reparse(ROOT / 'test.txt')
    if hashlib.sha256((ROOT/'test.txt').read_bytes()).hexdigest() != FIXTURE_SHA:
        raise RuntimeError('Fixture changed')
    if type(retry_number) is not int or retry_number not in (1, 2):
        raise ValueError('Only explicitly reviewed retry slots are supported')
    data = DATA.with_name(DATA.name + f'-retry{retry_number}') if retry else DATA
    data.mkdir(exist_ok=False)  # Never overwrite/reuse an earlier Run or Registry.
    events = []
    def emit(event, **fields):
        row = dict(event=event, utc=datetime.now(timezone.utc).isoformat(),
                   monotonic=time.monotonic(), **fields)
        events.append(row)
        (data/'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
    config = ServerConfig('c4-r2-filesystem', str(NODE),
        (str(SCRIPT), '--read-only', '--confine-roots', str(ROOT)), str(NODE.parent))
    review = LaunchReview([config], base=BASE, live=True, reviewed_files=tuple(SOURCE_HASHES))
    runtime = ReviewedStdioRuntime(review, wait_seconds=30)
    service = None
    try:
        emit('helper', pid=os.getpid(), data=str(data), executable=config.executable,
             args=config.args, cwd=config.cwd, approval_seconds=120, run_seconds=180, call_seconds=30)
        service = MCPService(session_database_path=data/'sessions.sqlite3', database_path=data/'mcp.sqlite3',
            approved_configs=[config], runtime_factory=lambda: runtime)
        sid = service.create_server(config.server_id, 'R2 read_file only')['server_id']
        runtime.manager._client_factory.bind(ReadOnlyScope(sid, 'filesystem', ROOT, frozenset({'read_file'})))
        await asyncio.to_thread(service.start_server, sid)
        tools = await asyncio.to_thread(service.list_tools, sid)
        if {t['name'] for t in tools} != {'read_file'}: raise RuntimeError('Unexpected exposed tools')
        emit('started', server_id=sid, protocol=runtime.handshakes[sid], attempts=runtime.attempts)
        await asyncio.to_thread(service.set_permission, sid, 'read_file', True)
        catalog = MCPCatalog(service)
        entries = catalog.discover()
        if len(entries) != 1: raise RuntimeError('Unexpected Catalog')
        entry = entries[0]
        arguments = {'path': str(ROOT/'test.txt')}
        def model(messages, info):
            returned = [p for m in messages for p in m.parts if getattr(p, 'part_kind', '') == 'tool-return']
            if returned:
                return ModelResponse(parts=[TextPart(json.dumps(returned[-1].content, ensure_ascii=False, default=str))])
            return ModelResponse(parts=[ToolCallPart(entry.alias, arguments, tool_call_id='r2-read-file')])
        core = ServiceApprovalCore(catalog, FakeServiceExecutor(service))
        core.create_session('r2-file')
        turn = DeferredFixtureRun(core, 'r2-file', FunctionModel(model))
        await turn.start('Read only the exact approved fixture; no other operations.')
        proposal = turn.proposals[0]
        issued = time.monotonic()
        emit('proposal', proposal=proposal, run=core.status(turn.run_id), attempts=runtime.attempts)
        if runtime.attempts != 0: raise RuntimeError('Unexpected execution before approval')
        allow = await request_decision(turn, proposal['proposal_id'])
        decided = time.monotonic()
        emit('local_decision', allow=allow, proposal_wait_seconds=decided-issued)
        await turn.resume()
        operations = [core.operation(oid) for oid in core._operations]
        emit('result', run=core.status(turn.run_id), proposals=turn.proposals,
             operations=operations, answer=turn.answer, attempts=runtime.attempts,
             execution_seconds=time.monotonic()-decided,
             fixture_sha256=hashlib.sha256((ROOT/'test.txt').read_bytes()).hexdigest())
        if allow:
            # Deterministic FunctionModel serializes the original ToolReturn;
            # do not mistake any arbitrary final model claim for file evidence.
            if core.status(turn.run_id)['state'] != 'COMPLETED' or runtime.attempts != 1:
                raise RuntimeError('Read did not complete exactly once')
            result = operations[0].get('result') if len(operations) == 1 else None
            if not result or result.get('is_error') or operations[0]['state'] != 'COMPLETED':
                raise RuntimeError('Missing successful operation evidence')
            texts = [p['text'] for p in result['content'] if p.get('type') == 'text']
            if texts != [(ROOT/'test.txt').read_text(encoding='utf-8')]:
                raise RuntimeError('Fixture result mismatch')
        elif runtime.attempts != 0:
            raise RuntimeError('Unexpected execution after Deny')
    except BaseException as error:
        emit('stopped_on_error', code=getattr(error, 'code', type(error).__name__), message=str(error))
        raise
    finally:
        if service is not None: await asyncio.to_thread(service.shutdown)
        else: await asyncio.to_thread(runtime.shutdown)
        emit('closed', attempts=runtime.attempts, host_running=runtime.manager._host.running)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-start', action='store_true')
    parser.add_argument('--retry', action='store_true', help='Separate approved restart; preserve previous evidence')
    parser.add_argument('--retry-number', type=int, choices=(1, 2), default=1)
    options = parser.parse_args()
    if not options.approved_start: parser.error('Separate user server-start approval required')
    asyncio.run(run(approved_start=True, retry=options.retry, retry_number=options.retry_number))


if __name__ == '__main__': main()

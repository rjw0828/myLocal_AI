"""Filesystem-only retest harness. Start flag requires separate recorded user approval.

No process/files are created on import or --help. Tool Allow is a separate queue
decision against the fresh pending Proposal; no old C-4 command is consumed.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from mcp_manager.models import ServerConfig
from mcp_manager.service import MCPService
from .live_transport import LaunchReview, ReviewedStdioRuntime
from .readiness_gate import ReadOnlyScope, IsolationLayout, no_reparse
from .mcp_catalog import MCPCatalog
from .service_execution import FakeServiceExecutor
from .service_approval import ServiceApprovalCore
from .approved_bridge import DeferredFixtureRun


BASE = Path(r'C:\Users\Rohn\AppData\Local\Temp\local-ai-c4-20261119-readonly')
ROOT = BASE/'workspace'
DATA = BASE/'r1-20261119-filesystem'
NODE = Path(r'E:\Workspace\node\node.exe')
SCRIPT = Path(r'F:\LLM\MCP_Servers\filesystem\node_modules\@modelcontextprotocol\server-filesystem\dist\index.js')
FIXTURE_SHA = '4f7bde2dc126d620893fcb4c6f90d4ca50f3c19f7f195f6315986768c68455af'
SOURCE_HASHES = {
    NODE: '1c331340fa6cac5e4294725959070a143fa0507195a78ad77eed7943f719aac9',
    SCRIPT: '44715d2c53dfd9005a03ae4c46d8ccd6632c39af138c99de71a496d119b93352',
    SCRIPT.parent/'server.js': 'eb145c8a9d88a050f0ac85d7e544eb009076c08dd6f9c3179492fcf4decaaeca',
    SCRIPT.parent/'lib.js': 'f034ed05d60189cae1e066ccab834398d2e8adaf206c9003e529000b7965d4d0',
}


def policy(proposal_seconds, run_seconds):
    if (proposal_seconds, run_seconds) not in {(120, 180), (600, 900)}:
        raise ValueError('Only original policy or explicitly reviewed R1-only policy is supported')
    return {'approval_seconds': proposal_seconds, 'run_seconds': run_seconds, 'call_seconds': 30}


class Retest:
    def __init__(self, settings):
        self.settings, self.turns, self.events = settings, {}, []
        self.runtime = self.service = None
        self.owns_data = False

    def emit(self, event, **data):
        record = {'event': event, 'observed_utc': datetime.now(timezone.utc).isoformat(),
                  'observed_monotonic': time.monotonic(), **data}
        self.events.append(record)
        (DATA/'events.json').write_text(json.dumps(self.events, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=False, default=str), flush=True)

    async def start(self, approved):
        if not approved: raise PermissionError('No explicit operator-approved start')
        IsolationLayout(BASE); no_reparse(ROOT); no_reparse(ROOT/'test.txt')
        for path, expected in SOURCE_HASHES.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError('Reviewed server source/executable changed')
        if hashlib.sha256((ROOT/'test.txt').read_bytes()).hexdigest() != FIXTURE_SHA:
            raise RuntimeError('Original harmless fixture changed')
        DATA.mkdir(exist_ok=False); self.owns_data = True; (DATA/'commands').mkdir()
        config = ServerConfig('c4-r1-filesystem', str(NODE),
            (str(SCRIPT), '--read-only', '--confine-roots', str(ROOT)), str(NODE.parent))
        self.emit('helper', pid=os.getpid(), settings=self.settings, root=str(ROOT), data=str(DATA),
                  executable=config.executable, args=config.args, cwd=config.cwd)
        review = LaunchReview([config], base=BASE, live=True, reviewed_files=tuple(SOURCE_HASHES))
        self.runtime = ReviewedStdioRuntime(review, wait_seconds=30)
        self.service = MCPService(session_database_path=DATA/'sessions.sqlite3', database_path=DATA/'mcp.sqlite3',
                                  approved_configs=[config], runtime_factory=lambda: self.runtime)
        row = self.service.create_server(config.server_id, 'R1 Filesystem only'); self.sid = row['server_id']
        self.runtime.manager._client_factory.bind(ReadOnlyScope(self.sid, 'filesystem', ROOT))
        await asyncio.to_thread(self.service.start_server, self.sid)
        tools = await asyncio.to_thread(self.service.list_tools, self.sid)
        if {t['name'] for t in tools} != {'list_directory', 'read_file'}:
            raise RuntimeError('Required read-only tool set was not discovered')
        self.emit('started', server_id=self.sid, protocol=self.runtime.handshakes[self.sid],
                  tools=tools, attempts=self.runtime.attempts)

    async def propose(self, case):
        if case not in {'directory', 'file'} or case in self.turns:
            raise ValueError('Only a fresh sequential case may be proposed')
        if case == 'file' and ('directory' not in self.turns or
                self.turns['directory'].core.status(self.turns['directory'].run_id)['state'] != 'COMPLETED'):
            raise ValueError('Directory must complete before File Run is created')
        tool = 'list_directory' if case == 'directory' else 'read_file'
        args = {'path': str(ROOT if case == 'directory' else ROOT/'test.txt')}
        await asyncio.to_thread(self.service.set_permission, self.sid, tool, True)
        catalog = MCPCatalog(self.service)
        entry = next(e for e in catalog.discover() if e.server_id == self.sid and e.original_name == tool)
        core = ServiceApprovalCore(catalog, FakeServiceExecutor(self.service), **self.settings)
        core.create_session('r1-'+case)
        def model(messages, info):
            returned = [p for m in messages for p in m.parts if getattr(p, 'part_kind', '') == 'tool-return']
            if returned: return ModelResponse(parts=[TextPart(json.dumps(returned[-1].content, ensure_ascii=False, default=str))])
            return ModelResponse(parts=[ToolCallPart(entry.alias, args, tool_call_id='r1-'+case)])
        turn = DeferredFixtureRun(core, 'r1-'+case, FunctionModel(model)); self.turns[case] = turn
        self.emit('run_created', case=case, run=core.status(turn.run_id),
                  run_created_monotonic=core.status(turn.run_id)['deadline']-self.settings['run_seconds'])
        await turn.start('Run only the one explicitly specified tool request.')
        self.emit('proposal', case=case, proposals=turn.proposals, run=core.status(turn.run_id),
                  proposal_created_monotonic=turn.proposals[0]['expires_at']-self.settings['approval_seconds'],
                  attempts=self.runtime.attempts)

    async def decide(self, command):
        turn = self.turns[command['case']]
        # Chat click time is not available. This is the command receipt/apply
        # boundary, recorded explicitly instead of presenting it as click time.
        self.emit('decision_received', case=command['case'], proposal_id=command['proposal_id'],
                  allow=command['allow'], user_click_time=None, run=turn.core.status(turn.run_id))
        turn.decide(command['proposal_id'], command['allow'])
        await turn.resume()
        self.emit('result', case=command['case'], run=turn.core.status(turn.run_id), answer=turn.answer,
                  operations=[turn.core.operation(oid) for oid in turn.core._operations], attempts=self.runtime.attempts)

    async def run(self, approved):
        try:
            await self.start(approved)
            started = time.monotonic()
            while time.monotonic()-started < 1800:
                files = sorted((DATA/'commands').glob('*.json'))
                if not files: await asyncio.sleep(.2); continue
                path = files[0]; command = json.loads(path.read_text(encoding='utf-8'))
                path.rename(path.with_suffix('.claimed'))
                if command['action'] == 'shutdown': break
                if command['action'] == 'propose': await self.propose(command['case'])
                elif command['action'] == 'decide': await self.decide(command)
                elif command['action'] == 'status':
                    self.emit('status', attempts=self.runtime.attempts,
                        turns={k:t.core.status(t.run_id) for k,t in self.turns.items()})
                else: raise ValueError('Unsupported reviewed command')
        except Exception as error:
            if self.owns_data: self.emit('stopped_on_error', code=getattr(error,'code',type(error).__name__), message=str(error))
            raise
        finally:
            if self.service is not None: await asyncio.to_thread(self.service.shutdown)
            if self.runtime is not None:
                self.emit('closed', attempts=self.runtime.attempts, host_running=self.runtime.manager._host.running)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-start', action='store_true')
    parser.add_argument('--proposal-seconds', type=int, default=120)
    parser.add_argument('--run-seconds', type=int, default=180)
    args = parser.parse_args()
    settings = policy(args.proposal_seconds, args.run_seconds)
    if not args.approved_start: parser.error('Separate user start approval required')
    asyncio.run(Retest(settings).run(True))


if __name__ == '__main__': main()

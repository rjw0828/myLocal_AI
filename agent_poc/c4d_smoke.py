"""Bounded approval-mode experiment using existing management/runtime APIs.

No LLM or Agent loop. Live entry requires separately reviewed start/auto scope
and interactive Allow Once for ASK. Never retry or restart failed operations.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from mcp_manager.models import ServerConfig, ToolInfo, ToolCallResult
from mcp_manager.registry import config_data, fingerprint
from .live_transport import LaunchReview, ReviewedStdioRuntime
from .mcp_management import MCPManagement, experimental_path
from .readiness_gate import IsolationLayout, ReadOnlyScope, aligned_read_only_scope

FIXTURE='Local AI Lab C4D fixture, value=314159\n'


def process_snapshot():
    """Public Windows process metadata only; never terminate by name."""
    if sys.platform!='win32':return {}
    import ctypes
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',w.DWORD),('cntUsage',w.DWORD),('pid',w.DWORD),
            ('heap',ctypes.c_size_t),('module',w.DWORD),('threads',w.DWORD),
            ('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('name',w.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    kernel.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:raise OSError('Cannot inspect owned processes')
    result={};entry=Entry();entry.dwSize=ctypes.sizeof(entry)
    try:
        more=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while more:
            result[int(entry.pid)]=int(entry.parent)
            more=kernel.Process32NextW(handle,ctypes.byref(entry))
    finally:kernel.CloseHandle(handle)
    return result


def descendants(snapshot,parent):
    owned=set();parents={parent}
    while True:
        found={pid for pid,ppid in snapshot.items() if ppid in parents and pid not in owned}
        if not found:return owned
        owned.update(found);parents=found


async def run_smoke(base, python, node, script, *, live=False, approved_start=False,
                    approved_auto_scope=False, ask=None, output=print):
    if live and not (approved_start is True and approved_auto_scope is True):
        raise PermissionError('Separate explicit server-start AND exact auto-execution scope approval required')
    if live and ask is None:raise PermissionError('Interactive ASK approval channel required')
    base=experimental_path(base)
    # Each invocation has fresh profiles, DBs, Runs and Proposals. No reuse.
    base.mkdir(exist_ok=False)
    layout=IsolationLayout(base);layout.root.mkdir()
    file=layout.root/'test.txt';file.write_text(FIXTURE,encoding='utf-8',newline='\n')
    configs=(ServerConfig('c4d-time',str(python),('-m','mcp_server_time','--local-timezone=Asia/Seoul'),str(Path(python).parent)),
        ServerConfig('c4d-filesystem',str(node),(str(script),'--read-only','--confine-roots',str(layout.root)),str(Path(node).parent)))
    source_files=tuple(set(Path(c.executable) for c in configs)|{Path(script)})
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    review=LaunchReview(configs,base=base,live=live,reviewed_files=source_files)
    runtime=ReviewedStdioRuntime(review,wait_seconds=30)
    if not live:
        runtime.tools=[ToolInfo('get_current_time','Memory time',{'type':'object','properties':{
            'timezone':{'type':'string','default':'Asia/Seoul'}}})]+[
            ToolInfo(n,'Memory file',{'type':'object','properties':{'path':{'type':'string'}},'required':['path']})
            for n in ('list_directory','read_file')]
    kinds={fingerprint(config_data(config)):kind for config,kind in zip(configs,('time','filesystem'))}
    def bind(runtime,config,options):
        kind=kinds[fingerprint(config_data(config))]
        scope=aligned_read_only_scope(ReadOnlyScope(config.server_id,kind,
            layout.root if kind=='filesystem' else None,
            frozenset({'get_current_time'} if kind=='time' else {'read_file'})),config)
        runtime.manager._client_factory.bind(scope);return scope
    manager=None;owned=set();report={'live':live,'calls':[],'source_sha256':hashes,
        'fixture_sha256':hashlib.sha256(FIXTURE.encode()).hexdigest(),'status':'INCOMPLETE'}
    def emit(event,**fields):
        output(json.dumps({'event':event,'utc':datetime.now(timezone.utc).isoformat(),**fields},ensure_ascii=False,default=str))
    def save():
        (base/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    try:
        manager=MCPManagement(base/'management',runtime_factory=lambda:runtime,contract_binder=bind)
        ids={}
        for config,kind in zip(configs,('time','filesystem')):
            server=manager.add_server('C4D '+kind,config.executable,list(config.args),cwd=config.cwd,
                root=layout.root if kind=='filesystem' else None,confirm=lambda r:r['purpose']=='SAVE_PROFILE_ONLY')
            sid=server['server_id'];ids[kind]=sid
            result=manager.test_connection(sid,confirm=lambda r:r['config_fingerprint'] in kinds
                and r['purpose']=='START_AND_DISCOVER_ONLY')
            owned.update(descendants(process_snapshot(),os.getpid()))
            expected={'get_current_time'} if kind=='time' else {'read_file'}
            if {t['name'] for t in result['tools']}!=expected:raise RuntimeError('Unexpected exposed tools')
            if runtime.attempts!=0:raise RuntimeError('Tool execution before approval')
            emit('discovered',server_id=sid,kind=kind,protocol=runtime.handshakes[sid],tools=sorted(expected),attempts=runtime.attempts)
            manager.set_permission(sid,next(iter(expected)),True)
        report['servers']=ids;report['protocols']=dict(runtime.handshakes)

        cases=[('ASK_EVERY_TIME','time','get_current_time',{'timezone':'Asia/Seoul'}),
               ('AUTO_ALLOW_SAFE','time','get_current_time',{'timezone':'Asia/Seoul'}),
               ('ALLOW_ALL','filesystem','read_file',{'path':str(file)})]
        for mode,kind,tool,args in cases:
            sid=ids[kind]
            if mode!='ASK_EVERY_TIME':
                # This callback consumes the separately user-reviewed exact
                # experiment scope; it is not a generic production auto-Allow.
                resource=layout.root if kind=='filesystem' else None
                manager.trust(sid,tool,resource=resource,verified_read_only=mode=='AUTO_ALLOW_SAFE',
                    confirm=lambda r:r['server_id']==sid and r['tool']==tool and r['resource']==(str(resource) if resource else None))
            manager.set_mode(mode,server_id=sid,enable_auto=mode!='ASK_EVERY_TIME')
            if not live:
                text=json.dumps({'timezone':'Asia/Seoul','datetime':'2026-11-19T09:30:00+09:00'}) if kind=='time' else FIXTURE
                runtime.result=ToolCallResult(({'type':'text','text':text},),None,False)
            before=runtime.attempts;prepared=manager.prepare_tool_test(sid,tool,args)
            expected='ASK' if mode=='ASK_EVERY_TIME' else 'AUTO_APPROVE'
            if prepared['decision']['action']!=expected:raise RuntimeError('Unexpected approval decision')
            if runtime.attempts!=before:raise RuntimeError('Preparation executed a tool')
            emit('proposal',mode=mode,server_id=sid,tool=tool,arguments=args,**prepared)
            decision_started=time.monotonic();approved_at=None
            def consent(request):
                nonlocal approved_at
                exact=(request['server_id']==sid and request['tool']==tool and request['arguments']==args)
                if not exact:return False
                if mode=='ASK_EVERY_TIME':
                    allowed=ask(request) is True if ask is not None else False
                else:allowed=approved_auto_scope is True if live else True
                approved_at=time.monotonic()
                return allowed
            result=await manager.run_tool_test(prepared['test_id'],confirm=consent)
            ended=time.monotonic();test=manager._tests[prepared['test_id']]
            core=test['core'];proposal=core.proposal(test['run'],test['proposal'])
            record={'mode':mode,'server_id':sid,'tool':tool,'arguments':args,
                'decision':prepared['decision'],'proposal_id':proposal['proposal_id'],
                'proposal_state':proposal['state'],'approval_method':proposal.get('approval_method'),
                'sdk_attempts':runtime.attempts-before,'approval_wait_seconds':(approved_at or ended)-decision_started,
                'execution_seconds':ended-(approved_at or ended),**result}
            report['calls'].append(record);save();emit('outcome',**record)
            if result['run']['state']!='COMPLETED' or runtime.attempts-before!=1:
                raise RuntimeError('Call denied, expired, failed or unresolved; no automatic retry')
            operation=result['operations'][0] if len(result['operations'])==1 else {}
            if (proposal['state']!='CONSUMED' or not operation.get('service_token_consumed')
                    or not operation.get('proposal_consumed') or operation.get('state')!='COMPLETED'):
                raise RuntimeError('Missing one-use execution evidence')
            tool_result=operation.get('result')
            if not tool_result or tool_result['is_error']:raise RuntimeError('No successful tool result')
            texts=[p['text'] for p in tool_result['content'] if p.get('type')=='text']
            if kind=='filesystem' and texts!=[FIXTURE]:raise RuntimeError('Fixture result mismatch')
            if kind=='time':
                values=[json.loads(t) for t in texts]
                if len(values)!=1 or values[0].get('timezone')!='Asia/Seoul':raise RuntimeError('Unexpected Time result')
                stamp=datetime.fromisoformat(values[0]['datetime'])
                if stamp.utcoffset().total_seconds()!=9*3600:raise RuntimeError('Unexpected timezone offset')
        report['audit']=manager.audit()
        admissions=[r for r in report['audit'] if r['event']=='operation_admitted']
        if len(admissions)!=3 or [r['method'] for r in admissions]!=['MANUAL','AUTO','AUTO']:
            raise RuntimeError('Admission audit mismatch')
        if runtime.attempts!=3 or runtime.sends!=3:raise RuntimeError('Unexpected SDK call count')
        report['status']='PASS'
    except BaseException as error:
        report['status']='FAIL';report['error']=getattr(error,'code',type(error).__name__)
        raise
    finally:
        try:
            if manager is not None:
                try:report['audit']=manager.audit()
                finally:manager.shutdown()
            else:runtime.shutdown()
        finally:
            report['sdk_attempts']=runtime.attempts;report['successful_responses']=runtime.sends
            remaining=owned & set(process_snapshot())
            for _ in range(10):
                if not remaining:break
                await asyncio.sleep(.1);remaining=owned & set(process_snapshot())
            report['observed_owned_pids']=sorted(owned);report['remaining_owned_pids']=sorted(remaining)
            report['fixture_unchanged']=file.read_text(encoding='utf-8')==FIXTURE
            if remaining or not report['fixture_unchanged']:report['status']='FAIL'
            save();emit('finished',status=report['status'],sdk_attempts=runtime.attempts,remaining_owned_pids=sorted(remaining),report=str(base/'result.json'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base','python','node','script'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--approved-start',action='store_true')
    parser.add_argument('--approved-auto-scope',action='store_true')
    args=parser.parse_args()
    if not args.approved_start or not args.approved_auto_scope:
        parser.error('Separate start and exact auto scope approval required')
    if sys.platform!='win32' or not sys.stdin.isatty():parser.error('Interactive local Windows PowerShell required')
    def ask(review):
        print('ASK: 위 Proposal의 정확한 시간 조회 1회를 허용할까요? y=Allow Once, 나머지=Deny',flush=True)
        try:return input('y/n: ').strip().lower()=='y'
        except (EOFError,KeyboardInterrupt):return False
    try:
        report=asyncio.run(run_smoke(args.base,args.python,args.node,args.script,live=True,
            approved_start=args.approved_start,approved_auto_scope=args.approved_auto_scope,ask=ask))
        return 0 if report['status']=='PASS' else 1
    except Exception as error:
        print('실측 중단:',getattr(error,'code',type(error).__name__),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
